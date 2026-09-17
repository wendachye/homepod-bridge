# HomePodSender

This is a separate command-line program under **GPL-3.0-or-later**, built
against [airplay2-rs](https://github.com/lmcgartland/airplay2-rs) revision
`a2f980cf25bf13ae20158497cc2d2ea69cd88fa7`. The Python bridge communicates
with it through stdin/stdout; it does not load the Rust library.

Install Rust stable and Visual Studio C++ Build Tools, then run:

```powershell
python scripts/build_native_sender.py --source-package
```

The script verifies the upstream archive hash, applies Windows socket,
live PTP synchronization, control-channel framing and async discovery fixes,
adds `homepod_sender.rs`, and builds with the upstream Cargo.lock. The
framing and discovery patches are maintained in `scripts/native_patches.py`
and reapplied from the original verified archive on every build. The build
runs the sender and all nine supporting libraries' tests. Regressions inspect
actual live/file UDP synchronization packets, check framing interoperability
against the active control cipher, and bound discovery in a subprocess so
an async-runtime hang fails CI instead of wedging the runner.
The release source ZIP includes the modified source, vendored Cargo
dependencies, their licenses, and offline build instructions. Distribute
`dist/native/HomePodSender-source.zip`, the license and third-party notices
alongside any EXE containing this helper.

One sender process supports one HomePod target. Single-target Apple Home
stereo-pair delivery is not working in the current hardware tests: only one
member plays, including with the experimental clock correction. Delivery to
the whole pair from one selected entry is deferred from the next release;
selecting an individual speaker remains available. See the
[validation record](../docs/windows-validation-2026-09-17.md). Multiple independently selected
devices still use the original pyatv transport. The native sender uses
PTP ports 319/320, ALAC at 44.1 kHz, and resamples incoming live PCM.
Its live prebuffer adds about one second; `raop_latency` applies only to
the pyatv transport. No sample-accurate multi-room guarantee is made.
The tray applies saved volume; standalone CLI native streams start at 50%.

The IPC protocol is documented in `homepod_bridge/native_sender.py`.
`HomePodSender.exe --self-test` checks offline startup and protocol version.
Runtime diagnostics are restricted to warnings to avoid logging protocol
secrets. Receiver setup is bounded by the Python supervisor, which also
terminates an unresponsive child during disconnect.
