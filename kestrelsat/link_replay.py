"""Read-only reconstruction of Link Tracker ZIP/folder recordings.

Never extract arbitrary archive paths. Copy only the four known files into an
owned temporary cache, validate/normalize into separate view CSVs, and retain the
original bytes for re-export. Loading uses no radio and never changes live state.
All parsing runs in the UI's existing plain-data file worker, not on Tk's thread.
"""
from collections import deque
import csv
from datetime import datetime
import json
import math
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import uuid
import zipfile

from . import link_tracker as link

FILES = ('packets.csv', 'probes.csv', 'events.csv', 'session.json')
MAX_TOTAL = 512*1024*1024
MAX_FILE = 384*1024*1024
MAX_JSON = 1024*1024
MAX_ROWS = 2_000_000


def _lines(handle):
    # TextIO's default iteration can allocate an entire malformed giant line
    # before csv's field limit is checked. Bound physical lines as well as the
    # total input size; quoted multiline fields remain supported by DictReader.
    while True:
        line = handle.readline(256*1024+1)
        if not line:
            return
        if len(line) > 256*1024:
            raise ValueError('CSV line exceeds the import limit')
        yield line


def parse_source(text):
    text = str(text).strip().lower().removeprefix('0x')
    if not text or not re.fullmatch(r'[0-9a-f]{1,16}', text) or int(text, 16) == 0xffff:
        raise ValueError('Enter one satellite receive address in hex, such as 0000; FFFF is broadcast')
    return int(text, 16)


def number(value, label, low=0, high=1e12):
    if isinstance(value, bool):
        raise ValueError(label+' must be numeric, not a boolean')
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise ValueError(label+' must be a number') from None
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(label+' is outside its valid range')
    return result


def boolean(value):
    if str(value).lower() not in ('true', 'false'):
        raise ValueError('Boolean CSV flags must be True or False')
    return str(value).lower() == 'true'


def timestamp(text):
    if not isinstance(text, str) or len(text) > 80:
        raise ValueError('Missing/invalid UTC timestamp')
    try:
        parsed = datetime.fromisoformat(text[:-1]+'+00:00' if text.endswith('Z') else text)
        if parsed.utcoffset() is None:
            raise ValueError('Timestamp must contain a UTC offset')
        return parsed
    except (ValueError, OverflowError):
        raise ValueError('Invalid timestamp: '+text[:80]) from None


