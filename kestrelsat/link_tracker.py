"""Durable link experiments, without Tk or firmware changes.

Tracker owns the existing exclusive payload stream. The sole raw API reader
supplies a bounded metadata tap; disk I/O never runs on that reader. One HELLO
is one round-trip probe, without application retries. Radio retries are separate.
"""
from collections import deque
import csv
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import secrets
import tempfile
import threading
import time
import uuid
import zipfile

import serial
from . import remote_files as files
from . import __version__

COMMON = ('utc', 'elapsed_s')
PACKETS = COMMON + ('event', 'packet', 'source', 'address_bits', 'frame', 'method',
                    'rssi_dBm', 'packet_age_s', 'source_verified', 'valid_sample', 'detail')
PROBES = COMMON + ('sequence', 'request_id', 'outcome', 'latency_ms', 'duration_s',
                   'radio_retry_delta', 'malformed_lines', 'stale_replies', 'rolling_success_pct', 'detail')
EVENTS = COMMON + ('event', 'detail', 'distance_m')
LIMIT = 20000


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def bounded_rows(path, limit=LIMIT):
    """Full-session envelope with bounded memory; preserve the exact raw CSV.

    Retain bucket endpoints, RSSI extrema and one missing measurement/failure.
    This preserves outage evidence without loading many hours into the GUI.
    """
    if limit < 8:
        raise ValueError('Plot envelope limit must be at least 8')
    with Path(path).open(newline='', encoding='utf-8') as handle:
        count = sum(1 for _ in csv.DictReader(handle))
    if count <= limit:
        with Path(path).open(newline='', encoding='utf-8') as handle:
            return list(csv.DictReader(handle))
    bucket_size = max(1, math.ceil(count/max(1, limit//8)))
    result, candidates, extrema = [], {}, {}
    def add(index, row):
        candidates['last'] = (index, row)
        candidates.setdefault('first', (index, row))
        for field, reverse in (('rssi_dBm', False), ('rssi_dBm', True),
                               ('latency_ms', True), ('rolling_success_pct', False)):
            if field == 'rssi_dBm' and row.get('valid_sample') not in (None, True, 'True'):
                continue
            if row.get(field) not in ('', None):
                value = float(row[field]); key = (field, reverse)
                if key not in extrema or (value > extrema[key] if reverse else value < extrema[key]):
                    extrema[key] = value
                    candidates[key] = (index, row)
        if row.get('rssi_dBm') == '' or row.get('outcome', 'success') != 'success':
            candidates.setdefault('missing', (index, row))
    def emit():
        # At most seven candidates per bucket, even for an enormous source CSV.
        result.extend(row for _, row in sorted(dict(candidates.values()).items()))
        candidates.clear(); extrema.clear()
    with Path(path).open(newline='', encoding='utf-8') as handle:
        for index, row in enumerate(csv.DictReader(handle)):
            add(index, row)
            if (index+1) % bucket_size == 0:
                emit()
        emit()
    return result


class Recording:
    """Worker writes CSV; Tk reads bounded snapshots under the recording lock."""
    def __init__(self, directory, name, rate, timeout, configuration=None, clock=time.monotonic):
        if rate not in (1, 2, 5, 10) or not math.isfinite(timeout) or not .1 <= timeout <= 5:
            raise ValueError('Choose 1, 2, 5 or 10 Hz and a timeout from 0.1 to 5 s')
        self.clock, self.started, self.started_utc = clock, clock(), utc_now()
        self.id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
        self.directory = Path(directory)/self.id
        self.directory.mkdir(parents=True, exist_ok=False)
        self.name, self.rate, self.timeout = name, rate, timeout
        self.configuration = dict(configuration or {})
        self.lock = threading.RLock()
        self.packets, self.probes, self.events = (deque(maxlen=LIMIT) for _ in range(3))
        self.count = self.successes = self.packet_count = self.measurements = 0
        self.dropped = self.skipped = 0
        self.last_rssi = self.last_rssi_at = None
        self.recent_probes = deque(maxlen=10000)
        self.last_method = 'Waiting for received packets'
        self.status, self.detail, self.ended = 'running', '', None
        self.handles, self.writers = {}, {}
        self.row_counts = dict(packets=0, probes=0, events=0)
        try:
            for kind, columns in (('packets', PACKETS), ('probes', PROBES), ('events', EVENTS)):
                handle = (self.directory/(kind+'.csv')).open('x', newline='', encoding='utf-8')
                self.handles[kind] = handle
                self.writers[kind] = csv.DictWriter(handle, fieldnames=columns)
                self.writers[kind].writeheader(); handle.flush()
            self._metadata()
            self.mark('started', name)
        except Exception:
            for handle in self.handles.values():
                handle.close()
            raise

    def _metadata(self):
        data = dict(schema=1, gcs_version=__version__, id=self.id, name=self.name, started_utc=self.started_utc,
                    requested_hz=self.rate, probe_timeout_s=self.timeout,
                    probe='KFS1 HELLO, one attempt, no application retries',
                    rssi_direction='satellite to ground', configuration=self.configuration,
                    status=self.status, detail=self.detail, probes=self.count, successes=self.successes,
                    rf_packets=self.packet_count, rssi_measurements=self.measurements,
                    metadata_drops=self.dropped, skipped_schedule_slots=self.skipped, rows=self.row_counts,
                    elapsed_s=max(0, (self.ended if self.ended is not None else self.clock())-self.started),
                    ended_utc=None if self.ended is None else self.stamp(self.ended)['utc'])
        path = self.directory/'session.json'
        temporary = self.directory/'session.json.tmp'
        temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
        temporary.replace(path)

    def _write(self, kind, row):
        self.writers[kind].writerow(row)
        # Flush complete rows during the test rather than relying on a clean
        # Stop after a USB failure. The intended 1–10 Hz load is modest.
        self.handles[kind].flush()
        self.row_counts[kind] += 1
        getattr(self, kind).append(row)

    def stamp(self, at=None):
        elapsed = max(0, (self.clock() if at is None else at)-self.started)
        # Anchor UTC to the session start so queued metadata retains reception
        # time and wall-clock adjustments cannot make elapsed time run backward.
        utc = datetime.fromisoformat(self.started_utc)+timedelta(seconds=elapsed)
        return dict(utc=utc.isoformat(timespec='milliseconds'), elapsed_s=elapsed)

    def packet(self, event, verified):
        with self.lock:
            if self.status != 'running':
                return
            at = event['at']
            value = event.get('rssi')
            packet_at = event.get('packet_at', at)
            usable = (value is not None and verified and event.get('detail') != 'ambiguous'
                      and packet_at >= self.started and 0 <= at-packet_at <= 2)
            row = dict(self.stamp(at), event=event['event'], packet=event.get('packet', ''),
                       source=event.get('source', ''), address_bits=event.get('address_bits', ''),
                       frame=event.get('frame', ''), method=event.get('method', ''),
                       rssi_dBm=value if value is not None else '',
                       packet_age_s=max(0, at-event['packet_at']) if event.get('packet_at') is not None else '',
                       source_verified=verified, valid_sample=usable, detail=event.get('detail', ''))
            self._write('packets', row)
            if event['event'] == 'packet':
                self.packet_count += 1
            # Raw logs retain unverified and racing DB samples, but neither is
            # presented as a reliable measurement of the selected satellite.
            if usable:
                self.measurements += 1
                self.last_rssi, self.last_rssi_at = value, at
            self.last_method = row['method'] or row['detail']

    def probe(self, sequence, request, outcome, started, retries, malformed, stale, detail='', received_at=None):
        with self.lock:
            now = self.clock()
            while self.recent_probes and self.recent_probes[0][0] < now-30:
                self.recent_probes.popleft()
            if outcome != 'cancelled':
                self.recent_probes.append((now, outcome == 'success'))
            rolling = 100*sum(ok for _, ok in self.recent_probes)/len(self.recent_probes) if self.recent_probes else ''
            row = dict(self.stamp(now), sequence=sequence, request_id=f'{request:08x}', outcome=outcome,
                       latency_ms=((received_at if received_at is not None else now)-started)*1000 if outcome == 'success' else '',
                       duration_s=now-started, radio_retry_delta=retries,
                       malformed_lines=malformed, stale_replies=stale, rolling_success_pct=rolling, detail=detail)
            self._write('probes', row)
            if outcome != 'cancelled':
                self.count += 1
                self.successes += outcome == 'success'

    def mark(self, event, detail='', distance=None):
        with self.lock:
            if self.status != 'running':
                return
            if distance is not None and (not math.isfinite(distance) or distance < 0):
                raise ValueError('Distance must be a finite nonnegative number of metres')
            self._write('events', dict(self.stamp(), event=event, detail=detail[:1000],
                                       distance_m='' if distance is None else distance))

    def finish(self, status='stopped', detail=''):
        with self.lock:
            if self.status != 'running':
                return
            try:
                self.mark(status, detail)
            except OSError as exc:
                status, detail = 'failed', 'Recording write failed: '+str(exc)
            self.status, self.detail, self.ended = status, detail, self.clock()
            try:
                self._metadata()
            except OSError as exc:
                self.status, self.detail = 'failed', 'Session metadata write failed: '+str(exc)
            finally:
                for handle in self.handles.values():
                    try:
                        handle.close()
                    except OSError as exc:
                        self.status, self.detail = 'failed', str(exc)

    def snapshot(self):
        with self.lock:
            elapsed = max(0, (self.ended if self.ended is not None else self.clock())-self.started)
            return dict(name=self.name, id=self.id, status=self.status, detail=self.detail, elapsed=elapsed,
                        requested_hz=self.rate, probes=self.count, successes=self.successes,
                        success_pct=100*self.successes/self.count if self.count else None,
                        actual_hz=self.count/elapsed if elapsed else 0,
                        measurement_hz=self.measurements/elapsed if elapsed else 0,
                        packets=list(self.packets), probe_rows=list(self.probes), events=list(self.events),
                        rssi=self.last_rssi, age=None if self.last_rssi_at is None else self.clock()-self.last_rssi_at,
                        method=self.last_method, dropped=self.dropped, skipped=self.skipped,
                        directory=str(self.directory))

    def export(self, path):
        # Stopped logs are immutable. Do not block fresh probes while compressing
        # an hours-long active session; its raw CSVs are already accessible.
        with self.lock:
            if self.status == 'running':
                raise RuntimeError('Stop tracking before exporting the complete CSV bundle')
        path = Path(path)
        if path.resolve().is_relative_to(self.directory.resolve()):
            raise ValueError('Export outside the original recording folder to preserve its raw logs')
        # Preserve an existing export if compression/disk I/O fails midway.
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.link-export-', delete=False) as handle:
            temporary = Path(handle.name)
        try:
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
                for name in ('packets.csv', 'probes.csv', 'events.csv', 'session.json'):
                    archive.write(self.directory/name, arcname=name)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


class Tracker:
    """One pending probe, bounded parsing, explicit failures and skipped slots."""
    def __init__(self, port, recording, cancel, expected_source=None):
        self.port, self.recording, self.cancel = port, recording, cancel
        self.expected_source = expected_source
        self.buffer = bytearray()
        self.discard = False
        self.request_id = secrets.randbits(32)
        self.sequence = 0

    def _metadata(self, end=False):
        events, dropped = self.port.drain_rssi_capture(self, end=end)
        record = self.recording
        if dropped:
            with record.lock:
                record.dropped += dropped
            record.mark('metadata_overflow', str(dropped)+' radio metadata events lost')
        for event in events:
            source = event.get('source')
            expected = self.expected_source if self.expected_source is not None else self.port.destination
            verified = bool(source and expected not in (None, 0, 0xffff) and int(source, 16) == expected)
            record.packet(event, verified)

    def _read(self):
        data = self.port.read(min(max(self.port.in_waiting, 1), files.MAX_LINE))
        self.read_at = self.recording.clock()
        replies, malformed = [], 0
        for byte in data:
            if byte == 10:
                reply = None if self.discard else files.decode(bytes(self.buffer))
                self.buffer.clear(); self.discard = False
                if reply is not None:
                    replies.append(reply)
                else:
                    malformed += 1
            elif not self.discard:
                if len(self.buffer) < files.MAX_LINE:
                    self.buffer.append(byte)
                else:
                    self.buffer.clear(); self.discard = True
        self._metadata()
        return replies, malformed

    def _probe(self):
        self.sequence += 1
        self.request_id = (self.request_id+1) & 0xffffffff
        record = self.recording
        started = record.clock()
        before = self.port.traffic_snapshot()['radio_retries']
        outcome, detail, malformed, stale = 'timeout', '', 0, 0
        received_at = None
        try:
            payload = b'\n'+files.encode(self.request_id, 'HELLO')
            try:
                if self.port.write(payload) != len(payload):
                    raise serial.SerialException('Incomplete probe write')
            except serial.SerialException as exc:
                # A missing local TX status does not prove no RF reply arrived.
                # Still check payload until this same probe's overall deadline.
                outcome, detail = 'tx_error', str(exc)
            while not self.cancel.is_set() and record.clock()-started < record.timeout:
                replies, bad = self._read(); malformed += bad
                for request, fields in replies:
                    if request != self.request_id:
                        stale += 1
                    elif fields == ['HELLO', '1', '128']:
                        outcome, received_at = 'success', self.read_at
                        return
                    else:
                        outcome, detail = 'invalid_reply', ' '.join(fields)[:200]
                        return
        except serial.SerialException as exc:
            outcome, detail = 'transport_error', str(exc)
            raise
        finally:
            if self.cancel.is_set():
                outcome = 'cancelled'
            after = self.port.traffic_snapshot()['radio_retries']
            record.probe(self.sequence, self.request_id, outcome, started,
                         max(0, after-before), malformed, stale, detail, received_at)

    def run(self):
        record = self.recording
        old_timeout, old_write_timeout = self.port.timeout, self.port.write_timeout
        captured = False
        status, detail = 'stopped', ''
        try:
            self.port.begin_rssi_capture(self); captured = True
            self.port.timeout = .025
            self.port.write_timeout = min(1, record.timeout)
            self.port.set_rssi_polling(True)
            period, due = 1/record.rate, record.clock()
            while not self.cancel.is_set():
                now = record.clock()
                if now < due:
                    replies, bad = self._read()
                    for request, fields in replies:
                        record.mark('unmatched_reply', f'{request:08x}: '+ ' '.join(fields)[:100])
                    if bad:
                        record.mark('unparsed_idle_lines', str(bad))
                    self.cancel.wait(min(.01, max(0, due-record.clock())))
                    continue
                self._probe()
                due += period
                now = record.clock()
                if now > due:
                    missed = math.floor((now-due)/period)+1
                    with record.lock:
                        record.skipped += missed
                    record.mark('schedule_skipped', str(missed)+' slots; previous probe still occupied link')
                    due += missed*period
        except Exception as exc:
            status, detail = ('stopped', '') if self.cancel.is_set() else ('failed', str(exc))
        finally:
            try:
                if captured:
                    self._metadata(end=True)
            except Exception as exc:
                status, detail = 'failed', 'Final metadata logging failed: '+str(exc)
            self.port.timeout, self.port.write_timeout = old_timeout, old_write_timeout
            self.port.set_rssi_polling(False)
            record.finish(status, detail)
