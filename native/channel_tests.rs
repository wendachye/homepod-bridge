#[cfg(test)]
mod bridge_channel_tests {
    use super::*;

    #[test]
    fn framed_messages_interoperate_with_the_active_control_cipher() {
        for size in [1, 4, 1024, 1025, 2500] {
            let plaintext = vec![0x5a; size];
            let mut channel = EncryptedChannel::with_keys([1; 32], [2; 32]);
            let mut peer = ControlCipher::new([2; 32], [1; 32]);
            let framed = channel.encrypt(&plaintext).unwrap();
            assert_eq!(peer.decrypt(&framed).unwrap(), plaintext);
            let reply = peer.encrypt(&plaintext).unwrap();
            assert_eq!(channel.decrypt(&reply).unwrap(), plaintext);
            assert_eq!(channel.write_nonce(), ((size + 1023) / 1024) as u64);
        }
    }

    #[test]
    fn length_parser_counts_the_tag_outside_the_plaintext_length() {
        assert_eq!(EncryptedChannel::parse_frame_length(&[4, 0]), Some(22));
        assert_eq!(EncryptedChannel::parse_frame_length(&[0, 4]), Some(1042));
        assert_eq!(EncryptedChannel::parse_frame_length(&[4]), None);
    }

    #[test]
    fn raw_block_interoperates_without_a_second_length_prefix() {
        let mut channel = EncryptedChannel::with_keys([1; 32], [2; 32]);
        let mut peer = ControlCipher::new([2; 32], [1; 32]);
        let encrypted = channel.encrypt_raw(b"hello").unwrap();
        assert_eq!(encrypted.len(), 5 + 16);
        assert_eq!(peer.decrypt_raw(&encrypted).unwrap(), b"hello");
    }

    #[test]
    fn raw_api_rejects_empty_or_multiple_blocks_without_consuming_a_nonce() {
        let mut channel = EncryptedChannel::with_keys([1; 32], [2; 32]);
        assert!(channel.encrypt_raw(&[]).is_err());
        assert!(channel.encrypt_raw(&vec![0; 1025]).is_err());
        assert_eq!(channel.write_nonce(), 0);
    }
}
