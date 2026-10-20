# Two-HomePod synchronization investigation

Status: automated verification, the shared-clock file comparison and the full
Windows EXE listening test passed. Preparing `0.12.5rc2` as a beta candidate;
endurance and hardware recovery validation remain incomplete.

## Reproduction

The user reported echo from the start with both Living Room entries selected.
The running desktop executable was the older `dist/HomePodBridge.exe`, SHA-256
`b615555d1211855d755039d8596c1b09d05149b13c4141f27aac0addb89df16f`.
Both it and the published `0.12.5rc1` use independent RAOP sessions for multiple
selections.

The first release-EXE listening test was missed. The replay did not start:
startup discovery found only one selected HomePod. Direct discovery confirmed
both selected devices were reachable. These attempts are not audio passes.

A minimized reproduction used the production engine and RAOP transport with
direct discovery and generated live PCM, removing Windows system audio from
the experiment. A user-controlled test window made playback repeatable. The
user clarified that the tones were audible and the final simultaneous pulses
echoed. Saved evidence:
`dist/release-gate/sync-baseline-20261021-013330/result.json`.
Earlier missed/unanswered attempts remain unevaluated.

An offline timing probe used the actual engine, live WAV pipes, pyatv decoder
and RTP packet sender with recording endpoints. It compares the scheduled
playout time of identical capture samples:

```powershell
.venv\Scripts\python.exe dist/debug/check_pair_schedule.py 0.25
```

Adding a 250 ms setup delay after decoder priming produced maximum differences
of 250.113 ms and 248.912 ms in two runs (10 ms assertion failed). Removing
that delay produced 0.000 ms difference across 56,320 common samples.
These are scheduling measurements, not microphone measurements.

The old buffer trimming does not account for audio already consumed by each
decoder or the independently established timestamp origins.

## Change

- The tray selects a group supervisor for multiple PCM targets. Supported
  HomePods use a single native helper, live reader, decoder and PTP timeline.
- Both speakers must be discovered and connected before the group is announced
  ready. Directed discovery falls back to stable identifiers after DHCP changes.
- UI volume sliders address individual group members. A failed member's feedback
  restarts the whole group rather than leaving an independent session behind.
- Non-HomePod selections and missing-helper installations retain the legacy
  RAOP fallback. The CLI is unchanged. Selecting one member to automatically
  play an entire Apple Home stereo pair remains unsupported.

## Verification

- Python suite after the group change: 235 passed.
- Native helper compiled; 986 native tests passed and four upstream tests
  remained ignored. This includes the new two-receiver UDP regression,
  asserting identical audio payloads, RTP timestamps and PTP mappings.
- A separate full application preview was built in `dist/sync-preview`.
  Its packaged runtime smoke check passed, including the native helper,
  PCM decode, Windows imports and two UI open/close cycles. SHA-256:
  `cc619643d97bb385e18696ac6ba9f43d2030d705d8b6655c4ecbd705cb57157c`.
- Shared-clock file probe completed with both connections and restored the
  original bridge. The user confirmed "One synchronized beat from both HomePods":
  `dist/release-gate/sync-shared-clock-20261021-013830/result.json`.
- Full preview EXE playback passed: the user confirmed "both HomePods sound in
  sync now" after the user-started test. Evidence recorded 48 kHz stereo WASAPI
  capture, a nonzero signal, zero capture failures and restored original settings:
  `dist/release-gate/full-sync-preview-20261021-015148/result.json`.
- The versioned release build contains the same playback implementation as the
  tested preview. Its separate runtime and package checks are recorded with
  the release assets. Restart/reconnect behavior on hardware and long-duration
  audible synchronization still require validation.
- Initial tray discovery can still omit a selected device before the group
  supervisor starts; the preview test requires both endpoints in its readiness
  check and will fail rather than play a one-speaker test.

Local diagnostics are in `dist/debug`; no microphone recording is taken.
The published release assets and original desktop executable are unchanged.
