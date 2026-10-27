"""Thread-safe transfer measurements, one-second sampling, and durable CSV logs."""

# READING GUIDE - useful file throughput versus serial traffic.
# A Session represents one transfer. progress records accepted useful file bytes;
# sample computes their delta over a monotonic interval, normally one second.
# Serial RX/TX counts additionally include framing, retries, API overhead and AT
# queries, so they are not interchangeable with download_Bps/upload_Bps.
#
# A zero file rate means no new useful bytes were completed in that interval. It
# is not a disconnect indication. RSSI is shown only when both radio reception
# and RSSI timestamps are fresh (within two seconds) and belong to this session.
# A stalled transfer can therefore show zero rate AND missing RSSI without the
# serial port disconnecting or a newly measured weak-signal value.
#
# Monotonic clocks measure durations; UTC timestamps label/export samples. Bounded
# in-memory history serves live graphs, while append-only CSV retains the full
# session. CSV errors are reported as recording errors and do not stop the file
# transfer. Recorder connects sessions to longer Recording timelines.
from collections import deque
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import threading
import time
import uuid

from .transfer_recordings import Recording

COLUMNS = ('session', 'started_utc', 'protocol', 'direction', 'file', 'baud', 'transport',
           'status', 'elapsed_s', 'interval_s', 'file_bytes', 'total_bytes',
           'download_Bps', 'upload_Bps', 'average_Bps', 'serial_rx_Bps', 'serial_tx_Bps',
           'protocol_retries', 'radio_tx_retries', 'rssi_dBm', 'rssi_age_s', 'rssi_polling', 'sample_utc', 'transfer_profile')


def traffic(port):
    if port is not None and hasattr(port, 'traffic_snapshot'):
        return port.traffic_snapshot()
    return dict(rx=0, tx=0, rssi=None, rssi_at=None, packet_at=None, radio_retries=None,
                rssi_error='RSSI unavailable on this connection')


