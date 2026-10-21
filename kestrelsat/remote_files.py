"""KFS1 read-only SD browser/download client. No GUI or platform dependencies.

The same framed protocol is implemented in Lab 8's file_service.h. Serial reads
must be exclusively owned by this client for the duration of an operation.
"""
from dataclasses import dataclass
from pathlib import Path
import os
import secrets
import tempfile
import threading
import time
import zlib

MAX_LINE = 768
MAX_PATH = 192
CHUNK = 128
MAX_ENTRIES = 4096


class RemoteError(Exception):
    pass


class Cancelled(RemoteError):
    pass


@dataclass(frozen=True)
class Entry:
    name: str
    directory: bool
    size: int


def encode(request_id, *fields):
    body = '|'.join(('@KFS1', f'{request_id:08x}', *map(str, fields))).encode('ascii')
    frame = body + f'|{zlib.crc32(body):08x}\n'.encode('ascii')
    if len(frame) >= MAX_LINE:
        raise RemoteError('Protocol frame is too long')
    return frame


def decode(line):
    try:
        body, checksum = line.rstrip(b'\r\n').rsplit(b'|', 1)
        if len(line) >= MAX_LINE or len(checksum) != 8 or zlib.crc32(body) != int(checksum, 16):
            return None
        fields = body.decode('ascii').split('|')
        if fields[0] != '@KFS1' or len(fields) < 3 or len(fields[1]) != 8:
            return None
        return int(fields[1], 16), fields[2:]
    except (ValueError, UnicodeError):
        return None


def path_hex(path):
    if not path.startswith('/') or '\\' in path or any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise RemoteError('Use an absolute SD-card path')
    if path != '/' and any(p in ('', '.', '..') for p in path[1:].split('/')):
        raise RemoteError('Use a canonical SD-card path without dot components')
    raw = path.encode('utf-8')
    if len(raw) > MAX_PATH:
        raise RemoteError(f'SD path exceeds {MAX_PATH} bytes')
    return raw.hex()


def unsigned(text, maximum=0xffffffff):
    if not text.isascii() or not text.isdigit() or len(text) > 10:
        raise RemoteError('Invalid number in satellite response')
    value = int(text)
    if value > maximum:
        raise RemoteError('Satellite response exceeds the protocol limit')
    return value


