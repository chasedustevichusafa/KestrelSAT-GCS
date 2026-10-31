"""KFS1 SD browser and verified upload/download client. No GUI or platform dependencies.

The same framed protocol is implemented in Lab 8's file_service.h. Serial reads
must be exclusively owned by this client for the duration of an operation.
"""

# READING GUIDE - ground half of the KFS1 file protocol.
# The satellite implements the same contract in Lab 8 include/file_service.h.
# This module has no widgets: a tab worker grants it exclusive payload-stream
# ownership, then reports dictionaries through progress/diagnostic callbacks.
#
# There are two framing layers in API mode. xbee_api.py consumes raw radio API
# frames and exposes their RF payload bytes as a serial-like stream. encode/decode
# here frame KFS commands INSIDE that stream. Their CRC is independent of the
# XBee checksum, and the whole-file CRC is a third check across all file chunks.
# None of these checks is encryption or sender authentication.
#
# Legacy peers still move 128-byte hex chunks. FAST1 peers negotiate a per-file
# chunk/codec using OPENX/PUTX; HELLO stays unchanged so old clients work with new
# firmware. Base64 reduces data expansion from 2x to about 4/3x. Optional batches
# contain TWO data requests, never a full file or an unbounded sliding window.
# A batch drains before the next begins, matching the firmware's two-entry replay
# cache. This bounds loss recovery and avoids treating a lost reply as lost data.
import base64
import binascii
from dataclasses import dataclass
from pathlib import Path
import os
import re
import secrets
import tempfile
import threading
import time
import zlib

MAX_LINE = 1024
MAX_PATH = 192
CHUNK = 128
MAX_ENTRIES = 4096


@dataclass(frozen=True)
class TransferProfile:
    chunk: int
    codec: str
    batch: int = 1

    @property
    def label(self):
        return f'{self.chunk}-{self.codec}-batch{self.batch}'


# The UI captures this choice before starting a worker. Auto selects the larger
# compact mode only after explicit FAST1 capability discovery; old peers receive
# only legacy commands. Explicit experiments fail clearly on old firmware.
PROFILE_LABELS = {
    'auto': 'Auto (512 bytes, Base64)',
    'legacy': 'Baseline (128 bytes, hex)',
    'hex256': 'Larger blocks (256 bytes, hex)',
    'b64_256': 'Compact (256 bytes, Base64)',
    'b64_512': 'Fast (512 bytes, Base64)',
    'batch2': 'Experimental (512 bytes, Base64, batch 2)',
}
PROFILES = {
    'legacy': TransferProfile(128, 'HEX'),
    'hex256': TransferProfile(256, 'HEX'),
    'b64_256': TransferProfile(256, 'B64'),
    'b64_512': TransferProfile(512, 'B64'),
    'batch2': TransferProfile(512, 'B64', 2),
}


class RemoteError(Exception):
    pass


class OperationUncertain(RemoteError):
    pass


class UploadUncertain(OperationUncertain):
    pass


class Cancelled(RemoteError):
    pass


@dataclass(frozen=True)
class Entry:
    name: str
    directory: bool
    size: int


# The outer CRC includes the prefix, request ID and all fields. It excludes
# its own separator/checksum and the terminating newline. Construct once per
# request so retransmissions have exactly the same identity and payload.
def encode(request_id, *fields):
    body = '|'.join(('@KFS1', f'{request_id:08x}', *map(str, fields))).encode('ascii')
    frame = body + f'|{zlib.crc32(body):08x}\n'.encode('ascii')
    if len(frame) >= MAX_LINE:
        raise RemoteError('Protocol frame is too long')
    return frame


# Invalid input returns None so the caller can discard noise/corruption and
# continue scanning. A syntactically valid ERR response is different: it is a
# real satellite result and must not be treated as a missing reply.
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


# Validate the SD path before encoding its UTF-8 bytes. UTF-8 byte length,
# not Python character count, is the capacity shared with the C++ receiver.
# The chosen local download path is handled separately by the UI.
def path_hex(path):
    if not path.startswith('/') or '\\' in path or any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise RemoteError('Use an absolute SD-card path')
    if path != '/' and any(p in ('', '.', '..') for p in path[1:].split('/')):
        raise RemoteError('Use a canonical SD-card path without dot components')
    raw = path.encode('utf-8')
    if len(raw) > MAX_PATH:
        raise RemoteError(f'SD path exceeds {MAX_PATH} bytes')
    return raw.hex()


