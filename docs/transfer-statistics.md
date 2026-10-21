# Transfer statistics and XBee RSSI

Implemented on **2026-10-20** on `feature/transfer-statistics`. The satellite's
Lab 8 firmware and radio remain unchanged. This feature build adds transfer
measurements and optional ground-radio API support to the GCS.

## Live graphs and exports

Every Remote Files download and ZMODEM send/receive automatically starts a
recording. Directory listings and ordinary serial commands do not count as file
transfers. Open **Transfer Stats**, choose a session, and use **Show live graphs**.
The graph window uses the same PyQtGraph system as telemetry plots and has two
aligned plots: useful file download/upload rate in **B/s**, and ground receiver
RSSI in **dBm**. It remains available after completion, cancellation, or failure.
Closing the graph window does not discard the recording.

The tab shows the current one-second rate, average, unique file bytes, elapsed
time, known file total, status, and protocol retries where available. Stalls are
recorded as zero-speed intervals. Duplicate/retransmitted file offsets are not
counted twice. ZMODEM resumed bytes already held by the peer are excluded, and
batch transfers accumulate the files transferred in this session. Upload rate
tracks unique payload positions sent; final protocol completion determines
success. The Lab 8 satellite remains download-only; this does not add satellite
file uploads. Existing ZMODEM uploads are measured when used with a compatible
receiver.

- **Export PNG...** saves the combined graphs, rate graph, or RSSI graph at 1600
  pixels wide. Combined images include the filename, protocol/direction, baud,
  start time, and outcome. PNG export requires the plotting dependencies.
- **Export CSV...** saves the complete session measurements, including serial
  traffic rates, retries, RSSI/age, and whether polling was enabled. The serial
  counters include protocol overhead, retransmissions, and API/AT frames; they
  are not an over-the-air RF bitrate measurement.
- Full CSV logs are automatically retained in `transfer_statistics/` relative to
  the application's launch directory. A recording error is shown in the tab
  without aborting the file transfer. Keep this directory writable.

The most recent 20 sessions stay selectable. Each graph retains the latest 7200
samples (about two hours); the CSV log retains all recorded samples. Sampling
uses a monotonic clock. Delayed UI updates use the actual interval rather than
inventing one-second samples. A final partial interval records the transfer's
outcome. RSSI polling overhead is included in measured transfer time and serial
traffic; compare otherwise similar runs with polling on and off to quantify it.

## Ground XBee setup in XCTU

Use the new feature-build executable before changing the radio. The older GCS
expects transparent mode and cannot use an API-configured radio.

1. Disconnect and close the GCS. Connect the **ground-station** XBee by USB and
   open it in XCTU at its working **9600 baud, 8N1** configuration.
2. Save/export a backup of the radio's current configuration in XCTU.
3. Find **AP — API Enable** under the serial interfacing settings. Set it to
   **API Mode Without Escapes (`AP=1`)**, then click **Write** to apply it.
   This build supports AP=1, not escaped API mode AP=2.
4. Preserve **BD (baud rate), DH/DL (destination), PAN/network, and security**
   settings. Do not update the radio firmware or change the satellite XBee's
   transparent mode as part of this setup. Use the existing paired link.
5. Close XCTU so it releases the COM port. Start the new GCS, select the ground
   COM port, **9600 baud**, and **Radio mode → XBee API (AP=1)**, then Connect.
6. The GCS checks API mode and reads the configured destination and payload
   capacity using local API AT commands. It does not write radio settings. Test
   **Remote Files → Refresh / Open**, then download a small known file.
7. In **Transfer Stats**, leave **Poll ground receiver RSSI during transfers**
   checked and open the live graphs. Compare another run with it unchecked.

To return to the previously working setup, close the GCS, set **AP=0** in XCTU,
write the change, close XCTU, then choose **Transparent** in the GCS. Both radio
modes provide rate graphs and CSV/PNG export; RSSI requires API mode.

[Digi's AP documentation](https://docs.digi.com/resources/documentation/digidocs/90002002/Reference/r_cmd_AP.htm)
defines AP=0/1/2. The supplied XBee 3 Zigbee XCTU profile also defines AP=1 as
"API Mode Without Escapes" and DB as the magnitude of the last good packet RSSI
(e.g. `0x58` is displayed as **-88 dBm**).

## Optional 115200-baud test — 2026-10-21

The GCS already supports 115200 through the Connection tab's **Baud Rate**
selector. Speed graphs and API/RSSI support use the selected rate; no GCS code
change is needed. Before selecting it, configure the ground XBee BD to 115200,
configure the satellite XBee BD to 115200, and upload the matching
**esp32thing_plus_115200** firmware environment. The normal **esp32thing_plus**
firmware environment stays at 9600 for rollback.

See the [115200 firmware setup and rollback procedure](https://github.com/chasedustevichusafa/Lab-8-Wireless-Image-Capture-and-Transmission/blob/feature/high-baud-link/Astro-331-2026-Fall/Astro331_Lab8_Code/README.md#optional-115200-baud-test).
Keep each radio's API/transparent mode, pairing, and network/security settings
unchanged. In the API-mode instructions above, substitute 115200 for 9600 only
after changing the radio's BD setting. Do not use 115200 with the old 9600 firmware.

A higher UART baud rate does not change the RF bitrate or guarantee a proportional
speed increase. Compare the same file with polling enabled/disabled at each
rate, and verify the received contents.

## What the RSSI plot means

The GCS polls the **ground radio's local DB value** at most once per second while
recording and only after a new received RF data packet. The background transport
separates RF payloads, transmit statuses, and AT responses. It never sends `+++`
or enters transparent command mode during a transfer.

DB describes the **last received RF packet**, not continuously measured signal
strength. In a routed network it can describe the last hop, and unrelated received
radio traffic can affect it. It is not a measurement of signal strength at the
satellite. During uploads, ground-side readings may come from return/acknowledgment
traffic. Measuring the satellite's receive RSSI is a separate future feature.

Readings older than two seconds, periods with no fresh packet, disabled polling,
and unavailable DB responses appear as gaps. Unsupported DB queries do not stop
file transfers. The tab reports a DB/connection diagnostic when available. No
fake zero-dBm samples are inserted. RSSI acquisition and API-mode behavior still
need validation on your physical radio pair.

## Verification and Windows build

Run the existing full test runner on Linux:

```sh
QT_QPA_PLATFORM=offscreen xvfb-run -a python tests/run_all.py
```

`test_transfer_stats.py` verifies rates, stalls, retransmission/resume accounting,
CSV retention, and all three real PNG exports. `test_xbee_api.py` verifies API
checksums/fragmentation, data/status/AT separation, explicit receive frames,
transmit errors/timeouts, RSSI pacing, shutdown, and KFS downloads through a
simulated API radio. The existing Lab 8 C++/Python compatibility tests remain
applicable. Automated tests do not establish physical radio performance.

**Windows test build** runs on pushes to `feature/transfer-statistics` as well as
`main` and the existing file-transfer branch. Download the completed run's
`KestrelSAT-GCS-Windows-<commit>` artifact while signed into GitHub, extract it,
and run `KestrelSAT_GCS.exe`. This workflow does not publish a release.
