"""Tests for _SupprRpcClosedFilter — structlog-safe shutdown noise suppression.

RED phase: tests verify that the filter correctly handles structlog's
ProcessorFormatter records (where getMessage() returns a JSON string or a
dict repr, not the original plain-text message) in addition to plain stdlib
log records.
"""

import json
import logging
import threading

import pytest

from chatmail_prober.__main__ import _SupprRpcClosedFilter


def _make_record(message: str, level=logging.ERROR) -> logging.LogRecord:
    record = logging.LogRecord(
        name="deltachat.rpc",
        level=level,
        pathname="",
        lineno=0,
        msg=message,
        args=(),
        exc_info=None,
    )
    return record


def _make_structlog_record(event: str, level=logging.ERROR) -> logging.LogRecord:
    """Simulate a record that has been processed by structlog's ProcessorFormatter.

    structlog wraps the original event dict into record.msg as a rendered
    string (JSON or ConsoleRenderer output).  The filter must handle both.
    """
    # JSON-rendered (production mode): msg is a JSON string
    payload = json.dumps({"event": event, "level": "error", "logger": "deltachat.rpc"})
    record = logging.LogRecord(
        name="deltachat.rpc",
        level=level,
        pathname="",
        lineno=0,
        msg=payload,
        args=(),
        exc_info=None,
    )
    return record


class TestSupprRpcClosedFilterPlainRecords:
    """Filter behaviour for plain stdlib log records."""

    def test_passes_rpc_closed_during_normal_operation(self):
        """RPC server closed errors must pass through when NOT shutting down."""
        event = threading.Event()  # not set
        f = _SupprRpcClosedFilter(event)
        record = _make_record("RPC server closed unexpectedly")
        assert f.filter(record) is True

    def test_suppresses_rpc_closed_during_shutdown(self):
        """RPC server closed errors must be suppressed when shutdown_event is set."""
        event = threading.Event()
        event.set()
        f = _SupprRpcClosedFilter(event)
        record = _make_record("RPC server closed unexpectedly")
        assert f.filter(record) is False

    def test_passes_other_errors_during_shutdown(self):
        """Non-RPC-closed errors must pass through even during shutdown."""
        event = threading.Event()
        event.set()
        f = _SupprRpcClosedFilter(event)
        record = _make_record("Connection reset by peer on mailchat.pl:993")
        assert f.filter(record) is True

    def test_passes_info_records_always(self):
        event = threading.Event()
        event.set()
        f = _SupprRpcClosedFilter(event)
        record = _make_record("some info message", level=logging.INFO)
        assert f.filter(record) is True


class TestSupprRpcClosedFilterStructlogRecords:
    """Filter behaviour for structlog ProcessorFormatter-rendered records.

    After structlog processes a record, record.msg is a rendered string
    (JSON in production mode).  The filter must inspect the rendered content,
    not just record.getMessage(), to correctly detect the 'RPC server closed'
    pattern.
    """

    def test_passes_structlog_rpc_closed_during_normal_operation(self):
        """Structlog-rendered RPC closed record passes when not shutting down."""
        event = threading.Event()
        f = _SupprRpcClosedFilter(event)
        record = _make_structlog_record("RPC server closed")
        assert f.filter(record) is True

    def test_suppresses_structlog_rpc_closed_during_shutdown(self):
        """Structlog-rendered RPC closed record is suppressed during shutdown."""
        event = threading.Event()
        event.set()
        f = _SupprRpcClosedFilter(event)
        record = _make_structlog_record("RPC server closed")
        assert f.filter(record) is False

    def test_passes_structlog_other_errors_during_shutdown(self):
        """Structlog-rendered non-RPC-closed record passes even during shutdown."""
        event = threading.Event()
        event.set()
        f = _SupprRpcClosedFilter(event)
        record = _make_structlog_record("Connection reset by peer")
        assert f.filter(record) is True

    def test_handles_non_json_structlog_output(self):
        """ConsoleRenderer output (non-JSON) must not crash the filter."""
        event = threading.Event()
        event.set()
        f = _SupprRpcClosedFilter(event)
        # ConsoleRenderer produces colourised text, not JSON
        record = _make_record(
            "2026-04-12T10:00:00Z [error    ] RPC server closed  [deltachat.rpc]"
        )
        # Must suppress — the pattern is present in the rendered text
        assert f.filter(record) is False
