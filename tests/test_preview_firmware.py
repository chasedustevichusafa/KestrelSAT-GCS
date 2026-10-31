"""Optional POSIX integration: actual unchanged Lab 8 C++ camera/file service.

Run manually alongside the portable GUI runner:
    python tests/test_preview_firmware.py --firmware /path/to/Astro331_Lab8_Code
Uses the firmware's existing host compiler/PTY fixture; no firmware files change.
"""
import argparse
from pathlib import Path
import importlib.util
import io
import sys
import unittest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kestrelsat import preview, camera_files as camera, remote_files as files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--firmware', type=Path, default=ROOT.parent/'Lab-8-Wireless-Image-Capture-and-Transmission/Astro-331-2026-Fall/Astro331_Lab8_Code')
    args = parser.parse_args()
    path = args.firmware/'test/test_protocol.py'
    # The firmware fixture has its own CLI parser. Give it only its supported
    # GCS argument while importing, then restore this script's arguments.
    old = sys.argv
    sys.argv = [str(path), '--gcs', str(ROOT)]
    try:
        spec = importlib.util.spec_from_file_location('firmware_preview_fixture', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.argv = old

    class PreviewFirmwareTests(module.FirmwareProtocolTests):
        def test_real_camera_cycle_all_transfer_profiles_and_repeated_cleanup(self):
            original = (self.sd/'images/photo.jpg').read_bytes()
            for profile in files.PROFILE_LABELS:
                size = (320, 240)
                with io.BytesIO() as stream:
                    Image.new('RGB', size, (50, 100, 150)).save(stream, 'JPEG')
                    (self.sd/'fifo.bin').write_bytes(stream.getvalue())
                client = camera.CameraClient(self.client('camera-fixture').port, timeout=.12, profile=profile)
                for number in range(3):
                    request = preview.new_preview(3)
                    records = []
                    metrics = {}
                    images = []
                    def present(receipt):
                        self.assertTrue((self.sd/request['path'][1:]).exists())
                        receipt.present(lambda frame: images.append(frame.image))
                    try:
                        preview.run_frame(client, request, self.root/'preview.jpg', recover=False,
                            publish=present, checkpoint=records.append, progress=lambda **data: None,
                            download_started=lambda: None, download_finished=lambda: None, metrics=metrics)
                        self.assertFalse((self.sd/request['path'][1:]).exists())
                        self.assertEqual(records[-1]['phase'], 'deleting')
                        self.assertEqual(metrics['bytes'], (self.root/'preview.jpg').stat().st_size)
                    finally:
                        for image in images: image.close()
            self.assertEqual((self.sd/'images/photo.jpg').read_bytes(), original)
            self.assertFalse(list((self.sd/'images').glob('*_preview_*')))
            self.assertFalse(list((self.sd/'images').glob('.kfs-*.part')))

        def test_real_firmware_pending_capture_recovery_has_no_new_exposure(self):
            with io.BytesIO() as stream:
                Image.new('RGB', (320, 240)).save(stream, 'JPEG')
                (self.sd/'fifo.bin').write_bytes(stream.getvalue())
            client = camera.CameraClient(self.client('camera-fixture').port, timeout=.12, profile='batch2')
            request = preview.new_preview(3)
            initial = client.start_capture(request)
            client.wait_capture(request, initial, poll_interval=.001)
            # A CAPTURE retry after deleting its saved image would still report
            # the retained result. Recovery must only inspect the SAME job.
            images = []
            try:
                preview.run_frame(client, request, self.root/'recovered.jpg', recover=True,
                    publish=lambda receipt: receipt.present(lambda frame: images.append(frame.image)),
                    checkpoint=lambda record: None, progress=lambda **data: None,
                    download_started=lambda: None, download_finished=lambda: None, metrics={})
                self.assertFalse((self.sd/request['path'][1:]).exists())
                self.assertEqual(list((self.sd/'images').glob('*_preview_*')), [])
            finally:
                for image in images: image.close()

    # Select these two new tests only; the 39-test firmware suite runs separately.
    suite = unittest.TestSuite(PreviewFirmwareTests(name) for name in (
        'test_real_camera_cycle_all_transfer_profiles_and_repeated_cleanup',
        'test_real_firmware_pending_capture_recovery_has_no_new_exposure'))
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
