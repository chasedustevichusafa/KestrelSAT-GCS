"""Rates, retry accounting, CSV retention, graph lifecycle, and real PNG exports."""
from pathlib import Path
import csv
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from kestrelsat.transfer_stats import Recorder
from kestrelsat import themes


class Clock:
    now = 100
    def __call__(self):
        return self.now


class Port:
    def __init__(self):
        self.rx = self.tx = 0
        self.rssi_at = self.packet_at = None
    def traffic_snapshot(self):
        return dict(rx=self.rx,tx=self.tx,rssi=-60,rssi_at=self.rssi_at,
                    packet_at=self.packet_at,radio_retries=0)


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.recorder = Recorder(self.tmp.name,self.clock)

    def test_interval_rates_stalls_retries_and_csv(self):
        port = Port()
        session = self.recorder.start('KFS1','download','photo.jpg',9600,'API',port)
        session.progress(done=0,total=256)
        session.progress(done=128,total=256)
        port.rx,port.tx = 300,40
        self.clock.now += 1
        self.recorder.tick()
        first = session.snapshot()['samples'][-1]
        self.assertEqual(first['download_Bps'],128)
        self.assertEqual(first['serial_rx_Bps'],300)
        self.assertEqual(first['upload_Bps'],0)
        self.clock.now += 1
        session.progress(retry=1)
        self.recorder.tick()
        self.assertEqual(session.samples[-1]['download_Bps'],0)
        session.progress(done=64,total=256)
        session.progress(done=128,total=256)
        session.progress(done=256,total=256)
        self.clock.now += 2
        session.finish('completed')
        self.assertEqual(session.samples[-1]['download_Bps'],64)
        self.assertEqual(session.done,256)
        destination=Path(self.tmp.name)/'export.csv'
        session.export_csv(destination)
        with destination.open(newline='') as f:
            rows=list(csv.DictReader(f))
        self.assertEqual(len(rows),3)
        self.assertEqual(rows[-1]['status'],'completed')
        self.assertEqual(rows[-1]['protocol_retries'],'1')
        self.assertEqual(rows[-1]['rssi_dBm'],'')
        self.assertEqual(float(rows[-1]['average_Bps']),64)

    def test_multiple_uploads_and_resume_do_not_double_count(self):
        session=self.recorder.start('ZMODEM','upload','first.bin',9600,'Transparent')
        session.progress(name='first.bin',sent=50,total=100,status='starting')
        session.progress(name='first.bin',sent=100,total=100)
        session.progress(name='first.bin',sent=70,total=100)
        session.progress(name='first.bin',sent=100,total=100)
        session.progress(name='first.bin',sent=0,total=10,status='starting')
        session.progress(name='first.bin',sent=10,total=10)
        self.clock.now += 1
        session.finish('completed')
        self.assertEqual(session.done,60)
        self.assertEqual(session.total,60)
        self.assertEqual(session.samples[-1]['upload_Bps'],60)
        self.assertIsNone(session.retries)

    def test_rssi_gaps_and_history_memory_bound_full_csv_retained(self):
        port=Port()
        session=self.recorder.start('KFS1','download','a',9600,'API',port,rssi_enabled=True)
        session.samples=__import__('collections').deque(maxlen=2)
        port.rssi_at=port.packet_at=self.clock.now
        for _ in range(4):
            self.clock.now += 1
            session.sample()
        self.assertEqual(len(session.samples),2)
        self.assertIsNone(session.samples[-1]['rssi_dBm'])
        session.finish('cancelled')
        with session.csv_path.open() as f:
            rows=list(csv.DictReader(f))
        self.assertEqual(len(rows),5)
        self.assertEqual(rows[0]['rssi_dBm'],'-60')
        self.assertEqual(rows[-1]['status'],'cancelled')

    def test_csv_failure_does_not_abort_progress(self):
        blocker=Path(self.tmp.name)/'not-a-directory'
        blocker.write_text('file')
        recorder=Recorder(blocker,self.clock)
        session=recorder.start('KFS1','download','a',9600,'Transparent')
        session.progress(done=128,total=128)
        self.clock.now += 1
        session.finish('completed')
        self.assertTrue(session.error)
        self.assertEqual(session.done,128)
        with self.assertRaises(OSError):
            session.export_csv(Path(self.tmp.name)/'export.csv')


