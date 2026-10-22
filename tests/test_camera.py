"""Camera protocol, real Tk controls/viewing, and legacy API transport integration."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import io
import gc
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
from kestrelsat import camera_files as cf, remote_files as rf
from kestrelsat.image_viewer import decode_image
from test_remote_files import SatellitePort


def jpeg(size=(640, 480)):
    with io.BytesIO() as stream:
        Image.new('RGB', size, (40, 90, 130)).save(stream, format='JPEG')
        return stream.getvalue()


class CameraPort(SatellitePort):
    def __init__(self):
        super().__init__()
        self.files['/images/old.jpg'] = jpeg()
        self.job = None
        self.captures = 0
        self.camera_support = self.camera_ready = True
        self.hold = False
        self.resolutions = (1 << 14)-1

    def write(self, data):
        parsed = rf.decode(data.strip())
        if not parsed:
            return super().write(data)
        request, command = parsed
        op = command[0]
        if op == 'CAPS':
            response = ['CAPS', 'UPLOAD1', 'MANAGE1', *(['CAMERA1'] if self.camera_support else []), '128']
        elif op == 'CAMINFO':
            response = ['CAMERA', '5MP', str(self.resolutions if self.camera_ready else 0),
                        'NONE' if self.camera_ready else 'CAMERA_NOT_FOUND', 'SD_READY']
        elif op in ('CAPTURE', 'CAMSTATUS'):
            job = command[1]
            if op == 'CAPTURE':
                if not self.job or self.job['job'] != job:
                    if not self.camera_ready:
                        self.rx.extend(rf.encode(request, 'ERR', 'CAMERA_UNAVAILABLE')); return len(data)
                    self.captures += 1
                    self.job = dict(job=job, path=bytes.fromhex(command[3]).decode(), mode=int(command[2]))
                    self.job['data'] = jpeg(cf.RESOLUTIONS[self.job['mode']])
                state = 'CAPTURING'
            else:
                state = 'CAPTURING' if self.hold else 'SAVED'
            if not self.job or self.job['job'] != job:
                response = ['CAMJOB', job, 'UNKNOWN', '0', '0', '00000000', '', 'UNKNOWN']
            else:
                content = self.job['data']
                if state == 'SAVED':
                    self.files[self.job['path']] = content
                response = ['CAMJOB', job, state, str(len(content) if state == 'SAVED' else 0),
                            str(len(content)), f'{rf.zlib.crc32(content):08x}', self.job['path'].encode().hex(), 'NONE']
        elif op == 'LIST' and bytes.fromhex(command[1]).decode() == '/images':
            entries = [(path, data) for path, data in sorted(self.files.items()) if path.startswith('/images/')]
            index = int(command[2])
            if index >= len(entries):
                response = ['END']
            else:
                path, content = entries[index]
                response = ['ENTRY', str(index+1), 'F', str(len(content)), path.rsplit('/', 1)[-1].encode().hex()]
        else:
            return super().write(data)
        self.rx.extend(rf.encode(request, *response))
        self.write_threads.append(threading.get_ident())
        return len(data)


class ClientTests(unittest.TestCase):
    def test_local_timestamp_offset_resolution_and_pending_validation(self):
        now = datetime(2026, 10, 21, 15, 30, 45, tzinfo=timezone(timedelta(hours=-6)))
        first = cf.new_capture(5, now)
        second = cf.new_capture(5, now)
        self.assertRegex(first['path'], r'^/images/20261021T153045-0600_640x480_[a-f0-9]{8}\.jpg$')
        self.assertNotEqual(first['path'], second['path'])
        self.assertEqual(cf.pending_capture(first), first)
        for value in (None, {}, dict(first, path='/elsewhere.jpg'), dict(first, job='bad'), dict(first, path=None), dict(first, mode=None)):
            self.assertIsNone(cf.pending_capture(value))
        with self.assertRaises(rf.RemoteError):
            cf.new_capture(5, now.replace(tzinfo=None))

    def test_capture_info_status_and_no_automatic_download(self):
        port = CameraPort()
        client = cf.CameraClient(port, timeout=.1)
        info = client.camera_info()
        self.assertTrue(info.ready and info.sd_ready)
        self.assertEqual(len(info.modes), 14)
        pending = cf.new_capture(3)
        result = client.wait_capture(pending, client.start_capture(pending), poll_interval=.001)
        self.assertEqual(result['state'], 'SAVED')
        self.assertEqual(port.opens, 0)
        self.assertIn(pending['path'], port.files)
        port.camera_support = False
        with self.assertRaisesRegex(rf.RemoteError, 'firmware update'):
            client.camera_info()

    def test_cancel_wait_retains_job_and_invalid_replies_are_rejected(self):
        port = CameraPort(); port.hold = True
        cancel = threading.Event()
        client = cf.CameraClient(port, cancel=cancel, timeout=.1)
        pending = cf.new_capture(5)
        initial = client.start_capture(pending)
        cancel.set()
        with self.assertRaises(rf.OperationUncertain):
            client.wait_capture(pending, initial)
        self.assertEqual(port.captures, 1)
        with self.assertRaises(rf.RemoteError):
            client._job(['CAMJOB', pending['job'], 'SAVED', '5', '4', '00000000', pending['path'].encode().hex(), 'NONE'], pending['job'], pending['path'])
        with self.assertRaises(rf.RemoteError):
            client._job(['CAMJOB', pending['job'], 'SAVED', '4', '4', '00000000', '/wrong.jpg'.encode().hex(), 'NONE'], pending['job'], pending['path'])

    def test_view_limit_uses_open_metadata_and_closes_oversized_session(self):
        class LargePort(CameraPort):
            def write(self, data):
                parsed = rf.decode(data.strip())
                if parsed and parsed[1][0] == 'OPEN':
                    self.rx.extend(rf.encode(parsed[0], 'FILE', '12345678', cf.MAX_IMAGE+1))
                    return len(data)
                return super().write(data)
        with tempfile.TemporaryDirectory() as tmp:
            port = LargePort()
            client = rf.Client(port, timeout=.1)
            with self.assertRaisesRegex(rf.RemoteError, 'viewing size limit'):
                client.download('/images/old.jpg', Path(tmp)/'view.jpg', max_bytes=cf.MAX_IMAGE)
            self.assertFalse(list(Path(tmp).iterdir()))
            self.assertFalse(port.rx)

    def test_decode_rejects_corruption_and_non_jpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'image.jpg'
            path.write_bytes(jpeg())
            decoded = decode_image(path)
            self.assertEqual(decoded.size, (640, 480)); decoded.close()
            Image.new('RGB', (20, 20)).save(path, format='PNG')
            with self.assertRaises(ValueError): decode_image(path)
            path.write_bytes(b'not an image')
            with self.assertRaises(Exception): decode_image(path)


class TabTests(unittest.TestCase):
    def setUp(self):
        # Each case creates a fresh Tcl interpreter. Collect the previous case
        # here, on the Tk thread, before any new radio/transfer worker can run.
        gc.collect()
        self.original = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        os.chdir(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.chdir, self.original)
        from kestrelsat.app import SerialGUI
        self.root = tk.Tk(); self.app = SerialGUI(self.root)
        self.port = CameraPort()
        self.app.serial_connection = self.port
        self.app.is_connected = True; self.app.test_mode = False
        self.app.read_thread = threading.Thread(target=self.app.read_serial_data, daemon=True)
        self.app.read_thread.start()
        self.tab = self.app.camera
        self.app.notebook.select(self.tab.frame)
        self.root.update()
        self.addCleanup(self.app.on_closing)

    def finish(self, timeout=7):
        deadline = time.monotonic()+timeout
        while self.tab.busy and time.monotonic() < deadline:
            self.root.update(); time.sleep(.005)
        self.root.update()
        self.assertFalse(self.tab.busy)
        self.assertFalse(self.app._transfer_active.is_set())

    def test_capture_view_download_rename_delete_and_statistics(self):
        recording = self.app.transfer_recorder.start_recording('Camera test')
        self.tab.refresh(); self.finish()
        self.assertEqual(str(self.tab.capture_button['state']), 'normal')
        self.tab.resolution.set('320x240')
        self.tab.capture_selected(); self.finish()
        self.assertEqual(self.port.captures, 1)
        self.assertEqual(self.port.opens, 0, 'Capture must not download automatically')
        self.assertIsNone(self.tab.pending)
        selected = self.tab.tree.selection()[0]
        name = self.tab.entries[selected].name
        self.assertIn('_320x240_', name)
        self.assertIn('saved', self.tab.status.get())
        self.tab.view_selected(); self.finish()
        self.assertEqual(len(self.tab.viewers), 1)
        viewer = self.tab.viewers[0]
        self.assertEqual(viewer.original.size, (320, 240))
        self.assertTrue(viewer.path.exists())
        viewer.full_size(); viewer.render()
        self.assertEqual(viewer.zoom, 1)
        viewer.change_zoom(.8); viewer.render()
        self.assertLess(viewer.zoom, 1)
        for theme in ('light', 'high_contrast', 'dark'):
            self.app.themes.apply(theme)
        viewer.close(); self.assertFalse(self.tab.viewers)
        stats = self.app.transfer_recorder.active.snapshot()
        self.assertEqual((stats['direction'], stats['status']), ('download', 'completed'))
        with patch('kestrelsat.remote_tab.simpledialog.askstring', return_value='renamed.jpg'):
            self.tab.rename_selected()
        self.finish()
        key = next(key for key, entry in self.tab.entries.items() if entry.name == 'renamed.jpg')
        self.tab.tree.selection_set(key)
        with patch('kestrelsat.remote_tab.messagebox.askyesno', return_value=False):
            self.tab.delete_selected()
        self.assertIn('/images/renamed.jpg', self.port.files)
        with patch('kestrelsat.remote_tab.messagebox.askyesno', return_value=True):
            self.tab.delete_selected()
        self.finish()
        self.assertNotIn('/images/renamed.jpg', self.port.files)
        self.app.transfer_recorder.stop_recording()
        self.assertEqual(recording.snapshot()['operation_count'], 1)
        self.assertTrue(any(event['event'] == 'camera_saved' for event in recording.events))

    def test_disconnect_and_check_recover_without_new_capture(self):
        self.tab.refresh(); self.finish()
        self.port.hold = True
        self.tab.capture_selected()
        deadline = time.monotonic()+2
        while self.port.captures == 0 and time.monotonic() < deadline:
            self.root.update(); time.sleep(.005)
        pending = dict(self.tab.pending)
        self.app.disconnect()
        self.root.update()
        self.assertEqual(self.tab.pending, pending)
        self.assertIsNotNone(cf.pending_capture(self.app.settings.get('camera_pending')))
        # Simulate reconnect to the same onboard job after the radio link returns.
        self.port.closed = False; self.port.hold = False
        self.app.serial_connection = self.port
        self.app.is_connected = True
        self.app.stop_reading = threading.Event()
        self.app.read_thread = threading.Thread(target=self.app.read_serial_data, daemon=True)
        self.app.read_thread.start()
        self.tab.check_capture(); self.finish()
        self.assertEqual(self.port.captures, 1)
        self.assertIsNone(self.tab.pending)
        self.assertIn('saved', self.tab.status.get())

    def test_unsupported_or_missing_camera_keeps_gallery_available(self):
        self.port.camera_support = False
        self.tab.refresh(); self.finish()
        self.assertEqual(str(self.tab.capture_button['state']), 'disabled')
        self.assertTrue(self.tab.entries)
        self.assertIn('firmware update', self.tab.status.get())
        self.port.camera_support = True; self.port.camera_ready = False
        self.tab.refresh(); self.finish()
        self.assertEqual(str(self.tab.capture_button['state']), 'disabled')
        self.assertIn('CAMERA_NOT_FOUND', self.tab.status.get())
        self.assertTrue(self.tab.entries)
        self.app._transfer_active.set()
        self.tab.capture_selected()
        self.assertEqual(self.port.captures, 0)
        self.app._transfer_active.clear()

    def test_bad_jpeg_keeps_verified_download_and_reports_view_error(self):
        self.port.files['/images/bad.jpg'] = b'not a valid JPEG'
        self.tab.refresh(); self.finish()
        key = next(key for key, entry in self.tab.entries.items() if entry.name == 'bad.jpg')
        self.tab.tree.selection_set(key)
        self.tab.view_selected(); self.finish()
        self.assertFalse(self.tab.viewers)
        self.assertIn('cannot display', self.tab.status.get())
        self.assertEqual(self.app.transfer_recorder.active.status, 'completed')
        self.assertTrue(list((Path(self.app.downloads_dir)/'camera').glob('view-*/bad.jpg')))

    def test_legacy_api_capture_and_view_keep_one_raw_reader(self):
        from kestrelsat.xbee_api import ApiSerial
        from test_xbee_api import Radio
        self.app.disconnect()
        peer = CameraPort()
        raw = Radio(peer); raw.receive_format = 'legacy64'
        port = ApiSerial(raw).initialize()
        self.app.serial_connection = port; self.app.is_connected = True
        self.app.stop_reading = threading.Event()
        self.app.read_thread = threading.Thread(target=self.app.read_serial_data, daemon=True)
        self.app.read_thread.start()
        self.tab.refresh(); self.finish()
        self.tab.capture_selected(); self.finish()
        self.tab.view_selected(); self.finish()
        self.assertEqual(peer.captures, 1)
        self.assertEqual(len(raw.readers), 1)
        self.assertEqual(len(self.tab.viewers), 1)
        self.assertEqual(self.app.transfer_recorder.active.status, 'completed')


if __name__ == '__main__':
    unittest.main()
