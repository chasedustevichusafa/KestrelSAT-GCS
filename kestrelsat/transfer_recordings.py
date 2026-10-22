"""Continuous, bounded-memory recordings spanning independent transfer sessions."""
from collections import deque
import csv
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import threading
import uuid

COLUMNS = ('recording', 'sample_utc', 'elapsed_s', 'interval_s', 'state', 'operation',
           'protocol', 'direction', 'file', 'baud', 'transport', 'outcome',
           'captured_bytes', 'download_bytes', 'upload_bytes', 'download_Bps', 'upload_Bps',
           'active_average_Bps', 'recording_average_Bps', 'protocol_retries',
           'radio_tx_retries', 'serial_rx_Bps', 'serial_tx_Bps', 'rssi_dBm', 'rssi_age_s', 'rssi_polling')
OPERATIONS = ('recording', 'operation', 'protocol', 'direction', 'file', 'baud', 'transport',
              'start_utc', 'end_utc', 'start_elapsed_s', 'end_elapsed_s', 'duration_s',
              'captured_bytes', 'average_Bps', 'outcome', 'partial_start', 'partial_end')
EVENTS = ('recording', 'timestamp_utc', 'elapsed_s', 'event', 'operation', 'detail')


class Recording:
    def __init__(self, directory, name, started, started_utc=None):
        self.lock = threading.RLock()
        self.started = self.last_sample = started
        self.origin = (datetime.fromisoformat(started_utc) if started_utc else datetime.now(timezone.utc))
        self.started_utc = self.origin.isoformat(timespec='microseconds')
        self.id = 'recording-'+self.origin.strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
        self.name = name
        self.status = 'running'
        self.error = ''
        self.samples = deque(maxlen=7200)
        self.events = deque(maxlen=2000)
        self.operations = deque(maxlen=2000)
        self.sample_count = self.event_count = 0
        self.operation_count = self.download_bytes = self.upload_bytes = 0
        self.active_seconds = 0.0
        self.current = None
        self._logs = {}
        self.csv_path = Path(directory)/(self.id+'.csv')
        self.operations_path = self.csv_path.with_suffix('.operations.csv')
        self.events_path = self.csv_path.with_suffix('.events.csv')
        try:
            self.csv_path.parent.mkdir(parents=True, exist_ok=True)
            for kind, path, columns in [('samples', self.csv_path, COLUMNS),
                                        ('operations', self.operations_path, OPERATIONS),
                                        ('events', self.events_path, EVENTS)]:
                handle = path.open('w', newline='', encoding='utf-8')
                self._logs[kind] = (handle, csv.DictWriter(handle, fieldnames=columns))
                self._logs[kind][1].writeheader()
                handle.flush()
        except OSError as exc:
            self._log_error(exc)
        self.event('recording_started', name, started)
        self._row(started)

    def utc(self, now):
        return (self.origin+timedelta(seconds=max(0, now-self.started))).isoformat(timespec='microseconds')

    def _close_logs(self):
        for handle, _ in self._logs.values():
            try:
                handle.close()
            except OSError:
                pass
        self._logs.clear()

    def _log_error(self, exc):
        self.error = f'Recording CSV unavailable: {exc}'
        self._close_logs()

    def _write(self, kind, row):
        if kind in self._logs:
            try:
                handle, writer = self._logs[kind]
                writer.writerow(row)
                handle.flush()
            except OSError as exc:
                self._log_error(exc)

    def event(self, event, detail, now, operation=''):
        with self.lock:
            if self.status != 'running':
                return
            row = dict(recording=self.id, timestamp_utc=self.utc(now),
                       elapsed_s=max(0, now-self.started), event=event, operation=operation, detail=detail)
            self.event_count += 1
            self.events.append(row)
            self._write('events', row)

    def begin(self, info, now, partial=False):
        """info is copied under the Session lock; never acquire a Session lock here."""
        with self.lock:
            if self.status != 'running':
                return
            if self.current:
                raise RuntimeError('Recording already contains an active transfer')
            self.idle(now, force=True)
            self.current = dict(info, capture_start=now, baseline=info['done'], captured=0,
                                partial_start=partial)
            self.event('operation_started', info['name'], now, info['id'])

    def _row(self, now, delta=0, source=None):
        interval = max(0, now-self.last_sample)
        elapsed = max(0, now-self.started)
        op = self.current
        direction = op['direction'] if op else ''
        if direction == 'download':
            self.download_bytes += delta
        elif direction == 'upload':
            self.upload_bytes += delta
        if op:
            self.active_seconds += interval
        total = self.download_bytes+self.upload_bytes
        row = dict.fromkeys(COLUMNS, None)
        row.update(recording=self.id, sample_utc=self.utc(now), elapsed_s=elapsed,
                   interval_s=interval, state='transfer' if op else 'idle',
                   operation=op['id'] if op else '', protocol=op['protocol'] if op else '',
                   direction=direction, file=op['name'] if op else '', baud=op['baud'] if op else '',
                   transport=op['mode'] if op else '', outcome=source.get('status', '') if source else '',
                   captured_bytes=op['captured'] if op else 0,
                   download_bytes=self.download_bytes, upload_bytes=self.upload_bytes,
                   download_Bps=delta/interval if direction == 'download' and interval > 0 else 0,
                   upload_Bps=delta/interval if direction == 'upload' and interval > 0 else 0,
                   active_average_Bps=total/self.active_seconds if self.active_seconds > 0 else 0,
                   recording_average_Bps=total/elapsed if elapsed > 0 else 0)
        if source:
            for key in ('protocol_retries', 'radio_tx_retries', 'serial_rx_Bps', 'serial_tx_Bps',
                        'rssi_dBm', 'rssi_age_s', 'rssi_polling'):
                row[key] = source.get(key)
        self.sample_count += 1
        self.samples.append(row)
        self.last_sample = now
        self._write('samples', row)

    def idle(self, now, force=False):
        with self.lock:
            if self.status == 'running' and self.current is None and (
                    now-self.last_sample >= 1 or force and now > self.last_sample):
                self._row(now)

    def ingest(self, session_id, source, now):
        with self.lock:
            op = self.current
            if self.status != 'running' or not op or op['id'] != session_id:
                return
            captured = max(op['captured'], int(source['file_bytes'])-op['baseline'])
            delta = captured-op['captured']
            op['captured'] = captured
            op['name'] = source['file']
            self._row(now, delta, source)
            if source['status'] != 'running':
                self._end_operation(now, source['status'])

    def _end_operation(self, now, outcome, partial_end=False):
        op = self.current
        duration = max(0, now-op['capture_start'])
        row = dict(recording=self.id, operation=op['id'], protocol=op['protocol'],
                   direction=op['direction'], file=op['name'], baud=op['baud'], transport=op['mode'],
                   start_utc=self.utc(op['capture_start']), end_utc=self.utc(now),
                   start_elapsed_s=max(0, op['capture_start']-self.started),
                   end_elapsed_s=max(0, now-self.started), duration_s=duration,
                   captured_bytes=op['captured'], average_Bps=op['captured']/duration if duration > 0 else 0,
                   outcome=outcome, partial_start=op['partial_start'], partial_end=partial_end)
        self.operations.append(row)
        self.operation_count += 1
        self._write('operations', row)
        self.event('operation_finished', outcome, now, op['id'])
        self.current = None
        self._row(now)  # Drop rate to zero at the boundary, before the idle gap.

    def finish(self, now, outcome='stopped'):
        with self.lock:
            if self.status != 'running':
                return
            if self.current:
                self._end_operation(now, 'in_progress', partial_end=True)
            self.idle(now, force=True)
            self.event('recording_finished', outcome, now)
            self.status = outcome
            self._close_logs()

    def snapshot(self):
        with self.lock:
            last = self.samples[-1] if self.samples else {}
            return dict(id=self.id, name=self.name, status=self.status, is_recording=True,
                        started_utc=self.started_utc, samples=list(self.samples), events=list(self.events),
                        operations=list(self.operations), operation_count=self.operation_count,
                        current_operation=self.current['name'] if self.current else '',
                        done=self.download_bytes+self.upload_bytes, download_bytes=self.download_bytes,
                        upload_bytes=self.upload_bytes, active_seconds=self.active_seconds,
                        elapsed=last.get('elapsed_s', 0), error=self.error, csv_path=str(self.csv_path),
                        operations_path=str(self.operations_path), events_path=str(self.events_path))

    def graph_snapshot(self):
        """Stream a full-timeline overview, keeping extrema with bounded plot memory."""
        with self.lock:
            if self.error:
                raise OSError(self.error)
            snap = self.snapshot()
            count, event_count = self.sample_count, self.event_count
            for handle, _ in self._logs.values():
                handle.flush()
        # Up to ten representative points per bucket, preserving rate/RSSI peaks.
        bucket_size = max(1, math.ceil(count/1200))
        points, bucket = [], {}
        def emit():
            if bucket:
                selected = {index: row for index, row in bucket.values()}
                points.extend(selected[index] for index in sorted(selected))
                bucket.clear()
        with self.csv_path.open(newline='', encoding='utf-8') as handle:
            for index, source in enumerate(csv.DictReader(handle)):
                if index >= count:
                    break
                row = {key: float(source[key]) if source[key] else None
                       for key in ('elapsed_s', 'download_Bps', 'upload_Bps', 'rssi_dBm')}
                pair = (index, row)
                bucket.setdefault('first', pair)
                bucket['last'] = pair
                for key in ('download_Bps', 'upload_Bps', 'rssi_dBm'):
                    if row[key] is not None:
                        for suffix, compare in (('min', lambda a, b: a < b), ('max', lambda a, b: a > b)):
                            candidate = key+suffix
                            if candidate not in bucket or compare(row[key], bucket[candidate][1][key]):
                                bucket[candidate] = pair
                if row['rssi_dBm'] is None:
                    bucket.setdefault('missing_first', pair)
                    bucket['missing_last'] = pair
                if (index+1) % bucket_size == 0:
                    emit()
        emit()
        # Select markers across the full timeline; complete events remain in CSV.
        events = []
        stride = max(1, math.ceil(event_count/198))
        with self.events_path.open(newline='', encoding='utf-8') as handle:
            for index, row in enumerate(csv.DictReader(handle)):
                if index >= event_count:
                    break
                if index == 0 or index % stride == 0 or index == event_count-1:
                    row['elapsed_s'] = float(row['elapsed_s'])
                    events.append(row)
        snap.update(samples=points, events=events)
        return snap

    def export_csv(self, destination, kind='samples'):
        with self.lock:
            if self.error:
                raise OSError(self.error)
            path = {'samples': self.csv_path, 'operations': self.operations_path, 'events': self.events_path}[kind]
            if kind in self._logs:
                self._logs[kind][0].flush()
            if Path(destination).resolve() != path.resolve():
                shutil.copyfile(path, destination)
