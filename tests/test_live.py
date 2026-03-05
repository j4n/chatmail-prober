"""Live integration tests against real chatmail relays.

Set CMPING_LIVE_TEST to a comma-separated list of relays to enable:

    CMPING_LIVE_TEST=nine.testrun.org uv run pytest tests/test_live.py -v
    CMPING_LIVE_TEST=nine.testrun.org,tarpit.fun,mehl.cloud uv run pytest tests/test_live.py -v

Tests adapt to however many relays are provided:
- 1 relay: self-loop + error handling tests
- 2+ relays: also cross-relay tests for all ordered pairs
"""

import os

import pytest

from chatmail_prober.prober import run_probe


def _get_relays():
    """Parse relay list from CMPING_LIVE_TEST env var."""
    val = os.environ.get("CMPING_LIVE_TEST", "")
    if not val:
        return []
    return [r.strip() for r in val.split(",") if r.strip()]


RELAYS = _get_relays()

live = pytest.mark.skipif(not RELAYS, reason="set CMPING_LIVE_TEST=relay1,relay2,...")


def _self_loop_ids():
    return [f"{r}->{r}" for r in RELAYS]


def _cross_pair_ids():
    return [f"{s}->{d}" for s in RELAYS for d in RELAYS if s != d]


@live
class TestLiveSelfLoop:
    """Probe each relay to itself."""

    @pytest.mark.parametrize("relay", RELAYS, ids=_self_loop_ids())
    def test_self_loop(self, relay, tmp_path):
        result = run_probe(relay, relay, count=3, cache_dir=str(tmp_path / "cache"))

        assert result.error is None, f"{relay} self-loop failed: {result.error}"
        assert result.sent == 3
        assert result.received > 0
        assert result.loss < 100.0
        assert len(result.rtts_ms) == result.received
        assert all(rtt > 0 for rtt in result.rtts_ms)
        assert result.account_setup_time > 0
        assert result.message_time > 0


@live
@pytest.mark.skipif(len(RELAYS) < 2, reason="need >=2 relays for cross-relay tests")
class TestLiveCrossRelay:
    """Probe all ordered cross-relay pairs."""

    @pytest.mark.parametrize(
        "src,dst",
        [(s, d) for s in RELAYS for d in RELAYS if s != d],
        ids=_cross_pair_ids(),
    )
    def test_pair(self, src, dst, tmp_path):
        result = run_probe(src, dst, count=2, cache_dir=str(tmp_path / "cache"))

        assert result.error is None, f"{src} -> {dst} failed: {result.error}"
        assert result.sent == 2
        assert result.received > 0
        assert len(result.rtts_ms) == result.received
        assert all(rtt > 0 for rtt in result.rtts_ms)


@live
class TestLiveErrorHandling:
    """Verify graceful handling of unreachable relays."""

    def test_nonexistent_relay(self, tmp_path):
        result = run_probe(
            "nonexistent.invalid", RELAYS[0],
            count=1, cache_dir=str(tmp_path / "cache"),
        )
        assert result.error is not None
        assert result.sent == 0
