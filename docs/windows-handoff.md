# Continue on a Windows PC

This guide hands off the reviewed and fixed app from the Mac to Windows for
development and real HomePod testing. Use **`codex/windows-exe-build`**; the
changes have not been merged into `main`.

## Current state

- App version: **0.12.5rc1**, still beta pending hardware validation. Use the
  branch head for development and the exact release commit for candidate tests.
- [Candidate notes](releases/0.12.5rc1.md) describe the narrowed scope:
  whole-pair playback from one selection is deferred. Select both entries to
  target both HomePods; synchronized two-speaker playback remains unconfirmed.
- Fixes cover capture failure handling and silence timing, stable speaker
  selection and saved volumes, reconnect state, cancellation of auto-connect,
  volume window teardown, and Windows packaging.
- [Validated Windows build, September 14, 2026](https://github.com/wendachye/homepod-bridge/actions/runs/34814462386):
  Windows tests passed on Python 3.10, 3.12, and 3.14; Linux tests also passed.
  The installed Windows wheel had **219 passed, 1 skipped**. The packaged EXE
  passed imports, PCM decoding, two actual volume window creations, and normal
  shutdown.
- Subsequent Windows capture, native tests and clean Sandbox checks are
  recorded in the [September 17–18 validation log](windows-validation-2026-09-17.md).
- **Still pending:** final-candidate two-speaker synchronization, prolonged
  audible playback, controlled network/output recovery, sleep/resume and the
  remaining interactive checks. Use
  [the release validation checklist](release-validation.md) before declaring
  the app production-ready.

## 1. Get the source

For development, install **Git for Windows** and **Python 3.12, 64-bit**,
including pip, the `py` launcher, and Tcl/Tk. Open PowerShell. These commands
use the virtual environment directly, so activation and execution-policy
changes are unnecessary. Run each block in order and stop if a command fails.

For a new checkout:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\source" | Out-Null
Set-Location "$env:USERPROFILE\source"
git clone --branch codex/windows-exe-build https://github.com/wendachye/homepod-bridge.git
Set-Location homepod-bridge
git status --short --branch
```

If the repository already exists, open PowerShell in that folder instead.
Run `git status` and preserve any local work before switching branches, then:

```powershell
git fetch origin
git switch codex/windows-exe-build
git pull --ff-only
git status --short --branch
```

Do not copy the Mac's `.venv`, `build`, or Python cache folders. Dependencies
contain platform-specific binaries and must be installed on Windows.

## 2. Create the Windows development environment

Run from the repository root. Use a fresh `.venv`; if one already exists,
confirm it is a Windows Python 3.12 x64 environment before reusing it.

```powershell
py -3.12 -c "import struct, sys, tkinter; assert struct.calcsize('P') == 8, '64-bit Python required'; print(sys.version)"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 x64 with Tcl/Tk is required' }
py -3.12 -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed' }
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-windows.lock
if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed' }
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
if ($LASTEXITCODE -ne 0) { throw 'Editable installation failed' }
.\.venv\Scripts\python.exe -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed' }
```

The lock includes test and build tools. The editable install uses those
dependencies and makes source edits available immediately. Keep the lock
unchanged unless intentionally updating dependencies.

Run the regression suite and offline smoke check in an interactive Windows
desktop session; some tests briefly open real windows:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
.\.venv\Scripts\python.exe -m homepod_bridge.smoke_check .\dist\source-smoke.json
if ($LASTEXITCODE -ne 0) { throw 'Source smoke check failed' }
Get-Content .\dist\source-smoke.json
```

These checks need no HomePod. Investigate Windows/Tk test skips or failures;
do not treat skipped hardware or UI coverage as validated behavior.

## 3. Test actual playback

1. Put the PC and HomePod on a local network that permits discovery between
   devices. Guest/client isolation or filtering of mDNS can block discovery;
   using the same Wi-Fi band alone does not establish connectivity.
2. Check the Home app's speaker-access setting: this app expects access for
   **Anyone on the Same Network**. Scan should report `OK: streamable`.
3. Allow Python or HomePodBridge through Windows Firewall on your trusted
   private network when prompted.
4. Select an available **stereo** default Windows audio output. The app
   captures that output through WASAPI loopback. It does not mute the PC's
   speakers; a silent/virtual output is optional for HomePod-only sound.
   Reconnect after changing the default output.
5. Quit any running tray instance before switching between source and EXE
   testing. Disconnect other bridge sessions before CLI playback tests.

Discover speakers from source:

```powershell
.\.venv\Scripts\python.exe -m homepod_bridge -v scan --timeout 10
```

Replace `Bedroom` with your discovered speaker name, start at a comfortable
volume, and play audio on the PC:

```powershell
.\.venv\Scripts\python.exe -m homepod_bridge -v stream --device "Bedroom" --latency 0.5
```

Stop with `Ctrl+C`. To test the tray with visible diagnostics:

```powershell
.\.venv\Scripts\python.exe -m homepod_bridge -v tray
```

The icon may be in the taskbar's hidden-icons area. Right-click it, select a
speaker under **Devices**, then **Connect**. Test **Volume**, disconnect,
reconnect, and **Quit**. Only one tray instance can run at a time.

Continue with every applicable scenario in
[release-validation.md](release-validation.md). Record the commit, Windows
version, audio output/driver, HomePod model/firmware, and results. Include
logs and reproduction steps for failures. Real playback is the main remaining
work; successful offline checks alone do not finish it.

## 4. Get the release candidate

No Python installation is required to run the bundled EXE. `dist/` is
git-ignored, so **cloning or pulling does not download the EXE**.

For 0.12.5rc1, use the candidate's [GitHub release](https://github.com/wendachye/homepod-bridge/releases)
or its successful CI artifact. A draft release is visible only to repository
collaborators and is not a production approval. Check `SHA256SUMS.txt` from
that candidate; do not use a checksum from an older build.

The following download instructions and checksum refer only to the older
September 14 build and are retained for rollback:

Open that [successful build](https://github.com/wendachye/homepod-bridge/actions/runs/34814462386),
sign in to GitHub if needed, and download **windows-build-and-smoke-results**
under Artifacts. Extract the ZIP and find `HomePodBridge.exe` inside its
nested `dist` folder. It also contains the wheel and two smoke reports.
You can instead copy the existing Mac file
`homepod-bridge/dist/HomePodBridge.exe` to the Windows PC.

For that exact September 14 artifact, the EXE SHA-256 is:

```text
9ec3a132182fc87a977f4c793b47bbfe3c35ecc2676fe7a4578cc7605c43eafc
```

Check it from the folder containing the EXE:

```powershell
Get-FileHash .\HomePodBridge.exe -Algorithm SHA256
```

A newly built EXE may have a different hash. If the saved artifact expires,
use a later successful CI artifact or build from source below. Confirm that
the **windows-package** job passed; CI also uploads diagnostics from failed
builds.

Double-click the EXE to launch the tray. It accepts `--self-test` for offline
checks; the `scan` and `stream` commands above use the source CLI, not the EXE.

## 5. Build a new EXE after changes

Quit the running EXE first. From the repository root, after the tests pass:

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm --noconsole --onefile --name HomePodBridge --collect-submodules homepod_bridge --collect-submodules pyatv --collect-binaries miniaudio --hidden-import pystray._win32 launcher.py
if ($LASTEXITCODE -ne 0) { throw 'EXE build failed' }
```

Output: `dist\HomePodBridge.exe`. Both `--collect-submodules` arguments are
intentional: omitting app modules previously caused the frozen startup check
to fail. Run the EXE smoke check with a timeout, matching CI:

```powershell
$exePath = (Resolve-Path .\dist\HomePodBridge.exe).Path
$reportPath = Join-Path (Resolve-Path .\dist).Path 'exe-smoke.json'
Remove-Item $reportPath -ErrorAction SilentlyContinue
$proc = Start-Process -FilePath $exePath -ArgumentList @('--self-test', "`"$reportPath`"") -WorkingDirectory $env:TEMP -PassThru
if (-not $proc.WaitForExit(60000)) { taskkill.exe /PID $proc.Id /T /F; throw 'EXE self-test timed out' }
if (Test-Path $reportPath) { Get-Content $reportPath }
if ($proc.ExitCode -ne 0) { throw "EXE exited with $($proc.ExitCode)" }
if (-not (Test-Path $reportPath)) { throw 'Missing EXE self-test report' }
if (-not (Get-Content $reportPath -Raw | ConvertFrom-Json).ok) { throw 'EXE smoke check failed' }
Get-FileHash $exePath -Algorithm SHA256
```

The smoke test briefly opens and closes two volume windows, writes `ok: true`
on success, and exits. Then launch the EXE normally and repeat the hardware
checks for the candidate you intend to use.

## Settings, logs, and startup

- Settings: `%APPDATA%\homepod-bridge\config.json`.
- Tray log: `%APPDATA%\homepod-bridge\logs\bridge-tray.log`.
- CLI log: `%APPDATA%\homepod-bridge\logs\bridge-cli.log`.
- Windowless crash diagnostics, when present:
  `%APPDATA%\homepod-bridge\logs\crash.log`.

These files are local to each Windows user and are not in Git. Start with
fresh settings on the new PC, or back up existing settings before copying
them. Quit the tray before editing or replacing the config. Older saved
speaker names migrate only when a scan identifies a unique speaker;
duplicate names need reselection in **Devices**.

Wait until manual testing passes before enabling startup. For a source
installation in its permanent folder, run
`.\.venv\Scripts\pythonw.exe .\create_shortcuts.pyw` to create Desktop and
Startup shortcuts. For an EXE installation, create a shortcut to the EXE
in its permanent location and put that shortcut in `shell:startup` if wanted;
the Python shortcut helper is for source installations. **Auto-connect at
launch** controls connecting after launch, not launching Windows apps at login.

## Continue development or hand off to another assistant

Open this repository on the Windows PC and use this prompt:

> Read docs/windows-handoff.md and docs/release-validation.md. Continue on
> codex/windows-exe-build and inspect the current Git status before changing
> files. The Mac review fixes and Windows CI packaging are complete; real
> Windows/HomePod validation is still outstanding. Set up the locked Python
> 3.12 x64 environment, run tests and smoke checks, then help me validate
> capture, playback, reconnect, sleep/resume, and volume-window behavior on
> this PC. Record results, investigate failures, and add regression coverage
> for any code fixes. Keep untested scenarios explicit.

Before each work session, check `git status` and pull with `git pull --ff-only`
when your working tree is ready. Review changes with `git diff`, commit the
intended source/docs files, and `git push` to the same branch. Every push
triggers CI and a Windows build. Keep `.venv`, generated EXEs, local settings,
and personal logs out of source commits. Authentication for pushing must be
set up separately on the Windows PC.
