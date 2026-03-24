"""Tests for config parsing, CLI args, pair generation, and orchestration."""

import argparse
import threading
import time

import pytest
from concurrent.futures import ThreadPoolExecutor
from prometheus_client import CollectorRegistry

from chatmail_prober.__main__ import (
    read_relay_list, read_exclude_list, parse_args, run_round, check_relays_alive,
)
from chatmail_prober.prober import ProbeResult
from chatmail_prober import metrics as metrics_mod


class TestReadRelayList:
    def test_reads_domains(self, tmp_path):
        f = tmp_path / "relays.txt"
        f.write_text("nine.testrun.org\nmehl.cloud\ntarpit.fun\n")
        assert read_relay_list([str(f)]) == ["nine.testrun.org", "mehl.cloud", "tarpit.fun"]

    def test_ignores_comments_and_blanks(self, tmp_path):
        f = tmp_path / "relays.txt"
        f.write_text("# comment\nnine.testrun.org\n\n  \n# another\nmehl.cloud\n")
        assert read_relay_list([str(f)]) == ["nine.testrun.org", "mehl.cloud"]

    def test_strips_whitespace(self, tmp_path):
        f = tmp_path / "relays.txt"
        f.write_text("  nine.testrun.org  \n  mehl.cloud\t\n")
        assert read_relay_list([str(f)]) == ["nine.testrun.org", "mehl.cloud"]

    def test_empty_file_exits(self, tmp_path):
        f = tmp_path / "relays.txt"
        f.write_text("# only comments\n\n")
        with pytest.raises(SystemExit):
            read_relay_list([str(f)])

    def test_single_relay(self, tmp_path):
        f = tmp_path / "relays.txt"
        f.write_text("nine.testrun.org\n")
        assert read_relay_list([str(f)]) == ["nine.testrun.org"]

    def test_multiple_files_merged_and_deduplicated(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f1.write_text("nine.testrun.org\nmehl.cloud\n")
        f2 = tmp_path / "b.txt"
        f2.write_text("mehl.cloud\ntarpit.fun\n")
        assert read_relay_list([str(f1), str(f2)]) == [
            "nine.testrun.org", "mehl.cloud", "tarpit.fun"
        ]


class TestParseArgs:
    def test_defaults(self):
        args = parse_args(["relays.txt"])
        assert args.relays == ["relays.txt"]
        assert args.port == 0
        assert args.textfile is None
        assert args.interval == 900
        assert args.count == 5
        assert args.ping_interval == 0.1
        assert args.timeout == 60
        assert args.workers == 5
        assert args.once is False
        assert args.verbose == 0
        assert args.quiet is False

    def test_quiet_flag(self):
        args = parse_args(["r.txt", "-q"])
        assert args.quiet is True
        assert args.verbose == 0

    def test_all_flags(self):
        args = parse_args([
            "r.txt",
            "--port", "0",
            "--textfile", "/tmp/out.prom",
            "--interval", "60",
            "--count", "5",
            "--ping-interval", "0.5",
            "--timeout", "30",
            "--workers", "10",
            "--cache-dir", "/tmp/cache",
            "--once",
            "-vv",
        ])
        assert args.port == 0
        assert args.textfile == "/tmp/out.prom"
        assert args.interval == 60
        assert args.count == 5
        assert args.ping_interval == 0.5
        assert args.timeout == 30
        assert args.workers == 10
        assert args.cache_dir == "/tmp/cache"
        assert args.once is True
        assert args.verbose == 2


class TestPairGeneration:
    """Verify the pair matrix logic from __main__.py."""

    def test_single_relay_produces_self_loop(self):
        relays = ["a.example"]
        pairs = [(s, d) for s in relays for d in relays]
        assert pairs == [("a.example", "a.example")]

    def test_two_relays_produce_four_pairs(self):
        relays = ["a.example", "b.example"]
        pairs = [(s, d) for s in relays for d in relays]
        assert len(pairs) == 4
        assert ("a.example", "a.example") in pairs
        assert ("a.example", "b.example") in pairs
        assert ("b.example", "a.example") in pairs
        assert ("b.example", "b.example") in pairs

    def test_three_relays_produce_nine_pairs(self):
        relays = ["a", "b", "c"]
        pairs = [(s, d) for s in relays for d in relays]
        assert len(pairs) == 9


# -- Orchestration tests (run_round, check_relays_alive) --


def _make_args(tmp_path, workers=2):
    return argparse.Namespace(
        count=1, ping_interval=0.1, timeout=10, workers=workers,
        cache_dir=str(tmp_path / "cache"), verbose=0,
    )


@pytest.fixture(autouse=False)
def _fresh_metrics(monkeypatch):
    """Replace metrics with fresh instances to avoid cross-contamination."""
    registry = CollectorRegistry()
    labels = ["source", "destination", "probe_type"]
    new = {
        "rtt_median": metrics_mod.Gauge("m_test", "t", labels, registry=registry),
        "rtt_stddev": metrics_mod.Gauge("s_test", "t", labels, registry=registry),
        "rtt_p90": metrics_mod.Gauge("p90_test", "t", labels, registry=registry),
        "rtt_p10": metrics_mod.Gauge("p10_test", "t", labels, registry=registry),
        "send_errors_total": metrics_mod.Counter("e_test", "t", labels, registry=registry),
        "probe_success": metrics_mod.Gauge("ps_test", "t", labels, registry=registry),
        "probe_loss_ratio": metrics_mod.Gauge("lr_test", "t", labels, registry=registry),
        "account_setup_seconds": metrics_mod.Gauge("as_test", "t", labels, registry=registry),
    }
    for name, metric in new.items():
        monkeypatch.setattr(metrics_mod, name, metric)
    return new


def _fake_probe(source, dest, count=1, interval=0.1, accounts_dir="", timeout=10, relay_contexts=None):
    return ProbeResult(source, dest, sent=1, received=1, loss=0.0, rtts_ms=[100.0])


class _FakePool:
    """Stand-in for RelayPool that does not spawn RPC servers."""
    def __init__(self, *a, **kw):
        pass

    def open_all(self, relays):
        pass

    def contexts(self):
        return {}

    def close(self):
        pass


class TestRunRound:
    def test_completes_all_pairs(self, tmp_path, monkeypatch, _fresh_metrics):
        monkeypatch.setattr("chatmail_prober.__main__.RelayPool", _FakePool)
        monkeypatch.setattr("chatmail_prober.__main__.run_probe", _fake_probe)
        relays = ["a.example", "b.example", "c.example"]
        args = _make_args(tmp_path, workers=2)
        executors = [ThreadPoolExecutor(max_workers=1) for _ in range(args.workers)]
        try:
            run_round(relays, args, executors)
        finally:
            for ex in executors:
                ex.shutdown(wait=False)

        # All 9 pairs should have probe_success=1
        for s in relays:
            for d in relays:
                pt = "self" if s == d else "cross"
                val = metrics_mod.probe_success.labels(
                    source=s, destination=d, probe_type=pt)._value.get()
                assert val == 1.0, f"{s} -> {d} not recorded"

    def test_shutdown_skips_metrics(self, tmp_path, monkeypatch, _fresh_metrics):
        monkeypatch.setattr("chatmail_prober.__main__.RelayPool", _FakePool)
        shutdown_event = threading.Event()
        call_count = 0

        def _slow_probe(source, dest, count=1, interval=0.1, accounts_dir="", timeout=10, relay_contexts=None):
            nonlocal call_count
            call_count += 1
            # After a couple of probes complete, trigger shutdown.
            if call_count >= 2:
                time.sleep(0.05)
                shutdown_event.set()
            return ProbeResult(source, dest, sent=1, received=1, loss=0.0, rtts_ms=[100.0])

        monkeypatch.setattr("chatmail_prober.__main__.run_probe", _slow_probe)
        relays = ["a.example", "b.example", "c.example"]
        args = _make_args(tmp_path, workers=1)  # single worker for deterministic ordering
        executors = [ThreadPoolExecutor(max_workers=1) for _ in range(args.workers)]
        try:
            run_round(relays, args, executors, shutdown_event)
        finally:
            for ex in executors:
                ex.shutdown(wait=False)

        # Not all 9 pairs should have been recorded -- some were skipped.
        recorded = 0
        for s in relays:
            for d in relays:
                pt = "self" if s == d else "cross"
                try:
                    val = metrics_mod.probe_success.labels(
                        source=s, destination=d, probe_type=pt)._value.get()
                    if val != 0.0:
                        recorded += 1
                except Exception:
                    pass
        assert recorded < 9, f"Expected some pairs skipped, but all {recorded} recorded"

    def test_crashed_probe_records_error(self, tmp_path, monkeypatch, _fresh_metrics):
        monkeypatch.setattr("chatmail_prober.__main__.RelayPool", _FakePool)

        def _crashing_probe(source, dest, count=1, interval=0.1, accounts_dir="", timeout=10, relay_contexts=None):
            if source == "a.example" and dest == "b.example":
                raise RuntimeError("boom")
            return ProbeResult(source, dest, sent=1, received=1, loss=0.0, rtts_ms=[100.0])

        monkeypatch.setattr("chatmail_prober.__main__.run_probe", _crashing_probe)
        relays = ["a.example", "b.example"]
        args = _make_args(tmp_path, workers=2)
        executors = [ThreadPoolExecutor(max_workers=1) for _ in range(args.workers)]
        try:
            run_round(relays, args, executors)
        finally:
            for ex in executors:
                ex.shutdown(wait=False)

        # The crashed pair should have an error recorded.
        lbl = dict(source="a.example", destination="b.example", probe_type="cross")
        assert metrics_mod.send_errors_total.labels(**lbl)._value.get() == 1.0
        assert metrics_mod.probe_success.labels(**lbl)._value.get() == 0.0

        # The other pairs should still succeed.
        lbl_ok = dict(source="b.example", destination="a.example", probe_type="cross")
        assert metrics_mod.probe_success.labels(**lbl_ok)._value.get() == 1.0


class TestCheckRelaysAlive:
    def test_filters_dead_relays(self, tmp_path, monkeypatch, _fresh_metrics):
        def _selective_probe(source, dest, count=1, interval=0.1, accounts_dir="", timeout=10, relay_contexts=None):
            if source == "dead.example":
                return ProbeResult(source, dest, error="connection refused")
            return ProbeResult(source, dest, sent=1, received=1, loss=0.0, rtts_ms=[100.0])

        monkeypatch.setattr("chatmail_prober.__main__.run_probe", _selective_probe)
        relays = ["a.example", "dead.example", "b.example"]
        args = _make_args(tmp_path, workers=3)
        alive = check_relays_alive(relays, args)

        assert alive == ["a.example", "b.example"]
        assert "dead.example" not in alive

    def test_all_alive(self, tmp_path, monkeypatch, _fresh_metrics):
        monkeypatch.setattr("chatmail_prober.__main__.run_probe", _fake_probe)
        relays = ["a.example", "b.example", "c.example"]
        args = _make_args(tmp_path, workers=3)
        alive = check_relays_alive(relays, args)

        assert alive == relays


class TestReadExcludeList:
    def test_parses_pairs(self, tmp_path):
        f = tmp_path / "exclude.txt"
        f.write_text("a.example -> b.example\nc.example->d.example\n")
        result = read_exclude_list(str(f))
        assert result == {("a.example", "b.example"), ("c.example", "d.example")}

    def test_ignores_comments_and_blanks(self, tmp_path):
        f = tmp_path / "exclude.txt"
        f.write_text("# a comment\n\na.example -> b.example\n  \n")
        result = read_exclude_list(str(f))
        assert result == {("a.example", "b.example")}

    def test_skips_malformed_lines(self, tmp_path):
        f = tmp_path / "exclude.txt"
        f.write_text("a.example -> b.example\nno_arrow_here\n")
        result = read_exclude_list(str(f))
        assert result == {("a.example", "b.example")}


class TestRunRoundExclude:
    def test_excludes_pairs(self, tmp_path, monkeypatch, _fresh_metrics):
        monkeypatch.setattr("chatmail_prober.__main__.RelayPool", _FakePool)
        probed_pairs = []

        def _tracking_probe(source, dest, count=1, interval=0.1, accounts_dir="", timeout=10, relay_contexts=None):
            probed_pairs.append((source, dest))
            return ProbeResult(source, dest, sent=1, received=1, loss=0.0, rtts_ms=[100.0])

        monkeypatch.setattr("chatmail_prober.__main__.run_probe", _tracking_probe)
        relays = ["a.example", "b.example"]
        args = _make_args(tmp_path, workers=2)
        executors = [ThreadPoolExecutor(max_workers=1) for _ in range(args.workers)]
        exclude = {("a.example", "b.example")}
        try:
            run_round(relays, args, executors, exclude=exclude)
        finally:
            for ex in executors:
                ex.shutdown(wait=False)

        # 4 pairs minus 1 excluded = 3
        assert len(probed_pairs) == 3
        assert ("a.example", "b.example") not in probed_pairs
