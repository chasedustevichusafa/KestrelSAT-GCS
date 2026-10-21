# Verified satellite uploads — 2026-10-21

Use matching GCS and Lab 8 builds from `feature/remote-upload`. This extends the
working stability build. The project owner confirmed the preceding stability
build worked on hardware and subsequently confirmed the upload build works.
The current [file-management build](remote-file-management.md) includes these uploads.

## Upload a file

1. Extract the matching firmware source artifact. In PlatformIO, upload
   `esp32thing_plus_115200` for the existing 115200 radios. The USB startup line
   should contain `KFS1 upload-1`. The default environment remains 9600.
2. Extract and launch the matching Windows GCS. Keep the working radio pairing,
   UART rate and mode: satellite AP=0; ground AP=1 and GCS XBee API (AP=1) for
   RSSI, or ground AP=0 and GCS Transparent. Legacy AO=2 output is supported.
3. Open **Remote Files**, browse to the destination folder and **Refresh / Open**.
   Upload controls become available after the firmware advertises upload support.
   Older firmware continues to support browsing/downloads and displays an update
   message for uploads.
4. Choose **Upload...**, select a local file, then accept or edit its remote name.
   The filename dialog shows the destination folder. Existing names are rejected,
   including case-insensitive SD aliases. Choose a new name to keep both files.
5. Wait for **Upload verified and saved**. The folder refreshes automatically.
   Download the new file and compare its contents with the local original during
   hardware testing. A folder-refresh failure after confirmed completion is
   reported separately from upload failure.

Uploads transfer files into existing folders; this version does not create
folders or replace files. Both directions restart after interruption rather than
resume. Maximum file size is 4 GiB minus one byte; paths are limited to 192 UTF-8
bytes and must leave room for the temporary filename in the destination folder.
Use FAT-compatible names without `<>:"|?*`, trailing dots/spaces or a `.kfs-`
prefix (case-insensitive). The reserved prefix identifies upload temporary files.

## Reliability and cleanup

The GCS calculates the source CRC32 before starting and checks the sent stream
and source metadata for changes. The satellite creates an exclusive
`.kfs-xxxxxxxx.part` file in the target folder. Every 128-byte WRITE has a checked
frame and offset; acknowledgments advance progress only after a full write and
SD sync. Transient write failures retry the same range up to three times; the
SD adapter reopens only that session's temporary file before retrying. Duplicate
writes replay an acknowledgment without appending data or counting it twice.

After all data is received, the satellite closes/reopens the temporary file and
reads it back in bounded batches. Only the correct size, received-stream CRC and
stored-data CRC permit COMMIT to rename it to the final filename. Existing files
are checked again at publication and are preserved. Read-back batches keep
progress and cancellation available during verification.

Cancel, idle expiry and known failures attempt to remove the temporary file.
After a reset, power interruption or failed cleanup, **Refresh** and select the
`.kfs-xxxxxxxx.part` entry, then **Remove partial...**. The dialog confirms the
exact path. The firmware restricts cleanup to reserved temporary names, rejects
directories and refuses cleanup during an active session. The upload-only build restricts deletion to partials; the newer file-management
build adds a separate confirmed Delete control for ordinary files. Filesystem rename is not a power-loss transaction;
after a reset, inspect the folder/card rather than assuming completion.

A lost COMMIT acknowledgment is retried idempotently. If needed, the GCS asks for
the retained completion status, even when Cancel raced the reply. A confirmed
stored result is success. If publication cannot be confirmed or ruled out, the
GCS explicitly reports an unknown completion state: refresh and download/verify
the destination before retrying. The last completed result is held in RAM until
a later completion; a satellite reset loses that status.

## Measurements and diagnostics

Upload sessions use the existing **Transfer Stats** live rate/RSSI graphs and
CSV/PNG exports. Useful upload bytes count only acknowledged payload offsets,
excluding retries. Checksum preparation and SD verification count toward elapsed
time and average rate; verification intervals show zero new upload throughput.
Directory refresh after completion is excluded from the upload session.

RSSI remains the ground receiver's latest received-packet strength, sampled at
most once per second in API mode; it does not measure the satellite receiver's
RSSI. SD retry attempts are visible in the satellite USB log and are distinct
from GCS wire retry counts.

Metadata-only captures under `transfer_diagnostics/` include WRITE/VERIFY offsets,
timing, retries, final outcomes and radio diagnostics. They exclude file data,
sample successful checkpoints once per second and rotate at 2 MiB with one
backup. Recording failures do not abort transfers. The USB log adds SD_WRITE
failures/recoveries and upload publication/cleanup messages. Existing SDCHECK
local read diagnostics remain available with the GCS disconnected.

## Protocol extension

HELLO remains `HELLO|1|128`, preserving older download clients. New commands use
the existing KFS1 frame checksum and request IDs:

| Command | Purpose / successful response |
| --- | --- |
| CAPS | `CAPS|UPLOAD1|128` capability check |
| PUT pathHex size crc32 | Create temporary file; `READY|token|size` |
| WRITE token offset dataHex | Write/sync up to 128 bytes; `ACK|next|size` |
| VERIFY token offset | Read back up to 1024 bytes; `CHECKED|next|size` |
| COMMIT token | Verify totals and publish; `STORED|size|crc32` |
| STATUS token | Active position or retained STORED result |
| ABORT token | Clean unfinished file; ABORTED result or retained STORED result |
| CLEAN pathHex | Remove a reserved inactive temporary file; REMOVED |

A permanent write/verification failure closes the session and reports diagnostic
fields. A session permits one direction at a time. Radio settings are never
changed by these commands. Local files and transmitted data are streamed with
bounded buffers rather than loaded into firmware RAM.

## Verification

The regression suites cover real Tk upload/cleanup controls, capability fallback,
statistics direction, source/binary/empty files, lost READY/WRITE/COMMIT replies,
duplicate requests, partial/permanent SD writes, read-back corruption, space and
sync failures, cancellation during writing/verification, late cancellation after
COMMIT, existing-name collisions and stale temporary cleanup. The actual C++
service runs twenty upload/download round trips with injected write failures;
the legacy API simulation runs twenty roughly 14 KB round trips with RSSI polling.

For hardware acceptance, start with an empty file and a small known file, then a
14 KB JPEG and a larger binary. Compare original/uploaded/downloaded contents,
test Cancel and disconnect, and repeat twenty times per supported mode with API
RSSI polling on/off. Save diagnostics and rate exports. Automated simulation and
successful firmware compilation do not establish physical upload reliability.
