"""Camera controls and SD image gallery, sharing the proven file-transfer worker."""
from pathlib import Path
import posixpath
import re
import tempfile
import time
import tkinter as tk
from tkinter import ttk

from . import camera_files as camera
from . import remote_files as rf
from .remote_tab import RemoteFilesTab
from .image_viewer import ImageViewer
from .transfer_diagnostics import Capture


class CameraTab(RemoteFilesTab):
    def __init__(self, app):
        self.info = None
        self.pending = camera.pending_capture(app.settings.get('camera_pending'))
        self.viewers = []
        super().__init__(app, title='Camera', event_kind='camera')
        self.directory = '/images'; self.path.set('/images')
        self.path_entry.configure(state='readonly')
        for button in self.buttons:
            if button['text'] in ('Up', 'Upload...', 'Remove partial...'):
                button.pack_forget()
            elif button['text'] == 'Refresh / Open':
                button.configure(text='Refresh images')
        self.tree.heading('kind', text='Resolution')
        self.tree.column('kind', width=110, minwidth=85)
        bar = ttk.Frame(self.frame)
        bar.pack(fill=tk.X, before=self.frame.winfo_children()[0], pady=(0, 8))
        ttk.Label(bar, text='Resolution:').pack(side=tk.LEFT)
        self.resolution = tk.StringVar(value='640x480')
        self.resolution_combo = ttk.Combobox(bar, textvariable=self.resolution, width=13, state='disabled')
        self.resolution_combo.pack(side=tk.LEFT, padx=6)
        self.capture_button = ttk.Button(bar, text='Capture image', command=self.capture_selected, state='disabled')
        self.capture_button.pack(side=tk.LEFT, padx=(0, 6))
        self.check_button = ttk.Button(bar, text='Check capture', command=self.check_capture)
        self.check_button.pack(side=tk.LEFT)
        self.buttons.extend((self.capture_button, self.check_button))
        self.view_button = ttk.Button(self.cancel_button.master, text='View', command=self.view_selected)
        self.view_button.pack(side=tk.LEFT, before=self.cancel_button, padx=6)
        self.buttons.append(self.view_button)
        self.status.set('Connect and Refresh images to detect the camera and list /images. Capture saves to SD; View downloads one saved JPEG.')
        self._set_busy(False)

    def _set_busy(self, value):
        super()._set_busy(value)
        if not hasattr(self, 'capture_button'):
            return
        self.path_entry.configure(state='disabled' if value else 'readonly')
        self.resolution_combo.configure(state='disabled' if value or not self.info or not self.info.ready else 'readonly')
        if not value:
            enabled = self.info and self.info.ready and self.info.sd_ready and self.pending is None
            self.capture_button.configure(state='normal' if enabled else 'disabled')
            self.check_button.configure(state='normal' if self.pending else 'disabled')

    def _save_pending(self, pending):
        self.pending = pending
        self.app._write_settings({'camera_pending': pending})

    def refresh(self):
        if self._ready():
            self._start('camera_refresh', '/images')

    def capture_selected(self):
        if not self._ready():
            return
        if self.pending:
            self.status.set('Use Check capture to resolve the previous job first.'); return
        if not self.info or not self.info.ready or not self.info.sd_ready:
            self.status.set('Refresh images and check camera / SD status first.'); return
        try:
            size = tuple(map(int, self.resolution.get().split('x')))
            mode = camera.RESOLUTIONS.index(size)
            if mode not in self.info.modes:
                raise ValueError()
        except ValueError:
            self.status.set('Choose a supported resolution.'); return
        pending = camera.new_capture(mode)
        self._save_pending(pending) # Persist before dispatch, including a disconnect race.
        self._start('camera_capture', pending['path'], dict(pending))

    def check_capture(self):
        if self._ready() and self.pending:
            self._start('camera_check', self.pending['path'], dict(self.pending))

    def view_selected(self):
        if len(self.viewers) >= 4:
            self.status.set('Close an image window before opening another (maximum four).'); return
        if not self._ready():
            return
        path = self._selected_file()
        if not path:
            return
        entry = self.entries[self.tree.selection()[0]]
        if entry.size > camera.MAX_IMAGE:
            self.status.set('Image exceeds the 8 MiB viewing limit. Use Download to save it.'); return
        try:
            cache = Path(self.app.downloads_dir)/'camera'
            cache.mkdir(parents=True, exist_ok=True)
            folder = Path(tempfile.mkdtemp(prefix='view-', dir=cache))
        except OSError as exc:
            self.status.set('Cannot create image download folder: '+str(exc)); return
        self._start('view', path, folder/entry.name)

    def open_selected(self, event=None):
        self.view_selected()

    def _start(self, action, path, destination=None):
        if action == 'rename' and not camera.image_path(destination):
            self.status.set('Keep a .jpg or .jpeg filename in /images.'); return
        super()._start(action, path, destination)
        self.cancel_button.configure(text='Stop waiting' if action in ('camera_capture', 'camera_check') else 'Cancel')

    def _worker(self, generation, port, cancel, action, path, destination, stats):
        if not action.startswith('camera_'):
            super()._worker(generation, port, cancel, action, path, destination, stats)
            return
        result = dict(event='failed', detail='Camera operation did not complete')
        diagnostic = Capture(dict(action=action, path=path,
            baud=getattr(port, 'baudrate', None), mode='API' if getattr(port, 'api_mode', False) else 'Transparent'))
        def post(event, **fields):
            self.app._post_rx(('camera', dict(fields, event=event, generation=generation)))
        last_progress = 0
        def progress(**fields):
            nonlocal last_progress
            now = time.monotonic()
            if now-last_progress >= .1:
                post('progress', **fields)
                last_progress = now
        try:
            with self.app._serial_read_lock:
                if cancel.is_set() or port is None:
                    raise rf.Cancelled('Cancelled before dispatch')
                old = (port.timeout, port.write_timeout)
                try:
                    port.timeout = .1; port.write_timeout = 5 if getattr(port, 'api_mode', False) else 1
                    client = camera.CameraClient(port, cancel=cancel, progress=progress, diagnostic=diagnostic.record)
                    if action == 'camera_refresh':
                        capabilities = set()
                        try:
                            capabilities = client.capabilities()
                            info = client.camera_info(capabilities)
                            result = dict(event='camera_listed', camera_info=info, manage_supported='MANAGE1' in capabilities)
                        except rf.Cancelled:
                            raise
                        except rf.RemoteError as exc:
                            result = dict(event='camera_listed', camera_info=None, camera_error=str(exc),
                                          manage_supported='MANAGE1' in capabilities)
                    else:
                        initial = client.start_capture(destination) if action == 'camera_capture' else None
                        outcome = client.wait_capture(destination, initial)
                        result = dict(event='camera_result', outcome=outcome, pending=destination)
                        self.app.transfer_recorder.event('camera_'+outcome['state'].lower(), path+' | '+outcome['error'])
                    try:
                        result['entries'] = client.list_directory('/images')
                    except rf.Cancelled:
                        raise
                    except rf.RemoteError as exc:
                        # /images is created by the first capture, not by Refresh.
                        result['entries'] = []
                        result['list_error'] = str(exc)
                finally:
                    port.timeout, port.write_timeout = old
        except rf.OperationUncertain as exc:
            result = dict(event='uncertain', detail=str(exc))
        except rf.Cancelled:
            result = dict(event='uncertain' if action != 'camera_refresh' else 'cancelled',
                detail='Stopped waiting. The satellite may still save the image. Use Check capture after reconnecting.')
        except Exception as exc:
            result = dict(event='failed', detail=str(exc), rejected=action == 'camera_capture' and
                          isinstance(exc, rf.RemoteError) and str(exc).startswith('Satellite:'))
        finally:
            if hasattr(port, 'set_transfer_cancel'):
                port.set_transfer_cancel(None)
            self.app.transfer_recorder.event('activity_finished', f'{action}: {path} | {result["event"]}')
            diagnostic.close(result['event'], detail=result.get('detail', ''), outcome=result.get('outcome'))
            if diagnostic.path:
                result['diagnostic_path'] = str(diagnostic.path)
            self.app._transfer_active.clear()
            post(result.pop('event'), **result)

    def _gallery(self, entries):
        self.tree.delete(*self.tree.get_children()); self.entries.clear()
        self.directory = '/images'; self.path.set('/images')
        for index, entry in enumerate(sorted(entries, key=lambda e: (bool(re.match(r'^\d{8}T\d{6}[+-]\d{4}_', e.name)), e.name.casefold()), reverse=True)):
            if entry.directory or not entry.name.lower().endswith(('.jpg', '.jpeg')):
                continue
            key = str(index); self.entries[key] = entry
            match = re.search(r'_(\d+x\d+)_', entry.name)
            self.tree.insert('', tk.END, iid=key, text=entry.name,
                             values=(match[1] if match else '—', f'{entry.size:,}'))

    def on_event(self, info):
        if info['generation'] != self.generation:
            if info.get('image') is not None:
                info['image'].close()
            return
        event = info['event']
        if event in ('camera_listed', 'camera_result', 'viewed') and info.get('diagnostic_path'):
            self.app.log_message('Camera diagnostics: '+info['diagnostic_path'], 'SYSTEM')
        if event in ('camera_listed', 'camera_result'):
            if event == 'camera_listed':
                self.info = info['camera_info']
                if self.info:
                    modes = [f'{camera.RESOLUTIONS[i][0]}x{camera.RESOLUTIONS[i][1]}' for i in self.info.modes]
                    self.resolution_combo.configure(values=modes)
                    if self.resolution.get() not in modes:
                        self.resolution.set(modes[0] if modes else '')
            else:
                self._save_pending(None)
            self._set_busy(False)
            self._gallery(info.get('entries', []))
            if event == 'camera_listed':
                if not self.info:
                    text = info.get('camera_error', 'Camera unavailable')
                else:
                    text = f'{self.info.model} | '+('Camera ready' if self.info.ready else self.info.error)+' | '+('SD ready' if self.info.sd_ready else 'SD unavailable')
                self.status.set(text+f' | {len(self.entries)} images. Capture saves to SD; View downloads a saved JPEG.')
                self.manage_supported = info.get('manage_supported', False)
                self._set_busy(False)
            else:
                outcome = info['outcome']; state = outcome['state']
                if state == 'SAVED':
                    self.progress.set(100)
                    self.status.set('Captured, SD-verified and saved: '+outcome['path']+'. Select View to download it.')
                    for key, entry in self.entries.items():
                        if entry.name == posixpath.basename(outcome['path']):
                            self.tree.selection_set(key); self.tree.see(key); break
                elif state == 'UNKNOWN':
                    self.status.set('Capture status was lost, possibly after a satellite reset. Refresh and inspect /images before another capture.')
                else:
                    self.status.set(f'Capture {state.lower()}: {outcome["error"]}. Refresh to inspect files.')
            if info.get('list_error') and 'DIRECTORY_UNAVAILABLE' not in info['list_error']:
                self.status.set(self.status.get()+' | Folder refresh: '+info['list_error'])
            self.app.log_message('Camera: '+self.status.get(), 'SYSTEM')
            return
        if event == 'viewed':
            self._set_busy(False)
            if self.app.is_connected:
                try:
                    viewer = ImageViewer(self.app, info['destination'], info['image'], self.viewers.remove)
                    self.viewers.append(viewer)
                    self.status.set('Viewing downloaded local copy: '+info['destination'])
                    self.progress.set(100)
                except Exception as exc:
                    info['image'].close()
                    self.status.set('Image downloaded, but viewer could not open: '+str(exc))
            else:
                info['image'].close()
            return
        if info.get('rejected'):
            self._save_pending(None)
        super().on_event(info)
        if 'entries' in info:
            self._gallery(info['entries'])
        if info.get('view_error'):
            self.status.set('Downloaded and verified, but cannot display JPEG: '+info['view_error'])

    def invalidate(self):
        self.info = None
        super().invalidate()
        self.directory = '/images'; self.path.set('/images')
        if self.pending:
            self.status.set('Disconnected. Capture may finish on SD. Reconnect and use Check capture.')

    def close(self):
        for viewer in list(self.viewers):
            viewer.close()
