// SPDX-License-Identifier: GPL-3.0-or-later
// Included inside airplay-timing::ptp by the bridge build.
// HomePod gPTP provides Sync + Follow_Up without answering the E2E Delay_Req
// used by the upstream loop. Estimate remote-minus-local from matched packets.
// This aligns clock epochs; it does not measure one-way network propagation.

#[derive(Default)]
struct HomePodClock {
    pending: std::collections::VecDeque<(PtpHeader, u64, std::time::Instant)>,
    samples: std::collections::VecDeque<(i64, std::time::Instant)>,
}

impl HomePodClock {
    fn observe(&mut self, packet: &[u8], event_port: bool, local_ns: u64,
               now: std::time::Instant) -> Option<ClockOffset> {
        let header = PtpHeader::parse(packet).ok()?;
        if header.version != 2 || header.message_length < 44
            || usize::from(header.message_length) > packet.len() {
            return None;
        }
        let origin = PtpTimestamp::parse(&packet[34..44]).ok()?;
        if origin.nanoseconds >= 1_000_000_000 {
            return None;
        }
        self.pending.retain(|(_, _, when)| now.duration_since(*when) < Duration::from_secs(1));
        let (received_ns, correction) = match header.message_type {
            PtpMessageType::Sync if event_port => {
                if header.flags & 0x0200 != 0 {
                    self.pending.retain(|(previous, _, _)| !Self::matches(previous, &header));
                    self.pending.push_back((header, local_ns, now));
                    while self.pending.len() > 8 { self.pending.pop_front(); }
                    return None;
                }
                (local_ns, i128::from(header.correction_field))
            }
            PtpMessageType::FollowUp if !event_port => {
                let index = self.pending.iter().position(|(sync, _, _)| Self::matches(sync, &header))?;
                let (sync, received_ns, _) = self.pending.remove(index)?;
                (received_ns, i128::from(sync.correction_field) + i128::from(header.correction_field))
            }
            _ => return None,
        };
        if origin.to_nanos() == 0 { return None; }
        // correctionField is signed nanoseconds scaled by 2^16. Clock applies
        // offsets by addition, so the sign MUST be remote minus local.
        let remote_ns = i128::try_from(origin.to_nanos()).ok()? + correction / 65536;
        let raw = i64::try_from(remote_ns - i128::from(received_ns)).ok()?;
        // Keep the least delayed observation over a short, bounded window.
        // Reset across a clock step instead of retaining an obsolete epoch.
        if self.samples.back().is_some_and(|(previous, _)| raw.abs_diff(*previous) > 1_000_000_000) {
            self.samples.clear();
        }
        self.samples.retain(|(_, when)| now.duration_since(*when) < Duration::from_secs(2));
        self.samples.push_back((raw, now));
        while self.samples.len() > 32 { self.samples.pop_front(); }
        let best = self.samples.iter().map(|(offset, _)| *offset).max()?;
        Some(ClockOffset {
            offset_ns: best,
            // No round-trip measurement: do not claim a known error bound.
            error_ns: u64::MAX,
            rtt_ns: 0,
        })
    }

    fn matches(sync: &PtpHeader, follow: &PtpHeader) -> bool {
        sync.sequence_id == follow.sequence_id
            && sync.domain_number == follow.domain_number
            && sync.source_port_identity == follow.source_port_identity
    }
}

async fn run_bmca_slave(
    event_socket: tokio::net::UdpSocket,
    general_socket: tokio::net::UdpSocket,
    master_ip: std::net::IpAddr,
    event_dest: std::net::SocketAddr,
    clock_identity: [u8; 8],
    offset_tx: tokio::sync::watch::Sender<ClockOffset>,
) -> Result<()> {
    let mut event_buf = [0u8; 256];
    let mut general_buf = [0u8; 256];
    let mut clock = HomePodClock::default();
    let mut request_sequence = 0u16;
    loop {
        let (packet, event_port) = tokio::select! {
            result = event_socket.recv_from(&mut event_buf) => {
                let (len, source) = result?;
                if source.ip() != master_ip { continue; }
                (&event_buf[..len], true)
            }
            result = general_socket.recv_from(&mut general_buf) => {
                let (len, source) = result?;
                if source.ip() != master_ip { continue; }
                (&general_buf[..len], false)
            }
        };
        let local_ns = PtpTimestamp::now().to_nanos() as u64;
        if let Some(offset) = clock.observe(packet, event_port, local_ns, std::time::Instant::now()) {
            tracing::debug!("HomePod clock estimate: remote-minus-local={}ns (one-way)", offset.offset_ns);
            // SETUP's initial receiver is dropped before the streamer
            // subscribes. Keep updating across that normal handover.
            offset_tx.send_replace(offset);
            // Retain upstream Delay_Req probing so clock solicitation stays
            // unchanged. Publishing the estimate must not depend on its reply.
            request_sequence = request_sequence.wrapping_add(1);
            let mut request = PtpHeader::new(PtpMessageType::DelayReq, request_sequence);
            request.source_port_identity[..8].copy_from_slice(&clock_identity);
            request.source_port_identity[8..].copy_from_slice(&1u16.to_be_bytes());
            let mut bytes = request.serialize().to_vec();
            bytes.extend_from_slice(&PtpTimestamp::now().serialize());
            event_socket.send_to(&bytes, event_dest).await?;
        }
    }
}

