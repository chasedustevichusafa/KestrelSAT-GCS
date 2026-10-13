# Repository guidance

## Purpose and layout

KestrelSAT GCS is a Python desktop application for serial communication, live
telemetry plots, logging, notes, and ZMODEM file transfer. The main interface
uses tkinter/ttk; plots use separate PyQtGraph/PyQt5 windows.

- `serial_gui.py`: stable launcher; keep it compatible with the PyInstaller spec.
- `kestrelsat/app.py`: `SerialGUI`, serial I/O, tabs, dialogs, settings, logging,
  and transfer coordination.
- `kestrelsat/plotting.py`: `PlotTab`, parsing, buffers, channel controls, and
  plot windows. Each plot owns its state and parses incoming lines independently.
- `kestrelsat/channels.py`: `Channel` dataclass holding telemetry and UI state.
- `kestrelsat/themes.py` and `theming.py`: palettes and `ThemeManager`.
- `kestrelsat/scaling.py` and `widgets.py`: DPI scaling, scrolling, and tooltips.
- `kestrelsat/zmodem.py`: protocol framing, CRCs, escaping, sender, and receiver.
- `kestrelsat/__init__.py`: single source of truth for `__version__`.
- `tests/`: executable regression suites and an informational benchmark.
- `KestrelSAT_GCS_v1.spec`, `build_exe.bat`, and
  `.github/workflows/release.yml`: Windows packaging and release automation.

## Setup and commands

Use Python 3.10 or newer; the release workflow uses Python 3.11. Work from the
repository root in a virtual environment:

```bash
python -m venv .venv
# Activate .venv using the appropriate command for your shell.
python -m pip install -r requirements.txt
python serial_gui.py
python check_ports.py
```

The requirements pin pyserial, pyqtgraph, and PyQt5. tkinter is supplied by the
Python installation; Debian/Ubuntu may need `python3-tk`. Plotting dependencies
are optional at runtime: preserve the serial monitor's ability to start when
PyQtGraph/PyQt5 are absent. Use `TEST MODE` for GUI checks without a serial device.

## Implementation boundaries

- Keep Tk operations on the main thread. Reader and transfer workers post events
  through `_post_rx`; `_pump_rx` drains the bounded queue. Do not call widgets or
  `root.after()` from worker threads.
- Preserve exclusive serial-port use during ZMODEM transfers. The telemetry
  reader pauses while `_transfer_active` is set; progress returns through the
  event queue. Retain cancellation and disconnect handling.
- Preserve bounded receive buffers, queue budgets, monitor history, and channel
  and plot limits. Keep plot redraws throttled instead of redrawing per sample.
- Keep plot buffers, delimiters, sample counters, and controls per `PlotTab`.
  Clearing, pausing, or removing one plot must leave the others independent.
- Cancel scheduled callbacks and clean up curves, channel rows, and workers when
  clearing data, closing windows, disconnecting, or shutting down.
- Use the existing theme tokens and scaling helpers. Preserve token parity,
  contrast checks, user-selected channel colours, accessible controls at small
  window sizes, and non-compounding display scaling.
- Match nearby Python conventions and preserve copyright and GPL notices.
  Keep focused changes within the existing package structure.

## Validation

The suites are standalone Python scripts with their own checks and exit codes.
Use the existing runner; each suite runs in a separate process to isolate Tk roots.
No separate lint or formatting command is configured.

```bash
python tests/test_themes.py
python tests/run_all.py
```

The theme test parses `kestrelsat/themes.py` with `ast` and requires no display or
third-party dependencies. On Linux, GUI suites require Tk and a virtual display:

```bash
QT_QPA_PLATFORM=offscreen xvfb-run -a python tests/run_all.py
QT_QPA_PLATFORM=offscreen xvfb-run -a python tests/test_correctness.py
```

`QT_QPA_PLATFORM=offscreen` handles Qt; `xvfb-run` supplies a display for Tk.
Run the suite relevant to a change, and the full runner for changes spanning
serial reception, GUI lifecycle, plotting, or shared UI infrastructure.
`tests/README.md` explains coverage; consult the scripts when comments differ
from implementation. Tests that exercise worker delivery may require a real
`mainloop()` rather than repeated `root.update()` calls.

ZMODEM tests use POSIX `pty`; verify platform support before running the full
suite on Windows. Install `lrzsz` on Debian/Ubuntu for `sz`/`rz` interoperability
checks; those checks are skipped when the reference tools are missing.
`tests/bench.py` reports timing and does not assert correctness. Report skipped
tests and missing display/dependency prerequisites explicitly. Simulated and
PTY tests do not establish physical radio or hardware performance.

## Runtime files and packaging

- Settings, logs, notes, and downloads are relative to the launch directory.
  Keep `serial_gui_settings.json`, `logs/`, `notes/`, `downloads/`, virtual
  environments, bytecode, and build outputs out of commits. Use a temporary
  working directory for checks that write runtime state.
- Preserve existing user changes; inspect `git status --short` before and after
  work. Keep temporary inspection files outside the checkout.
- For a Windows build, install `pyinstaller` and `pillow`, then run
  `python -m PyInstaller KestrelSAT_GCS_v1.spec` on Windows. PyInstaller does not
  cross-compile. `build_exe.bat` uses `.venv` and deletes `build/` and `dist/`.
- Keep the launcher, logo, icon, splash screen, and bundled `LICENSE` compatible
  with the spec. Update `__version__` only when a version change is requested.
- Pushing a `v*` tag or manually dispatching the release workflow can publish
  an executable. For a release, the tag must match `kestrelsat.__version__`.