class Client:
    def __init__(self, port, cancel=None, progress=None, timeout=4.0, attempts=4):
        self.port = port
        self.cancel = cancel or threading.Event()
        self.progress = progress or (lambda **info: None)
        self.timeout = timeout
        self.attempts = attempts
        self._id = secrets.randbits(32)
        self._buffer = bytearray()
        self._discard = False

    def _check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled('Operation cancelled')

    def request(self, op, *args, expected=None):
        self._id = (self._id + 1) & 0xffffffff
        request_id = self._id
        frame = encode(request_id, op, *args)
        for attempt in range(self.attempts):
            self._check_cancel()
            if attempt:
                self.progress(status=f'No valid reply; retry {attempt}/{self.attempts-1}', retry=1)
            # Prepending newline also terminates any truncated previous request.
            outgoing = b'\n' + frame
            if self.port.write(outgoing) != len(outgoing):
                raise RemoteError('Incomplete serial write')
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                self._check_cancel()
                data = self.port.read(min(max(self.port.in_waiting, 1), MAX_LINE))
                if not data:
                    continue
                for byte in data:
                    if byte == 10:
                        reply = None if self._discard else decode(bytes(self._buffer))
                        self._buffer.clear()
                        self._discard = False
                        if reply is None or reply[0] != request_id:
                            continue
                        fields = reply[1]
                        if fields[0] == 'ERR':
                            raise RemoteError('Satellite: ' + ' '.join(fields[1:]))
                        if expected and fields[0] not in expected:
                            raise RemoteError(f'Unexpected satellite response: {fields[0]}')
                        return fields
                    elif not self._discard:
                        if len(self._buffer) < MAX_LINE:
                            self._buffer.append(byte)
                        else:
                            self._buffer.clear()
                            self._discard = True
        detail = 'Satellite did not reply. Check Lab 8 firmware, SD card, radio link and baud rate.'
        diagnostic = getattr(self.port, 'diagnostic_summary', None)
        if callable(diagnostic):
            detail += ' ' + diagnostic()
        raise RemoteError(detail)

    def hello(self):
        reply = self.request('HELLO', expected={'HELLO'})
        if reply != ['HELLO', '1', str(CHUNK)]:
            raise RemoteError('Unsupported satellite file protocol version')

    def list_directory(self, path):
        encoded = path_hex(path)
        self.hello()
        entries = []
        names = set()
        for index in range(MAX_ENTRIES + 1):
            reply = self.request('LIST', encoded, index, expected={'ENTRY', 'END'})
            if reply == ['END']:
                return entries
            if len(reply) != 5 or unsigned(reply[1], MAX_ENTRIES) != index + 1 or reply[2] not in ('D', 'F'):
                raise RemoteError('Invalid directory entry')
            if index == MAX_ENTRIES:
                raise RemoteError(f'Directory exceeds {MAX_ENTRIES} entries')
            try:
                name = bytes.fromhex(reply[4]).decode('utf-8')
            except (ValueError, UnicodeError) as exc:
                raise RemoteError('Unsupported filename encoding') from exc
            # Remote names are data, never local paths or executable commands.
            if not name or name in ('.', '..') or '/' in name or '\\' in name or name in names:
                raise RemoteError('Invalid or duplicate filename from satellite')
            path_hex(('/' if path == '/' else path + '/') + name)
            names.add(name)
            entries.append(Entry(name, reply[2] == 'D', unsigned(reply[3])))
            self.progress(status=f'Reading directory: {len(entries)} entries')
        raise RemoteError('Directory is too large')

    def download(self, path, destination):
        encoded = path_hex(path)
        self.hello()
        token = None
        temporary = None
        started = time.monotonic()
        try:
            reply = self.request('OPEN', encoded, expected={'FILE'})
            if len(reply) != 3 or len(reply[1]) != 8:
                raise RemoteError('Invalid file metadata')
            try:
                int(reply[1], 16)
            except ValueError as exc:
                raise RemoteError('Invalid file session') from exc
            token, total = reply[1], unsigned(reply[2])
            destination = Path(destination)
            with tempfile.NamedTemporaryFile(prefix='.kestrelsat-', suffix='.part',
                                             dir=destination.parent, delete=False) as output:
                temporary = Path(output.name)
                done, crc = 0, 0
                self.progress(done=done, total=total, elapsed=0)
                while done < total:
                    self._check_cancel()
                    reply = self.request('READ', token, done, expected={'DATA'})
                    if len(reply) != 3 or unsigned(reply[1]) != done:
                        raise RemoteError('Incorrect file offset')
                    try:
                        block = bytes.fromhex(reply[2])
                    except ValueError as exc:
                        raise RemoteError('Invalid file data') from exc
                    if len(block) != min(CHUNK, total - done):
                        raise RemoteError('Incorrect file chunk length')
                    output.write(block)
                    crc = zlib.crc32(block, crc)
                    done += len(block)
                    self.progress(done=done, total=total, elapsed=time.monotonic()-started)
                reply = self.request('END', token, expected={'DONE'})
                if reply != ['DONE', str(total), f'{crc:08x}']:
                    raise RemoteError('Whole-file checksum failed; download was not saved')
                token = None
                self._check_cancel()
                output.flush()
                os.fsync(output.fileno())
            # The caller chooses the exact local destination and confirms overwrite.
            os.replace(temporary, destination)
            temporary = None
            return destination
        finally:
            if token is not None:
                # Best effort close even after Cancel. Firmware also expires the
                # session after 30 seconds if the connection has disappeared.
                try:
                    saved_cancel, saved_timeout, saved_attempts = self.cancel, self.timeout, self.attempts
                    self.cancel, self.timeout, self.attempts = threading.Event(), 0.5, 1
                    try:
                        self.request('CLOSE', token, expected={'CLOSED'})
                    finally:
                        self.cancel, self.timeout, self.attempts = saved_cancel, saved_timeout, saved_attempts
                except Exception:
                    pass
            if temporary is not None:
                temporary.unlink(missing_ok=True)
