"""Remote Files tab and worker lifecycle. All widget access stays on Tk's thread."""

# READING GUIDE - Tk orchestration around a GUI-independent file client.
# The main Tk thread validates the user's action, captures immutable arguments,
# marks the port busy and starts one worker. The worker owns the shared serial
# read lock until the operation and cleanup finish. It reports events through
# app._post_rx; on_event runs later on Tk's thread and is the only widget updater.
#
# The busy flag is local presentation state. app._transfer_active and the shared
# read lock are the cross-feature ownership boundary used by telemetry, Camera
# and ZMODEM. The generation number is a stale-result guard: an event from before
# a disconnect must not repaint a new connection's gallery or operation status.
#
# A protocol retry, a satellite SD read attempt, and a raw radio retransmission
# are distinct mechanisms. Status/JSONL statistics must not present them as one
# counter. Downloads are saved locally only after complete size/CRC agreement;
# viewing is a separate decode step after that verified download.
import posixpath
from pathlib import Path
import threading
import time
from tkinter import ttk, filedialog, simpledialog, messagebox
import tkinter as tk

from . import remote_files
from .transfer_diagnostics import Capture


class RemoteFilesTab:
    def __init__(self, app, title='Remote Files', event_kind='remote_files'):
        self.app = app
        self.event_kind = event_kind
        self.title = title
        self.frame = ttk.Frame(app.notebook, padding=10)
        app.notebook.add(self.frame, text=title)
        self.directory = '/'
        self.entries = {}
        self.busy = False
        self.upload_supported = False
        self.manage_supported = False
        self.generation = 0
        self.path = tk.StringVar(value='/')
        self.status = tk.StringVar(value='Connect on the Connection tab, then Refresh to browse the satellite SD card.')
        self.progress = tk.DoubleVar(value=0)
        # Both file pages share one preference, but every worker gets a frozen
        # string captured on Tk's thread. Never read StringVar from a worker.
        if not hasattr(app, 'transfer_profile_var'):
            saved = app.settings.get('file_transfer_profile', 'auto')
            if saved not in remote_files.PROFILE_LABELS:
                saved = 'auto'
            app.transfer_profile_var = tk.StringVar(value=remote_files.PROFILE_LABELS[saved])
        self.profile_var = app.transfer_profile_var

        bar = ttk.Frame(self.frame)
        bar.pack(fill=tk.X)
        ttk.Label(bar, text='SD path:').pack(side=tk.LEFT)
        self.path_entry = ttk.Entry(bar, textvariable=self.path)
        self.path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.path_entry.bind('<Return>', lambda event: self.refresh())
        actions = ttk.Frame(self.frame)
        actions.pack(fill=tk.X, pady=(8, 4))
        file_actions = ttk.Frame(self.frame)
        file_actions.pack(fill=tk.X, pady=(0, 4))
        self.buttons = []
        self.upload_buttons = []
        self.manage_buttons = []
        for label, command, row in [('Refresh / Open', self.refresh, actions), ('Up', self.up, actions),
                                   ('Download...', self.download_selected, actions), ('Upload...', self.upload_selected, actions),
                                   ('Rename...', self.rename_selected, file_actions), ('Delete...', self.delete_selected, file_actions),
                                   ('Remove partial...', self.remove_partial, file_actions)]:
            button = ttk.Button(row, text=label, command=command)
            button.pack(side=tk.LEFT, padx=(0, 6))
            self.buttons.append(button)
            if label in ('Upload...', 'Remove partial...'):
                self.upload_buttons.append(button)
                button.configure(state=tk.DISABLED)
            if label in ('Rename...', 'Delete...'):
                self.manage_buttons.append(button)
                button.configure(state=tk.DISABLED)
        self.cancel_button = ttk.Button(actions, text='Cancel', command=self.cancel, state=tk.DISABLED)
        self.cancel_button.pack(side=tk.LEFT)
        speed_bar = ttk.Frame(self.frame)
        speed_bar.pack(fill=tk.X, pady=4)
        ttk.Label(speed_bar, text='Transfer profile:').pack(side=tk.LEFT)
        self.profile_combo = ttk.Combobox(speed_bar, state='readonly', textvariable=self.profile_var,
                                         values=list(remote_files.PROFILE_LABELS.values()), width=48)
        self.profile_combo.pack(side=tk.LEFT, padx=6, fill=tk.X, expand=True)
        self.profile_combo.bind('<<ComboboxSelected>>', self._save_profile)
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

    def _profile_key(self):
        selected = self.profile_var.get()
        return next((key for key, label in remote_files.PROFILE_LABELS.items() if label == selected), 'auto')

    def _save_profile(self, event=None):
        self.app._write_settings({'file_transfer_profile': self._profile_key()})

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
        self.profile_combo.configure(state='disabled' if value else 'readonly')
        for button in self.buttons:
            button.configure(state=tk.DISABLED if value else tk.NORMAL)
        if not value and not self.upload_supported:
            for button in self.upload_buttons:
                button.configure(state=tk.DISABLED)
        if not value and not self.manage_supported:
            for button in self.manage_buttons:
                button.configure(state=tk.DISABLED)
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
            self.status.set(self.title+' requires Lab 8 firmware and a real serial connection; TEST MODE supplies telemetry only.')
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

    def upload_selected(self):
        if not self._ready():
            return
        source = filedialog.askopenfilename(title='Choose a file to upload to the satellite')
        if not source or not self._ready():
            return
        folder = self.directory
        name = simpledialog.askstring('Upload to '+folder,
            'Remote filename in '+folder+' (existing files are preserved):',
            initialvalue=Path(source).name, parent=self.app.root)
        if not name or not self._ready():
            return
        if '/' in name or '\\' in name:
            self.status.set('Enter a filename; uploads go into the displayed folder.')
            return
        target = posixpath.join(folder, name)
        try:
            remote_files.upload_path_hex(target)
        except remote_files.RemoteError as exc:
            self.status.set(str(exc)); return
        if any(entry.name.casefold() == name.casefold() for entry in self.entries.values()):
            self.status.set('That filename already exists. Choose a different upload name.')
            return
        self._start('upload', target, source)

    def _selected_file(self):
        selection = self.tree.selection()
        entry = self.entries.get(selection[0]) if selection else None
        if entry is None or entry.directory:
            self.status.set('Select a file; Delete and Rename do not change folders.')
            return None
        if entry.name.casefold().startswith('.kfs-'):
            self.status.set('Use Remove partial for unfinished upload files.')
            return None
        return posixpath.join(self.directory, entry.name)

    def delete_selected(self):
        if not self._ready():
            return
        path = self._selected_file()
        generation, port = self.generation, self.app.serial_connection
        if path and messagebox.askyesno('Delete satellite file',
                'Permanently delete '+path+' from the satellite SD card?',
                default=messagebox.NO, parent=self.app.root) and self._ready():
            if generation != self.generation or port is not self.app.serial_connection:
                self.status.set('Connection or folder changed; select the file again.'); return
            self._start('delete', path)

    def rename_selected(self):
        if not self._ready():
            return
        path = self._selected_file()
        if path is None:
            return
        folder, old_name = posixpath.split(path)
        generation, port = self.generation, self.app.serial_connection
        name = simpledialog.askstring('Rename satellite file',
            'New filename for '+path+':', initialvalue=old_name, parent=self.app.root)
        if not name or not self._ready():
            return
        if generation != self.generation or port is not self.app.serial_connection:
            self.status.set('Connection or folder changed; select the file again.'); return
        if name == old_name:
            self.status.set('Name unchanged.'); return
        if '/' in name or '\\' in name:
            self.status.set('Enter a filename; Rename keeps the current folder.'); return
        target = posixpath.join(folder, name)
        try:
            remote_files.upload_path_hex(target, temporary=False)
        except remote_files.RemoteError as exc:
            self.status.set(str(exc)); return
        if any(entry.name.casefold() == name.casefold() for entry in self.entries.values()):
            self.status.set('That name already exists; choose a different name. Case-only renames are not supported.')
            return
        self._start('rename', path, target)

    def remove_partial(self):
        if not self._ready():
            return
        selection = self.tree.selection()
        entry = self.entries.get(selection[0]) if selection else None
        if entry is None or entry.directory or not remote_files.partial_path(entry.name):
            self.status.set('Select a .kfs-xxxxxxxx.part temporary upload file to remove.')
            return
        path = posixpath.join(self.directory, entry.name)
        if messagebox.askyesno('Remove partial upload', 'Remove unfinished upload '+path+'?', parent=self.app.root) and self._ready():
            self._start('clean', path)

    # Capture Tk state before the worker starts. Set the shared active flag
    # before it competes for the read lock, preventing the normal telemetry
    # reader from taking bytes intended for the new transfer. A fresh generation
    # and cancellation Event identify this operation across asynchronous results.
    def _start(self, action, path, destination=None):
        self.generation += 1
        if action not in ('download', 'upload'):
            self.app.transfer_recorder.event('activity_started', f'{action}: {path}')
        generation = self.generation
        self._set_busy(True)
        self.progress.set(0)
        self.status.set('Contacting satellite...')
        if action in ('delete', 'rename'):
            self.cancel_button.configure(state=tk.DISABLED)
        self.app._transfer_cancel = cancel = threading.Event()
        self.app._transfer_active.set()
        port = self.app.serial_connection
        if hasattr(port, 'set_transfer_cancel'):
            port.set_transfer_cancel(cancel)
        stats = self.app.start_transfer_statistics('KFS1', 'download' if action == 'view' else action, path) if action in ('download', 'upload', 'view') else None
        worker = threading.Thread(target=self._worker,
                                  args=(generation, port, cancel, action, path, destination, stats, self._profile_key()), daemon=True)
        self.app._transfer_thread = worker
        worker.start()

    # Everything inside the serial-read lock runs off Tk's thread. Temporarily
    # adjust serial timeout budgets and restore them even on failure. Progress
    # updates feed measurements immediately, but UI events are throttled to avoid
    # flooding the bounded queue. Decoding a verified JPEG is separate from the
    # transport result: a bad image can be correctly downloaded but not viewable.
    def _worker(self, generation, port, cancel, action, path, destination, stats, profile='auto'):
        def post(event, **info):
            self.app._post_rx((self.event_kind, dict(info, event=event, generation=generation)))
        last_progress = 0

        def progress(**info):
            nonlocal last_progress
            if stats is not None:
                stats.progress(**info)
            now = time.monotonic()
            if now-last_progress >= 0.1:
                post('progress', **info)
                last_progress = now

        capture = Capture(dict(action=action, path=path, requested_profile=profile,
            baud=getattr(port, 'baudrate', None),
            mode='API' if getattr(port, 'api_mode', False) else 'Transparent',
            rssi_polling=getattr(stats, 'rssi_enabled', False)))
        result = {'event': 'failed', 'detail': 'Operation did not complete'}
        try:
            with self.app._serial_read_lock:
                if cancel.is_set() or port is None:
                    raise remote_files.Cancelled('Operation cancelled')
                old_timeout, old_write_timeout = port.timeout, port.write_timeout
                try:
                    port.timeout = 0.1
                    port.write_timeout = 5.0 if getattr(port, 'api_mode', False) else 1.0
                    client = remote_files.Client(port, cancel=cancel, progress=progress, diagnostic=capture.record, profile=profile)
                    if action == 'list':
                        entries = client.list_directory(path)
                        result = dict(event='listed', entries=entries, path=path)
                        try:
                            capabilities = client.capabilities()
                            result['upload_supported'] = 'UPLOAD1' in capabilities
                            result['manage_supported'] = 'MANAGE1' in capabilities
                        except Exception as exc:
                            result['upload_supported'] = False
                            result['manage_supported'] = False
                            result['capability_error'] = str(exc)
                    elif action in ('download', 'view'):
                        saved = client.download(path, destination, max_bytes=8*1024*1024 if action == 'view' else None)
                        result = dict(event='downloaded', destination=str(saved))
                        if action == 'view':
                            if stats is not None:
                                stats.finish('completed')
                            try:
                                from .image_viewer import decode_image
                                result.update(event='viewed', image=decode_image(saved))
                            except Exception as exc:
                                result['view_error'] = str(exc)
                    else:
                        if action == 'upload':
                            client.upload(destination, path)
                            if stats is not None:
                                stats.finish('completed')
                            result = dict(event='uploaded', destination=path)
                        elif action == 'clean':
                            client.clean_partial(path)
                            result = dict(event='cleaned', destination=path)
                        elif action == 'delete':
                            client.delete_file(path)
                            result = dict(event='deleted', destination=path)
                        else:
                            client.rename_file(path, destination)
                            result = dict(event='renamed', destination=destination, source=path)
                        try:
                            folder = posixpath.dirname(path) or '/'
                            self.app.transfer_recorder.event('folder_refresh', folder)
                            result.update(entries=client.list_directory(folder), path=folder)
                        except Exception as exc:
                            result['refresh_error'] = str(exc)
                finally:
                    port.timeout, port.write_timeout = old_timeout, old_write_timeout
        except remote_files.OperationUncertain as exc:
            result = dict(event='uncertain', detail=str(exc))
        except remote_files.Cancelled:
            result = dict(event='cancelled', detail='Cancelled. Upload temporary files are cleaned when reachable; Refresh to check for .part files.')
        except Exception as exc:
            result = dict(event='cancelled' if cancel.is_set() else 'failed', detail=str(exc))
        finally:
            if action not in ('download', 'upload'):
                self.app.transfer_recorder.event('activity_finished', f'{action}: {path} | {result["event"]}')
            if hasattr(port, 'set_transfer_cancel'):
                port.set_transfer_cancel(None)
            if stats is not None:
                stats.finish('completed' if result['event'] in ('downloaded', 'uploaded', 'viewed') else
                             'uncertain' if result['event'] == 'uncertain' else
                             'cancelled' if result['event'] == 'cancelled' else 'failed')
            try:
                radio = port.traffic_snapshot() if hasattr(port, 'traffic_snapshot') else {}
                if hasattr(port, 'diagnostic_summary'):
                    radio['api_summary'] = port.diagnostic_summary()
            except Exception as exc:
                radio = {'error': str(exc)}
            capture.close(result['event'], detail=result.get('detail', ''), radio=radio)
            result['diagnostic_path'] = str(capture.path) if capture.path else ''
            result['diagnostic_error'] = capture.error
            self.app._transfer_active.clear()
            post(**result)

    # Called from app._pump_rx on Tk's thread. Ignore events for an invalidated
    # generation; a late completion must not overwrite a fresh connection's UI.
    # Widget state and dialogs belong here, not in the serial worker callback.
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
                self.status.set(info.get('status') or f'{done:,} / {total:,} bytes | {rate:.0f} bytes/s')
            else:
                self.status.set(info['status'])
            return
        if 'manage_supported' in info:
            self.manage_supported = info['manage_supported']
        if 'upload_supported' in info:
            self.upload_supported = info['upload_supported']
        self._set_busy(False)
        if info.get('diagnostic_path'):
            self.app.log_message('Remote Files diagnostics: ' + info['diagnostic_path'], 'SYSTEM')
        if info.get('diagnostic_error'):
            self.app.log_message('Remote Files diagnostics unavailable: ' + info['diagnostic_error'], 'ERROR')
        # Delayed events after a disconnect cannot restore stale SD listings.
        if not self.app.is_connected:
            self.invalidate()
            return
        if event == 'listed' or event in ('uploaded', 'cleaned', 'deleted', 'renamed') and 'entries' in info:
            self.tree.delete(*self.tree.get_children())
            self.entries.clear()
            self.directory = info['path']
            self.path.set(self.directory)
            for index, entry in enumerate(sorted(info['entries'], key=lambda e: (not e.directory, e.name.casefold()))):
                key = str(index)
                self.entries[key] = entry
                self.tree.insert('', tk.END, iid=key, text=entry.name,
                                 values=('Folder' if entry.directory else 'File', '' if entry.directory else f'{entry.size:,}'))
            notice = f'{len(self.entries)} entries in {self.directory}. Double-click folders; select files to download.'
            if event == 'listed' and not self.upload_supported:
                notice += (' Upload availability could not be checked; Refresh to retry.' if info.get('capability_error')
                           else ' Uploads require the updated satellite firmware.')
            if event == 'listed' and self.upload_supported and not self.manage_supported:
                notice += ' Delete/Rename require the updated satellite firmware.'
            self.status.set(notice)
        if event in ('uploaded', 'cleaned', 'deleted', 'renamed'):
            self.progress.set(100)
            prefixes = dict(uploaded='Upload verified and saved: ', cleaned='Partial upload removed: ',
                            deleted='File deleted: ', renamed='File renamed to: ')
            notice = prefixes[event] + info['destination']
            if info.get('refresh_error'):
                notice += ' | Folder refresh failed; use Refresh. ' + info['refresh_error']
            self.status.set(notice)
            self.app.log_message(notice, 'SYSTEM')
        elif event == 'downloaded':
            self.progress.set(100)
            self.status.set('Verified and saved: ' + info['destination'])
            self.app.log_message('Remote file downloaded: ' + info['destination'], 'SYSTEM')
        elif event != 'listed':
            self.status.set(info['detail'])
            if event in ('failed', 'uncertain'):
                self.app.log_message('Remote Files: ' + info['detail'], 'ERROR')

    def cancel(self):
        if self.busy and self.app._transfer_cancel is not None:
            self.app._transfer_cancel.set()
            self.status.set('Cancelling...')

    def invalidate(self):
        self.upload_supported = False
        self.manage_supported = False
        self.generation += 1
        self.tree.delete(*self.tree.get_children())
        self.entries.clear()
        self.directory = '/'
        self.path.set('/')
        self.progress.set(0)
        self._set_busy(False)
        self.status.set('Disconnected. Connect and Refresh to read the satellite SD card.')
