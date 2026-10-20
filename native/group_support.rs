// SPDX-License-Identifier: GPL-3.0-or-later
// Included in airplay-client::client by the reproducible build.
impl AirPlayClient {
    /// Apply one UI slider without changing the other group members.
    pub async fn set_group_member_volume(&mut self, index: usize, volume: f32) -> Result<()> {
        let connection = if index == 0 {
            self.connection.as_mut()
        } else {
            self.group_connections.get_mut(index - 1)
        }.ok_or_else(|| Error::Rtsp(RtspError::NoSession))?;
        connection.set_volume(volume).await
    }

    /// A missing secondary must fail the group, so the caller reconnects all
    /// members on a fresh common clock instead of silently leaving one behind.
    pub async fn send_group_feedback(&mut self) -> Result<()> {
        self.connection.as_mut().ok_or_else(|| Error::Rtsp(RtspError::NoSession))?
            .send_feedback().await?;
        for connection in &mut self.group_connections {
            connection.send_feedback().await?;
        }
        Ok(())
    }
}
