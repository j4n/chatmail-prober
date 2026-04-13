"""RED tests for phase-tracking structured log events.

Phase 1 of the interactive feature set: AccountMaker and run_probe must emit
structured log events at each phase boundary so that the cli_summary renderer
can track timing and display a gocmping-style breakdown.

Expected events (in order):
  event="setup_start"   relay=<domain>
  event="setup_done"    relay=<domain>  elapsed_s=<float>
  event="join_start"    relay=<domain>
  event="join_done"     relay=<domain>  elapsed_s=<float>
"""
from __future__ import annotations

import json
import queue
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import structlog

from chatmail_prober.log_config import configure_logging
from chatmail_prober.prober import AccountMaker, run_probe


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _capture_json_events(fn):
    """Run fn() with JSON logging to stderr captured; return list of dicts."""
    import io
    import sys

    configure_logging(tty=False, level="DEBUG")
    buf = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = buf
    try:
        fn()
    finally:
        sys.stderr = old_stderr
        configure_logging(tty=False, level="DEBUG")

    events = []
    for line in buf.getvalue().splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

class _FakeRpc:
    """Minimal RPC stub that delivers a single IMAP_INBOX_IDLE event."""

    def __init__(self):
        self._queues: dict[int, queue.Queue] = {}

    def get_queue(self, account_id: int) -> queue.Queue:
        q = queue.Queue()
        q.put({"kind": "ImapInboxIdle"})
        self._queues[account_id] = q
        return q


class _FakeAccount:
    def __init__(self, account_id: int, addr: str, rpc: _FakeRpc):
        self.id = account_id
        self._addr = addr
        self._rpc = rpc
        self._config: dict[str, str] = {
            "configured_addr": addr,
            "addr": addr,
        }

    def get_config(self, key: str) -> str | None:
        return self._config.get(key)

    def set_config(self, key: str, value: str) -> None:
        self._config[key] = value

    def set_config_from_qr(self, qr_url: str) -> None:
        # Parse dcaccount:<domain> URL to derive a realistic configured_addr
        if qr_url.startswith("dcaccount:"):
            domain = qr_url[len("dcaccount:"):]
        else:
            domain = qr_url
        self._config["configured_addr"] = f"newuser@{domain}"
        self._config["addr"] = f"newuser@{domain}"

    def start_io(self) -> None:
        pass


class _FakeDC:
    def __init__(self, rpc: _FakeRpc):
        self._rpc = rpc
        self._accounts: list[_FakeAccount] = []
        self._next_id = 1

    def get_all_accounts(self) -> list[_FakeAccount]:
        return list(self._accounts)

    def add_account(self) -> _FakeAccount:
        acct = _FakeAccount(self._next_id, "", self._rpc)
        self._next_id += 1
        self._accounts.append(acct)
        return acct


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSetupPhaseEvents:
    """AccountMaker.get_relay_account must emit setup_start / setup_done events."""

    def test_setup_start_emitted(self):
        rpc = _FakeRpc()
        dc = _FakeDC(rpc)
        maker = AccountMaker(dc)

        with patch(
            "chatmail_prober.prober.create_qr_url",
            return_value="dcaccount:relay.example",
        ):
            events = _capture_json_events(
                lambda: maker.get_relay_account("relay.example")
            )

        event_names = [e["event"] for e in events]
        assert "setup_start" in event_names, f"setup_start missing from {event_names}"

    def test_setup_done_emitted_with_elapsed(self):
        rpc = _FakeRpc()
        dc = _FakeDC(rpc)
        maker = AccountMaker(dc)

        with patch(
            "chatmail_prober.prober.create_qr_url",
            return_value="dcaccount:relay.example",
        ):
            events = _capture_json_events(
                lambda: maker.get_relay_account("relay.example")
            )

        done_events = [e for e in events if e.get("event") == "setup_done"]
        assert done_events, "setup_done event missing"
        assert "elapsed_s" in done_events[0], "setup_done missing elapsed_s field"
        assert isinstance(done_events[0]["elapsed_s"], float)

    def test_setup_events_carry_relay_field(self):
        rpc = _FakeRpc()
        dc = _FakeDC(rpc)
        maker = AccountMaker(dc)

        with patch(
            "chatmail_prober.prober.create_qr_url",
            return_value="dcaccount:relay.example",
        ):
            events = _capture_json_events(
                lambda: maker.get_relay_account("relay.example")
            )

        phase_events = [e for e in events if e.get("event") in ("setup_start", "setup_done")]
        for ev in phase_events:
            assert ev.get("relay") == "relay.example", (
                f"relay field missing or wrong in {ev}"
            )

    def test_no_setup_events_when_account_reused(self):
        """When an account is already online, no setup_start/done should be emitted."""
        rpc = _FakeRpc()
        dc = _FakeDC(rpc)
        maker = AccountMaker(dc)

        # Pre-populate an online account for relay.example
        existing = _FakeAccount(99, "user@relay.example", rpc)
        maker.online.append(existing)

        events = _capture_json_events(
            lambda: maker.get_relay_account("relay.example")
        )

        phase_events = [e for e in events if e.get("event") in ("setup_start", "setup_done")]
        assert not phase_events, f"Unexpected setup events on reuse: {phase_events}"


class TestJoinPhaseEvents:
    """wait_account_online must emit join_start / join_done events."""

    def test_join_start_emitted(self):
        rpc = _FakeRpc()
        dc = _FakeDC(rpc)
        maker = AccountMaker(dc)
        acct = _FakeAccount(1, "user@relay.example", rpc)

        events = _capture_json_events(
            lambda: maker.wait_account_online(acct, timeout=5.0)
        )

        event_names = [e["event"] for e in events]
        assert "join_start" in event_names, f"join_start missing from {event_names}"

    def test_join_done_emitted_with_elapsed(self):
        rpc = _FakeRpc()
        dc = _FakeDC(rpc)
        maker = AccountMaker(dc)
        acct = _FakeAccount(1, "user@relay.example", rpc)

        events = _capture_json_events(
            lambda: maker.wait_account_online(acct, timeout=5.0)
        )

        done_events = [e for e in events if e.get("event") == "join_done"]
        assert done_events, "join_done event missing"
        assert "elapsed_s" in done_events[0]
        assert isinstance(done_events[0]["elapsed_s"], float)

    def test_join_events_carry_addr_field(self):
        rpc = _FakeRpc()
        dc = _FakeDC(rpc)
        maker = AccountMaker(dc)
        acct = _FakeAccount(1, "user@relay.example", rpc)

        events = _capture_json_events(
            lambda: maker.wait_account_online(acct, timeout=5.0)
        )

        join_events = [e for e in events if e.get("event") in ("join_start", "join_done")]
        for ev in join_events:
            assert "addr" in ev, f"addr field missing in {ev}"
