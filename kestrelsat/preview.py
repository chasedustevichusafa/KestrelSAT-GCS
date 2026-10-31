"""GUI-independent, one-frame preview cycle using the existing camera firmware.

The satellite still saves and rereads each JPEG on SD. We never delete an image
merely because download succeeded: the GUI must explicitly accept its decoded
frame first. Persisted capture/delete intent survives disconnect and restart.
"""
from pathlib import Path
import re
import threading
import time
import zlib

from . import camera_files as camera
from .remote_files import Cancelled, RemoteError, OperationUncertain
from .image_viewer import decode_image


def new_preview(mode):
    request = camera.new_capture(mode)
    stamp, rest = request['path'][8:].split('_', 1)
    return dict(request, path=f'/images/{stamp}_preview_{rest}', phase='capture')


def pending_preview(value):
    """Only our exact timestamp/resolution/job namespace is eligible for cleanup.

    Gallery contents are never a deletion source. Even edited/corrupt settings
    cannot turn an ordinary camera photograph into a disposable preview frame.
    """
    request = camera.pending_capture(value)
    if request is None:
        return None
    width, height = camera.RESOLUTIONS[request['mode']]
    pattern = rf'/images/\d{{8}}T\d{{6}}[+-]\d{{4}}_preview_{width}x{height}_{request["job"]}\.jpg'
    if not re.fullmatch(pattern, request['path']):
        return None
    phase = value.get('phase', 'capture')
    if phase not in ('capture', 'deleting'):
        return None
    request['phase'] = phase
    if phase == 'deleting':
        mutation = value.get('delete_request')
        if not isinstance(mutation, str) or not re.fullmatch('[0-9a-f]{8}', mutation):
            return None
        request['delete_request'] = mutation
    return request


def next_frame_time(started, interval, finished):
    """Start-to-start target with no accumulated backlog or catch-up burst."""
    if interval not in (5, 10):
        raise ValueError('Preview interval must be 5 or 10 seconds')
    return max(started+interval, finished)


class FrameReceipt:
    """One-owner handoff of a Pillow image from worker to GUI.

    Rendering and cancellation can race. Hold the lock through the GUI's render
    callback so the worker cannot close the image while PhotoImage uses it.
    After acceptance the GUI owns image/file lifetime. Before acceptance the
    worker owns them. An ignored/full event queue expires safely, without DELETE.
    """
    def __init__(self, path, image, metadata):
        self.path, self.image, self.metadata = Path(path), image, metadata
        self.lock = threading.Lock()
        self.event = threading.Event()
        self.accepted = False
        self.finished = False

    def present(self, render):
        with self.lock:
            if self.finished:
                return False
            try:
                render(self)
                self.accepted = True
                return True
            finally:
                self.finished = True
                self.event.set()

    def reject(self):
        with self.lock:
            if not self.finished:
                self.finished = True
                self.event.set()

    def wait(self, cancel, timeout=5):
        deadline = time.monotonic()+timeout
        while not self.event.wait(.02):
            if cancel.is_set() or time.monotonic() >= deadline:
                self.reject()
                break
        return self.accepted


class CaptureTerminal(RemoteError):
    """A definite terminal/rejected job can release its persisted capture intent."""


class GuiRequest:
    """Cancellable worker-to-GUI call; a stale queued checkpoint cannot run late."""
    def __init__(self, value=None):
        self.value = value
        self.lock = threading.Lock()
        self.event = threading.Event()
        self.done = False
        self.result = None
        self.error = None

    def perform(self, callback):
        with self.lock:
            if self.done:
                return
            try:
                self.result = callback(self.value)
            except Exception as exc:
                self.error = exc
            finally:
                self.done = True
                self.event.set()

    def abort(self, error=None):
        def fail(value):
            raise error or Cancelled('Preview stopped before GUI acknowledgment')
        self.perform(fail)

    def wait(self, cancel, timeout=5):
        deadline = time.monotonic()+timeout
        while not self.event.wait(.02):
            if cancel.is_set():
                self.abort()
            elif time.monotonic() >= deadline:
                self.abort(RemoteError('Preview GUI acknowledgment timed out; SD frame retained'))
        if self.error is not None:
            raise self.error
        return self.result


