"""Remote Files tab and worker lifecycle. All widget access stays on Tk's thread."""
import posixpath
import threading
import time
from tkinter import ttk, filedialog
import tkinter as tk

from . import remote_files


class RemoteFilesTab:
    def __init__(self, app):
        self.app = app
        self.frame = ttk.Frame(app.notebook, padding=10)
        app.notebook.add(self.frame, text='Remote Files')
        self.directory = '/'
        self.entries = {}
        self.busy = False
        self.generation = 0
        self.path = tk.StringVar(value='/')
        self.status = tk.StringVar(value='Connect on the Connection tab, then Refresh to browse the satellite SD card.')
        self.progress = tk.DoubleVar(value=0)

        bar = ttk.Frame(self.frame)
        bar.pack(fill=tk.X)
        ttk.Label(bar, text='SD path:').pack(side=tk.LEFT)
        self.path_entry = ttk.Entry(bar, textvariable=self.path)
        self.path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.path_entry.bind('<Return>', lambda event: self.refresh())
        actions = ttk.Frame(self.frame)
        actions.pack(fill=tk.X, pady=(8, 4))
        self.buttons = []
        for label, command in [('Refresh / Open', self.refresh), ('Up', self.up),
                               ('Download...', self.download_selected)]:
            button = ttk.Button(actions, text=label, command=command)
            button.pack(side=tk.LEFT, padx=(0, 6))
            self.buttons.append(button)
        self.cancel_button = ttk.Button(actions, text='Cancel', command=self.cancel, state=tk.DISABLED)
        self.cancel_button.pack(side=tk.LEFT)
        ttk.Progressbar(self.frame, variable=self.progress, maximum=100).pack(fill=tk.X, pady=4)
        self.status_label = ttk.Label(self.frame, textvariable=self.status, wraplength=450)
        self.status_label.pack(fill=tk.X, pady=(0, 8))
        self.frame.bind('<Configure>', lambda event: self.status_label.configure(wraplength=max(160, event.width-24)))

        listing = ttk.Frame(self.frame)
        listing.pack(fill=tk.BOTH, expand=True)
        listing.rowconfigure(0, weight=1)
        listing.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(listing, columns=('kind', 'size'), selectmode='browse',
                                 style='RemoteFiles.Treeview')
        self.tree.heading('#0', text='Name')
        self.tree.heading('kind', text='Type')
        self.tree.heading('size', text='Bytes')
        self.tree.column('#0', width=300, minwidth=120)
        self.tree.column('kind', width=80, minwidth=65, stretch=False)
        self.tree.column('size', width=110, minwidth=80, anchor=tk.E, stretch=False)
        self.tree.grid(row=0, column=0, sticky='nsew')
        vertical = ttk.Scrollbar(listing, orient=tk.VERTICAL, command=self.tree.yview)
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal = ttk.Scrollbar(listing, orient=tk.HORIZONTAL, command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.bind('<Double-1>', self.open_selected)
        self.tree.bind('<Return>', self.open_selected)
        app.themes.add_listener(self._theme)
        self._theme(app.themes.colors)

    def _theme(self, colors):
        style = self.app.themes.style
        style.configure('RemoteFiles.Treeview', background=colors['field_bg'],
                        fieldbackground=colors['field_bg'], foreground=colors['fg'],
                        bordercolor=colors['border'])
        style.map('RemoteFiles.Treeview',
                  background=[('selected', colors['select_bg'])],
                  foreground=[('selected', colors['select_fg'])])
        style.configure('RemoteFiles.Treeview.Heading', background=colors['surface_alt'],
                        foreground=colors['fg'], relief='flat')
        style.map('RemoteFiles.Treeview.Heading',
                  background=[('active', colors['button_hover'])])

    def _set_busy(self, value):
        self.busy = value
        for button in self.buttons:
            button.configure(state=tk.DISABLED if value else tk.NORMAL)
        self.path_entry.configure(state=tk.DISABLED if value else tk.NORMAL)
        self.cancel_button.configure(state=tk.NORMAL if value else tk.DISABLED)

    def _ready(self):
        if self.busy or self.app._transfer_active.is_set():
            self.status.set('Wait for the current transfer to finish, or cancel it.')
            return False
        if not self.app.is_connected:
            self.status.set('Connect to the XBee serial port on the Connection tab first.')
            return False
        if self.app.test_mode:
            self.status.set('Remote Files requires Lab 8 firmware and a real serial connection; TEST MODE supplies telemetry only.')
            return False
        return True

    def refresh(self):
        if not self._ready():
            return
        path = self.path.get().strip()
        try:
            remote_files.path_hex(path)
        except remote_files.RemoteError as exc:
            self.status.set(str(exc))
            return
        self._start('list', path)

    def up(self):
        if not self._ready():
            return
        self.path.set(posixpath.dirname(self.directory) or '/')
        self.refresh()

    def open_selected(self, event=None):
        if self.busy:
            return
        selection = self.tree.selection()
        if not selection:
            return
        entry = self.entries[selection[0]]
        if entry.directory:
            self.path.set(posixpath.join(self.directory, entry.name))
            self.refresh()
        else:
            self.download_selected()

    def download_selected(self):
        if not self._ready():
            return
        selection = self.tree.selection()
        if not selection or self.entries[selection[0]].directory:
            self.status.set('Select a file to download. Double-click a folder to open it.')
            return
        entry = self.entries[selection[0]]
        destination = filedialog.asksaveasfilename(
            title='Download satellite file', initialdir=self.app.downloads_dir,
            initialfile=entry.name, confirmoverwrite=True)
        # The dialog runs a nested Tk event loop: recheck connection/ownership.
        if destination and self._ready():
            self._start('download', posixpath.join(self.directory, entry.name), destination)

    def _start(self, action, path, destination=None):
        self.generation += 1
        generation = self.generation
        self._set_busy(True)
        self.progress.set(0)
        self.status.set('Contacting satellite...')
        self.app._transfer_cancel = cancel = threading.Event()
        self.app._transfer_active.set()
        port = self.app.serial_connection
        worker = threading.Thread(target=self._worker,
                                  args=(generation, port, cancel, action, path, destination), daemon=True)
        self.app._transfer_thread = worker
        worker.start()

    def _worker(self, generation, port, cancel, action, path, destination):
        def post(event, **info):
            self.app._post_rx(('remote_files', dict(info, event=event, generation=generation)))
        last_progress = 0

        def progress(**info):
            nonlocal last_progress
            now = time.monotonic()
            if now-last_progress >= 0.1:
                post('progress', **info)
                last_progress = now

        result = {'event': 'failed', 'detail': 'Operation did not complete'}
        try:
            with self.app._serial_read_lock:
                if cancel.is_set() or port is None:
                    raise remote_files.Cancelled('Operation cancelled')
                old_timeout, old_write_timeout = port.timeout, port.write_timeout
                try:
                    port.timeout, port.write_timeout = 0.1, 1.0
                    client = remote_files.Client(port, cancel=cancel, progress=progress)
                    if action == 'list':
                        entries = client.list_directory(path)
                        result = dict(event='listed', entries=entries, path=path)
                    else:
                        saved = client.download(path, destination)
                        result = dict(event='downloaded', destination=str(saved))
                finally:
                    port.timeout, port.write_timeout = old_timeout, old_write_timeout
        except remote_files.Cancelled:
            result = dict(event='cancelled', detail='Cancelled. Any incomplete local download was removed.')
        except Exception as exc:
            result = dict(event='failed', detail=str(exc))
        finally:
            self.app._transfer_active.clear()
            post(**result)

    def on_event(self, info):
        if info['generation'] != self.generation:
            return
        event = info['event']
        if event == 'progress':
            if 'done' in info:
                done, total = info['done'], info['total']
                elapsed = info['elapsed']
                rate = done / elapsed if elapsed > 0 else 0
                self.progress.set(100 * done / total if total else 0)
                self.status.set(f'{done:,} / {total:,} bytes | {rate:.0f} bytes/s')
            else:
                self.status.set(info['status'])
            return
        self._set_busy(False)
        # Delayed events after a disconnect cannot restore stale SD listings.
        if not self.app.is_connected:
            self.invalidate()
            return
        if event == 'listed':
            self.tree.delete(*self.tree.get_children())
            self.entries.clear()
            self.directory = info['path']
            self.path.set(self.directory)
            for index, entry in enumerate(sorted(info['entries'], key=lambda e: (not e.directory, e.name.casefold()))):
                key = str(index)
                self.entries[key] = entry
                self.tree.insert('', tk.END, iid=key, text=entry.name,
                                 values=('Folder' if entry.directory else 'File', '' if entry.directory else f'{entry.size:,}'))
            self.status.set(f'{len(self.entries)} entries in {self.directory}. Double-click folders; select files to download.')
        elif event == 'downloaded':
            self.progress.set(100)
            self.status.set('Verified and saved: ' + info['destination'])
            self.app.log_message('Remote file downloaded: ' + info['destination'], 'SYSTEM')
        else:
            self.status.set(info['detail'])
            if event == 'failed':
                self.app.log_message('Remote Files: ' + info['detail'], 'ERROR')

    def cancel(self):
        if self.busy and self.app._transfer_cancel is not None:
            self.app._transfer_cancel.set()
            self.status.set('Cancelling...')

    def invalidate(self):
        self.generation += 1
        self.tree.delete(*self.tree.get_children())
        self.entries.clear()
        self.directory = '/'
        self.path.set('/')
        self.progress.set(0)
        self._set_busy(False)
        self.status.set('Disconnected. Connect and Refresh to read the satellite SD card.')
