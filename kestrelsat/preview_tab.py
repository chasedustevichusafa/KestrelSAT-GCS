"""Opt-in periodic still-image preview, coordinated with the existing serial owner.

Tk owns every timer, preference, image and pending-record mutation. A worker
owns one complete frame cycle, including the GUI's display acknowledgment and
remote cleanup. Between cycles no worker holds the port or blocks other tabs.
There is never a frame queue, background auto-start or a second raw API reader.
"""
import csv
from datetime import datetime
from pathlib import Path
import shutil
import tempfile
import threading
import time
import uuid
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from . import camera_files as camera, remote_files as files
from . import preview
from .transfer_diagnostics import Capture

COLUMNS = ('timestamp_local', 'job', 'path', 'resolution', 'requested_interval_s',
           'capture_s', 'download_s', 'decode_s', 'display_wait_s', 'cleanup_s',
           'cycle_s', 'display_interval_s', 'overrun_s', 'bytes', 'transfer_profile',
           'outcome', 'detail')


class LivePreviewTab:
    def __init__(self, app):
        self.app = app
        self.frame = ttk.Frame(app.notebook, padding=10)
        app.notebook.add(self.frame, text='Live Preview')
        self.running = self.busy = self.closed = False
        self.generation = 0
        self.timer = self.render_timer = None
        self.watch_timer = self.age_timer = None
        self._completion = None
        self.worker = self.cancel = None
        self.info = None
        raw_pending = app.settings.get('preview_pending')
        self.pending = preview.pending_preview(raw_pending)
        self.invalid_pending = raw_pending is not None and self.pending is None
        self.image = self.photo = self.current_path = self.current_metadata = None
        self.cache = None
        self.log_path = None
        self.log_handle = None
        self.log_error = ''
        self.last_display = None
        self.frame_count = 0
        self.clock = time.monotonic
        self.next_due = 0
        self.last_cycle_start = None
        self.status = tk.StringVar(value='Preview is off. Connect, select resolution and interval, then Start.')
        self.details = tk.StringVar(value='No preview frame received.')
        self.csv_status = tk.StringVar()
        self.resolution = tk.StringVar(value=app.settings.get('preview_resolution', '640x480'))
        saved_interval = app.settings.get('preview_interval', 10)
        self.interval = tk.StringVar(value=str(saved_interval if saved_interval in (5, 10) else 10))
        self.profile = app.transfer_profile_var # Same saved choice as Camera/Remote Files.
        self.progress = tk.DoubleVar()

        controls = ttk.Frame(self.frame)
        controls.pack(fill=tk.X)
        ttk.Label(controls, text='Resolution:').pack(side=tk.LEFT)
        self.resolution_combo = ttk.Combobox(controls, textvariable=self.resolution, state='readonly', width=12,
            values=[f'{w}x{h}' for w, h in camera.RESOLUTIONS])
        self.resolution_combo.pack(side=tk.LEFT, padx=6)
        ttk.Label(controls, text='Refresh:').pack(side=tk.LEFT)
        self.interval_combo = ttk.Combobox(controls, textvariable=self.interval, values=('5', '10'), state='readonly', width=4)
        self.interval_combo.pack(side=tk.LEFT, padx=6)
        ttk.Label(controls, text='seconds').pack(side=tk.LEFT)
        self.start_button = ttk.Button(controls, text='Start', command=self.start)
        self.start_button.pack(side=tk.LEFT, padx=(12, 6))
        self.stop_button = ttk.Button(controls, text='Stop', command=self.stop)
        self.stop_button.pack(side=tk.LEFT)
        choices = ttk.Frame(self.frame)
        choices.pack(fill=tk.X, pady=6)
        ttk.Label(choices, text='Transfer profile:').pack(side=tk.LEFT)
        self.profile_combo = ttk.Combobox(choices, textvariable=self.profile, state='readonly',
                                         values=list(files.PROFILE_LABELS.values()), width=44)
        self.profile_combo.pack(side=tk.LEFT, padx=6, fill=tk.X, expand=True)
        self.profile_combo.bind('<<ComboboxSelected>>', self._preferences)
        self.resolution_combo.bind('<<ComboboxSelected>>', self._preferences)
        self.interval_combo.bind('<<ComboboxSelected>>', self._preferences)
        recovery = ttk.Frame(self.frame)
        recovery.pack(fill=tk.X)
        self.recover_button = ttk.Button(recovery, text='Recover pending frame', command=self.recover)
        self.recover_button.pack(side=tk.LEFT, padx=(0, 6))
        self.forget_button = ttk.Button(recovery, text='Clear pending record...', command=self.forget)
        self.forget_button.pack(side=tk.LEFT, padx=(0, 6))
        self.save_button = ttk.Button(recovery, text='Save frame...', command=self.save_frame)
        self.save_button.pack(side=tk.LEFT)
        ttk.Progressbar(self.frame, variable=self.progress, maximum=100).pack(fill=tk.X, pady=6)
        self.status_label = ttk.Label(self.frame, textvariable=self.status, wraplength=700)
        self.status_label.pack(fill=tk.X)
        self.detail_label = ttk.Label(self.frame, textvariable=self.details, wraplength=700)
        self.detail_label.pack(fill=tk.X, pady=6)
        self.csv_label = ttk.Label(self.frame, textvariable=self.csv_status, wraplength=700)
        self.csv_label.pack(fill=tk.X, pady=(0, 6))
        self.canvas = tk.Canvas(self.frame, highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind('<Configure>', self._schedule_render)
        self.frame.bind('<Configure>', self._wrap)
        app.themes.add_listener(self._theme)
        self._theme(app.themes.colors)
        self._controls()
        if self.pending or self.invalid_pending:
            self.status.set('Preview is off. Resolve the pending frame before starting another capture.')

    def _theme(self, colors):
        self.canvas.configure(background=colors['field_bg'])

    def _wrap(self, event):
        if event.widget is self.frame:
            width = max(160, event.width-20)
            self.status_label.configure(wraplength=width)
            self.detail_label.configure(wraplength=width)
            self.csv_label.configure(wraplength=width)

    def _profile_key(self):
        return next((key for key, label in files.PROFILE_LABELS.items() if label == self.profile.get()), 'auto')

    def _preferences(self, event=None):
        self.app._write_settings(dict(preview_resolution=self.resolution.get(),
                                     preview_interval=int(self.interval.get()),
                                     file_transfer_profile=self._profile_key()))
        if self.running and not self.busy and self.last_cycle_start is not None:
            self.next_due = preview.next_frame_time(self.last_cycle_start, int(self.interval.get()), self.clock())

    def _controls(self):
        self.start_button.configure(state='disabled' if self.running or self.busy else 'normal')
        self.stop_button.configure(state='normal' if self.running or self.busy else 'disabled')
        pending = self.pending is not None or self.invalid_pending
        self.recover_button.configure(state='normal' if pending and not self.busy else 'disabled')
        self.forget_button.configure(state='normal' if pending and not self.busy else 'disabled')
        self.save_button.configure(state='normal' if self.current_path else 'disabled')

    def _persist(self, request):
        if request is not None and preview.pending_preview(request) is None:
            raise files.RemoteError('Invalid preview ownership record')
        # _write_settings returns success for preview's pre-send checkpoint.
        # Failure must prevent capture/delete; a GUI preference can be best effort,
        # but an irreversible request's recovery record cannot be silently lost.
        if self.app._write_settings({'preview_pending': request}) is False:
            raise files.RemoteError('Cannot save preview recovery record; no new command was sent')
        self.pending = request
        self.invalid_pending = False
        self._controls()

    def _ready(self):
        if not self.app.is_connected or self.app.serial_connection is None:
            self.status.set('Connect to the ground radio first.'); return False
        if self.app.test_mode:
            self.status.set('Live Preview requires the real camera; TEST MODE supplies telemetry only.'); return False
        if self.app.camera.pending:
            self.status.set('Resolve the pending still image with Camera → Check capture first.'); return False
        return True

    def start(self):
        if self.closed or self.busy or self.running or not self._ready():
            return
        if self.pending or self.invalid_pending:
            self.status.set('Recover or inspect and clear the pending frame before starting.'); return
        self.running = True
        self.info = None # Probe actual camera-supported resolutions on each Start.
        self.last_display = None
        self.frame_count = 0
        self.last_cycle_start = None
        self.next_due = self.clock()
        self._open_log()
        self._preferences()
        self.app.transfer_recorder.event('preview_started', f'{self.resolution.get()} | {self.interval.get()} s')
        self.status.set('Starting preview...')
        self._controls()
        self._tick()

    def stop(self):
        if self.running or self.busy:
            self.app.transfer_recorder.event('preview_stopped', 'New captures disabled')
        self.running = False
        self._cancel_timer()
        if self.cancel is not None and self.busy:
            self.cancel.set()
        self.status.set('Stopping current frame; accepted capture may finish on SD.' if self.busy else
                        'Preview stopped. Last frame retained locally; no new captures scheduled.')
        self._controls()
        if not self.busy:
            self._close_log()

    def recover(self):
        if self.closed or self.busy or not self._ready():
            return
        if self.pending is None:
            self.status.set('Invalid recovery record. Inspect SD images before clearing it.'); return
        if self._port_busy():
            self.status.set('Wait for the other operation to finish before recovery.'); return
        self.running = False # Recovery is a single operation and never restarts a loop.
        self._open_log()
        try:
            self._launch('frame', dict(self.pending), recover=True)
        except Exception as exc:
            self.status.set('Recovery could not start: '+str(exc))
            self._close_log(); self._controls()

    def forget(self):
        if self.busy or self.app._transfer_active.is_set():
            self.status.set('Wait for the current operation to finish.'); return
        path = self.pending['path'] if self.pending else 'the image referenced by the invalid record'
        if messagebox.askyesno('Clear preview recovery record',
                'Inspect '+path+' in Camera/Remote Files first.\n\n'
                'Clear this local record? The satellite image is kept, and an accepted capture may still finish.',
                parent=self.app.root):
            try:
                self._persist(None)
                self.status.set('Recovery record cleared. No satellite files were deleted.')
            except Exception as exc:
                self.status.set(str(exc))

    def _port_busy(self):
        worker = self.app._transfer_thread
        return self.app._transfer_active.is_set() or worker is not None and worker.is_alive()

    def _tick(self):
        self.timer = None
        if self.closed or not self.running:
            return
        if not self._ready():
            self.running = False
            self._controls(); self._close_log()
            return
        if self.app.camera.busy or self._port_busy():
            self.status.set('Preview waiting for the other camera/file operation.')
            self.timer = self.app.root.after(200, self._tick)
            return
        now = self.clock()
        if now < self.next_due:
            if self.current_metadata:
                age = max(0, now-self.current_metadata['displayed_at'])
                self.status.set(f'Preview active | last display update {age:.1f} s ago | next capture in {self.next_due-now:.1f} s')
            self.timer = self.app.root.after(200, self._tick)
            return
        if self.info is None:
            try:
                self._launch('probe')
            except Exception as exc:
                self.running = False
                self.status.set('Preview paused: '+str(exc))
                self._controls(); self._close_log()
            return
        try:
            size = tuple(map(int, self.resolution.get().split('x')))
            mode = camera.RESOLUTIONS.index(size)
            if mode not in self.info.modes:
                raise ValueError()
            request = preview.new_preview(mode)
            self._persist(request) # On Tk, before the worker may send CAPTURE.
            self._launch('frame', request)
        except Exception as exc:
            self.running = False
            self.status.set('Preview paused: '+str(exc))
            self._controls(); self._close_log()

    def _launch(self, action, request=None, recover=False):
        # Capture all Tk state here. No worker is allowed to consult a StringVar.
        if self.cache is None:
            root = Path(self.app.downloads_dir)/'live_preview'
            root.mkdir(parents=True, exist_ok=True)
            self.cache = Path(tempfile.mkdtemp(prefix='preview-', dir=root))
        self.busy = True
        self._completion = None
        self.generation += 1
        self.progress.set(0)
        interval, profile = int(self.interval.get()), self._profile_key()
        port = self.app.serial_connection
        self.app._transfer_active.set()
        self.cancel = cancel = threading.Event()
        self.app._transfer_cancel = cancel
        if hasattr(port, 'set_transfer_cancel'):
            port.set_transfer_cancel(cancel)
        self.worker = threading.Thread(target=self._worker,
            args=(self.generation, port, cancel, action, request, recover, interval, profile, self.cache), daemon=True)
        self.app._transfer_thread = self.worker
        self._controls()
        self.worker.start()
        self.watch_timer = self.app.root.after(50, self._watch_worker)

    def _watch_worker(self):
        """A saturated telemetry queue must not strand the tab in Busy.

        Frame/checkpoint delivery can time out safely without deleting SD data.
        The final outcome also has this one-slot mailbox so completion remains
        observable even if app._post_rx has to drop its queued notification.
        """
        self.watch_timer = None
        if self.closed or not self.busy:
            return
        if self._completion is not None:
            generation, result = self._completion
            self.on_event(dict(event='finished', generation=generation, result=result))
        elif self.busy:
            self.watch_timer = self.app.root.after(50, self._watch_worker)

    def _worker(self, generation, port, cancel, action, request, recover, interval, profile, cache):
        def post(event, **data):
            self.app._post_rx(('live_preview', dict(data, event=event, generation=generation)))
        def gui_call(event, value=None):
            task = preview.GuiRequest(value)
            post(event, task=task)
            return task.wait(cancel)
        started = self.clock()
        metrics = dict(requested_interval_s=interval, timestamp_local=datetime.now().astimezone().isoformat(),
                       started_at=started)
        if request:
            metrics.update(job=request['job'], path=request['path'],
                           resolution='x'.join(map(str, camera.RESOLUTIONS[request['mode']])))
        capture = Capture(dict(action='live_preview_'+action, requested_profile=profile,
                               interval_s=interval, path=metrics.get('path', ''),
                               baud=getattr(port, 'baudrate', None)))
        result = dict(action=action, resolved=False, outcome='failed')
        stats = None
        last_progress = 0
        def progress(**info):
            nonlocal last_progress
            if stats is not None:
                stats.progress(**info)
            now = self.clock()
            if now-last_progress >= .1:
                post('progress', info=info); last_progress = now
        def start_download():
            nonlocal stats
            stats = gui_call('download_start', request['path'])
        def finish_download():
            if stats is not None:
                stats.finish('completed')
        try:
            with self.app._serial_read_lock:
                if cancel.is_set():
                    raise files.Cancelled('Preview stopped')
                old_timeout, old_write_timeout = port.timeout, port.write_timeout
                try:
                    port.timeout = .1
                    port.write_timeout = 5 if getattr(port, 'api_mode', False) else 1.0
                    client = camera.CameraClient(port, cancel=cancel,
                        progress=lambda **data: post('progress', info=data), diagnostic=capture.record, profile=profile)
                    if action == 'probe':
                        caps = client.capabilities()
                        if 'MANAGE1' not in caps:
                            raise files.RemoteError('Preview needs delete support for automatic SD cleanup')
                        info = client.camera_info(caps)
                        if not info.ready or not info.sd_ready:
                            raise files.RemoteError('Camera or SD unavailable: '+info.error)
                        result.update(outcome='completed', info=info)
                    else:
                        # Recovery must not overwrite the displayed local copy:
                        # a cancelled/rejected retry removes only its own staging.
                        staging = cache/(request['job']+'-'+uuid.uuid4().hex[:8]+'.jpg')
                        preview.run_frame(client, request, staging, recover=recover,
                            publish=lambda receipt: post('frame', receipt=receipt),
                            checkpoint=lambda record: gui_call('checkpoint', record), progress=progress,
                            download_started=start_download, download_finished=finish_download, metrics=metrics, clock=self.clock)
                        result.update(outcome='completed', resolved=True)
                finally:
                    port.timeout, port.write_timeout = old_timeout, old_write_timeout
        except preview.CaptureTerminal as exc:
            result.update(detail=str(exc), resolved=True)
        except Exception as exc:
            result.update(outcome='cancelled' if cancel.is_set() else 'failed', detail=str(exc))
        finally:
            if stats is not None:
                stats.finish('cancelled' if cancel.is_set() else 'failed') # Idempotent after valid download.
            metrics.update(cycle_s=self.clock()-started,
                           overrun_s=max(0, self.clock()-started-interval),
                           outcome=result['outcome'], detail=result.get('detail', ''))
            if action == 'frame':
                self.app.transfer_recorder.event('preview_frame_'+result['outcome'],
                    metrics.get('path', '')+f' | {metrics["cycle_s"]:.2f} s cycle')
            capture.close(result['outcome'], **metrics)
            result.update(metrics=metrics, diagnostic=str(capture.path) if capture.path else '')
            if hasattr(port, 'set_transfer_cancel'):
                port.set_transfer_cancel(None)
            if self.closed:
                # Shutdown may have skipped cleanup while this worker still
                # owned staging files. No more GUI saves/captures can start.
                shutil.rmtree(cache, ignore_errors=True)
            self.app._transfer_active.clear()
            self._completion = (generation, result)
            post('finished', result=result)

    def on_event(self, event):
        if self.closed or event['generation'] != self.generation:
            if 'receipt' in event:
                event['receipt'].reject()
            if 'task' in event:
                event['task'].abort()
            return
        kind = event['event']
        if kind == 'checkpoint':
            event['task'].perform(self._persist)
        elif kind == 'download_start':
            event['task'].perform(lambda path: self.app.start_transfer_statistics('KFS1', 'download', path))
        elif kind == 'frame':
            receipt = event['receipt']
            if self.cancel.is_set() or not self.app.is_connected:
                receipt.reject()
            else:
                try:
                    receipt.present(self._display)
                except Exception as exc:
                    self.status.set('Could not display preview: '+str(exc))
        elif kind == 'progress':
            data = event['info']
            if data.get('total'):
                self.progress.set(100*data.get('done', 0)/data['total'])
            if data.get('status'):
                self.status.set(data['status'])
        elif kind == 'finished':
            if not self.busy:
                return # Mailbox and normal queue may both deliver this outcome.
            self._completion = None
            self.busy = False
            result = event['result']
            if result['resolved']:
                try:
                    self._persist(None)
                except Exception as exc:
                    result.update(outcome='failed', detail=str(exc))
            if result['action'] == 'probe' and result['outcome'] == 'completed':
                self.info = result['info']
                values = [f'{camera.RESOLUTIONS[i][0]}x{camera.RESOLUTIONS[i][1]}' for i in self.info.modes]
                self.resolution_combo.configure(values=values)
                if self.resolution.get() not in values:
                    self.resolution.set(values[0])
                self.status.set('Camera ready; starting preview.' if self.running else 'Preview stopped. Camera check completed.')
            elif result['action'] == 'frame':
                metrics = result['metrics']
                self._write_log(metrics)
                self.last_cycle_start = metrics['started_at']
                self.next_due = preview.next_frame_time(metrics['started_at'], int(self.interval.get()), self.clock())
                if result['outcome'] == 'completed':
                    self.status.set('Frame displayed and SD preview image removed.' if metrics.get('bytes') else
                                    'Previous cleanup confirmed. Press Start to resume preview.')
                else:
                    self.running = False
                    self.status.set('Preview paused: '+result.get('detail', 'Operation interrupted')+
                                    (' | SD frame retained: '+self.pending['path'] if self.pending else ''))
            if result['outcome'] != 'completed':
                self.running = False
                if result['action'] == 'probe':
                    self.status.set('Preview paused: '+result.get('detail', 'Camera unavailable'))
            self._controls()
            if self.running:
                self.timer = self.app.root.after(10, self._tick)
            else:
                self._close_log()

    def _display(self, receipt):
        # Build PhotoImage before replacing the previous frame. If rendering
        # fails, the worker retains the SD image and the old display stays valid.
        photo = self._make_photo(receipt.image)
        old_image, old_path = self.image, self.current_path
        self.canvas.delete('all')
        self.canvas.create_image(self.canvas.winfo_width()/2, self.canvas.winfo_height()/2,
                                 anchor=tk.CENTER, image=photo)
        self.image, self.photo, self.current_path = receipt.image, photo, receipt.path
        now = self.clock()
        self.current_metadata = dict(receipt.metadata, displayed_at=now)
        # Image age includes capture/download latency, rather than resetting to
        # zero at display. Snapshot the wall-clock delta once, then use monotonic
        # elapsed time so clock corrections cannot make a displayed frame younger.
        try:
            requested = datetime.fromisoformat(receipt.metadata['requested_local'])
            self.current_metadata['age_at_display'] = max(0, (datetime.now().astimezone()-requested).total_seconds())
        except (ValueError, TypeError):
            self.current_metadata['age_at_display'] = None
        gap = None if self.last_display is None else now-self.last_display
        self.last_display = now
        self.frame_count += 1
        self.current_metadata['display_interval_s'] = gap
        elapsed = receipt.metadata.get('download_s', 0)
        rate = receipt.metadata.get('bytes', 0)/elapsed if elapsed else 0
        self.details.set(f'Frame {self.frame_count} | {receipt.metadata["requested_local"]} | '
            f'{receipt.image.width}×{receipt.image.height} | {receipt.metadata.get("bytes", 0):,} bytes | '
            f'{rate:.0f} B/s | actual refresh: '+(f'{gap:.1f} s' if gap is not None else 'first frame'))
        if old_image is not None:
            old_image.close()
        if old_path is not None and old_path != self.current_path:
            try:
                old_path.unlink(missing_ok=True)
            except OSError as exc:
                self.running = False
                self.log_error = 'Could not remove old local preview cache: '+str(exc)
                self.status.set(self.log_error)
        self._controls()
        if self.age_timer is None:
            self.age_timer = self.app.root.after(250, self._age)

    def _age(self):
        self.age_timer = None
        if self.closed or self.current_metadata is None:
            return
        meta = self.current_metadata
        base = meta.get('age_at_display')
        age = None if base is None else base+max(0, self.clock()-meta['displayed_at'])
        gap = meta.get('display_interval_s')
        rate = meta.get('bytes', 0)/meta['download_s'] if meta.get('download_s') else 0
        stale = ' | previous frame is stale' if age is not None and age > int(self.interval.get()) else ''
        age_text = f'{age:.1f} s' if age is not None else 'unavailable'
        self.details.set(f'Frame {self.frame_count} | {meta["requested_local"]} | '
            f'{self.image.width}×{self.image.height} | {meta.get("bytes", 0):,} bytes | '
            f'{rate:.0f} B/s | image age {age_text} | actual refresh: '+
            (f'{gap:.1f} s' if gap is not None else 'first frame')+stale)
        self.age_timer = self.app.root.after(250, self._age)

    def _make_photo(self, image):
        from PIL import Image, ImageTk
        ratio = min(1, max(1, self.canvas.winfo_width()-8)/image.width,
                       max(1, self.canvas.winfo_height()-8)/image.height)
        resized = image.resize((max(1, int(image.width*ratio)), max(1, int(image.height*ratio))), Image.Resampling.LANCZOS)
        try:
            return ImageTk.PhotoImage(resized, master=self.canvas)
        finally:
            resized.close()

    def _schedule_render(self, event=None):
        if not self.closed and self.image is not None:
            if self.render_timer is not None:
                self.app.root.after_cancel(self.render_timer)
            self.render_timer = self.app.root.after(80, self._render)

    def _render(self):
        self.render_timer = None
        if self.closed or self.image is None:
            return
        self.photo = self._make_photo(self.image)
        self.canvas.delete('all')
        self.canvas.create_image(self.canvas.winfo_width()/2, self.canvas.winfo_height()/2,
                                 anchor=tk.CENTER, image=self.photo)

    def save_frame(self):
        if self.current_path is None:
            return
        # Native file dialogs run a nested event loop: another frame may arrive
        # while the user chooses a destination. Snapshot the CLICKED frame first,
        # so the saved filename/content cannot silently change under the dialog.
        name = Path(self.current_metadata['path']).name
        snapshot = None
        try:
            with tempfile.NamedTemporaryFile(prefix='save-', suffix='.jpg', dir=self.cache, delete=False) as handle:
                snapshot = Path(handle.name)
            shutil.copyfile(self.current_path, snapshot)
            target = filedialog.asksaveasfilename(parent=self.app.root, title='Save displayed preview frame',
                initialfile=name, defaultextension='.jpg', filetypes=[('JPEG image', '*.jpg')])
            if target and not self.closed:
                if self.cache.resolve() in Path(target).resolve().parents:
                    raise ValueError('Choose a location outside the temporary preview cache')
                shutil.copyfile(snapshot, target)
                self.status.set('Displayed frame saved to '+target)
        except (OSError, ValueError) as exc:
            if not self.closed:
                messagebox.showerror('Could not save frame', str(exc), parent=self.app.root)
        finally:
            if snapshot is not None:
                snapshot.unlink(missing_ok=True)

    def _open_log(self):
        self._close_log()
        self.log_error = ''
        try:
            folder = Path(self.app.transfer_recorder.directory)/'preview'
            folder.mkdir(parents=True, exist_ok=True)
            self.log_path = folder/('preview-'+datetime.now().strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]+'.csv')
            self.log_handle = self.log_path.open('w', newline='', encoding='utf-8')
            self.writer = csv.DictWriter(self.log_handle, fieldnames=COLUMNS, extrasaction='ignore')
            self.writer.writeheader(); self.log_handle.flush()
            self.csv_status.set('Cycle CSV: '+str(self.log_path))
        except OSError as exc:
            self.log_error = str(exc)
            self.csv_status.set('Cycle CSV unavailable: '+self.log_error)
            self._close_log()

    def _write_log(self, metrics):
        if not self.log_handle:
            return
        row = dict(metrics)
        row['display_interval_s'] = (self.current_metadata.get('display_interval_s')
            if self.current_metadata and self.current_metadata['job'] == metrics.get('job') else None)
        try:
            self.writer.writerow(row); self.log_handle.flush()
        except OSError as exc:
            self.log_error = str(exc)
            self.csv_status.set('Cycle CSV recording stopped: '+self.log_error)
            self._close_log()

    def _close_log(self):
        if self.log_handle:
            try:
                self.log_handle.close()
            except OSError as exc:
                self.log_error = str(exc)
            self.log_handle = None

    def _cancel_timer(self):
        if self.timer is not None:
            self.app.root.after_cancel(self.timer)
            self.timer = None

    def invalidate(self):
        self.stop()
        self.generation += 1
        self.busy = False
        self.info = None
        self.status.set('Disconnected. Preview is off; recover any pending frame after reconnecting.')
        if self.watch_timer is not None:
            self.app.root.after_cancel(self.watch_timer)
            self.watch_timer = None
        self._controls()
        self._close_log()

    def close(self):
        if self.closed:
            return
        self.closed = True
        # Publish Closed BEFORE cancellation wakes the worker, so one of this
        # method or its finalizer always owns cleanup even in a tight exit race.
        self.invalidate()
        if self.age_timer is not None:
            self.app.root.after_cancel(self.age_timer)
            self.age_timer = None
        if self.render_timer is not None:
            self.app.root.after_cancel(self.render_timer)
            self.render_timer = None
        self.photo = None
        if self.image is not None:
            self.image.close(); self.image = None
        # Only this session's private directory is removed. If a worker outlives
        # shutdown's bounded join, let its finalizer own pending files instead.
        if self.cache is not None and (self.worker is None or not self.worker.is_alive()):
            shutil.rmtree(self.cache, ignore_errors=True)