def _copy(source, target, maximum):
    total = 0
    with target.open('xb') as out:
        while True:
            chunk = source.read(1024*1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise ValueError('A session file exceeds the import size limit')
            out.write(chunk)
    return total


def _copy_input(path, raw):
    """Bound declared AND actual bytes; never trust ZIP names as output paths."""
    total = 0
    if path.is_dir():
        for name in FILES:
            source = path/name
            if source.is_symlink() or not source.is_file():
                raise ValueError('Session folder must contain a regular '+name)
            with source.open('rb') as handle:
                total += _copy(handle, raw/name, MAX_JSON if name == 'session.json' else MAX_FILE)
            if total > MAX_TOTAL:
                raise ValueError('Session exceeds the 512 MiB import limit')
    else:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 64:
                raise ValueError('Too many archive entries')
            selected = {}
            for entry in entries:
                parts = PurePosixPath(entry.filename)
                if (parts.is_absolute() or '..' in parts.parts or '\\' in entry.filename
                        or ':' in entry.filename or stat.S_ISLNK(entry.external_attr >> 16)):
                    raise ValueError('Unsupported archive path or symbolic link')
                if entry.filename in FILES:
                    if entry.filename in selected or entry.flag_bits & 1:
                        raise ValueError('Duplicate or encrypted session file')
                    selected[entry.filename] = entry
            if set(selected) != set(FILES):
                raise ValueError('Bundle must contain packets.csv, probes.csv, events.csv and session.json at its root')
            for name, entry in selected.items():
                limit = MAX_JSON if name == 'session.json' else MAX_FILE
                if entry.file_size > limit or total+entry.file_size > MAX_TOTAL:
                    raise ValueError('Session exceeds the import size limit')
                with archive.open(entry) as handle:
                    total += _copy(handle, raw/name, limit)
                if total > MAX_TOTAL:
                    raise ValueError('Session exceeds the 512 MiB import limit')


class SavedSession:
    """Immutable recording interface used by the existing history/graph/export UI.

    The temporary cache lives as long as this plain object does, including while
    a file worker holds it after history eviction or UI closure. No Tk references
    or filesystem deletion from a Qt callback are needed for cache ownership.
    """
    imported = True

    def __init__(self, path, source_override=None, cache_root=None):
        self.source_path = Path(path).resolve()
        if source_override is not None:
            source_override = parse_source(f'{source_override:x}')
        self.source_override = source_override
        if cache_root is not None:
            Path(cache_root).mkdir(parents=True, exist_ok=True)
        self.cache = tempfile.TemporaryDirectory(prefix='link-replay-', dir=cache_root, ignore_cleanup_errors=True)
        root = Path(self.cache.name)
        self.raw_directory = root/'raw'; self.raw_directory.mkdir()
        self.directory = root/'view'; self.directory.mkdir()
        try:
            _copy_input(self.source_path, self.raw_directory)
            self.metadata = json.loads((self.raw_directory/'session.json').read_text(encoding='utf-8-sig'))
            meta = self.metadata
            if not isinstance(meta, dict) or type(meta.get('schema')) is not int or meta['schema'] != 1:
                raise ValueError('Unsupported Link Tracker bundle schema')
            self.id = meta.get('id', '')
            if not isinstance(self.id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', self.id):
                raise ValueError('Invalid session identifier')
            if not isinstance(meta.get('name'), str) or len(meta['name']) > 1000:
                raise ValueError('Invalid session name')
            timestamp(meta.get('started_utc'))
            self.name = meta['name']
            self.rate = number(meta.get('requested_hz'), 'Probe rate')
            if self.rate not in (1, 2, 5, 10):
                raise ValueError('Unsupported recorded probe rate')
            self.rate = int(self.rate)
            self.timeout = number(meta.get('probe_timeout_s'), 'Probe timeout', .1, 5)
            self.selection_id = self.id+' [saved '+uuid.uuid4().hex[:6]+']'
            self.status = 'imported'
            self.original_status = str(meta.get('status', 'unknown'))[:100]
            self.row_counts = dict(packets=0, probes=0, events=0)
            self.count = self.successes = self.packet_count = self.measurements = 0
            self.unverified = 0
            self.last_rssi = self.last_rssi_at = None
            self.last_method = ''
            self.rssi_errors, self.last_error = 0, ''
            self.elapsed = number(meta.get('elapsed_s', 0), 'Session elapsed time')
            self.skipped = self.dropped = 0
            self.warnings = []
            if self.original_status == 'running':
                self.warnings.append('Original session ended without a recorded Stop; rebuilt available CSV rows.')
            self._validate('packets', link.PACKETS, optional={'valid_sample'})
            self._validate('probes', link.PROBES, optional={'rolling_success_pct'})
            self._validate('events', link.EVENTS)
            for field, attr in (('metadata_drops', 'dropped'), ('skipped_schedule_slots', 'skipped')):
                value = number(meta.get(field, 0), field)
                if value != int(value): raise ValueError(field+' must be an integer')
                setattr(self, attr, max(getattr(self, attr), int(value)))
            if (meta.get('probes', self.count) != self.count or
                    meta.get('successes', self.successes) != self.successes):
                self.warnings.append('Summary counts rebuilt from CSV differ from original metadata.')
            if self.unverified and not self.measurements and source_override is None:
                self.warnings.append('Saved RSSI is unverified; reopen with the optional source recheck to analyze a known satellite address.')
            if source_override is not None:
                self.warnings.append(f'Imported RSSI rechecked against source {source_override:04x}; raw files unchanged.')
            self._full = self._make_snapshot()
        except Exception:
            self.cache.cleanup()
            raise

    def _validate(self, kind, columns, optional=frozenset()):
        """Stream complete rows, reject malformed/nonfinite data, bound all state."""
        previous, sequence, recent = -1, -1, deque(maxlen=10000)
        with (self.raw_directory/(kind+'.csv')).open(newline='', encoding='utf-8-sig') as source, \
                (self.directory/(kind+'.csv')).open('x', newline='', encoding='utf-8') as out:
            reader = csv.DictReader(_lines(source), strict=True)
            fields = reader.fieldnames or []
            if (len(fields) != len(set(fields)) or len(fields) > 64 or not set(columns)-optional <= set(fields)):
                raise ValueError(kind+'.csv has missing or duplicate columns')
            writer = csv.DictWriter(out, fieldnames=columns); writer.writeheader()
            for index, row in enumerate(reader, 2):
                try:
                    if index-1 > MAX_ROWS or None in row or any(v is None or len(v) > 4096 for v in row.values()):
                        raise ValueError('Too many rows or malformed CSV fields')
                    timestamp(row['utc'])
                    elapsed = number(row['elapsed_s'], 'Elapsed time')
                    if elapsed < previous:
                        raise ValueError('Elapsed time must not run backward')
                    previous = elapsed; self.elapsed = max(self.elapsed, elapsed)
                    row['elapsed_s'] = elapsed
                    if kind == 'packets':
                        self._packet(row)
                    elif kind == 'probes':
                        current = number(row['sequence'], 'Probe sequence', 0, 0xffffffff)
                        if current != int(current) or current <= sequence:
                            raise ValueError('Probe sequences must be increasing integers')
                        sequence = current
                        self._probe(row, recent)
                    else:
                        if row['distance_m']:
                            row['distance_m'] = number(row['distance_m'], 'Distance')
                        if row['event'] in ('metadata_overflow', 'schedule_skipped'):
                            match = re.match(r'\d+', row['detail'])
                            if match:
                                value = int(match[0])
                                if row['event'] == 'metadata_overflow': self.dropped += value
                                else: self.skipped += value
                    writer.writerow({key: row.get(key, '') for key in columns})
                    self.row_counts[kind] += 1
                except (ValueError, OverflowError) as exc:
                    raise ValueError(f'{kind}.csv row {index}: {exc}') from None

    def _packet(self, row):
        if row['event'] not in ('packet', 'rssi', 'rssi_error'):
            raise ValueError('Unknown packet event')
        if row['event'] != 'rssi_error':
            bits = number(row['address_bits'], 'Address bits', 16, 64)
            if bits not in (16, 64) or not re.fullmatch('[0-9a-fA-F]{'+str(int(bits)//4)+'}', row['source']):
                raise ValueError('Invalid receive source address')
            if row['frame'] not in ('80', '81', '90', '91') or (row['frame'] == '81') != (bits == 16):
                raise ValueError('Invalid receive frame/address combination')
            packet = number(row['packet'], 'Packet number', 1)
            if packet != int(packet): raise ValueError('Packet numbers must be integers')
        value = number(row['rssi_dBm'], 'RSSI', -127, -1) if row['rssi_dBm'] else None
        age = number(row['packet_age_s'], 'Packet age') if row['packet_age_s'] else None
        verified = boolean(row['source_verified'])
        if value is not None and not verified: self.unverified += 1
        physically_valid = (value is not None and age is not None and age <= 2
                            and row['elapsed_s']+1e-7 >= age and row['detail'] != 'ambiguous')
        valid = boolean(row['valid_sample']) if 'valid_sample' in row else verified and physically_valid
        if valid and (not verified or not physically_valid):
            raise ValueError('Inconsistent RSSI validity flags')
        if self.source_override is not None:
            verified = bool(row['source'] and int(row['source'], 16) == self.source_override)
            valid = verified and physically_valid
        row['rssi_dBm'] = '' if value is None else value
        row['source_verified'], row['valid_sample'] = verified, valid
        if row['event'] == 'packet': self.packet_count += 1
        if valid:
            self.measurements += 1
            self.last_rssi, self.last_rssi_at = value, row['elapsed_s']
            self.last_error = ''
        if row['event'] == 'rssi_error' or row['detail'] == 'ambiguous':
            self.rssi_errors += 1; self.last_error = row['detail']
        self.last_method = row['method'] or row['detail']

    def _probe(self, row, recent):
        if row['outcome'] not in ('success', 'timeout', 'tx_error', 'invalid_reply', 'transport_error', 'cancelled'):
            raise ValueError('Unknown probe outcome')
        if not re.fullmatch(r'[0-9a-fA-F]{8}', row['request_id']):
            raise ValueError('Invalid probe request identifier')
        row['duration_s'] = number(row['duration_s'], 'Probe duration')
        if row['outcome'] == 'success':
            row['latency_ms'] = number(row['latency_ms'], 'Response latency')
            if row['latency_ms'] > row['duration_s']*1000+.01:
                raise ValueError('Latency exceeds the probe duration')
        elif row['latency_ms']:
            raise ValueError('Failed probes cannot have a successful latency')
        for key in ('radio_retry_delta', 'malformed_lines', 'stale_replies'):
            value = number(row[key], key)
            if value != int(value): raise ValueError(key+' must be an integer')
        while recent and recent[0][0] < row['elapsed_s']-30: recent.popleft()
        if row['outcome'] != 'cancelled':
            self.count += 1; self.successes += row['outcome'] == 'success'
            recent.append((row['elapsed_s'], row['outcome'] == 'success'))
        if row.get('rolling_success_pct'):
            row['rolling_success_pct'] = number(row['rolling_success_pct'], 'Rolling success', 0, 100)
        else:
            row['rolling_success_pct'] = 100*sum(ok for _,ok in recent)/len(recent) if recent else ''

    def _make_snapshot(self):
        packets = link.bounded_rows(self.directory/'packets.csv', limit=link.LIMIT)
        for row in packets:
            row['source_verified'] = boolean(row['source_verified'])
            row['valid_sample'] = boolean(row['valid_sample'])
        probes = link.bounded_rows(self.directory/'probes.csv', limit=link.LIMIT)
        # CSV readers return text; the existing live summary expects numeric
        # latency. Keep the reconstructed interface identical to live rows.
        for row in probes:
            if row['latency_ms'] != '': row['latency_ms'] = float(row['latency_ms'])
        return dict(id=self.id, name=self.name, status='imported', detail='; '.join(self.warnings),
            imported=True, original_status=self.original_status, source_override=self.source_override,
            elapsed=self.elapsed, requested_hz=self.rate, probes=self.count, successes=self.successes,
            success_pct=100*self.successes/self.count if self.count else None,
            actual_hz=self.count/self.elapsed if self.elapsed else 0,
            measurement_hz=self.measurements/self.elapsed if self.elapsed else 0,
            packets=packets,
            probe_rows=probes,
            events=link.bounded_rows(self.directory/'events.csv', limit=link.LIMIT),
            rssi=self.last_rssi, age=None if self.last_rssi_at is None else max(0,self.elapsed-self.last_rssi_at),
            method=self.last_method, dropped=self.dropped, skipped=self.skipped,
            rssi_errors=self.rssi_errors, rssi_error=self.last_error, directory=str(self.source_path),
            decimated=any(count > link.LIMIT for count in self.row_counts.values()))

    def snapshot(self):
        result = dict(self._full)
        for key in ('packets', 'probe_rows', 'events'): result[key] = list(result[key])
        return result

    def __del__(self):
        # This object contains no GUI resources. A worker holding it keeps the
        # cache alive; the last owner can release it safely on either thread.
        cache = getattr(self, 'cache', None)
        if cache is not None:
            cache.cleanup()

    def graph_snapshot(self):
        return self.snapshot()

    def export(self, path):
        target = Path(path).resolve()
        if target == self.source_path or self.source_path.is_dir() and target in [self.source_path/name for name in FILES]:
            raise ValueError('Choose a different export path to preserve the original session')
        link.export_bundle(self.raw_directory, target)