def partial_path(path):
    return bool(re.fullmatch(r'\.kfs-[0-9a-f]{8}\.part', path.rsplit('/', 1)[-1]))


def upload_path_hex(path, temporary=True):
    encoded = path_hex(path)
    name = path.rsplit('/', 1)[-1]
    if (not name or name.casefold().startswith('.kfs-') or any(c in path for c in '<>:"|?*') or
            any(part.endswith(('.', ' ')) for part in path[1:].split('/')) or
            temporary and len(path[:len(path)-len(name)].encode('utf-8'))+18 > MAX_PATH):
        raise RemoteError('Choose an SD filename without reserved characters or a .kfs- prefix')
    return encoded


def unsigned(text, maximum=0xffffffff):
    if not text.isascii() or not text.isdigit() or len(text) > 10:
        raise RemoteError('Invalid number in satellite response')
    value = int(text)
    if value > maximum:
        raise RemoteError('Satellite response exceeds the protocol limit')
    return value


class Client:
    def __init__(self, port, cancel=None, progress=None, timeout=4.0, attempts=4, diagnostic=None, profile='auto'):
        if profile not in PROFILE_LABELS:
            raise ValueError('Unknown transfer profile')
        self.requested_profile = profile
        self.transfer_profile = PROFILES['legacy']
        self.fast = False
        self.port = port
        self.cancel = cancel or threading.Event()
        self.progress = progress or (lambda **info: None)
        self.timeout = timeout
        self.attempts = attempts
        self.diagnostic = diagnostic
        self.counters = dict(retries=0, timeouts=0, malformed=0, stale=0)
        self._attempt = 0
        self._id = secrets.randbits(32)
        self._buffer = bytearray()
        self._discard = False

    def _check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled('Operation cancelled')

    def _record(self, event, **details):
        if self.diagnostic is not None:
            self.diagnostic(event, **details)

    # Wrap a single logical request with diagnostic context. elapsed_s includes
    # all attempts for this request, not just the final successful attempt. The
    # counters aggregate this Client operation; satellite SD attempts are reported
    # inside specific error replies and are not counted as RF/protocol retries.
    def request(self, op, *args, expected=None):
        started = time.monotonic()
        context = dict(operation=op, request=f'{(self._id+1) & 0xffffffff:08x}')
        if op in ('READ', 'READX', 'WRITE', 'WRITEX', 'VERIFY') and len(args) > 1:
            context['offset'] = args[1]
        self._attempt = 0
        self._record('request', **context)
        try:
            result = self._request(op, *args, expected=expected)
        except Exception as exc:
            elapsed = time.monotonic()-started
            detail = (f'{exc} [operation={op}' +
                      (f' offset={context["offset"]}' if 'offset' in context else '') +
                      f' request={context["request"]} attempt={self._attempt}/{self.attempts}' +
                      f' elapsed={elapsed:.2f}s]')
            self._record('request_failed', **context, attempt=self._attempt,
                         elapsed_s=elapsed, error=detail, counters=dict(self.counters))
            if isinstance(exc, Cancelled):
                raise
            if isinstance(exc, RemoteError):
                raise RemoteError(detail) from exc
            raise
        self._record('reply', **context, attempt=self._attempt,
                     elapsed_s=time.monotonic()-started, counters=dict(self.counters))
        return result

    # Increment the request ID once OUTSIDE the attempt loop. Retrying with a
    # new ID could execute a successful-but-unacknowledged WRITE or capture again.
    # The leading newline terminates a previously truncated outgoing command.
    # A bounded receive buffer resynchronizes at newline and ignores stale IDs.
    # The timeout begins after port.write; in API mode that write also waits for
    # local radio TX statuses, so it is not a pure over-the-air RTT measurement.
    def _request(self, op, *args, expected=None):
        self._id = (self._id + 1) & 0xffffffff
        request_id = self._id
        frame = encode(request_id, op, *args)
        for attempt in range(self.attempts):
            self._attempt = attempt+1
            self._check_cancel()
            if attempt:
                self.counters['retries'] += 1
                self._record('retry', operation=op, request=f'{request_id:08x}',
                             attempt=attempt+1, offset=args[1] if op in ('READ', 'READX', 'WRITE', 'WRITEX', 'VERIFY') and len(args) > 1 else None)
                self.progress(status=f'No valid reply; retry {attempt}/{self.attempts-1}', retry=1)
            # Prepending newline also terminates any truncated previous request.
            outgoing = b'\n' + frame
            if self.port.write(outgoing) != len(outgoing):
                raise RemoteError('Incomplete serial write')
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                self._check_cancel()
                for reply in self._receive():
                    if reply[0] != request_id:
                        self.counters['stale'] += 1
                        continue
                    fields = reply[1]
                    self._validate_reply(fields, expected)
                    return fields
            self.counters['timeouts'] += 1
            self._record('timeout', operation=op, request=f'{request_id:08x}',
                         attempt=attempt+1, offset=args[1] if op in ('READ', 'READX', 'WRITE', 'WRITEX', 'VERIFY') and len(args) > 1 else None,
                         counters=dict(self.counters))
        detail = 'Satellite did not reply. Check Lab 8 firmware, SD card, radio link and baud rate.'
        diagnostic = getattr(self.port, 'diagnostic_summary', None)
        if callable(diagnostic):
            detail += ' ' + diagnostic()
        raise RemoteError(detail)

    def _receive(self):
        """Parse a complete read before returning, preserving any trailing partial line.

        One serial/API read can contain several replies. Returning from inside
        the byte loop would throw away a second reply or the start of its frame,
        making batched transfers fail intermittently even on a lossless wire.
        """
        data = self.port.read(min(max(self.port.in_waiting, 1), MAX_LINE))
        replies = []
        for byte in data:
            if byte == 10:
                reply = None if self._discard else decode(bytes(self._buffer))
                self._buffer.clear()
                self._discard = False
                if reply is None:
                    self.counters['malformed'] += 1
                else:
                    replies.append(reply)
            elif not self._discard:
                if len(self._buffer) < MAX_LINE:
                    self._buffer.append(byte)
                else:
                    self._buffer.clear()
                    self._discard = True
        return replies

    @staticmethod
    def _validate_reply(fields, expected):
        if fields[0] == 'ERR':
            if len(fields) == 6 and fields[1] in ('READ_FAILED', 'WRITE_FAILED', 'VERIFY_READ_FAILED'):
                raise RemoteError(f'Satellite: {fields[1]} offset={fields[2]} '
                    f'wanted={fields[3]} got={fields[4]} attempts={fields[5]}')
            raise RemoteError('Satellite: ' + ' '.join(fields[1:]))
        if expected and fields[0] not in expected:
            raise RemoteError(f'Unexpected satellite response: {fields[0]}')

    def _select_profile(self):
        """Negotiate once per file, never switch encodings mid-transfer.

        Auto can fall back before opening a file. A timeout, SD error or checksum
        failure during a transfer must remain an error, not restart silently in
        another mode and hide a reliability problem in the benchmark data.
        """
        requested = self.requested_profile
        self.fast = requested != 'legacy' and 'FAST1' in self.capabilities()
        if requested == 'legacy':
            self.hello()
        if not self.fast and requested not in ('auto', 'legacy'):
            raise RemoteError('This transfer profile needs FAST1 speed-test firmware; select Auto or Baseline')
        self.transfer_profile = PROFILES['b64_512' if self.fast and requested == 'auto' else
                                         requested if self.fast else 'legacy']
        actual = self.transfer_profile.label
        self._record('transfer_profile', requested=requested, transfer_profile=actual,
                     chunk=self.transfer_profile.chunk, codec=self.transfer_profile.codec,
                     batch=self.transfer_profile.batch, fallback=requested == 'auto' and not self.fast)
        self.progress(transfer_profile=actual, status='Transfer profile: '+actual)

    def _encode_block(self, block):
        return (base64.b64encode(block).decode('ascii') if self.transfer_profile.codec == 'B64'
                else block.hex())

    def _decode_block(self, text):
        try:
            if self.transfer_profile.codec == 'HEX':
                return bytes.fromhex(text)
            block = base64.b64decode(text, validate=True)
            if base64.b64encode(block).decode('ascii') != text:
                raise ValueError('Noncanonical Base64')
            return block
        except (ValueError, binascii.Error) as exc:
            raise RemoteError('Invalid file data') from exc

    def _data_requests(self, op, requests, expected):
        """Bounded two-request experiment with same-ID, ordered loss recovery.

        A batch completes entirely before the next is sent. Keep successes and
        retry only unconfirmed members. BAD_OFFSET on a later member means the
        earlier request did not arrive; it is retried after the missing member,
        not treated as SD damage. Firmware must not cache that transient error.
        Useful-byte progress is reported only after replies are validated by the
        upload/download loop. Radio TX status remains independent of this ACK.
        """
        if len(requests) == 1:
            return [self.request(op, *requests[0], expected={expected})]
        if not 1 < len(requests) <= 2 or not self.fast:
            raise RemoteError('Invalid transfer request batch')
        pending, results, order = {}, {}, []
        for args in requests:
            self._id = (self._id+1) & 0xffffffff
            order.append(self._id)
            pending[self._id] = (args, encode(self._id, op, *args))
        for attempt in range(1, self.attempts+1):
            self._check_cancel()
            for request_id, (args, frame) in pending.items():
                self._record('request' if attempt == 1 else 'retry', operation=op,
                             request=f'{request_id:08x}', offset=args[1], attempt=attempt, batch=2)
            if attempt > 1:
                self.counters['retries'] += len(pending)
                self.progress(retry=len(pending), status=f'Batch recovery: attempt {attempt}/{self.attempts}')
            # At most two 512-byte Base64 commands (< 1.6 KiB) fit the firmware's
            # 2 KiB UART ring. One API write preserves their ordering and shares
            # one bounded write deadline; it still checks every radio TX status.
            outgoing = b'\n'+b''.join(frame for args, frame in pending.values())
            if self.port.write(outgoing) != len(outgoing):
                raise RemoteError('Incomplete serial write')
            deadline = time.monotonic()+self.timeout
            deferred = set()
            while time.monotonic() < deadline:
                self._check_cancel()
                for request_id, fields in self._receive():
                    if request_id not in pending:
                        self.counters['stale'] += 1
                        continue
                    if fields == ['ERR', 'BAD_OFFSET']:
                        deferred.add(request_id)
                        continue
                    self._validate_reply(fields, {expected})
                    results[request_id] = fields
                    args, frame = pending.pop(request_id)
                    deferred.discard(request_id)
                    self._record('reply', operation=op, request=f'{request_id:08x}',
                                 offset=args[1], attempt=attempt, batch=2)
                if not pending:
                    return [results[request_id] for request_id in order]
                if deferred == set(pending):
                    break # All remaining requests got a recoverable future-offset reply.
            for request_id, (args, frame) in pending.items():
                if request_id not in deferred:
                    self.counters['timeouts'] += 1
                    self._record('timeout', operation=op, request=f'{request_id:08x}',
                                 offset=args[1], attempt=attempt, counters=dict(self.counters))
        offsets = ', '.join(str(args[1]) for args, frame in pending.values())
        raise RemoteError(f'Transfer batch did not complete: {op} offsets={offsets}; '
                          'try the non-batched Fast profile and inspect diagnostics')

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

    def capabilities(self):
        self.hello()
        try:
            reply = self.request('CAPS', expected={'CAPS'})
        except RemoteError as exc:
            if 'Satellite: BAD_COMMAND' in str(exc):
                return set()
            raise
        return set(reply[1:-1]) if len(reply) >= 3 and reply[-1] == str(CHUNK) else set()

    def supports_upload(self):
        return 'UPLOAD1' in self.capabilities()

    # A destructive operation may succeed even when its acknowledgment is lost.
    # Keep the original request identity and use retained RESULT metadata to
    # reconcile the outcome. An unconfirmed result is reported as uncertainty;
    # it must not be converted into a new operation that could affect another
    # file appearing under the old name after the first mutation.
    def _manage(self, op, *args, before_send=None):
        if 'MANAGE1' not in self.capabilities():
            raise RemoteError('Satellite firmware needs the delete/rename update')
        request_id = f'{(self._id+1) & 0xffffffff:08x}'
        expected = 'DELETED' if op == 'DELETE' else 'RENAMED'
        # Preview persists this exact mutation ID before DELETE can reach the
        # radio. A restart can query RESULT instead of blindly issuing a new
        # destructive command. A failed checkpoint must prevent the send.
        if before_send is not None:
            before_send(request_id)
        try:
            if self.request(op, *args, expected={expected}) != [expected]:
                raise RemoteError('Unexpected file operation reply')
        except Exception as exc:
            if isinstance(exc, RemoteError) and str(exc).startswith('Satellite:'):
                raise
            result = self._upload_cleanup('RESULT', request_id)
            if result == ['RESULT', op, expected]:
                return
            if result and result[:3] == ['RESULT', op, 'ERR']:
                raise RemoteError('Satellite: '+' '.join(result[3:])) from exc
            raise OperationUncertain(f'{op.title()} completion could not be confirmed. '
                'Refresh the folder before trying again. '+str(exc)) from exc

    def delete_file(self, path, before_send=None):
        self._manage('DELETE', upload_path_hex(path, temporary=False), before_send=before_send)

    def rename_file(self, path, target):
        encoded = upload_path_hex(path, temporary=False)
        renamed = upload_path_hex(target, temporary=False)
        if path.rsplit('/', 1)[0] != target.rsplit('/', 1)[0]:
            raise RemoteError('Rename keeps the file in the same folder')
        self._manage('RENAME', encoded, renamed)

    def require_upload(self):
        if not self.supports_upload():
            raise RemoteError('Satellite firmware needs the upload update; downloads remain available')

    def clean_partial(self, path):
        encoded = path_hex(path)
        if not partial_path(path):
            raise RemoteError('Select a .kfs-xxxxxxxx.part upload temporary file')
        self.require_upload()
        if self.request('CLEAN', encoded, expected={'REMOVED'}) != ['REMOVED']:
            raise RemoteError('Unexpected partial-file cleanup reply')

    # Cancellation must not cancel its own cleanup immediately. Temporarily use
    # a fresh Event and short retry/write budgets while retaining port ownership.
    # Restore every setting in finally so the caller's cancellation state survives.
    # Cleanup is best effort; unreachable hardware may retain a reserved partial.
    def _upload_cleanup(self, op, token):
        # Cleanup/status must still work after Cancel; keep it bounded and hold
        # the same exclusive port ownership as the transfer.
        old_cancel, old_timeout, old_attempts = self.cancel, self.timeout, self.attempts
        old_write_timeout = self.port.write_timeout
        fresh = threading.Event()
        self.cancel, self.timeout, self.attempts = fresh, 0.7, 2
        self.port.write_timeout = 0.7
        setter = getattr(self.port, 'set_transfer_cancel', None)
        if setter:
            setter(fresh)
        try:
            return self.request(op, token)
        except Exception:
            return None
        finally:
            self.cancel, self.timeout, self.attempts = old_cancel, old_timeout, old_attempts
            self.port.write_timeout = old_write_timeout
            if setter:
                setter(old_cancel)

    # Upload has three independent checks: hash the local source before sending,
    # hash bytes actually sent, then ask the satellite to reread its SD temporary.
    # WRITE acknowledgment advances useful-byte progress only after a complete
    # synced write. Recheck local file metadata/content to detect source changes.
    # COMMIT uncertainty is special: the final name may already exist. Recover
    # STATUS/ABORT with the known token rather than declaring failure and blindly
    # starting another upload into the same destination.
    def upload(self, source, path):
        encoded = upload_path_hex(path)
        self.require_upload()
        self._select_profile()
        chunk = self.transfer_profile.chunk
        token = None
        started = time.monotonic()
        completed = commit_started = False
        failure = None
        total = expected_crc = 0
        expected_stored = None
        try:
            with Path(source).open('rb') as local:
                source_stat = os.fstat(local.fileno())
                total = source_stat.st_size
                if total > 0xffffffff:
                    raise RemoteError('File exceeds the satellite 4 GiB minus one byte limit')
                self.progress(done=0, total=total, elapsed=0, status='Calculating file checksum...')
                remaining = total
                while remaining:
                    self._check_cancel()
                    block = local.read(min(65536, remaining))
                    if not block:
                        raise RemoteError('Local source changed while calculating checksum')
                    expected_crc = zlib.crc32(block, expected_crc)
                    remaining -= len(block)
                if local.read(1):
                    raise RemoteError('Local source changed while calculating checksum')
                local.seek(0)
                self._check_cancel()
                # PUT's request ID is also its session token. Knowing it before
                # the reply permits ABORT even if every READY reply is lost.
                token = f'{(self._id+1) & 0xffffffff:08x}'
                opening = ('PUTX' if self.fast else 'PUT')
                extra = (chunk, self.transfer_profile.codec) if self.fast else ()
                ready = self.request(opening, encoded, total, f'{expected_crc:08x}', *extra, expected={'READY'})
                metadata = ['READY', token, str(total)]+([str(chunk), self.transfer_profile.codec] if self.fast else [])
                if ready != metadata:
                    raise RemoteError('Invalid upload session metadata')
                done = sent_crc = 0
                while done < total:
                    self._check_cancel()
                    blocks, requests = [], []
                    offset = done
                    for _ in range(self.transfer_profile.batch):
                        if offset >= total:
                            break
                        block = local.read(min(chunk, total-offset))
                        if len(block) != min(chunk, total-offset):
                            raise RemoteError('Local source changed during upload')
                        blocks.append(block)
                        requests.append((token, offset, self._encode_block(block)))
                        offset += len(block)
                    replies = self._data_requests('WRITEX' if self.fast else 'WRITE', requests, 'ACK')
                    for block, reply in zip(blocks, replies):
                        if reply != ['ACK', str(done+len(block)), str(total)]:
                            raise RemoteError('Incorrect satellite upload acknowledgment')
                        done += len(block)
                        sent_crc = zlib.crc32(block, sent_crc)
                        self.progress(done=done, total=total, elapsed=time.monotonic()-started)
                final_stat = os.fstat(local.fileno())
                if (local.read(1) or sent_crc != expected_crc or final_stat.st_size != total or
                        final_stat.st_mtime_ns != source_stat.st_mtime_ns):
                    raise RemoteError('Local source changed during upload')
            verified = 0
            while True:
                self._check_cancel()
                reply = self.request('VERIFY', token, verified, expected={'CHECKED'})
                next_offset = min(total, verified+1024)
                if reply != ['CHECKED', str(next_offset), str(total)]:
                    raise RemoteError('Incorrect satellite verification progress')
                verified = next_offset
                self.progress(done=total, total=total, elapsed=time.monotonic()-started,
                              status=f'Verifying SD contents: {verified:,} / {total:,} bytes')
                if verified == total:
                    break
            self._check_cancel()
            expected_stored = ['STORED', str(total), f'{expected_crc:08x}']
            commit_started = True
            reply = self.request('COMMIT', token, expected={'STORED'})
            if reply != expected_stored:
                raise RemoteError('Satellite upload completion checksum/size mismatch')
            completed = True
        except Exception as exc:
            failure = exc
        if token is not None and not completed:
            status = self._upload_cleanup('STATUS', token) if commit_started else None
            if status is not None and status == expected_stored:
                completed = True
            else:
                aborted = self._upload_cleanup('ABORT', token)
                if aborted is not None and aborted == expected_stored:
                    completed = True
                elif commit_started and not (aborted and aborted[0] == 'ABORTED'):
                    raise UploadUncertain('Upload completion could not be confirmed. Refresh the folder and '
                                          'verify the destination before retrying. ' + str(failure)) from failure
        if completed:
            self.progress(done=total, total=total, elapsed=time.monotonic()-started, status='Upload verified')
            return path
        if failure is not None:
            raise failure
        raise RemoteError('Upload did not complete')

    # OPEN returns the authoritative size used for limits and chunk validation.
    # Write into a unique temporary in the destination directory so failure cannot
    # replace an existing user file. END must agree on total size and running CRC;
    # only then flush/fsync and atomically replace the selected local destination.
    # CLOSE cleanup uses a fresh cancellation Event and a short bounded attempt.
    # This restarts interrupted downloads; it is not a resumable offset protocol.
    def download(self, path, destination, max_bytes=None):
        encoded = path_hex(path)
        self._select_profile()
        chunk = self.transfer_profile.chunk
        token = None
        temporary = None
        started = time.monotonic()
        try:
            extra = (chunk, self.transfer_profile.codec) if self.fast else ()
            reply = self.request('OPENX' if self.fast else 'OPEN', encoded, *extra, expected={'FILE'})
            if (len(reply) != (5 if self.fast else 3) or len(reply[1]) != 8 or
                    self.fast and reply[3:] != [str(chunk), self.transfer_profile.codec]):
                raise RemoteError('Invalid file metadata')
            try:
                int(reply[1], 16)
            except ValueError as exc:
                raise RemoteError('Invalid file session') from exc
            token, total = reply[1], unsigned(reply[2])
            if max_bytes is not None and total > max_bytes:
                raise RemoteError('File exceeds the viewing size limit; use Download to save it')
            destination = Path(destination)
            with tempfile.NamedTemporaryFile(prefix='.kestrelsat-', suffix='.part',
                                             dir=destination.parent, delete=False) as output:
                temporary = Path(output.name)
                done, crc = 0, 0
                self.progress(done=done, total=total, elapsed=0)
                while done < total:
                    self._check_cancel()
                    offsets = list(range(done, min(total, done+chunk*self.transfer_profile.batch), chunk))
                    replies = self._data_requests('READX' if self.fast else 'READ',
                                                  [(token, offset) for offset in offsets], 'DATA')
                    for reply in replies:
                        if len(reply) != 3 or unsigned(reply[1]) != done:
                            raise RemoteError('Incorrect file offset')
                        block = self._decode_block(reply[2])
                        if len(block) != min(chunk, total-done):
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
