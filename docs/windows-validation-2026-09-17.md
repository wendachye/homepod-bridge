# Windows validation, September 17, 2026

Current candidate: version **0.12.4** on
`codex/windows-exe-build`, originally validated locally against base
`e41ec5129f9c58d71cc2d62a03615b3a4126e383`.
Status: **beta; the native encryption/discovery test blockers are repaired,
and the final Windows Python suites pass without skips. Single-selection
listening, Linux CI and the extended hardware checks remain unconfirmed**.

## 0.12.4: blocked automated checks completed

The five upstream encrypted-channel failures came from adding a second
frame around output that `ControlCipher` already frames, plus inconsistent
length parsing and a raw API that returned framed bytes. The wrapper now
delegates framed encryption/decryption to the active cipher and exposes raw
output only for a single nonempty block of up to 1024 plaintext bytes.
The length parser includes the authentication tag separately. Legacy
big-endian test expectations were corrected to the same HomeKit framing
used by the existing sender. Four new tests independently check wire
interoperability, multiple blocks, tag/length accounting and raw limits.
All four failed before the patch and pass after it.

Discovery's async stream repeatedly used blocking `recv_timeout` calls
without yielding when the network was quiet. That prevented the runtime
from polling its own timeout; scan also blocked cancellation. Both paths
now select between asynchronous receiver futures and timers. Two new
subprocess-bounded tests exercise the real current-thread runtime, so a
regression fails within three seconds instead of hanging CI. Both failed
before the fix. The original physical-network browse test now emits events
for all three HomePods and finishes at its ten-second deadline.

The Windows Tcl failure was exposed by treating unexpected constructor
errors as failures. A full run reproduced the `init.tcl` read error under
pytest's file-descriptor capture. Switching to Python-stream capture
(`--capture=sys`) produced clean full-suite results. Pytest's Windows
capture implementation closes/duplicates native standard-stream handles;
the observed difference is consistent with interference with Tcl's retained
native handles. Standalone creation of 100 interpreters also passed.
Application UI code was not changed. The project and outside-checkout wheel
CI step now select stream capture, and actual Tcl errors can no longer be
silently counted as missing-display skips.

| Final check | Result |
| --- | --- |
| Windows Python 3.10.21 | **228 passed, zero skips**, 15.01 s. |
| Windows Python 3.12.14 | **228 passed, zero skips**, 15.64 s. |
| Windows Python 3.14.6 | **228 passed, zero skips**, 13.93 s. |
| Installed 0.12.4 wheel outside checkout | Verified the import comes from the temporary environment's installed wheel; **228 passed, zero skips**, 14.48 s. Shared locked dependency directory; wheel smoke passed. |
| Native helper and nine supporting libraries | **976 passed, zero failures** in the build's normal suite. Four optional tests were subsequently exercised: two audio-file checks and two real-network discovery checks passed, for **980 unique passing native tests**. |
| Original discovery timeout reproduction | Both real-network tests passed, including the formerly stuck browse test, which finished at **10.00 s**. |
| New native regressions | Four cipher interoperability/raw-frame tests and two bounded discovery tests observed failing before the fix and passing after it. Existing three UDP PTP/NTP tests remain green. |
| EXE smoke | Passed codec/import checks, bundled helper startup, real popup reopen/shutdown and normal exit. |
| Popup endurance | 100 opens/closes, one interpreter and one owner thread, normal shutdown; final private-byte sample 14,651,392. |
| Package verification | Wheel Python files match the checkout; source ZIP integrity and all modified native/test files match the prepared source. Extracting the helper from the EXE confirms byte-for-byte equality with the newly built native helper. |

The build now reapplies the two dependency corrections from the verified
original archive, using `scripts/native_patches.py`, and runs all nine
supporting library suites. CI includes a build timeout and the bounded
discovery regressions. No tests were removed to make the results green.
Upstream documentation examples remain explicitly ignored, and the unused
terminal-UI/Bluetooth crates are outside this sender's tested dependency set.

Current artifacts:

- `dist/release-fixes/exe/HomePodBridge.exe`, 35,216,900 bytes, SHA-256
  `b615555d1211855d755039d8596c1b09d05149b13c4141f27aac0addb89df16f`.
- `dist/release-fixes/wheel/homepod_bridge-0.12.4-py3-none-any.whl`, SHA-256
  `ddf6a8f0db3370d53be71bffb5ab577a2ff9b9dc96482ccb834dd7d9ddaf92d7`.
