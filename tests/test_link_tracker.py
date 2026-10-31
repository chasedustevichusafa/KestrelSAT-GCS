"""Packet RSSI, unreliable-link probes, durable recording, graphs and Tk ownership.

Radio/SatellitePort are byte-stream fixtures, not RF models. These tests verify
measurement semantics and lifecycle; only physical range tests establish rate
or range. Windows runs this suite too, including real Tk and PNG exports.
"""
from collections import deque
import csv
import json
import math
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kestrelsat import link_tracker as link
from kestrelsat.link_plot import graph_data
from kestrelsat.xbee_api import ApiSerial
from test_xbee_api import Radio, DESTINATION
from test_remote_files import SatellitePort


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def connect(self, peer=True, format='legacy64'):
        raw = Radio(SatellitePort() if peer else None)
        raw.receive_format = format; raw.baudrate = 115200
        port = ApiSerial(raw).initialize()
        self.addCleanup(port.close)
        return raw, port

    def wait(self, condition, timeout=3):
        deadline = time.monotonic()+timeout
        while not condition() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(condition())

    def start(self, port, rate=5, timeout=.2, source=None):
        record = link.Recording(self.folder, 'test', rate, timeout)
        cancel = threading.Event(); port.set_transfer_cancel(cancel)
        worker = threading.Thread(target=link.Tracker(port, record, cancel, source).run)
        def cleanup():
            cancel.set(); worker.join(2)
            self.assertFalse(worker.is_alive())
        self.addCleanup(cleanup)
        worker.start()
        return record, cancel, worker

    def stop(self, cancel, worker):
        cancel.set(); worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_embedded_rssi_both_formats_exact_source_and_no_db(self):
        for format, source in (('legacy64', None), ('legacy16', 0x1234)):
            with self.subTest(format=format):
                raw, port = self.connect(format=format)
                record, cancel, worker = self.start(port, source=source)
                self.wait(lambda: record.count >= 3)
                self.stop(cancel, worker)
                snap = record.snapshot()
                self.assertEqual(snap['successes'], snap['probes'])
                self.assertEqual(snap['rssi'], -55)
                self.assertGreaterEqual(record.measurements, 3)
                self.assertFalse(any(cmd == b'DB' for cmd, _ in raw.commands))
                self.assertEqual(len(raw.readers), 1)
                self.assertIsNone(port._rssi_capture)
                self.assertTrue(all(r['source_verified'] for r in snap['packets'] if r['event'] == 'packet'))
                self.assertTrue(all(r['rolling_success_pct'] == 100 for r in snap['probe_rows'] if r['outcome'] == 'success'))

    def test_short_source_is_not_inferred_from_unrelated_long_destination(self):
        raw, port = self.connect(format='legacy16')
        record, cancel, worker = self.start(port)
        self.wait(lambda: record.count >= 2)
        self.stop(cancel, worker)
        self.assertGreater(record.packet_count, 0)
        self.assertIsNone(record.last_rssi)
        self.assertTrue(all(not r['source_verified'] for r in record.packets))
        self.assertTrue(all(not math.isfinite(v) for v in graph_data(record.snapshot())['raw']))

    def test_modern_db_fallback_fresh_only_and_rate_capped(self):
        raw, port = self.connect(format='modern')
        record, cancel, worker = self.start(port, rate=10)
        self.wait(lambda: record.measurements >= 2, timeout=4)
        self.stop(cancel, worker)
        queries = [at for cmd, at in raw.commands if cmd == b'DB']
        self.assertGreaterEqual(len(queries), 2)
        self.assertGreaterEqual(queries[1]-queries[0], .95)
        self.assertTrue(any(r['method'] == 'DB' and r['rssi_dBm'] == -55 for r in record.packets))

    def test_db_failure_is_a_gap_and_does_not_stop_healthy_probes(self):
        raw, port = self.connect(format='modern')
        raw.db_supported = False
        record, cancel, worker = self.start(port)
        self.wait(lambda: any(r['event'] == 'rssi_error' for r in record.packets))
        self.wait(lambda: record.count >= 2)
        self.stop(cancel, worker)
        self.assertEqual(record.successes, record.count)
        self.assertIsNone(record.last_rssi)
        self.assertEqual(record.status, 'stopped')

    def test_other_sender_during_db_query_marks_measurement_ambiguous(self):
        raw, port = self.connect(format='modern')
        original = port.at
        def at(command, timeout=1.5):
            answer = original(command, timeout)
            if command == 'DB':
                before = port._filtered_packets
                raw.rf(b'other sender', source=DESTINATION+1)
                self.wait(lambda: port._filtered_packets > before)
            return answer
        with patch.object(port, 'at', side_effect=at):
            record, cancel, worker = self.start(port)
            self.wait(lambda: any(r['detail'] == 'ambiguous' for r in record.packets))
            self.stop(cancel, worker)
        self.assertIsNone(record.last_rssi)
        self.assertTrue(any(r['rssi_dBm'] == -55 and r['detail'] == 'ambiguous' for r in record.packets))

    def test_transport_failure_closes_logs_and_releases_capture(self):
        raw, port = self.connect()
        record, cancel, worker = self.start(port)
        self.wait(lambda: record.count >= 1)
        import serial
        with port._condition:
            port._error = serial.SerialException('USB removed')
            port._condition.notify_all()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(record.status, 'failed')
        self.assertIn('USB removed', record.detail)
        self.assertIsNone(port._rssi_capture)
        self.assertTrue(all(h.closed for h in record.handles.values()))

    def test_loss_has_no_retries_no_backlog_and_no_fake_rssi(self):
        raw, port = self.connect(peer=False)
        record, cancel, worker = self.start(port, rate=10, timeout=.15)
        self.wait(lambda: record.count >= 2)
        self.stop(cancel, worker)
        self.assertEqual(record.successes, 0)
        self.assertGreater(record.skipped, 0)
        self.assertIsNone(record.last_rssi)
        self.assertTrue(all(r['latency_ms'] == '' for r in record.probes))
        self.assertEqual(bytes(raw.transmitted).count(b'|HELLO|'), len(record.probes))
        self.assertLessEqual(record.count, 3)
        data = graph_data(record.snapshot())
        self.assertTrue(all(v == 0 for v in data['success']))
        self.assertTrue(all(not math.isfinite(v) for v in data['latency']))

    def test_stop_pending_probe_is_not_counted_as_link_failure(self):
        raw, port = self.connect(peer=False)
        record, cancel, worker = self.start(port, timeout=1)
        self.wait(lambda: b'HELLO' in raw.transmitted)
        before = time.monotonic()
        self.stop(cancel, worker)
        self.assertLess(time.monotonic()-before, .4)
        self.assertEqual(record.count, 0)
        self.assertEqual(record.probes[-1]['outcome'], 'cancelled')
        self.assertEqual(record.status, 'stopped')
        self.assertEqual(port.timeout, .1)

    def test_late_reply_is_logged_and_does_not_succeed_a_new_probe(self):
        raw, port = self.connect()
        raw.delay = .2
        record, cancel, worker = self.start(port, rate=2, timeout=.1)
        self.wait(lambda: any(e['event'] == 'unmatched_reply' for e in record.events))
        self.stop(cancel, worker)
        self.assertEqual(record.successes, 0)
        self.assertTrue(any(r['outcome'] == 'timeout' for r in record.probes))

    def test_invalid_crc_and_wrong_request_are_not_successes(self):
        class Peer(SatellitePort):
            def write(self, data):
                result = super().write(data)
                self.rx[:] = b'@KFS1|00000000|HELLO|1|128|00000000\n'+link.files.encode(0, 'HELLO', '1', '128')
                return result
        raw = Radio(Peer()); raw.receive_format = 'legacy64'
        port = ApiSerial(raw).initialize(); self.addCleanup(port.close)
        record, cancel, worker = self.start(port)
        self.wait(lambda: record.count >= 1)
        self.stop(cancel, worker)
        row = next(r for r in record.probes if r['outcome'] != 'cancelled')
        self.assertEqual(row['outcome'], 'timeout')
        self.assertGreater(row['malformed_lines'], 0)
        self.assertGreater(row['stale_replies'], 0)

    def test_metadata_overflow_is_explicit_and_reader_never_logs_to_disk(self):
        raw, port = self.connect(peer=False)
        owner = object(); port.begin_rssi_capture(owner)
        port._rssi_events = deque(maxlen=2)
        for _ in range(4):
            raw.rf(b'x')
        self.wait(lambda: port._packet_number == 4)
        events, dropped = port.drain_rssi_capture(owner, end=True)
        self.assertEqual(len(events), 2); self.assertEqual(dropped, 2)
        self.assertEqual(port.drain_rssi_capture(object()), ([], 0))
        self.assertEqual(len(raw.readers), 1)

    def test_invalid_embedded_rssi_and_wrong_peer_do_not_create_measurements(self):
        raw, port = self.connect(peer=False)
        owner = object(); port.begin_rssi_capture(owner)
        raw.rf(b'wrong', source=DESTINATION+1)
        raw.enqueue(b'\x80'+DESTINATION.to_bytes(8, 'big')+b'\x00\x00payload')
        self.wait(lambda: port._packet_number == 1)
        events, dropped = port.drain_rssi_capture(owner, end=True)
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0]['rssi'])
        self.assertIsNone(port.traffic_snapshot()['rssi'])

    def test_disk_failure_stops_session_and_releases_metadata_tap(self):
        raw, port = self.connect()
        record = link.Recording(self.folder, 'disk', 5, .2)
        with patch.object(record, '_write', side_effect=OSError('disk full')):
            link.Tracker(port, record, threading.Event()).run()
        self.assertEqual(record.status, 'failed')
        self.assertIn('disk full', record.detail)
        self.assertIsNone(port._rssi_capture)
        self.assertTrue(all(h.closed for h in record.handles.values()))

    def test_raw_csv_retains_data_beyond_memory_and_zip_has_configuration(self):
        record = link.Recording(self.folder, 'long', 2, 1, {'baud':115200})
        record.packets = deque(maxlen=3)
        with self.assertRaises(RuntimeError):
            record.export(self.folder/'active.zip')
        for i in range(100):
            record.packet(dict(event='packet', at=record.started+i/10, packet_at=record.started+i/10,
                               packet=i, source='1234', rssi=-60, method='packet'), True)
        record.mark('marker', 'Hill', 100)
        record.finish()
        self.assertEqual(len(record.packets), 3)
        with (record.directory/'packets.csv').open() as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 100)
        self.assertAlmostEqual(float(rows[-1]['elapsed_s']), 9.9)
        output = self.folder/'export.zip'; record.export(output)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()), {'packets.csv','probes.csv','events.csv','session.json'})
            self.assertEqual(json.loads(archive.read('session.json'))['configuration']['baud'], 115200)
            self.assertIn(b'Hill', archive.read('events.csv'))

    def test_full_envelope_is_bounded_and_preserves_extremes(self):
        path = self.folder/'packets.csv'
        with path.open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=link.PACKETS); writer.writeheader()
            for i in range(1000):
                writer.writerow(dict(elapsed_s=i, valid_sample=True,
                    rssi_dBm=-30 if i == 150 else -120 if i == 151 else -60))
        rows = link.bounded_rows(path, limit=64)
        self.assertLessEqual(len(rows), 64)
        self.assertIn('-30', [r['rssi_dBm'] for r in rows]); self.assertIn('-120', [r['rssi_dBm'] for r in rows])
        self.assertEqual(float(rows[0]['elapsed_s']), 0)
        self.assertEqual(float(rows[-1]['elapsed_s']), 999)

    def test_smoothing_preserves_gaps_and_distance_is_explicit(self):
        record = link.Recording(self.folder, 'graphs', 2, 1)
        for offset, value in ((0,-50), (1,-70), (10,-80)):
            record.packet(dict(event='packet', at=record.started+offset, rssi=value, method='packet'), True)
        snap = record.snapshot(); snap['elapsed'] = 15
        snap['events'].append(dict(event='marker', elapsed_s=1, distance_m=100, detail='100 m'))
        data = graph_data(snap)
        self.assertEqual(data['median'][:2], [-50,-60])
        self.assertTrue(any(not math.isfinite(v) for v in data['raw']))
        self.assertEqual(data['median'][-2], -80)
        self.assertEqual(data['distance_x'], [100])
        self.assertEqual(data['distance_y'], [-60])
        record.finish()

    def test_simulated_hour_at_ten_hz_keeps_all_csv_rows_and_bounded_plots(self):
        clock = [100.0]
        record = link.Recording(self.folder, 'simulated hour', 10, 1, clock=lambda: clock[0])
        count = 36010
        for i in range(count):
            clock[0] = 100+i/10
            lost = 18000 <= i < 18050
            if not lost:
                record.packet(dict(event='packet', at=clock[0], packet_at=clock[0],
                                   packet=i, source='1234', rssi=-60, method='packet'), True)
            record.probe(i, i, 'timeout' if lost else 'success', clock[0]-.03, 0, 0, 0)
        record.finish()
        snap = record.snapshot()
        self.assertEqual(snap['probes'], count)
        self.assertEqual(snap['successes'], count-50)
        self.assertEqual(len(record.probes), link.LIMIT)
        with (record.directory/'probes.csv').open() as handle:
            self.assertEqual(sum(1 for _ in csv.DictReader(handle)), count)
        rows = link.bounded_rows(record.directory/'probes.csv')
        self.assertLessEqual(len(rows), link.LIMIT)
        self.assertEqual(float(rows[0]['elapsed_s']), 0)
        self.assertAlmostEqual(float(rows[-1]['elapsed_s']), 3600.9)
        self.assertTrue(any(row['outcome'] == 'timeout' for row in rows))
        snap['probe_rows'] = rows
        data = graph_data(snap)
        self.assertEqual(data['success'], [float(row['rolling_success_pct']) for row in rows])

    def test_previous_session_db_sample_is_raw_only(self):
        record = link.Recording(self.folder, 'fresh', 2, 1)
        record.packet(dict(event='rssi', at=record.started+.1, packet_at=record.started-.1,
                           rssi=-40, method='DB'), True)
        self.assertIsNone(record.last_rssi)
        self.assertFalse(record.packets[-1]['valid_sample'])
        self.assertTrue(all(not math.isfinite(v) for v in graph_data(record.snapshot())['raw']))
        record.finish()

    def test_export_preserves_raw_logs_and_previous_export_on_error(self):
        record = link.Recording(self.folder, 'exports', 2, 1); record.finish()
        raw = record.directory/'packets.csv'; original = raw.read_bytes()
        with self.assertRaises(ValueError):
            record.export(raw)
        self.assertEqual(raw.read_bytes(), original)
        output = self.folder/'existing.zip'; output.write_bytes(b'previous export')
        with patch('zipfile.ZipFile.write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                record.export(output)
        self.assertEqual(output.read_bytes(), b'previous export')
        self.assertFalse(list(self.folder.glob('.link-export-*')))

    def test_invalid_parameters_and_marker_rejected(self):
        for rate, timeout in ((0,1),(3,1),(10,math.nan),(2,0)):
            with self.assertRaises(ValueError):
                link.Recording(self.folder, 'bad', rate, timeout)
        record = link.Recording(self.folder, 'valid', 2, 1)
        for distance in (-1, math.nan, math.inf):
            with self.assertRaises(ValueError):
                record.mark('marker', distance=distance)
        record.finish()


class UITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.old = Path.cwd(); os.chdir(self.tmp.name); self.addCleanup(os.chdir, self.old)
        from kestrelsat.app import SerialGUI
        self.root = tk.Tk(); self.app = SerialGUI(self.root)
        self.addCleanup(self.app.on_closing)
        self.tab = self.app.link_tracker
        self.raw = Radio(SatellitePort()); self.raw.receive_format = 'legacy64'; self.raw.baudrate = 115200
        self.port = ApiSerial(self.raw).initialize()
        self.app.serial_connection = self.port
        self.app.is_connected = True; self.app.test_mode = False

    def pump(self, predicate, timeout=4):
        deadline = time.monotonic()+timeout
        while not predicate() and time.monotonic() < deadline:
            self.root.update(); time.sleep(.01)
        self.assertTrue(predicate())

    def test_opt_in_and_exclusive_ownership_then_release(self):
        self.assertFalse(self.tab.busy)
        self.assertFalse(self.app._transfer_active.is_set())
        self.tab.start()
        self.pump(lambda: self.tab.active.count >= 2)
        self.assertTrue(self.app._transfer_active.is_set())
        self.assertFalse(self.app.remote_files._ready())
        self.app.live_preview.start(); self.assertFalse(self.app.live_preview.running)
        count = bytes(self.raw.transmitted).count(b'HELLO')
        self.tab.start(); self.assertEqual(bytes(self.raw.transmitted).count(b'HELLO'), count)
        self.tab.stop(); self.pump(lambda: not self.tab.busy)
        self.assertFalse(self.app._transfer_active.is_set())
        self.assertTrue(self.app.remote_files._ready())
        self.app.remote_files.refresh()
        self.pump(lambda: not self.app.remote_files.busy)
        self.assertEqual(len(self.app.remote_files.entries), 2)
        self.assertTrue(all(h.closed for h in self.tab.active.handles.values()))

    def test_stats_tick_does_not_disable_tracker_fallback(self):
        self.raw.receive_format = 'modern'
        self.app.transfer_stats.rssi_enabled.set(False)
        self.tab.start()
        self.pump(lambda: self.tab.active.measurements >= 1)
        self.assertTrue(self.port._poll_enabled.is_set())
        self.tab.stop(); self.pump(lambda: not self.tab.busy)
        self.app.transfer_stats._tick()
        self.assertFalse(self.port._poll_enabled.is_set())

    def test_disconnect_stops_and_reconnect_does_not_restart(self):
        self.tab.start(); self.pump(lambda: self.tab.active.count >= 1)
        record = self.tab.active
        self.app.disconnect()
        self.pump(lambda: not self.tab.busy)
        self.assertFalse(self.tab.worker.is_alive())
        self.assertEqual(record.status, 'stopped')
        self.assertTrue(any(e['event'] == 'disconnected' for e in record.events))
        raw = Radio(SatellitePort()); port = ApiSerial(raw).initialize()
        self.app.serial_connection = port; self.app.is_connected = True
        self.root.update()
        self.assertFalse(self.tab.busy)
        self.assertFalse(raw.transmitted)

    def test_reject_transparent_busy_preview_and_bad_source(self):
        self.app.test_mode = True; self.tab.start(); self.assertFalse(self.tab.busy)
        self.app.test_mode = False; self.app.live_preview.running = True
        self.tab.start(); self.assertFalse(self.tab.busy)
        self.app.live_preview.running = False
        self.tab.source.set('not hex'); self.tab.start(); self.assertFalse(self.tab.busy)
        self.assertFalse(self.app._transfer_active.is_set())

    def test_graphs_png_markers_full_loading_and_close_do_not_stop_tracking(self):
        self.assertIsNotNone(self.app.qt_app)
        self.tab.rate.set('5'); self.tab.start()
        self.pump(lambda: self.tab.active.count >= 3)
        self.tab.marker.set('100 m'); self.tab.distance.set('100'); self.tab.add_marker()
        self.assertTrue(self.tab.show_graphs()); self.tab._draw()
        self.app.qt_app.processEvents()
        self.assertLessEqual(self.tab.plot.latency.viewRange()[1][0], 0)
        self.assertGreaterEqual(self.tab.plot.latency.viewRange()[1][1], 10)
        for target in ('Combined','RSSI','Success','Latency','Distance'):
            path = Path(self.tmp.name)/(target+'.png')
            self.tab.plot.save(path, target)
            data = path.read_bytes(); self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')
            self.assertGreater(struct.unpack('>I',data[16:20])[0], 500)
        self.tab.plot.close(); self.app.qt_app.processEvents()
        self.assertIsNone(self.tab.plot); self.assertTrue(self.tab.busy)
        self.tab.stop(); self.pump(lambda: not self.tab.busy)
        self.tab.mode.set('Full session'); self.tab._view()
        self.pump(lambda: self.tab.full_data is not None)
        self.assertEqual(self.tab.full_data['probes'], self.tab.active.count)
        self.assertTrue(self.tab.show_graphs()); self.tab._draw()
        for theme in ('light','high_contrast','dark'):
            self.app.themes.apply(theme)
        self.tab._draw()

    def test_close_during_pending_probe_releases_worker_and_timer(self):
        self.raw.peer = None
        self.tab.start(); self.pump(lambda: b'HELLO' in self.raw.transmitted)
        self.tab.close()
        self.assertFalse(self.tab.worker.is_alive())
        self.assertIsNone(self.tab.timer)
        self.assertIsNone(self.port._rssi_capture)
        self.assertTrue(all(h.closed for h in self.tab.active.handles.values()))


if __name__ == '__main__':
    unittest.main()
