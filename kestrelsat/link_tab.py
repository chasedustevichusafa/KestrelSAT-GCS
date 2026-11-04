"""Opt-in Link Tracker UI. Workers own serial, probe logging and bulk file work.

A session reserves the same _transfer_active/read-lock mechanism as file and
camera operations. Independent polling of a completion mailbox means a full
telemetry GUI queue cannot strand ownership or lose a worker's final result.
Closing a graph does not stop recording; Stop/Disconnect/Application Close do.
Manual checkpoint/stop log entries share the recording lock with the worker.
"""
from collections import deque
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from . import link_tracker as link
from .link_plot import LinkPlot
from .widgets import ScrollableFrame


class LinkTrackerTab:
    def __init__(self, app):
        self.app = app
        self.frame = ttk.Frame(app.notebook, padding=10)
        app.notebook.add(self.frame, text='Link Tracker')
        self.busy = self.closed = False
        self.worker = self.cancel = self.timer = self.plot = None
        self.recording = self.active = None
        self.history = deque(maxlen=10)
        self.full_data = None
        self.io_worker = self.io_result = self.io_kind = None
        self.last_draw = 0
        self.name = tk.StringVar(value='Range test')
        self.rate = tk.StringVar(value='2')
        self.timeout = tk.StringVar(value='1.0')
        self.source = tk.StringVar()
        self.selection = tk.StringVar()
        self.mode = tk.StringVar(value='Last 60 s')
        self.smooth = tk.BooleanVar(value=True)
        self.target = tk.StringVar(value='Combined')
        self.marker = tk.StringVar()
        self.distance = tk.StringVar()
        self.status = tk.StringVar(value='Tracking is off. Connect in XBee API (AP=1) mode, then Start.')
        self.summary = tk.StringVar(value='Packet RSSI is preferred; other API formats use DB at up to 1 Hz.')
        self.paths = tk.StringVar(value='Raw CSV and configuration are saved automatically in link_tracking/.')
        self.scroll = ScrollableFrame(self.frame, vscroll=True, hscroll=True)
        self.scroll.pack(fill=tk.BOTH, expand=True)
        body = self.scroll.inner
        self.inputs = []
        for title, variable, values in (('Session name', self.name, None),
                                        ('Probes per second (10 Hz experimental)', self.rate, ('1','2','5','10')),
                                        ('Probe timeout, seconds', self.timeout, ('.5','1.0','2.0')),
                                        ('Expected receive source, optional hex (MY for 16-bit frames)', self.source, None)):
            row = ttk.Frame(body); row.pack(fill=tk.X, pady=3)
            ttk.Label(row, text=title+':').pack(side=tk.LEFT)
            widget = ttk.Combobox(row, textvariable=variable, values=values, state='readonly', width=10) if values else ttk.Entry(row, textvariable=variable, width=28)
            widget.pack(side=tk.LEFT, padx=6, fill=tk.X, expand=True)
            self.inputs.append((widget, 'readonly' if values else 'normal'))
        controls = ttk.Frame(body); controls.pack(fill=tk.X, pady=8)
        self.start_button = ttk.Button(controls, text='Start tracking', command=self.start)
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(controls, text='Stop', command=self.stop, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=6)
        ttk.Button(controls, text='Show graphs', command=self.show_graphs).pack(side=tk.LEFT)
        self.combo = ttk.Combobox(body, textvariable=self.selection, state='readonly', width=65)
        self.combo.pack(fill=tk.X, pady=4)
        self.combo.bind('<<ComboboxSelected>>', self._select)
        views = ttk.Frame(body); views.pack(fill=tk.X, pady=4)
        ttk.Label(views, text='Graph view:').pack(side=tk.LEFT)
        mode = ttk.Combobox(views, textvariable=self.mode, values=('Last 60 s','Last 5 min','Full session','Manual'), state='readonly', width=15)
        mode.pack(side=tk.LEFT, padx=6); mode.bind('<<ComboboxSelected>>', self._view)
        ttk.Checkbutton(views, text='5 s median / min/max band', variable=self.smooth).pack(side=tk.LEFT)
        exports = ttk.Frame(body); exports.pack(fill=tk.X, pady=4)
        ttk.Button(exports, text='Export CSV bundle...', command=self.export_csv).pack(side=tk.LEFT)
        ttk.Button(exports, text='Export PNG...', command=self.export_png).pack(side=tk.LEFT, padx=6)
        ttk.Combobox(exports, textvariable=self.target, values=('Combined','RSSI','Success','Latency','Distance'), state='readonly', width=12).pack(side=tk.LEFT)
        marks = ttk.LabelFrame(body, text='Timestamped checkpoint', padding=6); marks.pack(fill=tk.X, pady=8)
        ttk.Label(marks, text='Note:').grid(row=0, column=0)
        ttk.Entry(marks, textvariable=self.marker).grid(row=0, column=1, sticky='ew', padx=6)
        ttk.Label(marks, text='Distance (m), optional:').grid(row=1, column=0)
        ttk.Entry(marks, textvariable=self.distance).grid(row=1, column=1, sticky='ew', padx=6)
        ttk.Button(marks, text='Add marker', command=self.add_marker).grid(row=0, column=2, rowspan=2)
        marks.columnconfigure(1, weight=1)
        for var in (self.status, self.summary, self.paths):
            ttk.Label(body, textvariable=var, wraplength=700, justify=tk.LEFT).pack(fill=tk.X, pady=5)
        ttk.Label(body, text='RSSI measures satellite to ground reception only. Failed probes can be caused by either direction. '
            'Missing or unverified RSSI stays missing; successful packets alone do not prove a reliable link.\n'
            'Use unicast and your working baud/AP settings. A 64-bit destination cannot verify a 16-bit receive source; '
            'enter the satellite radio MY address above if necessary. DB fallback is approximate and capped near 1 Hz.\n'
            'Graphs refresh twice per second; every available sample is logged. Full-session logs are loaded after Stop; '
            'long plots use a bounded envelope. Choose Manual to zoom/pan without automatic scrolling.',
            wraplength=700, justify=tk.LEFT).pack(fill=tk.X, pady=8)
        body.bind('<Configure>', self._wrap)
        app.themes.add_listener(self._theme)
        self._theme(app.themes.colors)
        self._tick()

    def _wrap(self, event):
        for child in self.scroll.inner.winfo_children():
            if isinstance(child, ttk.Label):
                child.configure(wraplength=max(180, event.width-20))

    def _theme(self, colors):
        self.scroll.canvas.configure(background=colors['surface'])
        if self.plot is not None:
            self.plot.theme(colors)

    def _controls(self):
        self.start_button.configure(state=tk.DISABLED if self.busy else tk.NORMAL)
        self.stop_button.configure(state=tk.NORMAL if self.busy else tk.DISABLED)
        for widget, idle in self.inputs:
            widget.configure(state=tk.DISABLED if self.busy else idle)

    def start(self):
        if self.closed or self.busy:
            return
        app, port = self.app, self.app.serial_connection
        if not app.is_connected or app.test_mode or not getattr(port, 'api_mode', False):
            self.status.set('Connect a real ground radio using XBee API (AP=1) before tracking.'); return
        worker = app._transfer_thread
        if app._transfer_active.is_set() or worker is not None and worker.is_alive() or app.live_preview.running:
            self.status.set('Stop Live Preview and wait for camera/file operations to finish first.'); return
        if app.camera.pending or app.live_preview.pending or app.live_preview.invalid_pending:
            self.status.set('Resolve pending captures on Camera/Live Preview before a link-only experiment.'); return
        try:
            text = self.source.get().strip().lower().removeprefix('0x')
            if text and (len(text) > 16 or any(c not in '0123456789abcdef' for c in text)):
                raise ValueError('Expected source must be a hexadecimal radio address')
            source = int(text, 16) if text else None
            if source == 0xffff:
                raise ValueError('Expected source must identify one radio, not a broadcast address')
            configuration = dict(baud=getattr(port, 'baudrate', None), ap=1,
                                 ground_port=getattr(port.port, 'port', None), rf_payload_limit=port.chunk_size,
                                 destination=f'{port.destination:016x}', expected_source=text or 'destination',
                                 note='MAC retries remain enabled as configured in XCTU; no radio settings are changed')
            record = link.Recording('link_tracking', self.name.get().strip()[:100] or 'Range test',
                                    int(self.rate.get()), float(self.timeout.get()), configuration)
        except (ValueError, OSError) as exc:
            self.status.set('Cannot start: '+str(exc)); return
        self.active = self.recording = record
        self.history.appendleft(record)
        self.combo.configure(values=[r.id+' | '+r.name for r in self.history])
        self.selection.set(record.id+' | '+record.name)
        self.full_data = None
        self.cancel = cancel = threading.Event()
        self.busy = True
        app._transfer_cancel = cancel
        app._transfer_active.set()
        port.set_transfer_cancel(cancel)
        app.serial_buffer = ''
        def run():
            try:
                with app._serial_read_lock:
                    link.Tracker(port, record, cancel, source).run()
            except Exception as exc:
                record.finish('failed', str(exc))
            finally:
                port.set_transfer_cancel(None)
                if app._transfer_cancel is cancel:
                    app._transfer_active.clear()
        self.worker = app._transfer_thread = threading.Thread(target=run, daemon=True)
        try:
            self.worker.start()
        except Exception as exc:
            self.busy = False
            port.set_transfer_cancel(None); app._transfer_active.clear()
            record.finish('failed', str(exc))
        self._controls()
        self.status.set('Tracking started. No application retries; late/missed replies remain visible.')
        self.paths.set(str(record.directory.resolve()))

    def stop(self, reason='operator_stop'):
        if self.busy and self.cancel is not None:
            try:
                self.active.mark(reason)
            except OSError as exc:
                self.status.set('Logging error: '+str(exc))
            self.cancel.set()
            self.status.set('Stopping tracking...')

    def _select(self, event=None):
        if self.io_worker is not None and self.io_worker.is_alive():
            self.status.set('Wait for the current export/load to finish.'); return
        self.recording = next((r for r in self.history if self.selection.get().startswith(r.id)), None)
        self.full_data = None
        self._view()

    def _io(self, kind, work):
        if self.io_worker is not None and self.io_worker.is_alive():
            self.status.set('Wait for the current export/load to finish.'); return False
        # The worker captures only plain recording data and this mailbox, not
        # the Tk tab. It can finish an immutable-file operation after shutdown
        # without retaining Tk variables whose destructor needs Tk's thread.
        completion = []
        self.io_kind, self.io_result = kind, completion
        record = self.recording
        def run():
            try:
                completion.append((record, work(), None))
            except Exception as exc:
                completion.append((record, None, str(exc)))
        self.io_worker = threading.Thread(target=run, daemon=True); self.io_worker.start()
        return True

    def _view(self, event=None):
        self.last_draw = 0
        record = self.recording
        if self.mode.get() == 'Full session' and record is not None:
            if record.status == 'running':
                self.status.set('During tracking, graphs use bounded recent history. Stop to load the complete session.')
            elif self.full_data is None:
                def load():
                    snap = record.snapshot()
                    snap['packets'] = link.bounded_rows(record.directory/'packets.csv')
                    snap['probe_rows'] = link.bounded_rows(record.directory/'probes.csv')
                    snap['events'] = link.bounded_rows(record.directory/'events.csv')
                    snap['decimated'] = any(count > link.LIMIT for count in record.row_counts.values())
                    return snap
                if self._io('full', load):
                    self.status.set('Loading full-session plot envelope from CSV...')

    def add_marker(self):
        if not self.busy or self.active is None:
            self.status.set('Start tracking before adding a checkpoint.'); return
        try:
            distance = float(self.distance.get()) if self.distance.get().strip() else None
            self.active.mark('marker', self.marker.get().strip() or 'Checkpoint', distance)
            self.marker.set('')
        except (ValueError, OSError) as exc:
            self.status.set('Marker not saved: '+str(exc))

    def show_graphs(self):
        if self.app.qt_app is None:
            messagebox.showinfo('Graphs unavailable', 'PyQtGraph and PyQt5 are required. Raw recording still works.', parent=self.app.root)
            return False
        if self.plot is None:
            self.plot = LinkPlot(self)
        self.plot.window.show(); self.plot.window.raise_()
        self.last_draw = 0
        return True

    def export_csv(self):
        record = self.recording
        if record is None or record.status == 'running':
            self.status.set('Stop tracking, then select a session to export. Raw logs are already on disk.'); return
        path = filedialog.asksaveasfilename(parent=self.app.root, title='Export Link Tracker CSV bundle',
                    defaultextension='.zip', initialfile=record.id+'.zip', filetypes=[('CSV bundle ZIP','*.zip')])
        if path:
            if self._io('export', lambda: record.export(path)):
                self.status.set('Exporting CSV bundle...')

    def export_png(self):
        if self.recording is None:
            self.status.set('Start or select a recording first.'); return
        if self.mode.get() == 'Full session' and self.full_data is None:
            self._view(); self.status.set('Stop tracking and wait for full-session loading before exporting this view.'); return
        path = filedialog.asksaveasfilename(parent=self.app.root, title='Export current Link Tracker graph view',
                    defaultextension='.png', initialfile=self.recording.id+'.png', filetypes=[('PNG','*.png')])
        if path and self.show_graphs():
            try:
                self._draw()
                self.app.qt_app.processEvents()
                self.plot.save(path, self.target.get())
            except Exception as exc:
                self.status.set('PNG export failed: '+str(exc))

    def _draw(self):
        if self.plot is None or self.recording is None:
            return
        snap = self.full_data if self.mode.get() == 'Full session' and self.full_data is not None else self.recording.snapshot()
        # Do not smooth or search distance checkpoints through hours of hidden
        # data on every redraw. Durable CSV and Full session remain available.
        if self.mode.get().startswith('Last'):
            cutoff = snap['elapsed']-(65 if self.mode.get() == 'Last 60 s' else 305)
            snap['packets'] = [r for r in snap['packets'] if float(r['elapsed_s']) >= cutoff]
            snap['probe_rows'] = [r for r in snap['probe_rows'] if float(r['elapsed_s']) >= cutoff-30]
        self.plot.draw(snap, self.mode.get(), self.smooth.get())

    def _tick(self):
        self.timer = None
        if self.closed or self.app._closing:
            return
        if self.busy and self.worker is not None and not self.worker.is_alive():
            self.busy = False
            self._controls()
            snap = self.active.snapshot()
            self.status.set('Tracking '+snap['status']+('. '+snap['detail'] if snap['detail'] else '. Logs saved.'))
            self._view()
        if self.io_worker is not None and not self.io_worker.is_alive() and self.io_result:
            record, data, error = self.io_result[0]
            self.io_worker = self.io_result = None
            if error:
                self.status.set('File operation failed: '+error)
            elif self.io_kind == 'full' and record is self.recording:
                self.full_data = data
                self.status.set('Full-session envelope loaded; raw CSV preserves every recorded row.')
            elif self.io_kind == 'export':
                self.status.set('CSV bundle exported.')
        if self.recording is not None:
            snap = self.recording.snapshot()
            fresh = snap['rssi'] is not None and snap['age'] <= 2
            signal = f'{snap["rssi"]} dBm, age {snap["age"]:.1f} s' if fresh else 'no fresh verified RSSI'
            success = f'{snap["success_pct"]:.1f}%' if snap['success_pct'] is not None else '--'
            latest = snap['probe_rows'][-1] if snap['probe_rows'] else None
            latency = f'{latest["latency_ms"]:.1f} ms' if latest and latest['latency_ms'] != '' else '--'
            last_rf = next((r for r in reversed(snap['packets']) if r['event'] == 'packet'), None)
            source_note = f' | RX 0x{last_rf["frame"]} source {last_rf["source"]}' if last_rf else ''
            note = ' | Enter the expected satellite receive source to verify RSSI.' if last_rf and not last_rf['source_verified'] else ''
            error = ' | RSSI: '+snap['rssi_error'] if snap['rssi_error'] else ''
            self.summary.set(f'{snap["elapsed"]:.1f} s | {signal} | probe success {success} ({snap["successes"]}/{snap["probes"]}) | '
                f'last latency {latency}\nRequested {snap["requested_hz"]} Hz | achieved {snap["actual_hz"]:.2f} probes/s | '
                f'verified RSSI {snap["measurement_hz"]:.2f}/s | {snap["method"]} | '
                f'skipped slots {snap["skipped"]} | metadata lost {snap["dropped"]} | '
                f'RSSI errors {snap["rssi_errors"]}{source_note}{note}{error}')
        if self.plot is not None:
            if time.monotonic()-self.last_draw >= .5:
                self._draw(); self.last_draw = time.monotonic()
            self.app.qt_app.processEvents()
        self.timer = self.app.root.after(250, self._tick)

    def close(self):
        if self.closed:
            return
        self.stop('application_closed')
        self.closed = True
        if self.timer is not None:
            self.app.root.after_cancel(self.timer); self.timer = None
        if self.worker is not None and self.worker.is_alive():
            self.worker.join(timeout=2)
        if self.plot is not None:
            self.plot.close()