- `dist/native/HomePodSender.exe`, SHA-256
  `77a002b9e251ab3247b51fd0143f89875e9aa25c59ac8335bfac8cd3d7228c05`.
- `dist/native/HomePodSender-source.zip`, SHA-256
  `21b3e7cf6ee46590f7a908ac52ec8a9ba415f89531a975e40d7168aafdab1ae3`.

Evidence: `dist/release-fixes/`, including Python JUnit XML, native build
logs, network discovery output, EXE/wheel smoke reports and
`artifact-verification.json`. Red reproductions and temporary diagnostics
are in `dist/release-fixes/debug/`. The previously running 0.12.3 EXE was
not replaced or relaunched during this work.

### Remaining release gates

Linux tests could not run locally: the installed Docker Desktop engine
crashes during startup because its `Docker/run/dockerInference` socket
cannot be accessed. Stopping the instance started for this check and trying
to rename that temporary socket did not repair it; the rename failed with
the same filesystem error. Docker images, containers and settings were not
reset. At the end of this local check the changes were uncommitted and
GitHub CI had not run. The follow-up validation will use the CI checks
attached to the candidate's commit on `codex/windows-exe-build`.

A listener is still needed for the single-selected-target test. No new
claim of audible stereo synchronization follows from these automated
repairs. Eight-hour playback, ten sleep/resume cycles, network/output-loss
recovery and a clean Windows machine without development Python are still
pending. The recommendation remains beta until those checks pass.

## Earlier expanded release checks on 0.12.3

The following checks were run after the user requested all tests. These
are local Windows results, not a GitHub Actions result for a committed
release candidate. No application code was changed during this test run.

| Check | Result |
| --- | --- |
| Full Python suite, Windows / Python 3.10.21 | 227 passed, 1 Tk initialization skip; 15.81 s. Used a new isolated environment and the project dependency requirements, as in the CI test job. |
| Full Python suite, Windows / Python 3.12.14 | 227 passed, 1 Tk initialization skip; 14.60 s. Used the existing locked build environment. |
| Full Python suite, Windows / Python 3.14.6 | 227 passed, 1 Tk initialization skip; 14.41 s. |
| Fresh 0.12.3 wheel outside the checkout | 227 passed, 1 Tk initialization skip; 14.33 s. A temporary virtual environment imported the installed wheel from its own site-packages, with the locked dependency directory shared read-only. Wheel smoke checks passed. |
| Tk follow-up | All 16 rendered UI tests passed in isolation. A full rerun with a constructor diagnostic wrapper passed all 228 tests. A separate reporting-only probe reproduced the intermittent skip and captured its underlying Tcl error; see below. |
| Native sender and supporting libraries | 965 passed, 5 failed, 4 optional tests initially ignored. Includes all nine libraries used by the sender and the helper's three PCM tests; all three actual UDP PTP/NTP regression tests passed. |
| Four optional native tests | Three passed: WAV generation, 1,000 packets encoded from the existing test WAV, and a local scan finding all three HomePods. The browse-events test hung beyond its intended ten seconds and reproduced a 30-second timeout in isolation. Overall unique native outcome: **968 passed, 5 failed, 1 timed out**. |
| Native documentation tests | Command completed successfully; six upstream examples are explicitly ignored, and no executable documentation tests ran. |
| Dependency consistency | Passed for Python 3.10, 3.12 and 3.14. |
| Fresh wheel and separate EXE build | Both built successfully from the current source. The existing EXE was retained. |
| Existing and rebuilt EXE smoke | Both passed imports, PCM decode, native helper startup, actual popup reopen/shutdown, and normal exit from a temporary working directory. |
| Popup endurance | 100 real opens/closes passed, with one interpreter and one owner thread. Private bytes were 14,938,112 at every ten-window sample. This is a short endurance check, not an overnight result. |
| Passive WASAPI capture | 5.015 seconds, 234 chunks / 958,464 bytes, zero failures. All chunks were silent, so audible capture fidelity remains untested. No captured audio was saved or transmitted. |
| Artifact integrity | Wheel and native source ZIP integrity passed. All 19 Python module files in the wheel match the checkout. Archived helper, packet tests, patched connection, and Windows RTP source match the prepared build source. |

