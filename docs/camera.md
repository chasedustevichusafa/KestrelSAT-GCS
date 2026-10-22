# Remote still-image capture — 2026-10-21

Use matching `feature/remote-camera` builds of the GCS and Lab 8 firmware. The
owner confirmed that the preceding file-management and statistics builds work;
physical camera/SD integration testing is pending for this feature. There is no
live preview or automatic image stream.

## First test

1. Extract the firmware ZIP and open `KestrelSAT-Lab8` in PlatformIO. Upload
   `esp32thing_plus_115200` for the current 115200 radios. The default
   `esp32thing_plus` environment remains 9600. Confirm USB startup marker
   `KFS1 camera-1`, followed by camera sensor/model/status output.
2. Keep existing board, wiring, SD clock and radio configuration. The satellite
   XBee remains AP=0. Ground AP=1 pairs with GCS **XBee API (AP=1)**, or ground
   AP=0 with GCS **Transparent**. Legacy AO=2 indicators remain supported.
3. Launch the matching Windows executable, connect at the working baud rate,
   then open **Camera → Refresh images**. The page shows the detected model,
   camera/SD status and JPEGs under `/images`. An absent images folder before
   the first capture is normal. Capture creates it if needed.
4. Select a resolution and **Capture image**. Default is 640×480 when supported.
   The dropdown contains only modes advertised by the detected camera. Wait
   through Capturing, Saving and Verifying until **Captured, SD-verified and saved**.
5. The new file is selected in the gallery. **View** downloads it through KFS1,
   verifies the transfer checksum, then opens a static image window. **Fit**,
   **−**, **+** and **100%** change the display; scrollbars reveal the original
   image at full size. Closing the viewer leaves its downloaded copy intact.
6. **Download...** saves to a chosen local destination. **Rename...** keeps a
   `.jpg`/`.jpeg` name in `/images`, rejects existing names and keeps the SD file
   in that folder. **Delete...** permanently removes the selected satellite
   image after confirming its full path. Local downloaded copies remain separate.

## Naming and viewing

Names use the **GCS computer's local capture-request time**, UTC offset, requested
resolution and unique job suffix. For example:

```text
/images/20261021T153045-0600_640x480_a14f9082.jpg
```

This is the request time, not a measured satellite shutter time. The lab firmware
has no configured real-time clock. The offset distinguishes daylight-saving-time
changes; the unique suffix handles requests within the same second. Existing SD
files are never overwritten. Renaming can change the generated name; the gallery
extracts resolution from generated names, while the viewer shows decoded dimensions.

View starts a fresh verified download on each click. Copies are kept in distinct
`downloads/camera/view-…/` folders beneath the launch directory. They are not
silently reused or deleted. Up to four image windows can be open. Viewing is
limited to 8 MiB and 8 megapixels, with zoom up to original size; oversized files
can still be saved using Download. Non-JPEG or corrupt image decoding reports an
error while preserving a successfully verified download. Camera decoding uses
pinned Pillow; the serial monitor remains able to launch if it is missing.

## Interrupted or failed capture

The GCS saves the pending job ID/path to its settings before sending CAPTURE.
Retries use the same job ID. The satellite retains its last job and result
independently of ordinary file requests. A lost start reply is recovered by
querying status, never by starting a fresh capture with a new ID.

**Stop waiting** stops the GCS wait, not the onboard capture. Disconnecting or
closing the GCS also lets the satellite finish saving. Reconnect and use
**Check capture** before requesting another image. Pending intent survives a GCS
restart when its settings can be saved. A satellite reset or a newer camera job
loses/replaces the retained status; UNKNOWN tells you to Refresh and inspect
`/images` before another capture. The client does not automatically resubmit an
unknown job. Losing the radio connection does not delete a saved image.

Captures use exclusive `.kfs-xxxxxxxx.part` files beside the final image. Failures
attempt cleanup; power loss or SD faults can leave a partial. Inspect it using
**Remote Files**, then **Remove partial...** when appropriate. Ordinary Delete
excludes reserved partials. An error ending in `CLEANUP_FAILED` means cleanup
needs checking. SD rename/publication is not a power-loss transaction.

