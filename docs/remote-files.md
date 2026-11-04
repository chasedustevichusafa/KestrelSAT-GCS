# Remote SD-card files over XBee

The completed main build browses satellite SD folders and supports verified
upload/download, rename/delete and partial-file cleanup, including saved JPEGs.
[Camera](camera.md) and [Live Preview](live-preview.md) are separate completed tabs.
The original File → ZMODEM commands remain available for other compatible peers;
use the **Remote Files** tab with the Lab 8 firmware.

## Integration status — 2026-10-20

The project owner confirmed that the Windows GCS and matching Lab 8 firmware
work together on the KestrelSAT/XBee hardware for remote file access and transfer.
The steps below remain the verification procedure for new builds or setups.
The owner subsequently confirmed the completed file, camera and preview workflows;
use main artifacts and the final configuration in the [README](../README.md).

## Hardware test procedure

1. In the firmware repository, open
   `Astro-331-2026-Fall/Astro331_Lab8_Code` as the PlatformIO project. Build and
   upload `esp32thing_plus_115200_camera_cs27` for the confirmed GPIO27 camera
   wiring. It retains the working lab board identifier. Keep the SD card inserted.
   Other baud/CS environments remain available for matching original wiring.
2. Start `KestrelSAT_GCS.exe`. On **Connection**, select the ground-station XBee's
   USB COM port and **115200 baud**, 8 data bits, no parity, 1 stop bit,
   **XBee API (AP=1)**. Keep satellite AP=0 and working unicast destinations.
3. Open **Remote Files** and click **Refresh / Open**. The path starts at `/`.
   Double-click a folder, use **Up** to return, or enter an absolute path and
   press Enter. The list shows names, folders, and file sizes.
4. Select a small known text file and click **Download…**. Choose its destination.
   Wait for **Verified and saved**. Compare it with the original SD-card file.
5. Repeat with a JPEG, an empty file, and a file in a subfolder. Try **Cancel**
   during a larger download, then Refresh and retry. Unplug/reconnect the radio
   during a transfer to confirm the failure is reported and a fresh attempt works.

The tab shares the Connection tab's COM port. Ordinary sends and ZMODEM transfers
are blocked during a remote operation. Directory browsing and downloads run on a
worker; the UI stays responsive. A canceled/failed download removes its temporary
`.part` file and leaves an existing destination unchanged. Replacement happens
only after whole-file verification; the save dialog asks about overwriting.
Disconnect clears the listing, so reconnect and Refresh before selecting files.
Downloads and uploads restart from the beginning after interruption; resumption
is not implemented. Verified [uploads](remote-upload.md) and protected
[rename/delete](remote-file-management.md) are available in the current build.

Use the confirmed satellite **AP=0**, ground **AP=1 / GCS API** and unicast
setup; both-transparent file transfers are also supported. Firmware does not
change radio settings. Match each local firmware/GCS UART to its radio. For the
optional 9600 comparison, the absolute 8N1 UART ceiling is 960 bytes/s,
while encoding, request/reply pacing and radio overhead reduce useful throughput
further. The progress display reports measured bytes/s. A delay under 0.5 seconds
for an arbitrary file is not achievable on this link.

## Windows executable without a release

The **Windows test build** workflow creates a downloadable executable artifact;
it does not create tags or publish a release. Pushes and merges into `main`
automatically build a fresh executable. Relevant pull requests also validate
and package the app; manual runs remain available. You can run it from GitHub:

1. Open the GCS repository → **Actions** → **Windows test build**.
2. Click **Run workflow**, select the branch containing the changes, then run it.
   For the completed project select `main`; use a specific available ref for
   a deliberate comparison build.
3. Open the completed green run. Under **Artifacts**, download
   `KestrelSAT-GCS-Windows-<commit>` and unzip it.
4. Run `KestrelSAT_GCS.exe`. `SHA256.txt` records its checksum. Python is not
   required on your PC. The executable is unsigned.

The older **Build Windows executable** workflow publishes a release; it is not
needed for this test build. For a local Windows build, install the requirements,
PyInstaller and Pillow, then run `python -m PyInstaller --noconfirm KestrelSAT_GCS_v1.spec`.

## Protocol and validation

KFS1 uses ASCII lines with request IDs and CRC32-protected frames. Paths and
Baseline data use hex; FAST1 negotiates larger hex/Base64 blocks. See
[transfer profiles](transfer-speed.md). Hex avoids `+++` in legacy data payloads.
The firmware sends one response to each request; the client retries a lost or
corrupted response with the same ID. The firmware replays confirmed responses
without advancing its file offset twice; FAST1 retains two data replies for its
bounded batch mode. A sequential legacy 128-byte chunk transfer
ends with a byte count and whole-file CRC32. This detects accidental corruption;
it is not authentication or encryption.

Frames are bounded to 1024 bytes, absolute UTF-8 SD paths to 192 bytes, directory
listings to 4096 entries, and files to 4 GiB minus one byte. One directory entry
is requested per exchange. Card contents must stay unchanged while browsing or
transferring. Each file is held open read-only during its download. Heartbeats
pause during protocol activity and resume after 30 seconds idle; idle file
sessions are closed then as well. Missing cards produce an error rather than
preventing radio startup; insert/check the card and reset the board to retry SD
initialization. New firmware boots without waiting for a USB serial monitor.

Run the GCS suites using `QT_QPA_PLATFORM=offscreen xvfb-run -a python tests/run_all.py`
on Linux, or `python tests/test_remote_files.py` on Windows. Cross-language tests
in Lab 8 compile the actual firmware protocol engine with a host filesystem
adapter and drive it with the GCS client over PTYs. They cover corrupt, dropped,
duplicated and truncated replies, binary/empty files, nested directories, final
checksum failure, cancellation, and disconnects. These checks verify protocol
compatibility, not physical radio reliability; perform the hardware test above.

## Verified uploads — 2026-10-21

Current main builds include Upload and Remove partial controls in
this tab. See [upload setup, protocol, recovery and hardware tests](remote-upload.md).
Existing file names are preserved; uploads publish only after SD read-back verification.

## Delete and rename — 2026-10-21

Current `main` builds include file-only Delete and Rename
controls. See [setup, limitations and lost-reply recovery](remote-file-management.md).
