"""Bounded, metadata-only KFS diagnostics; recording never controls a transfer."""
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
