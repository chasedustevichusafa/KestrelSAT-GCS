"""Portable XBee AP=1 framing, AT multiplexing, RSSI and KFS integration tests."""
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import serial
from kestrelsat.xbee_api import ApiSerial, MeteredSerial, Parser, frame
from kestrelsat.remote_files import Client
from test_remote_files import SatellitePort

DESTINATION = 0x0013a20040b90001


class Radio:
    def __init__(self, peer=None):
        self.timeout, self.write_timeout = 0.05, 1
        self.baudrate = 9600
        self.delay = 0
        self.condition = threading.Condition()
        self.rx = bytearray()
        self.parser = Parser()
        self.closed = False
        self.commands = []
        self.transmitted = bytearray()
        self.readers = set()
        self.peer = peer
        self.peer_buffer = bytearray()
        self.delivery_status = 0
        self.db_supported = True
        self.drop_tx_status = False

    @property
    def in_waiting(self):
        with self.condition:
            return len(self.rx)

    def enqueue(self, packet):
        with self.condition:
            self.rx.extend(frame(packet))
            self.condition.notify_all()

    def rf(self, data, explicit=False, source=DESTINATION):
        header = b'\x91' + source.to_bytes(8,'big') + b'\xff\xfe\xe8\xe8\x00\x11\xc1\x05\x01' if explicit else (
            b'\x90' + source.to_bytes(8,'big') + b'\xff\xfe\x01')
        self.enqueue(header+data)

    def write(self, data):
        if self.closed:
            raise serial.SerialException('closed')
        for packet in self.parser.feed(data):
            if packet[0] == 8:
                command = packet[2:4]
                self.commands.append((command, time.monotonic()))
                replies = {b'AP': b'\x01', b'DH': (DESTINATION >> 32).to_bytes(4,'big'),
                           b'DL': (DESTINATION & 0xffffffff).to_bytes(4,'big'), b'NP': b'\x40', b'DB': b'\x37'}
                status = 1 if command == b'DB' and not self.db_supported else 0
                # An unrelated AT reply with the same frame ID must not satisfy the query.
                self.enqueue(bytes([0x88,packet[1]])+b'ZZ\x00noise')
                self.enqueue(bytes([0x88,packet[1]])+command+bytes([status])+replies.get(command,b''))
            elif packet[0] == 0x10:
                if self.delay:
                    time.sleep(self.delay)
                self.assert_destination(packet)
                payload = packet[14:]
                self.transmitted.extend(payload)
                if not self.drop_tx_status:
                    self.enqueue(bytes([0x8b,packet[1],0xff,0xfe,1,self.delivery_status,0]))
                if self.peer:
                    self.peer_buffer.extend(payload)
                    while b'\n' in self.peer_buffer:
                        line, _, rest = self.peer_buffer.partition(b'\n')
                        self.peer_buffer[:] = rest
                        if not line:
                            continue
                        self.peer.write(line+b'\n')
                        answer = bytes(self.peer.rx)
                        self.peer.rx.clear()
                        for offset in range(0,len(answer),32):
                            self.rf(answer[offset:offset+32])
        return len(data)

    def assert_destination(self, packet):
        assert int.from_bytes(packet[2:10],'big') == DESTINATION
        assert len(packet[14:]) <= 64

    def read(self, n=1):
        self.readers.add(threading.get_ident())
        with self.condition:
            if not self.rx and not self.closed:
                self.condition.wait(self.timeout)
            data = bytes(self.rx[:min(n,11)])
            del self.rx[:len(data)]
            return data

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()

    def flush(self):
        pass


