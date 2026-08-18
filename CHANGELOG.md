# Changelog

## 0.12.1 - fix: silence forever after the display sleeps
- The real cause of "it never reconnects after sleep". The machine had not
  actually suspended at all (verified: zero suspended time since boot). The
  trigger is the **display** entering standby: the default output here is
  the monitor's HDMI audio, and Windows removes that endpoint while the
  display sleeps, then restores it with the SAME id on wake.
- The device watcher treated a failed probe as transient noise and skipped
  it, and on wake the id matched the baseline, so nothing ever fired. The
  capture stream stayed bound to the dead endpoint, delivered no frames,
  and the read loop dutifully injected silence - so there was no error to
  recover from and the HomePods played silence indefinitely.
- The watcher now counts a sustained absence and restarts capture when the
  endpoint comes back, which re-detects the device and rebuilds the session.
- Also corrects 0.12.0's sleep detection, which could never have worked:
  CPython implements `time.monotonic()` with QueryPerformanceCounter, which
  keeps counting across S3, so comparing it against the wall clock showed
  no jump. It now uses QueryUnbiasedInterruptTime, the documented clock
  that stops while suspended.

## 0.12.0 - reconnect after the PC wakes from sleep
- After a sleep/wake cycle the tray stayed on a session that had died
  hours earlier and never reconnected. Confirmed from a real log: **five
  hours of complete silence** across the sleep - no capture failure, no
  reconnect attempt, no error of any kind.
- Cause: nothing in the stack can notice. RAOP audio goes out over UDP, so
  sending to a HomePod that vanished never raises; the capture thread keeps
  injecting silence when the machine is quiet, so our pipe never starves;
  and therefore pyatv's `stream_file` never returns and the reconnect
  watchdog never runs. The engine's capture-recovery path never fires
  either, because capture never actually failed.
- Fix: the tray now watches for the machine being suspended by comparing
  wall-clock against monotonic time (monotonic does not advance across
  sleep on Windows). On resume it restarts the session - or runs
  auto-connect if it was idle and auto-connect is enabled. Restarting also
  re-detects the output device, which sleep/wake commonly changes.

## 0.11.1 - fix: the app killed itself after using the volume flyout
- Diagnosed from Windows Error Reporting: `pythonw.exe` faulting in
  `tcl86t.dll`, exception `0x80000003` (a Tcl panic), three times in one
  evening with no Python traceback and nothing in the log - the process
  simply vanished and the tray disappeared.
- Cause: Tk's interpreter must be deleted by the thread that created it.
  The volume flyout runs on a short-lived worker thread, so once that
  thread exits the interpreter can only be finalized by a garbage
  collection - on whatever thread happens to trigger one. When that is not
  the creating thread, Tcl calls `Tcl_Panic` and aborts the whole process.
  The 0.6.0 fix only covered the quit path; these crashes happened
  mid-session, minutes after the flyout was closed.
- Fix: closed flyouts are retained permanently (`volume_slider._RETIRED`)
  so the finalizer is never reachable. What leaks is a dormant interpreter,
  not widgets or timers - the window itself is destroyed first.
  Note a forced `gc.collect()` on the worker thread does NOT work: a global
  collect also finalizes Tk objects owned by other threads, panicking for
  the same reason. That was tried and reproduced the crash in a test.
- At quit, the process now exits without running interpreter finalization
  (only when a flyout was created), since Python would otherwise finalize
  that retained interpreter from the main thread and turn a clean quit into
  a crash. Config, logs and RAOP sessions are all flushed beforehand.
- Regression tests: the retention contract, plus a harness that opens and
  closes a flyout on a worker thread and then garbage-collects on the main
  thread - the exact sequence that was killing the app.

## 0.11.0 - multi-room sync at the protocol floor
- Every device now starts its stream the same distance from live. A RAOP
  receiver plays each sample at an absolute time derived from *where its
  stream began* - `play_time(sample s) = T0 + (L - a)/rate + s/rate`, where
  T0 is that session's anchor and `a` the capture position of its frame 0 -
  so whatever backlog sat in a device's buffer when it first read became
  that device's permanent offset. Devices connect seconds apart, so they
  each locked in a different one. Priming every pipe to the same small
  margin at its first read makes T0 and `a` cancel, putting all devices on
  one schedule regardless of connect order.
- Measured playback skew between two HomePods: **11.3 ms**, which is the
  NTP clock-recovery floor for WiFi (each receiver estimates our clock
  independently; pyatv implements no PTP). This is as tight as two
  independent RAOP sessions can be.
