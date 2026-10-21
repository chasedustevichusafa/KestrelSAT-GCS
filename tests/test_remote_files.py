"""Portable KFS1 client and real Tk tab regression tests (no radio required)."""
from pathlib import Path
import os
import json
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kestrelsat import remote_files as rf
from kestrelsat.transfer_diagnostics import Capture


class SatellitePort:
    """Deterministic wire peer; firmware compatibility is tested in Lab 8."""
    def __init__(self):
        self.timeout = 0.02
        self.write_timeout = 1
        self.rx = bytearray()
        self.content = bytes(range(256)) * 3 + b'+++\x00\xff\n'
        self.closed = False
        self.readers = set()
        self.write_threads = []
        self.delay = False
        self.token = '00000000'
        self.offset = 0
        self.opens = 0
        self.files = {}
        self.upload_support = True
        self.uploading = None
        self.completed = {}
        self.read_content = self.content

    @property
    def in_waiting(self):
        return len(self.rx)

    def write(self, data):
        self.write_threads.append(threading.get_ident())
        if self.closed:
            raise OSError('Port closed')
        parsed = rf.decode(data.strip())
        if not parsed:
            return len(data)
        request_id, command = parsed
        op = command[0]
        if op == 'HELLO':
            response = ['HELLO', '1', '128']
        elif op == 'CAPS':
            response = ['CAPS', 'UPLOAD1', '128'] if self.upload_support else ['ERR', 'BAD_COMMAND']
        elif op == 'PUT':
            target = bytes.fromhex(command[1]).decode()
            if target.lower() in ('/data.bin',) or any(path.lower() == target.lower() for path in self.files):
                response = ['ERR', 'TARGET_EXISTS']
            else:
                self.uploading = dict(token=f'{request_id:08x}', target=target, size=int(command[2]),
                                      crc=command[3], data=bytearray())
                response = ['READY', f'{request_id:08x}', command[2]]
        elif op in ('STATUS', 'ABORT', 'COMMIT') and command[1] in self.completed:
            response = self.completed[command[1]]
        elif op in ('WRITE', 'VERIFY', 'COMMIT', 'STATUS', 'ABORT'):
            upload = self.uploading
            if upload is None or command[1] != upload['token']:
                response = ['ERR', 'NO_SESSION']
            elif op == 'WRITE':
                offset, block = int(command[2]), bytes.fromhex(command[3])
                if offset == len(upload['data']):
                    upload['data'].extend(block)
                response = ['ACK', str(len(upload['data'])), str(upload['size'])]
            elif op == 'VERIFY':
                if f'{rf.zlib.crc32(upload["data"]):08x}' != upload['crc']:
                    response = ['ERR', 'VERIFY_CRC_MISMATCH']
                else:
                    response = ['CHECKED', str(min(upload['size'], int(command[2])+1024)), str(upload['size'])]
            elif op == 'COMMIT':
                self.files[upload['target']] = bytes(upload['data'])
                response = ['STORED', str(upload['size']), upload['crc']]
                self.completed[command[1]] = response
                self.uploading = None
            elif op == 'STATUS':
                response = ['UPLOADING', str(len(upload['data'])), str(upload['size'])]
            else:
                self.uploading = None
                response = ['ABORTED', 'REMOVED']
        elif op == 'CLEAN':
            target = bytes.fromhex(command[1]).decode()
            if target in self.files and rf.partial_path(target):
                del self.files[target]
                response = ['REMOVED']
            else:
                response = ['ERR', 'CLEAN_FAILED']
        elif op == 'LIST':
            path, index = bytes.fromhex(command[1]).decode(), int(command[2])
            entries = ([['ENTRY', '1', 'D', '0', 'images'.encode().hex()],
                        ['ENTRY', '2', 'F', str(len(self.content)), 'data.bin'.encode().hex()]]
                       if path == '/' else [['ENTRY', '1', 'F', str(len(self.content)), 'photo.jpg'.encode().hex()]])
            for target, content in self.files.items():
                if str(Path(target).parent).replace('\\', '/') == path:
                    entries.append(['ENTRY', str(len(entries)+1), 'F', str(len(content)), Path(target).name.encode().hex()])
            response = entries[index] if index < len(entries) else ['END']
        elif op == 'OPEN':
            self.token = f'{request_id:08x}'
            self.offset = 0
            self.opens += 1
            path = bytes.fromhex(command[1]).decode()
            self.read_content = self.files.get(path, self.content)
            response = ['FILE', self.token, str(len(self.read_content))]
        elif op == 'READ':
            if self.delay:
                return len(data)
            offset = int(command[2])
            block = self.read_content[offset:offset+128]
            self.offset = offset+len(block)
            response = ['DATA', str(offset), block.hex()]
        elif op == 'END':
            response = ['DONE', str(len(self.read_content)), f'{rf.zlib.crc32(self.read_content):08x}']
        elif op == 'CLOSE':
            response = ['CLOSED']
        else:
            response = ['ERR', 'BAD_COMMAND']
        # Fragment every response over multiple reads to exercise streaming.
        self.rx.extend(rf.encode(request_id, *response))
        return len(data)

    def read(self, count):
        self.readers.add(threading.get_ident())
        if not self.rx:
            time.sleep(min(self.timeout, 0.01))
            return b''
        data = bytes(self.rx[:min(count, 19)])
        del self.rx[:len(data)]
        return data

    def close(self):
        self.closed = True