class Session:
    def __init__(self, directory, protocol, direction, name, baud, mode, port=None,
                 clock=time.monotonic, rssi_enabled=False, observer=None):
        self.lock = threading.RLock()
        self.clock = clock
        self.observer = observer
        self.started = self.last_sample = clock()
        self.started_utc = datetime.now(timezone.utc).isoformat(timespec='microseconds')
        self.id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
        self.protocol, self.direction, self.name = protocol, direction, name
        self.baud, self.mode, self.port = baud, mode, port
        self.status = 'running'
        self.transfer_profile = ''
        self.done = self.total = self._base = self._previous_total = self._initial = self._high = 0
        self._file = None
        self.retries = 0 if protocol == 'KFS1' else None
        self.last_done = 0
        self.last_traffic = traffic(port)
        self.radio_start = self.last_traffic.get('radio_retries') or 0
        self.rssi_enabled = rssi_enabled
        self.samples = deque(maxlen=7200)  # last two hours; CSV retains the full session
        self.error = ''
        self.csv_path = Path(directory)/(self.id+'.csv')
        self._handle = None
        try:
            self.csv_path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.csv_path.open('w', newline='', encoding='utf-8')
            self._writer = csv.DictWriter(self._handle, fieldnames=COLUMNS)
            self._writer.writeheader()
            self._handle.flush()
        except OSError as exc:
            self.error = f'CSV recording unavailable: {exc}'
            self._close_log()

    # Progress can describe multi-file/resumed observations. Account only new
    # useful bytes rather than summing repeated cumulative totals from callbacks.
    # Protocol retry markers increment their own counter without adding file bytes.
    def progress(self, **info):
        with self.lock:
            if self.status != 'running':
                return
            if 'transfer_profile' in info:
                self.transfer_profile = info['transfer_profile']
            if info.get('retry'):
                self.retries = (self.retries or 0)+info['retry']
            value = info.get('done', info.get('sent', info.get('received')))
            if value is None or info.get('status') == 'skipped':
                return
            name = info.get('name', self.name)
            starting = self._file is None or info.get('status') == 'starting' or name != self._file
            if starting:
                self._base = self.done
                self._previous_total = self.total
                # A resumed peer already owns these bytes; they were not transferred now.
                self._initial = value if info.get('status') == 'starting' else 0
                self._high = self._initial
                self._file = name
            self.name = name
            self._high = max(self._high, value)
            self.done = self._base + max(0, self._high-self._initial)
            self.total = self._previous_total + max(0, (info.get('total') or 0)-self._initial)

    # Take a locked, coherent snapshot and divide byte DELTA by elapsed DELTA.
    # This yields interval B/s, not UART baud and not the lifetime average. Fresh
    # RSSI also requires a recent received packet; querying DB cannot make an old
    # last-packet measurement evidence of a presently healthy link.
    def sample(self, force=False, now=None):
        with self.lock:
            now = self.clock() if now is None else now
            interval = now-self.last_sample
            if not force and (self.status != 'running' or interval < 1):
                return None
            if interval <= 0:
                interval = 1e-9
            elapsed = max(0, now-self.started)
            current = traffic(self.port)
            rate = max(0, self.done-self.last_done)/interval
            rssi_at = current.get('rssi_at')
            packet_at = current.get('packet_at')
            age = None if rssi_at is None else max(0, now-rssi_at)
            valid = (self.rssi_enabled and age is not None and age <= 2 and packet_at is not None
                     and now-packet_at <= 2 and rssi_at >= self.started and packet_at >= self.started)
            row = dict(session=self.id, started_utc=self.started_utc, protocol=self.protocol,
                       transfer_profile=self.transfer_profile,
                       direction=self.direction, file=self.name, baud=self.baud, transport=self.mode,
                       status=self.status, elapsed_s=elapsed, interval_s=interval,
                       file_bytes=self.done, total_bytes=self.total,
                       download_Bps=rate if self.direction == 'download' else 0,
                       upload_Bps=rate if self.direction == 'upload' else 0,
                       average_Bps=self.done/elapsed if elapsed > 0 else 0,
                       serial_rx_Bps=max(0,current['rx']-self.last_traffic['rx'])/interval,
                       serial_tx_Bps=max(0,current['tx']-self.last_traffic['tx'])/interval,
                       protocol_retries=self.retries,
                       radio_tx_retries=(max(0,current['radio_retries']-self.radio_start)
                                         if current.get('radio_retries') is not None else None),
                       rssi_dBm=current.get('rssi') if valid else None,
                       rssi_age_s=age if self.rssi_enabled else None,
                       rssi_polling=self.rssi_enabled,
                       sample_utc=(datetime.fromisoformat(self.started_utc)+
                                   timedelta(seconds=elapsed)).isoformat(timespec='microseconds'))
            self.samples.append(row)
            self.last_done, self.last_sample, self.last_traffic = self.done, now, current
            if self._handle:
                try:
                    self._writer.writerow(row)
                    self._handle.flush()
                except OSError as exc:
                    self.error = f'CSV recording stopped: {exc}'
                    self._close_log()
            if self.observer:
                self.observer(self, row, now)
            return row

    # Terminal state is idempotent: callbacks/cleanup may both attempt to finish.
    # Force a final measurement before closing CSV so the last accepted bytes
    # are represented even when less than one normal sample interval elapsed.
    def finish(self, outcome):
        with self.lock:
            if self.status != 'running':
                return
            self.status = outcome
            self.sample(force=True)
            self._close_log()

    def _close_log(self):
        if self._handle:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None

    def snapshot(self):
        with self.lock:
            return dict(started=self.started, id=self.id, name=self.name, status=self.status, done=self.done, total=self.total,
                        protocol=self.protocol, direction=self.direction, mode=self.mode, baud=self.baud,
                        transfer_profile=self.transfer_profile,
                        retries=self.retries, error=self.error, samples=list(self.samples),
                        csv_path=str(self.csv_path), started_utc=self.started_utc,
                        rssi_note=traffic(self.port).get('rssi_error', ''))

    def export_csv(self, destination):
        with self.lock:
            if self.error:
                raise OSError(self.error)
            if self._handle:
                self._handle.flush()
            if Path(destination).resolve() != self.csv_path.resolve():
                shutil.copyfile(self.csv_path, destination)