- The fix only ever DISCARDS backlog, so it lowers latency rather than
  adding any - correcting sync by delaying the leading device was rejected
  for that reason.
- Note on measurement: send-time skew is NOT playback skew. Pacer drift,
  network jitter and buffering only consume the receiver's margin; they do
  not move playback. Measuring transmit times gave 16-32 ms and was
  meaningless. Only (T0 - a) matters.

## 0.10.1 - resync now pulls toward live, not just level
- 0.10.0 equalised the rooms against each other, which fixed sync but left
  whatever backlog they happened to share at connect - measured 107-128ms
  standing in both buffers for a whole session. Because RAOP paces at exact
  realtime and never catches up, that was permanent added latency.
- The resync task now trims every device toward a 50ms working margin
  rather than toward the group minimum, and runs for single-device sessions
  too. Re-measured live: standing occupancy **median 0ms** (was 107/128ms),
  skew **median 0ms**. Roughly 110ms of latency removed on top of sync.

## 0.10.0 - multi-room drift correction
- Speakers now hold a common live edge. Each device runs its own RAOP
  session with its own buffer, and the backlog standing in that buffer IS
  how far behind live it plays. Devices connect seconds apart and
  independently discard different amounts of startup backlog, so they
  settled at different offsets: measured between two HomePods, **21 ms
  typical, 64 ms p95, 320 ms peak** - audible as a smear or echo. A resync
  task now trims any laggard back to the most-live device once it drifts
  past 20 ms. Re-measured live: **0 ms median skew** (p95 43 ms).
- Note this corrects OUR contribution only. Two HomePods remain two
  independent AirPlay sessions and are not sample-locked. For speakers in
  the same room, pairing them in the Apple Home app is still the right
  answer: they become a single endpoint with Apple's internal sync, and the
  bridge streams to one target.

## 0.9.2 - fix: silence injection over-produced during playback
- The 0.7.1 keep-alive injected silence on a fixed per-chunk deadline, but
  WASAPI hands over 480-frame packets every 10ms and 480 does not divide
  CHUNK_FRAMES, so a full chunk is ready only every 20-30ms. The deadline
  therefore expired early on every longer gap and injected a chunk nothing
  asked for - about 6 times a second **while music was playing**. Measured
  on the real device: **1.1327x realtime**, i.e. 13% more audio than exists.
  RAOP drains at realtime only, so the excess piled up as permanent latency
  until the live buffer clipped it, and then dropped ~2.1s of audio per 45s
  (continuous dropouts).
- Silence is now injected only after a real gap in delivery
  (`IDLE_GRACE_SECONDS = 0.2`), which is the only thing that actually means
  the machine went quiet. Verified on the real device with audio playing:
  **0.9991x realtime, zero injected chunks**; the fully-idle keep-alive path
  is unchanged.
- This was invisible to earlier testing because every test - including the
  0.7.1 measurements and the live session runs - was taken on a SILENT
  machine, the one case the old loop got right. There is now a regression
  test driving a WASAPI-accurate bursty stream.

## 0.9.1 - fix: the WAV header could be dropped from the live stream
- The bounded buffer drops its OLDEST bytes to stay near-live, and the WAV
  header written in 0.9.0 was by definition the oldest bytes. Once ~1s of
  audio arrived before pyatv's first read - which is exactly what the live
  path does - the header was discarded and pyatv received unidentifiable
  raw PCM. The visible symptom was an "ERROR root: Failed to parse
  metadata" traceback per connect (reported from a real session log); the
  underlying risk was losing the stream's format identity entirely.
  The header is now served from outside the buffer, so it can never be
  dropped, and the buffer holds pure audio (its bound is now purely a
  latency bound). Verified against real HomePods: the error is gone.
- Buffer drops are now frame-aligned. Discarding a non-multiple of the
  frame size from raw PCM would shift the channel interleave for the rest
  of the session; `StreamBuffer(align=...)` trims to a frame boundary.

## 0.9.0 - latency cut from ~3.2s to ~0.6s
Measured on Windows 11 by driving pyatv's real code offline; both changes
verified end to end through the engine's own pipe construction.

