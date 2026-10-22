"""Single-shot camera jobs over the existing checked KFS1 transport."""
from dataclasses import dataclass
from datetime import datetime
import re
import secrets
import time

from .remote_files import Client, RemoteError, OperationUncertain, unsigned, path_hex, upload_path_hex

RESOLUTIONS = ((96, 96), (128, 128), (160, 120), (320, 240), (320, 320), (640, 480),
               (800, 600), (1024, 768), (1280, 720), (1280, 1024), (1600, 1200),
               (1920, 1080), (2048, 1536), (2592, 1944))
MAX_IMAGE = 8*1024*1024
STATES = {'CAPTURING', 'SAVING', 'VERIFYING', 'SAVED', 'ERROR', 'CANCELLED', 'UNKNOWN'}


@dataclass(frozen=True)
class CameraInfo:
    model: str
    mask: int
    error: str
    sd_ready: bool

    @property
    def ready(self):
        return self.error == 'NONE' and bool(self.mask)

    @property
    def modes(self):
        return [index for index in range(len(RESOLUTIONS)) if self.mask & (1 << index)]


def new_capture(mode, now=None):
    if not isinstance(mode, int) or mode not in range(len(RESOLUTIONS)):
        raise RemoteError('Choose a supported resolution')
    now = datetime.now().astimezone() if now is None else now
    if now.utcoffset() is None:
        raise RemoteError('Capture timestamp needs a local timezone offset')
    job = f'{secrets.randbits(32):08x}'
    width, height = RESOLUTIONS[mode]
    path = f'/images/{now:%Y%m%dT%H%M%S%z}_{width}x{height}_{job}.jpg'
    return dict(job=job, mode=mode, path=path, requested_local=now.isoformat(timespec='seconds'))


def pending_capture(value):
    try:
        if (not isinstance(value, dict) or not isinstance(value['job'], str) or
                not isinstance(value['mode'], int) or not isinstance(value['path'], str) or
                not isinstance(value['requested_local'], str) or not re.fullmatch('[0-9a-f]{8}', value['job']) or
                value['mode'] not in range(len(RESOLUTIONS)) or not image_path(value['path'])):
            return None
        return {key: value[key] for key in ('job', 'mode', 'path', 'requested_local')}
    except (KeyError, TypeError, UnicodeError, RemoteError):
        return None


def image_path(path):
    path_hex(path)
    return path.startswith('/images/') and '/' not in path[8:] and path.lower().endswith(('.jpg', '.jpeg'))


class CameraClient(Client):
    def camera_info(self, capabilities=None):
        if 'CAMERA1' not in (self.capabilities() if capabilities is None else capabilities):
            raise RemoteError('Capture requires the camera firmware update')
        reply = self.request('CAMINFO', expected={'CAMERA'})
        if len(reply) != 5 or reply[4] not in ('SD_READY', 'SD_UNAVAILABLE'):
            raise RemoteError('Invalid camera information')
        return CameraInfo(reply[1], unsigned(reply[2], (1 << len(RESOLUTIONS))-1), reply[3], reply[4] == 'SD_READY')

    def _job(self, reply, job, path):
        if (len(reply) != 8 or reply[0] != 'CAMJOB' or reply[1] != job or reply[2] not in STATES or
                not re.fullmatch('[0-9a-fA-F]{8}', reply[5])):
            raise RemoteError('Invalid capture status')
        state = reply[2]
        try:
            remote = bytes.fromhex(reply[6]).decode('utf-8')
        except (ValueError, UnicodeError) as exc:
            raise RemoteError('Invalid capture path') from exc
        if state != 'UNKNOWN' and (remote != path or not image_path(remote)):
            raise RemoteError('Capture reply does not match the requested image')
        done = unsigned(reply[3], MAX_IMAGE)
        total = unsigned(reply[4], 0xffffffff if state == 'ERROR' else MAX_IMAGE)
        if done > total or state == 'SAVED' and (total < 4 or done != total or reply[7] != 'NONE'):
            raise RemoteError('Invalid saved-image size or outcome')
        return dict(job=job, state=state, done=done, total=total, crc=reply[5].lower(), path=remote, error=reply[7])

    def capture_status(self, job, path):
        if not re.fullmatch('[0-9a-f]{8}', job) or not image_path(path):
            raise RemoteError('Invalid capture job')
        return self._job(self.request('CAMSTATUS', job, expected={'CAMJOB'}), job, path)

    def start_capture(self, pending):
        pending = pending_capture(pending)
        if pending is None:
            raise RemoteError('Invalid capture request')
        job, path = pending['job'], pending['path']
        try:
            reply = self.request('CAPTURE', job, pending['mode'], upload_path_hex(path), expected={'CAMJOB'})
            return self._job(reply, job, path)
        except RemoteError as exc:
            if str(exc).startswith('Satellite:'):
                raise
            # Never send a new CAPTURE after uncertainty. Query this job only.
            try:
                return self.capture_status(job, path)
            except Exception:
                raise OperationUncertain('Capture acceptance is unknown. Use Check capture; do not request another image yet.') from exc

    def wait_capture(self, pending, initial=None, poll_interval=1.0, timeout=150.0):
        job, path = pending['job'], pending['path']
        deadline = time.monotonic()+timeout
        result = initial
        try:
            while True:
                self._check_cancel()
                if result is None:
                    result = self.capture_status(job, path)
                state = result['state']
                self.progress(done=result['done'], total=result['total'], elapsed=0,
                              status=f'{state.title()}: {result["done"]:,} / {result["total"]:,} bytes')
                if state in ('SAVED', 'ERROR', 'CANCELLED', 'UNKNOWN'):
                    return result
                if time.monotonic() >= deadline:
                    raise OperationUncertain('Capture is still unconfirmed. Use Check capture to recover its result.')
                if self.cancel.wait(poll_interval):
                    self._check_cancel()
                result = None
        except OperationUncertain:
            raise
        except Exception as exc:
            raise OperationUncertain('Stopped waiting for capture. It may still finish on the satellite. '
                                     'Reconnect if needed and use Check capture. '+str(exc)) from exc
