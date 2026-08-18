# Third-party notices

homepod-bridge (MIT) depends on the following packages. License names were
verified from package metadata at release time.

| Package | License | Role |
|---|---|---|
| [pyatv](https://github.com/postlund/pyatv) | MIT | AirPlay/RAOP sender protocol |
| [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) | MIT | WASAPI loopback capture (Windows) |
| [Pillow](https://python-pillow.org/) | MIT-CMU | Tray icon rendering |
| [pystray](https://github.com/moses-palmer/pystray) | LGPL-3.0 | System-tray UI |
| [lameenc](https://github.com/chrisstaite/lameenc) | LGPL-3.0-or-later (bundles [LAME](https://lame.sourceforge.io/), LGPL) | MP3 encoding |

Distributing this project as **source** (this repository) is straightforward
under all of the above. If you distribute a **bundled binary** (e.g. a
PyInstaller `HomePodBridge.exe`), note that pystray and lameenc/LAME are
LGPL: keep this notice with the binary, link back to their sources, and
preserve the user's ability to swap those components (a standard PyInstaller
onedir build, or providing the build instructions in this repo, satisfies
that in practice).

AirPlay, HomePod, and Apple are trademarks of Apple Inc. This project is an
independent, unofficial implementation built on the community's
reverse-engineered understanding of the AirPlay protocol and is not
affiliated with or endorsed by Apple.
