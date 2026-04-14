"""Tests for structlog contextvars binding in run_round and check_relays_alive.

RED phase: tests verify that log records emitted inside run_round carry
src/dst/worker_id fields, and records inside check_relays_alive carry a
relay field — injected automatically via bound_contextvars, not by explicit
string formatting.
"""

import json
import logging
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import structlog

from chatmail_prober.log_config import configure_logging
from chatmail_prober.metrics import relay_status  # reset between tests


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_args(tmp_path, workers=2, count=1, timeout=30):
    args = MagicMock()
    args.workers = workers
    args.count = count
    args.ping_interval = 0.0
    args.timeout = timeout
    args.cache_dir = str(tmp_path)
    return args


def _ok_result(src, dst):
    from chatmail_prober.prober import ProbeResult
    return ProbeResult(src, dst, sent=1, received=1, loss=0.0, rtts_ms=[50.0])


def _err_result(src, dst, error="probe failed"):
    from chatmail_prober.prober import ProbeResult
    return ProbeResult(src, dst, error=error)


# ---------------------------------------------------------------------------
# run_round: src/dst/worker_id bound into every probe log record
# ---------------------------------------------------------------------------

class TestRunRoundContextBinding:
    """run_round binds src, dst, worker_id into structlog context per probe."""

    def setup_method(self):
        configure_logging(tty=False, level=logging.DEBUG)

    def test_probe_log_carries_src_and_dst(self, tmp_path, monkeypatch, capsys):
        """A warning emitted for a failed probe must carry src and dst as JSON fields."""
        from chatmail_prober.__main__ import run_round

        def _fake_probe(src, dst, *a, **kw):
            return _err_result(src, dst, error="timeout")

        monkeypatch.setattr("chatmail_prober.__main__.run_probe", _fake_probe)
        monkeypatch.setattr("chatmail_prober.__main__.update_metrics", lambda r: None)

        args = _make_args(tmp_path, workers=1)
        executor = __import__("concurrent.futures", fromlist=["ThreadPoolExecutor"]).ThreadPoolExecutor(max_workers=1)
        pool = MagicMock()
        pool.contexts.return_value = {}

        try:
            run_round(["relay.a", "relay.b"], args,
                      executors=[executor], worker_pools=[pool],
                      shutdown_event=threading.Event())
        finally:
            executor.shutdown(wait=True)

        captured = capsys.readouterr()
        warning_lines = [
            json.loads(l) for l in captured.err.splitlines()
            if l.strip() and '"level": "warn"' in l
        ]
        assert warning_lines, "expected at least one warning log line"
        # Every warning about a failed probe must carry src and dst
        probe_warnings = [l for l in warning_lines if "timeout" in str(l)]
        assert probe_warnings, "expected probe failure warning"
        for rec in probe_warnings:
            assert "src" in rec, f"missing src in {rec}"
            assert "dst" in rec, f"missing dst in {rec}"

    def test_probe_log_carries_worker_id(self, tmp_path, monkeypatch, capsys):
        """A warning emitted for a failed probe must carry worker_id as a JSON field."""
        from chatmail_prober.__main__ import run_round

        def _fake_probe(src, dst, *a, **kw):
            return _err_result(src, dst, error="rpc crash")

        monkeypatch.setattr("chatmail_prober.__main__.run_probe", _fake_probe)
        monkeypatch.setattr("chatmail_prober.__main__.update_metrics", lambda r: None)

        args = _make_args(tmp_path, workers=1)
        executor = __import__("concurrent.futures", fromlist=["ThreadPoolExecutor"]).ThreadPoolExecutor(max_workers=1)
        pool = MagicMock()
        pool.contexts.return_value = {}

        try:
            run_round(["relay.a", "relay.b"], args,
                      executors=[executor], worker_pools=[pool],
                      shutdown_event=threading.Event())
        finally:
            executor.shutdown(wait=True)

        captured = capsys.readouterr()
        warning_lines = [
            json.loads(l) for l in captured.err.splitlines()
            if l.strip() and '"level": "warn"' in l
        ]
        probe_warnings = [l for l in warning_lines if "rpc crash" in str(l)]
        assert probe_warnings
        for rec in probe_warnings:
            assert "worker_id" in rec, f"missing worker_id in {rec}"

    def test_success_log_carries_src_and_dst(self, tmp_path, monkeypatch, capsys):
        """An info log for a successful probe must carry src and dst."""
        from chatmail_prober.__main__ import run_round

        def _fake_probe(src, dst, *a, **kw):
            return _ok_result(src, dst)

        monkeypatch.setattr("chatmail_prober.__main__.run_probe", _fake_probe)
        monkeypatch.setattr("chatmail_prober.__main__.update_metrics", lambda r: None)

        args = _make_args(tmp_path, workers=1)
        executor = __import__("concurrent.futures", fromlist=["ThreadPoolExecutor"]).ThreadPoolExecutor(max_workers=1)
        pool = MagicMock()
        pool.contexts.return_value = {}

        try:
            run_round(["relay.a", "relay.b"], args,
                      executors=[executor], worker_pools=[pool],
                      shutdown_event=threading.Event())
        finally:
            executor.shutdown(wait=True)

        captured = capsys.readouterr()
        info_lines = [
            json.loads(l) for l in captured.err.splitlines()
            if l.strip() and '"level": "info"' in l
        ]
        probe_infos = [l for l in info_lines if "avg_ms" in l or "probe_ok" in l.get("event", "")]
        assert probe_infos, "expected probe success info log"
        for rec in probe_infos:
            assert "src" in rec
            assert "dst" in rec


