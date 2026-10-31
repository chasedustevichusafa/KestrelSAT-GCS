# Link Tracker — 2026-10-30

Link Tracker is a dedicated long range measurement mode. It uses small KFS1
`HELLO` requests and replies with the existing Lab 8 firmware. It does not read
SD files, capture images, change radio settings or require a firmware upload.

## First test

1. Use the working radio configuration: ground AP=1, satellite AP=0, matching
   UART baud rates (normally 115200), and the tested unicast destinations.
2. Connect in the GCS using **XBee API (AP=1)**. Open **Link Tracker**.
3. Name the session, choose **2 probes/s**, and leave the timeout at **1.0 s**.
   Leave **Expected receive source** blank initially when the configured unicast
   destination matches the receive address type.
4. Click **Start tracking**, then **Show graphs**. RSSI, probe success, response
   latency and achieved rates should become available. Raw logs save automatically.
5. If probes succeed but RSSI is unverified, inspect the reported RX frame/source.
   A legacy `0x81` frame carries a 16-bit address, not the radio's serial number.
   Stop, enter the satellite radio's **MY** short address in hexadecimal, then
   start a new session. For `0x80`, `0x90` or `0x91`, the receive source is the
   64-bit **SH+SL** address. Explicit source selection affects measurement
   verification; it does not program the radio's destination.
6. Add a checkpoint note and optional distance in metres at each test location.
   Stop at the end, select **Full session**, and wait for the CSV plot to load.
   Export a CSV bundle ZIP and PNG graphs for the presentation.

Tracking is opt-in and starts off after startup/reconnect. Stop it before Camera,
Live Preview, Remote Files or ZMODEM operations. It reserves the existing serial
owner/read lock so telemetry and other tabs cannot consume probe replies. Closing
only the graph window leaves recording running. Stop, Disconnect and application
close cancel a pending probe and close the CSVs. No automatic reconnection or
tracking restart is attempted.

## Sampling and interpretation

| Setting | Intended use |
| --- | --- |
| 1 probe/s | Conservative baseline and long recordings |
| 2 probes/s | Default; first range test |
| 5 probes/s | More detailed tracking, after comparison with 2 Hz |
| 10 probes/s | Experimental; verify achieved rates and failures on hardware |

Each probe has a unique request ID and exactly **one application attempt**. The
radio can still perform its configured MAC retries; local TX-status retry deltas
are recorded separately. A successful TX status is not a successful probe: a
complete, CRC-valid matching `HELLO|1|128` response is required. Late, corrupt and
unrelated replies do not satisfy the next probe. Idle late replies are logged as
unmatched events. A cancelled in-flight probe is excluded from success totals.

There is only one pending probe. Timeout defaults to one second and includes
local API transmission/status waits and response handling. A blocking raw UART
write can overrun that target by its own bounded write timeout; CSV durations
retain the actual time taken. A slow probe skips
missed schedule slots; it never queues a burst to catch up. Actual probe rate,
RSSI measurement rate and skipped slots are shown separately. A missed schedule
slot was not transmitted and is not counted as a failed probe. Response latency
is the host-observed complete-reply delay, including UART/API/firmware work, not
RF propagation time. CSV logging after the read is excluded from that latency.

The application never changes sample rate silently. Increase the rate only if
achieved rates and successful replies remain stable. At lower baud rates or poor
RF conditions the achieved rate can be much lower than the requested rate.

### RSSI sources

- Legacy `0x80` / `0x81` API frames contain **per-packet RSSI**. Use those values
  directly; no `DB` query is needed, even at higher probe rates. A logical reply
  may occupy multiple RF packets, so packet RSSI rate can exceed probe rate.
- Modern `0x90` / `0x91` frames have no RSSI field. Query the ground radio's `DB`
  register only after fresh RF reception, at most about once per second.
  Increasing probe rate does not create 5 or 10 independent DB measurements/s.
- `DB` is an approximate last-packet register. If reception changes during its
  query, including a filtered other sender, flag the result as ambiguous and
  exclude it from the graph. Do not poll when the most recent packet belongs to
  a filtered other sender. Keep errors and ambiguous readings in raw logs.
