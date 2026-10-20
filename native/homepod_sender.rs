// SPDX-License-Identifier: GPL-3.0-or-later
// HomePod Bridge live-PCM command-line sender. Built inside airplay2-rs.
use airplay_audio::{AlacEncoder, LiveAudioDecoder, LivePcmFrame};
use airplay_client::{AirPlayClient, Connection};
use airplay_core::device::{Device, DeviceId};
use airplay_core::features::Features;
use airplay_core::stream::{PtpMode, StreamType, TimingProtocol};
use airplay_core::{AudioFormat, StreamConfig};
use std::io::{Read, Write};
use std::net::IpAddr;
use std::time::Duration;

type Error = Box<dyn std::error::Error>;
enum Command { Volume(f32), MemberVolume(usize, f32), Stop, Error(String) }

enum Sender { Single(Connection), Group(AirPlayClient) }
impl Sender {
    async fn volume(&mut self, volume: f32) -> Result<(), Error> {
        match self {
            Self::Single(conn) => conn.set_volume(volume).await?,
            Self::Group(client) => {
                for index in 0..client.group_device_count() {
                    client.set_group_member_volume(index, volume).await?;
                }
            }
        }
        Ok(())
    }
    async fn member_volume(&mut self, index: usize, volume: f32) -> Result<(), Error> {
        match self {
            Self::Group(client) => client.set_group_member_volume(index, volume).await?,
            Self::Single(conn) if index == 0 => conn.set_volume(volume).await?,
            _ => return Err("invalid group member".into()),
        }
        Ok(())
    }
    async fn start(&mut self, decoder: LiveAudioDecoder) -> Result<(), Error> {
        match self {
            Self::Single(conn) => conn.start_streaming_live(decoder).await?,
            Self::Group(client) => client.start_live_streaming_to_group(decoder).await?,
        }
        Ok(())
    }
    async fn feedback(&mut self) -> Result<(), Error> {
        match self {
            Self::Single(conn) => conn.send_feedback().await?,
            Self::Group(client) => client.send_group_feedback().await?,
        }
        Ok(())
    }
    fn stats(&self) -> (u64, u64) {
        match self {
            Self::Single(conn) => (conn.streamer_packets_sent(), conn.streamer_underruns()),
            Self::Group(client) => {
                let stats = client.stats_snapshot();
                (stats.packets_sent, stats.underruns)
            }
        }
    }
    async fn close(&mut self) -> Result<(), Error> {
        match self {
            Self::Single(conn) => { conn.stop().await?; conn.disconnect().await?; }
            Self::Group(client) => { client.stop().await?; client.disconnect().await?; }
        }
        Ok(())
    }
}

#[derive(serde::Deserialize)]
struct Target {
    host: IpAddr,
    port: u16,
    features: String,
}

fn parse_args(args: &[String]) -> Result<(Vec<Device>, u32, u8), Error> {
    let (targets, rate, channels) = if args.get(1).map(String::as_str) == Some("--group") {
        if args.len() != 5 { return Err("usage: HomePodSender --group targets-json sample-rate channels".into()); }
        let targets: Vec<Target> = serde_json::from_str(&args[2])?;
        if !(2..=16).contains(&targets.len()) { return Err("group requires 2 to 16 endpoints".into()); }
        (targets, &args[3], &args[4])
    } else {
        if args.len() != 6 { return Err("usage: HomePodSender host port features sample-rate channels".into()); }
        (vec![Target { host: args[1].parse()?, port: args[2].parse()?, features: args[3].clone() }],
         &args[4], &args[5])
    };
    let sample_rate: u32 = rate.parse()?;
    let channels: u8 = channels.parse()?;
    if !(8000..=192000).contains(&sample_rate) || !(1..=2).contains(&channels) {
        return Err("unsupported PCM format".into());
    }
    let mut seen = std::collections::HashSet::new();
    let mut devices = Vec::new();
    for target in targets {
        if target.port == 0 || !seen.insert((target.host, target.port)) {
            return Err("invalid or duplicate endpoint".into());
        }
        devices.push(device(target.host, target.port, Features::from_txt_value(&target.features)?));
    }
    Ok((devices, sample_rate, channels))
}

// rubato's fixed-input resampler requires exactly 1024 frames. WASAPI and
// pipe reads can split anywhere, so retain the remainder between commands.
#[derive(Default)]
struct PcmBlocks { pending: Vec<i16> }
impl PcmBlocks {
    fn push(&mut self, body: &[u8], channels: u8) -> Vec<Vec<i16>> {
        for bytes in body.chunks_exact(2) {
            let sample = i16::from_le_bytes([bytes[0], bytes[1]]);
            self.pending.push(sample);
            if channels == 1 { self.pending.push(sample); }
        }
        let mut blocks = Vec::new();
        while self.pending.len() >= 2048 {
            blocks.push(self.pending.drain(..2048).collect());
        }
        blocks
    }
}