class Recorder:
    def __init__(self, directory, clock=time.monotonic):
        self.directory, self.clock = directory, clock
        self.lock = threading.RLock()
        self.sessions = deque(maxlen=20)
        self.active = None
        self.recording = None
        self.recordings = deque(maxlen=20)
        self.combine_cancel = threading.Event()

    def start(self, protocol, direction, name, baud, mode, port=None, rssi_enabled=False):
        with self.lock:
            if self.active and self.active.status == 'running':
                raise RuntimeError('A transfer statistics session is already active')
            session = Session(self.directory, protocol, direction, name, baud, mode, port,
                              self.clock, rssi_enabled, self._on_sample)
            if self.recording:
                self.recording.begin(session.snapshot(), session.started)
            self.sessions.appendleft(session)
            self.active = session
            return session

    def tick(self):
        with self.lock:
            session = self.active
        if session:
            session.sample()
        recording = self.recording
        if recording:
            recording.idle(self.clock())

    def history(self):
        with self.lock:
            return list(self.sessions)

    def _on_sample(self, session, row, now):
        recording = self.recording
        if recording:
            recording.ingest(session.id, row, now)

    # A recording may begin while a transfer is already running. Capture a
    # snapshot/baseline under the session lock, then hand a copy to Recording;
    # do not introduce an inverse lock order by letting Recording acquire Session.
    def start_recording(self, name):
        with self.lock:
            if self.recording:
                raise RuntimeError('A multi-operation recording is already running')
            session = self.active
            if session:
                with session.lock:
                    now = self.clock()
                    if session.status == 'running':
                        session.sample(force=True, now=now)
                    recording = Recording(self.directory, name, now)
                    if session.status == 'running':
                        recording.begin(session.snapshot(), now, partial=True)
                    self.recording = recording
            else:
                recording = self.recording = Recording(self.directory, name, self.clock())
            self.recordings.appendleft(recording)
            return recording

    def stop_recording(self, outcome='stopped'):
        with self.lock:
            recording = self.recording
            if not recording:
                return
            session = self.active
            if session:
                with session.lock:
                    now = self.clock()
                    if session.status == 'running':
                        session.sample(force=True, now=now)
                    recording.finish(now, outcome)
                    self.recording = None
            else:
                recording.finish(self.clock(), outcome)
                self.recording = None

    def event(self, event, detail):
        recording = self.recording
        if recording:
            recording.event(event, detail, self.clock())

    def recording_history(self):
        with self.lock:
            return list(self.recordings)

    # Rebuild the combined timeline from complete session CSV files, preserving
    # the original monotonic timing and real idle gaps. Bounded live deques may
    # have dropped old samples, so they cannot substitute for durable source CSVs.
    def combine(self, sessions, name):
        """Stream full CSVs from finished transfers in this process, preserving gaps."""
        sessions = sorted(set(sessions), key=lambda item: item.started)
        if len(sessions) < 2:
            raise ValueError('Select at least two finished transfers')
        snapshots = [session.snapshot() for session in sessions]
        if any(snap['status'] == 'running' for snap in snapshots):
            raise ValueError('Wait for selected transfers to finish')
        if any(snap['error'] for snap in snapshots):
            raise OSError('A selected transfer has incomplete CSV data')
        recording = Recording(self.directory, name, sessions[0].started, snapshots[0]['started_utc'])
        try:
            for session, snap in zip(sessions, snapshots):
                if self.combine_cancel.is_set():
                    raise RuntimeError('Application closing; combine stopped')
                while session.started-recording.last_sample >= 1:
                    if self.combine_cancel.is_set():
                        raise RuntimeError('Application closing; combine stopped')
                    recording.idle(recording.last_sample+1)
                info = dict(snap, done=0)
                recording.begin(info, session.started)
                with session.csv_path.open(newline='', encoding='utf-8') as handle:
                    last = None
                    for row in csv.DictReader(handle):
                        if self.combine_cancel.is_set():
                            raise RuntimeError('Application closing; combine stopped')
                        row['file_bytes'] = int(row['file_bytes'])
                        for key in ('serial_rx_Bps', 'serial_tx_Bps', 'rssi_dBm', 'rssi_age_s',
                                    'protocol_retries', 'radio_tx_retries'):
                            row[key] = float(row[key]) if row.get(key) else None
                        row['rssi_polling'] = row['rssi_polling'] == 'True'
                        last = session.started+float(row['elapsed_s'])
                        recording.ingest(session.id, row, last)
                    if last is None or recording.current:
                        raise ValueError('Selected CSV is incomplete; cannot combine it reliably')
            recording.finish(recording.last_sample, 'completed')
            if recording.error:
                raise OSError(recording.error)
        except Exception:
            recording.finish(recording.last_sample, 'failed')
            raise
        with self.lock:
            self.recordings.appendleft(recording)
        return recording
