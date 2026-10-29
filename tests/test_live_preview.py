"""Preview ownership, display-before-delete, recovery, scheduling and real Tk UI."""
from pathlib import Path
import csv
import gc
import json
import os
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from kestrelsat import preview, camera_files as camera, remote_files as files
from test_camera import CameraPort


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.port = CameraPort()
        self.client = camera.CameraClient(self.port, timeout=.02, attempts=2)
        self.request = preview.new_preview(3)
        self.receipts = []
        self.records = []
        self.metrics = {}
        self.progress = []

    def tearDown(self):
        for receipt in self.receipts:
            if receipt.accepted:
                receipt.image.close()

    def publish(self, receipt):
        self.assertIn(self.request['path'], self.port.files, 'SD image deleted before presentation')
        self.assertEqual(receipt.image.size, (320, 240))
        receipt.present(lambda item: self.receipts.append(item))

    def cycle(self, request=None, **kwargs):
        arguments = dict(recover=False, publish=self.publish, checkpoint=self.records.append,
                         progress=lambda **data: self.progress.append(data), download_started=lambda: None,
                         download_finished=lambda: None, metrics=self.metrics)
        arguments.update(kwargs)
        return preview.run_frame(self.client, request or self.request, self.root/'frame.jpg', **arguments)

    def test_owned_namespace_and_strict_settings_validation(self):
        self.assertEqual(preview.pending_preview(self.request), self.request)
        ordinary = camera.new_capture(3)
        self.assertIsNone(preview.pending_preview(ordinary))
        for value in (dict(self.request, path='/images/old.jpg'), dict(self.request, phase='bad'),
                      dict(self.request, phase='deleting'), dict(self.request, job='00000000')):
            self.assertIsNone(preview.pending_preview(value))
        deleting = dict(self.request, phase='deleting', delete_request='1234abcd')
        self.assertEqual(preview.pending_preview(deleting), deleting)

    def test_capture_display_delete_order_and_user_photographs_preserved(self):
        old = dict(self.port.files)
        self.cycle()
        self.assertEqual(self.port.captures, 1)
        self.assertNotIn(self.request['path'], self.port.files)
        self.assertEqual(self.port.files, old)
        self.assertTrue((self.root/'frame.jpg').exists())
        self.assertEqual(self.records[-1]['phase'], 'deleting')
        self.assertEqual(self.metrics['transfer_profile'], '128-HEX-batch1')
        self.assertEqual(self.progress[-1]['done'], self.metrics['bytes'])

    def test_rejected_or_stale_display_keeps_sd_file_and_removes_local_staging(self):
        with self.assertRaisesRegex(files.RemoteError, 'display was not accepted'):
            self.cycle(publish=lambda receipt: receipt.reject())
        self.assertIn(self.request['path'], self.port.files)
        self.assertFalse((self.root/'frame.jpg').exists())
        self.assertFalse(self.records)

    def test_decode_and_dimension_errors_never_delete(self):
        with patch('kestrelsat.preview.decode_image', side_effect=ValueError('bad JPEG')):
            with self.assertRaisesRegex(ValueError, 'bad JPEG'):
                self.cycle()
        self.assertIn(self.request['path'], self.port.files)
        self.assertFalse((self.root/'frame.jpg').exists())
        self.request = preview.new_preview(3)
        with patch('kestrelsat.preview.decode_image', return_value=Image.new('RGB', (10, 10))):
            with self.assertRaisesRegex(files.RemoteError, 'dimensions'):
                self.cycle()
        self.assertIn(self.request['path'], self.port.files)

    def test_failed_delete_checkpoint_prevents_destructive_request(self):
        def failed(record):
            raise OSError('settings disk unavailable')
        with self.assertRaisesRegex(OSError, 'settings disk'):
            self.cycle(checkpoint=failed)
        self.assertIn(self.request['path'], self.port.files)
        self.assertFalse(self.port.mutations)
        self.assertTrue((self.root/'frame.jpg').exists())

    def test_stop_after_display_keeps_sd_file(self):
        def stop(receipt):
            self.publish(receipt)
            self.client.cancel.set()
        with self.assertRaises(files.Cancelled):
            self.cycle(publish=stop)
        self.assertIn(self.request['path'], self.port.files)
        self.assertFalse(self.records)

    def test_replaced_preview_file_is_not_deleted_even_with_valid_transfer_crc(self):
        from test_camera import jpeg
        replacement = jpeg((320, 240))+b'changed after original capture'
        def replace():
            self.port.files[self.request['path']] = replacement
        with self.assertRaisesRegex(files.RemoteError, 'changed since capture'):
            self.cycle(download_started=replace)
        self.assertEqual(self.port.files[self.request['path']], replacement)
        self.assertFalse(self.records)

    def test_lost_delete_and_result_replies_recover_without_new_capture_or_delete(self):
        original = self.port.write
        dropping = [True]
        deletes = []
        def write(data):
            parsed = files.decode(data.strip())
            count = original(data)
            if parsed and parsed[1][0] == 'DELETE':
                deletes.append(parsed[0])
            if dropping[0] and parsed and parsed[1][0] in ('DELETE', 'RESULT'):
                self.port.rx.clear()
            return count
        self.port.write = write
        with self.assertRaises(files.OperationUncertain):
            self.cycle()
        self.assertNotIn(self.request['path'], self.port.files)
        pending = self.records[-1]
        self.assertEqual(pending['phase'], 'deleting')
        dropping[0] = False
        before = len(deletes)
        self.cycle(pending, recover=True)
        self.assertEqual((self.port.captures, len(deletes)), (1, before))
        self.port.mutations.clear()
        with self.assertRaisesRegex(files.OperationUncertain, 'cleanup is unconfirmed'):
            self.cycle(pending, recover=True)
        self.assertEqual(self.port.captures, 1)

    def test_pending_capture_recovers_same_job_without_reexposure(self):
        self.client.start_capture(self.request)
        self.cycle(recover=True)
        self.assertEqual(self.port.captures, 1)
        self.assertNotIn(self.request['path'], self.port.files)

    def test_schedule_has_no_backlog_or_early_captures(self):
        self.assertEqual(preview.next_frame_time(100, 5, 102), 105)
        self.assertEqual(preview.next_frame_time(100, 5, 108), 108)
        self.assertEqual(preview.next_frame_time(100, 10, 108), 110)
        with self.assertRaises(ValueError): preview.next_frame_time(0, 7, 1)

    def test_cancelled_gui_checkpoint_cannot_execute_late(self):
        call = preview.GuiRequest('identity')
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(files.Cancelled): call.wait(cancel)
        mutations = []
        call.perform(mutations.append)
        self.assertFalse(mutations)


class TabTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        cwd = os.getcwd(); os.chdir(self.tmp.name); self.addCleanup(os.chdir, cwd)
        from kestrelsat.app import SerialGUI
        self.root = tk.Tk(); self.app = SerialGUI(self.root)
        self.port = CameraPort()
        self.app.serial_connection = self.port
        self.app.is_connected = True; self.app.test_mode = False
        self.app.read_thread = threading.Thread(target=self.app.read_serial_data, daemon=True)
        self.app.read_thread.start()
        self.tab = self.app.live_preview
        self.app.notebook.select(self.tab.frame)
        self.root.update()
        self.tab.resolution.set('320x240')
        self.addCleanup(self.app.on_closing)

    def until(self, predicate, timeout=7):
        deadline = time.monotonic()+timeout
        while not predicate() and time.monotonic() < deadline:
            self.root.update(); time.sleep(.005)
        self.root.update()
        self.assertTrue(predicate(), self.tab.status.get())

    def one_frame(self):
        count = self.tab.frame_count
        self.tab.start()
        self.until(lambda: self.tab.frame_count > count and not self.tab.busy)

    def test_off_by_default_full_frame_save_cache_bounds_and_no_gallery_scan(self):
        self.assertEqual(self.port.captures, 0)
        self.assertFalse(self.tab.running)
        recording = self.app.transfer_recorder.start_recording('Preview')
        commands = []
        original = self.port.write
        def write(data):
            parsed = files.decode(data.strip())
            if parsed: commands.append(parsed[1][0])
            return original(data)
        self.port.write = write
        self.one_frame()
        self.assertNotIn('LIST', commands)
        self.assertEqual(self.tab.image.size, (320, 240))
        self.assertGreaterEqual(self.tab.current_metadata['age_at_display'], .2)
        self.assertFalse(self.tab.pending)
        self.assertEqual(list(self.port.files), ['/images/old.jpg'])
        self.assertEqual(self.app.transfer_recorder.active.status, 'completed')
        saved = Path(self.tmp.name)/'saved.jpg'
        with patch('kestrelsat.preview_tab.filedialog.asksaveasfilename', return_value=str(saved)):
            self.tab.save_frame()
        self.assertTrue(saved.exists())
        self.tab.next_due = 0
        self.until(lambda: self.tab.frame_count == 2 and not self.tab.busy)
        self.assertEqual(len(list(self.tab.cache.glob('*.jpg'))), 1)
        self.assertTrue(saved.exists())
        self.tab.stop()
        with self.tab.log_path.open() as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['outcome'] == 'completed' for row in rows))
        self.assertTrue(rows[-1]['display_interval_s'])
        self.assertEqual(recording.snapshot()['operation_count'], 2)
        self.assertFalse(self.app._transfer_active.is_set())

    def test_stop_during_capture_manual_capture_gate_and_recovery(self):
        self.port.hold = True
        self.tab.start()
        self.until(lambda: self.port.captures == 1)
        request = dict(self.tab.pending)
        self.tab.stop()
        self.until(lambda: not self.tab.busy)
        self.assertEqual(self.tab.pending, request)
        self.assertFalse(self.tab.running)
        self.app.camera.capture_selected()
        self.assertIn('Live Preview', self.app.camera.status.get())
        self.assertEqual(self.port.captures, 1)
        self.port.hold = False
        self.tab.recover()
        self.until(lambda: not self.tab.busy)
        self.assertEqual(self.port.captures, 1)
        self.assertIsNone(self.tab.pending)
        self.assertFalse(self.tab.running)

    def test_other_operations_are_not_preempted_and_manual_pending_blocks_start(self):
        self.app._transfer_active.set()
        self.tab.start()
        self.root.update()
        self.assertTrue(self.tab.running)
        self.assertEqual(self.port.captures, 0)
        self.app._transfer_active.clear()
        self.until(lambda: self.tab.frame_count == 1 and not self.tab.busy)
        self.tab.stop()
        self.app.camera._save_pending(camera.new_capture(3))
        self.tab.start()
        self.assertFalse(self.tab.running)
        self.assertIn('Check capture', self.tab.status.get())

    def test_disconnect_invalidates_queued_images_and_never_auto_restarts(self):
        self.port.hold = True
        self.tab.start()
        self.until(lambda: self.port.captures == 1)
        self.app.disconnect()
        self.root.update()
        self.assertFalse(self.tab.running)
        self.assertIsNotNone(self.tab.pending)
        receipt = preview.FrameReceipt(Path('stale.jpg'), Image.new('RGB', (1, 1)), {})
        self.tab.on_event(dict(event='frame', generation=self.tab.generation-1, receipt=receipt))
        self.assertTrue(receipt.finished)
        self.assertFalse(receipt.accepted)
        receipt.image.close()
        self.assertEqual(self.port.captures, 1)

    def test_initial_checkpoint_failure_prevents_capture(self):
        self.tab.info = camera.CameraInfo('5MP', (1 << 14)-1, 'NONE', True)
        with patch.object(self.app, '_write_settings', return_value=False):
            self.tab.running = True; self.tab.next_due = 0
            self.tab._tick()
        self.assertEqual(self.port.captures, 0)
        self.assertFalse(self.tab.running)
        self.assertIn('recovery record', self.tab.status.get())

    def test_critical_queue_drop_finishes_through_mailbox_without_issuing_commands(self):
        # Dropped probe completion must still release Busy. Probe results carry
        # no image; the mailbox schedules another safe cycle only when running.
        self.tab.running = True
        with patch.object(self.app, '_post_rx', return_value=None):
            self.tab._launch('probe')
            self.until(lambda: not self.tab.busy)
        self.tab.stop()
        self.assertEqual(self.port.captures, 0)
        self.assertFalse(self.app._transfer_active.is_set())

    def test_save_dialog_copies_clicked_frame_even_if_display_changes(self):
        self.one_frame(); self.tab.stop()
        clicked = self.tab.current_path.read_bytes()
        target = Path(self.tmp.name)/'clicked.jpg'
        def dialog(**kwargs):
            # Native dialogs permit GUI event pumping. Model another frame
            # replacing/removing the displayed cache while the dialog is open.
            self.tab.current_path.write_bytes(b'new frame contents')
            return str(target)
        with patch('kestrelsat.preview_tab.filedialog.asksaveasfilename', side_effect=dialog):
            self.tab.save_frame()
        self.assertEqual(target.read_bytes(), clicked)
        self.assertFalse(list(self.tab.cache.glob('save-*')))

    def test_restart_loads_pending_identity_but_does_not_start_preview(self):
        self.port.hold = True
        self.tab.start()
        self.until(lambda: self.port.captures == 1)
        request = dict(self.tab.pending)
        self.app.on_closing()
        self.assertFalse(self.tab.cache.exists())
        from kestrelsat.app import SerialGUI
        root = tk.Tk(); app = SerialGUI(root)
        try:
            self.assertEqual(app.live_preview.pending, request)
            self.assertFalse(app.live_preview.running)
            self.assertFalse(app.live_preview.busy)
            self.assertEqual(self.port.captures, 1)
        finally:
            app.on_closing()

    def test_unsupported_resolution_and_legacy_api_single_raw_reader(self):
        from test_xbee_api import Radio
        from kestrelsat.xbee_api import ApiSerial
        self.app.disconnect()
        peer = CameraPort(); peer.resolutions = 1 << 3
        raw = Radio(peer); raw.receive_format = 'legacy64'
        port = ApiSerial(raw).initialize()
        self.app.serial_connection = port; self.app.is_connected = True
        self.app.stop_reading = threading.Event()
        self.app.read_thread = threading.Thread(target=self.app.read_serial_data, daemon=True)
        self.app.read_thread.start()
        self.tab.resolution.set('1920x1080')
        self.one_frame()
        self.assertEqual(self.tab.resolution.get(), '320x240')
        self.assertEqual(len(raw.readers), 1)
        self.tab.stop()
        for theme in ('light', 'high_contrast', 'dark'):
            self.app.themes.apply(theme)
        self.root.geometry('800x600'); self.root.update()
        self.tab._render()
        self.assertGreater(self.tab.canvas.winfo_width(), 1)


if __name__ == '__main__':
    unittest.main()
