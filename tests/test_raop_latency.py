"""Receiver-side AirPlay buffer control.

pyatv hardcodes 22050 + sample_rate frames (1.5s at 44.1kHz) with no
setting, and it is the single largest remaining latency term.
"""
import pytest

from homepod_bridge import raop_latency

StreamContext = pytest.importorskip(
    "pyatv.protocols.raop.protocols"
).StreamContext


@pytest.fixture
def fresh(monkeypatch):
    """Guarantee a pristine pyatv before AND after each test - other tests
    (any TrayApp construction) patch it globally."""
    raop_latency.restore()
    monkeypatch.delenv("HOMEPOD_BRIDGE_RAOP_LATENCY", raising=False)
    yield raop_latency
    raop_latency.restore()


def context(sample_rate=44100):
    ctx = StreamContext()
    ctx.sample_rate = sample_rate
    ctx.reset()
    return ctx


def test_stock_pyatv_holds_one_and_a_half_seconds(fresh):
    ctx = context()
    assert ctx.latency == 22050 + 44100  # 1.5s - the value we are cutting


def test_apply_shortens_the_receiver_hold(fresh):
    assert fresh.apply(0.5) is True
    ctx = context()
    assert ctx.latency == int(0.5 * 44100)
    assert ctx.latency / 44100 == pytest.approx(0.5)


def test_latency_follows_the_session_sample_rate(fresh):
    fresh.apply(0.5)
    assert context(48000).latency == 24000  # frames, not a fixed count


@pytest.mark.parametrize(
    "requested,expected",
    [(0.05, 0.25), (0.25, 0.25), (1.0, 1.0), (5.0, 2.0)],
)
def test_clamped_to_the_range_pyatv_advertises(fresh, requested, expected):
    """pyatv advertises latencyMin 11025 / latencyMax 88200 in SETUP; asking
    for something outside what we advertised is not safe."""
    fresh.apply(requested)
    assert context().latency == int(expected * 44100)


def test_env_var_restores_stock_behaviour(fresh, monkeypatch):
    monkeypatch.setenv("HOMEPOD_BRIDGE_RAOP_LATENCY", "1.5")
    fresh.apply(0.5)  # argument is overridden by the env var
    assert context().latency == int(1.5 * 44100)


def test_apply_is_idempotent(fresh):
    fresh.apply(0.5)
    fresh.apply(0.5)
    fresh.apply(0.5)  # must not stack wrappers
    assert context().latency == int(0.5 * 44100)


def test_invalid_env_var_falls_back_to_the_argument(fresh, monkeypatch):
    monkeypatch.setenv("HOMEPOD_BRIDGE_RAOP_LATENCY", "loud")
    fresh.apply(0.75)
    assert context().latency == int(0.75 * 44100)
