"""Live integration tests against real chatmail relays.

Uses relays from relays.txt.example by default.  Override with
CMPING_LIVE_TEST=relay1,relay2,... to test different relays.

    uv run pytest tests/test_live.py -v
    CMPING_LIVE_TEST=custom.relay uv run pytest tests/test_live.py -v
"""

import os
import threading
from pathlib import Path

import pytest

from chatmail_prober.__main__ import read_relay_list
from chatmail_prober.prober import run_probe

EXAMPLE_RELAYS = str(Path(__file__).resolve().parent.parent / "relays.txt.example")


def _get_relays():
    """Parse relay list from CMPING_LIVE_TEST env var, or from relays.txt.example."""
    val = os.environ.get("CMPING_LIVE_TEST", "")
    if val:
        return [r.strip() for r in val.split(",") if r.strip()]
    return read_relay_list([EXAMPLE_RELAYS])


RELAYS = _get_relays()


def _self_loop_ids():
    return [f"{r}->{r}" for r in RELAYS]


def _cross_pair_ids():
    return [f"{s}->{d}" for s in RELAYS for d in RELAYS if s != d]


class TestLiveSelfLoop:
    """Probe each relay to itself."""

    @pytest.mark.parametrize("relay", RELAYS, ids=_self_loop_ids())
    def test_self_loop(self, relay, tmp_path):
        result = run_probe(relay, relay, count=3,
                           accounts_dir=str(tmp_path / "cache"))

        assert result.error is None, f"{relay} self-loop failed: {result.error}"
        assert result.sent == 3
        assert result.received > 0
        assert result.loss < 100.0
        assert len(result.rtts_ms) == result.received
        assert all(rtt > 0 for rtt in result.rtts_ms)
        assert result.account_setup_time > 0
        assert result.message_time > 0


@pytest.mark.skipif(len(RELAYS) < 2, reason="need >=2 relays for cross-relay tests")
class TestLiveCrossRelay:
    """Probe all ordered cross-relay pairs."""

    @pytest.mark.parametrize(
        "src,dst",
        [(s, d) for s in RELAYS for d in RELAYS if s != d],
        ids=_cross_pair_ids(),
    )
    def test_cross_relay(self, src, dst, tmp_path):
        result = run_probe(src, dst, count=2,
                           accounts_dir=str(tmp_path / "cache"))

        assert result.error is None, f"{src} -> {dst} failed: {result.error}"
        assert result.sent == 2
        assert result.received > 0
        assert len(result.rtts_ms) == result.received
        assert all(rtt > 0 for rtt in result.rtts_ms)


class TestLiveErrorHandling:
    """Verify graceful handling of unreachable relays."""

    def test_nonexistent_relay(self, tmp_path):
        result = run_probe(
            "nonexistent.invalid", RELAYS[0],
            count=1, accounts_dir=str(tmp_path / "cache"),
        )
        assert result.error is not None
        assert result.sent == 0


class TestThreadCleanup:
    """Verify that threads don't accumulate across probes."""

    def test_no_thread_leak(self, tmp_path):
        baseline = threading.active_count()
        for i in range(5):
            run_probe(RELAYS[0], RELAYS[0], count=1,
                      accounts_dir=str(tmp_path / "cache"), timeout=45)
        # Threads should not grow linearly with probe count.
        assert threading.active_count() <= baseline + 4, (
            f"Thread leak: started at {baseline}, now {threading.active_count()}"
        )
