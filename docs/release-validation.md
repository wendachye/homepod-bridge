# Release validation

Keep the app classified as beta until a release candidate passes both CI
and the hardware checks below. CI alone does not test WASAPI drivers, real
HomePods, Wi-Fi recovery, audio quality, or Apple firmware compatibility.

## Scope of the next release

At the user's request on September 18, 2026, playing an entire Apple Home
stereo pair by selecting only one member is deferred from this release.
Its failed listening tests remain recorded; this feature is excluded from
the release criteria, not marked passed. Do not advertise automatic
stereo-pair forwarding from a single selection.

Selecting one entry still targets that individual speaker. To target both
members, users must select both entries. The current implementation uses
independent streams for multiple selections, so synchronization and reliable
playback with both selected remain required validation for that use case.
This scope change does not waive CI, single-speaker playback, or the other
hardware checks below.

## Automated checks

CI runs portable and Windows regression tests, including the lowest
supported Python version (3.10). The release job uses Windows x64 and Python
3.12 with the hash-locked `requirements-windows.lock`, builds a wheel,
installs it, and reruns tests outside the checkout. It then builds the
windowless EXE and checks its imports, PCM decoding, repeated volume popup
creation, and normal process exit without audio hardware or network access.

The workflow uploads candidate artifacts and JSON smoke reports. They are
validation artifacts, not an automatic public release. Do not treat a green
build as evidence that the manual checks passed.

## Windows and HomePod checks

Record the commit, artifact SHA-256, Windows build, Python version (for a
source install), output device/driver, HomePod model and firmware, number of
speakers, network setup, and receiver latency setting for each run.

| Scenario | Pass condition |
| --- | --- |
| Clean install and launch | EXE starts on a machine without a development Python installation; tray, volume popup, notifications, and logs work. |
| Upgrade/move installation | Start Menu launches the current path; unique existing selections and volumes migrate correctly; duplicate old names require explicit reselection. |
| Overnight playback (at least 8 hours) | Audio remains usable across a WAV session rollover; no stuck green state or growing delay; record any dropouts and recovery times. |
| Short intermittent sounds | Repeated short sounds separated by 10–150 ms quiet periods retain their spacing, without growing gaps or dropouts. |
| Silence then playback | After 10 minutes idle, sound resumes without manual reconnect. |
| Network and speaker loss | Disconnect Wi-Fi/reboot a speaker and restore it; record observed recovery time, state changes, and whether manual intervention is needed. |
| Output device changes | Switch stereo outputs, unplug/replug an output, and put an HDMI monitor in standby; capture recovers or reports failure clearly. |
| Sleep/resume (10 cycles) | An active session resumes; a manually disconnected session stays idle. |
| User commands during discovery | Disconnect/disable auto-connect while a scan is pending; no late connection starts. Quit exits normally. |
| Duplicate speaker names | Each row selects only its intended physical speaker and retains independent volume after restart. |
| Per-room volume | Set an override, drag master away and back rapidly, and close/reopen; visible levels match the heard levels and saved settings. |
| Popup endurance (100 opens/closes) | One UI owner thread/interpreter is reused; no memory growth proportional to popup count, no Tcl abort at quit. |

Attach relevant rotating logs and smoke reports to the release record. Mark
untested combinations as untested. Investigate failures before describing
that build as production-ready.
