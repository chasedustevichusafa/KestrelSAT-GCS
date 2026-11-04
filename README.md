# KestrelSAT Ground Control Station

A Python desktop ground station for the USAFA ASTRO KestrelSAT project: serial
telemetry, verified XBee file transfers, camera capture and periodic preview,
transfer measurements, and dedicated RSSI/range tracking. Tkinter/ttk supplies
the main interface; PyQtGraph/PyQt5 supplies the graph windows.

**Project completion documentation: November 4, 2026 (America/Denver).**
The owner confirmed the completed file, camera, preview and link-tracking
workflows, and the feature branches have been merged into this fork's `main`.
Use the current `main` builds of both repositories. Earlier development notes
and test proposals are not additional installation requirements.

## Start with the completed setup

1. Download the GCS from a successful **main** run of
   [Windows test build](https://github.com/chasedustevichusafa/KestrelSAT-GCS/actions/workflows/build-windows.yml).
   Under **Artifacts**, select `KestrelSAT-GCS-Windows-<commit SHA>`. Sign into
   GitHub, extract the ZIP, then run `KestrelSAT_GCS.exe`; Python is not required
   for the packaged executable. Close other GCS instances and XCTU first.
2. Get the matching [Lab 8 firmware source](https://github.com/chasedustevichusafa/Lab-8-Wireless-Image-Capture-and-Transmission/actions/workflows/lab8-firmware.yml).
   Extract `KestrelSAT-Lab8-Firmware`, open `KestrelSAT-Lab8` in PlatformIO, and
   upload `esp32thing_plus_115200_camera_cs27` for camera CS wired to GPIO27.
   Select the environment explicitly: PlatformIO's default remains GPIO12/9600.
3. Use the settings below. Firmware selection does not program the radios;
   write UART/AP/address settings separately in XCTU, then close XCTU.
4. In **Connection**, select the ground-radio COM port, **115200**, **8N1**, and
   **XBee API (AP=1)**. Connect, then use **Remote Files → Refresh / Open** or
   **Camera → Refresh images**. Begin with a small verified file/capture.
5. Keep the executable in a writable folder. Settings, logs, downloaded images,
   recordings and diagnostics are relative to the launch working directory.
   Preserve exported CSV bundles as well as PNGs for later analysis.

| Component | Completed working configuration |
| --- | --- |
| Satellite PlatformIO environment | `esp32thing_plus_115200_camera_cs27` |
| Board identifier | `sparkfun_esp32s2_thing_plus_c`, retained from the working labs |
| Camera chip select | GPIO27; match the physical wire |
| Satellite firmware ↔ satellite XBee UART | 115200 baud, 8N1; satellite radio **AP=0** |
| GCS ↔ ground XBee UART | 115200 baud, 8N1; ground radio **AP=1**, GCS **XBee API (AP=1)** |
| Radio destination addressing | Unicast: each radio's DH/DL equals its peer's SH/SL |
| Other radio settings | Retain the working network/security settings and MAC acknowledgments |
| USB Serial Monitor | 115200; independent of the XBee UART baud |
| Normal transfer profile | Auto (Fast with FAST1 firmware); Experimental is an explicit comparison option |

### Transparent, API, broadcast and unicast

**AP=0 (transparent)** passes ordinary serial bytes, like a wireless serial cable.
**AP=1 (API)** frames traffic between the local radio and its attached computer,
allowing the GCS to identify receive sources, inspect transmit status and query
radio diagnostics. The GCS handles legacy `0x80`/`0x81` and modern `0x90`/`0x91`
receive frames; the lab profile's legacy AO output is supported.
The ground radio strips/adds its API envelopes locally, so the satellite still
uses AP=0. Both ends transparent also support file transfers, but API diagnostics
and the dedicated Link Tracker require the ground API connection.

AP mode does not select broadcast/unicast or increase RF bitrate. The supplied
lab profile's `DH=0, DL=FFFF` is broadcast. For the working unicast setup, read
both radios' SH/SL and set **ground DH/DL = satellite SH/SL**, and **satellite
DH/DL = ground SH/SL**, then write both radios and reconnect the GCS. A higher
UART baud removes a serial bottleneck; RF packet overhead and retries still
limit useful transfer speed.

## Completed features and operating guides

| Page or feature | What it does | Detailed guide |
| --- | --- | --- |
| Connection / serial monitor | Port detection, serial settings, text/hex, timestamps, send controls, logging and TEST MODE | [Usage](#usage) |
| Telemetry plots | Independent multi-channel plot tabs, labels, colours, visibility, scaling and resizable windows | [Plotting](#plotting) |
| Remote Files | Browse SD folders; upload/download with CRC32 verification; cancel; rename/delete; remove reserved partial uploads | [Remote files](docs/remote-files.md), [uploads](docs/remote-upload.md), [management](docs/remote-file-management.md) |
| Camera | Select supported resolution, capture JPEG to SD, View/Download, Rename/Delete and recover a pending capture | [Camera](docs/camera.md) |
| Live Preview | Opt-in 5/10-second refresh targets, selectable resolution, Save frame, preview-only SD cleanup and native-pixel pop-out | [Live Preview](docs/live-preview.md) |
| Transfer profile | Baseline, Larger blocks, Compact, Fast, Experimental and automatic legacy fallback | [Transfer speed](docs/transfer-speed.md) |
| Transfer Stats | Live useful upload/download rate, optional ground RSSI, session CSV and PNG exports | [Measurements and RSSI](docs/transfer-statistics.md) |
| Recordings | One timeline across operations, idle gaps and reconnects; combine finished sessions; summary/event CSVs | [Multi-operation recordings](docs/multi-operation-recordings.md) |
| Link Tracker | Dedicated RSSI/probe-success/latency logging, distance checkpoints, CSV bundles and offline reconstruction | [Link Tracker](docs/link-tracker.md) |
| Graph formatting | Independent white/black graphs; RSSI over speed using a separate right-hand dBm axis | [Graph formatting](docs/graph-formatting.md) |
| Standard ZMODEM | Separate interoperability with sz/rz and compatible terminal tools; not the Lab 8 KFS1 protocol | [ZMODEM](#file-transfer-zmodem) |
| Source documentation | Detailed comments explaining serial ownership, protocol, SD, camera jobs and GUI lifecycle | [Code walkthrough](docs/code-walkthrough.md) |

### Remote files, camera and preview

Uploads require an existing destination folder and a new name. They write a
reserved temporary file, verify stored size/CRC by SD read-back, then publish the
final name without replacing existing files. Downloads verify the complete file
before replacing the local destination. Interrupted transfers restart rather
than resume. Rename operates within the current folder; Delete is permanent and
asks for confirmation. Use **Remove partial...** only for reserved `.kfs-` files.

**Camera** captures one still without starting preview. Filenames contain the
**GCS local request time**, UTC offset, resolution and unique job suffix, for
example `20261021T153045-0600_640x480_a14f9082.jpg`; this is not a satellite RTC
or exact shutter timestamp. Use **View** to download/display the saved JPEG.
**Stop waiting** does not cancel an accepted onboard exposure; reconnect and use
**Check capture** before requesting another image.

**Live Preview** repeatedly captures, verifies, downloads and displays still
JPEGs. It is off until Start; 5/10 seconds are start-to-start targets, not a video
frame-rate guarantee. Only the exact preview-created image is automatically
deleted, after verified download and successful display. Ordinary still images
remain on SD. **Save frame...** keeps the current JPEG on the PC. **Pop out image**
opens a resizable window displaying native image pixels with scrollbars, without
resizing the image. The resolution/refresh guide and per-frame byte budget are
based on reported speed tiers; scene-dependent JPEG sizes and capture/SD time
can cause overruns. Pending capture/delete identities have explicit recovery
controls and never automatically restart preview after reconnect.

### Transfer profiles and measured speed

| Profile | Useful bytes per block | Encoding | Requests per batch |
| --- | ---: | --- | ---: |
| Baseline | 128 | Hex | 1 |
| Larger blocks | 256 | Hex | 1 |
| Compact | 256 | Base64 | 1 |
| Fast | 512 | Base64 | 1 |
| Experimental | 512 | Base64 | 2 |
| Auto | Fast with FAST1 firmware; otherwise Baseline | Negotiated | 1 |

**Batch 2** means at most two data requests are sent before both must be
confirmed. It is not a two-byte block or two simultaneous transfers. Retries
retain request IDs; full-file checks, SD sync/read-back and overwrite protection
remain in place. Profiles do not change baud, radio AP/addressing or camera
resolution. The actual negotiated profile is recorded in current CSVs/headers.

The owner reported roughly **1500 B/s Baseline**, **3850 B/s Auto/Fast**, and
**4700 B/s Experimental** on the working setup. These are observed download
figures, not guaranteed rates, upload benchmarks or RF bitrates. Compare the same
saved file, position and settings; retain complete durations, failures and retry
counts. The [speed guide](docs/transfer-speed.md) explains controlled comparisons.

### Recording, graph exports and offline replay

In **Transfer Stats**, start a named recording before several operations and
stop it afterwards. Original elapsed time, idle intervals, operation outcomes
and connection events are retained. **Combine transfers...** combines sessions
from the current app run in original time order; it does not remove idle gaps.
Export measurements, operation summaries and PNGs before closing the app.

Graphs default to **white**. **Options → Graph Background → White / Black**
(or Preferences) changes graph appearance independently of the UI theme.
**Overlay RSSI on speed graph (right dBm axis)** combines rate and RSSI on a
shared time axis while keeping independent units. Missing RSSI remains a gap;
formatting does not change recorded data. PNG export supports the full timeline
or current graph view. A rate of zero alone is not a serial disconnection.

**Link Tracker** generates dedicated probes without transferring a file. Start
at **2 Hz**; 1/2/5 Hz and experimental 10 Hz are available, with one outstanding
probe and no automatic retries that would hide failures. It measures received
satellite-to-ground RSSI, round-trip probe success and round-trip latency, not
symmetric link strength. Legacy receive frames carry packet RSSI; other formats
use a fresh-packet DB fallback near 1 Hz. Actual sampling depends on the radio.

If success/latency appears but RSSI is blank, inspect **RX source** in the summary.
For legacy 16-bit frames, **Expected receive source** must match the satellite
radio's **MY** address, which can be `0000`; its 64-bit SH/SL unicast destination
cannot verify a 16-bit source by itself. Source verification and freshness are
required for RSSI. The blue band shows the rolling RSSI minimum-to-maximum range around the
median, not another radio or extra raw measurements; see the
[graph definitions](docs/link-tracker.md).

Export the complete **CSV bundle** (session metadata, probes, packets and events).
**Open CSV bundle...** or **Open session folder...** reconstructs Link Tracker
plots offline without a radio. Optional source rechecking is explicit and
labelled, and leaves original files unchanged. General transfer CSV import is
not a Link Tracker bundle: keep each export with its matching feature.

## Downloads, old versions and recovery

GitHub Actions artifacts are tied to a specific run/commit and require a GitHub
login. The Windows test artifact is retained for **14 days**; the firmware source
artifact for **30 days**. Older unexpired successful runs can be downloaded from
Actions. Expired/deleted artifacts cannot be retrieved, but committed source
remains in Git history: check out the desired commit or manually run the workflow
at an available branch/tag containing it. A manual **Windows test build** creates
an artifact; the separate **Build Windows executable** release workflow publishes
a versioned release and should only be used when a release is intended.

For a matching rollback, retain both source commit IDs, radio profiles, selected
firmware environment and exported datasets. A pre-speed firmware still works
with Auto's legacy fallback, but explicit fast profiles require FAST1. Older
firmware may lack camera/upload/management capabilities; the UI detects them.

If **XBee API reply timed out** occurs before Connected, verify the adapter is
plugged in, the COM port, matching ground-radio baud/AP and that XCTU/other GCS
instances are closed. A **Satellite did not reply** message after connecting
also requires checking satellite firmware baud, AP=0, peer addresses, power and
SD readiness. SD `READ_FAILED` is a different failure: retain the reported
byte offset and [diagnostics](docs/file-transfer-stability.md), and use the USB
`SDCHECK` procedure if needed. Do not infer an SD fault from a zero-rate interval.

The confirmed GPIO27 setup addresses the earlier camera-attached flashing issue.
Select the matching CS environment. If flashing still fails, power down before
disconnecting/reconnecting camera wiring and follow the
[camera recovery guide](docs/camera.md); retain flash verification checks.

## Completion record — 2026-11-04

- Consolidated the merged file-transfer, camera, preview, speed, statistics,
  graph-formatting and Link Tracker/offline-replay documentation.
- Replaced obsolete feature-branch installation instructions with current main
  artifacts, final GPIO27/115200/API/unicast settings and accurate retention rules.
- Recorded owner-confirmed operation and reported speed tiers. Repeated-transfer
  reliability and range are properties of the tested setup, not universal
  guarantees. Automated tests establish software behavior, not RF coverage.
- Retained the original telemetry/ZMODEM usage, licensing and source-build guidance
  below. Labs 4/5 remain firmware reference projects; Lab 8 is the deployed service.

## Requirements

- Python 3.10 or newer when running source (the packaged Windows exe needs no Python).
- tkinter (bundled with standard Windows/macOS Python; Debian/Ubuntu `python3-tk`).
- Pinned dependencies in `requirements.txt`: pyserial, pyqtgraph, PyQt5 and Pillow.

Without PyQtGraph/PyQt5 the serial monitor can start, but graphs are unavailable.

## Installation

1. Install the required dependencies:
```bash
pip install -r requirements.txt
```

2. Run the application:
```bash
python serial_gui.py
```

To list the serial ports visible to Python without launching the GUI:
```bash
python check_ports.py
```

## Usage

### Connecting to a Device

1. **Select Port**: Choose your device from the dropdown, or click "Refresh" to rescan.
   Choose `TEST MODE` to run against simulated data with no hardware attached.
2. **Configure Settings**: Set the baud rate, and use "Configure Serial Settings" for data
   bits, parity, and stop bits.
3. **Connect**: Click "Connect". Leave "Clear on Connect" enabled for a clean start.

### Sending Data

1. Type your message in the "Send Data" field
2. Choose line ending options (newline/carriage return) if needed
3. Click "Send" or press Enter to transmit

### Receiving Data

- Received data appears in the Serial Monitor on the Connection tab
- Toggle between text and hex display
- Enable timestamps to see when each line arrived
- Auto-scroll keeps the newest data visible

### Plotting

Each **Plot** tab is one plot. "Show Plot Window" opens it in its own window, titled
"Serial Plot 1", "Serial Plot 2", and so on.

**Multiple plots.** The app starts with a single `Plot 1` tab and a `+` tab to its right.
Click `+` to add `Plot 2`, `Plot 3`, and so on, each with its own window. Every plot is
configured independently - its own delimiter, buffer sizes, axes, title, and per-channel
colours and visibility - so you can watch the same stream several different ways at once.

Every plot parses every incoming line itself, so all plots see the same data by default;
what differs is how each one is configured to show it. Clearing or pausing one plot leaves
the others untouched. "Remove This Plot" closes a plot and discards its data; the last
remaining plot cannot be removed.

1. **Data format**: send one sample per line as delimited values. Named channels work too,
   for example `Temp:25.5,Humidity:67`. Unnamed values are named `Ch1`, `Ch2`, ... by position.
2. **Delimiter**: comma, space, tab, or a custom character.
3. **Buffer Size** is how many samples are retained; **Plot Width** is how many are shown.
4. **X-Axis** can be the sample number or any detected channel, so you can plot one channel
   against another. Axis labels and the plot title can be overridden.
5. **Per-channel controls** appear as channels are detected: visibility, display name, line
   thickness, colour, dot size, and whether to draw the connecting line.
6. **Pause Plot** freezes the display while data keeps being collected. **Clear Plot Data**
   drops the channels and their settings; **Clear Buffer** keeps the channels and discards
   all but the most recent sample of each.

### Notepad

A scratch pad for notes taken alongside a session. **Insert Timestamp (Ctrl+T)** stamps the
current date and time at the cursor. The shortcut works from any tab and switches to the
Notepad so you can see the stamp land.

### Logging

- "Start Logging" writes everything received to a file continuously, and the header shows
  the current file size
- "Save Log" captures the current contents of the display in one shot
- Both default to the `logs/` folder, created next to wherever the app is launched from

## Configuration Options

### Serial Port Settings
- **Port**: Physical serial port (COM1, COM2, etc. on Windows; /dev/ttyUSB0, etc. on Linux)
- **Baud Rate**: Communication speed (9600, 115200, etc.)
- **Data Bits**: Number of data bits per character (5-8)
- **Parity**: Error checking method (None, Even, Odd, Mark, Space)
- **Stop Bits**: Number of stop bits (1, 1.5, 2)

### Appearance
Pick a theme from **Options → Appearance**, or from **Options → Preferences**. Both use the
same setting, the change applies immediately, and your choice is remembered between sessions.

- **Dark (Mission Control)** — the default. Deep-space blues with cyan and amber accents.
- **Light** — a conventional light theme for well-lit indoor use and for screenshots.
- **High Contrast (Sunlight)** — black on white with heavy borders and thicker plot traces,
  for reading the screen outdoors in direct sun.

The theme covers the plot window as well: background, axes, grid, legend, and the default
channel trace colours all follow it. A colour you pick yourself for a channel is left alone.

Note that Windows' own file, colour, and message dialogs are drawn by the operating system
and cannot be themed, so those still appear in the system's colours.

### Display Scale
On a high-DPI screen the interface scales itself so text stays readable. **Options →
Preferences → Display Scale** shows what was detected and lets you override it
(100% through 250%).

Two situations are handled, and they are not the same problem:

- **Windows scaling at 125/150/200%.** The app declares itself DPI-aware, so Windows
  no longer bitmap-stretches it — text is drawn crisply — and it then scales itself to
  match.
- **A 4K panel running at 100% scaling.** Windows reports a normal 96 DPI here because
  no scaling is active, yet the pixels are physically tiny. Automatic falls back to the
  raw resolution and enlarges anyway.

Changing the setting resizes text immediately. The window itself and the connection
indicator take their new size on the next start.

### File Transfer (ZMODEM)
Files can be moved over the same serial connection the telemetry uses, so a board with a
ZMODEM-capable bootloader or shell can be loaded and read back without a second cable.

- **File → Send File (ZMODEM)…** picks one or more files and sends them.
- **File → Receive File (ZMODEM)…** waits for the far end to offer a file and asks where to put it.
- **Automatic downloads**: when the connected device starts a transfer (for example by running
  `sz`), the app recognises the offer in the incoming stream and receives into `downloads/`
  without being asked.

The implementation is interoperable with the standard tools — it is tested directly against
`sz` and `rz` in both directions.

Turn it off under **Options → Preferences → File Transfer**. It is on by default; disabling it
removes the automatic download behaviour, which is worth doing if a device happens to emit data
that resembles a ZMODEM header. The menu entries then explain that it is disabled rather than
disappearing.

A transfer takes exclusive use of the port for its duration, so the serial monitor pauses while
one is running and resumes afterwards.

### Display Options
- **Timestamps**: Add time stamps to all messages
- **Auto-scroll**: Automatically scroll to show newest data
- **Hex Display**: Show received data in hexadecimal format

### Send Options
- **Add newline (\\n)**: Append newline character to sent data
- **Add carriage return (\\r)**: Append carriage return character to sent data
- **Show echo**: Show data you send in the Serial Monitor display (on by default)

## Building a Windows executable

Locally, `build_exe.bat` runs PyInstaller against the project's `.venv`, or build from
the spec directly:

```bat
pyinstaller KestrelSAT_GCS_v1.spec
```

The result is a single `dist/KestrelSAT_GCS.exe` with the icon and splash screen bundled.

The regular main/PR executable comes from **Windows test build** as described
above. Versioned releases use a separate workflow: pushing a `v*` tag runs
`.github/workflows/release.yml` on a Windows runner and attaches the executable to the
release. PyInstaller does not cross-compile, so the build has to happen on Windows —
this keeps that off anyone's laptop. The workflow also fails the build if the tag does
not match `kestrelsat.__version__`.

## Troubleshooting

### Common Issues

1. **Port Access Denied**: Make sure the port isn't being used by another application
2. **No Ports Listed**: Check if your device drivers are installed correctly
3. **Connection Lost**: Verify physical connection and device power
4. **Garbled Text**: Check baud rate and other serial parameters match your device
5. **Nothing plots**: Confirm the delimiter matches your data, and that each line is a
   complete sample. The first line after connecting is discarded, since it is usually a
   partial fragment left in the device's buffer.
6. **"PyQtGraph not available"**: Install the plotting dependencies with
   `pip install pyqtgraph PyQt5`

### Error Messages

- **"Failed to connect"**: Port may be in use or device not properly connected
- **"Connection was lost"**: Physical connection interrupted or device disconnected
- **"Invalid configuration"**: Check that all settings are valid numbers/selections

## File Structure

```
serial_gui.py                  # Entry point
kestrelsat/
    app.py                     # SerialGUI: window, tabs, dialogs, serial I/O
    plotting.py                # PlotTab: one plot's tab, data and window
    themes.py                  # Theme palettes (imports nothing else)
    theming.py                 # ThemeManager: applies a theme to the widget tree
    channels.py                # Channel record
    scaling.py                 # High-DPI display scaling
    remote_files.py            # KFS1 client, integrity and negotiated profiles
    xbee_api.py                # API transport, receive sources and diagnostics
    camera_tab.py              # Still capture/view and recovery UI
    preview.py                 # Preview scheduling and capture/cleanup ownership
    preview_tab.py             # Preview controls and native-pixel pop-out
    transfer_stats.py          # Rate/RSSI recordings and CSV export
    stats_tab.py               # Transfer graphs and RSSI overlay
    link_tracker.py            # Dedicated probe/RSSI recording
    link_replay.py             # Validated offline CSV-bundle reconstruction
    link_tab.py                # Link Tracker UI and graph exports
    graph_style.py             # Independent white/black graph formatting
    zmodem.py                  # ZMODEM file transfer protocol
    widgets.py                 # ToolTip, ScrollableFrame
tests/                         # See tests/README.md
check_ports.py                 # Standalone serial port lister
requirements.txt               # Python dependencies
build_exe.bat                  # Windows executable build script
KestrelSAT_GCS_v1.spec         # PyInstaller spec for the current build
serial_gui_settings.example.json  # Example settings file
README.md                      # This file
serial_gui_settings.json       # Auto-generated settings (created after first use)
logs/                          # Auto-generated default folder for log files
downloads/                     # Auto-generated default folder for received files
```

`serial_gui.py` stays the entry point, so the PyInstaller spec needs no changes
when the package is reorganised.

Settings and logs are written relative to the working directory the app was launched from.

## Technical Details

- tkinter/ttk for the main interface, PyQtGraph for the plot window
- pyserial for serial communication
- Reception runs on a background thread so the GUI stays responsive
- Plot redraws are throttled to roughly 30 FPS regardless of incoming sample rate
- JSON-based settings storage for user preferences
- Error handling for common serial communication issues

## Tests

```bash
python tests/run_all.py
```

On Linux these need a display: `QT_QPA_PLATFORM=offscreen xvfb-run -a python tests/run_all.py`.
See `tests/README.md` for what each suite covers.

## License

Copyright (C) 2026 Wyatt Harris

This program is free software: you can redistribute it and/or modify it under
the terms of the [GNU General Public License](LICENSE) as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version.

### Authorship

This software was written by Wyatt Harris in a personal capacity. It is **not** a
work of the United States Government, was not prepared in the course of the
author's official duties, and does not represent the official position of the
U.S. Air Force Academy, the Department of the Air Force, the Department of
Defense, or the U.S. Government. Copyright is held personally by the author,
which is what makes licensing it under the GPL possible.

The USAFA ASTRO course is mentioned only to describe what the program was built
for; it is not a claim of institutional authorship or ownership.

It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR
PURPOSE. See the [LICENSE](LICENSE) file for the full terms.

### Why GPL v3 specifically

This is not a free choice: `PyQt5` is distributed by Riverbank Computing under
the GPL v3 (or a paid commercial licence). Because the plot window links PyQt5,
a distributed build of this program has to be GPL v3 as well — GPL v2 is *not*
compatible with GPL v3, so v2 is not an option here.

The other dependencies are permissive and impose no such constraint:
`pyqtgraph` is MIT and `pyserial` is BSD.

Practically, this means anyone you give the program or the `.exe` to is entitled
to the corresponding source, under these same terms.
