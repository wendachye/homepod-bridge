# homepod-bridge

Stream **Windows system audio** ("what you hear") to an Apple **HomePod** over AirPlay. No license, no 10-minute cutoff — an open replacement for the TuneBlade use case.

Pipeline: `WASAPI loopback (PyAudioWPatch) → int16 PCM/WAV → pyatv RAOP stream → HomePod`, wrapped in a reconnect watchdog. Audio is sent as PCM; no MP3 encoding is used in normal operation.

## Requirements

- Windows 10/11, Python 3.10+
- PC and HomePod on the **same Wi-Fi network and band** (a HomePod on 2.4 GHz won't be discovered from a PC on 5 GHz on some routers)
- HomePod speaker access set to **"Anyone on the Same Network"** (Home app → your HomePod → Allow Speaker & TV Access). This is what makes RAOP show `Pairing: NotNeeded`.

## Install

```powershell
pip install -e ".[dev]"     # from this folder (or: pip install -r requirements.txt)
```

This installs two commands: `homepod-bridge` (console CLI) and
`homepod-bridge-tray` (windowless tray app).

On first run, Windows Firewall will ask about Python — **allow on private networks**, or device discovery (mDNS) will silently fail.

## Usage

```powershell
# 1. Discover devices and verify yours is streamable
python -m homepod_bridge scan

# Example output:
#   Bedroom   192.168.1.42   RAOP=NotNeeded   OK: streamable

# 2. Stream everything you hear to it
python -m homepod_bridge stream --device "Bedroom"

# Multi-room: repeat --device to stream to several speakers at once
python -m homepod_bridge stream --device "Living Room" --device "Living Room (2)"
```

Options: `--latency 0.5` (receiver buffer in seconds), `-v` before the command for debug logs. `--bitrate` and `--quality` remain accepted for compatibility but have no effect on PCM streaming. Stop with `Ctrl+C`.

## Known limitations

- Built on pyatv's reverse-engineered AirPlay sender (its `stream_file` is
  marked incubating) - Apple firmware updates can break streaming until the
  library catches up.
- ~0.6 s latency (was ~3.2 s before 0.9.0). Most of it is the AirPlay
  receiver buffer, tunable via `raop_latency` in config.json (default 0.5 s,
  range 0.25-2.0). Raise it if audio breaks up on a busy network. Still not
  suitable for gaming; fine for music, and for video delay the audio track
  (VLC: `j`/`k`). Multi-device sync is best-effort, not sample-locked.
- If audio capture dies (sleep/resume, device removal), the tray session
  auto-restarts up to 3 times per minute, then stops with a notification.
  The CLI exits with an error so a caller can decide when to restart it.
- Only one tray instance can run; a second tray launch shows an "already running"
  dialog. Console commands are independent.
- On first run the tray registers a "HomePod Bridge" Start Menu entry. This
  is what makes Windows label its notifications (without it they are
  attributed to "Python", the host interpreter). Delete that Start Menu
  entry to undo.
- Logs: `%APPDATA%\homepod-bridge\logs\bridge-tray.log` (tray) and
  `bridge-cli.log` (CLI), rotating with 3 backups each. Import-time crashes
  under windowless `pythonw` leave a breadcrumb in `crash.log` there too.

## Known behaviour & troubleshooting

| Symptom | Cause / fix |
|---|---|
| `RAOP=Disabled` or `Unsupported` in scan | Home app → HomePod → set speaker access to "Anyone on the Same Network". |
| Device not found at all | Same Wi-Fi band? Firewall allowed? Try `scan --timeout 10`. |
| `RTSP ... SETUP failed with code 500` | Known HomePod quirk — restart the HomePod (unplug 10 s). The watchdog retries automatically. |
| Stream drops occasionally | The watchdog reconnects with backoff and a fresh PCM/WAV stream. The tray changes to connecting as soon as the failed session ends. |
| Audio delay | ~0.6 s by default. Lower it further with `raop_latency` in config.json (min 0.25 s), at the cost of jitter tolerance. Fine for music; for video, delay the audio track (VLC: `j`/`k`) — unusable for gaming. |
| Audio breaks up / stutters after upgrading | The receiver buffer is now 0.5 s instead of 1.5 s. Raise `raop_latency` in config.json (try 1.0, then 1.5 for the old behaviour) and restart the tray. |
| Stereo pair issues | HomePod stereo pairs are historically flaky with third-party RAOP senders. Test with a single unit first. |
| Multi-device offset | Each speaker gets its own RAOP session, so sync is best-effort. Two unpaired HomePods in the *same room* may have an audible offset; if they're the same model, make them a stereo pair in the Home app (they merge into one endpoint with perfect internal sync) and stream with a single `--device`. |

