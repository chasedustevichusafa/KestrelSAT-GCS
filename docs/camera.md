# Remote still-image capture — 2026-10-21

Use matching GCS and Lab 8 firmware builds from `main` after merge, or
`feature/remote-camera` while testing this PR. On 2026-10-22 the owner confirmed
camera operation with GPIO27 and unicast radios and reported no remaining issues
at the agreed stopping point. Measured transfer results and their limits are
documented below. There is no live preview or automatic image stream.

## First test

1. Extract the firmware ZIP and open `KestrelSAT-Lab8` in PlatformIO. Upload
   `esp32thing_plus_115200_camera_cs27` for the confirmed GPIO27/115200 setup.
   `esp32thing_plus_115200` retains GPIO12; default `esp32thing_plus` retains
   GPIO12/9600. Firmware and physical CS wiring must match. Confirm USB startup marker
   `KFS1 camera-2`, followed by camera sensor/model/status output. For upload
   failures with the camera attached, follow the GPIO12 procedure below.
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
file-service firmware running. Check wiring/power, then **Refresh images** to retry
failed initialization (at most once per three seconds, with no active file/camera
job). No reconnect/reset is required for late camera power. A ready camera is not
reset by Refresh. Sensor initialization has a four-second wait budget.

## Hardware and implementation

Lab 5's Arducam Mega 3.0.0 source and MIT license are copied into Lab 8 without
modifying the vendor files. Examples and unrelated platforms' project trees are
not packaged. Camera chip select is GPIO12; SD chip select GPIO5; SPI uses
SCK18/MISO19/MOSI23. These retain the working labs' board configuration. The
camera adapter switches SPI transactions at 1 MHz, releases camera CS before each
SD write and preserves the existing SD clock. Its operation table replaces the
vendor's indefinite sensor/capture waits; no preview thread is started.

The portable capture job reads up to 256 FIFO bytes per loop. Like the vendor
`capture2SD` example, it uses independent `readByte` operations, avoiding burst
resumption/dummy-byte state across SD transactions. A streaming JPEG framer finds
SOI, follows segment lengths through the scan to EOI, and excludes bytes outside
that frame. Split markers and embedded thumbnail markers are handled. The
firmware syncs/closes the extracted JPEG and reads all bytes back to compare its
CRC32 before publication. Saved size/CRC describe the JPEG rather than the FIFO.
Single-byte camera reads trade speed for simpler shared-bus behavior; saving
large resolutions can take longer. SD operations have bounded retries at
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
| `CAMDIAG|job_hex` (optional `CAMDIAG1` capability) | `CAMDETAIL|job_hex|fifo_bytes|read_bytes|saved_bytes|prefix_bytes|first_8_bytes_hex|last_8_bytes_hex|error` |

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

The initial camera build passed all 13 GCS suites and 30 firmware host/PTY tests.
Camera-2 also passes all 13 GCS suites (including 11 camera tests) and adds
real padded-JPEG, split-marker, thumbnail and malformed FIFO cases,
plus a fake-bus check of the production adapter with the unchanged vendor driver.
All 33 firmware host/PTY tests pass; both baud variants and the optional GPIO27
build compile. These establish software behavior, not electrical reliability.

On hardware, begin at 320×240 or 640×480: capture, View, check decoded dimensions,
Download and compare copies, then rename/delete a disposable image. Test a higher
supported resolution, repeated captures, Stop waiting/Check capture and reconnect.
Confirm existing upload/download/statistics behavior with the new firmware. Save
USB camera/SD output and GCS diagnostics for any failure.

## Camera test follow-up — 2026-10-22

The first hardware report showed an API-mode 96×96 capture reaching 1,840/1,840
bytes, then `INVALID_JPEG`, with no retries, timeouts or malformed radio frames.
The original code required the whole FIFO to start/end at the JPEG boundaries.
The vendor's [capture2SD example](https://github.com/ArduCAM/Arducam_Mega/blob/main/examples/ArduinoUNO/capture2SD/capture2SD.ino)
instead finds SOI and stops at EOI. Camera-2 implements streaming framing and
uses that example's single-byte read primitive. This addresses a confirmed
software limitation; the original log has no image bytes, so it does not prove
which padding/readout condition occurred on this camera. Corrupt or incomplete
frames still fail, are cleaned up and are never published as successful images.

New captures record `camera_detail` metadata in the GCS JSONL: FIFO/read/saved
counts, skipped prefix, first/last eight FIFO bytes and terminal error. Refresh
also records model/mode mask/status. These bounded byte samples help distinguish
framing, missing markers and readout faults without recording whole images.
Diagnostics are optional and cannot replace a known capture result; older camera
firmware without CAMDIAG1 remains usable.

