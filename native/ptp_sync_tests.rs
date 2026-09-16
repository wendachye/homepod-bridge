// SPDX-License-Identifier: GPL-3.0-or-later
// Included inside airplay-client's connection module by our build script.
// Tests the real file/live Connection entry points and their UDP wire output.
#[cfg(test)]
mod bridge_ptp_sync_tests {
    use super::*;
    use airplay_audio::LivePcmFrame;
    use airplay_core::DeviceId;
    use std::time::Duration;

    fn device() -> Device {
        Device {
            id: DeviceId([1,2,3,4,5,6]), name: "Loopback receiver".into(),
            model: "HomePod".into(), manufacturer: None, serial_number: None,
            addresses: vec!["127.0.0.1".parse().unwrap()], port: 7000,
            features: Default::default(), required_sender_features: None,
            public_key: None, source_version: Default::default(),
            firmware_version: None, os_version: None, protocol_version: None,
            requires_password: false, status_flags: 0, access_control: None,
            pairing_identity: None, system_pairing_identity: None,
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

    async fn receive_sync(live: bool, protocol: TimingProtocol) -> Vec<u8> {
        let control = UdpSocket::bind("127.0.0.1:0").unwrap();
        control.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        let data = UdpSocket::bind("127.0.0.1:0").unwrap();
        let device = device();
        let config = StreamConfig { timing_protocol: protocol, ..Default::default() };
        let mut session = RtspSession::new(device.clone(), config.clone());
        session.set_connected().unwrap();
        session.set_paired().unwrap();
        session.process_setup_phase1_response(b"<plist version=\"1.0\"><dict/></plist>").unwrap();
        let phase2 = format!(
            "<plist version=\"1.0\"><dict><key>streams</key><array><dict>\
             <key>type</key><integer>{}</integer>\
             <key>dataPort</key><integer>{}</integer>\
             <key>controlPort</key><integer>{}</integer>\
             </dict></array></dict></plist>",
            config.stream_type as u32, data.local_addr().unwrap().port(),
            control.local_addr().unwrap().port(),
        );
        session.process_setup_phase2_response(phase2.as_bytes()).unwrap();
        let mut control_receiver = RtpReceiver::new();
        control_receiver.bind(0).unwrap();
        // Pairing and SETUP are complete. No RTSP server is needed: the
        // streaming entry points tolerate FLUSH/volume failures. Audio and
        // control packets still travel through the real RTP sender sockets.
        let mut conn = Connection {
            device, rtsp: RtspConnection::new("127.0.0.1:7000".parse().unwrap()),
            session, streamer: None, playback_state: PlaybackState::Stopped,
            volume: 0.2, stream_config: config, timing_offset: Some(Default::default()),
            timing_tx: None, control_task: None, control_stop: Arc::new(AtomicBool::new(false)),
            timing_task: None, timing_server: None, ptp_master: None,
            ptp_master_sync_task: None, control_receiver: Some(Arc::new(control_receiver)), events_stream: None,
            ptp_master_clock_id: Some([1,2,3,4,5,6,7,8]), render_delay_ms: 0,
            eq_config: None, eq_params: None, spatial_params: None, spatial_speakers: None,
            stream_stats: crate::stats::StreamStats::new(),
        };
        let mut file = None;
        let mut live_sender = None;
        if live {
            let (sender, decoder) = LiveAudioDecoder::create_pair(44100, 2, 128);
            for _ in 0..96 {
                assert!(sender.try_send(LivePcmFrame {
                    samples: vec![1000; 2048], channels: 2, sample_rate: 44100,
                }));
            }
            conn.start_streaming_live(decoder).await.unwrap();
            live_sender = Some(sender);
        } else {
            let path = std::env::temp_dir().join(format!("bridge-sync-{}.wav", uuid::Uuid::new_v4()));
            let mut wav = Vec::new();
            let length = 44100u32 * 4 * 2;
            wav.extend(b"RIFF"); wav.extend((36 + length).to_le_bytes());
            wav.extend(b"WAVEfmt "); wav.extend(16u32.to_le_bytes());
            wav.extend(1u16.to_le_bytes()); wav.extend(2u16.to_le_bytes());
            wav.extend(44100u32.to_le_bytes()); wav.extend(176400u32.to_le_bytes());
            wav.extend(4u16.to_le_bytes()); wav.extend(16u16.to_le_bytes());
            wav.extend(b"data"); wav.extend(length.to_le_bytes());
            wav.resize(44 + length as usize, 0);
            std::fs::write(&path, wav).unwrap();
            conn.start_streaming(AudioDecoder::open(&path).unwrap()).await.unwrap();
            file = Some(path);
        }
        let received = tokio::task::spawn_blocking(move || {
            let mut wire = [0u8; 128];
            control.recv_from(&mut wire).map(|(size, _)| wire[..size].to_vec())
        }).await.unwrap();
        conn.stop().await.unwrap();
        conn.control_stop.store(true, Ordering::Release);
        if let Some(task) = conn.control_task.take() { task.await.unwrap(); }
        conn.streamer = None;
        drop(live_sender);
        if let Some(path) = file { std::fs::remove_file(path).unwrap(); }
        received.expect("stream must emit a sync packet")
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn file_ptp_uses_type_87() {
        let packet = receive_sync(false, TimingProtocol::Ptp).await;
        assert_eq!(packet[1] & 0x7f, 87);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn live_ptp_uses_type_87() {
        let packet = receive_sync(true, TimingProtocol::Ptp).await;
        assert_eq!(packet[1] & 0x7f, 87, "PTP live audio must use the same sync protocol as file audio");
        assert!(packet.windows(8).any(|w| w == [1,2,3,4,5,6,7,8]));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn live_ntp_retains_type_84() {
        let packet = receive_sync(true, TimingProtocol::Ntp).await;
        assert_eq!(packet[1] & 0x7f, 84);
    }
}
