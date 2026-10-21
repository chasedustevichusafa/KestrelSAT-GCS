"""XBee AP=1 transport: one raw reader, demultiplexed data/AT/status frames.

The satellite radio may remain transparent. No +++ command mode or radio writes
are used here. Local AP/DH/DL/NP queries use API AT frames only.
"""
import threading
import time
import serial

MAX_FRAME = 2048
MAX_RX = 1 << 20


def frame(data):
    return b'\x7e' + len(data).to_bytes(2, 'big') + data + bytes([0xff-(sum(data) & 0xff)])


class Parser:
    def __init__(self):
        self.buffer = bytearray()
        self.last_byte = 0

    def feed(self, data, now=None):
        now = time.monotonic() if now is None else now
        if self.buffer and now-self.last_byte > 2:
            self.buffer.clear()
        self.last_byte = now
        self.buffer.extend(data)
        result = []
        while self.buffer:
            try:
                start = self.buffer.index(0x7e)
            except ValueError:
                self.buffer.clear()
                break
            del self.buffer[:start]
            if len(self.buffer) < 3:
                break
            length = int.from_bytes(self.buffer[1:3], 'big')
            if not 1 <= length <= MAX_FRAME:
                del self.buffer[0]
                continue
            if len(self.buffer) < length+4:
                break
            if sum(self.buffer[3:length+4]) & 0xff != 0xff:
                del self.buffer[0]
                continue
            result.append(bytes(self.buffer[3:length+3]))
            del self.buffer[:length+4]
        return result


class MeteredSerial:
    """Transparent serial port with counters used by transfer statistics."""
    api_mode = False

    def __init__(self, port):
        self.port = port
        self._counts_lock = threading.Lock()
        self._rx = self._tx = 0

    def read(self, n=1):
        data = self.port.read(n)
        with self._counts_lock:
            self._rx += len(data)
        return data

    def write(self, data):
        n = self.port.write(data)
        with self._counts_lock:
            self._tx += n
        return n

    def traffic_snapshot(self):
        with self._counts_lock:
            return dict(rx=self._rx, tx=self._tx, rssi=None, rssi_at=None,
                        packet_at=None, radio_retries=None, rssi_error='Transparent mode: RSSI unavailable')

    def __getattr__(self, name):
        return getattr(self.port, name)

    @property
    def timeout(self):
        return self.port.timeout

    @timeout.setter
    def timeout(self, value):
        self.port.timeout = value

    @property
    def write_timeout(self):
        return self.port.write_timeout

    @write_timeout.setter
    def write_timeout(self, value):
        self.port.write_timeout = value


