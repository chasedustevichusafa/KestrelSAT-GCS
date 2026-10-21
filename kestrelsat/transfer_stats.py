"""Thread-safe transfer measurements, one-second sampling, and durable CSV logs."""
from collections import deque
import csv
from datetime import datetime, timezone
from pathlib import Path
import shutil
import threading
import time
import uuid

COLUMNS = ('session', 'started_utc', 'protocol', 'direction', 'file', 'baud', 'transport',
           'status', 'elapsed_s', 'interval_s', 'file_bytes', 'total_bytes',
           'download_Bps', 'upload_Bps', 'average_Bps', 'serial_rx_Bps', 'serial_tx_Bps',
           'protocol_retries', 'radio_tx_retries', 'rssi_dBm', 'rssi_age_s', 'rssi_polling')


def traffic(port):
    if port is not None and hasattr(port, 'traffic_snapshot'):
        return port.traffic_snapshot()
    return dict(rx=0, tx=0, rssi=None, rssi_at=None, packet_at=None, radio_retries=None,
                rssi_error='RSSI unavailable on this connection')


class Session:
    def __init__(self, directory, protocol, direction, name, baud, mode, port=None,
                 clock=time.monotonic, rssi_enabled=False):
        self.lock = threading.RLock()
        self.clock = clock
        self.started = self.last_sample = clock()
        self.started_utc = datetime.now(timezone.utc).isoformat(timespec='seconds')
        self.id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
        self.protocol, self.direction, self.name = protocol, direction, name
        self.baud, self.mode, self.port = baud, mode, port
        self.status = 'running'
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

    def progress(self, **info):
        with self.lock:
            if self.status != 'running':
                return
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
                       rssi_polling=self.rssi_enabled)
            self.samples.append(row)
            self.last_done, self.last_sample, self.last_traffic = self.done, now, current
            if self._handle:
                try:
                    self._writer.writerow(row)
                    self._handle.flush()
                except OSError as exc:
                    self.error = f'CSV recording stopped: {exc}'
                    self._close_log()
            return row

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
            return dict(id=self.id, name=self.name, status=self.status, done=self.done, total=self.total,
                        protocol=self.protocol, direction=self.direction, mode=self.mode, baud=self.baud,
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

    def start(self, protocol, direction, name, baud, mode, port=None, rssi_enabled=False):
        with self.lock:
            if self.active and self.active.status == 'running':
                raise RuntimeError('A transfer statistics session is already active')
            session = Session(self.directory, protocol, direction, name, baud, mode, port,
                              self.clock, rssi_enabled)
            self.sessions.appendleft(session)
            self.active = session
            return session

    def tick(self):
        with self.lock:
            session = self.active
        if session:
            session.sample()

    def history(self):
        with self.lock:
            return list(self.sessions)