The original sandboxed test/build attempts encountered Windows temporary
directory access errors. The successful runs above used normal desktop
permissions. The intermittent Tk skip is a separate issue: the original
exception says Tcl could not read its `init.tcl` file, with `No error` as
the OS error text. The test currently labels any constructor `TclError`
as "no display available". The skip affects different rendered tests on
different runs. Its cause is not resolved; do not count the skipped tests
as passed merely because an instrumented rerun or a standalone UI run
succeeded. Evidence: `skip-context.json`, `skip-context-tests.log`,
`tk-diagnostic.log`, and `tk-full-diagnostic.log` in the evidence directory.

### Reproducible native failures

Five tests in `airplay-pairing/src/channel.rs` fail:

- `encrypt_decrypt_roundtrip`
- `framed_message_has_correct_format`
- `multiple_messages_with_incrementing_nonces`
- `parse_frame_length_works`
- `raw_encrypt_decrypt_roundtrip`

The narrow reproduction also fails all five:

```powershell
cargo test --offline --locked --release --manifest-path dist/airplay2-rs-a2f980cf25bf13ae20158497cc2d2ea69cd88fa7/Cargo.toml -p airplay-pairing --lib channel::tests
```

Errors include a 24-byte frame where the test expects 22 bytes, inconsistent
frame-length parsing, and failed decryption round trips. The pairing
channel, underlying cipher, core error source and Cargo lockfile match the
verified pinned upstream archive byte-for-byte. These failures are not
introduced by the local PTP patch. The active client/RTSP connection code
uses `ControlCipher` directly rather than the failing `EncryptedChannel`
wrapper, so this result alone does not establish the cause of the reported
speaker silence. The dependency failures still need a documented resolution
or impact assessment before release; they have not been suppressed or fixed.

The optional discovery test
`browser::tests::integration::browse_emits_events_for_real_devices` emitted
events for all three HomePods but never reached its ten-second timeout.
The first run was stopped after the test runner reported it had exceeded
60 seconds. An isolated reproduction also failed to exit within a strict
30-second process deadline; the harness killed and reaped that test process.
This is not a "no speakers found" result. Evidence:
`native-optional-tests.log`, `native-discovery-reproduction.log`, and
`native-discovery-reproduction.json`. Reproduce with
`python dist/release-validation-0.12.3/reproduce_discovery_timeout.py` using
the retained native test binary. The same run's scan-only test finished
successfully. No bridge stream or speaker selection was changed for these
tests.

Evidence is under `dist/release-validation-0.12.3/`: Python JUnit XML and
logs, native library/helper logs, the narrow pairing reproduction,
`upstream-comparison.json`, smoke reports, and `artifact-verification.json`.
Native terminal UI and Bluetooth crates are outside the bundled sender's
dependency set and were not tested.

### Artifacts from this run

- Existing `dist/HomePodBridge.exe` remains SHA-256
  `ec1b228930ef3936fdefea80eef93ee6f7ef2e7aed28daa4795fb9912d3f3897`.
- Separate validation EXE, `dist/release-validation-0.12.3/exe/HomePodBridge.exe`,
  35,217,263 bytes, SHA-256
  `9ad35b3ae408568962e31972b83c6461fd2940876c4020447cdfb2cf41ff1320`.
- Fresh wheel, `dist/release-validation-0.12.3/wheel/homepod_bridge-0.12.3-py3-none-any.whl`,
  SHA-256 `63d227d5834dbdf5c0746f907a4e2dcf942e38cf5e7ad8cb2b467a278848c9b1`.
- Native helper and corresponding-source ZIP hashes remain the values in
  the 0.12.3 artifact list below. Windows builds are not established as
  byte-for-byte reproducible.

Linux CI, a clean Windows PC without development Python, native
single-selection music through both HomePods, acoustic synchronization,
eight-hour playback, network/output loss and ten sleep/resume cycles
remain unverified. A successful local build or discovery scan does not
satisfy those checks. The release recommendation remains **beta**.

## 0.12.3: live sender silence

The user confirmed that selecting only Living Room produced **no sound
from either HomePod**, even after waiting. The native helper was running
and sending packets at 48 kHz input, so a connected state did not establish
audible playback. The working 0.12.2 result with both entries checked used
the independent pyatv path.

Comparing the upstream connection entry points found that file playback
called `streamer.set_ptp_sync_mode(clock_id)` after PTP setup, while live
playback omitted it. A localhost UDP regression fixture drives the real
`Connection.start_streaming` and `start_streaming_live` methods after
session setup, receives the actual synchronization packets, and asserts
their payload type and the PTP clock identity. It does not require a
HomePod or transmit audio outside loopback.

