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
                raw = Radio(SatellitePort())
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
                self.assertFalse(port._reader.is_alive())
                self.assertFalse(port._poller.is_alive())
            finally:
                if app is not None:
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
