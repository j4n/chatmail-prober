"""Tests for RelayPool: shared rpc-server lifecycle, relay set, duck-typing.

RelayPool owns one deltachat-rpc-server process per worker thread and
serves multiple relay domains from it. Tests here mock chatmail_prober.pool.Rpc
so no real subprocess is spawned.
"""
from __future__ import annotations

from unittest.mock import patch

from chatmail_prober.probe import RelayPool


class TestRelayPool:
    """Only invariants worth asserting; trivial constructor-mock checks dropped."""

    @patch("chatmail_prober.pool.Rpc")
    def test_open_all_deduplicates_rpc_and_unions_relays(self, MockRpc, tmp_path):
        pool = RelayPool(tmp_path)
        pool.open_all(["a.test", "b.test"])
        pool.open_all(["a.test", "c.test"])
        MockRpc.assert_called_once()
        assert set(pool.contexts().keys()) == {"a.test", "b.test", "c.test"}

    @patch("chatmail_prober.pool.Rpc")
    def test_reopen_restarts_rpc_and_keeps_relays(self, MockRpc, tmp_path):
        pool = RelayPool(tmp_path)
        pool.open_all(["a.test"])
        pool.reopen()
        assert MockRpc.call_count == 2
        assert "a.test" in pool.contexts()

    @patch("chatmail_prober.pool.Rpc")
    def test_prune_forgets_relays(self, MockRpc, tmp_path):
        pool = RelayPool(tmp_path)
        pool.open_all(["a.test", "b.test", "c.test"])
        pool.prune(["a.test", "c.test"])
        assert set(pool.contexts().keys()) == {"a.test", "c.test"}

    @patch("chatmail_prober.pool.Rpc")
    def test_contexts_duck_type_for_perform_direct_ping(self, MockRpc, tmp_path):
        """Each context yielded by the pool must expose .maker (consumed by _perform_direct_ping)."""
        pool = RelayPool(tmp_path)
        pool.open_all(["a.test"])
        ctx = pool.contexts()["a.test"]
        assert ctx.maker is pool.maker is not None
