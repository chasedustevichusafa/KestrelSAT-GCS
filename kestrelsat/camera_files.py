"""Single-shot camera jobs over the existing checked KFS1 transport."""

# READING GUIDE - capture control is separate from image download.
# CAPTURE asks the satellite to save a JPEG on its own SD card. CAMSTATUS polls
# one job; it does not transport pixel data. View/Download later use the ordinary
# file protocol. No preview stream or repeated automatic exposure is created.
#
# The ground computer supplies LOCAL request time plus UTC offset in filenames;
# the satellite has no configured real-time clock. A random job suffix separates
# requests with equal timestamps and allows retries to refer to the same capture.
# The resolution index must match the vendor enum/bitmask, not a pixel count.
#
# The difficult case is an uncertain start reply: the exposure may already be
# running or saved. Reuse the original job ID and query status instead of taking
# another photo. UNKNOWN after reset/replacement asks the operator to inspect SD;
# it is not permission to silently repeat a capture.
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


# The timestamp marks the PC request, not measured shutter time. Its UTC
# offset distinguishes repeated local times around daylight-saving changes.
# The mode index is used on the wire; width/height are descriptive filename
# fields. Preserve the returned job/path together for every retry/check.
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

    # Validate identity, state, path, byte counts and terminal outcome before
    # showing success. An ERROR may report a rejected oversized FIFO length;
    # SAVED has stricter useful-image size/count rules. UNKNOWN has no matching
    # path to validate because the satellite no longer retains that job.
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

    def capture_diagnostics(self, job):
        """Optional metadata; its failure must not replace a known capture result."""
        try:
            if not re.fullmatch('[0-9a-f]{8}', job):
                raise RemoteError('Invalid capture job')
            if 'CAMDIAG1' not in self.capabilities():
                return None
            reply = self.request('CAMDIAG', job, expected={'CAMDETAIL'})
            if (len(reply) != 9 or reply[1] != job or
                    any(not re.fullmatch('[a-fA-F0-9]{16}', text) for text in reply[6:8])):
                raise RemoteError('Invalid camera diagnostics')
            fifo, read, saved, prefix = [unsigned(text, MAX_IMAGE) for text in reply[2:6]]
            if read > fifo or saved > read or prefix > read:
                raise RemoteError('Invalid camera byte counts')
            detail = dict(job=job, fifo_bytes=fifo, read_bytes=read, saved_bytes=saved,
                          prefix_bytes=prefix, first_hex=reply[6], last_hex=reply[7], error=reply[8])
            self._record('camera_detail', **detail)
            return detail
        except Exception as exc:
            self._record('camera_detail_unavailable', error=str(exc))
            return None

    # A valid satellite rejection can be reported directly. An unconfirmed
    # start may have succeeded: query CAMSTATUS for the SAME ID/path instead of
    # generating a new CAPTURE. If even that query fails, expose uncertainty so
    # the operator can reconnect/check rather than accidentally take two photos.
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

    # Waits are cancellation-aware on the GCS, but cancellation here means
    # stop observing, not abort the onboard exposure. Status uses a finite total
    # deadline. A terminal UNKNOWN is returned to the UI for operator inspection,
    # not transformed into an automatic retry with a new image name.
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