class ApiTests(unittest.TestCase):
    def connect(self, peer=None):
        raw = Radio(peer)
        port = ApiSerial(raw).initialize()
        self.addCleanup(port.close)
        return raw, port

    def wait(self, condition, timeout=2):
        end = time.monotonic()+timeout
        while not condition() and time.monotonic() < end:
            time.sleep(0.01)
        self.assertTrue(condition())

    def test_known_frame_fragmentation_and_corruption(self):
        self.assertEqual(frame(bytes.fromhex('08014442')), bytes.fromhex('7e00040801444270'))
        parser = Parser()
        payload = b'\x90'+bytes(range(64))+b'\x7e\x11\x13'
        packet = frame(payload)
        found = []
        for byte in packet:
            found.extend(parser.feed(bytes([byte])))
        self.assertEqual(found,[payload])
        bad = bytearray(packet)
        bad[-1] ^= 1
        self.assertEqual(parser.feed(b'junk\x7e\xff\xff'+bytes(bad)+packet),[payload])
        parser.feed(b'\x7e\x01',now=1)
        self.assertEqual(parser.feed(frame(b'\x8a\x00'),now=4),[b'\x8a\x00'])

    def test_receive_filters_and_api_responses_do_not_leak(self):
        raw, port = self.connect()
        raw.rf(b'wrong-peer',source=DESTINATION+1)
        raw.rf(b'hello\x00\x7e')
        raw.rf(b'world',explicit=True)
        self.wait(lambda: port.in_waiting >= 12)
        self.assertEqual(port.read(12),b'hello\x00\x7eworld')
        self.assertEqual(len(raw.readers),1)
        self.assertEqual(port.in_waiting,0)

    def test_transmit_chunking_and_delivery_errors(self):
        raw, port = self.connect()
        payload = bytes(range(256))*2
        self.assertEqual(port.write(payload),len(payload))
        self.assertEqual(raw.transmitted,payload)
        self.assertEqual(port.traffic_snapshot()['radio_retries'],8)
        raw.delivery_status = 0x24
        with self.assertRaisesRegex(serial.SerialException,'delivery failed'):
            port.write(b'x')

    def test_status_timeout_and_close_wake_pending_reader(self):
        raw, port = self.connect()
        raw.drop_tx_status = True
        port.write_timeout = 0.05
        with self.assertRaises(serial.SerialTimeoutException):
            port.write(b'x')
        port.timeout = None
        result = []
        def read():
            try:
                port.read(1)
            except serial.SerialException:
                result.append('closed')
        worker = threading.Thread(target=read)
        worker.start()
        port.close()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result,['closed'])
        self.assertFalse(port._reader.is_alive())
        self.assertFalse(port._poller.is_alive())

    def test_transfer_cancel_interrupts_transmit_status_wait(self):
        raw, port = self.connect()
        raw.drop_tx_status = True
        cancel = threading.Event()
        port.set_transfer_cancel(cancel)
        result = []
        def write():
            try:
                port.write(b'waiting')
            except serial.SerialException as exc:
                result.append(str(exc))
        worker = threading.Thread(target=write)
        worker.start()
        self.wait(lambda: bool(raw.transmitted))
        cancel.set()
        worker.join(timeout=0.3)
        self.assertFalse(worker.is_alive())
        self.assertIn('cancelled',result[0])
        raw.drop_tx_status = False
        port.set_transfer_cancel(None)
        self.assertEqual(port.write(b'next'),4)

    def test_rssi_polls_only_after_new_packets_and_can_be_disabled(self):
        raw, port = self.connect()
        port.set_rssi_polling(True)
        raw.rf(b'first')
        self.wait(lambda: port.traffic_snapshot()['rssi'] == -55)
        first = [at for cmd,at in raw.commands if cmd == b'DB']
        self.assertEqual(len(first),1)
        time.sleep(1.1)
        self.assertEqual(len([cmd for cmd,_ in raw.commands if cmd == b'DB']),1)
        raw.rf(b'next')
        self.wait(lambda: len([cmd for cmd,_ in raw.commands if cmd == b'DB'])==2)
        times = [at for cmd,at in raw.commands if cmd == b'DB']
        self.assertGreaterEqual(times[1]-times[0],1)
        port.set_rssi_polling(False)
        raw.rf(b'not-polled')
        time.sleep(0.1)
        self.assertEqual(len([cmd for cmd,_ in raw.commands if cmd == b'DB']),2)
        self.assertEqual(port.read(100),b'firstnextnot-polled')

    def test_unsupported_rssi_does_not_break_file_transfer(self):
        peer = SatellitePort()
        raw, port = self.connect(peer)
        raw.db_supported = False
        port.set_rssi_polling(True)
        client = Client(port,timeout=1)
        self.assertEqual(len(client.list_directory('/')),2)
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp)/'binary.bin'
            client.download('/data.bin',destination)
            self.assertEqual(destination.read_bytes(),peer.content)
        self.wait(lambda: any(cmd == b'DB' for cmd,_ in raw.commands))
        self.assertIsNone(port.traffic_snapshot()['rssi'])
        self.assertEqual(port.in_waiting,0)


if __name__ == '__main__':
    unittest.main()