class ApiSerial:
    api_mode = True

    def __init__(self, port):
        self.port = port
        self.timeout = 0.1
        self.write_timeout = 5.0
        self._condition = threading.Condition(threading.RLock())
        self._write_lock = threading.Lock()
        self._data_write_lock = threading.Lock()
        self._closed = threading.Event()
        self._poll_enabled = threading.Event()
        self._pending = {}
        self._retired = {}
        self._next_id = 0
        self._payload = bytearray()
        self._error = None
        self._rx = self._tx = self._radio_retries = 0
        self._packet_at = self._rssi_at = self._rssi = None
        self._packet_number = 0
        self._frame_counts = {}
        self._filtered_packets = 0
        self._last_delivery = None
        self._rssi_error = 'No RSSI reading yet'
        self._transfer_cancel = None
        self.destination = None
        self.chunk_size = 64
        self.port.timeout = 0.05
        self.port.write_timeout = 1.0
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._poller = None

    def initialize(self):
        try:
            if self.at('AP') != b'\x01':
                raise serial.SerialException('Select AP=1 (API without escapes) in XCTU for the ground XBee.')
            dh, dl = self.at('DH'), self.at('DL')
            if not 1 <= len(dh) <= 4 or not 1 <= len(dl) <= 4:
                raise serial.SerialException('Invalid XBee destination address')
            self.destination = (int.from_bytes(dh, 'big') << 32) | int.from_bytes(dl, 'big')
            try:
                maximum = int.from_bytes(self.at('NP'), 'big')
            except serial.SerialException:
                maximum = 64
            if maximum < 1:
                raise serial.SerialException('XBee NP reports no available payload capacity')
            self.chunk_size = min(maximum, 256)
            self._poller = threading.Thread(target=self._poll_loop, daemon=True)
            self._poller.start()
        except Exception:
            self.close()
            raise
        return self

    def _raise_if_closed(self):
        if self._error:
            raise serial.SerialException(str(self._error))
        if self._closed.is_set():
            raise serial.SerialException('XBee connection closed')

    def set_transfer_cancel(self, event):
        self._transfer_cancel = event
        with self._condition:
            self._condition.notify_all()

    def _check_transfer_cancel(self, kind):
        if kind == 0x10 and self._transfer_cancel is not None and self._transfer_cancel.is_set():
            raise serial.SerialException('File transfer cancelled')

    def _request(self, kind, body, expected, timeout=1.5, command=None):
        with self._condition:
            self._raise_if_closed()
            now = time.monotonic()
            self._retired = {key: expiry for key, expiry in self._retired.items() if expiry > now}
            for _ in range(255):
                self._next_id = self._next_id % 255 + 1
                if self._next_id not in self._pending and self._next_id not in self._retired:
                    break
            else:
                raise serial.SerialException('Too many outstanding XBee requests')
            request_id = self._next_id
            pending = dict(expected=expected, command=command, response=None)
            self._pending[request_id] = pending
        try:
            packet = frame(bytes([kind, request_id])+body)
            with self._write_lock:
                self._raise_if_closed()
                self._check_transfer_cancel(kind)
                n = self.port.write(packet)
                with self._condition:
                    self._tx += n
                if n != len(packet):
                    raise serial.SerialTimeoutException('Incomplete XBee API frame write')
            deadline = time.monotonic()+timeout
            with self._condition:
                while pending['response'] is None:
                    self._raise_if_closed()
                    self._check_transfer_cancel(kind)
                    remaining = deadline-time.monotonic()
                    if remaining <= 0:
                        raise serial.SerialTimeoutException('XBee API reply timed out; verify AP=1 and radio pairing')
                    self._condition.wait(min(remaining, 0.05))
                return pending['response']
        except Exception:
            with self._condition:
                # Do not let a late timed-out reply satisfy a reused frame ID.
                self._retired[request_id] = time.monotonic()+30
            raise
        finally:
            with self._condition:
                self._pending.pop(request_id, None)

    def at(self, command, timeout=1.5):
        response = self._request(0x08, command.encode('ascii'), 0x88, timeout, command.encode('ascii'))
        if response[4] != 0:
            raise serial.SerialException(f'XBee AT{command} failed (status {response[4]})')
        return response[5:]

    def _dispatch(self, packet):
        with self._condition:
            kind = packet[0]
            self._frame_counts[kind] = self._frame_counts.get(kind, 0)+1
            # The course's XB3-24A 2014 profile defaults to AO=2, which
            # outputs legacy 802.15.4 receive frames even with AP=1.
            header = {0x80: 11, 0x81: 5, 0x90: 12, 0x91: 18}.get(kind)
            if header is not None and len(packet) >= header:
                if kind == 0x81:
                    source = int.from_bytes(packet[1:3], 'big')
                    # A short-address frame contains no source serial number.
                    # Filter configured short unicasts; 64-bit destinations
                    # cannot be compared to these 16-bit source addresses.
                    wrong_peer = (self.destination is not None and
                                  0 < self.destination < 0xffff and
                                  source != self.destination)
                else:
                    source = int.from_bytes(packet[1:9], 'big')
                    wrong_peer = (self.destination is not None and
                                  self.destination > 0xffff and
                                  source != self.destination)
                if wrong_peer:
                    self._filtered_packets += 1
                    return
                data = packet[header:]
                if len(self._payload)+len(data) > MAX_RX:
                    raise serial.SerialException('XBee payload queue overflow; transfer stopped')
                self._payload.extend(data)
                self._packet_at = time.monotonic()
                self._packet_number += 1
            elif kind in (0x88, 0x8b) and len(packet) >= (5 if kind == 0x88 else 7):
                pending = self._pending.get(packet[1])
                if pending and pending['expected'] == kind:
                    if kind != 0x88 or packet[2:4] == pending['command']:
                        pending['response'] = packet
                        if kind == 0x8b:
                            self._radio_retries += packet[4]
                            self._last_delivery = packet[5]
            self._condition.notify_all()

    def _read_loop(self):
        parser = Parser()
        try:
            while not self._closed.is_set():
                data = self.port.read(min(max(self.port.in_waiting, 1), MAX_FRAME))
                if not data:
                    continue
                with self._condition:
                    self._rx += len(data)
                for packet in parser.feed(data):
                    self._dispatch(packet)
        except Exception as exc:
            if not self._closed.is_set():
                with self._condition:
                    self._error = exc
                    self._condition.notify_all()

    def _poll_loop(self):
        sampled = -1
        next_poll = 0
        while not self._closed.wait(0.05):
            now = time.monotonic()
            with self._condition:
                packet_number = self._packet_number
            if not self._poll_enabled.is_set() or not packet_number or packet_number == sampled or now < next_poll:
                continue
            next_poll = now+1
            try:
                value = self.at('DB', timeout=0.7)
                if len(value) != 1 or not 1 <= value[0] <= 127:
                    raise serial.SerialException('XBee DB reading unavailable')
                with self._condition:
                    self._rssi, self._rssi_at = -value[0], time.monotonic()
                    self._rssi_error = ''
                sampled = packet_number
            except serial.SerialException as exc:
                with self._condition:
                    self._rssi_error = str(exc)

    def set_rssi_polling(self, enabled):
        if enabled:
            self._poll_enabled.set()
        else:
            self._poll_enabled.clear()

    def traffic_snapshot(self):
        with self._condition:
            return dict(rx=self._rx, tx=self._tx, rssi=self._rssi, rssi_at=self._rssi_at,
                        packet_at=self._packet_at, radio_retries=self._radio_retries, rssi_error=self._rssi_error)

    def diagnostic_summary(self):
        """Bounded metadata for a failed request; never includes file payloads."""
        with self._condition:
            frames = ', '.join(f'0x{kind:02x}:{count}'
                               for kind, count in sorted(self._frame_counts.items())) or 'none'
            destination = f'{self.destination:016x}' if self.destination is not None else 'unset'
            delivery = 'none' if self._last_delivery is None else f'0x{self._last_delivery:02x}'
            return (f'XBee API: destination={destination}; delivery={delivery}; '
                    f'RF packets={self._packet_number}; filtered={self._filtered_packets}; '
                    f'frames=[{frames}]')

    @property
    def baudrate(self):
        return self.port.baudrate

    @property
    def in_waiting(self):
        with self._condition:
            self._raise_if_closed()
            return len(self._payload)

    def read(self, n=1):
        deadline = None if self.timeout is None else time.monotonic()+self.timeout
        with self._condition:
            while len(self._payload) < n:
                self._raise_if_closed()
                if deadline is not None:
                    remaining = deadline-time.monotonic()
                    if remaining <= 0:
                        break
                else:
                    remaining = 0.1
                self._condition.wait(min(remaining, 0.1))
            result = bytes(self._payload[:n])
            del self._payload[:len(result)]
            return result

    def write(self, data):
        if self.destination is None:
            raise serial.SerialException('XBee API transport has not been initialized')
        timeout = self.write_timeout if self.write_timeout is not None else 10
        deadline = time.monotonic()+timeout
        with self._data_write_lock:
            for offset in range(0, len(data), self.chunk_size):
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise serial.SerialTimeoutException('XBee transmission timed out')
                payload = data[offset:offset+self.chunk_size]
                body = self.destination.to_bytes(8, 'big') + b'\xff\xfe\x00\x00' + payload
                status = self._request(0x10, body, 0x8b, min(remaining, 3))
                if status[5] != 0:
                    raise serial.SerialException(f'XBee delivery failed (status 0x{status[5]:02x})')
        return len(data)

    def flush(self):
        self.port.flush()

    def close(self):
        self._closed.set()
        self._poll_enabled.clear()
        with self._condition:
            self._condition.notify_all()
        self.port.close()
        for thread in (self._reader, self._poller):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=1)