- Measurements require a matching source address and fresh, in-session packet
  metadata. Unverified samples remain in raw logs with `valid_sample=False`.
  The displayed last RSSI becomes stale after two seconds; it is never repeated
  as a newly measured sample. Missing RSSI is never zero dBm.

These are **satellite-to-ground received-packet** measurements. Signals from
packets that never arrive cannot be measured. Strong received-packet RSSI can
coexist with poor delivery. Round-trip probe failure can involve either direction,
UART/firmware delays or local API trouble; it is not a direct measurement of
one-way RF packet loss. Satellite-side RSSI would require additional support.

## Graphs

The resizable graph window provides synchronized elapsed-time plots:

- Raw verified RSSI, optional five-second median and min–max variability band.
- Successful probes over the trailing 30 seconds. Crosses at zero identify
  individual failed probes; the line is the window's success percentage.
- Complete response latency. Failed responses create gaps, not zero-ms readings.
- RSSI versus manually entered distance: median of available verified samples
  within two seconds either side of each checkpoint. No GPS distance or automatic
  interpolation is implied. Wait at a location before adding its marker.

Choose **Last 60 s**, **Last 5 min**, **Full session**, or **Manual**. Manual leaves
the time range under zoom/pan control. Graphs redraw at two Hz independently of
measurement/logging. Gaps break RSSI smoothing and the time axis continues during
outages. Marker labels are limited to 30 visible annotations; all markers stay in
CSV. The last ten sessions from this application run remain selectable.

Live history is bounded to 20,000 rows per log. After Stop, Full session streams
CSV off the GUI thread. Up to 20,000 rows are exact; longer logs use a bounded
endpoint/extrema/failure envelope. RSSI extrema, maximum latency and minimum
recorded rolling success are retained per bucket. The 30-second success statistic
is computed and saved during collection, not recomputed from decimated rows.
Smoothing is hidden for decimated full-session views. Distance summaries in those
views are approximate. Exact analysis always uses the complete raw CSV.

PNG export saves the current graph range, with combined or individual RSSI,
success, latency and distance targets. For the entire experiment, Stop, load Full
session, then export. Graph zoom does not modify measurements or CSV contents.

## Saved files and reliability

Each session creates `link_tracking/<UTC timestamp>-<unique id>/`:

| File | Contents |
| --- | --- |
| `packets.csv` | RF packet and RSSI-query events, elapsed/UTC times, receive source/address type/API format, RSSI method, raw dBm, packet age, source verification, sample validity, errors/ambiguity |
| `probes.csv` | Probe sequence/request ID, success/timeout/TX error/invalid reply/cancellation, latency, duration, observed radio retries, malformed/stale replies and original rolling success percentage |
| `events.csv` | Start/stop/disconnect, checkpoints/distances, skipped schedule slots, idle unmatched replies and metadata overflow counts |
| `session.json` | Schema, name, requested rate/timeout, baud/AP/destination/source selection, counters, elapsed time and final outcome |

Elapsed time uses the monotonic clock. UTC labels are anchored to session start,
including queued packet reception times, so a computer clock correction does not
reverse the experiment timeline. CSV rows are flushed continuously; they are not
reconstructed from graph pixels. Stop before exporting the complete CSV bundle.
Export runs off the GUI thread and preserves the original logs and any previous
export if compression fails. Logs remain on disk after the recent-session list
or application closes; external tools can read them later.

The API reader never writes logs or calls Tk. Its metadata queue is bounded;
overflow is counted and logged visibly. CSV write failures stop tracking and
release the port rather than silently continuing an incomplete experiment.
Normal closure records an outcome and closes files. Abrupt process/power loss can
leave the metadata marked `running`; review the surviving CSV before analysis.
Flushing is not a guarantee against operating-system or physical disk failure.

## Suggested physical comparison

At fixed antenna orientation and distance, record at least 60 seconds each at
1, 2, 5 and (optionally) 10 Hz. Compare actual probe and RSSI rates, success,
latency, skipped slots and recorded errors. Repeat near the range limit. Select
the fastest stable rate, then do a longer test with distance/obstruction markers.
Keep baud, addressing, channel, power and antenna placement documented and
consistent between comparisons. Successful simulation and packaging do not
establish on-air performance or a validated 10 Hz hardware rate.
