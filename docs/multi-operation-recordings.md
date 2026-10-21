# Multi-operation transfer recordings — 2026-10-21

This GCS feature builds on the existing per-transfer statistics. Use the Windows
artifact from `feature/multi-operation-recordings`; the current Lab 8 firmware
and working radio settings remain compatible. No firmware update is required.

## Record a test

1. In **Transfer Stats**, choose **Start recording...** and name the test.
2. Run downloads/uploads from Remote Files or ZMODEM as usual. Leave gaps, browse
   folders, rename/delete files, or disconnect/reconnect as needed. The named
   recording remains selected as new individual transfers are created.
3. Choose **Show live graphs**. Download and upload rates share one elapsed-time
   axis with RSSI. Transfer-start lines identify filenames; hover event lines for
   their timestamp, type and detail. Other lines identify outcomes, connection
   changes, metadata activity and RSSI setting changes.
4. Choose **Stop recording** when finished. This stops statistics collection for
   the named recording without cancelling an ongoing transfer. Individual transfer
   statistics continue independently. Starting or stopping during a transfer
   captures only that portion and marks it partial in the operation summary.
5. Select either a named recording or an individual transfer in the selector.
   Closing the graph window preserves both. A recording can span reconnects and
   different baud rates/modes; each operation records its own configuration.

Sampling uses approximately one-second intervals and actual elapsed monotonic
time. Transfer boundaries add partial/zero-duration rows so very short transfers
and an immediate drop to idle are preserved. Delayed ticks are longer measured
intervals, not invented one-second measurements. UTC timestamps are anchored to
the start time and advanced by monotonic elapsed time, so changing the system
clock during a test does not change its durations. These are GCS observation
timestamps, not satellite timestamps.

## Exports and automatic logs

Full logs are saved under `transfer_statistics/` relative to the launch directory:

| File | Contents |
| --- | --- |
| `recording-….csv` | Measurement timestamps, elapsed/interval seconds, active operation and filename, direction, configuration, outcome, useful upload/download bytes and rates, averages, retries, serial traffic and RSSI |
| `recording-….operations.csv` | One row per finished captured operation, including captured start/end times, duration, bytes, outcome and partial-start/end flags |
| `recording-….events.csv` | Recording/operation starts and ends, connection changes, folder activity, rename/delete/cleanup activity, and RSSI setting changes |

**Export CSV...** copies the selected recording's complete measurements.
**Export operations...** copies its operation summary. While recording is running,
the summary includes only operations whose captured portion has ended; current
operation measurements and its start event are already logged. The events CSV is
available directly at the path shown in the tab. A ZMODEM batch is one operation; its measurement rows retain the active filename.
Each write is flushed; normal
application shutdown finalizes the recording. After an unexpected process exit,
completed writes remain available, but an active operation may have no final
summary/end event. Logging failures are reported without aborting a file transfer.

**Export PNG...** supports rates, RSSI, or both. Choose **Full timeline** to export
the entire named recording, including older measurements read from its CSV, or
**Current view** to preserve your zoom. Full export restores your live view afterward.
Individual-transfer PNGs retain their existing in-memory history limit. Long
recording overviews keep rate/RSSI extrema and missing-RSSI points in bounded
buckets; they are decimated views. CSV measurements retain every written row.
Markers are limited to 200 visible events and 20 filename labels for readability;
full exports select markers across the timeline. All events remain in CSV.

The latest 20 individual transfers and 20 named recordings remain selectable in
this application run. Each live graph retains 7200 measurement rows (roughly two
hours, less when many transfer boundaries are recorded). Recording summaries and
events retain up to 2000 entries in memory; their CSV files retain the full history.
Saved files are not automatically deleted. Old logs remain usable in spreadsheet
or analysis tools, but this version does not reopen recordings across app restarts.

## What the numbers mean

- **Useful bytes/rate:** unique file payload bytes acknowledged/reported by the
  existing transfer protocol. Retries do not increase useful bytes. Failed or
  cancelled transfers can have measured bytes even though no verified final file
  was saved. Upload checksum preparation and SD verification remain transfer time.
- **During transfers average:** captured useful bytes divided by captured transfer
  time, including protocol stalls and verification, excluding gaps between operations.
- **Including gaps average:** captured useful bytes divided by the entire recording
  duration, including idle time and disconnects.
- **Idle rows:** zero useful file throughput. Browsing, rename/delete and cleanup
  are metadata activity, not file transfers. Idle serial traffic and RSSI are left
  blank rather than inferred from other operations.
- **RSSI:** existing ground receiver API measurements, with the same freshness
  checks and approximately one-second transfer-only polling. Idle/transparent/
  stale measurements appear as gaps; recording itself generates no extra RF traffic.
  Retry counters in measurement rows are source-operation cumulative counters,
  including when a recording starts partway through an operation.

## Combine finished transfers

Choose **Combine transfers...**, select at least two finished transfers using
Ctrl/Shift, give the result a name, and choose **Combine selected**. The GCS reads
their complete per-transfer CSVs in a background worker and places them on one
timeline using their original monotonic start times. Unselected time remains idle
for useful throughput in the combined selection; it does not imply no other radio
activity occurred. Their original recordings are preserved. Missing/incomplete
source logs produce an error instead of an incomplete combined result.

Only retained finished transfers from the current app run are selectable. Gaps
are represented as zero file throughput with unavailable RSSI/serial counters;
RSSI is not reconstructed between operations. Earlier metadata/connection events
were not part of individual transfer logs and cannot be recovered by combining.
Use Start recording before the test when those events matter.

## Validation

Automated tests cover mixed uploads/downloads, real API-mode browsing/download
integration, idle gaps, failure/cancellation/reconnect markers, partial captures,
full-log combining after graph history eviction, long-timeline decimation, disk
errors, combine cancellation, dialog/worker delivery, graph lifecycle/themes,
CSV summaries and all three real PNG exports with zoom restoration. The full GCS
regression runner passed all 12 suites, including 15 statistics tests. Windows
portable tests also run during executable packaging. A physical
multi-operation test remains necessary to measure the actual radio link.
