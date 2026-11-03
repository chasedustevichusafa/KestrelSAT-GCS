"""Transfer Statistics tab with live PyQtGraph plots and PNG/CSV export."""

# READING GUIDE - presentation and exports for transfer measurements.
# Tk owns this tab, schedules _tick and selects Session/Recording snapshots.
# Separate PyQtGraph windows display rate and RSSI curves; CSV measurements remain
# owned by the recorder rather than being reconstructed from plotted pixels.
#
# The RSSI checkbox controls optional local ground-radio DB polling during an
# active measured transfer. It does not issue an RF command to the satellite or
# change XCTU settings. Missing RSSI is a gap, never an invented zero dBm sample.
# PNG export can show the full durable timeline or the current graph range; the
# chosen export extent matters when comparing benchmark configurations.
from pathlib import Path
import math
import html
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from . import themes, scaling, graph_style
from .widgets import ScrollableFrame


class StatisticsTab:
    def __init__(self, app):
        self.app = app
        self.frame = ttk.Frame(app.notebook, padding=10)
        app.notebook.add(self.frame, text='Transfer Stats')
        self.window = None
        self._timer = None
        self._combine_worker = None
        self._newest = None
        self._sessions = {}
        self._plot_key = None
        self._markers = []
        self.overlay_view = self.overlay_curve = None
        self._overlay_enabled = False
        self.overlay_rssi = tk.BooleanVar(value=bool(app.settings.get('transfer_rssi_overlay', False)))
        self.selection = tk.StringVar()
        self.summary = tk.StringVar(value='Transfers are recorded individually. Start a named recording to capture multiple operations and idle gaps.')
        self.rssi_enabled = tk.BooleanVar(value=bool(app.settings.get('transfer_rssi', True)))
        self.export_target = tk.StringVar(value='Combined')
        self.png_extent = tk.StringVar(value='Full timeline')
        self.scroll = ScrollableFrame(self.frame, vscroll=True, hscroll=True)
        self.scroll.pack(fill=tk.BOTH, expand=True)
        self.body = self.scroll.inner
        self.scroll.canvas.configure(background=themes.CURRENT['surface'])

        recording_controls = ttk.Frame(self.body)
        recording_controls.pack(fill=tk.X, pady=(0, 6))
        self.start_button = ttk.Button(recording_controls, text='Start recording...', command=self.start_recording)
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(recording_controls, text='Stop recording', command=self.stop_recording, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=6)
        self.combine_button = ttk.Button(recording_controls, text='Combine transfers...', command=self.combine_transfers)
        self.combine_button.pack(side=tk.LEFT)
        self.recording_status = tk.StringVar(value='No multi-operation recording running.')
        ttk.Label(self.body, textvariable=self.recording_status).pack(anchor=tk.W, pady=(0, 6))
        ttk.Label(self.body, text='Recording or individual transfer:').pack(anchor=tk.W)
        self.combo = ttk.Combobox(self.body, textvariable=self.selection, state='readonly')
        self.combo.pack(fill=tk.X, pady=(4, 8))
        self.combo.bind('<<ComboboxSelected>>', lambda event: self._refresh())
        controls = ttk.Frame(self.body)
        controls.pack(fill=tk.X)
        ttk.Button(controls, text='Show live graphs', command=self.show_graphs).pack(side=tk.LEFT)
        ttk.Button(controls, text='Export CSV...', command=self.export_csv).pack(side=tk.LEFT, padx=6)
        export = ttk.Frame(self.body)
        export.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(export, text='Export PNG...', command=self.export_png).pack(side=tk.LEFT)
        ttk.Combobox(export, textvariable=self.export_target, state='readonly', width=12,
                     values=('Combined', 'Rates', 'RSSI')).pack(side=tk.LEFT, padx=6)
        self.operations_button = ttk.Button(export, text='Export operations...', command=self.export_operations)
        self.operations_button.pack(side=tk.LEFT, padx=6)
        scope = ttk.Frame(self.body)
        scope.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(scope, text='PNG time range:').pack(side=tk.LEFT)
        ttk.Combobox(scope, textvariable=self.png_extent, state='readonly', width=16,
                     values=('Full timeline', 'Current view')).pack(side=tk.LEFT, padx=6)
        ttk.Checkbutton(self.body, text='Overlay RSSI on speed graph (right dBm axis)',
                        variable=self.overlay_rssi, command=self._overlay_changed).pack(anchor=tk.W, pady=(8, 0))
        ttk.Checkbutton(self.body, text='Record ground receiver RSSI during transfers (API mode only)',
                        variable=self.rssi_enabled, command=self._rssi_changed).pack(anchor=tk.W, pady=(12, 6))
        self.summary_label = ttk.Label(self.body, textvariable=self.summary, wraplength=540, justify=tk.LEFT)
        self.summary_label.pack(anchor=tk.W, fill=tk.X, pady=8)
        ttk.Label(self.body, text='Rates use one-second intervals. RSSI is the last received RF packet, in dBm.\n'
                  'Idle gaps use zero file throughput; stale RSSI appears as gaps. Graphs retain 7200 samples.\n'
                  'Full CSV logs, operation summaries and events are saved in transfer_statistics/.',
                  wraplength=540, justify=tk.LEFT).pack(anchor=tk.W, fill=tk.X)
        self.body.bind('<Configure>', self._resize)
        app.themes.add_listener(self._theme)
        self._tick()

    def start_recording(self):
        name = simpledialog.askstring('Start recording', 'Name this multi-operation recording:',
                                      initialvalue='Transfer test', parent=self.app.root)
        if name is None:
            return
        try:
            recording = self.app.transfer_recorder.start_recording(name.strip() or 'Transfer test')
            self._refresh()
            self._select(recording)
        except RuntimeError as exc:
            messagebox.showinfo('Recording unavailable', str(exc), parent=self.app.root)

    def stop_recording(self):
        self.app.transfer_recorder.stop_recording()
        self._refresh()

    def _select(self, session):
        for label, value in self._sessions.items():
            if value is session:
                self.selection.set(label)
                break
        self._refresh()

    def combine_transfers(self):
        sessions = [session for session in self.app.transfer_recorder.history()
                    if session.snapshot()['status'] != 'running']
        if len(sessions) < 2:
            messagebox.showinfo('Combine transfers', 'Complete at least two transfers first.', parent=self.app.root)
            return
        dialog = tk.Toplevel(self.app.root)
        dialog.title('Combine finished transfers')
        dialog.transient(self.app.root)
        footer = ttk.Frame(dialog)
        footer.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=10)
        ttk.Label(dialog, text='Select transfers from this application run. Original timing and gaps are preserved.',
                  wraplength=520).pack(padx=12, pady=8)
        name = tk.StringVar(value='Combined transfer test')
        ttk.Entry(dialog, textvariable=name).pack(fill=tk.X, padx=12)
        choices = tk.Listbox(dialog, selectmode=tk.EXTENDED, exportselection=False, width=72, height=10)
        choices.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        for session in sessions:
            snap = session.snapshot()
            choices.insert(tk.END, f'{snap["started_utc"]} | {snap["direction"]} | {snap["name"]} | {snap["status"]}')
        def combine():
            selected = [sessions[index] for index in choices.curselection()]
            if len(selected) < 2:
                messagebox.showinfo('Select transfers', 'Select at least two finished transfers.', parent=dialog)
                return
            title = name.get().strip() or 'Combined transfer test'
            dialog.destroy()
            self.combine_button.configure(state=tk.DISABLED)
            def worker():
                try:
                    recording = self.app.transfer_recorder.combine(selected, title)
                    payload = dict(recording=recording)
                except Exception as exc:
                    payload = dict(error=str(exc))
                self.app._post_rx(('stats_combined', payload))
            self._combine_worker = threading.Thread(target=worker, daemon=True)
            self._combine_worker.start()
        ttk.Button(footer, text='Combine selected', command=combine).pack(side=tk.LEFT)
        ttk.Button(footer, text='Cancel', command=dialog.destroy).pack(side=tk.LEFT, padx=6)
        self.app.themes.restyle(dialog)
        self.app._prepare_dialog(dialog)
        dialog.grab_set()

    def on_combined(self, payload):
        self.combine_button.configure(state=tk.NORMAL)
        if 'error' in payload:
            messagebox.showerror('Combine failed', payload['error'], parent=self.app.root)
        else:
            self._refresh()
            self._select(payload['recording'])

    def export_operations(self):
        recording = self.selected_session()
        if recording is None or not recording.snapshot().get('is_recording'):
            messagebox.showinfo('Select a recording', 'Select a multi-operation recording first.', parent=self.app.root)
            return
        path = filedialog.asksaveasfilename(title='Export operation summary', defaultextension='.csv',
                                          initialfile=recording.id+'.operations.csv', filetypes=[('CSV', '*.csv')])
        if path:
            try:
                recording.export_csv(path, kind='operations')
            except OSError as exc:
                messagebox.showerror('CSV export failed', str(exc), parent=self.app.root)

    def _resize(self, event):
        for widget in self.body.winfo_children():
            if isinstance(widget, ttk.Label):
                widget.configure(wraplength=max(160, event.width-24))

    def _rssi_changed(self):
        self.app._write_settings({'transfer_rssi': bool(self.rssi_enabled.get())})
        self.app.transfer_recorder.event('configuration', f'RSSI polling enabled: {bool(self.rssi_enabled.get())}')
        session = self.app.transfer_recorder.active
        if session and session.status == 'running':
            with session.lock:
                session.rssi_enabled = bool(self.rssi_enabled.get() and getattr(session.port, 'api_mode', False))

    def selected_session(self):
        return self._sessions.get(self.selection.get())

    # Runs from Tk's scheduled callback. Advance measurements, enable optional
    # RSSI polling only for the appropriate active session, then refresh display.
    # A closed graph window does not stop measurement or the durable CSV stream.
    def _tick(self):
        if self.app._closing:
            return
        try:
            self.app.transfer_recorder.tick()
            active = self.app.transfer_recorder.active
            port = self.app.serial_connection
            if port is not None and hasattr(port, 'set_rssi_polling'):
                tracker = getattr(self.app, 'link_tracker', None)
                port.set_rssi_polling(bool(tracker and tracker.busy and self.app._transfer_active.is_set() or
                    active and active.status == 'running' and self.rssi_enabled.get()))
            self._refresh()
            if self.window is not None and self.app.qt_app is not None:
                self.app.qt_app.processEvents()
        finally:
            self._timer = self.app.root.after(250, self._tick)

    def _refresh(self):
        recorder = self.app.transfer_recorder
        recording = recorder.recording
        self.start_button.configure(state=tk.DISABLED if recording else tk.NORMAL)
        self.stop_button.configure(state=tk.NORMAL if recording else tk.DISABLED)
        if recording:
            current = recording.snapshot()
            self.recording_status.set(f'Recording: {current["name"]} | {current["elapsed"]:.1f} s | '
                                      f'{current["operation_count"]} finished operations | '
                                      f'{current["current_operation"] or "Idle"}')
        else:
            self.recording_status.set('No multi-operation recording running.')
        sessions = recorder.recording_history()+recorder.history()
        mapping = {}
        for session in sessions:
            snap = session.snapshot()
            label = (f'Recording | {snap["name"]} | {snap["id"][-8:]}' if snap.get('is_recording') else
                     f'{snap["started_utc"]} | {snap["protocol"]} {snap["direction"]} | {snap["id"][-8:]}')
            mapping[label] = session
        self._sessions = mapping
        values = tuple(mapping)
        if tuple(self.combo['values']) != values:
            self.combo.configure(values=values)
        newest = sessions[0].id if sessions else None
        if newest != self._newest:
            self._newest = newest
            if self.selection.get() not in mapping:
                self.selection.set(values[0] if values else '')
            elif not self.selected_session().snapshot().get('is_recording'):
                self.selection.set(values[0] if values else '')
        session = self.selected_session()
        if session is None:
            return
        snap = session.snapshot()
        self.operations_button.configure(state=tk.NORMAL if snap.get('is_recording') else tk.DISABLED)
        samples = snap['samples']
        last = samples[-1] if samples else {}
        rate = last.get('download_Bps', 0)+last.get('upload_Bps', 0)
        rssi = last.get('rssi_dBm')
        rssi_text = f'{rssi} dBm' if rssi is not None else (snap.get('rssi_note') or 'unavailable / stale / disabled')
        if snap.get('is_recording'):
            text = (f'{snap["name"]} | {snap["status"]} | {snap["operation_count"]} finished operations\n'
                    f'{snap["download_bytes"]:,} downloaded / {snap["upload_bytes"]:,} uploaded bytes\n'
                    f'Elapsed: {snap["elapsed"]:.1f} s | Transfer time: {snap["active_seconds"]:.1f} s\n'
                    f'{rate:.1f} B/s current | {last.get("active_average_Bps", 0):.1f} B/s during transfers | '
                    f'{last.get("recording_average_Bps", 0):.1f} B/s including gaps\n'
                    f'Ground receiver RSSI: {rssi_text}\nCSV log: {snap["csv_path"]}\n'
                    f'Operations: {snap["operations_path"]}\nEvents: {snap["events_path"]}')
        else:
            retry = snap['retries'] if snap['retries'] is not None else 'not tracked for ZMODEM'
            text = (f'{snap["name"]}\n{snap["protocol"]} {snap["direction"]} | {snap["status"]} | '
                    f'{snap["mode"]} | {snap["baud"]} baud\n'
                    f'{snap["done"]:,} / {snap["total"]:,} useful file bytes | {rate:.1f} B/s current | '
                    f'{last.get("average_Bps", 0):.1f} B/s average\n'
                    f'Elapsed: {last.get("elapsed_s", 0):.1f} s | Protocol retries: {retry}\n'
                    f'Ground receiver RSSI: {rssi_text}\nCSV log: {snap["csv_path"]}')
            if snap.get('transfer_profile'):
                text += '\nTransfer profile: '+snap['transfer_profile']
        if snap['error']:
            text += '\n'+snap['error']
        self.summary.set(text)
        if self.window is not None:
            self._draw(snap)

    def show_graphs(self):
        if self.app.qt_app is None:
            messagebox.showinfo('Graphs unavailable', 'PyQtGraph and PyQt5 are required for graphs and PNG export.')
            return False
        if self.window is None:
            import pyqtgraph as pg
            owner = self
            class Window(pg.GraphicsLayoutWidget):
                def close(self):
                    # GraphicsView.close clears the C++ scene BEFORE closeEvent.
                    # Disconnect/remove the sibling ViewBox while it is alive.
                    if owner.window is self:
                        owner._dispose_overlay()
                    return super().close()

                def closeEvent(self, event):
                    if owner.window is self:
                        owner._dispose_overlay()
                        owner.window = None
                        owner._plot_key = None
                    super().closeEvent(event)
            self._markers = []
            self.window = Window()
            self.window.setWindowTitle('Transfer speed and ground receiver RSSI')
            self.window.resize(scaling.px(self.app.ui_scale, 900), scaling.px(self.app.ui_scale, 640))
            self.header = self.window.addLabel('Transfer statistics', row=0, col=0)
            self.rate_plot = self.window.addPlot(row=1, col=0, title='Useful file transfer rate')
            self.rate_plot.setLabel('left', 'Rate', units='B/s')
            self.rate_plot.setLabel('bottom', 'Elapsed time', units='s')
            self.rate_plot.addLegend()
            self.download_curve = self.rate_plot.plot(name='Download')
            self.upload_curve = self.rate_plot.plot(name='Upload')
            self.rssi_plot = self.window.addPlot(row=2, col=0, title='Ground receiver RSSI (last RF packet)')
            self.rssi_plot.setLabel('left', 'RSSI', units='dBm')
            self.rssi_plot.setLabel('bottom', 'Elapsed time', units='s')
            self.rssi_plot.setXLink(self.rate_plot)
            self.rssi_curve = self.rssi_plot.plot(connect='finite', symbol='o', symbolSize=4)
            self._create_overlay()
            self._theme(themes.CURRENT)
            self._apply_overlay(self.overlay_rssi.get())
        self._refresh()
        self.window.show()
        self.app.qt_app.processEvents()
        return True

    def _create_overlay(self):
        """A second Y view shares time but never mixes dBm with the B/s scale."""
        import pyqtgraph as pg
        from PyQt5 import QtCore
        self.overlay_view = pg.ViewBox(enableMenu=False)
        self.overlay_view.setMouseEnabled(x=False, y=False)
        self.overlay_view.setAcceptedMouseButtons(QtCore.Qt.NoButton)
        self.overlay_view.setAcceptHoverEvents(False)
        self.overlay_view.setZValue(-1)
        self.rate_plot.scene().addItem(self.overlay_view)
        self.rate_plot.getAxis('right').linkToView(self.overlay_view)
        self.rate_plot.getAxis('right').setLabel('RSSI', units='dBm')
        self.overlay_view.setXLink(self.rate_plot)
        self.overlay_curve = pg.PlotDataItem(connect='finite', symbol='o', symbolSize=4)
        self.overlay_view.addItem(self.overlay_curve)
        self.rate_plot.getViewBox().sigResized.connect(self._sync_overlay)
        self._overlay_enabled = False
        self.overlay_view.hide()
        self.rate_plot.hideAxis('right')
        self._sync_overlay()

    def _sync_overlay(self):
        if self.overlay_view is not None:
            import pyqtgraph as pg
            view = self.rate_plot.getViewBox()
            self.overlay_view.setGeometry(view.sceneBoundingRect())
            self.overlay_view.linkedViewChanged(view, pg.ViewBox.XAxis)

    def _apply_overlay(self, enabled):
        """Presentation only. Keep both curves' data for standalone RSSI exports."""
        if self.window is None or bool(enabled) == self._overlay_enabled:
            return
        self._overlay_enabled = bool(enabled)
        if enabled:
            self.window.ci.removeItem(self.rssi_plot)
            self.rssi_plot.hide()
            self.rate_plot.showAxis('right')
            self.overlay_view.show()
            self.rate_plot.legend.addItem(self.overlay_curve, 'RSSI (right axis)')
        else:
            self.overlay_view.hide()
            self.rate_plot.hideAxis('right')
            self.window.ci.addItem(self.rssi_plot, row=2, col=0)
            self.rssi_plot.show()
            self.rate_plot.legend.removeItem(self.overlay_curve)
        self._sync_overlay()
        self._theme(self.app.themes.colors)

    def _overlay_changed(self):
        self.app._write_settings({'transfer_rssi_overlay': bool(self.overlay_rssi.get())})
        self._apply_overlay(self.overlay_rssi.get())
        self._plot_key = None
        self._refresh()

    def _dispose_overlay(self):
        # Remove the scene sibling and resize signal when its window closes;
        # reopening must create one fresh ViewBox, not accumulate old views.
        if self.overlay_view is not None:
            self.rate_plot.getViewBox().sigResized.disconnect(self._sync_overlay)
            scene = self.overlay_view.scene()
            self.overlay_view.close()
            if scene is not None:
                scene.removeItem(self.overlay_view)
            self.overlay_view = self.overlay_curve = None
        # A detached standalone RSSI panel is not owned by the window layout.
        if self._overlay_enabled:
            self.window.ci.addItem(self.rssi_plot, row=2, col=0)
            self.rssi_plot.show()
        self._overlay_enabled = False

    def _theme(self, colors):
        self.scroll.canvas.configure(background=colors['surface'])
        if self.window is None:
            return
        import pyqtgraph as pg
        colors = graph_style.colors(self.app, colors)
        self.window.setBackground(colors['plot_bg'])
        self.header.setText(self.header.text, color=colors['plot_fg'])
        palette = colors['plot_palette']
        self.download_curve.setPen(pg.mkPen(palette[0], width=2))
        self.upload_curve.setPen(pg.mkPen(palette[1], width=2))
        self.rssi_curve.setPen(pg.mkPen(palette[2], width=2))
        self.rssi_curve.setSymbolBrush(palette[2])
        self.rssi_curve.setSymbolPen(palette[2])
        self.overlay_curve.setPen(pg.mkPen(palette[2], width=2, style=2))
        self.overlay_curve.setSymbolBrush(palette[2]); self.overlay_curve.setSymbolPen(palette[2])
        for plot in (self.rate_plot, self.rssi_plot):
            graph_style.theme_plot(plot, colors)
        for _, marker in self._markers:
            marker.setPen(pg.mkPen(colors['plot_fg'], width=1, style=2))
            if getattr(marker, 'label', None) is not None:
                marker.label.setColor(colors['plot_fg'])
        if self.rate_plot.legend:
            self.rate_plot.legend.setLabelTextColor(colors['plot_fg'])

    def _draw(self, snap, full=False):
        samples = snap['samples']
        key = (snap['id'], len(samples), samples[-1]['elapsed_s'] if samples else None, snap['status'],
               len(snap.get('events', [])))
        if key == self._plot_key and not full:
            return
        self._plot_key = key
        x = [s['elapsed_s'] for s in samples]
        self.download_curve.setData(x, [s['download_Bps'] for s in samples])
        self.upload_curve.setData(x, [s['upload_Bps'] for s in samples])
        rssi = [s['rssi_dBm'] if s['rssi_dBm'] is not None else math.nan for s in samples]
        has_rssi = any(math.isfinite(value) for value in rssi)
        self.overlay_curve.setData(x if has_rssi else [], rssi if has_rssi else [], connect='finite')
        self.rate_plot.setTitle('Useful file transfer rate' +
            (' + ground RSSI' + ('' if has_rssi else ' (no fresh measurements)') if self._overlay_enabled else ''),
            color=graph_style.colors(self.app)['plot_fg'])
        self.rssi_plot.setTitle('Ground receiver RSSI (last RF packet)' if has_rssi else
                                'Ground receiver RSSI (no fresh measurements)', color=graph_style.colors(self.app)['plot_fg'])
        if has_rssi:
            self.rssi_curve.setData(x, rssi, connect='finite')
        else:
            self.rssi_curve.setData([], [])
        for plot, marker in self._markers:
            plot.removeItem(marker)
        self._markers.clear()
        import pyqtgraph as pg
        events = [event for event in snap.get('events', [])
                  if not samples or samples[0]['elapsed_s'] <= event['elapsed_s'] <= samples[-1]['elapsed_s']][-200:]
        labelled = 0
        for event in events:
            detail = f'{event["elapsed_s"]:.1f}s {event["event"]}: {event["detail"]}'
            for plot in (self.rate_plot, self.rssi_plot):
                label = None
                if plot is self.rate_plot and event['event'] == 'operation_started' and labelled < 20:
                    label = 'Start: '+event['detail'].rsplit('/', 1)[-1][:24]
                    labelled += 1
                marker = pg.InfiniteLine(event['elapsed_s'], angle=90, movable=False, label=label,
                                         labelOpts={'position': 0.95, 'color': graph_style.colors(self.app)['plot_fg']},
                                         pen=pg.mkPen(graph_style.colors(self.app)['plot_fg'], width=1, style=2))
                marker.setToolTip(detail)
                plot.addItem(marker)
                self._markers.append((plot, marker))
        if snap.get('is_recording'):
            caption = (f'{snap["name"]} | {snap["operation_count"]} operations | {snap["elapsed"]:.1f} s | '
                       f'{snap["status"]} | {snap["started_utc"]}')
        else:
            caption = (f'{snap["name"]} | {snap["protocol"]} {snap["direction"]} | '
                       f'{snap["baud"]} baud | {snap["status"]} | {snap["started_utc"]}')
            if snap.get('transfer_profile'):
                caption += ' | '+snap['transfer_profile']
        self.header.setText(html.escape(caption), color=graph_style.colors(self.app)['plot_fg'])
        self.window.setWindowTitle(f'Transfer statistics | {snap["name"]} | {snap["status"]}')
        if full or getattr(self, '_drawn_id', None) != snap['id']:
            self.rate_plot.enableAutoRange()
            self.rssi_plot.enableAutoRange()
            self.overlay_view.enableAutoRange(axis='y')
        self._drawn_id = snap['id']

    def export_csv(self):
        session = self.selected_session()
        if session is None:
            messagebox.showinfo('No transfer', 'Start a file transfer to record statistics first.')
            return
        path = filedialog.asksaveasfilename(title='Export transfer measurements', defaultextension='.csv',
                                          initialfile=session.id+'.csv', filetypes=[('CSV', '*.csv')])
        if path:
            try:
                session.export_csv(path)
            except OSError as exc:
                messagebox.showerror('CSV export failed', str(exc))

    # Export a graph view, not a recalculation of the experiment. Full=True
    # uses the complete available recording; a zoomed export uses the current
    # range. Compare like extents/axes when presenting different configurations.
    def save_png(self, path, target='Combined', full=True):
        if not self.show_graphs():
            raise RuntimeError('Graphs unavailable')
        import pyqtgraph.exporters
        session = self.selected_session()
        original_overlay = self._overlay_enabled
        views = [self.rate_plot.getViewBox(), self.rssi_plot.getViewBox(), self.overlay_view]
        ranges = [(view, view.viewRange(), view.autoRangeEnabled()) for view in views]
        restore_data = full and hasattr(session, 'graph_snapshot')
        try:
            # A standalone RSSI export temporarily restores its panel. The
            # persisted overlay preference and current interactive view survive.
            if target == 'RSSI' and original_overlay:
                self._apply_overlay(False)
            if restore_data:
                self._draw(session.graph_snapshot(), full=True)
                self.rate_plot.autoRange(); self.rssi_plot.autoRange(); self.overlay_view.autoRange()
            self.app.qt_app.processEvents()
            self._sync_overlay()
            item = self.window.scene() if target == 'Combined' else (self.rate_plot if target == 'Rates' else self.rssi_plot)
            exporter = pyqtgraph.exporters.ImageExporter(item)
            exporter.parameters()['width'] = 1600
            exporter.export(str(path))
        finally:
            self._apply_overlay(original_overlay)
            if restore_data:
                self._plot_key = None
                self._draw(session.snapshot())
            for view, ranges_before, auto in ranges:
                view.setRange(xRange=ranges_before[0], yRange=ranges_before[1], padding=0)
                view.enableAutoRange(x=auto[0], y=auto[1])

    def export_png(self):
        session = self.selected_session()
        if session is None:
            messagebox.showinfo('No transfer', 'Start a file transfer to record statistics first.')
            return
        path = filedialog.asksaveasfilename(title='Export transfer graph', defaultextension='.png',
                                          initialfile=session.id+'.png', filetypes=[('PNG image', '*.png')])
        if path:
            try:
                self.save_png(path, self.export_target.get(), full=self.png_extent.get() == 'Full timeline')
            except Exception as exc:
                messagebox.showerror('PNG export failed', str(exc))

    def close(self):
        self.app.transfer_recorder.combine_cancel.set()
        if self._combine_worker is not None:
            self._combine_worker.join(timeout=2.0)
        if self._timer is not None:
            self.app.root.after_cancel(self._timer)
            self._timer = None
        if self.window is not None:
            self.window.close()
