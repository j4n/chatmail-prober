"""Tests for --unreachable/-u relay file support.

Design contract:
  - --unreachable FILE loads a list of known-dead relays
  - These relays are included in the alive check but NOT in the probe matrix
  - If an unreachable relay passes the alive check it is promoted to the active
    set for that round and logged as relay_recovered
  - smoke_check.py excludes unreachable relays from the expected matrix N
"""
from __future__ import annotations

import pathlib
import sys
import textwrap
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# parse_args tests
# ---------------------------------------------------------------------------

from chatmail_prober.__main__ import parse_args


class TestUnreachableFlag:
    def test_unreachable_flag_long_form(self, tmp_path):
        f = tmp_path / "unreachable.txt"
        f.write_text("dead.example\n")
        args = parse_args(["relays.txt", f"--unreachable={f}"])
        assert args.unreachable == str(f)

    def test_unreachable_flag_short_form(self, tmp_path):
        f = tmp_path / "unreachable.txt"
        f.write_text("dead.example\n")
        args = parse_args(["relays.txt", "-u", str(f)])
        assert args.unreachable == str(f)

    def test_unreachable_defaults_to_none(self):
        args = parse_args(["relays.txt"])
        assert args.unreachable is None


# ---------------------------------------------------------------------------
# read_relay_list with unreachable file
# ---------------------------------------------------------------------------

from chatmail_prober.__main__ import read_relay_list


class TestReadUnreachableRelays:
    def test_unreachable_file_is_readable(self, tmp_path):
        f = tmp_path / "unreachable.txt"
        f.write_text("dead.example\nstate.blocked.example\n")
        result = read_relay_list([f])
        assert result == ["dead.example", "state.blocked.example"]


# ---------------------------------------------------------------------------
# check_relays_alive with unreachable relays
# ---------------------------------------------------------------------------

from chatmail_prober.__main__ import check_relays_alive


def _make_args(**kwargs):
    defaults = dict(
        workers=2, count=1, ping_interval=1, timeout=10,
        alive_check_interval=86400,
    )
    defaults.update(kwargs)
    return MagicMock(**defaults)


def _mock_probe(error=None, rtts_ms=None):
    from chatmail_prober.prober import ProbeResult
    return ProbeResult(
        source="a.example", destination="a.example",
        sent=1, received=0 if error else 1,
        rtts_ms=rtts_ms or ([] if error else [100.0]),
        error=error,
        account_setup_time=0.5,
    )


class TestUnreachablePromotion:
    """Unreachable relays that pass the alive check are promoted to active set."""

    def test_recovered_unreachable_relay_is_included_in_alive(self, tmp_path):
        """A relay in unreachable_relays that passes the alive check is promoted."""
        normal = ["a.example"]
        unreachable = ["was.dead.example"]

        def fake_probe(src, dst, count, ping_interval, timeout, relay_contexts):
            # Both relays succeed
            return _mock_probe()

        args = _make_args()
        with patch(
            "chatmail_prober.__main__.run_probe", side_effect=fake_probe
        ), patch(
            "chatmail_prober.__main__.RelayPool"
        ) as mock_pool_cls:
            mock_pool = MagicMock()
            mock_pool.contexts.return_value = {}
            mock_pool_cls.return_value = mock_pool
            alive, dead = check_relays_alive(
                normal, args,
                unreachable_relays=unreachable,
            )

        assert "was.dead.example" in alive

    def test_still_dead_unreachable_relay_stays_out(self, tmp_path):
        """A relay in unreachable_relays that fails the alive check is NOT promoted."""
        normal = ["a.example"]
        unreachable = ["still.dead.example"]

        def fake_probe(src, dst, count, ping_interval, timeout, relay_contexts):
            if src == "still.dead.example":
                return _mock_probe(error="timeout")
            return _mock_probe()

        args = _make_args()
        with patch(
            "chatmail_prober.__main__.run_probe", side_effect=fake_probe
        ), patch(
            "chatmail_prober.__main__.RelayPool"
        ) as mock_pool_cls:
            mock_pool = MagicMock()
            mock_pool.contexts.return_value = {}
            mock_pool_cls.return_value = mock_pool
            alive, dead = check_relays_alive(
                normal, args,
                unreachable_relays=unreachable,
            )

        assert "still.dead.example" not in alive
        assert "still.dead.example" in dead

    def test_recovered_relay_logged(self, tmp_path, caplog):
        """A recovered unreachable relay is logged as relay_recovered_from_unreachable."""
        import logging
        normal = ["a.example"]
        unreachable = ["was.dead.example"]

        def fake_probe(src, dst, count, ping_interval, timeout, relay_contexts):
            return _mock_probe()

        args = _make_args()
        with patch(
            "chatmail_prober.__main__.run_probe", side_effect=fake_probe
        ), patch(
            "chatmail_prober.__main__.RelayPool"
        ) as mock_pool_cls:
            mock_pool = MagicMock()
            mock_pool.contexts.return_value = {}
            mock_pool_cls.return_value = mock_pool
            with caplog.at_level(logging.WARNING, logger="chatmail_prober.__main__"):
                alive, dead = check_relays_alive(
                    normal, args,
                    unreachable_relays=unreachable,
                )

        assert any("relay_recovered_from_unreachable" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# smoke_check.py --exclude-unreachable
# ---------------------------------------------------------------------------

from scripts.smoke_check import _read_relays, main as smoke_main


class TestSmokeCheckUnreachableExclusion:
    def _write_prom(self, path: pathlib.Path, relays: list[str]) -> None:
        lines = [
            "# HELP cmping_rtt_median_seconds rtt",
            "# TYPE cmping_rtt_median_seconds gauge",
        ]
        for s in relays:
            for d in relays:
                lines.append(
                    f'cmping_rtt_median_seconds{{source="{s}",destination="{d}",probe_type="cross"}} 0.5'
                )
        lines += [
            "# HELP cmping_account_setup_seconds setup",
            "# TYPE cmping_account_setup_seconds gauge",
        ]
        for s in relays:
            for d in relays:
                lines.append(
                    f'cmping_account_setup_seconds{{source="{s}",destination="{d}",probe_type="cross"}} 1.0'
                )
        lines += [
            "# HELP cmping_relay_status status",
            "# TYPE cmping_relay_status gauge",
        ]
        for r in relays:
            lines.append(f'cmping_relay_status{{relay="{r}"}} 1')
        lines += [
            "cmping_last_round_completion_timestamp 1744540000",
            "cmping_round_duration_seconds 12.4",
            "cmping_rounds_total 42",
        ]
        path.write_text("\n".join(lines) + "\n")

    def test_unreachable_relays_excluded_from_expected_n(self, tmp_path):
        """With 3 relays total and 1 unreachable, expected N is 2, not 3."""
        active = ["a.example", "b.example"]
        unreachable = ["dead.example"]

        relay_file = tmp_path / "relays.txt"
        relay_file.write_text("\n".join(active + unreachable) + "\n")

        unreachable_file = tmp_path / "unreachable.txt"
        unreachable_file.write_text("\n".join(unreachable) + "\n")

        prom_file = tmp_path / "chatmail-prober.prom"
        self._write_prom(prom_file, active)  # only active relays in .prom

        rc = smoke_main([
            "--prom", str(prom_file),
            "--relays", str(relay_file),
            "--exclude-unreachable", str(unreachable_file),
        ])
        assert rc == 0, "Expected PASS when unreachable relays are excluded from N"
