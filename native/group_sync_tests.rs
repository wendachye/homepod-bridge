// SPDX-License-Identifier: GPL-3.0-or-later
// Exercise real UDP packets from the common streamer used by group playback.
#[cfg(test)]
mod bridge_group_sync_tests {
    use super::*;
    use crate::LivePcmFrame;
    use std::net::UdpSocket;
    use std::time::Duration;

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn both_receivers_get_identical_audio_timestamps_and_ptp_mapping() {
        let mut sockets = Vec::new();
        let mut senders = Vec::new();
        for index in 0..2 {
            let audio = UdpSocket::bind("127.0.0.1:0").unwrap();
            let control = UdpSocket::bind("127.0.0.1:0").unwrap();
            audio.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
            control.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
            let mut sender = RtpSender::new(audio.local_addr().unwrap(), index + 1);
            sender.bind(0).unwrap();
            sender.set_control_dest(control.local_addr().unwrap());
            sender.set_control_socket(UdpSocket::bind("127.0.0.1:0").unwrap());
            sockets.push((audio, control));
            senders.push(sender);
        }
        let config = StreamConfig {
            timing_protocol: airplay_core::TimingProtocol::Ptp,
            ..Default::default()
        };
        let mut streamer = AudioStreamer::new(config);
        streamer.set_rtp_senders(senders).await;
        streamer.set_ptp_sync_mode([1, 2, 3, 4, 5, 6, 7, 8]).await;
        let (input, decoder) = LiveAudioDecoder::create_pair(44100, 2, 128);
        for block in 0..96 {
            assert!(input.try_send(LivePcmFrame {
                samples: vec![block as i16; 2048], channels: 2, sample_rate: 44100,
            }));
        }
        streamer.start_live(decoder).await.unwrap();
        let captured = tokio::task::spawn_blocking(move || {
            sockets.into_iter().map(|(audio, control)| {
                let mut buffer = [0u8; 8192];
                let mut packets = Vec::new();
                for _ in 0..30 {
                    let (size, _) = audio.recv_from(&mut buffer).unwrap();
                    packets.push(buffer[..size].to_vec());
                }
                let (size, _) = control.recv_from(&mut buffer).unwrap();
                (packets, buffer[..size].to_vec())
            }).collect::<Vec<_>>()
        }).await;
        streamer.stop().await.unwrap();
        drop(input);
        let captured = captured.unwrap();
        for (left, right) in captured[0].0.iter().zip(&captured[1].0) {
            assert_eq!(&left[4..8], &right[4..8], "same PCM must have the same RTP timestamp");
            assert_eq!(&left[12..], &right[12..], "both receivers must get the same encoded PCM");
        }
        assert_eq!(captured[0].1[1] & 0x7f, 87);
        assert_eq!(captured[0].1, captured[1].1, "both receivers need the same clock mapping");
    }
}