class MultiOperationTests(unittest.TestCase):
    setUp = RecordingTests.setUp
    def test_continuous_timeline_idle_gaps_directions_and_exports(self):
        recording = self.recorder.start_recording('API range test')
        self.clock.now += 2
        self.recorder.tick()
        first = self.recorder.start('KFS1', 'download', 'a.bin', 115200, 'API')
        first.progress(done=100, total=200)
        self.clock.now += 1
        self.recorder.tick()
        first.progress(done=50, total=200)
        first.progress(done=200, total=200)
        self.clock.now += 1
        first.finish('completed')
        self.clock.now += 10
        self.recorder.tick()
        second = self.recorder.start('KFS1', 'upload', 'b.bin', 115200, 'API')
        second.progress(done=300, total=300)
        self.clock.now += 3
        second.finish('completed')
        self.recorder.stop_recording()
        snap = recording.snapshot()
        self.assertEqual((snap['download_bytes'], snap['upload_bytes']), (200, 300))
        self.assertEqual(snap['operation_count'], 2)
        self.assertEqual(snap['active_seconds'], 5)
        self.assertEqual(snap['elapsed'], 17)
        rows = snap['samples']
        self.assertEqual(rows[-1]['active_average_Bps'], 100)
        self.assertAlmostEqual(rows[-1]['recording_average_Bps'], 500/17)
        self.assertTrue(any(row['state'] == 'idle' and row['interval_s'] == 10 for row in rows))
        self.assertTrue(all(row['download_Bps'] == row['upload_Bps'] == 0
                            and row['rssi_dBm'] is None for row in rows if row['state'] == 'idle'))
        self.assertEqual([op['outcome'] for op in snap['operations']], ['completed', 'completed'])
        for kind, path in [('samples', recording.csv_path), ('operations', recording.operations_path),
                           ('events', recording.events_path)]:
            dest = Path(self.tmp.name)/('export-'+kind+'.csv')
            recording.export_csv(dest, kind)
            self.assertEqual(path.read_bytes(), dest.read_bytes())
            with dest.open(newline='') as handle:
                exported = list(csv.DictReader(handle))
            self.assertTrue(exported)
        self.assertEqual(first.status, 'completed')

    def test_partial_capture_does_not_stop_transfer(self):
        session = self.recorder.start('KFS1', 'upload', 'large.bin', 9600, 'Transparent')
        session.progress(done=100, total=1000)
        self.clock.now += 1
        recording = self.recorder.start_recording('Middle only')
        session.progress(done=250, total=1000)
        self.clock.now += 2
        self.recorder.stop_recording()
        op = recording.snapshot()['operations'][0]
        self.assertEqual(op['captured_bytes'], 150)
        self.assertTrue(op['partial_start'])
        self.assertTrue(op['partial_end'])
        self.assertEqual(op['duration_s'], 2)
        self.assertEqual(session.status, 'running')
        self.assertEqual(session.done, 250)
        session.progress(done=1000, total=1000)
        self.clock.now += 1
        session.finish('completed')
        self.assertEqual(recording.snapshot()['upload_bytes'], 150)
        self.assertEqual(session.done, 1000)

    def test_failures_disconnect_events_rssi_freshness_and_reconnect(self):
        recording = self.recorder.start_recording('Reconnect')
        port = Port()
        first = self.recorder.start('KFS1', 'download', 'failed', 9600, 'API', port, True)
        self.clock.now += 1
        port.rssi_at = port.packet_at = self.clock.now
        first.progress(done=10, total=100)
        self.recorder.tick()
        self.assertEqual(recording.samples[-1]['rssi_dBm'], -60)
        self.clock.now += 3
        first.finish('failed')
        self.assertIsNone(recording.samples[-1]['rssi_dBm'])
        self.recorder.event('disconnected', 'Adapter unplugged')
        self.clock.now += 4
        self.recorder.tick()
        self.recorder.event('connected', 'New port | 115200 | Transparent')
        second = self.recorder.start('KFS1', 'download', 'cancelled', 115200, 'Transparent')
        self.clock.now += .25
        second.finish('cancelled')
        self.recorder.event('activity_finished', 'rename: /a | renamed')
        self.recorder.stop_recording()
        snap = recording.snapshot()
        self.assertEqual([op['outcome'] for op in snap['operations']], ['failed', 'cancelled'])
        self.assertEqual([op['baud'] for op in snap['operations']], [9600, 115200])
        self.assertTrue({'disconnected', 'connected', 'activity_finished'} <= {e['event'] for e in snap['events']})
        self.assertEqual(snap['download_bytes'], 10)

    def test_combine_reads_full_csv_preserves_gaps_and_originals(self):
        first = self.recorder.start('KFS1', 'download', 'first', 9600, 'Transparent')
        first.samples = __import__('collections').deque(maxlen=2)
        for value in range(1, 6):
            first.progress(done=value*10, total=50)
            self.clock.now += 1
            self.recorder.tick()
        first.finish('completed')
        self.clock.now += 10
        second = self.recorder.start('KFS1', 'upload', 'second', 115200, 'Transparent')
        self.clock.now += 2
        second.progress(done=200, total=200)
        second.finish('completed')
        originals = [session.csv_path.read_bytes() for session in (first, second)]
        combined = self.recorder.combine([second, first, first], 'Combined')
        snap = combined.snapshot()
        self.assertEqual(snap['operation_count'], 2)
        self.assertEqual(snap['done'], 250)
        self.assertEqual(snap['elapsed'], 17)
        self.assertEqual(snap['active_seconds'], 7)
        self.assertEqual(snap['operations'][1]['start_elapsed_s'], 15)
        with combined.csv_path.open(newline='') as handle:
            rows = list(csv.DictReader(handle))
        self.assertTrue(any(row['elapsed_s'] == '1.0' for row in rows))
        self.assertEqual(sum(row['state'] == 'idle' and float(row['interval_s']) > 0 for row in rows), 10)
        self.assertEqual(originals, [session.csv_path.read_bytes() for session in (first, second)])
        with self.assertRaises(ValueError):
            self.recorder.combine([first], 'Not enough')
        running = self.recorder.start('KFS1', 'upload', 'running', 9600, 'Transparent')
        with self.assertRaises(ValueError):
            self.recorder.combine([first, running], 'Still running')
        running.finish('cancelled')

    def test_long_recording_full_graph_and_csv_survive_memory_limit(self):
        recording = self.recorder.start_recording('Long test')
        recording.samples = __import__('collections').deque(maxlen=2)
        session = self.recorder.start('KFS1', 'download', 'a', 9600, 'Transparent')
        for index in range(2405):
            self.clock.now += 1
            session.progress(done=100 if index < 3 else 300, total=300)
            self.recorder.tick()
        session.finish('completed')
        self.clock.now += 2
        self.recorder.stop_recording()
        overview = recording.graph_snapshot()
        self.assertEqual(len(recording.samples), 2)
        self.assertEqual(overview['samples'][0]['elapsed_s'], 0)
        self.assertEqual(overview['samples'][-1]['elapsed_s'], 2407)
        self.assertEqual(max(row['download_Bps'] for row in overview['samples']), 200)
        self.assertLess(len(overview['samples']), 2405)

    def test_combine_failure_and_shutdown_close_logs_without_touching_sources(self):
        first = self.recorder.start('KFS1', 'download', 'first', 9600, 'Transparent')
        self.clock.now += 1
        first.finish('completed')
        second = self.recorder.start('KFS1', 'upload', 'second', 9600, 'Transparent')
        self.clock.now += 1
        second.finish('completed')
        first.csv_path.unlink()
        with self.assertRaises(OSError):
            self.recorder.combine([first, second], 'Missing source')
        self.assertEqual(second.status, 'completed')
        self.recorder.combine_cancel.set()
        with self.assertRaises(RuntimeError):
            self.recorder.combine([first, second], 'Shutdown')
        for path in Path(self.tmp.name).glob('recording-*.csv'):
            # Windows also requires handles to be closed before rename/unlink.
            path.rename(path.with_suffix('.closed'))

    def test_disk_failure_keeps_transfer_alive_and_reports_error(self):
        blocker = Path(self.tmp.name)/'blocker'
        blocker.write_text('file')
        self.recorder.directory = blocker
        recording = self.recorder.start_recording('Cannot write')
        session = self.recorder.start('KFS1', 'download', 'a', 9600, 'Transparent')
        self.clock.now += 1
        session.progress(done=10, total=10)
        session.finish('completed')
        self.recorder.stop_recording()
        self.assertEqual(session.done, 10)
        self.assertEqual(recording.snapshot()['download_bytes'], 10)
        self.assertTrue(recording.error)
        with self.assertRaises(OSError):
            recording.export_csv(Path(self.tmp.name)/'copy.csv')


