"""Transfer Statistics tab with live PyQtGraph plots and PNG/CSV export."""
from pathlib import Path
import math
import html
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from . import themes, scaling


class StatisticsTab:
    def __init__(self, app):
        self.app = app
        self.frame = ttk.Frame(app.notebook, padding=10)
        app.notebook.add(self.frame, text='Transfer Stats')
        self.window = None
        self._timer = None
        self._newest = None
        self._sessions = {}
        self._plot_key = None
        self.selection = tk.StringVar()
        self.summary = tk.StringVar(value='A recording starts automatically for file downloads and ZMODEM sends/receives.')
        self.rssi_enabled = tk.BooleanVar(value=bool(app.settings.get('transfer_rssi', True)))
        self.export_target = tk.StringVar(value='Combined')

        ttk.Label(self.frame, text='Transfer session:').pack(anchor=tk.W)
        self.combo = ttk.Combobox(self.frame, textvariable=self.selection, state='readonly')
        self.combo.pack(fill=tk.X, pady=(4, 8))
        self.combo.bind('<<ComboboxSelected>>', lambda event: self._refresh())
        controls = ttk.Frame(self.frame)
        controls.pack(fill=tk.X)
        ttk.Button(controls, text='Show live graphs', command=self.show_graphs).pack(side=tk.LEFT)
        ttk.Button(controls, text='Export CSV...', command=self.export_csv).pack(side=tk.LEFT, padx=6)
        export = ttk.Frame(self.frame)
        export.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(export, text='Export PNG...', command=self.export_png).pack(side=tk.LEFT)
        ttk.Combobox(export, textvariable=self.export_target, state='readonly', width=12,
                     values=('Combined', 'Rates', 'RSSI')).pack(side=tk.LEFT, padx=6)
        ttk.Checkbutton(self.frame, text='Poll ground receiver RSSI during transfers (API mode only)',
                        variable=self.rssi_enabled, command=self._rssi_changed).pack(anchor=tk.W, pady=(12, 6))
        self.summary_label = ttk.Label(self.frame, textvariable=self.summary, wraplength=540, justify=tk.LEFT)
        self.summary_label.pack(anchor=tk.W, fill=tk.X, pady=8)
        ttk.Label(self.frame, text='Rates use one-second intervals. RSSI is the last received RF packet, in dBm.\n'
                  'Stale RSSI appears as gaps. Graphs retain the last two hours; CSV logs retain the full session.\n'
                  'The most recent 20 sessions remain selectable. Full CSV logs are saved in transfer_statistics/.',
                  wraplength=540, justify=tk.LEFT).pack(anchor=tk.W, fill=tk.X)
        self.frame.bind('<Configure>', self._resize)
        app.themes.add_listener(self._theme)
        self._tick()

    def _resize(self, event):
        for widget in self.frame.winfo_children():
            if isinstance(widget, ttk.Label):
                widget.configure(wraplength=max(160, event.width-24))

    def _rssi_changed(self):
        self.app._write_settings({'transfer_rssi': bool(self.rssi_enabled.get())})
        session = self.app.transfer_recorder.active
        if session and session.status == 'running':
            with session.lock:
                session.rssi_enabled = bool(self.rssi_enabled.get() and getattr(session.port, 'api_mode', False))

    def selected_session(self):
        return self._sessions.get(self.selection.get())

    def _tick(self):
        if self.app._closing:
            return
        try:
            self.app.transfer_recorder.tick()
            active = self.app.transfer_recorder.active
            port = self.app.serial_connection
            if port is not None and hasattr(port, 'set_rssi_polling'):
                port.set_rssi_polling(bool(active and active.status == 'running' and self.rssi_enabled.get()))
            self._refresh()
            if self.window is not None and self.app.qt_app is not None:
                self.app.qt_app.processEvents()
        finally:
            self._timer = self.app.root.after(250, self._tick)

    def _refresh(self):
        sessions = self.app.transfer_recorder.history()
        mapping = {}
        for session in sessions:
            snap = session.snapshot()
            label = f'{snap["started_utc"]} | {snap["protocol"]} {snap["direction"]} | {snap["id"][-8:]}'
            mapping[label] = session
        self._sessions = mapping
        values = tuple(mapping)
        if tuple(self.combo['values']) != values:
            self.combo.configure(values=values)
        newest = sessions[0].id if sessions else None
        if newest != self._newest:
            self._newest = newest
            self.selection.set(values[0] if values else '')
        session = self.selected_session()
        if session is None:
            return
        snap = session.snapshot()
        samples = snap['samples']
        last = samples[-1] if samples else {}
        rate = last.get('download_Bps', 0)+last.get('upload_Bps', 0)
        rssi = last.get('rssi_dBm')
        rssi_text = f'{rssi} dBm' if rssi is not None else (snap['rssi_note'] or 'unavailable / stale / disabled')
        retry = snap['retries'] if snap['retries'] is not None else 'not tracked for ZMODEM'
        text = (f'{snap["name"]}\n{snap["protocol"]} {snap["direction"]} | {snap["status"]} | '
                f'{snap["mode"]} | {snap["baud"]} baud\n'
                f'{snap["done"]:,} / {snap["total"]:,} useful file bytes | {rate:.1f} B/s current | '
                f'{last.get("average_Bps", 0):.1f} B/s average\n'
                f'Elapsed: {last.get("elapsed_s", 0):.1f} s | Protocol retries: {retry}\n'
                f'Ground receiver RSSI: {rssi_text}\nCSV log: {snap["csv_path"]}')
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
                def closeEvent(self, event):
                    owner.window = None
                    owner._plot_key = None
                    super().closeEvent(event)
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
            self._theme(themes.CURRENT)
        self._refresh()
        self.window.show()
        self.app.qt_app.processEvents()
        return True

    def _theme(self, colors):
        if self.window is None:
            return
        import pyqtgraph as pg
        self.window.setBackground(colors['plot_bg'])
        self.header.setText(self.header.text, color=colors['plot_fg'])
        palette = colors['plot_palette']
        self.download_curve.setPen(pg.mkPen(palette[0], width=2))
        self.upload_curve.setPen(pg.mkPen(palette[1], width=2))
        self.rssi_curve.setPen(pg.mkPen(palette[2], width=2))
        self.rssi_curve.setSymbolBrush(palette[2])
        self.rssi_curve.setSymbolPen(palette[2])
        for plot in (self.rate_plot, self.rssi_plot):
            plot.showGrid(x=True, y=True, alpha=colors['plot_grid_alpha'])
            for name in ('left', 'bottom'):
                plot.getAxis(name).setPen(colors['plot_fg'])
                plot.getAxis(name).setTextPen(colors['plot_fg'])
            plot.titleLabel.setText(plot.titleLabel.text, color=colors['plot_fg'])
        if self.rate_plot.legend:
            self.rate_plot.legend.setLabelTextColor(colors['plot_fg'])

    def _draw(self, snap):
        samples = snap['samples']
        key = (snap['id'], len(samples), samples[-1]['elapsed_s'] if samples else None, snap['status'])
        if key == self._plot_key:
            return
        self._plot_key = key
        x = [s['elapsed_s'] for s in samples]
        self.download_curve.setData(x, [s['download_Bps'] for s in samples])
        self.upload_curve.setData(x, [s['upload_Bps'] for s in samples])
        rssi = [s['rssi_dBm'] if s['rssi_dBm'] is not None else math.nan for s in samples]
        if any(math.isfinite(value) for value in rssi):
            self.rssi_curve.setData(x, rssi, connect='finite')
        else:
            self.rssi_curve.setData([], [])
        caption = html.escape(f'{snap["name"]} | {snap["protocol"]} {snap["direction"]} | '
                              f'{snap["baud"]} baud | {snap["status"]} | {snap["started_utc"]}')
        self.header.setText(caption, color=themes.CURRENT['plot_fg'])
        self.window.setWindowTitle(f'Transfer statistics | {snap["name"]} | {snap["status"]}')

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

    def save_png(self, path, target='Combined'):
        if not self.show_graphs():
            raise RuntimeError('Graphs unavailable')
        import pyqtgraph.exporters
        item = self.window.scene() if target == 'Combined' else (self.rate_plot if target == 'Rates' else self.rssi_plot)
        exporter = pyqtgraph.exporters.ImageExporter(item)
        exporter.parameters()['width'] = 1600
        exporter.export(str(path))

    def export_png(self):
        session = self.selected_session()
        if session is None:
            messagebox.showinfo('No transfer', 'Start a file transfer to record statistics first.')
            return
        path = filedialog.asksaveasfilename(title='Export transfer graph', defaultextension='.png',
                                          initialfile=session.id+'.png', filetypes=[('PNG image', '*.png')])
        if path:
            try:
                self.save_png(path, self.export_target.get())
            except Exception as exc:
                messagebox.showerror('PNG export failed', str(exc))

    def close(self):
        if self._timer is not None:
            self.app.root.after_cancel(self._timer)
            self._timer = None
        if self.window is not None:
            self.window.close()