- **Audio is now sent as raw PCM instead of MP3 (saves ~1.62s).** pyatv
  transmits uncompressed PCM to the receiver regardless of what it is fed,
  so the MP3 stage only meant pyatv immediately decoded it back again - a
  lossy transcode that saved no bandwidth and cost enormous latency: its
  decoder will not start until it has filled a 64 KiB buffer, which at
  320kbps takes 1.64s of wall time, and because RAOP paces strictly at
  realtime and never catches up, that startup stall became permanent delay
  for the whole session. Measured `open_source()`: 1644ms -> 1.6ms, first
  audio 1644ms -> 22ms. (Counter-intuitively this also means lowering the
  bitrate used to make latency *worse*, not better.)
  The WAV header's declared length matters: 0xFFFFFFFF is a scan-me
  sentinel that makes the parser read the stream forever (measured: still
  stalled after 12s), so a large finite length is used instead.
- **The AirPlay receiver hold is now configurable, default 0.5s (was a
  hardcoded 1.5s, saves 1.0s).** pyatv fixes it at `22050 + sample_rate`
  frames with no setting, so `raop_latency.py` patches that one assignment;
  the value reaches the device purely as the offset between audio-packet
  timestamps and the announced playhead, so changing it is self-consistent.
  Clamped to the 0.25-2.0s range pyatv itself advertises. Tune via
  `raop_latency` in config.json, `--latency` on the CLI, or
  `HOMEPOD_BRIDGE_RAOP_LATENCY=1.5` to restore stock behaviour.
  **Raise it if audio breaks up** - lower means less tolerance for network
  jitter.
- **The live buffer bound is now 0.4s (was 4s), worth another ~0.7s.**
  Capture starts feeding the moment a device connects, but pyatv only begins
  consuming after its RTSP negotiation, and everything that piles up in that
  window becomes permanent session delay. Live measurement against real
  HomePods: at a 4s bound the buffer carried ~886ms of standing backlog; at
  0.4s it discards that once at connect (683ms, all within the first five
  seconds) and then runs at **median 0ms occupancy with zero further drops**.
  The capture thread injects silence when the PC is quiet, so a tight bound
  cannot underrun, and network jitter is absorbed by the receiver's buffer.
- `--bitrate` / `--quality` are now inert (kept for compatibility); nothing
  is encoded any more, which also removes the LAME CPU cost and the
  surround-channel encoder failure mode from the audio path.

## 0.8.0 - notifications say "HomePod Bridge"
- Toasts were labeled "Python" because Windows resolves a tray
  notification's header from the *host executable* - `pythonw.exe`, whose
  file description is literally "Python". The tray now sets an explicit
  AppUserModelID and registers a Start Menu shortcut carrying the same ID,
  which is what Windows resolves the display name and icon from. Verified
  end to end on Windows 11: header reads "HomePod Bridge" with the app's
  icon. Both halves are required - the ID alone shows the raw ID string
  with no icon, and a Startup-folder shortcut does not count (the app
  resolver ignores that subfolder).
- The Start Menu entry is created automatically on first run (and by
  `create_shortcuts.pyw`); it also makes the app launchable from Start Menu
  search. To remove it, delete "HomePod Bridge" from the Start Menu.