fn event(value: serde_json::Value) {
    println!("{}", value);
    let _ = std::io::stdout().flush();
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.get(1).map(String::as_str) == Some("--self-test") {
        event(serde_json::json!({"ok": true, "protocol": 1, "sender": "HomePodSender",
                               "capabilities": ["shared-clock-group", "member-volume"]}));
        return;
    }
    // Protocol traces can contain session keys. Never enable them here.
    tracing_subscriber::fmt().with_max_level(tracing::Level::WARN)
        .with_ansi(false).with_writer(std::io::stderr).init();
    let result = tokio::runtime::Builder::new_multi_thread().worker_threads(4)
        .enable_all().build().unwrap().block_on(run(&args));
    if let Err(error) = result {
        event(serde_json::json!({"event": "error", "message": error.to_string()}));
        std::process::exit(1);
    }
}

fn device(ip: IpAddr, port: u16, features: Features) -> Device {
    let id = match ip {
        IpAddr::V4(v4) => { let o = v4.octets(); DeviceId([o[0],o[1],o[2],o[3],0,0]) },
        IpAddr::V6(v6) => { let o = v6.octets(); DeviceId([o[0],o[1],o[2],o[3],o[4],o[5]]) },
    };
    Device {
        id, name: "HomePod Bridge".into(), model: "HomePod".into(),
        manufacturer: None, serial_number: None, addresses: vec![ip], port,
        features, required_sender_features: None, public_key: None,
        source_version: Default::default(), firmware_version: None, os_version: None,
        protocol_version: None, requires_password: false, status_flags: 0,
        access_control: None, pairing_identity: None, system_pairing_identity: None,
        bluetooth_address: None, homekit_home_id: None, group_id: None,
        is_group_leader: false, group_public_name: None,
        group_contains_discoverable_leader: false, home_group_id: None,
        household_id: None, parent_group_id: None,
        parent_group_contains_discoverable_leader: false, tight_sync_id: None,
        raop_port: None, raop_encryption_types: None, raop_codecs: None,
        raop_transport: None, raop_metadata_types: None, raop_digest_auth: false,
        vodka_version: None,
    }
}

