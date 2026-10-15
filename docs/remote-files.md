# Remote SD-card files over XBee

This milestone browses and downloads files already on the satellite SD card,
including JPEGs. Camera capture and live image preview are the next milestone.
The original File → ZMODEM commands remain available for other compatible peers;
use the **Remote Files** tab with the Lab 8 firmware.

## First hardware test

1. In the firmware repository, open
   `Astro-331-2026-Fall/Astro331_Lab8_Code` as the PlatformIO project. Build and
   upload its `esp32thing_plus` environment. It keeps the board identifier and
   wiring from the working labs. Keep the SD card inserted.
2. Start `KestrelSAT_GCS.exe`. On **Connection**, select the ground-station XBee's
   USB COM port and **9600 baud**, 8 data bits, no parity, 1 stop bit. Connect.
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
Downloads restart from the beginning; resumable transfers and SD writes/deletion
are not implemented.

Both XBees must retain their working paired **transparent serial** configuration.
The firmware does not change radio settings. Do not switch only one end's baud
rate. At 9600 baud, downloads take time: the absolute UART ceiling is 960 bytes/s,
while encoding, request/reply pacing and radio overhead reduce useful throughput
further. The progress display reports measured bytes/s. A delay under 0.5 seconds
for an arbitrary file is not achievable on this link.

## Windows executable without a release

The **Windows test build** workflow creates a downloadable executable artifact;
it does not create tags or publish a release. A push to `feature/remote-files`
automatically builds. You can also run it from GitHub:

1. Open the GCS repository → **Actions** → **Windows test build**.
2. Click **Run workflow**, select the branch containing the changes, then run it.
   If GitHub has not registered a newly added manual workflow yet, use the build
   automatically triggered by the feature-branch push.
3. Open the completed green run. Under **Artifacts**, download
   `KestrelSAT-GCS-Windows-<commit>` and unzip it.
4. Run `KestrelSAT_GCS.exe`. `SHA256.txt` records its checksum. Python is not
   required on your PC. The executable is unsigned.

The older **Build Windows executable** workflow publishes a release; it is not
needed for this test build. For a local Windows build, install the requirements,
PyInstaller and Pillow, then run `python -m PyInstaller --noconfirm KestrelSAT_GCS_v1.spec`.

## Protocol and validation

KFS1 uses ASCII lines with request IDs, CRC32-protected frames, hex-encoded paths
and file data. Hex avoids both binary/text ambiguity and `+++` in radio payloads.
The firmware sends one response to each request; the client retries a lost or
corrupted response with the same ID. The firmware replays the previous response
without advancing its file offset twice. A sequential 128-byte chunk transfer
ends with a byte count and whole-file CRC32. This detects accidental corruption;
it is not authentication or encryption.

Frames are bounded to 768 bytes, absolute UTF-8 SD paths to 192 bytes, directory
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
