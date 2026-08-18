"""Platform-neutral: on Windows this exercises the real named-mutex path
(which CI previously skipped - hiding a retained-handle bug where a failed
acquire kept the dead owner's mutex alive forever)."""
from homepod_bridge.single_instance import SingleInstance


def test_second_instance_is_rejected_until_first_releases():
    name = "homepod-bridge-test-lock"
    first, second = SingleInstance(name), SingleInstance(name)
    try:
        assert first.acquire() is True
        assert second.acquire() is False  # someone is already running
        first.release()
        assert second.acquire() is True  # lock is reusable after release
    finally:
        first.release()
        second.release()


def test_release_is_idempotent():
    guard = SingleInstance("homepod-bridge-test-lock-2")
    assert guard.acquire() is True
    guard.release()
    guard.release()  # must not raise
