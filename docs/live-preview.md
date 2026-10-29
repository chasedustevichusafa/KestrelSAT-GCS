# Live Preview — 2026-10-29

Live Preview repeatedly captures a still JPEG, saves/verifies it on the satellite,
downloads it with file CRC verification, and displays it inside the new tab.
After the GUI accepts the image, the GCS automatically removes that preview's SD
file. Your ordinary photographs remain on SD. The existing firmware is sufficient;
this update changes the GCS only.

## Start and compare

1. Extract the new Windows GCS artifact and run its executable. Connect using
   your existing ground-radio COM port, baud and API settings. For the confirmed
   setup these are 115200, GCS XBee API/AP=1, ground radio AP=1, satellite AP=0,
   working unicast/MAC acknowledgments and camera CS=GPIO27.
2. Open **Live Preview**. Select a resolution and a **5** or **10** second refresh
   interval. The default is **640×480 / 10 seconds**. Start with **320×240 / 5
   seconds** when testing the shorter interval. Start queries the camera and
   filters the choices to its supported resolutions before taking a picture.
   An unsupported selection is replaced by the first supported resolution.
3. Choose a transfer profile. This is shared with Camera/Remote Files. Your
   reported experimental rate was about 4700 B/s; Auto/Fast was about 3850 B/s.
   Use the same profile when comparing refresh intervals and resolutions.
4. Press **Start**. Preview is off by default and never restarts automatically
   after reconnect or application restart. Resolution, interval and profile
   preferences are remembered; the running state is not.
5. The image fits the panel and resizes with it. The details show timestamp,
   actual JPEG dimensions, bytes, download rate, frame age and achieved refresh
   interval. Image age includes capture/download time and is based on the GCS
   request timestamp. A previous image is marked stale when older than the target
   period, including while its replacement is downloading.
6. Press **Save frame...** to save the displayed JPEG permanently on your PC.
   The file dialog defaults to its timestamp/resolution filename. The clicked
   frame is snapshotted before opening the dialog, so arriving frames cannot
   change which picture is saved.

The interval is a start-to-start target, including camera/SD work, transmission,
display and cleanup. A larger or more detailed JPEG can exceed the deadline.
Only one frame runs at a time; an overrun starts the next cycle when resources
are available, without a backlog or a burst of queued captures. Changing an
interval while waiting recalculates the next deadline. Other changes apply at
the next frame boundary.

## SD cleanup and local storage

Preview filenames look like
`/images/20261029T143000-0600_preview_640x480_1234abcd.jpg`.
They retain GCS local time, UTC offset, resolution and a random capture job ID.
Only the exact job/path generated for this preview is eligible for automatic
deletion. No directory scan or filename-based bulk cleanup is performed.

Deletion requires all of the following:

- The satellite reports that this capture was saved and SD-verified.
- The download agrees with its own complete-file CRC AND the capture's original
  size/CRC. A file replaced under that path while preview paused is retained.
- Pillow fully decodes a JPEG with the requested dimensions and within existing
  8 MiB download / 8 megapixel limits.
- The GUI successfully displays and explicitly accepts the decoded frame.
- The exact DELETE request identity has been saved before transmission.

Local images live in a private directory under `downloads/live_preview/`.
Normally there is one displayed JPEG plus at most one incoming frame. Save uses
one additional temporary snapshot while its dialog is open. Superseded frames
and a cleanly closed session's private directory are removed. Files you save
elsewhere are preserved. Failed local cleanup pauses new captures. A forced
process exit may leave a private cache directory; no broad startup cleanup runs.

## Stop, disconnect and recover

**Stop** disables scheduling and cancels current GCS work. An exposure already
accepted by the satellite may still finish on SD. The last displayed frame stays
available locally. Capture and cleanup intent are saved in settings before their
commands can be sent; an explicit settings-write failure prevents transmission.

If a job remains pending, use **Recover pending frame** after reconnecting. It
checks the original capture identity, downloads/displays the same image if saved,
and performs its normal cleanup. It never starts another exposure or turns the
continuous loop back on. A pending DELETE is recovered by its retained RESULT;
the GCS does not blindly issue another destructive operation.

If the satellite reset, another mutation replaced its retained result, or the
outcome otherwise remains unknown, inspect the exact filename in Camera/Remote
Files. **Clear pending record...** asks for confirmation and clears only local
recovery intent; it never deletes the satellite file. Capture errors, JPEG/CRC
failures, uncertain cleanup and persistent link failures pause preview.

The Camera tab refuses a new still capture while preview intent remains
unresolved. Preview likewise waits for an existing still capture to be resolved.
Active frames exclusively own the existing serial connection. Between frames
manual file operations can use it, and preview waits rather than preempting them.
All Tk controls/images stay on the GUI thread; workers send bounded events.

## Graphs, CSV and diagnostics

Each frame's download feeds the existing Transfer Statistics graphs, RSSI option,
CSV and PNG exports. Those measurements cover the download rather than counting
camera FIFO bytes as downloaded bytes. Start a multi-operation recording there
to collect several preview frames on one timeline, including capture/idle gaps.

A separate cycle CSV is shown in the tab under
`transfer_statistics/preview/`. It records local timestamp/offset, job/path,
resolution, target interval, capture/download/decode/display-wait/cleanup times,
total cycle time, achieved display interval, overrun, bytes, profile and outcome.
Logging failures are displayed and do not authorize a new capture or delete.
Metadata-only JSONL diagnostics remain under `transfer_diagnostics/`.

## Tests and downloads

- `tests/test_live_preview.py`: lifecycle, scheduling, namespace protection,
  display-before-delete, changed-file protection, failed checkpoints, lost
  cleanup replies, recovery, bounded caches, Save/dialog races, queue saturation,
  restart/disconnect behavior and real Tk/API integration.
- `python tests/test_preview_firmware.py --firmware /path/to/Astro331_Lab8_Code`:
  optional POSIX/C++ checks against the unchanged real firmware parser/job service,
  with repeated frames across all transfer profiles and pending-job recovery.
- `tests/run_all.py` includes the portable preview suite. Linux GUI checks use
  `QT_QPA_PLATFORM=offscreen xvfb-run -a python tests/run_all.py`.

Windows artifacts build on `feature/live-camera-preview`, pull requests and main.
The existing Lab 8 `esp32thing_plus_115200_camera_cs27` firmware remains compatible.
Automated checks establish software behavior; use your camera/radios to verify
achieved refresh intervals and long-running stability.
