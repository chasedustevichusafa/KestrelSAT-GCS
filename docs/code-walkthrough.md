# Reading the current GCS code — 2026-10-23

The source comments explain the working implementation. This change does not
implement the proposed transfer-speed experiments or change radio settings.

## Follow one operation

1. `serial_gui.py` starts `SerialGUI` in `kestrelsat/app.py`. The application owns
   the serial connection, settings, telemetry reader and GUI event queue.
2. `remote_tab.py` captures a file action on Tk's thread and starts a worker with
   exclusive serial-read ownership. `camera_tab.py` extends this lifecycle for
   capture/status and on-demand viewing. Generation IDs reject stale UI results.
3. `remote_files.py` implements KFS1 requests: newline-delimited fields, hex data,
   CRC32, same-ID retries and complete-file verification. `camera_files.py` adds
   capture identity/status recovery; pixels still use the normal file download.
4. With API mode selected, `xbee_api.py` wraps this payload stream in ground-radio
   API frames. Its sole raw reader routes RF data, local AT replies and TX status
   independently. The satellite remains AP=0. API frame IDs and KFS request IDs
   identify different exchanges.
5. The satellite's Lab 8 `file_service.h` handles the matching commands; its
   `main.cpp` adapts them to SdFat. Camera work runs cooperatively through
   `camera_job.h`, `camera_hardware.h` and `jpeg_stream.h`.
6. Workers post plain events; `app._pump_rx` calls tab handlers on Tk's thread.
   `image_viewer.py` decodes a downloaded file off-thread and creates Tk images
   on the GUI thread. Capture does not start live preview.

## Understand the measurements

`transfer_stats.py` samples accepted file-byte progress, separate serial counters
and fresh RSSI. A zero rate means no useful-byte progress in that interval; it
does not establish that the COM port disconnected. Local DB reports the ground
radio's last received signal strength, and stale reception produces a graph gap.

`transfer_recordings.py` places multiple sessions and idle gaps on a shared
monotonic timeline. Its CSVs retain measurements beyond the bounded live history.
`stats_tab.py` controls recording, plots and image/CSV export.
`transfer_diagnostics.py` records operation metadata and throttled checkpoints
rather than file contents or a complete RF packet trace.

## Other reading paths

- `plotting.py` and `channels.py`: independent telemetry parsers, buffers,
  channel controls and throttled Qt redraws.
- `zmodem.py`: the separate ZMODEM implementation and position recovery for
  compatible peers; its binary protocol is not KFS1's transfer format.
- `scaling.py`, `themes.py`, `theming.py`, `widgets.py`: baseline-based display
  scaling, semantic colours, wheel routing and widget lifetimes.
- `tests/README.md`: executable regression suites, including protocol, lifecycle,
  image, recording and interoperability checks.

The current KFS1 contract still uses one 128-byte file chunk per request/reply
with hex encoding. Baud rate is UART speed, not measured useful throughput.
Comments describe invariants and limitations; test simulation does not establish
physical camera or radio performance.