Reproduction:

```powershell
cargo test --locked --release --manifest-path dist/airplay2-rs-a2f980cf25bf13ae20158497cc2d2ea69cd88fa7/Cargo.toml -p airplay-client --lib bridge_ptp_sync_tests -- --nocapture
```

Before the correction, the file PTP and live NTP controls passed; live PTP
failed with `left: 84, right: 87`. After applying the missing PTP setup,
all three packet tests passed (1.37 s), including the expected clock ID.
The three existing native PCM tests also pass. The build script applies
the pinned upstream patch, installs the regression fixture, runs both
test groups, and includes the correction in the corresponding-source ZIP.

A separate CLI initialization check found that a native stream without a
tray callback stayed at the helper's initial muted volume. Standalone
native streams now explicitly apply the existing 50% bridge default;
tray streams continue to apply their saved volume. The regression went
from no volume calls to `[50.0]`. All 39 targeted Python tests pass.

Evidence: `dist/debug/ptp-red.log`, `dist/debug/ptp-fixed-build.log`,
`dist/native-package-build.log`. The file-tone baseline was repeated;
its listening response is pending. The packet regression establishes the
protocol fix; real music output still needs the user's confirmation.

After launching 0.12.3, the user reported "sounds ok now". Inspection at
that point found both Living Room entries selected, volume 38%, and
independent pyatv sessions connected at 01:45:14 and 01:45:15. Record the
current two-stream music result as user-confirmed usable. Do not attribute
it to native single-target stereo playback. The working selection was
left unchanged; reconnect synchronization and long-duration stability
remain untested.

The 0.12.3 frozen startup check passed, including the bundled helper,
PCM decoding, Windows imports and actual popup reopen/shutdown. It was
launched at 01:41:29 local time using the existing single-target selection.
Current artifacts (also recorded in `dist/ptp-artifacts.json`):

- `dist/HomePodBridge.exe`, 35,215,301 bytes, SHA-256
  `ec1b228930ef3936fdefea80eef93ee6f7ef2e7aed28daa4795fb9912d3f3897`.
- `dist/native/HomePodSender.exe`, 4,367,360 bytes, SHA-256
  `67f77667699b855d4e36019ba66c0c0d04cc4b0a89d36673f9c0d8424d194cca`.
- `dist/native/HomePodSender-source.zip`, 101,908,905 bytes, SHA-256
  `72b2ef732a6e643cca5fcc794ddb7aa9ea18b7dfb2b586b3365bb7b389e6565a`.
  ZIP integrity passed; both test/helper sources match the repository, and
  the archived live connection method contains the PTP correction.
- `dist/homepod_bridge-0.12.3-py3-none-any.whl`; installed-wheel checks
  below apply to 0.12.2. Current 0.12.3 targeted source tests and frozen
  smoke checks are recorded above.

## 0.12.2 investigation and checks

- User confirmed an iPhone plays through both members of the Apple Home
  stereo pair. Both are HomePod (2nd generation), firmware 26.6.
- A separate AirPlay 2/PTP sender built from airplay2-rs revision
  `a2f980cf25bf13ae20158497cc2d2ea69cd88fa7` sent a quiet alternating L/R
  44.1 kHz tone to Living Room. User explicitly confirmed: "Yes, both
  speakers alternated." This establishes pair routing, not measured
  acoustic synchronization or long-term reliability.
- Integrated the helper with the existing WASAPI capture, tray volume,
  retry watchdog and teardown. One selected HomePod uses the native
  helper when bundled; multiple selected targets retain independent
  pyatv sessions. Initial native test selection was just Living Room,
  volume 50%; the user later selected both entries.
- First live 48 kHz run exposed a fixed-input resampler error: a
  352-frame input block was smaller than the required 1024 frames.
  The helper now retains partial chunks and feeds whole 1024-frame
  blocks; its tests cover sample order, stereo preservation, mono
  duplication, and actual 48-to-44.1 kHz resampling.
- Corrected EXE launched successfully and connected to Living Room
  at 01:05:44 local time. The log later recorded a normal stream end
  and capture restart, followed by a new connection at 01:06:54;
  the trigger was not established. The sender remains active. A
  listening check for both speakers and audible echo was requested. User
  subsequently reported "sounds ok now" and explicitly confirmed both
  entries were checked. The logs show independent pyatv sessions at that
  point, so this validates the current two-stream listening result, not
  music through the native stereo-pair sender. Reconnect stability and
  long-term synchronization remain untested.