#[cfg(test)]
mod bridge_ptp_clock_tests {
    use super::*;
    use tokio::net::UdpSocket;
    use std::time::Duration;

    fn packet(header: &PtpHeader, remote_ns: u128) -> Vec<u8> {
        let mut bytes = header.serialize().to_vec();
        bytes.extend_from_slice(&PtpTimestamp::from_nanos(remote_ns).serialize());
        bytes
    }

    #[test]
    fn follows_matching_source_sequence_and_domain_once_with_signed_corrections() {
        let mut clock = HomePodClock::default();
        let now = std::time::Instant::now();
        let mut sync = PtpHeader::new(PtpMessageType::Sync, 65535);
        sync.flags = 0x0200;
        sync.source_port_identity = [2; 10];
        sync.correction_field = 200 * 65536;
        assert!(clock.observe(&packet(&sync, 0), true, 5_000_000_000, now).is_none());
        let mut follow = sync.clone();
        follow.message_type = PtpMessageType::FollowUp;
        follow.correction_field = -50 * 65536;
        for field in 0..3 {
            let mut wrong = follow.clone();
            match field {
                0 => wrong.sequence_id = 0,
                1 => wrong.source_port_identity[0] ^= 1,
                _ => wrong.domain_number = 1,
            }
            assert!(clock.observe(&packet(&wrong, 3_000_000_000), false, 5_100_000_000, now).is_none());
        }
        let bytes = packet(&follow, 3_000_000_000);
        let offset = clock.observe(&bytes, false, 5_100_000_000, now).unwrap();
        // Use Sync reception, not the later Follow_Up arrival.
        assert_eq!(offset.offset_ns, -1_999_999_850);
        assert_eq!(Clock::new(44100).apply_offset(5_000_000_000, &offset), 3_000_000_150);
        assert!(clock.observe(&bytes, false, 5_100_000_000, now).is_none());
    }

    #[test]
    fn one_step_remote_ahead_uses_positive_offset() {
        let header = PtpHeader::new(PtpMessageType::Sync, 0);
        let offset = HomePodClock::default().observe(&packet(&header, 7_000_000_000),
            true, 5_000_000_000, std::time::Instant::now()).unwrap();
        assert_eq!(offset.offset_ns, 2_000_000_000);
    }

    #[test]
    fn rejects_invalid_packet_lengths_versions_and_nanoseconds() {
        let now = std::time::Instant::now();
        let mut clock = HomePodClock::default();
        let header = PtpHeader::new(PtpMessageType::Sync, 0);
        let valid = packet(&header, 3_000_000_000);
        assert!(clock.observe(&valid[..43], true, 5_000_000_000, now).is_none());
        for field in 0..3 {
            let mut bad = valid.clone();
            match field {
                0 => bad[1] = 1,
                1 => bad[2..4].copy_from_slice(&80u16.to_be_bytes()),
                _ => bad[40..44].copy_from_slice(&1_000_000_000u32.to_be_bytes()),
            }
            assert!(clock.observe(&bad, true, 5_000_000_000, now).is_none());
        }
    }

    #[test]
    fn filters_delayed_samples_but_expires_old_estimates_and_reacquires_steps() {
        let mut clock = HomePodClock::default();
        let now = std::time::Instant::now();
        let header = PtpHeader::new(PtpMessageType::Sync, 1);
        let bytes = packet(&header, 1_000_000_000);
        assert_eq!(clock.observe(&bytes, true, 1_001_000_000, now).unwrap().offset_ns, -1_000_000);
        assert_eq!(clock.observe(&bytes, true, 1_004_000_000, now + Duration::from_millis(100)).unwrap().offset_ns, -1_000_000);
        assert_eq!(clock.observe(&bytes, true, 1_003_000_000, now + Duration::from_millis(2200)).unwrap().offset_ns, -3_000_000);
        assert_eq!(clock.observe(&bytes, true, 11_000_000_000, now + Duration::from_millis(2300)).unwrap().offset_ns, -10_000_000_000);
    }