async fn run(args: &[String]) -> Result<(), Error> {
    let (mut devices, sample_rate, channels) = parse_args(args)?;
    let target_count = devices.len();
    let audio_format = AudioFormat::default();
    let asc = Some(AlacEncoder::new(audio_format.clone())?.magic_cookie());
    let config = StreamConfig {
        stream_type: StreamType::Realtime, audio_format,
        timing_protocol: TimingProtocol::Ptp, ptp_mode: PtpMode::Master,
        latency_min: 22050, latency_max: 88200, supports_dynamic_stream_id: true, asc,
    };
    let mut conn = if devices.len() == 1 {
        let mut connection = Connection::connect_auto(devices.remove(0), config, "3939").await?;
        connection.setup().await?;
        Sender::Single(connection)
    } else {
        let mut client = AirPlayClient::with_config(config, None)?;
        client.connect_group(&devices).await?;
        Sender::Group(client)
    };
    // Stay muted until the bridge applies the user's saved volume.
    conn.volume(0.0).await?;
    let (audio, decoder) = LiveAudioDecoder::create_pair(sample_rate, 2, 8);
    let (commands, mut received) = tokio::sync::mpsc::channel(16);
    std::thread::Builder::new().name("pcm-input".into()).spawn(move || {
        let result = (|| -> std::io::Result<()> {
            let mut input = std::io::stdin().lock();
            let mut blocks = PcmBlocks::default();
            loop {
                let mut header = [0u8; 5];
                if let Err(e) = input.read_exact(&mut header) {
                    if e.kind() == std::io::ErrorKind::UnexpectedEof { break; }
                    return Err(e);
                }
                let size = u32::from_le_bytes(header[1..5].try_into().unwrap()) as usize;
                if size > 16384 { return Err(std::io::Error::other("PCM packet too large")); }
                let mut body = vec![0u8; size];
                input.read_exact(&mut body)?;
                match header[0] {
                    1 if size > 0 && size % (channels as usize * 2) == 0 => {
                        for samples in blocks.push(&body, channels) {
                            if !audio.send(LivePcmFrame { samples, channels: 2, sample_rate }) {
                                return Ok(());
                            }
                        }
                    },
                    2 if size == 4 => {
                        let volume = f32::from_le_bytes(body.try_into().unwrap());
                        if !volume.is_finite() { return Err(std::io::Error::other("invalid volume")); }
                        if commands.blocking_send(Command::Volume(volume.clamp(0.0, 1.0))).is_err() { break; }
                    },
                    4 if size == 6 => {
                        let index = u16::from_le_bytes(body[..2].try_into().unwrap()) as usize;
                        let volume = f32::from_le_bytes(body[2..].try_into().unwrap());
                        if index >= target_count || !volume.is_finite() {
                            return Err(std::io::Error::other("invalid group volume"));
                        }
                        if commands.blocking_send(Command::MemberVolume(index, volume.clamp(0.0, 1.0))).is_err() {
                            break;
                        }
                    },
                    3 if size == 0 => break,
                    _ => return Err(std::io::Error::other("invalid command")),
                }
            }
            Ok(())
        })();
        let command = match result { Ok(()) => Command::Stop, Err(e) => Command::Error(e.to_string()) };
        let _ = commands.blocking_send(command);
    })?;
    // Signal before start_streaming_live: its prebuffer needs incoming PCM.
    event(serde_json::json!({"event": "ready", "protocol": 1, "targets": target_count}));
    conn.start(decoder).await?;
    let mut feedback = tokio::time::interval(Duration::from_secs(2));
    feedback.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut ticks = 0;
    let mut last_packets = 0;
    let mut stalled = 0;
    loop {
        tokio::select! {
            command = received.recv() => match command {
                Some(Command::Volume(value)) => {
                    conn.volume(value).await?;
                    event(serde_json::json!({"event":"volume", "value":value}));
                },
                Some(Command::MemberVolume(index, value)) => {
                    conn.member_volume(index, value).await?;
                    event(serde_json::json!({"event":"volume", "index":index, "value":value}));
                },
                Some(Command::Error(message)) => return Err(message.into()),
                Some(Command::Stop) | None => break,
            },
            _ = feedback.tick() => {
                conn.feedback().await?;
                let (packets, underruns) = conn.stats();
                stalled = if packets == last_packets { stalled + 1 } else { 0 };
                if stalled >= 5 { return Err("audio sender stalled".into()); }
                last_packets = packets;
                ticks += 1;
                if ticks % 5 == 0 {
                    event(serde_json::json!({"event":"stats", "packets":packets, "underruns":underruns}));
                }
            }
        }
    }
    conn.close().await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn group_arguments_preserve_order_and_pcm_format() {
        let args: Vec<String> = [
            "sender", "--group",
            r#"[{"host":"10.0.0.1","port":7000,"features":"0x1"},{"host":"10.0.0.2","port":7000,"features":"0x1"}]"#,
            "48000", "2",
        ].iter().map(|s| s.to_string()).collect();
        let (devices, rate, channels) = parse_args(&args).unwrap();
        assert_eq!((rate, channels), (48000, 2));
        assert_eq!(devices.len(), 2);
        assert_eq!(devices[0].addresses[0].to_string(), "10.0.0.1");
        assert_eq!(devices[1].addresses[0].to_string(), "10.0.0.2");
    }

    #[test]
    fn group_rejects_duplicate_or_incomplete_targets() {
        for json in [
            r#"[{"host":"10.0.0.1","port":7000,"features":"0x1"}]"#,
            r#"[{"host":"10.0.0.1","port":7000,"features":"0x1"},{"host":"10.0.0.1","port":7000,"features":"0x1"}]"#,
        ] {
            let args = ["sender", "--group", json, "48000", "2"]
                .iter().map(|s| s.to_string()).collect::<Vec<_>>();
            assert!(parse_args(&args).is_err());
        }
    }

    #[test]
    fn variable_capture_chunks_preserve_stereo_samples() {
        let original: Vec<i16> = (0..4096).map(|n| n as i16).collect();
        let bytes: Vec<u8> = original.iter().flat_map(|v| v.to_le_bytes()).collect();
        let mut blocks = PcmBlocks::default();
        let mut output = Vec::new();
        let mut offset = 0;
        for frames in [352, 128, 511, 1024, 33] {
            for block in blocks.push(&bytes[offset..offset + frames * 4], 2) {
                assert_eq!(block.len(), 2048);
                output.extend(block);
            }
            offset += frames * 4;
        }
        assert_eq!(output, original);
        assert!(blocks.pending.is_empty());
    }

    #[test]
    fn live_48khz_resampling_accepts_reblocked_capture() {
        let mut blocks = PcmBlocks::default();
        let (sender, mut decoder) = LiveAudioDecoder::create_pair(48000, 2, 16);
        // The actual failure was a 352-frame initial WASAPI chunk.
        for frames in [352, 128, 511, 1024, 33] {
            let pcm = vec![1000i16; frames * 2];
            let bytes: Vec<u8> = pcm.iter().flat_map(|v| v.to_le_bytes()).collect();
            for samples in blocks.push(&bytes, 2) {
                assert!(sender.try_send(LivePcmFrame { samples, channels: 2, sample_rate: 48000 }));
            }
        }
        let format = AudioFormat::default();
        let frame = decoder.decode_resampled(&format, 352).unwrap().unwrap();
        assert_eq!(frame.samples.len(), 704);
        assert_eq!(frame.sample_rate, 44100);
    }

    #[test]
    fn mono_capture_is_duplicated_to_both_channels() {
        let mut blocks = PcmBlocks::default();
        let input: Vec<u8> = (0..1024i16).flat_map(|v| v.to_le_bytes()).collect();
        let output = blocks.push(&input, 1);
        assert_eq!(output.len(), 1);
        for (n, frame) in output[0].chunks_exact(2).enumerate() {
            assert_eq!(frame, &[n as i16, n as i16]);
        }
    }
}
