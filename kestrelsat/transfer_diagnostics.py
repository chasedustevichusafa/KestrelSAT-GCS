"""Bounded, metadata-only KFS diagnostics; recording never controls a transfer."""

# READING GUIDE - diagnostic logging is observational and bounded.
# Each operation gets a uniquely named UTC JSONL file with context, request IDs,
# periodic healthy checkpoints, failures/retries and its final outcome. File data
# payloads are excluded; optional camera boundary samples are small explicit
# metadata supplied by the camera protocol. They are not an image archive.
#
# Healthy READ/WRITE/VERIFY request rows are suppressed and replies throttled to
# one checkpoint per second. Failure/retry events are retained, so the log is
# useful for timeout analysis but is NOT a complete packet trace or per-chunk
# latency distribution. Radio snapshots may contain connection-lifetime counters.
# Rotation keeps one backup; a write error disables logging without controlling
# whether the transfer succeeds or fails.
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time


class Capture:
    def __init__(self, context, directory='transfer_diagnostics', max_bytes=2*1024*1024):
        self.context = dict(context, client='KFS1 camera-2')
        self.max_bytes = max_bytes
        self.path = None
        self.stream = None
        self.error = ''
        self._last_reply = -float('inf')
        try:
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            prefix = datetime.now(timezone.utc).strftime('kfs-%Y%m%dT%H%M%SZ-')
            self.stream = tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                prefix=prefix, suffix='.jsonl', dir=directory, delete=False)
            self.path = Path(self.stream.name)
            self.record('started')
        except OSError as exc:
            self.error = str(exc)

    # Logging must not become a second transfer state machine. Throttle healthy
    # checkpoints, retain failures, rotate at the configured bound and catch disk
    # errors. The final status is supplied by the caller; a JSONL write error
    # never authorizes retrying or cancelling a satellite command.
    def record(self, event, **details):
        if self.stream is None or self.error:
            return
        now = time.monotonic()
        # One successful read checkpoint per second; always keep failures/retries.
        if event in ('request', 'reply') and details.get('operation') in ('READ', 'WRITE', 'VERIFY'):
            if event == 'request' or now-self._last_reply < 1:
                return
            self._last_reply = now
        try:
            row = dict(self.context, **details)
            row.update(event=event, utc=datetime.now(timezone.utc).isoformat())
            line = json.dumps(row, ensure_ascii=True)+'\n'
            if self.stream.tell()+len(line) > self.max_bytes:
                self.stream.close()
                os.replace(self.path, str(self.path)+'.1')
                self.stream = self.path.open('w', encoding='utf-8')
            self.stream.write(line)
            self.stream.flush()
        except (OSError, ValueError, TypeError) as exc:
            self.error = str(exc)

    def close(self, status, **details):
        self.record('finished', status=status, **details)
        if self.stream is not None:
            try:
                self.stream.close()
            except OSError as exc:
                self.error = str(exc)
