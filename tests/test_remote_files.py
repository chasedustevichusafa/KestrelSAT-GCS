"""Portable KFS1 client and real Tk tab regression tests (no radio required)."""
from pathlib import Path
import os
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kestrelsat import remote_files as rf


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
        elif op == 'LIST':
            path, index = bytes.fromhex(command[1]).decode(), int(command[2])
            entries = ([['ENTRY', '1', 'D', '0', 'images'.encode().hex()],
                        ['ENTRY', '2', 'F', str(len(self.content)), 'data.bin'.encode().hex()]]
                       if path == '/' else [['ENTRY', '1', 'F', str(len(self.content)), 'photo.jpg'.encode().hex()]])
            response = entries[index] if index < len(entries) else ['END']
        elif op == 'OPEN':
            self.token = f'{request_id:08x}'
            self.offset = 0
            self.opens += 1
            response = ['FILE', self.token, str(len(self.content))]
        elif op == 'READ':
            if self.delay:
                return len(data)
            offset = int(command[2])
            block = self.content[offset:offset+128]
            self.offset = offset+len(block)
            response = ['DATA', str(offset), block.hex()]
        elif op == 'END':
            response = ['DONE', str(len(self.content)), f'{rf.zlib.crc32(self.content):08x}']
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