## Tray app (v2)

```powershell
python -m homepod_bridge tray     # with console (shows logs - use while testing)
pythonw -m homepod_bridge tray    # no console window (daily use)
```

**Left-click** the tray icon to Connect/Disconnect (when it would be greyed out - no devices selected yet - the click does nothing). **Right-click** for the menu: **Connect/Disconnect** (double-click the icon does the same), **Devices** (checkbox multi-select + Rescan), **Volume** (opens a Win11-style flyout above the taskbar - drag or mouse-wheel to set 0-100%, sent live; with two or more devices selected, the flyout adds a labeled slider per device so each room can run its own level, while the "All devices" master slider on top sets every room at once; Esc or click-away closes it), **Auto-connect at launch**, **Quit**. Icon color = state: gray idle, amber connecting, green streaming. Settings persist at `%APPDATA%\homepod-bridge\config.json`.

**Start at login (no terminal needed)**: move this folder to its permanent
location, then double-click **`create_shortcuts.pyw`** once. It creates two
shortcuts - Startup folder (launches at every login) and Desktop (manual
launch) - both running windowless `pythonw`, and confirms with a dialog.

With **Auto-connect at launch** enabled in the tray menu, streaming resumes by
itself after every boot - auto-connect retries for ~2 minutes at launch so
slow Wi-Fi/mDNS at login doesn't strand the app idle. To undo, delete
`HomePod Bridge.lnk` from `shell:startup` and from the Desktop.

Disconnecting or disabling auto-connect cancels any pending launch retries.
A manual disconnect also stays disconnected across sleep/resume until you
connect again. Selections and per-room volumes are saved by device ID;
speakers with matching names have distinct labels. Existing name-based
settings migrate when a scan finds a unique match. If an old name matches
multiple speakers, select the intended speaker again in Devices.

Note: toggling a device or reconnecting re-detects the default audio output,
so switch Windows to your silent/virtual output *before* connecting.

### Build a single HomePodBridge.exe

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install --require-hashes -r requirements-windows.lock
.venv\Scripts\python -m PyInstaller --clean --noconfirm --noconsole --onefile --name HomePodBridge --collect-submodules pyatv --collect-binaries miniaudio --hidden-import pystray._win32 launcher.py
```

Output lands in `dist\HomePodBridge.exe`. CI builds this executable and an
installable wheel, tests the wheel outside the source checkout, and runs
the EXE with `--self-test REPORT.json` to check dependencies, PCM decoding,
and volume popup reopen/shutdown. The self-test needs no HomePod or audio
capture device. These checks do not establish playback reliability; follow
[the release validation procedure](docs/release-validation.md) on Windows
with a HomePod before distributing a release.

## Development

```powershell
python -m pytest tests/ -q
```

The flyout layout tests build a real tkinter window (a small window flashes
briefly on Windows); on headless machines without a display they skip.

Capture, shell identity, and the native tray require Windows. Portable logic
tests can also run elsewhere; Tk layout tests skip if Tk/display support is
unavailable. pyatv API usage is tested against **pyatv 0.18.0**.

Release build dependencies are locked for Windows x64 / Python 3.12 in
`requirements-windows.lock`. To intentionally refresh them, use
`uv pip compile pyproject.toml requirements-build.in --extra dev --python-version 3.12 --python-platform windows --generate-hashes -o requirements-windows.lock --upgrade`
and rerun CI plus the hardware validation procedure. See the
[uv locking documentation](https://docs.astral.sh/uv/pip/compile/).
