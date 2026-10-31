# Tests

These drive the real `SerialGUI` against a real Tk interpreter, so they need a
display. On Linux use `xvfb`; on Windows they run directly.

| file | what it covers | needs a display |
|---|---|---|
| `test_live_preview.py` | Frame timing, display/cleanup ownership, changed-file/CRC protection, cancellation, recovery, restart, queue saturation, local cache/Save races, native-pixel pop-out resizing/lifecycle, recommendation budgets and real Tk/API integration. | yes |
| `test_preview_firmware.py` | Optional POSIX/C++ preview cycles against the unchanged Lab 8 service. Run separately with `--firmware /path/to/Astro331_Lab8_Code`; needs the sibling firmware sources and host compiler. | no |
| `test_transfer_speed.py` | Negotiated profiles, binary/tail/empty transfers, batch loss recovery, API fragmentation/RSSI, legacy fallback, CSV profile labels and wire-byte counts. Real firmware interoperability is tested in Lab 8. | no |
| `test_transfer_stats.py` | Interval rates, stalls, unique-byte accounting, CSV recording, RSSI gaps, and PNG exports. | yes |
| `test_xbee_api.py` | AP=1 framing, AT/status/data multiplexing, RSSI polling, timeouts, and KFS downloads through a simulated radio. | no |
| `test_remote_files.py` | KFS1 framing, paths, timeouts, browsing, downloads, cancellation, disconnect cleanup, and serial ownership through the real tab. Uses a simulated wire peer. | yes |
| `test_themes.py` | Theme table: token parity across the three themes, colour format, WCAG contrast ratios. Parses `serial_gui.py` with `ast`, so it imports nothing. | no |
| `test_scaling.py` | High-DPI scaling: detection, font/geometry scaling, the Preferences round-trip, and that repeated application does not compound. | yes |
| `test_multiplot.py` | Multiple plot tabs: the default layout and `+` tab, adding and removing plots, renumbering (tabs and window titles), that every plot sees every line, and that configuration, data, clearing and sample numbering stay independent. | yes |
| `test_resize.py` | Resize safety: Send Data and the monitor controls stay visible at every window height, the plot tab scrolls horizontally so no channel control is unreachable, the computed minimum size and clamped launch geometry both fit the work area, wheel events do not scroll two regions at once, and every dialog is resizable, on-screen and closes on Escape. | yes |
| `test_correctness.py` | Regression tests for the crash and data-loss fixes: TEST MODE, tab delimiter, buffer cap, disconnect with a failing `close()`, timer cancellation, tooltips on buttons, channel cap, channel cleanup, legend consistency. | yes |
| `test_zmodem.py` | ZMODEM: CRC check values, every escape rule, round trips 0B-40KB, **interoperability against lrzsz's `sz`/`rz`** over a pty in both directions, and a full transfer driven through the app with its reader thread running. Interop is skipped if `sz`/`rz` are absent. | yes |
| `test_smoke.py` | Builds the whole UI, cycles all three themes, opens every dialog, checks settings persistence. | yes |
| `test_plot.py` | pyqtgraph window: background, axes, curve colours, live theme switching, close/reopen. | yes |
| `test_channel_scroll.py` | Plot tab layout: control ordering and the scrollable channel list. | yes |
| `bench.py` | Micro-benchmarks for the receive path, channel creation and redraw. Prints numbers; asserts nothing. | yes |

## Running

```bash
python3 tests/run_all.py                                   # Windows
QT_QPA_PLATFORM=offscreen xvfb-run -a python3 tests/run_all.py   # Linux
```

`QT_QPA_PLATFORM=offscreen` keeps PyQt5 from needing an X server of its own;
`xvfb-run` supplies one for Tk.

For the ZMODEM interoperability tests, install the reference tools:
`apt-get install lrzsz` (Debian/Ubuntu). Without them those checks are skipped
and only the self round-trip runs.

Or run any file on its own, e.g.
`QT_QPA_PLATFORM=offscreen xvfb-run -a python3 tests/test_correctness.py`.

## Note on the event loop

`test_correctness.py` drives a real `root.mainloop()` rather than spinning on
`root.update()`. That is deliberate: the worker threads hand data to the GUI
through a queue, and anything still using `root.after()` from a thread blocks
until the actual event loop services it — a `root.update()` loop will not
release it. The TEST MODE test would silently see zero channels otherwise.

Upload coverage (2026-10-21): `test_remote_files.py` checks capability fallback,
round trips, Tk upload/cleanup controls and upload statistics; `test_xbee_api.py`
adds twenty legacy API upload/download round trips with RSSI polling. Lab 8
`test/test_protocol.py` exercises the actual C++ upload service and injected
write/verification/acknowledgment failures. Physical radio/SD checks remain separate.

## Multi-operation statistics — 2026-10-21

`test_transfer_stats.py` exercises continuous mixed-direction recordings, idle
intervals, operation/configuration events, reconnects, partial captures without
transfer cancellation, full-CSV combining, memory-limited graph history, overview
decimation, logging failures and shutdown. Real Tk tests cover the combine worker,
API browse/download event integration, themes and graph lifecycle, measurement/
summary exports and all three PNG targets with restoration of the previous zoom.

## Camera tests — 2026-10-21

`test_camera.py` covers local timestamp/offset naming, supported modes, pending
job validation, capture/status parsing, checked on-demand JPEG downloads, corrupted
and oversized image handling, real Tk capture/view/zoom/close/rename/delete,
disconnect recovery without recapture, statistics, missing/old firmware and legacy
API frames with one raw reader. Each Tk case collects prior test interpreters on
the main thread before starting new workers. Lab 8's host/PTY suite separately
exercises the actual portable capture job and injected camera/SD failures.

## Link Tracker — 2026-10-30

`test_link_tracker.py` runs 26 portable core/API and real Tk/Qt regressions, with
PNG-export tests. It covers legacy per-packet RSSI without DB queries, modern
DB rate limiting/failures/ambiguous other-sender reception, source verification,
probe loss/late/corrupt replies without application retries, skipped schedule
slots, cancellation, transport/disk failures, bounded metadata and graph history,
complete CSV/ZIP logs, atomic export failures, a simulated hour at 10 Hz beyond
the in-memory history limit, smoothing gaps, distance markers, serial exclusion,
transfer-poller coordination, disconnect/reconnect and shutdown. The full runner
and Windows artifact builds include this suite. Fixtures verify semantics, not
physical XBee range, signal calibration or an achieved hardware sampling rate.
