#[cfg(test)]
mod bridge_discovery_tests {
    use super::*;
    use futures::StreamExt;

    // An in-runtime timeout cannot catch a poll_next implementation that never
    // returns Pending. Bound the real current-thread runtime in a child process.
    fn run_bounded(case: &str, test_name: &str) {
        if std::env::var("BRIDGE_DISCOVERY_TEST_CHILD").as_deref() == Ok(case) {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all().build().unwrap();
            runtime.block_on(async {
                let browser = ServiceBrowser::new().unwrap();
                if case == "browse" {
                    let mut stream = browser.browse().await.unwrap();
                    let result = tokio::time::timeout(Duration::from_millis(150), async {
                        while stream.next().await.is_some() {}
                    }).await;
                    assert!(result.is_err(), "browse must remain open until stopped");
                    browser.stop().await;
                    assert!(tokio::time::timeout(Duration::from_millis(250), stream.next())
                        .await.expect("stopped stream did not wake").is_none());
                } else {
                    let result = tokio::time::timeout(
                        Duration::from_millis(50), browser.scan(Duration::from_secs(2))
                    ).await;
                    assert!(result.is_err(), "scan blocked the runtime past its cancellation deadline");
                    browser.stop().await;
                }
            });
            return;
        }
        let mut child = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact", test_name, "--nocapture"])
            .env("BRIDGE_DISCOVERY_TEST_CHILD", case)
            .stdout(std::process::Stdio::null())
            .spawn().unwrap();
        let start = std::time::Instant::now();
        loop {
            if let Some(status) = child.try_wait().unwrap() {
                assert!(status.success(), "discovery child failed: {status}");
                break;
            }
            if start.elapsed() > Duration::from_secs(3) {
                child.kill().unwrap();
                child.wait().unwrap();
                panic!("discovery blocked the async runtime for over three seconds");
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }

    #[test]
    fn browse_allows_timeout_and_stop_on_current_thread_runtime() {
        run_bounded("browse", "browser::bridge_discovery_tests::browse_allows_timeout_and_stop_on_current_thread_runtime");
    }

    #[test]
    fn scan_allows_cancellation_on_current_thread_runtime() {
        run_bounded("scan", "browser::bridge_discovery_tests::scan_allows_cancellation_on_current_thread_runtime");
    }
}