class ClientTests(unittest.TestCase):
    def test_upload_capabilities_roundtrip_and_path_rules(self):
        port = SatellitePort()
        progress = []
        client = rf.Client(port, progress=lambda **info: progress.append(info))
        with tempfile.TemporaryDirectory() as tmp:
            source, downloaded = Path(tmp)/'source', Path(tmp)/'downloaded'
            for name, data in [('empty.txt', b''), ('binary.bin', port.content), ('photo.jpg', bytes(range(256))*56)]:
                source.write_bytes(data)
                path = '/images/'+name
                client.upload(source, path)
                client.download(path, downloaded)
                self.assertEqual(downloaded.read_bytes(), data)
            self.assertTrue(any('Verifying SD' in info.get('status', '') for info in progress))
            port.upload_support = False
            with self.assertRaisesRegex(rf.RemoteError, 'firmware needs the upload update'):
                client.upload(source, '/old-firmware.bin')
            self.assertNotIn('/old-firmware.bin', port.files)
        for path in ('/bad?.jpg', '/.KFS-aBcD1234.PART', '/trailing.', '/folder /file', '/bad:thing'):
            with self.subTest(path=path), self.assertRaises(rf.RemoteError):
                rf.upload_path_hex(path)

    def test_paths_and_frame_integrity(self):
        for path in ('relative', '/../secret', '/a//b', '/a/', '/a\nb', '/a\\b', '/'+'x'*193):
            with self.subTest(path=path), self.assertRaises(rf.RemoteError):
                rf.path_hex(path)
        frame = rf.encode(0x12345678, 'HELLO')
        self.assertEqual(rf.decode(frame), (0x12345678, ['HELLO']))
        self.assertIsNone(rf.decode(frame.replace(b'HELLO', b'HEXXO')))
        self.assertIsNone(rf.decode(b'x'*100000))
        self.assertNotIn(b'+++', rf.encode(12, 'OPEN', rf.path_hex('/+++ photo.jpg')))

    def test_listing_and_download(self):
        port = SatellitePort()
        client = rf.Client(port)
        self.assertEqual([e.name for e in client.list_directory('/')], ['images', 'data.bin'])
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'chosen.bin'
            client.download('/data.bin', dest)
            self.assertEqual(dest.read_bytes(), port.content)
            self.assertEqual(list(Path(tmp).iterdir()), [dest])

    def test_timeout_is_bounded_and_cancel_is_observed(self):
        port = SatellitePort()
        port.write = lambda data: len(data)
        client = rf.Client(port, timeout=0.02, attempts=2)
        with self.assertRaisesRegex(rf.RemoteError, 'did not reply'):
            client.hello()
        client.cancel.set()
        with self.assertRaises(rf.Cancelled):
            client.hello()

    def test_garbage_and_stale_frames_do_not_hide_valid_response(self):
        port = SatellitePort()
        port.rx.extend(b'x'*(rf.MAX_LINE+30) + b'\n' + rf.encode(1, 'ERR', 'STALE'))
        rf.Client(port).hello()

    def test_sd_failure_reports_offset_and_writes_metadata_capture(self):
        port = SatellitePort()
        original_write = port.write
        def write(data):
            request_id, fields = rf.decode(data.strip())
            if fields[0] == 'READ' and int(fields[2]) >= 128:
                port.rx.extend(rf.encode(request_id, 'ERR', 'READ_FAILED', 128, 128, -1, 3))
                return len(data)
            return original_write(data)
        port.write = write
        with tempfile.TemporaryDirectory() as tmp:
            capture = Capture(dict(path='/data.bin', baud=115200, mode='API'), directory=tmp)
            destination = Path(tmp)/'original.bin'
            destination.write_bytes(b'original')
            client = rf.Client(port, diagnostic=capture.record)
            with self.assertRaisesRegex(rf.RemoteError, 'READ_FAILED offset=128.*got=-1.*attempts=3.*operation=READ offset=128') as error:
                client.download('/data.bin', destination)
            capture.close('failed', detail=str(error.exception))
            rows = [json.loads(line) for line in capture.path.read_text().splitlines()]
            failure = next(row for row in rows if row['event'] == 'request_failed')
            self.assertEqual((failure['operation'], failure['offset']), ('READ', 128))
            self.assertEqual(rows[-1]['status'], 'failed')
            self.assertEqual(destination.read_bytes(), b'original')
            self.assertFalse(list(Path(tmp).glob('*.part')))
            self.assertNotIn(port.content.hex(), capture.path.read_text())

    def test_loss_and_corruption_retry_same_offset_with_diagnostics(self):
        port = SatellitePort()
        original_write = port.write
        attempts = []
        def write(data):
            request_id, fields = rf.decode(data.strip())
            if fields[0] == 'READ' and int(fields[2]) == 256:
                attempts.append(request_id)
                if len(attempts) <= 2:
                    return len(data)
                if len(attempts) == 3:
                    port.rx.extend(b'bad response\n')
                    return len(data)
            return original_write(data)
        port.write = write
        events = []
        client = rf.Client(port, timeout=0.015, diagnostic=lambda event, **info: events.append((event, info)))
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp)/'received.bin'
            client.download('/data.bin', destination)
            self.assertEqual(destination.read_bytes(), port.content)
        self.assertEqual(len(set(attempts)), 1)
        self.assertEqual(client.counters, dict(retries=3, timeouts=3, malformed=1, stale=0))
        self.assertEqual(sum(event == 'retry' for event, _ in events), 3)
        self.assertEqual(sum(event == 'timeout' for event, _ in events), 3)

    def test_unwritable_capture_does_not_abort_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocked = Path(tmp)/'blocked'
            blocked.write_text('file, not a directory')
            capture = Capture({}, directory=blocked)
            self.assertTrue(capture.error)
            port = SatellitePort()
            destination = Path(tmp)/'result'
            rf.Client(port, diagnostic=capture.record).download('/data.bin', destination)
            capture.close('downloaded')
            self.assertEqual(destination.read_bytes(), port.content)
            capture = Capture({}, directory=tmp)
            with patch.object(capture.stream, 'write', side_effect=OSError('disk full')):
                rf.Client(port, diagnostic=capture.record).download('/data.bin', destination)
            self.assertIn('disk full', capture.error)
            capture.close('downloaded')
            self.assertEqual(destination.read_bytes(), port.content)

    def test_capture_rotation_preserves_valid_metadata_and_final_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = Capture(dict(path='/test.bin'), directory=tmp, max_bytes=2048)
            for offset in range(100):
                capture.record('timeout', operation='READ', offset=offset)
            capture.close('failed', detail='test failure')
            for path in (capture.path, Path(str(capture.path)+'.1')):
                self.assertLessEqual(path.stat().st_size, 2048)
                self.assertTrue(all(isinstance(json.loads(line), dict) for line in path.read_text().splitlines()))
            last = json.loads(capture.path.read_text().splitlines()[-1])
            self.assertEqual((last['event'], last['status']), ('finished', 'failed'))


