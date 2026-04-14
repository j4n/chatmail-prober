"""Orchestration tests for check_relays_alive and run_round with mocked failures.

These tests verify the interaction between the alive-check filter and the
probe matrix: dead relays must be excluded from run_round, relay_status
metrics must reflect the correct integer codes, and the reopen guard must
not fire on application-level errors (DNS, auth, timeout).

All tests use monkeypatched run_probe; no live network or RPC binary needed.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from chatmail_prober.prober import ProbeResult
from chatmail_prober import metrics as metrics_mod
from chatmail_prober.__main__ import check_relays_alive, run_round


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _make_args(tmp_path, *, workers=3, timeout=90, count=1,
               ping_interval=0.1, interval=900):
    """Build a minimal argparse.Namespace for orchestration functions."""
    import argparse
    return argparse.Namespace(
        cache_dir=str(tmp_path),
        workers=workers,
        timeout=timeout,
        count=count,
        ping_interval=ping_interval,
        interval=interval,
        once=True,
        verbose=0,
        exclude=[],
    )


def _ok(src, dst):
    return ProbeResult(src, dst, sent=1, received=1, loss=0.0, rtts_ms=[50.0])


def _err(src, dst, error):
    return ProbeResult(src, dst, error=error)


@pytest.fixture(autouse=True)
def _fresh_metrics():
    """Clear all prometheus label sets before each test."""
    for metric in [
        metrics_mod.rtt_median, metrics_mod.rtt_stddev,
        metrics_mod.rtt_p90, metrics_mod.rtt_p10,
        metrics_mod.probe_success, metrics_mod.probe_loss_ratio,
        metrics_mod.account_setup_seconds, metrics_mod.send_errors_total,
        metrics_mod.relay_status,
    ]:
        metric._metrics.clear()
    yield


# ---------------------------------------------------------------------------
# TestAliveCheckMetrics — relay_status codes after check_relays_alive
# ---------------------------------------------------------------------------

class TestAliveCheckMetrics:
    """check_relays_alive must write the correct relay_status integer for each
    failure mode, matching the codes defined in relay_status_value()."""

    def test_dns_failure_sets_status_minus_six(self, tmp_path, monkeypatch):
        dns_error = (
            "Failed to setup sender profile on host.abc: JsonRpcError: "
            "{'code': -1, 'message': 'Error: IMAP failed to connect to "
            "imap.host.abc:993:tls: Could not find DNS resolutions for "
            "imap.host.abc:993. Check server hostname and your network'}"
        )

        def _probe(src, dst, count=1, interval=0.1, accounts_dir="",
                   timeout=10, relay_contexts=None):
            if src == "host.abc":
                return _err(src, dst, dns_error)
            return _ok(src, dst)

        monkeypatch.setattr("chatmail_prober.__main__.run_probe", _probe)
        args = _make_args(tmp_path, workers=2)
        alive, dead_set = check_relays_alive(["host.abc", "host.good"], args, Path(args.cache_dir))

        assert "host.abc" not in alive
        assert "host.abc" in dead_set
        assert "host.good" in alive
        assert metrics_mod.relay_status.labels(relay="host.abc")._value.get() == -6.0

    def test_auth_failure_sets_status_minus_three(self, tmp_path, monkeypatch):
        auth_error = (
            "Failed to setup sender profile on hostb.xyz: JsonRpcError: "
            "{'code': -1, 'message': 'Error: Cannot login as "
            '"user@hostb.xyz". [AUTHENTICATIONFAILED] Authentication failed.\'}'
        )

        def _probe(src, dst, count=1, interval=0.1, accounts_dir="",
                   timeout=10, relay_contexts=None):
            if src == "hostb.xyz":
                return _err(src, dst, auth_error)
            return _ok(src, dst)

        monkeypatch.setattr("chatmail_prober.__main__.run_probe", _probe)
        args = _make_args(tmp_path, workers=2)
        alive, dead_set = check_relays_alive(["hostb.xyz", "host.good"], args, Path(args.cache_dir))

        assert "hostb.xyz" not in alive
        assert metrics_mod.relay_status.labels(relay="hostb.xyz")._value.get() == -3.0

    def test_connection_timeout_sets_status_minus_one(self, tmp_path, monkeypatch):
        timeout_error = (
            "Failed to setup sender profile on hostd.xyz: JsonRpcError: "
            "{'code': -1, 'message': 'Error: IMAP failed to connect to "
            "hostd.xyz:993:tls: Connection timeout: deadline has elapsed'}"
        )

        def _probe(src, dst, count=1, interval=0.1, accounts_dir="",
                   timeout=10, relay_contexts=None):
            if src == "hostd.xyz":
                return _err(src, dst, timeout_error)
            return _ok(src, dst)

        monkeypatch.setattr("chatmail_prober.__main__.run_probe", _probe)
        args = _make_args(tmp_path, workers=2)
        alive, dead_set = check_relays_alive(["hostd.xyz", "host.good"], args, Path(args.cache_dir))

        assert "hostd.xyz" not in alive
        assert metrics_mod.relay_status.labels(relay="hostd.xyz")._value.get() == -1.0

    def test_online_relay_sets_status_one(self, tmp_path, monkeypatch):
        monkeypatch.setattr("chatmail_prober.__main__.run_probe",
                            lambda *a, **kw: _ok(a[0], a[1]))
        args = _make_args(tmp_path, workers=1)
        alive, dead_set = check_relays_alive(["host.good"], args, Path(args.cache_dir))

        assert alive == ["host.good"]
        assert dead_set == {}
        assert metrics_mod.relay_status.labels(relay="host.good")._value.get() == 1.0


# ---------------------------------------------------------------------------
# TestAliveCheckDoesNotTriggerReopen — application errors must not reopen RPC
# ---------------------------------------------------------------------------

class TestAliveCheckDoesNotTriggerReopen:
    """Application-level errors (DNS/auth/timeout) in check_relays_alive must
    not trigger pool.reopen().  check_relays_alive uses standalone run_probe
    (no relay_contexts), so there is no pool to reopen — but run_round does
    have one.  These tests verify the _rpc_keywords guard in run_round."""

    def test_dns_error_does_not_reopen_pool(self, tmp_path, monkeypatch):
        dns_error = (
            "Failed to setup sender profile on host.abc: JsonRpcError: "
            "{'code': -1, 'message': 'Error: Could not find DNS resolutions "
            "for imap.host.abc:993'}"
        )
        reopen_calls = []

        class _TrackingPool:
            def open_all(self, relays): pass
            def contexts(self): return {}
            def reopen(self, relay): reopen_calls.append(relay)
            def close(self): pass

        def _probe(src, dst, count=1, interval=0.1, accounts_dir="",
                   timeout=10, relay_contexts=None):
            if src == "host.abc":
                return _err(src, dst, dns_error)
            return _ok(src, dst)

        monkeypatch.setattr("chatmail_prober.__main__.run_probe", _probe)

        from concurrent.futures import ThreadPoolExecutor
        executor = ThreadPoolExecutor(max_workers=1)
        pool = _TrackingPool()
        shutdown_event = threading.Event()

        try:
            run_round(
                ["host.abc", "host.good"],
                _make_args(tmp_path, workers=1),
                executors=[executor],
                worker_pools=[pool],
                shutdown_event=shutdown_event,
            )
        finally:
            executor.shutdown(wait=False)

        assert reopen_calls == [], (
            f"pool.reopen() was called for {reopen_calls} on a DNS error — "
            "application-level errors must not trigger RPC context reopen."
        )

    def test_timeout_error_does_not_reopen_pool(self, tmp_path, monkeypatch):
        timeout_error = (
            "Timeout or error waiting for profiles to be online: "
            "Timeout waiting for user@hostc.zzz to come online"
        )
        reopen_calls = []

        class _TrackingPool:
            def open_all(self, relays): pass
            def contexts(self): return {}
            def reopen(self, relay): reopen_calls.append(relay)
            def close(self): pass

        def _probe(src, dst, count=1, interval=0.1, accounts_dir="",
                   timeout=10, relay_contexts=None):
            if src == "hostc.zzz":
                return _err(src, dst, timeout_error)
            return _ok(src, dst)

        monkeypatch.setattr("chatmail_prober.__main__.run_probe", _probe)

        from concurrent.futures import ThreadPoolExecutor
        executor = ThreadPoolExecutor(max_workers=1)
        pool = _TrackingPool()
        shutdown_event = threading.Event()

        try:
            run_round(
                ["hostc.zzz", "host.good"],
                _make_args(tmp_path, workers=1),
                executors=[executor],
                worker_pools=[pool],
                shutdown_event=shutdown_event,
            )
        finally:
            executor.shutdown(wait=False)

        assert reopen_calls == [], (
            f"pool.reopen() was called for {reopen_calls} on a timeout error — "
            "application-level errors must not trigger RPC context reopen."
        )


# ---------------------------------------------------------------------------
# TestRunRoundMetrics — probe matrix metrics after a mixed round
# ---------------------------------------------------------------------------

class TestRunRoundMetrics:
    """run_round must update probe_success and send_errors_total correctly
    for a mixed round containing both successful and failed probe pairs."""

    def test_mixed_round_updates_metrics(self, tmp_path, monkeypatch):
        def _probe(src, dst, count=1, interval=0.1, accounts_dir="",
                   timeout=10, relay_contexts=None):
            if src == "bad.example":
                return _err(src, dst, "connection refused")
            return _ok(src, dst)

        monkeypatch.setattr("chatmail_prober.__main__.run_probe", _probe)

        from concurrent.futures import ThreadPoolExecutor

        class _FakePool:
            def open_all(self, relays): pass
            def contexts(self): return {}
            def reopen(self, relay): pass
            def close(self): pass

        executors = [ThreadPoolExecutor(max_workers=1),
                     ThreadPoolExecutor(max_workers=1)]
        pools = [_FakePool(), _FakePool()]
        shutdown_event = threading.Event()

        try:
            run_round(
                ["good.example", "bad.example"],
                _make_args(tmp_path, workers=2),
                executors=executors,
                worker_pools=pools,
                shutdown_event=shutdown_event,
            )
        finally:
            for ex in executors:
                ex.shutdown(wait=False)

        # good.example -> good.example: success
        good_labels = dict(source="good.example", destination="good.example",
                           probe_type="self")
        assert metrics_mod.probe_success.labels(**good_labels)._value.get() == 1.0

        # bad.example -> bad.example: failure
        bad_labels = dict(source="bad.example", destination="bad.example",
                          probe_type="self")
        assert metrics_mod.probe_success.labels(**bad_labels)._value.get() == 0.0
        assert metrics_mod.send_errors_total.labels(**bad_labels)._value.get() >= 1.0
