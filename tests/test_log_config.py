"""Tests for chatmail_prober.log_config -- structlog pipeline configuration."""

import logging

import structlog

from chatmail_prober.log_config import configure_logging


class TestConfigureLogging:
    def test_sets_app_logger_level(self):
        configure_logging(level=logging.DEBUG)
        assert logging.getLogger("chatmail_prober").level == logging.DEBUG
        configure_logging(level=logging.WARNING)
        assert logging.getLogger("chatmail_prober").level == logging.WARNING

    def test_repeated_calls_dont_duplicate_handlers(self):
        configure_logging()
        configure_logging()
        configure_logging()
        assert len(logging.getLogger().handlers) == 1

    def test_console_output_contains_event(self, capsys):
        configure_logging(level=logging.DEBUG)
        log = structlog.get_logger("test.console")
        log.info("hello_world", answer=42)
        captured = capsys.readouterr()
        assert "hello_world" in captured.err
        assert "42" in captured.err

    def test_stdlib_logger_not_silently_dropped(self, capsys):
        configure_logging(level=logging.DEBUG)
        stdlib_log = logging.getLogger("test.stdlib")
        stdlib_log.setLevel(logging.DEBUG)
        stdlib_log.warning("stdlib_message")
        captured = capsys.readouterr()
        assert "stdlib_message" in captured.err

    def test_rpc_ready_dropped_at_info_level(self):
        configure_logging(level=logging.INFO)
        handler = logging.getLogger().handlers[0]
        record = logging.LogRecord(
            "deltachat", logging.INFO, "", 0,
            "RPC server ready. Core version: v1.2.3", (), None,
        )
        assert handler.filters[0].filter(record) is False

    def test_rpc_ready_visible_at_debug_level(self):
        configure_logging(level=logging.DEBUG)
        handler = logging.getLogger().handlers[0]
        record = logging.LogRecord(
            "deltachat", logging.INFO, "", 0,
            "RPC server ready. Core version: v1.2.3", (), None,
        )
        assert handler.filters[0].filter(record) is True
        assert record.levelno == logging.DEBUG