- Notification text no longer repeats the app name now that Windows shows
  it: the bold line carries the situation ("Streaming", "Streaming
  stopped", "Action failed") and the body carries the detail.

## 0.7.2 - latency bound
- The live MP3 buffer is now bounded by TIME (4 seconds of audio at the
  session bitrate) instead of 4MB of memory - which at 320kbps allowed
  ~105 seconds of backlog. The RAOP consumer only drains at realtime speed,
  so every network stall permanently added its duration to the audio delay
  until reconnect; long-running sessions could creep far behind live. Now a
  stall longer than 4s drops the oldest audio (brief glitch) and playback
  stays near-live. The ~2s AirPlay protocol latency is inherent and remains.

## 0.7.1 - fixes found in real-world logs
- Silence keep-alive: WASAPI loopback delivers NO frames while the machine
  is quiet, so pausing playback starved the MP3 stream and HomePods dropped
  the RAOP session (the repeated "not connected to remote" / "Stream ended"
  reconnect cycles in the logs). The capture loop now feeds real silence at
  the capture rate when nothing is playing - measured 46.9 chunks/s on a
  silent machine, previously zero.
- The capture thread no longer blocks inside `read()`, so `stop()` completes
  in milliseconds instead of hitting its 2s join timeout and taking the
  deliberate-leak path on nearly every disconnect ("capture thread did not
  exit; leaking its stream" appeared on almost every session end).
- Capture recovery retries the reopen (5 attempts, 2s apart) instead of
  dying on the first failure. A display/HDMI audio endpoint that has just
  disappeared typically rejects `pa.open` with "Insufficient memory" or
  "Device unavailable" for a few seconds while Windows resets it; that
  exception used to escape as an unretrieved task error, leaving the app
  idle and silent with no alert. If every attempt fails, streaming stops
  with a clear notification.

## 0.7.0 - per-room volume
- The volume flyout grows one labeled slider per device whenever two or
  more devices are selected, so different rooms can run at different
  levels; a single device (or stereo pair) keeps the classic one-slider UI.
- The master slider on top means "set every room": dragging it snaps all
  device rows (and clears their overrides). Mouse wheel adjusts the row
  under the pointer.
- Per-device levels persist in config (`device_volumes`) and are re-applied
  on connect and after watchdog reconnects; the master remains the default
  for devices without their own level.
- Engine: new `set_device_volume(_nowait)` API; volume applies now go
  through a per-device worker that drains to the latest requested level, so
  overlapping sends can no longer land out of order on a slow HomePod
  (closes a known limitation from 0.6.0). The worker also verifies the
  connection it applied to is still current, so a reconnect during a hung
  apply re-pushes the level to the fresh session; and a blocking volume
  call racing a Disconnect returns cleanly instead of raising.
- Flyout ordering rules: touching a device row delivers any pending master
  value first, and close() flushes rows in last-touched order - so an older
  master drag can never wipe a newer per-room tweak. The master mirrors the
  device rows only when it actually changed (a click on its thumb no longer
  desyncs the rows), and the window height is measured from real content so
  rows never get clipped at any device count or DPI.
- With exactly one device selected, the menu label and the slider show that
  device's effective level rather than a master that may be hiding an
  override (no more audible jump on the first drag).
- The master row is labeled "All devices" when room rows are shown, and the
  close button is gone (Esc or click-away closes the flyout) - every row
  now shares one layout, so equal values render their thumbs at the same
  position on every row.
- Config rejects NaN volume values (previously clamped to 100 - full blast
  from an undefined value).

## 0.6.0 - left-click restored + reliability pass

Left-click Connect/Disconnect:
- A single left-click (and double-click) of the tray icon toggles
  Connect/Disconnect again. The 0.5.1 crash fix removed the default menu
  item wholesale; the underlying race is gone now that the engine thread
  never rebuilds the menu and every HMENU rebuild (including pystray's own
  in `Icon.__call__` after a left-click) is serialized by the update lock.
  The default flag is gated to enabled (verified in pystray 0.19.5 source:
  a disabled default item still fires).

No more "silence with a green icon" - the capture thread can no longer die
silently:
- Sink-chain exceptions in the capture loop now fire `on_failure` (they used
  to kill the thread outside the recovery wiring); writes racing a pipe
  retirement are additionally guarded in `Mp3Pipe`, and a lock serializes
  `feed_pcm`/`finish` (lameenc releases the GIL during encode).
- Switching the Windows default output device is now detected (2s poll of
  the default render endpoint via Core Audio COM - PortAudio cannot see the
  change: its device table is frozen process-wide while any instance is
  alive) and restarts capture against the new device instead of streaming
  silence forever. In the CLI, where there is no restart machinery, capture
  death now exits with a clear error instead of hanging silently.
- `_stop_session` is exception-safe: a failing `capture.stop()` (typical
  when the device just died) can no longer strand the engine in STREAMING
  with recovery abandoned. Capture teardown itself is also hardened
  (terminate in finally; never close a stream under a stuck reader thread;
  no PyAudio leaks from failed construction or failed start).
- The capture-restart budget resets on user-initiated connects, so a manual
  reconnect after "capture keeps failing" is not killed by stale timestamps.
- Session mutations are serialized through a lock, so a capture-recovery
  restart racing a user Disconnect can no longer resurrect the session
  after the user stopped it.

Tray/menu correctness:
- The menu is rebuilt on the message thread right before display, so
  engine-driven transitions (autoconnect success, capture give-up) can no
  longer leave a stale Connect/Disconnect label that performs the inverted
  action. `_setup` no longer rebuilds the menu from pystray's setup thread
  (a residual v0.5.0-class crash window), and queued clicks dispatched
  inside the menu's modal message loop are ignored rather than destroying
  the menu currently on screen.
- Quit now closes an open volume flyout and joins its thread (fixes the
  Tcl_AsyncDelete abort-at-exit) and volume sends are fire-and-forget, so a
  busy engine can't freeze the flyout or close it mid-drag.
- Volume throttle: dragging back to the last-sent value cancels the stale
  pending update (50->60->50 no longer lands at 60).

Watchdog/AirPlay:
- Exponential backoff only resets after real streaming time, not after
  slow-failing scans/connects; late connects during Disconnect are ignored
  (no more ~10s hang and STREAMING flicker mid-stop); pyatv teardown tasks
  from `atv.close()` are awaited.
- Surround (5.1/7.1) output devices are rejected with a clear tray alert /
  CLI error instead of flapping reconnects forever (lameenc is stereo-only).

Diagnosability and config:
- The `homepod-bridge-tray` gui-script now configures logging (it produced
  no log file at all); tray and CLI use separate log files and rotation
  survives another process holding the log open; `python -m homepod_bridge`
  writes a crash.log breadcrumb for import-time failures under pythonw.
- Wrong-typed (but valid-JSON) config values fall back to defaults instead
  of crashing every launch; saves use unique temp files and a lock, so
  concurrent saves can't corrupt config.json.
- `create_shortcuts.pyw` validates that the interpreter baked into the
  shortcuts can actually import the app, failing with a clear dialog
  instead of creating silently-broken Startup shortcuts.
- Single-instance mutex moved to the `Global\` namespace (holds across
  fast-user-switching); a failed acquire no longer retains a handle that
  wedges acquire-after-release. Windows CI now exercises the mutex path.
- Removed the placeholder CHANGE-ME homepage URL from packaging metadata.

## 0.5.1 - critical fix: single left-click crashed the app
- Root cause (verified in pystray source): a left-click on the tray icon
  fires the menu's *default* item; ours toggled Connect/Disconnect, whose
  state change made the engine thread rebuild the menu concurrently with the
  icon thread's own rebuild (`Icon.__call__` -> `update_menu`). pystray's
  `_update_menu` calls `DestroyMenu` on a shared handle, so two threads doing
  it at once is a native crash - silent process exit, nothing in the log.
- Fix: no default menu item (single left-click is now inert; right-click
  opens the menu), one state-aware Toggle action, menu content generated
  lazily on the icon's own thread, HMENU rebuilt only from icon-thread
  contexts and serialized by a lock. The engine thread now only updates the
  icon color, tooltip, and notifications.
- New regression tests: no item may ever be default; left-click activation
  dispatches nothing; the lazy menu factory tracks live state.

## 0.5.0 - production-readiness pass
- Capture-death recovery: if WASAPI loopback dies (sleep/resume, device
  removal), the session auto-restarts with a fresh capture, re-detecting the
  default output device. Rapid repeated failures stop the session and raise a
  tray notification instead of restart-looping.
- Rotating file logs at `%APPDATA%/homepod-bridge/logs/bridge.log` (works
  under windowless `pythonw`), plus an "Open logs" tray menu item.
- Single-instance guard (named mutex on Windows) with an
  "already running" dialog.
- Packaging: `pyproject.toml` with `homepod-bridge` (console) and
  `homepod-bridge-tray` (windowless) entry points; MIT LICENSE;
  third-party notices; GitHub Actions CI (Windows + Linux/Xvfb).

## 0.4.x - tray application
- 0.4.4: double-clickable `create_shortcuts.pyw` creates Startup + Desktop
  shortcuts (auto-start at login, no terminal).
- 0.4.3: volume flyout layout fix (percentage/close visible); rendered-layout
  regression tests under a virtual display.
- 0.4.2: Win11-style volume flyout anchored above the taskbar; canvas-drawn
  slider; mouse-wheel support; DWM rounded corners.
- 0.4.1: auto-connect retries at launch (~2 min) until Wi-Fi/mDNS are ready.
- 0.4.0: volume slider popup; per-150ms throttled sends.
- 0.3.x: system-tray app (pystray) over a thread-safe BridgeEngine;
  connection events; AirPlay volume control; config persistence.

## 0.2.0 - multi-room
- Stream to multiple HomePods at once (`--device` repeatable); one capture
  fan-out, independent per-device encoder/session/watchdog.

## 0.1.0 - core bridge
- WASAPI loopback -> MP3 -> pyatv RAOP -> HomePod, with scan validation
  (RAOP pairing gate) and a reconnecting watchdog. Verified against
  pyatv 0.18.0.