Missing cameras, initialization/control/capture timeouts, unsupported resolution,
SD unavailability, insufficient space, failed writes/read-back and name collisions
are explicit errors. The camera's register waits are bounded; JPEG capture polls
cooperatively and times out after 15 seconds without completion. The whole job
has a 120-second limit. File reads/writes/deletion are blocked while a capture is
active; status and abort queries remain available. Camera faults leave the normal
file-service firmware running. Check wiring/power and reset to retry initialization.

## Hardware and implementation

Lab 5's Arducam Mega 3.0.0 source and MIT license are copied into Lab 8 without
modifying the vendor files. Examples and unrelated platforms' project trees are
not packaged. Camera chip select is GPIO12; SD chip select GPIO5; SPI uses
SCK18/MISO19/MOSI23. These retain the working labs' board configuration. The
camera adapter switches SPI transactions at 1 MHz, releases camera CS before each
SD write and preserves the existing SD clock. Its operation table replaces the
vendor's indefinite sensor/capture waits; no preview thread is started.

The portable capture job saves 256-byte FIFO blocks, syncs/closes the complete
temporary file, checks length and JPEG start/end markers, then reads all bytes
back and compares CRC32 before publication. SD operations have bounded retries at
the same offset; failed writes never reread/advance the camera FIFO for retries.
Full JPEG decoding happens on the GCS when View is requested. No full image buffer
is allocated on the satellite.

KFS1 adds `CAMERA1` to CAPS, retaining HELLO version 1 and 128-byte file chunks:

| Request | Reply |
| --- | --- |
| `CAMINFO` | `CAMERA|model|resolution_bitmask|camera_error|SD_READY_or_SD_UNAVAILABLE` |
| `CAPTURE|job_hex|mode_index|path_hex` | `CAMJOB|job_hex|state|done|total|crc32|path_hex|error` |
| `CAMSTATUS|job_hex` | Same job reply, or UNKNOWN |
| `CAMABORT|job_hex` | Same job reply after best-effort cancellation; an already saved job remains SAVED |

The GCS exposes Stop waiting and Check capture; CAMABORT is a protocol operation,
not an automatic action on disconnect. Capture paths are restricted to a single
`.jpg` file in `/images`; jobs reject active file sessions and existing target/
temporary files. CRCs protect all frames. RAM job retention is limited to the last
job until another job or reset. File transfers remain compatible with older GCS
clients, and the new GCS leaves Capture disabled against older firmware.

## Statistics, tests and hardware acceptance

View/Download use the existing download speed/RSSI sessions and CSV/PNG exports.
Named multi-operation recordings include camera start, terminal outcome and folder
activity markers. Capture/save time is metadata/idle time for useful file throughput;
image download time is a transfer. Capture itself adds no RSSI polling. Bounded
metadata-only diagnostics are saved in `transfer_diagnostics/`.

Automated tests cover the actual firmware job through the real Python client:
normal save/read-back/download, duplicate/lost requests, busy sessions, collisions,
absent hardware/card, capture/job timeouts, invalid image lengths/JPEG boundaries,
write/sync/read-back faults and cleanup failures. GCS tests use real JPEGs, real Tk
controls and legacy XBee API framing to cover capture, disconnect recovery, viewing,
zoom/close, rename/delete, size limits and transfer statistics. Compilation and
simulation do not establish physical camera operation.

All 13 GCS suites (including 10 camera tests) and 30 firmware host/PTY tests
passed. Both 9600 and 115200 firmware variants compile.

On hardware, begin at 320×240 or 640×480: capture, View, check decoded dimensions,
Download and compare copies, then rename/delete a disposable image. Test a higher
supported resolution, repeated captures, Stop waiting/Check capture and reconnect.
Confirm existing upload/download/statistics behavior with the new firmware. Save
USB camera/SD output and GCS diagnostics for any failure.