- PTP startup still logs a five-second timeout waiting for the first
  clock offset and starts with zero, as in the successful tone test.
  Sender scheduling jitter and a brief cluster of UDP buffer-space
  errors (Windows 10055 at 01:07:28) were logged. Their audible effect
  has not been established. Do not infer sample-accurate timing or
  uninterrupted playback from the connection status.

Current verification:

| Check | Result |
| --- | --- |
| Python source suite | 226 passed, 1 display-dependent test skipped; 14.42 s. |
| Installed 0.12.2 wheel outside checkout | 225 passed, 2 Tk display-dependent tests skipped; 13.88 s. Verified import from site-packages and restored editable installation. |
| Native release tests | 3 passed, including the real 48 kHz resampler. |
| Installed wheel and current frozen EXE smoke | Passed PCM decoding, Windows imports, real popup reopen/shutdown; EXE also passed bundled helper startup. |
| Corresponding-source archive | ZIP integrity passed; helper source matches the current repository file; vendored dependencies and offline Cargo configuration included. |
| Existing launch shortcuts | Startup, desktop and Start Menu now target the validated `dist/HomePodBridge.exe`; original shortcuts backed up under `dist/shortcut-backup-20260917`. |
| Shortcut-test isolation | Found that `test_install_validates_before_creating_shortcuts` called real native identity registration despite mocking PowerShell, replacing the actual Start Menu shortcut with a temporary test target. Mocked that separate side effect, repaired the shortcut and verified all 7 shortcut tests pass. |

Current outputs:

- `dist/HomePodBridge.exe`, 35,215,220 bytes; SHA-256
  `d29a3f3d0a2325255166b13df4fe5c270976072fec17c3de73d898175bdd337d`.
- `dist/native/HomePodSender.exe`, 4,364,288 bytes; SHA-256
  `3215136cb8f0f588ed1650d3ef7d9a1c52e35106113c666dc5c6da6fcb5b631d`.
  The helper embedded in the running EXE was linked in the preceding
  build from the same corrected source; its SHA-256 is
  `f9a65274f3cd462b8e2cc99e8962bba2cf6792c8264bad92b89bd96cd6790973`.
  Byte-for-byte reproducible Windows linking has not been established.
- `dist/native/HomePodSender-source.zip` and GPL license/notices.
- `dist/homepod_bridge-0.12.2-py3-none-any.whl` (Python fallback only).
- Evidence: `dist/stereo-exe-smoke.json`, `dist/stereo-wheel-smoke.json`,
  `dist/native-live-tests.log`, `dist/native-package-build.log`.
- Original EXE retained as `dist/HomePodBridge-0.12.1.exe`.

The sections below record the earlier 0.12.1 investigation. The full
hardware release checklist has not been completed for either candidate.

## Environment and artifacts

- Windows 11 Pro, version 10.0.26200, build 26200, x64.
- Project-local Python 3.12.14 x64, provisioned with uv because the existing
  system installation was Python 3.14. The runtime is under ignored
  `dist/python`; `.venv` depends on it, so retain that directory.
- Dependencies installed from `requirements-windows.lock` with hash checking.
  `pip check` passed. Editable development installation restored after wheel tests.
- Default loopback: LG ULTRAWIDE (NVIDIA High Definition Audio), 48 kHz, stereo.
  Driver version and network topology were not collected.
- Built `dist/HomePodBridge.exe` (33,366,210 bytes), SHA-256:
  `b4151cecd61768ee6859a20824b22d7c6729c636c152525e9e160d6adda9c4d7`.
- Built `dist/homepod_bridge-0.12.1-py3-none-any.whl`.

## Completed checks

| Check | Result |
| --- | --- |
| Source regression suite | 220 passed, no skips, 15.13 seconds. |
| Installed wheel outside checkout | Confirmed import from site-packages; 220 passed, no skips, 14.09 seconds. |
| Source, wheel, and frozen smoke checks | All passed imports, PCM decoding, Windows dependencies, two real popup creations, and normal teardown. EXE launched with a temporary working directory. |
| Popup endurance from source | 100 actual windows; one interpreter and one owner thread; normal shutdown. Private bytes were 14,934,016 at every ten-window sample from 10 through 100. This short automated run does not establish long-duration stability. |
| Passive WASAPI capture from source | Five seconds, 234 chunks / 958,464 bytes, no failure callbacks. All chunks were silent; non-silent capture fidelity remains untested. Audio was neither saved nor transmitted by this check. |
| Local discovery | Living Room, Living Room (2), and Master Bedroom all reported `RAOP=NotNeeded`, `OK: streamable`. |