# ---------------------------------------------------------------------------
# check_relays_alive: relay bound into every per-relay log record
# ---------------------------------------------------------------------------

class TestCheckRelaysAliveContextBinding:
    """check_relays_alive binds relay into structlog context per relay check."""

    def setup_method(self):
        configure_logging(tty=False, level=logging.DEBUG)

    def test_dead_relay_log_carries_relay_field(self, tmp_path, monkeypatch, capsys):
        """A DEAD warning must carry a relay field matching the dead relay."""
        from chatmail_prober.__main__ import check_relays_alive

        def _fake_probe(src, dst, *a, **kw):
            if src == "dead.relay":
                return _err_result(src, dst, error="DNS failure")
            return _ok_result(src, dst)

        monkeypatch.setattr("chatmail_prober.__main__.run_probe", _fake_probe)
        monkeypatch.setattr("chatmail_prober.__main__.clear_stale_relay_labels", lambda r: None)
        monkeypatch.setattr("chatmail_prober.__main__.relay_status",
                            MagicMock(**{"labels.return_value": MagicMock()}))
        monkeypatch.setattr("chatmail_prober.__main__.verify_relay_status", lambda r, e: -6)

        args = _make_args(tmp_path, workers=2)
        check_relays_alive(["dead.relay", "ok.relay"], args, Path(args.cache_dir))

        captured = capsys.readouterr()
        warning_lines = [
            json.loads(l) for l in captured.err.splitlines()
            if l.strip() and '"level": "warn"' in l
        ]
        dead_warnings = [l for l in warning_lines if "DNS failure" in str(l) or "DEAD" in str(l.get("event", ""))]
        assert dead_warnings, "expected DEAD warning"
        for rec in dead_warnings:
            assert "relay" in rec, f"missing relay field in {rec}"
            assert rec["relay"] == "dead.relay"

    def test_ok_relay_log_carries_relay_field(self, tmp_path, monkeypatch, capsys):
        """An OK info log must carry a relay field."""
        from chatmail_prober.__main__ import check_relays_alive

        monkeypatch.setattr("chatmail_prober.__main__.run_probe",
                            lambda src, dst, *a, **kw: _ok_result(src, dst))
        monkeypatch.setattr("chatmail_prober.__main__.clear_stale_relay_labels", lambda r: None)
        monkeypatch.setattr("chatmail_prober.__main__.relay_status",
                            MagicMock(**{"labels.return_value": MagicMock()}))
        monkeypatch.setattr("chatmail_prober.__main__.verify_relay_status", lambda r, e: 1)

        args = _make_args(tmp_path, workers=1)
        check_relays_alive(["ok.relay"], args, Path(args.cache_dir))

        captured = capsys.readouterr()
        info_lines = [
            json.loads(l) for l in captured.err.splitlines()
            if l.strip() and '"level": "info"' in l
        ]
        ok_infos = [l for l in info_lines if "OK" in str(l.get("event", "")) or "relay" in l]
        assert ok_infos, "expected OK info log with relay field"
        for rec in ok_infos:
            if "relay" in rec:
                assert rec["relay"] == "ok.relay"