    #[test]
    fn stale_sync_is_not_paired_with_later_followup() {
        let mut clock = HomePodClock::default();
        let now = std::time::Instant::now();
        let mut header = PtpHeader::new(PtpMessageType::Sync, 0);
        header.flags = 0x0200;
        clock.observe(&packet(&header, 0), true, 5_000_000_000, now);
        header.message_type = PtpMessageType::FollowUp;
        assert!(clock.observe(&packet(&header, 3_000_000_000), false,
            6_500_000_000, now + Duration::from_millis(1500)).is_none());
    }

    #[tokio::test]
    async fn clock_updates_survive_setup_receiver_handover() {
        let event = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let general = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let event_addr = event.local_addr().unwrap();
        let peer = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let (tx, rx) = tokio::sync::watch::channel(ClockOffset::default());
        let observer = tx.clone();
        drop(rx); // SETUP returned; the streamer has not subscribed yet.
        let task = tokio::spawn(run_bmca_slave(event, general,
            peer.local_addr().unwrap().ip(), peer.local_addr().unwrap(), [9; 8], tx));
        let header = PtpHeader::new(PtpMessageType::Sync, 1);
        peer.send_to(&packet(&header, 3_000_000_000), event_addr).await.unwrap();
        let mut bytes = [0u8; 64];
        let request = tokio::time::timeout(Duration::from_millis(300), peer.recv_from(&mut bytes)).await;
        let replacement = observer.subscribe();
        let offset = *replacement.borrow();
        task.abort();
        let _ = task.await;
        assert!(matches!(request, Ok(Ok((44, _)))), "Clock task stopped during receiver handover");
        assert!(offset.offset_ns < -1_000_000_000, "New subscriber must see the latest clock estimate");
    }

    #[tokio::test]
    async fn homepod_sync_follow_up_without_delay_response_sets_remote_clock() {
        let event = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let general = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let event_addr = event.local_addr().unwrap();
        let general_addr = general.local_addr().unwrap();
        let peer = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let (tx, mut rx) = tokio::sync::watch::channel(ClockOffset::default());
        let task = tokio::spawn(run_bmca_slave(event, general,
            peer.local_addr().unwrap().ip(), peer.local_addr().unwrap(), [9;8], tx));
        let mut sync = PtpHeader::new(PtpMessageType::Sync, 7);
        sync.flags = 0x0200;
        sync.source_port_identity = [1;10];
        let mut packet = sync.serialize().to_vec();
        packet.extend_from_slice(&[0;10]);
        peer.send_to(&packet, event_addr).await.unwrap();
        tokio::time::sleep(Duration::from_millis(10)).await;
        let mut follow = PtpHeader::new(PtpMessageType::FollowUp, 7);
        follow.source_port_identity = sync.source_port_identity;
        let remote = PtpTimestamp::from_nanos(3_789_794_000_000_000);
        let mut packet = follow.serialize().to_vec();
        packet.extend_from_slice(&remote.serialize());
        peer.send_to(&packet, general_addr).await.unwrap();
        // A HomePod sends these messages without replying to Delay_Req.
        let received = tokio::time::timeout(Duration::from_millis(300), rx.changed()).await;
        let mut keepalive = [0u8; 64];
        let request = tokio::time::timeout(Duration::from_millis(300), peer.recv_from(&mut keepalive)).await;
        task.abort();
        let _ = task.await;
        assert!(matches!(received, Ok(Ok(()))), "No remote clock update from Sync/Follow_Up");
        assert!(matches!(request, Ok(Ok((44, _)))), "Clock keepalive was not sent");
        assert_eq!(PtpHeader::parse(&keepalive).unwrap().message_type, PtpMessageType::DelayReq);
        let offset = *rx.borrow();
        let clock = crate::clock::Clock::new(44100);
        let mapped = clock.apply_offset(clock.now_wall_ns(), &offset);
        assert!(mapped.abs_diff(remote.to_nanos() as u64) < 100_000_000,
            "Clock must map PC epoch to receiver uptime: mapped={mapped}, offset={}", offset.offset_ns);
    }
}