The same report described flash verification MD5 mismatches with the camera
attached and intermittent detection. Lab camera CS is **GPIO12**. On the actual
ESP32 board this is MTDI, a boot strap that can select **1.8 V flash power** when
held high at reset, although the board uses 3.3 V flash.
[Espressif documents the resulting flash/boot failure](https://docs.espressif.com/projects/esptool/en/latest/esp32/advanced-topics/boot-mode-selection.html).
This is a plausible electrical cause, not established by the MD5 error alone;
power instability remains possible. Firmware cannot set the pin before the ROM
samples it. Board configuration and flash verification remain unchanged.

For a durable GPIO12 comparison, with **all power disconnected**, move only the
camera **CS** lead from GPIO12 to **GPIO27**, after confirming GPIO27 is unused
on your hardware. Upload **`esp32thing_plus_115200_camera_cs27`**. This optional
environment retains the working board, radio rate, SD and SPI pins, and prints
`camera_cs=27`. Existing environments still use GPIO12. Firmware and physical CS
wiring must match. If flashing still fails, disconnect the camera while unpowered,
upload, then power down again before restoring it. Do not hot-plug camera wiring.
Do not burn flash-voltage eFuses or disable MD5 verification as a workaround.

Keep the camera's existing rated supply connection; verify its exact model and
power wiring before changing voltage. A reported 5 V connection alone does not
establish power stability. If GPIO27 does not resolve upload/boot failures, collect
startup/reset output and check the shared supply, ground, cable and current
capacity. Firmware does not repair a power fault.

For the next hardware test, retain radio/AP settings, use matching camera-2 GCS
and firmware, and start at 320×240. Capture, View and confirm decoded dimensions,
then repeat at 96×96 and 640×480. If detection fails, wait three seconds and Refresh
images. Save the new JSONL on failure; it should now contain `camera_detail`.

## Confirmed hardware setup and transfer results — 2026-10-22

The owner confirmed that camera-2 captures work, GPIO27 works well, and the
system reached a working stopping point. The confirmed configuration is:

- Camera CS on GPIO27, firmware `esp32thing_plus_115200_camera_cs27`.
- Both radio UARTs at 115200; satellite AP=0, ground AP=1 with GCS API mode.
- Both radios use unicast: ground DH/DL are the satellite SH/SL, and satellite
  DH/DL are the ground SH/SL. The supplied lab profile uses DH=0/DL=FFFF broadcast;
  it is a course reference rather than the confirmed transfer configuration.
- MAC acknowledgments enabled (MM=0 in the lab radio firmware); legacy AO=2
  receive indicators remain supported. RSSI polling can remain enabled.

Save both XCTU profiles before changing destinations, copy the peer's hexadecimal
SH/SL exactly, write both radios, and reconnect the GCS so it reads the new
destination. Retain existing network settings. Firmware does not configure radios.

The following user-supplied JSONL logs downloaded the same saved 640×480 JPEG,
`20261022T195938-0600_640x480_da02b0a6.jpg`, with unicast addressing:

| Log | RSSI polling | Operation duration | Protocol timeouts/retries | Outcome |
| --- | --- | --- | --- | --- |
| `kfs-20261023T021016Z-o38jaxio.jsonl` | Off | 22.407 s | 0 / 0 | Downloaded, checksum verified |
| `kfs-20261023T021158Z-r41bqc_5.jsonl` | On | 22.411 s | 0 / 0 | Downloaded, checksum verified |

Both logs represent 260 sequential READ chunks with steady progress. Polling
returned a final RSSI of -21 dBm; that is an endpoint reading, not a session
average. One rejected protocol line was counted during OPEN in the polling-on
run, with no timeout/retry or measurable slowdown. Durations include setup and
completion; successful READ timings are recorded as periodic checkpoints rather
than every request. UTC filenames fall on October 23; the local test date was
October 22.

Earlier broadcast examples took 26.465 s with polling off (one timeout/retry)
and 38.615 s with polling on (four timeouts/retries), using different 640×480
images. Their difference mostly consisted of four-second reply waits. The
same-image unicast pair supports broadcast delivery as a contributor to the
earlier pauses; it does not establish that RSSI polling caused them.

The earlier 83,996-byte image failed with SD `READ_FAILED` at offset 34,816 after
three attempts. Its physical cause was not established, and local SDCHECK results
were not supplied. The owner subsequently reported all seemed fixed, but these
two unicast downloads are not a twenty-transfer reliability study or proof that
every large-image SD failure is eliminated. Keep USB/JSONL diagnostics and the
existing SDCHECK procedure available if an SD error recurs. No SD-clock reduction
was made as part of this handoff; the configured default remains 25 MHz.
