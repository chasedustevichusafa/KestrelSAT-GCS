"""Portable negotiated-speed, loss recovery, API and recording regressions.

The peer below models a byte stream, not RF timing. Actual C++ firmware is
exercised separately by Lab 8 test/test_protocol.py. Nothing here claims an
on-air speedup; the wire-byte/request reductions are deterministic measurements.
"""
from pathlib import Path
import base64
import csv
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kestrelsat import remote_files as rf
from kestrelsat.transfer_stats import Recorder
from test_remote_files import SatellitePort


class SpeedSatellitePort(SatellitePort):
    """FAST1 fixture with two-entry successful-data replay, like the firmware."""
    def __init__(self):
        super().__init__()
        self.chunk, self.codec = 128, 'HEX'
        self.frames = []
        self.replay = {}
        self.drop_request = self.drop_response = False
        self.drop_second = False
        self.data_replies = 0
        self.bytes_written = 0
        self.bytes_received = 0

    def read(self, count):
        data = super().read(count)
        self.bytes_received += len(data)
        return data

    def write(self, data):
        self.bytes_written += len(data)
        for line in bytes(data).splitlines():
            parsed = rf.decode(line)
            if not parsed:
                continue
            request_id, command = parsed
            op = command[0]
            self.frames.append((request_id, command))
            if op in ('READX', 'WRITEX') and self.drop_request:
                self.drop_request = False
                continue
            if line in self.replay:
                response = self.replay[line]
            elif op == 'CAPS':
                response = rf.encode(request_id, 'CAPS', 'UPLOAD1', 'MANAGE1', 'FAST1', 128)
            elif op in ('OPENX', 'PUTX'):
                self.chunk, self.codec = int(command[-2]), command[-1]
                self.replay.clear()
                self._legacy(request_id, ['OPEN' if op == 'OPENX' else 'PUT', *command[1:-2]])
                response = bytes(self.rx); self.rx.clear()
                fields = rf.decode(response)[1]
                response = rf.encode(request_id, *fields, self.chunk, self.codec)
            elif op == 'READX':
                offset = int(command[2])
                if offset != self.offset:
                    response = rf.encode(request_id, 'ERR', 'BAD_OFFSET')
                else:
                    block = self.read_content[offset:offset+self.chunk]
                    self.offset += len(block)
                    text = block.hex() if self.codec == 'HEX' else base64.b64encode(block).decode()
                    response = rf.encode(request_id, 'DATA', offset, text)
            elif op == 'WRITEX':
                block = bytes.fromhex(command[3]) if self.codec == 'HEX' else base64.b64decode(command[3])
                if int(command[2]) != len(self.uploading['data']):
                    response = rf.encode(request_id, 'ERR', 'BAD_OFFSET')
                else:
                    self.uploading['data'].extend(block)
                    response = rf.encode(request_id, 'ACK', len(self.uploading['data']), self.uploading['size'])
            else:
                self._legacy(request_id, command)
                continue
            fields = rf.decode(response)[1]
            if op in ('READX', 'WRITEX') and fields[0] in ('DATA', 'ACK'):
                if line not in self.replay:
                    self.replay[line] = response
                    if len(self.replay) > 2:
                        del self.replay[next(iter(self.replay))]
                self.data_replies += 1
                if self.drop_response or self.drop_second and self.data_replies == 2:
                    self.drop_response = False
                    continue
            self.rx.extend(response)
        return len(data)

    def _legacy(self, request_id, command):
        # Parent counts real worker/reader ownership and implements SD operations.
        super().write(rf.encode(request_id, *command))


class TransferSpeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_all_profiles_round_trip_binary_and_tail_lengths(self):
        for profile in rf.PROFILE_LABELS:
            for size in (0, 1, 2, 127, 128, 129, 255, 256, 257, 511, 512, 513, 1025, 4099):
                with self.subTest(profile=profile, size=size):
                    port = SpeedSatellitePort()
                    payload = bytes(range(256))*(size//256)+bytes(range(size % 256))
                    source = self.root/'source'; source.write_bytes(payload)
                    client = rf.Client(port, timeout=.05, profile=profile)
                    client.upload(source, '/speed.bin')
                    self.assertEqual(port.files['/speed.bin'], payload)
                    result = self.root/'result'
                    client.download('/speed.bin', result)
                    self.assertEqual(result.read_bytes(), payload)

    def test_batch_recovers_missing_first_request_first_or_second_reply(self):
        for fault in ('drop_request', 'drop_response', 'drop_second'):
            for direction in ('download', 'upload'):
                with self.subTest(fault=fault, direction=direction):
                    port = SpeedSatellitePort(); port.content = bytes(range(256))*13
                    setattr(port, fault, True)
                    client = rf.Client(port, timeout=.015, profile='batch2')
                    if direction == 'download':
                        result = self.root/'result'; client.download('/data.bin', result)
                        self.assertEqual(result.read_bytes(), port.content)
                    else:
                        source = self.root/'source'; source.write_bytes(port.content)
                        client.upload(source, '/upload.bin')
                        self.assertEqual(port.files['/upload.bin'], port.content)
                    self.assertGreater(client.counters['retries'], 0)

    def test_old_firmware_auto_fallback_and_explicit_profile_rejection(self):
        for support in (True, False):
            port = SatellitePort(); port.upload_support = support
            client = rf.Client(port, profile='auto')
            client.download('/data.bin', self.root/'old')
            self.assertEqual(client.transfer_profile.label, '128-HEX-batch1')
            self.assertEqual((self.root/'old').read_bytes(), port.content)
            with self.assertRaisesRegex(rf.RemoteError, 'FAST1'):
                rf.Client(port, profile='b64_512').download('/data.bin', self.root/'bad')
            self.assertFalse((self.root/'bad').exists())

    def test_merged_replies_and_fragmented_second_frame_are_preserved(self):
        # Override the fixture's tiny reads so one read contains a complete first
        # frame PLUS the beginning of frame two. This exposed the old early-return
        # parser's tendency to discard trailing bytes from the same serial read.
        port = SpeedSatellitePort(); port.content = bytes(range(256))*12
        def read(count):
            data = bytes(port.rx[:count]); del port.rx[:len(data)]; return data
        port.read = read
        client = rf.Client(port, timeout=.02, profile='batch2')
        client.download('/data.bin', self.root/'merged')
        self.assertEqual((self.root/'merged').read_bytes(), port.content)
        self.assertEqual(client.counters['retries'], 0)

    def test_compact_payload_is_strict_and_smaller_on_wire(self):
        client = rf.Client(SatellitePort())
        client.transfer_profile = rf.PROFILES['b64_512']
        for invalid in ('AB==', 'AAB=', '=AAA', 'AAAA=', 'AA==\n', '!!!!'):
            with self.subTest(invalid=invalid), self.assertRaises(rf.RemoteError):
                client._decode_block(invalid)
        totals = {}
        for profile in ('legacy', 'hex256', 'b64_256', 'b64_512', 'batch2'):
            port = SpeedSatellitePort(); port.content = bytes(range(256))*64
            client = rf.Client(port, profile=profile)
            client.download('/data.bin', self.root/'count')
            data_frames = [command for request_id, command in port.frames if command[0] in ('READ', 'READX')]
            # Count actual bytes through the fixture in BOTH directions,
            # including negotiation/END and their replies. Radio envelopes,
            # RF ACKs and RF retransmissions are below this measured boundary.
            totals[profile] = (len(data_frames), port.bytes_written+port.bytes_received)
        self.assertEqual(totals['b64_512'][0]*4, totals['legacy'][0])
        self.assertLess(totals['b64_512'][1], totals['legacy'][1]*.7)
        print('Deterministic 16 KiB wire model (data requests, payload-stream bytes):', totals)

    def test_api_legacy_and_modern_frames_with_rssi_and_fast_transfers(self):
        from test_xbee_api import Radio
        from kestrelsat.xbee_api import ApiSerial
        for receive_format in ('legacy64', 'legacy16', 'modern'):
            for profile in ('b64_512', 'batch2'):
                with self.subTest(receive_format=receive_format, profile=profile):
                    peer = SpeedSatellitePort(); peer.content = bytes(range(256))*13
                    raw = Radio(peer); raw.baudrate = 115200; raw.receive_format = receive_format
                    port = ApiSerial(raw).initialize()
                    try:
                        port.set_rssi_polling(True)
                        client = rf.Client(port, timeout=.5, profile=profile)
                        source = self.root/'api-source'; source.write_bytes(peer.content)
                        client.upload(source, '/api.bin')
                        self.assertEqual(peer.files['/api.bin'], peer.content)
                        client.download('/api.bin', self.root/'api-result')
                        self.assertEqual((self.root/'api-result').read_bytes(), peer.content)
                        self.assertEqual(client.counters['retries'], 0)
                        self.assertEqual(len(raw.readers), 1)
                    finally:
                        port.close()

    def test_actual_profile_is_preserved_in_session_recording_and_combined_csv(self):
        # Force both transfers into the same clock tick, as can happen on Windows.
        # Combined CSVs must preserve their supplied order even when times tie.
        recorder = Recorder(self.root/'stats', clock=lambda: 1000.0)
        recording = recorder.start_recording('profiles')
        sessions = []
        for profile in ('legacy', 'batch2'):
            session = recorder.start('KFS1', 'download', profile, 115200, 'API')
            sessions.append(session)
            client = rf.Client(SpeedSatellitePort(), profile=profile, progress=session.progress)
            client.download('/data.bin', self.root/'csv-download')
            session.finish('completed')
            with session.csv_path.open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[-1]['transfer_profile'], client.transfer_profile.label)
        recorder.stop_recording()
        with recording.operations_path.open() as handle:
            operations = list(csv.DictReader(handle))
        self.assertEqual([op['transfer_profile'] for op in operations], ['128-HEX-batch1', '512-B64-batch2'])
        combined = recorder.combine([*sessions, sessions[0]], 'combined')
        with combined.operations_path.open() as handle:
            self.assertEqual([op['transfer_profile'] for op in csv.DictReader(handle)],
                             ['128-HEX-batch1', '512-B64-batch2'])


if __name__ == '__main__':
    unittest.main()
