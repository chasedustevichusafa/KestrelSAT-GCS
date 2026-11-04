"""ZIP/folder reconstruction, validation, provenance and real offline GUI exports."""
import csv
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from kestrelsat import link_tracker as link
from kestrelsat import link_replay as replay
from kestrelsat.link_plot import graph_data


class Fixture:
    def __init__(self, root, verified=True, count=8):
        self.clock = [100.0]
        self.record = link.Recording(root/'logs', 'Saved graph test', 2, 1, clock=lambda:self.clock[0])
        for i in range(count):
            self.clock[0] += .5
            at = self.clock[0]
            self.record.packet(dict(event='packet', at=at, packet_at=at, packet=i+1,
                source='0000', address_bits=16, frame='81', rssi=-40-i%10, method='packet'), verified)
            self.record.probe(i+1, i+1, 'timeout' if i == 3 else 'success', at-.03, 0, 0, 0)
            if i == 4: self.record.mark('marker', 'Hill {checkpoint}', 100)
        self.record.finish()
        self.bundle = root/'test.zip'; self.record.export(self.bundle)

    def files(self):
        return {name:(self.record.directory/name).read_bytes() for name in replay.FILES}


def changed_csv(data, change):
    reader = csv.DictReader(io.StringIO(data.decode('utf-8-sig')))
    fields, rows = list(reader.fieldnames), list(reader)
    change(fields, rows)
    out = io.StringIO(newline=''); writer = csv.DictWriter(out, fieldnames=fields, extrasaction='ignore')
    writer.writeheader(); writer.writerows(rows)
    return out.getvalue().encode()


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.fixture = Fixture(self.root)

    def load(self, path=None, source=None):
        saved = replay.SavedSession(path or self.fixture.bundle, source, self.root/'cache')
        self.addCleanup(saved.cache.cleanup)
        return saved

    def write_bundle(self, contents):
        target = self.root/'modified.zip'
        with zipfile.ZipFile(target, 'w') as archive:
            for name, data in contents.items(): archive.writestr(name, data)
        return target

    def test_exact_zip_and_folder_graph_reconstruction_and_frozen_timing(self):
        original = graph_data(self.fixture.record.graph_snapshot())
        for path in (self.fixture.bundle, self.fixture.record.directory):
            saved = self.load(path)
            rebuilt = graph_data(saved.graph_snapshot())
            for key in original: np.testing.assert_allclose(rebuilt[key], original[key], equal_nan=True)
            snap = saved.snapshot()
            self.assertEqual(snap['probes'], 8); self.assertEqual(snap['successes'], 7)
            self.assertEqual(snap['elapsed'], 4)
            self.assertEqual(snap['requested_hz'], 2)
            self.assertEqual(snap['original_status'], 'stopped')
            before = saved.snapshot()['age']; time.sleep(.01)
            self.assertEqual(saved.snapshot()['age'], before)
            self.assertEqual(snap['events'][1]['detail'], 'Hill {checkpoint}')

    def test_source_override_recovers_only_matching_fresh_unambiguous_rssi(self):
        root = self.root/'unverified'; root.mkdir(); fixture = Fixture(root, verified=False)
        original = self.load(fixture.bundle)
        self.assertIsNone(original.snapshot()['rssi'])
        self.assertTrue(any('unverified' in warning for warning in original.warnings))
        contents = fixture.files()
        def change(fields, rows):
            rows[1]['source'] = '1234'
            rows[2]['packet_age_s'] = '3'
            rows[3]['detail'] = 'ambiguous'
            rows[4]['rssi_dBm'] = ''
        contents['packets.csv'] = changed_csv(contents['packets.csv'], change)
        altered = self.write_bundle(contents)
        before = altered.read_bytes(); saved = self.load(altered, source=0)
        rows = saved.graph_snapshot()['packets']
        self.assertTrue(rows[0]['valid_sample'])
        self.assertTrue(all(not row['valid_sample'] for row in rows[1:5]))
        self.assertEqual(saved.snapshot()['source_override'], 0)
        self.assertTrue(any('rechecked' in warning for warning in saved.warnings))
        self.assertEqual(altered.read_bytes(), before)

    def test_raw_reexport_is_byte_identical_and_does_not_overwrite_sources(self):
        root = self.root/'unverified'; root.mkdir(); fixture = Fixture(root, verified=False)
        saved = self.load(fixture.bundle, source=0)
        output = self.root/'copied.zip'; saved.export(output)
        with zipfile.ZipFile(output) as archive:
            for name, data in fixture.files().items(): self.assertEqual(archive.read(name), data)
        with self.assertRaises(ValueError): saved.export(fixture.bundle)
        folder = self.load(fixture.record.directory)
        with self.assertRaises(ValueError): folder.export(fixture.record.directory/'packets.csv')
        # Loading owns a private copy: moving the selected input later is safe.
        fixture.bundle.unlink()
        saved.export(self.root/'after-source-removed.zip')

    def test_legacy_optional_columns_are_reconstructed_before_decimation(self):
        files = self.fixture.files()
        files['packets.csv'] = changed_csv(files['packets.csv'], lambda fields, rows:fields.remove('valid_sample'))
        files['probes.csv'] = changed_csv(files['probes.csv'], lambda fields, rows:fields.remove('rolling_success_pct'))
        saved = self.load(self.write_bundle(files))
        expected = graph_data(self.fixture.record.graph_snapshot())
        actual = graph_data(saved.graph_snapshot())
        np.testing.assert_allclose(actual['raw'], expected['raw'], equal_nan=True)
        np.testing.assert_allclose(actual['success'], expected['success'])

    def test_large_recording_bounds_view_without_losing_summary_or_outage(self):
        root = self.root/'large'; root.mkdir(); fixture = Fixture(root, count=500)
        with patch.object(link, 'LIMIT', 32): saved = self.load(fixture.bundle)
        snap = saved.graph_snapshot()
        self.assertTrue(snap['decimated']); self.assertEqual(snap['probes'], 500)
        self.assertEqual(snap['successes'], 499)
        self.assertLessEqual(len(snap['packets']), 32)
        self.assertLessEqual(len(snap['probe_rows']), 32)
        self.assertEqual(float(snap['probe_rows'][0]['elapsed_s']), .5)
        self.assertEqual(float(snap['probe_rows'][-1]['elapsed_s']), 250)
        self.assertTrue(any(row['outcome'] == 'timeout' for row in snap['probe_rows']))

    def test_interrupted_metadata_rebuilds_csv_counts_and_reports_warning(self):
        files = self.fixture.files(); meta = json.loads(files['session.json'])
        meta.update(status='running', probes=0, successes=0, elapsed_s=0)
        files['session.json'] = json.dumps(meta).encode()
        saved = self.load(self.write_bundle(files))
        self.assertEqual(saved.snapshot()['probes'], 8)
        self.assertEqual(saved.snapshot()['elapsed'], 4)
        self.assertTrue(any('without a recorded Stop' in warning for warning in saved.warnings))

    def test_empty_stopped_recording_can_be_opened(self):
        record = link.Recording(self.root/'empty','Empty',2,1); record.finish()
        saved = self.load(record.directory)
        self.assertEqual(saved.snapshot()['probes'], 0)
        self.assertIsNone(saved.snapshot()['success_pct'])
        self.assertEqual(graph_data(saved.snapshot())['raw'], [])

    def test_bad_schema_missing_files_invalid_zip_and_unsafe_identifiers(self):
        files = self.fixture.files()
        for meta in ({'schema':2}, {'schema':True}, {'id':'../../outside'}, {'requested_hz':True}):
            modified = dict(files); value = json.loads(files['session.json']); value.update(meta)
            modified['session.json'] = json.dumps(value).encode()
            with self.assertRaises(ValueError): self.load(self.write_bundle(modified))
        missing = dict(files); del missing['events.csv']
        with self.assertRaises(ValueError): self.load(self.write_bundle(missing))
        broken = self.root/'broken.zip'; broken.write_bytes(b'not a zip')
        with self.assertRaises(zipfile.BadZipFile): self.load(broken)

    def test_archive_paths_duplicates_symlinks_and_limits_are_rejected_without_extraction(self):
        files = self.fixture.files()
        files['../outside.txt'] = b'bad'
        with self.assertRaises(ValueError): self.load(self.write_bundle(files))
        self.assertFalse((self.root/'outside.txt').exists())
        duplicate = self.root/'duplicate.zip'
        with zipfile.ZipFile(duplicate,'w') as archive:
            for name, data in self.fixture.files().items(): archive.writestr(name, data)
            archive.writestr('packets.csv',self.fixture.files()['packets.csv'])
        with self.assertRaises(ValueError): self.load(duplicate)
        symlink = self.root/'symlink.zip'
        with zipfile.ZipFile(symlink,'w') as archive:
            for name, data in self.fixture.files().items():
                info = zipfile.ZipInfo(name)
                if name == 'packets.csv': info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info,data)
        with self.assertRaises(ValueError): self.load(symlink)
        with patch.object(replay,'MAX_FILE',32):
            with self.assertRaises(ValueError): self.load()

    def test_csv_validation_rejects_nonfinite_reverse_time_flags_and_inconsistent_units(self):
        variants = [('packets.csv','elapsed_s','nan'),('packets.csv','elapsed_s','-1'),
                    ('packets.csv','rssi_dBm','0'),('packets.csv','valid_sample','maybe'),
                    ('packets.csv','source_verified','False'),('packets.csv','source','../../x'),
                    ('probes.csv','latency_ms','inf'),('probes.csv','outcome','made-up'),
                    ('probes.csv','duration_s','.000001'),('events.csv','distance_m','-1'),
                    ('events.csv','detail','x'*4097)]
        for file, key, value in variants:
            with self.subTest(file=file,key=key,value=value):
                files = self.fixture.files()
                def change(fields, rows): rows[1][key] = value
                files[file] = changed_csv(files[file], change)
                with self.assertRaises(ValueError): self.load(self.write_bundle(files))
        files = self.fixture.files()
        files['packets.csv'] = changed_csv(files['packets.csv'],lambda f,rows:rows[2].update(elapsed_s='0'))
        with self.assertRaises(ValueError): self.load(self.write_bundle(files))
        self.assertFalse(list((self.root/'cache').glob('link-replay-*')))

    def test_dense_smoothing_and_checkpoint_windows_match_reference_medians(self):
        import statistics
        packets = [dict(elapsed_s=i/50, rssi_dBm=-40-i%20, source_verified=True,
                        valid_sample=True, detail='') for i in range(500)]
        events = [dict(elapsed_s=at, distance_m=at*10, event='marker', detail='test') for at in (0,2,4,6,8,10)]
        data = graph_data(dict(packets=packets, probe_rows=[], events=events, elapsed=9.98))
        expected = [statistics.median([p['rssi_dBm'] for p in packets[:i+1]
                                      if p['elapsed_s'] >= row['elapsed_s']-5]) for i,row in enumerate(packets)]
        np.testing.assert_allclose(data['median'], expected)
        distances = [statistics.median([p['rssi_dBm'] for p in packets
                                       if abs(p['elapsed_s']-event['elapsed_s']) <= 2]) for event in events]
        np.testing.assert_allclose(data['distance_y'], distances)

    def test_invalid_override_is_rejected_and_zero_is_a_legal_source(self):
        self.assertEqual(replay.parse_source('0x0000'), 0)
        for text in ('', 'FFFF', 'nothex', '0'*17):
            with self.assertRaises(ValueError): replay.parse_source(text)


