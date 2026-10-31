"""Link experiment plots. No serial I/O; all Qt calls run on the GUI thread."""
from collections import deque
import bisect
import html
import math
import statistics

from . import themes, scaling


def graph_data(snapshot):
    """Preserve missing data as NaN and compute time-based, received-only stats.

    RSSI smoothing uses five seconds of verified received samples. It says
    nothing about packets that never arrived. Probe success uses a separate
    30-second window, including failures, to expose that survivorship bias.
    """
    nan = math.nan
    samples = []
    for row in snapshot['packets']:
        verified = row['source_verified'] in (True, 'True')
        value = row.get('rssi_dBm')
        valid = (row['valid_sample'] in (True, 'True') if 'valid_sample' in row else
                 value not in ('', None) and verified and row['detail'] != 'ambiguous')
        samples.append((float(row['elapsed_s']), float(value) if valid else nan))
    probes = [row for row in snapshot['probe_rows'] if row['outcome'] != 'cancelled']
    samples.extend((float(row['elapsed_s']), nan) for row in probes if row['outcome'] != 'success')
    samples.sort(key=lambda point: point[0])
    x, raw, med, low, high = [], [], [], [], []
    window = deque()
    previous = None
    for at, value in samples:
        if previous is not None and at-previous > 2:
            x.append(previous+min(1, (at-previous)/2))
            for curve in (raw, med, low, high):
                curve.append(nan)
            window.clear()
        while window and window[0][0] < at-5:
            window.popleft()
        if math.isfinite(value):
            window.append((at, value))
            values = [point[1] for point in window]
            middle, bottom, top = statistics.median(values), min(values), max(values)
        else:
            window.clear()
            middle = bottom = top = nan
        x.append(at); raw.append(value); med.append(middle); low.append(bottom); high.append(top)
        previous = at
    # Keep time moving through a complete outage; never carry the last RSSI
    # forward and call it a new measurement.
    if x and snapshot['elapsed'] > x[-1]:
        x.append(snapshot['elapsed'])
        for curve in (raw, med, low, high):
            curve.append(nan)
    px, success, latency, misses = [], [], [], []
    recent = deque()
    good = 0
    for row in probes:
        at, ok = float(row['elapsed_s']), row['outcome'] == 'success'
        while recent and recent[0][0] < at-30:
            good -= recent.popleft()[1]
        recent.append((at, ok)); good += ok
        px.append(at)
        success.append(float(row['rolling_success_pct']) if row.get('rolling_success_pct') not in ('', None) else 100*good/len(recent))
        latency.append(float(row['latency_ms']) if row['latency_ms'] != '' else nan)
        if not ok:
            misses.append(at)
    distance_x, distance_y = [], []
    finite = [(t,v) for t,v in samples if math.isfinite(v)]
    times = [t for t,v in finite]
    for event in snapshot['events']:
        if event.get('distance_m') not in ('', None):
            at = float(event['elapsed_s'])
            values = [v for t,v in finite[bisect.bisect_left(times, at-2):bisect.bisect_right(times, at+2)]]
            if values:
                distance_x.append(float(event['distance_m']))
                distance_y.append(statistics.median(values))
    return dict(x=x, raw=raw, median=med, low=low, high=high,
                px=px, success=success, latency=latency, misses=misses,
                distance_x=distance_x, distance_y=distance_y)


