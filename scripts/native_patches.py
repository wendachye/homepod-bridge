"""Small, reviewable corrections applied to the verified upstream source.

Always transform the original archive contents, so repeated builds are
idempotent and cannot accumulate edits in the generated source directory.
"""
from pathlib import Path
import zipfile


def replace(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"Native patch no longer matches: {old[:80]!r}")
    return text.replace(old, new, 1)


def section(text: str, start: str, end: str, replacement: str) -> str:
    if text.count(start) != 1:
        raise RuntimeError(f"Native patch section no longer matches: {start!r}")
    head, _, tail = text.partition(start)
    if end not in tail:
        raise RuntimeError(f"Native patch section end no longer matches: {end!r}")
    _, _, footer = tail.partition(end)
    return head + replacement + end + footer


def patch_channel(text: str) -> str:
    text = replace(text, "| Length (2 BE)  |", "| Length (2 LE)  |")
    text = replace(text, "Length: 2 bytes, big-endian, size of ciphertext + tag",
                   "Length: 2 bytes, little-endian, plaintext size (tag excluded)")
    text = replace(text, "/// Nonces are auto-incrementing counters, separate for read and write directions.",
                   "/// Messages use one or more blocks of at most 1024 plaintext bytes.\n"
                   "/// Nonces increment per block, separately for read and write directions.")
    text = section(text, "    pub fn encrypt(&mut self, plaintext:",
                   "    /// Encrypt data without framing", '''    pub fn encrypt(&mut self, plaintext: &[u8]) -> Result<Vec<u8>> {
        // ControlCipher already supplies authenticated HomeKit framing.
        self.write_cipher.encrypt(plaintext).map_err(Error::Crypto)
    }

''')
    text = section(text, "    pub fn encrypt_raw(&mut self, plaintext:",
                   "    /// Decrypt framed data", '''    pub fn encrypt_raw(&mut self, plaintext: &[u8]) -> Result<Vec<u8>> {
        // Without length prefixes, multiple blocks cannot be delimited.
        if plaintext.is_empty() || plaintext.len() > 1024 {
            return Err(Error::Crypto(CryptoError::Encryption(
                "Raw control data must contain 1..=1024 plaintext bytes".to_string(),
            )));
        }
        let framed = self.write_cipher.encrypt(plaintext).map_err(Error::Crypto)?;
        Ok(framed[2..].to_vec())
    }

''')
    text = section(text, "    pub fn decrypt(&mut self, framed:",
                   "    /// Decrypt raw ciphertext", '''    pub fn decrypt(&mut self, framed: &[u8]) -> Result<Vec<u8>> {
        self.read_cipher.decrypt(framed).map_err(Error::Crypto)
    }

''')
    text = replace(text, "        Some(2 + len)", "        Some(2 + len + 16)")
    text = replace(text, "        // Length prefix should encode 20 (4 + 16)",
                   "        // HomeKit length prefix excludes the 16-byte authentication tag.")
    text = replace(text, "        assert_eq!(framed[0], 0x00);\n        assert_eq!(framed[1], 20);",
                   "        assert_eq!(framed[0], 4);\n        assert_eq!(framed[1], 0);")
    text = replace(text, "[0x00, 0x14, 0x01, 0x02, 0x03]; // length = 20",
                   "[0x04, 0x00, 0x01, 0x02, 0x03]; // plaintext length = 4")
    text = replace(text, "// Frame claims 20 bytes but only has 10",
                   "// Frame claims 20 plaintext bytes plus a tag but only has 10 bytes")
    text = replace(text, "let truncated = [0x00, 0x14,", "let truncated = [0x14, 0x00,")
    return text


def patch_browser(text: str) -> str:
    text = section(text,
                   "                // Try to receive from either channel with a short timeout",
                   "            }\n        };", '''                // Never block Stream::poll_next: timers and cancellation must
                // keep running even when neither discovery channel has an event.
                tokio::select! {
                    event = airplay_receiver.recv_async() => {
                        let Ok(event) = event else { break; };
                        if let Some(event) = Self::handle_service_event(event, false, &devices).await {
                            yield event;
                        }
                    }
                    event = raop_receiver.recv_async() => {
                        let Ok(event) = event else { break; };
                        if let Some(event) = Self::handle_service_event(event, true, &devices).await {
                            yield event;
                        }
                    }
                    // stop() changes the shared flag; periodically wake a quiet stream.
                    _ = tokio::time::sleep(Duration::from_millis(100)) => {}
                }
''')
    text = section(text, "        let start = std::time::Instant::now();",
                   "        // Stop browsing", '''        let deadline = tokio::time::Instant::now() + timeout;

        while self.running.load(Ordering::SeqCst) {
            tokio::select! {
                _ = tokio::time::sleep_until(deadline) => break,
                event = airplay_receiver.recv_async() => {
                    let Ok(event) = event else { break; };
                    Self::handle_service_event(event, false, &devices).await;
                }
                event = raop_receiver.recv_async() => {
                    let Ok(event) = event else { break; };
                    Self::handle_service_event(event, true, &devices).await;
                }
                _ = tokio::time::sleep(Duration::from_millis(100)) => {}
            }
        }

''')
    return text


def patch_ptp(text: str) -> str:
    return section(
        text,
        "    // Phase 4: Slave loop (receive Sync/Follow_Up, send Delay_Req, calculate offset)",
        "/// Run as PTP master to multiple HomePods for group streaming.",
        '''    run_bmca_slave(
        event_socket, general_socket, master_ip, event_dest,
        clock_identity, offset_tx,
    ).await
}

''',
    )


def apply_patches(archive: Path, source: Path, root: Path) -> None:
    patches = [
        ("airplay-pairing", "channel.rs", patch_channel, "channel_tests.rs"),
        ("airplay-discovery", "browser.rs", patch_browser, "discovery_tests.rs"),
        ("airplay-timing", "ptp.rs", patch_ptp, "ptp_clock.rs"),
    ]
    with zipfile.ZipFile(archive) as upstream:
        for crate, module, transform, test_file in patches:
            directory = Path("crates") / crate / "src"
            member = (Path(source.name) / directory / module).as_posix()
            original = upstream.read(member).decode("utf-8")
            fixture = "bridge_" + test_file
            patched = transform(original) + f'\ninclude!("{fixture}");\n'
            (source / directory / module).write_text(patched, encoding="utf-8")
            (source / directory / fixture).write_bytes((root / "native" / test_file).read_bytes())
