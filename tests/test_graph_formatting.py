"""Graph-only palettes, dual-axis RSSI, truthful gaps and real PNG exports."""
import copy
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from PIL import Image
from kestrelsat.app import SerialGUI
from kestrelsat import graph_style
from kestrelsat.transfer_stats import Recorder
from kestrelsat.link_tracker import Recording


class FormattingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.old = Path.cwd(); os.chdir(self.tmp.name); self.addCleanup(os.chdir, self.old)
        self.root = tk.Tk(); self.app = SerialGUI(self.root); self.addCleanup(self.app.on_closing)
        self.clock = [100.0]
        class Port:
            signal = -50
            def traffic_snapshot(port):
                at = self.clock[0] if port.signal is not None else None
                return dict(rx=100, tx=10, rssi=port.signal, rssi_at=at, packet_at=at, radio_retries=0)
        self.port = Port()
        recorder = self.app.transfer_recorder = Recorder('stats', clock=lambda: self.clock[0])
        self.recording = recorder.start_recording('Formatting test')
        self.session = recorder.start('KFS1', 'download', '/image.jpg', 115200, 'API', self.port, rssi_enabled=True)
        self.session.progress(done=0, total=6000)
        for i in range(1, 7):
            self.clock[0] += 1
            self.port.signal = -40-i*5 if i != 3 else None
            self.session.progress(done=i*1000, total=6000)
            recorder.tick()
        self.session.finish('completed'); recorder.stop_recording()
        self.tab = self.app.transfer_stats
        self.tab._select(self.recording)
        for i in range(5):
            self.app.plots[0].update_plot_channels({'Temp': i, 'Sine': i*2}, i)
        self.app.plots[0].show_plot_window()
        self.assertTrue(self.tab.show_graphs())
        link = Recording('link', 'Formatting link', 2, 1, clock=lambda:self.clock[0])
        link.packet(dict(event='packet', at=self.clock[0], packet_at=self.clock[0], rssi=-55, method='packet'), True)
        link.finish()
        self.app.link_tracker.recording = link
        self.assertTrue(self.app.link_tracker.show_graphs())
        self.app.link_tracker._draw()
        self.pump()

    def pump(self):
        self.root.update(); self.app.qt_app.processEvents(); self.root.update()

    def background(self, value):
        self.app.graph_background_var.set(value); self.app.on_graph_background_selected(); self.pump()

    def overlay(self, value):
        self.tab.overlay_rssi.set(value); self.tab._overlay_changed(); self.pump()

    def check_backgrounds(self, wanted):
        windows = (self.app.plots[0].plot_widget, self.tab.window, self.app.link_tracker.plot.window)
        for window in windows:
            self.assertEqual(window.backgroundBrush().color().name().lower(), wanted)
        colors = graph_style.colors(self.app)
        plots = (self.app.plots[0].plot_widget.getPlotItem(), self.tab.rate_plot,
                 self.tab.rssi_plot, self.app.link_tracker.plot.rssi)
        for plot in plots:
            for name in ('left', 'bottom'):
                self.assertEqual(plot.getAxis(name).textPen().color().name().lower(), colors['plot_fg'].lower())
        self.assertIn(colors['plot_fg'].lower(), self.app.link_tracker.plot.rssi.titleLabel.item.toHtml().lower())

    def test_white_default_and_independent_black_choice_across_all_graphs(self):
        self.check_backgrounds('#ffffff')
        theme = self.app.theme_var.get()
        self.background('black'); self.check_backgrounds('#000000')
        self.assertEqual(self.app.theme_var.get(), theme)
        for theme in ('light', 'high_contrast', 'dark'):
            self.app.theme_var.set(theme); self.app.on_theme_selected(); self.pump()
            self.check_backgrounds('#000000')
        self.background('white'); self.check_backgrounds('#ffffff')

    def test_user_channel_colours_survive_background_switches(self):
        plot = self.app.plots[0]
        custom = plot.channels['Sine']; custom.color = '#C000C0'; custom.color_is_user = True
        for background in ('black','white'):
            self.background(background)
            self.assertEqual(custom.color, '#C000C0')
            self.assertEqual(custom.curve.opts['pen'].color().name().lower(), '#c000c0')
            self.assertIn(plot.channels['Temp'].color, graph_style.colors(self.app)['plot_palette'])

    def test_new_and_reopened_windows_obey_current_background_and_overlay(self):
        self.background('black'); self.overlay(True)
        first = self.tab.overlay_view
        self.tab.window.close(); self.pump(); self.assertIsNone(self.tab.window)
        self.assertIsNone(self.tab.overlay_view)
        self.tab.show_graphs(); self.pump()
        self.assertIsNot(self.tab.overlay_view, first)
        self.assertTrue(self.tab._overlay_enabled)
        self.check_backgrounds('#000000')
        self.app.add_plot_tab()
        self.app.plots[-1].show_plot_window(); self.pump()
        self.assertEqual(self.app.plots[-1].plot_widget.backgroundBrush().color().name(), '#000000')
        self.app.link_tracker.plot.close(); self.pump()
        self.app.link_tracker.show_graphs(); self.pump()
        self.check_backgrounds('#000000')

    def test_overlay_has_independent_y_units_and_preserves_raw_measurements(self):
        snapshot = copy.deepcopy(self.recording.snapshot())
        original_csv = self.session.csv_path.read_bytes()
        self.overlay(True)
        self.assertTrue(self.tab.rate_plot.getAxis('right').isVisible())
        self.assertEqual(self.tab.rate_plot.getAxis('right').labelUnits, 'dBm')
        self.assertEqual(self.tab.rate_plot.getAxis('left').labelUnits, 'B/s')
        self.assertIsNone(self.tab.rssi_plot.scene())
        np.testing.assert_allclose(self.tab.overlay_curve.xData, self.tab.rssi_curve.xData)
        np.testing.assert_allclose(self.tab.overlay_curve.yData, self.tab.rssi_curve.yData, equal_nan=True)
        self.assertTrue(np.isnan(self.tab.overlay_curve.yData).any())
        self.assertTrue(np.nanmax(self.tab.overlay_curve.yData) < 0)
        # Pen-width padding can extend slightly below the zero upload curve;
        # the actual negative RSSI range must not enter the speed view's bounds.
        self.assertGreater(self.tab.rate_plot.getViewBox().childrenBounds()[1][0],
                           np.nanmax(self.tab.overlay_curve.yData))
        self.overlay(False); self.overlay(True)
        self.assertEqual(self.recording.snapshot()['samples'], snapshot['samples'])
        self.assertEqual(self.session.csv_path.read_bytes(), original_csv)
        labels = [label.text for _, label in self.tab.rate_plot.legend.items]
        self.assertEqual(labels.count('RSSI (right axis)'), 1)

    def test_overlay_tracks_resize_and_zoom_without_changing_time(self):
        self.overlay(True)
        self.tab.window.resize(1150, 500); self.pump()
        primary = self.tab.rate_plot.getViewBox()
        primary.setXRange(1, 5, padding=0); self.pump()
        np.testing.assert_allclose(self.tab.overlay_view.viewRange()[0], primary.viewRange()[0], atol=1e-6)
        self.assertAlmostEqual(self.tab.overlay_view.geometry().width(), primary.sceneBoundingRect().width(), places=3)
        self.assertAlmostEqual(self.tab.overlay_view.geometry().height(), primary.sceneBoundingRect().height(), places=3)

    def test_missing_rssi_is_empty_not_zero_on_overlay(self):
        self.overlay(True)
        snap = copy.deepcopy(self.recording.snapshot())
        for row in snap['samples']:
            row['rssi_dBm'] = None
        self.tab._draw(snap, full=True)
        self.assertTrue(self.tab.overlay_curve.yData is None or len(self.tab.overlay_curve.yData) == 0)
        self.assertIn('no fresh measurements', self.tab.rate_plot.titleLabel.text)

    def test_png_backgrounds_and_visible_overlay_in_all_export_targets(self):
        for background in ('white','black'):
            self.background(background); self.overlay(True)
            for target in ('Combined','Rates','RSSI'):
                path = Path(self.tmp.name)/(background+'-'+target+'.png')
                self.tab.save_png(path, target, full=True)
                with Image.open(path) as image:
                    pixels = np.asarray(image.convert('RGB'))
                    self.assertTrue(np.all(pixels[0,0] >= 245) if background == 'white' else np.all(pixels[0,0] <= 10))
                    # RSSI's colour is distinct from both speed curves. More than
                    # a legend swatch must be present in the exported plot.
                    color = graph_style.colors(self.app)['plot_palette'][2]
                    rgb = np.array([int(color[i:i+2],16) for i in (1,3,5)])
                    matches = np.max(np.abs(pixels.astype(int)-rgb), axis=2) < 25
                    self.assertGreater(np.count_nonzero(matches), 300, (background,target))
                self.assertTrue(self.tab._overlay_enabled)
                self.assertTrue(self.tab.overlay_rssi.get())
            link_path = Path(self.tmp.name)/(background+'-link.png')
            self.app.link_tracker.plot.save(link_path, 'Combined')
            with Image.open(link_path) as image:
                pixel = np.asarray(image.convert('RGB'))[0,0]
                self.assertTrue(np.all(pixel >= 245) if background == 'white' else np.all(pixel <= 10))
            import pyqtgraph.exporters
            telemetry_path = Path(self.tmp.name)/(background+'-telemetry.png')
            pyqtgraph.exporters.ImageExporter(self.app.plots[0].plot_widget.getPlotItem()).export(str(telemetry_path))
            with Image.open(telemetry_path) as image:
                pixel = np.asarray(image.convert('RGB'))[0,0]
                self.assertTrue(np.all(pixel >= 245) if background == 'white' else np.all(pixel <= 10))

    def test_export_restores_zoom_and_layout_even_when_export_fails(self):
        self.overlay(True)
        view = self.tab.rate_plot.getViewBox()
        view.setRange(xRange=(2,4), yRange=(0,1500), padding=0); self.pump()
        before = np.array(view.viewRange())
        overlay_y = list(self.tab.overlay_view.viewRange()[1])
        import pyqtgraph.exporters
        for target in ('Rates','RSSI'):
            with patch.object(pyqtgraph.exporters.ImageExporter, 'export', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    self.tab.save_png(Path(self.tmp.name)/'fail.png',target,full=True)
            self.pump()
            self.assertTrue(self.tab._overlay_enabled)
            self.assertIsNone(self.tab.rssi_plot.scene())
            np.testing.assert_allclose(view.viewRange(),before,atol=1e-6)
            np.testing.assert_allclose(self.tab.overlay_view.viewRange()[1],overlay_y,atol=1e-6)

    def test_background_and_overlay_persist_across_application_restart(self):
        self.background('black'); self.overlay(True)
        self.app.on_closing()
        # A real restart has a fresh Tcl interpreter AND a fresh QApplication.
        # Reusing both native GUI libraries across destroyed Tk roots can crash
        # Python on Windows during later ThemeChanged/garbage-collection work.
        repo = str(Path(__file__).resolve().parents[1])
        script = f"""import sys
sys.path.insert(0, {repo!r})
import tkinter as tk
from kestrelsat.app import SerialGUI
root = tk.Tk()
app = SerialGUI(root)
try:
    assert app.graph_background == 'black'
    assert app.transfer_stats.overlay_rssi.get()
    assert app.transfer_stats.show_graphs()
    app.qt_app.processEvents()
    assert app.transfer_stats._overlay_enabled
    assert app.transfer_stats.window.backgroundBrush().color().name() == '#000000'
finally:
    app.on_closing()
"""
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout+'\n'+result.stderr)



if __name__ == '__main__':
    if len(sys.argv) > 1:
        unittest.main()
    else:
        # Standalone executable suite, like run_all.py. Each case exercises one
        # actual desktop instance; do not retain old Tcl/Qt native state between
        # cases. Failures still propagate without retries or skipped assertions.
        names = unittest.defaultTestLoader.getTestCaseNames(FormattingTests)
        failed = []
        for name in names:
            print('Graph formatting: '+name, flush=True)
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                     'FormattingTests.'+name])
            if result.returncode:
                failed.append(name)
        if failed:
            print('FAILED: '+', '.join(failed))
            sys.exit(1)
        print(f'All {len(names)} graph formatting cases passed.')