class UITests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from kestrelsat.app import SerialGUI
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.old = Path.cwd(); os.chdir(self.tmp.name); self.addCleanup(os.chdir,self.old)
        self.root = tk.Tk(); self.app = SerialGUI(self.root); self.addCleanup(self.app.on_closing)
        self.fixture = Fixture(Path(self.tmp.name))
        self.tab = self.app.link_tracker

    def pump(self, condition, timeout=6):
        deadline = time.monotonic()+timeout
        while not condition() and time.monotonic() < deadline:
            self.root.update(); self.app.qt_app.processEvents(); time.sleep(.01)
        self.root.update(); self.assertTrue(condition(), self.tab.status.get())

    def load(self, source=None):
        self.tab.load_saved(self.fixture.bundle, source)
        self.pump(lambda:self.tab.io_worker is None)
        self.assertTrue(getattr(self.tab.recording,'imported',False),self.tab.status.get())

    def test_offline_load_rebuilds_full_graphs_without_serial_or_live_state(self):
        self.assertFalse(self.app.is_connected); self.assertIsNone(self.app.serial_connection)
        self.load()
        self.assertEqual(self.tab.mode.get(),'Full session')
        self.assertFalse(self.tab.busy); self.assertIsNone(self.tab.active)
        self.assertFalse(self.app._transfer_active.is_set())
        self.assertIn('last recorded RSSI',self.tab.summary.get())
        self.assertIsNotNone(self.tab.plot)
        self.assertEqual(self.tab.full_data['probes'],8)
        original = graph_data(self.fixture.record.graph_snapshot())
        for key,value in graph_data(self.tab.full_data).items(): np.testing.assert_allclose(value,original[key],equal_nan=True)

    def test_loaded_session_pngs_themes_markers_and_source_override_provenance(self):
        from PIL import Image
        self.load(source=0)
        for background in ('white','black'):
            self.app.graph_background_var.set(background); self.app.on_graph_background_selected()
            self.tab._draw(); self.app.qt_app.processEvents()
            self.assertIn('RSSI rechecked: 0000',self.tab.plot.header.text)
            for target in ('Combined','RSSI','Success','Latency','Distance'):
                path = Path(self.tmp.name)/(background+target+'.png')
                self.tab.plot.save(path,target)
                with Image.open(path) as image: self.assertGreater(image.width,500)
        self.assertTrue(self.tab.plot.markers) # Literal braces in checkpoint text are safe.
        self.tab.plot.close(); self.app.qt_app.processEvents()
        self.tab.show_graphs(); self.tab._draw()
        self.assertIn('saved session',self.tab.plot.header.text)

    def test_corrupt_import_keeps_current_session_and_does_not_change_radio_ownership(self):
        self.load(); previous = self.tab.recording
        path = Path(self.tmp.name)/'bad.zip'; path.write_bytes(b'bad')
        self.tab.load_saved(path); self.pump(lambda:self.tab.io_worker is None)
        self.assertIs(self.tab.recording,previous)
        self.assertIn('failed',self.tab.status.get())
        self.assertFalse(self.app._transfer_active.is_set())

    def test_dialog_paths_source_recheck_and_loading_are_available_without_connection(self):
        self.tab.source.set('0000'); self.tab.recheck_import.set(True)
        with patch('kestrelsat.link_tab.filedialog.askopenfilename',return_value=str(self.fixture.bundle)):
            self.tab.open_bundle()
        self.pump(lambda:self.tab.io_worker is None)
        self.assertEqual(self.tab.recording.source_override,0)
        with patch('kestrelsat.link_tab.filedialog.askdirectory',return_value=str(self.fixture.record.directory)):
            self.tab.open_folder()
        self.pump(lambda:self.tab.io_worker is None)
        self.assertEqual(len(self.tab.history),2)
        labels = list(self.tab.combo['values']); self.assertNotEqual(labels[0],labels[1])
        self.tab.selection.set(labels[1]); self.tab._select(); self.pump(lambda:self.tab.io_worker is None)
        self.assertEqual(self.tab.selection.get(),labels[1])

    def test_close_during_import_releases_cache_without_late_gui_delivery(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        constructor = replay.SavedSession; caches = []
        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(3): raise RuntimeError('test loader release timed out')
            saved = constructor(*args, **kwargs); caches.append(saved.cache.name)
            return saved
        with patch('kestrelsat.link_tab.link_replay.SavedSession',side_effect=delayed):
            self.tab.load_saved(self.fixture.bundle)
            self.assertTrue(entered.wait(1))
            self.tab.close(); release.set()
            self.tab.io_worker.join(4)
        self.assertFalse(self.tab.io_worker.is_alive())
        self.assertIsNone(self.tab.recording)
        self.assertIsNone(self.tab.timer)
        self.assertTrue(caches)
        self.assertTrue(all(not Path(path).exists() for path in caches))

    def test_active_tracking_blocks_import_without_touching_live_state(self):
        self.tab.busy = True
        try:
            self.tab.load_saved(self.fixture.bundle)
            self.assertIsNone(self.tab.io_worker)
            self.assertIn('Stop tracking',self.tab.status.get())
        finally: self.tab.busy = False


if __name__ == '__main__':
    if len(sys.argv)>1:
        unittest.main()
    else:
        result = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(CoreTests))
        failed = not result.wasSuccessful()
        for name in unittest.defaultTestLoader.getTestCaseNames(UITests):
            print('Replay GUI: '+name,flush=True)
            process = subprocess.run([sys.executable,str(Path(__file__).resolve()),'UITests.'+name])
            failed |= process.returncode != 0
        sys.exit(1 if failed else 0)
