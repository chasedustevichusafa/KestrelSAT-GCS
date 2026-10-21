# File-transfer stability — 2026-10-21

Both repositories use `fix/file-transfer-stability`, based on the fork main
branches containing the merged transfer-statistics and high-baud work. Keep these changes separate from
main while testing on the satellite.

## Findings and changes

The hardware report is approximately 95% failures at varying offsets in XBee
API mode with a roughly 14 KB file, followed by `READ_ERROR`. The checked-in
service uses `READ_FAILED`, not `READ_ERROR`; the diagnostic firmware startup
marker and complete GCS/USB logs are needed to reconcile the exact deployed
build and error source. No hardware root cause has been confirmed.

The previous firmware made one SD read per 128-byte chunk. A short read, zero
read, or read error immediately closed the session and returned `READ_FAILED`.
The host fault-injection tests reproduced this behavior. This establishes a
software failure path, not the physical cause of the reported random failures.

The service now makes at most three attempts for the exact same chunk. The SD
adapter restores file position and FAT traversal state before a retry, with a
10 ms pause. The protocol offset and running CRC advance only after a full
chunk is read. Duplicate RF requests still replay the cached response without
reading or advancing the file again. After three unsuccessful attempts, the
session closes and returns `READ_FAILED|offset|wanted|got|attempts`. The GCS
shows these fields, operation, request ID, attempt and elapsed time. Existing
firmware/client combinations remain compatible with KFS1 version 1.

The GCS still retries missing/damaged responses with the same request ID. It
does not retry a valid permanent SD error or silently restart a session. The
verified temporary-file/atomic-save behavior is retained. Wire retries and
satellite SD retry attempts are different measurements: SD attempts are visible
in the satellite USB log, not the GCS wire-retry count.

## Obtain the diagnostic builds

In each fork's Actions tab, select a successful run on
`fix/file-transfer-stability`. Download/extract the Windows GCS artifact and the
Lab 8 firmware artifact while signed into GitHub. In PlatformIO, upload the
firmware's `esp32thing_plus_115200` environment for the existing 115200 radios.
The default `esp32thing_plus` environment remains 9600.

The satellite USB monitor is always 115200. After reset, verify
`KFS1 stability-1`, the reset reason, `SD_SPI_MHZ=25`, the XBee UART rate and
`SD ready`. Radio settings remain under XCTU control. Use satellite AP=0 and
either ground AP=0/GCS Transparent or ground AP=1/GCS XBee API (AP=1). Existing
legacy AO=2 output is supported.

## Isolate the failing layer

1. **Local SD baseline:** Disconnect the GCS. Open the satellite's USB serial
   monitor (a different COM port from the ground XBee), enable newline endings,
   and send `SDCHECK /path/to/the/failing-file.jpg`. It makes three read-only
   local passes without SD retries or file data transmission. Save the complete
   USB output. All three passes should report PASS with the same bytes/CRC32.
   FAIL reports pass, offset, requested/returned length and card/file errors.
   CRC_CHANGED indicates differing contents between completed passes. This
   command requires no active file session; after an interrupted transfer, wait
   for expiry or reset before scanning. Run it with the GCS disconnected because
   the scan occupies the firmware loop. Do not send it through the ground radio.
2. **Transparent link:** Using the same file, card, power arrangement and radio
   placement, test at 115200 with ground AP=0 and GCS Transparent. Preserve all
   network/address settings. Record success, time and failing offset.
3. **API without polling:** Set ground AP=1 and GCS XBee API (AP=1), keeping the
   satellite AP=0 and both UARTs 115200. Disable RSSI polling in Transfer Stats
   and repeat with the same file.
4. **API with polling:** Enable RSSI polling and repeat. Compare rate/CSV plots
   and failures with the previous case. Local SD failures, new reset messages,
   RF delivery errors, protocol timeouts and expired sessions are distinct
   evidence; one alone does not establish the cause of another.

After each GCS operation, the monitor prints the diagnostic file path under
`transfer_diagnostics/` in its launch directory. Captures use UTC timestamps,
file path/mode/baud, request IDs, periodic read offsets/timings, timeout/retry and
malformed/stale-frame counts, final outcome and radio snapshot. They exclude
file payloads. Include the matching `.jsonl.1` file if rotation occurred. Each
file is capped at about 2 MiB with one backup; captures from separate operations
are retained for comparison. A disk/logging error is reported without failing
the file transfer.

The satellite USB log records SD read failures/recoveries plus one read checkpoint
per second, UART truncation and session expiry. `card`, `data` and `file_latched`
are raw SdFat error/status fields; file error flags may remain latched after a
successful retry, so interpret them alongside `got`, `attempt` and timing.

## Evidence-based follow-up

If local SD scans fail, investigate the card, SPI wiring/shared chip selects and
power before blaming the RF link. The default SD SPI clock remains 25 MHz.
For a controlled lower-clock comparison, add `-D SD_SPI_MHZ=10` to the selected
PlatformIO environment's build_flags, retaining `-D XBEE_UART_BAUD=115200` for
the high-baud build. Upload it and confirm `SD_SPI_MHZ=10` at startup. Change
one variable at a time and restore the baseline afterward. A lower clock is a
diagnostic option, not an established fix.

If SD-only passes succeed but radio transfers fail, compare radio delivery,
protocol counters, reset messages and the polling-on/off cases. The diagnostic
build is intended to supply this evidence; it cannot establish a hardware root
cause from the cloud.

## Acceptance and automated checks

The hardware target is at least 20 consecutive verified downloads in each
supported radio mode, including the largest commonly failing file and API runs
with polling enabled/disabled. Compare the saved files against SD originals,
then test Cancel, disconnect and a permanent SD failure. Record the number of
runs and failures; do not mark this target complete until measured on hardware.

Automated tests inject short/zero/error reads, repeated failures at varying
chunk offsets, permanent read failures, packet loss/corruption/duplication,
expired sessions, cancellation and disconnects. They verify complete file
contents/CRC and preservation of existing files on failure. The stress checks
run 20 transfers through the actual C++ service with scattered SD faults, plus
20 simulated legacy API downloads of roughly 14 KB with polling off and 20
with polling on.
These simulations do not establish physical link reliability.

Run all GCS suites with `QT_QPA_PLATFORM=offscreen xvfb-run -a python tests/run_all.py`.
Run Lab 8's `python test/test_protocol.py --gcs /path/to/KestrelSAT-GCS` and
compile both firmware environments with PlatformIO. Both artifact workflows
include this stability branch and main; packaging verifies a Windows executable
or a Windows ZIP extraction/checksum round trip, respectively.