class LinkPlot:
    def __init__(self, tab):
        import pyqtgraph as pg
        self.tab = tab
        class Window(pg.GraphicsLayoutWidget):
            def closeEvent(self, event):
                tab.plot = None
                super().closeEvent(event)
        self.window = Window()
        self.window.setWindowTitle('Link Tracker | ground receiver RSSI')
        size = (scaling.px(tab.app.ui_scale, 1000), scaling.px(tab.app.ui_scale, 850))
        self.window.resize(*scaling.clamp_size(size, scaling.work_area(tab.app.root), margin=(30, 40)))
        self.header = self.window.addLabel('Link Tracker', row=0, col=0)
        self.rssi = self.window.addPlot(row=1, col=0, title='Satellite → ground RSSI | received packets only')
        self.rssi.addLegend()
        self.raw = self.rssi.plot(name='Raw RSSI', symbol='o', symbolSize=3, connect='finite')
        self.median = self.rssi.plot(name='5 s median', connect='finite')
        self.low = self.rssi.plot(connect='finite'); self.high = self.rssi.plot(connect='finite')
        self.band = pg.FillBetweenItem(self.low, self.high)
        self.rssi.addItem(self.band)
        self.success = self.window.addPlot(row=2, col=0, title='Round-trip probe success | rolling 30 s')
        self.success_curve = self.success.plot()
        self.failures = self.success.plot(pen=None, symbol='x', symbolSize=8, name='Failed probes at 0%')
        self.success.setYRange(-5, 105)
        self.latency = self.window.addPlot(row=3, col=0, title='Round-trip response latency | gaps are failures')
        self.latency_curve = self.latency.plot(symbol='o', symbolSize=3, connect='finite')
        self.distance = self.window.addPlot(row=4, col=0, title='RSSI near distance markers | median within ±2 s')
        self.distance_curve = self.distance.plot(pen=None, symbol='o', symbolSize=7)
        self.rssi.setLabel('left', 'RSSI', units='dBm')
        self.success.setLabel('left', 'Success', units='%')
        self.latency.setLabel('left', 'Latency', units='ms')
        self.distance.setLabel('left', 'RSSI', units='dBm')
        self.distance.setLabel('bottom', 'Manual distance', units='m')
        for plot in (self.rssi, self.success, self.latency):
            plot.setLabel('bottom', 'Elapsed time', units='s')
            plot.showGrid(x=True, y=True, alpha=.2)
        self.success.setXLink(self.rssi); self.latency.setXLink(self.rssi)
        self.markers = []
        self.theme(themes.CURRENT)
        self.window.show()

    def theme(self, colors):
        import pyqtgraph as pg
        self.window.setBackground(colors['plot_bg'])
        palette = colors['plot_palette']
        for curve, color in ((self.raw, palette[0]), (self.median, palette[1]),
                             (self.success_curve, palette[0]), (self.failures, colors['error']),
                             (self.latency_curve, palette[1]), (self.distance_curve, palette[0])):
            curve.setPen(None if curve in (self.failures, self.distance_curve) else pg.mkPen(color, width=1.5))
            curve.setSymbolBrush(color); curve.setSymbolPen(color)
        self.low.setPen(None); self.high.setPen(None)
        color = pg.mkColor(palette[1]); color.setAlpha(45); self.band.setBrush(color)
        for plot in (self.rssi, self.success, self.latency, self.distance):
            for axis in ('left', 'bottom'):
                plot.getAxis(axis).setPen(colors['plot_fg'])
                plot.getAxis(axis).setTextPen(colors['plot_fg'])

    def draw(self, snapshot, mode, smooth=True):
        import pyqtgraph as pg
        data = graph_data(snapshot)
        for curve, key in ((self.raw, 'raw'), (self.median, 'median'), (self.low, 'low'), (self.high, 'high')):
            curve.setData(data['x'], data[key], connect='finite')
        show_smooth = smooth and not snapshot.get('decimated', False)
        self.median.setVisible(show_smooth); self.band.setVisible(show_smooth)
        self.success_curve.setData(data['px'], data['success'])
        self.failures.setData(data['misses'], [0]*len(data['misses']))
        self.latency_curve.setData(data['px'], data['latency'], connect='finite')
        self.distance_curve.setData(data['distance_x'], data['distance_y'])
        for marker in self.markers:
            self.rssi.removeItem(marker)
        self.markers.clear()
        # Bound annotation rendering separately from the durable event log.
        for event in snapshot['events'][-30:]:
            if event['event'] != 'marker':
                continue
            marker = pg.InfiniteLine(float(event['elapsed_s']), angle=90,
                label=event['detail'][:30], labelOpts={'position': .9, 'color': themes.CURRENT['plot_fg']},
                pen=pg.mkPen(themes.CURRENT['plot_fg'], style=2))
            self.rssi.addItem(marker); self.markers.append(marker)
        elapsed = max(1, snapshot['elapsed'])
        if mode.startswith('Last'):
            span = 60 if mode == 'Last 60 s' else 300
            self.rssi.setXRange(max(0, elapsed-span), elapsed, padding=.01)
        elif mode == 'Full session':
            self.rssi.setXRange(0, elapsed, padding=.01)
        if mode != 'Manual':
            # A nearly constant RTT can otherwise produce microscopic floating
            # point axis ranges with unreadable ticks. Latency has a real zero.
            finite_latency = [v for v in data['latency'] if math.isfinite(v)]
            self.latency.setYRange(0, max(10, max(finite_latency, default=0)*1.15), padding=.02)
        caption = (f'{snapshot["name"]} | {snapshot["status"]} | {snapshot["probes"]} probes | '
                   'satellite → ground; missing RSSI is a gap')
        if snapshot.get('decimated'):
            caption += ' | full-session envelope; smoothing hidden'
        self.header.setText(html.escape(caption), color=themes.CURRENT['plot_fg'])

    def save(self, path, target):
        import pyqtgraph.exporters
        item = {'RSSI': self.rssi, 'Success': self.success, 'Latency': self.latency,
                'Distance': self.distance}.get(target)
        exporter = pyqtgraph.exporters.ImageExporter(item or self.window.scene())
        exporter.parameters()['width'] = 1800
        exporter.export(str(path))

    def close(self):
        self.window.close()
