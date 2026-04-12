"""Tests for the _RpcReadyFilter that normalises the upstream
'RPC server ready' stdlib log record into a structured DEBUG event.

RED phase: all tests fail until the filter is wired into configure_logging().
"""

import json
import logging

import pytest

from chatmail_prober.log_config import configure_logging


class TestRpcReadyFilter:
    """The root handler must rewrite 'RPC server ready' records."""

    def setup_method(self):
        configure_logging(tty=False, level=logging.DEBUG)

    # ------------------------------------------------------------------
    # Level downgrade
    # ------------------------------------------------------------------

    def test_rpc_ready_emitted_at_debug_not_info(self, capsys):
        """'RPC server ready' must be downgraded from INFO to DEBUG."""
        root_log = logging.getLogger()
        root_log.info("RPC server ready. Core version: v2.48.0")
        captured = capsys.readouterr()
        lines = [l for l in captured.err.splitlines() if l.strip()]
        assert lines, "expected output"
        parsed = json.loads(lines[-1])
        assert parsed["level"] == "debug", (
            f"expected level=debug, got level={parsed['level']!r}"
        )

    def test_rpc_ready_not_visible_at_info_level(self, capsys):
        """When the configured level is INFO, rpc_ready must be silent."""
        configure_logging(tty=False, level=logging.INFO)
        root_log = logging.getLogger()
        root_log.info("RPC server ready. Core version: v2.48.0")
        captured = capsys.readouterr()
        rpc_lines = [
            l for l in captured.err.splitlines()
            if "rpc_ready" in l or "RPC server ready" in l
        ]
        assert not rpc_lines, (
            f"rpc_ready should be invisible at INFO level, got: {rpc_lines}"
        )

    # ------------------------------------------------------------------
    # Structured fields
    # ------------------------------------------------------------------

    def test_rpc_ready_has_event_field(self, capsys):
        """The rewritten record must carry event='rpc_ready'."""
        root_log = logging.getLogger()
        root_log.info("RPC server ready. Core version: v2.48.0")
        captured = capsys.readouterr()
        lines = [l for l in captured.err.splitlines() if l.strip()]
        parsed = json.loads(lines[-1])
        assert parsed.get("event") == "rpc_ready", (
            f"expected event=rpc_ready, got {parsed.get('event')!r}"
        )

    def test_rpc_ready_has_version_field(self, capsys):
        """The rewritten record must carry version='v2.48.0'."""
        root_log = logging.getLogger()
        root_log.info("RPC server ready. Core version: v2.48.0")
        captured = capsys.readouterr()
        lines = [l for l in captured.err.splitlines() if l.strip()]
        parsed = json.loads(lines[-1])
        assert parsed.get("version") == "v2.48.0", (
            f"expected version=v2.48.0, got {parsed.get('version')!r}"
        )

    def test_rpc_ready_version_extracted_dynamically(self, capsys):
        """Version must be extracted from the message, not hardcoded."""
        root_log = logging.getLogger()
        root_log.info("RPC server ready. Core version: v3.1.0")
        captured = capsys.readouterr()
        lines = [l for l in captured.err.splitlines() if l.strip()]
        parsed = json.loads(lines[-1])
        assert parsed.get("version") == "v3.1.0"

    # ------------------------------------------------------------------
    # Non-matching records pass through unchanged
    # ------------------------------------------------------------------

    def test_unrelated_info_record_passes_through(self, capsys):
        """Non-matching INFO records must not be touched."""
        root_log = logging.getLogger()
        root_log.info("some other message")
        captured = capsys.readouterr()
        lines = [l for l in captured.err.splitlines() if l.strip()]
        assert lines, "expected output"
        parsed = json.loads(lines[-1])
        assert parsed["level"] == "info"
        assert "some other message" in parsed.get("event", "")