def run_frame(client, request, destination, *, recover, publish, checkpoint,
              progress, download_started, download_finished, metrics, clock=time.monotonic):
    """Own one radio cycle; the caller supplies locking and Tk event delivery.

    publish enqueues a FrameReceipt; checkpoint durably records delete intent on
    the GUI thread and acknowledges it before this worker can send DELETE.
    Neither callback may access Tk directly from this worker. Exceptions retain
    remote files and pending identity. Retrying a deleting intent ONLY queries
    RESULT, because CAMSTATUS's SAVED result outlives the actual file deletion.
    """
    request = pending_preview(request)
    if request is None:
        raise RemoteError('Invalid preview ownership record; inspect settings and SD images')
    client._check_cancel()
    if request['phase'] == 'deleting':
        result = client.request('RESULT', request['delete_request'], expected={'RESULT'})
        if result != ['RESULT', 'DELETE', 'DELETED']:
            raise OperationUncertain('Preview cleanup is unconfirmed. Inspect '+request['path']+
                                     ' in Remote Files before clearing its pending record.')
        return # Confirmed prior deletion; never take a replacement photo here.

    started = clock()
    try:
        initial = None if recover else client.start_capture(request)
    except RemoteError as exc:
        if str(exc).startswith('Satellite:'):
            raise CaptureTerminal(str(exc)) from exc
        raise
    outcome = client.wait_capture(request, initial, poll_interval=.25)
    metrics['capture_s'] = clock()-started
    if outcome['state'] in ('ERROR', 'CANCELLED'):
        raise CaptureTerminal('Preview capture '+outcome['state'].lower()+': '+outcome['error'])
    if outcome['state'] != 'SAVED':
        raise OperationUncertain('Preview job is unknown. Inspect '+request['path']+' before another capture.')
    client._check_cancel()
    destination = Path(destination)
    receipt = None
    image = None
    try:
        client.progress = progress # Capture FIFO counters must not count as downloaded bytes.
        download_started()
        started = clock()
        client.download(request['path'], destination, max_bytes=camera.MAX_IMAGE)
        metrics.update(download_s=clock()-started, bytes=destination.stat().st_size,
                       transfer_profile=client.transfer_profile.label)
        # END verifies what is on SD now. CAMSTATUS describes what THIS capture
        # originally saved. Require both to agree before a file can be treated
        # as disposable; a user may have replaced that path while preview paused.
        checksum = 0
        with destination.open('rb') as handle:
            for block in iter(lambda: handle.read(65536), b''):
                client._check_cancel()
                checksum = zlib.crc32(block, checksum)
        if metrics['bytes'] != outcome['total'] or f'{checksum:08x}' != outcome['crc']:
            raise RemoteError('Preview file changed since capture; SD image retained')
        download_finished()
        client._check_cancel()
        started = clock()
        image = decode_image(destination)
        metrics['decode_s'] = clock()-started
        expected = camera.RESOLUTIONS[request['mode']]
        if image.size != expected:
            raise RemoteError(f'Preview JPEG dimensions {image.size} do not match requested {expected}')
        receipt = FrameReceipt(destination, image, dict(request, **metrics))
        started = clock()
        publish(receipt)
        if not receipt.wait(client.cancel):
            client._check_cancel()
            raise RemoteError('Preview display was not accepted; SD image retained')
        metrics['display_wait_s'] = clock()-started
        client._check_cancel()
        started = clock()

        def before_delete(mutation):
            client._check_cancel()
            checkpoint(dict(request, phase='deleting', delete_request=mutation))
            client._check_cancel()

        # Uses the existing same-ID retries and RESULT recovery. The extra hook
        # records identity BEFORE the first outgoing destructive request.
        client.delete_file(request['path'], before_send=before_delete)
        metrics['cleanup_s'] = clock()-started
    finally:
        if receipt is None or not receipt.accepted:
            if image is not None:
                image.close()
            destination.unlink(missing_ok=True)
