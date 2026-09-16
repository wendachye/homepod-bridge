"""Build the separate GPL AirPlay 2 helper from verified, pinned source.

Tested with Rust 1.98.1, Cargo and the MSVC C++ build tools on Windows.
Use --source-package for releases to include dependencies in the source ZIP.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request
import zipfile

from native_patches import apply_patches

REVISION = "a2f980cf25bf13ae20158497cc2d2ea69cd88fa7"
SHA256 = "c39cd0e0adb0f4a8160e72335da8949450852a060046063af3e07419378e6ce5"
ROOT = Path(__file__).resolve().parent.parent
QOS = '''#[cfg(windows)]
fn set_socket_qos(socket: &UdpSocket) {
    let socket = socket2::SockRef::from(socket);
    if let Err(err) = socket.set_send_buffer_size(1024 * 1024) {
        tracing::debug!("Failed to enlarge send buffer: {}", err);
    }
}

#[cfg(unix)]
'''
LIVE_PTP = '''        // Enable PTP sync mode for live audio, matching the file path.
        if self.stream_config.timing_protocol == TimingProtocol::Ptp {
            if let Some(clock_id) = self.ptp_master_clock_id {
                streamer.set_ptp_sync_mode(clock_id).await;
            }
        }

'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-package", action="store_true")
    args = parser.parse_args()
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    archive = dist / "airplay2-source.zip"
    if not archive.exists():
        url = f"https://codeload.github.com/lmcgartland/airplay2-rs/zip/{REVISION}"
        urllib.request.urlretrieve(url, archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise RuntimeError("AirPlay source archive SHA256 mismatch")
    source = dist / f"airplay2-rs-{REVISION}"
    if not source.exists():
        with zipfile.ZipFile(archive) as z:
            for entry in z.infolist():
                if not (dist / entry.filename).resolve().is_relative_to(source.resolve()):
                    raise RuntimeError("unexpected path in source archive")
            z.extractall(dist)
    apply_patches(archive, source, ROOT)
    rtp = source / "crates/airplay-audio/src/rtp.rs"
    text = rtp.read_text(encoding="utf-8")
    if QOS not in text:
        marker = "fn set_socket_qos(socket: &UdpSocket) {"
        if text.count(marker) != 1:
            raise RuntimeError("Windows socket patch no longer matches upstream")
        rtp.write_text(text.replace(marker, QOS + marker), encoding="utf-8")
    shutil.copyfile(ROOT / "native/homepod_sender.rs", source / "crates/airplay-client/examples/homepod_sender.rs")
    client_src = source / "crates/airplay-client/src"
    connection = client_src / "connection.rs"
    text = connection.read_text(encoding="utf-8")
    head, separator, live = text.partition("    pub async fn start_streaming_live(")
    if not separator:
        raise RuntimeError("Live PTP patch no longer matches upstream")
    if LIVE_PTP not in live:
        marker = "        // Set up equalizer if configured\n"
        if marker not in live:
            raise RuntimeError("Live PTP insertion point no longer matches upstream")
        live = live.replace(marker, LIVE_PTP + marker, 1)
    text = head + separator + live
    include = '\ninclude!("bridge_ptp_sync_tests.rs");\n'
    if include not in text:
        text += include
    connection.write_text(text, encoding="utf-8")
    shutil.copyfile(ROOT / "native/ptp_sync_tests.rs", client_src / "bridge_ptp_sync_tests.rs")
    cargo = shutil.which("cargo")
    if not cargo:
        raise RuntimeError("cargo is not on PATH; install Rust and MSVC C++ build tools")
    env = os.environ.copy()
    target = Path(env.get("CARGO_TARGET_DIR", dist / "native-target")).resolve()
    env["CARGO_TARGET_DIR"] = str(target)
    subprocess.run([cargo, "build", "--locked", "--release", "-p", "airplay-client",
                    "--example", "homepod_sender"], cwd=source, env=env, check=True)
    subprocess.run([cargo, "test", "--locked", "--release", "-p", "airplay-client",
                    "--example", "homepod_sender"], cwd=source, env=env, check=True)
    subprocess.run([cargo, "test", "--locked", "--release", "--workspace",
                    "--exclude", "airplay-tui", "--exclude", "airplay-bluetooth",
                    "--lib", "--no-fail-fast"], cwd=source, env=env, check=True, timeout=300)
    output = dist / "native"
    output.mkdir(exist_ok=True)
    shutil.copyfile(target / "release/examples/homepod_sender.exe", output / "HomePodSender.exe")
    shutil.copyfile(ROOT / "native/LICENSE", output / "COPYING-HomePodSender.txt")
    shutil.copyfile(ROOT / "THIRD_PARTY_NOTICES.md", output / "THIRD_PARTY_NOTICES.md")
    subprocess.run([str(output / "HomePodSender.exe"), "--self-test"], check=True)
    if args.source_package:
        vendor = subprocess.run([cargo, "vendor", "--locked", "--versioned-dirs", "vendor"],
                                cwd=source, env=env, check=True, capture_output=True, text=True)
        config = source / ".cargo/config.toml"
        config.parent.mkdir(exist_ok=True)
        config.write_text(vendor.stdout, encoding="utf-8")
        (source / "BRIDGE-BUILD.txt").write_text(
            f"HomePodSender source based on airplay2-rs {REVISION}.\n"
            "Modifications: Windows UDP send-buffer support; live PCM CLI example;\n"
            "live PTP synchronization, control-channel framing, nonblocking discovery;\n"
            "UDP, cipher interoperability and bounded async discovery regression tests.\n"
            "Install Rust and MSVC C++ build tools, then run from this directory:\n"
            "cargo build --offline --locked --release -p airplay-client --example homepod_sender\n"
            "Binary: target/release/examples/homepod_sender.exe\n"
            "The helper and airplay2-rs are GPL-3.0-or-later; see LICENSE.\n",
            encoding="utf-8",
        )
        source_zip = output / "HomePodSender-source.zip"
        with zipfile.ZipFile(source_zip, "w", compression=zipfile.ZIP_DEFLATED,
                             strict_timestamps=False) as z:
            for path in source.rglob("*"):
                if path.is_file():
                    z.write(path, str(Path("HomePodSender-source") / path.relative_to(source)))
        print(f"Corresponding source: {source_zip}")
    print(f"Native sender: {output / 'HomePodSender.exe'}")


if __name__ == "__main__":
    main()