class GraphTests(unittest.TestCase):
    def test_api_connection_download_records_rssi_and_cleans_up(self):
        import tkinter as tk
        from unittest.mock import patch
        import time
        from kestrelsat.app import SerialGUI
        from test_xbee_api import Radio
        from test_remote_files import SatellitePort
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            app = None
            try:
                root = tk.Tk()
                app = SerialGUI(root)
                recording = app.transfer_recorder.start_recording('Real API operations')
                raw = Radio(SatellitePort())
                # Packet RSSI is exact even during continuous file replies.
                # DB fallback/association races are covered by Link Tracker and
                # API tests; a fast transfer need not contain an unambiguous DB.
                raw.receive_format = 'legacy64'
                raw.delay = 0.15
                app.port_var.set('FAKE-XBEE')
                app.radio_mode_var.set('XBee API (AP=1)')
                with patch('kestrelsat.app.serial.Serial',return_value=raw):
                    app.connect()
                self.assertTrue(app.is_connected)
                app.remote_files.refresh()
                def finish():
                    deadline = time.monotonic()+6
                    while app.remote_files.busy and time.monotonic()<deadline:
                        root.update()
                        time.sleep(.005)
                    root.update()
                    self.assertFalse(app.remote_files.busy)
                finish()
                key = next(key for key,entry in app.remote_files.entries.items() if not entry.directory)
                app.remote_files.tree.selection_set(key)
                destination = Path(tmp)/'download.bin'
                with patch('kestrelsat.remote_tab.filedialog.asksaveasfilename',return_value=str(destination)):
                    app.remote_files.download_selected()
                finish()
                self.assertEqual(destination.read_bytes(),raw.peer.content)
                session = app.transfer_recorder.active
                snap = session.snapshot()
                self.assertEqual(snap['status'],'completed')
                self.assertEqual(snap['mode'],'XBee API (AP=1)')
                self.assertEqual(snap['baud'],9600)
                self.assertEqual(snap['done'],len(raw.peer.content))
                self.assertTrue(any(row['rssi_dBm'] == -55 for row in snap['samples']))
                self.assertNotIn('@KFS1',app.received_text.get('1.0','end'))
                port = app.serial_connection
                app.disconnect()
                app.transfer_recorder.stop_recording()
                capture = recording.snapshot()
                self.assertEqual(capture['operation_count'], 1)
                self.assertEqual(capture['download_bytes'], len(raw.peer.content))
                self.assertTrue({'connected', 'disconnected', 'activity_started', 'activity_finished'} <=
                                {event['event'] for event in capture['events']})
                self.assertFalse(port._reader.is_alive())
                self.assertFalse(port._poller.is_alive())
            finally:
                if app is not None:
                    app.on_closing()
                os.chdir(original)

    def test_multi_operation_ui_graph_exports_and_stop_leave_transfer_running(self):
        import tkinter as tk
        from unittest.mock import patch
        from kestrelsat.app import SerialGUI
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            app = None
            try:
                root = tk.Tk()
                app = SerialGUI(root)
                tab = app.transfer_stats
                app.notebook.select(tab.frame)
                clock = Clock()
                recorder = app.transfer_recorder = Recorder(Path(tmp)/'statistics', clock)
                with patch('kestrelsat.stats_tab.simpledialog.askstring', return_value='Multi test'):
                    tab.start_recording()
                recording = recorder.recording
                self.assertIs(tab.selected_session(), recording)
                session = recorder.start('KFS1', 'download', 'one.bin', 115200, 'Transparent')
                session.progress(done=10, total=10)
                clock.now += 1
                session.finish('completed')
                clock.now += 5
                recorder.tick()
                session = recorder.start('KFS1', 'upload', 'two.bin', 115200, 'Transparent')
                session.progress(done=100, total=1000)
                clock.now += 1
                recorder.tick()
                tab._refresh()
                self.assertIs(tab.selected_session(), recording)
                self.assertIn('Multi test', tab.recording_status.get())
                self.assertEqual(str(tab.stop_button['state']), 'normal')
                self.assertTrue(tab.show_graphs())
                self.assertTrue(tab._markers)
                x, y = tab.download_curve.getData()
                self.assertEqual(max(y), 10)
                tab.rate_plot.setXRange(5, 7, padding=0)
                for target in ('Combined', 'Rates', 'RSSI'):
                    path = Path(tmp)/('recording-'+target+'.png')
                    tab.save_png(path, target)
                    image = path.read_bytes()
                    self.assertEqual(image[:8], b'\x89PNG\r\n\x1a\n')
                    self.assertEqual(struct.unpack('>I', image[16:20])[0], 1600)
                    self.assertAlmostEqual(tab.rate_plot.viewRange()[0][0], 5, places=3)
                with patch('kestrelsat.stats_tab.filedialog.asksaveasfilename', return_value=str(Path(tmp)/'summary.csv')):
                    tab.export_operations()
                self.assertTrue((Path(tmp)/'summary.csv').exists())
                tab.stop_recording()
                self.assertEqual(session.status, 'running')
                self.assertTrue(recording.operations[-1]['partial_end'])
                self.assertEqual(str(tab.start_button['state']), 'normal')
                session.finish('cancelled')
                tab.window.close()
                app.qt_app.processEvents()
                self.assertIs(tab.selected_session(), recording)
                for theme in ('light', 'high_contrast', 'dark'):
                    app.themes.apply(theme)
                    self.assertTrue(tab.show_graphs())
                self.assertIn('stopped', tab.summary.get())
            finally:
                if app:
                    app.on_closing()
                os.chdir(original)

    def test_combine_dialog_worker_delivers_result_on_main_thread(self):
        import tkinter as tk
        import time
        from kestrelsat.app import SerialGUI
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            app = None
            try:
                root = tk.Tk()
                app = SerialGUI(root)
                clock = Clock()
                app.transfer_recorder = recorder = Recorder(tmp, clock)
                for direction in ('download', 'upload'):
                    session = recorder.start('KFS1', direction, direction+'.bin', 9600, 'Transparent')
                    session.progress(done=20, total=20)
                    clock.now += 1
                    session.finish('completed')
                    clock.now += 2
                tab = app.transfer_stats
                tab.combine_transfers()
                dialog = next(child for child in root.winfo_children() if isinstance(child, tk.Toplevel))
                choices = next(child for child in dialog.winfo_children() if isinstance(child, tk.Listbox))
                self.assertEqual(choices.cget('background'), themes.CURRENT['field_bg'])
                choices.selection_set(0, tk.END)
                button = next(child for frame in dialog.winfo_children() for child in frame.winfo_children()
                              if child.winfo_class() == 'TButton' and child['text'] == 'Combine selected')
                button.invoke()
                deadline = time.monotonic()+5
                while str(tab.combine_button['state']) == 'disabled' and time.monotonic() < deadline:
                    root.update()
                    time.sleep(.005)
                self.assertEqual(str(tab.combine_button['state']), 'normal')
                combined = tab.selected_session().snapshot()
                self.assertTrue(combined['is_recording'])
                self.assertEqual(combined['operation_count'], 2)
                self.assertEqual(combined['done'], 40)
                self.assertEqual(combined['elapsed'], 4)
            finally:
                if app:
                    app.on_closing()
                os.chdir(original)

    def test_png_exports_and_session_preserved_when_graph_closed(self):
        import tkinter as tk
        from kestrelsat.app import SerialGUI
        original=os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            root=None
            try:
                root=tk.Tk()
                app=SerialGUI(root)
                app.notebook.select(app.transfer_stats.frame)
                session=app.transfer_recorder.start('KFS1','download','photo.jpg',9600,'Transparent')
                session.progress(done=128,total=256)
                session.sample(force=True,now=session.started+1)
                session.progress(done=256,total=256)
                session.finish('completed')
                app.transfer_stats._refresh()
                for target in ('Combined','Rates','RSSI'):
                    path=Path(tmp)/(target+'.png')
                    app.transfer_stats.save_png(path,target)
                    image=path.read_bytes()
                    self.assertEqual(image[:8],b'\x89PNG\r\n\x1a\n')
                    width,height=struct.unpack('>II',image[16:24])
                    self.assertEqual(width,1600)
                    self.assertGreater(height,100)
                app.transfer_stats.window.close()
                app.qt_app.processEvents()
                self.assertIsNone(app.transfer_stats.window)
                self.assertIs(app.transfer_stats.selected_session(),session)
                for theme in ('light','high_contrast','dark'):
                    app.themes.apply(theme)
                    self.assertTrue(app.transfer_stats.show_graphs())
                self.assertIn('completed',app.transfer_stats.summary.get())
                app.on_closing()
                root=None
            finally:
                if root is not None:
                    root.destroy()
                os.chdir(original)


if __name__ == '__main__':
    unittest.main()
