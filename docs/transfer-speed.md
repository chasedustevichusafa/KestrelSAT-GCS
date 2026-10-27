# Transfer-speed test builds — 2026-10-27

The `feature/transfer-speed` branches add negotiated speed profiles to **Remote
Files** and **Camera** downloads, Camera **View**, and file uploads. Image capture
still saves one JPEG to SD; viewing downloads it after capture. There is no live
preview. Keep the confirmed hardware and radio settings for the first comparison.

## Install and compare

1. Download the matching GCS Windows artifact and Lab 8 firmware source artifact
   from successful Actions runs on `feature/transfer-speed`. Extract each ZIP.
2. Open the firmware project in PlatformIO and upload
   `esp32thing_plus_115200_camera_cs27` for the confirmed GPIO27 camera wiring.
   The other three original baud/CS environments remain available. USB startup
   diagnostics identify this firmware as **KFS1 speed-1**.
3. Keep both radio UARTs and the GCS at **115200**, satellite AP=0, ground AP=1
   with GCS API mode, and the working unicast destinations/MAC-ACK settings.
   This firmware and GCS do not reprogram the radios or increase the RF bitrate.
4. Choose one saved image or binary file and download that SAME file with each
   profile below. Keep antenna placement, distance, RSSI polling, baud and all
   other settings constant. Begin with RSSI polling off for the first comparison.
5. Use Transfer Statistics to record each operation or a named multi-operation
   recording. Export CSV and PNG as before. Repeat each profile at least five
   times; retain failures and retry counts as well as successful speeds.
6. Repeat uploads with the same local source, using a fresh remote filename
   each time. Upload read-back verification is retained and takes time after
   useful-byte progress reaches 100%; compare complete operation durations too.

| Transfer profile | Useful bytes/block | Data encoding | Outstanding data requests |
| --- | ---: | --- | ---: |
| Baseline | 128 | Hex | 1 |
| Larger blocks | 256 | Hex | 1 |
| Compact | 256 | Base64 | 1 |
| Fast | 512 | Base64 | 1 |
| Experimental | 512 | Base64 | 2 per batch |
| Auto (default) | 512 with FAST1 firmware; 128 with older firmware | Base64 or legacy hex | 1 |

The first two profiles isolate block size; the two 256-byte profiles isolate
encoding; the final two 512-byte profiles isolate request batching. The selection
is shared by Camera and Remote Files and saved as a preference. Each worker
captures its choice before starting; another tab cannot change an in-flight
transfer's encoding. Changes affect subsequent file operations, not exposure.

## Reading the results

The actual negotiated value, such as `512-B64-batch1` or `128-HEX-batch1`, appears
in single-transfer graph headers and summaries. Session CSV, recording samples,
recording operations and combined recordings include `transfer_profile`.
Diagnostics include requested/actual profiles, chunk size, encoding and batch
size. Auto's legacy fallback is therefore distinguishable from a fast transfer.

512-byte blocks need one-quarter as many data requests as the 128-byte baseline.
Base64 represents three bytes with four characters, versus six hex characters.
These reduce command turnaround and UART/RF payload traffic. The portable suite
prints measured payload-stream byte/request counts for the same 16 KiB input;
those figures exclude XBee envelopes, RF retransmissions and radio turnaround.
Actual B/s and reliability still require your hardware tests. A larger chunk
spans more RF packets; a poor link may favor a smaller profile. Batch 2 is an
experiment on this half-duplex radio and may be slower than Fast on your setup.

Use the existing RSSI freshness rules: a rate of zero and a missing RSSI sample
do not establish a serial disconnection. After comparing profiles, repeat the
best stable one with RSSI enabled and export that comparison separately.

## Protocol and recovery

Legacy `HELLO|1|128`, commands and hex responses are preserved. New firmware
advertises `FAST1` in CAPS. Only after seeing that capability does a new client
use `OPENX(path, chunk, codec)` or `PUTX(path, size, CRC, chunk, codec)`. FILE/READY
echo the profile so the client validates agreement before sending file data.
Explicit fast selections require FAST1; Auto falls back before opening a file.

`READX` and `WRITEX` retain newline-delimited CRC32 records. Base64 is compact
text framing, not a raw binary UART protocol. Frames remain below 1024 bytes.
The firmware accepts 128/256-byte hex and 128/256/512-byte Base64 profiles;
the UI exposes the useful comparison set above. Upload ACKs still require synced
SD writes. Whole-file CRC, independent upload read-back, no-overwrite publication,
atomic local download replacement, bounded retries and cancellation remain.

Batch mode sends at most two data commands, then confirms BOTH before opening
the next batch. Only unconfirmed requests are retried with their original IDs.
Firmware retains two successful data replies independently of metadata replies.
If an earlier command was lost, a later offset can be rejected transiently;
that rejection is not cached, allowing the same command to succeed after repair.
There is no unbounded sliding window or automatic mid-transfer profile switch.

If a profile fails, retain the diagnostics and CSV, select Fast without batching,
then Baseline for a comparison. Do not change radio settings at the same time.
Previous main builds remain available for a complete software rollback.

## Automated checks

- `tests/test_transfer_speed.py`: binary/tail/empty files across all profiles,
  lost requests/replies, combined/fragmented reads, API formats with RSSI,
  legacy fallback, CSV labels and deterministic wire counts.
- Lab 8 `test/test_protocol.py`: actual C++ codec/parser/replay interoperability,
  malformed profiles/Base64, corruption/reordering/loss, SD recovery, full-file
  checksums and twenty consecutive image-sized downloads.
- Full GCS regression runner and all four PlatformIO builds. These establish
  software behavior and compilation, not measured on-air performance.