Local evidence (ignored build outputs): `dist/source-smoke.json`,
`dist/wheel-smoke.json`, `dist/exe-smoke.json`, `dist/capture-smoke.json`,
`dist/popup-endurance.json`, and `dist/build.log`. The two hardware/UI probe
scripts are also retained locally in `dist`.

## Remaining hardware validation

An existing system-Python tray process was already running with both Living
Room speakers selected, auto-connect enabled, master volume 38%, and receiver
latency 0.25 seconds. It was left running; its old logs are not evidence that
this candidate passed playback testing. No new streaming session was started.

Continue with a listener and an explicitly selected speaker. Quit the old
tray through its menu before launching the candidate EXE. Record the HomePod
model/firmware and test actual audio, per-room volume, silence/resumption,
network loss, output changes, ten sleep/resume cycles, and eight-hour playback
using [the release checklist](release-validation.md). Clean launch on a PC
without Python, installation relocation, notifications, duplicate physical
speaker names, and interactive commands during discovery also remain untested.

## Follow-up: two-speaker synchronization failure

The user reported that music was out of sync with two devices connected.
Mark two-speaker audible synchronization as **failed**, despite the passing
offline packaging and UI tests above. The exact acoustic offset, whether it
grows with time, and the HomePod models/firmware are not yet established.

An offline probe in `dist/sync_probe.py` exercised the real `PcmPipe.live`,
pyatv decoder (`open_source`), and RAOP `StreamClient.send_audio`. Synthetic
PCM samples carried frame numbers so their capture time could be compared
with the sender's scheduled playback time. Network transports were inert;
this did not stream audio or measure sound from the physical speakers.

Reproduction command:

```powershell
.\.venv\Scripts\python.exe dist/sync_probe.py
```

Two sessions started 150 ms apart. Introducing a 120 ms delay after one
decoder opened, representing an awaited setup/volume operation, produced
101.14 ms and 101.86 ms of scheduled offset on two runs. The probe failed
with `AssertionError: Same captured audio scheduled 101.9 ms apart`.
The control run removed only that extra delay and measured 9.93 ms.
These are measurements of the synthetic sender scenario, not the user's
audible delay or a guarantee of timing on another run.

The app starts independent `stream_file` sessions. PCM priming and the
engine's periodic trimming only limit the app's local queue; they do not
account for decoded audio buffered inside pyatv or tie the receivers'
playback timelines to the capture clock. The existing queue-size tests
therefore cannot establish speaker synchronization. A software correction
needs capture timestamps carried through decoding and coordinated playback
timing, with a receiver-level regression test and real listening validation.
Changing the common receiver latency alone does not provide that.

At this stage no synchronization fix had been applied, and the original
0.12.1 EXE was unchanged.

## Follow-up: stereo-pair workaround failed

After creating a stereo pair and selecting only Living Room in the bridge,
the user reported **only one speaker playing**. The saved config and latest
connection log confirmed a single Living Room session. Mark stereo-pair
playback as **failed**; the earlier suggestion that pairing would provide
synchronized output from a single target was not supported by testing.

Discovery identified both Living Room speakers as HomePod (2nd generation),
firmware 26.6. They initially advertised the same AirPlay group identifier,
with Living Room marked as leader. A later scan showed different group
suffixes and both devices advertising leadership. Group advertisements alone
do not establish stereo routing or prove that both speakers receive audio.

The installed pyatv 0.18.0 selects AirPlay V2 automatically for both devices;
forcing V2 would not change that selection. Its audio setup uses NTP timing,
sets `senderSupportsRelay` and `groupContainsGroupLeader` to false, and starts
an independent stream to the selected address. Its exposed settings have no
stereo-pair routing or shared PTP timing option. The inspected
[upstream sender](https://github.com/postlund/pyatv/blob/master/pyatv/protocols/raop/protocols/airplayv2.py)
uses the same setup. These facts support a sender-capability investigation;
they do not prove that flipping an individual setup flag would fix playback.

The user subsequently confirmed native iPhone AirPlay plays through both
members of the pair. That result supported investigating the bridge's
sender. The native sender implementation and its current evidence are
recorded at the beginning of this document.