class TabTests(unittest.TestCase):
    def setUp(self):
        # Runtime files are kept out of the checkout, including on Windows CI.
        self.original = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        os.chdir(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.chdir, self.original)
        from kestrelsat.app import SerialGUI
        self.root = tk.Tk()
        self.app = SerialGUI(self.root)
        self.port = SatellitePort()
        self.app.serial_connection = self.port
        self.app.is_connected = True
        self.app.test_mode = False
        self.app.read_thread = threading.Thread(target=self.app.read_serial_data, daemon=True)
        self.app.read_thread.start()
        self.tab = self.app.remote_files
        self.root.update()
        self.addCleanup(self.app.on_closing)

    def finish(self, timeout=5):
        deadline = time.monotonic()+timeout
        while self.tab.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.005)
        self.root.update()
        self.assertFalse(self.tab.busy, 'Remote Files worker did not complete')

    def test_upload_ui_refresh_statistics_and_partial_cleanup(self):
        self.assertTrue(self.tab.upload_buttons[0].instate(['disabled']))
        self.tab.refresh(); self.finish()
        self.assertFalse(self.tab.upload_buttons[0].instate(['disabled']))
        source = Path(self.tmp.name)/'source.bin'
        source.write_bytes(bytes(range(256))*56+b'+++')
        with patch('kestrelsat.remote_tab.filedialog.askopenfilename', return_value=str(source)), \
             patch('kestrelsat.remote_tab.simpledialog.askstring', return_value='uploaded.bin'):
            self.tab.upload_selected()
        self.finish()
        self.assertEqual(self.port.files['/uploaded.bin'], source.read_bytes())
        self.assertIn('Upload verified', self.tab.status.get())
        self.assertTrue(any(entry.name == 'uploaded.bin' for entry in self.tab.entries.values()))
        session = self.app.transfer_recorder.sessions[-1]
        self.assertEqual((session.direction, session.status, session.done), ('upload', 'completed', source.stat().st_size))
        self.assertNotIn('@KFS1', self.app.received_text.get('1.0', 'end'))
        self.port.files['/.kfs-12345678.part'] = b'partial'
        self.tab.refresh(); self.finish()
        key = next(key for key, entry in self.tab.entries.items() if entry.name == '.kfs-12345678.part')
        self.tab.tree.selection_set(key)
        with patch('kestrelsat.remote_tab.messagebox.askyesno', return_value=True):
            self.tab.remove_partial()
        self.finish()
        self.assertNotIn('/.kfs-12345678.part', self.port.files)
        self.assertIn('Partial upload removed', self.tab.status.get())
        self.port.upload_support = False
        self.tab.refresh(); self.finish()
        self.assertTrue(self.tab.upload_buttons[0].instate(['disabled']))
        self.assertIn('updated satellite firmware', self.tab.status.get())

    def test_browse_download_themes_and_serial_ownership(self):
        self.tab.refresh()
        self.finish()
        self.assertEqual(len(self.tab.tree.get_children()), 2)
        folder = next(key for key, e in self.tab.entries.items() if e.directory)
        self.tab.tree.selection_set(folder)
        self.tab.open_selected()
        self.finish()
        self.assertEqual(self.tab.directory, '/images')
        self.tab.tree.selection_set(self.tab.tree.get_children()[0])
        destination = Path(self.tmp.name) / 'photo.jpg'
        with patch('kestrelsat.remote_tab.filedialog.asksaveasfilename', return_value=str(destination)):
            self.tab.download_selected()
        self.finish()
        self.assertEqual(destination.read_bytes(), self.port.content)
        self.assertIn('Verified and saved', self.tab.status.get())
        self.assertEqual(self.tab.progress.get(), 100)
        self.tab.up()
        self.finish()
        self.assertEqual(self.tab.directory, '/')
        for theme in ('light', 'high_contrast', 'dark'):
            self.app.themes.apply(theme)
            self.root.update()
            colors = self.app.themes.colors
            self.assertEqual(self.app.themes.style.lookup('RemoteFiles.Treeview', 'background'), colors['field_bg'])
            self.assertEqual(self.app.themes.style.lookup('RemoteFiles.Treeview', 'foreground'), colors['fg'])
        self.assertFalse(self.app._transfer_active.is_set())
        # File protocol frames must never reach the ordinary telemetry parser.
        self.assertNotIn('@KFS1', self.app.received_text.get('1.0', 'end'))

    def test_cancel_send_guard_and_disconnect_invalidation(self):
        self.tab.refresh()
        self.finish()
        self.port.delay = True
        self.tab._start('download', '/data.bin', str(Path(self.tmp.name) / 'cancelled.bin'))
        deadline = time.monotonic()+2
        while self.port.opens == 0 and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.005)
        self.app.send_entry.insert(0, '3')
        before = len(self.port.write_threads)
        with patch('kestrelsat.app.messagebox.showinfo') as info:
            self.app.send_data()
            info.assert_called_once()
        self.assertEqual(len(self.port.write_threads), before)
        self.tab.cancel()
        self.finish()
        self.assertFalse(list(Path(self.tmp.name).glob('*.part')))
        self.assertFalse((Path(self.tmp.name) / 'cancelled.bin').exists())
        self.app.disconnect()
        self.assertFalse(self.tab.entries)
        self.assertFalse(self.tab.tree.get_children())
        self.assertFalse(self.app._transfer_thread.is_alive())

    def test_disconnected_and_test_mode_explain_requirements(self):
        self.app.is_connected = False
        self.tab.refresh()
        self.assertIn('Connect', self.tab.status.get())
        self.app.is_connected = True
        self.app.test_mode = True
        self.tab.refresh()
        self.assertIn('Lab 8', self.tab.status.get())
        self.assertFalse(self.tab.busy)


if __name__ == '__main__':
    unittest.main()
