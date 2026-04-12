"""Tests for chatmail_prober.log_config — structlog pipeline configuration.

RED phase: all tests fail until log_config.py is written.
"""

import json
import logging
import sys

import pytest
import structlog


class TestConfigureLogging:
    """configure_logging() sets up the structlog pipeline."""

    def test_configure_logging_is_importable(self):
        from chatmail_prober.log_config import configure_logging  # noqa: F401

    def test_configure_logging_returns_none(self, tmp_path):
        from chatmail_prober.log_config import configure_logging
        result = configure_logging(tty=False)
        assert result is None

    def test_configure_logging_accepts_tty_flag(self, tmp_path):
        from chatmail_prober.log_config import configure_logging
        # Should not raise for either value
        configure_logging(tty=True)
        configure_logging(tty=False)

    def test_configure_logging_accepts_level(self):
        from chatmail_prober.log_config import configure_logging
        configure_logging(tty=False, level=logging.DEBUG)
        configure_logging(tty=False, level=logging.WARNING)


class TestJsonRenderer:
    """When tty=False the pipeline emits valid JSON to the stdlib handler."""

    def setup_method(self):
        from chatmail_prober.log_config import configure_logging
        configure_logging(tty=False, level=logging.DEBUG)

    def test_structlog_logger_emits_json(self, capsys):
        log = structlog.get_logger("test.json")
        log.info("hello_world", answer=42)
        captured = capsys.readouterr()
        # At least one line of output
        lines = [l for l in captured.err.splitlines() if l.strip()]
        assert lines, "expected output on stderr"
        parsed = json.loads(lines[-1])
        assert parsed["event"] == "hello_world"
        assert parsed["answer"] == 42

    def test_json_output_contains_level(self, capsys):
        log = structlog.get_logger("test.level")
        log.warning("check_level")
        captured = capsys.readouterr()
        lines = [l for l in captured.err.splitlines() if l.strip()]
        parsed = json.loads(lines[-1])
        assert parsed["level"] == "warn"

    def test_json_output_contains_timestamp(self, capsys):
        log = structlog.get_logger("test.ts")
        log.info("check_ts")
        captured = capsys.readouterr()
        lines = [l for l in captured.err.splitlines() if l.strip()]
        parsed = json.loads(lines[-1])
        assert "timestamp" in parsed

    def test_json_output_contains_logger_name(self, capsys):
        log = structlog.get_logger("test.name_check")
        log.info("check_name")
        captured = capsys.readouterr()
        lines = [l for l in captured.err.splitlines() if l.strip()]
        parsed = json.loads(lines[-1])
        assert "logger" in parsed
        assert parsed["logger"] == "test.name_check"

    def test_stdlib_logger_also_emits_json(self, capsys):
        """Standard library loggers (e.g. deltachat) must also produce JSON."""
        stdlib_log = logging.getLogger("test.stdlib")
        stdlib_log.warning("stdlib_message extra_key=value")
        captured = capsys.readouterr()
        lines = [l for l in captured.err.splitlines() if l.strip()]
        assert lines, "expected output on stderr from stdlib logger"
        # The stdlib output may be plain text wrapped in a JSON envelope
        # OR plain text — what matters is it is not silently dropped.
        assert "stdlib_message" in captured.err


class TestDevRenderer:
    """When tty=True the pipeline emits human-readable coloured text."""

    def setup_method(self):
        from chatmail_prober.log_config import configure_logging
        configure_logging(tty=True, level=logging.DEBUG)

    def test_dev_renderer_does_not_emit_json(self, capsys):
        log = structlog.get_logger("test.dev")
        log.info("dev_event", key="val")
        captured = capsys.readouterr()
        lines = [l for l in captured.err.splitlines() if l.strip()]
        assert lines, "expected output on stderr"
        # Should NOT be valid JSON
        with pytest.raises((json.JSONDecodeError, ValueError)):
            json.loads(lines[-1])

    def test_dev_renderer_contains_event_text(self, capsys):
        log = structlog.get_logger("test.dev2")
        log.info("my_dev_event")
        captured = capsys.readouterr()
        assert "my_dev_event" in captured.err


class TestGetLogger:
    """get_logger() is a thin alias for structlog.get_logger()."""

    def test_get_logger_is_importable(self):
        from chatmail_prober.log_config import get_logger  # noqa: F401

    def test_get_logger_returns_bound_logger(self):
        from chatmail_prober.log_config import get_logger
        log = get_logger("test.alias")
        assert hasattr(log, "info")
        assert hasattr(log, "warning")
        assert hasattr(log, "debug")
        assert hasattr(log, "error")

    def test_get_logger_name_is_preserved(self, capsys):
        from chatmail_prober.log_config import configure_logging, get_logger
        configure_logging(tty=False, level=logging.DEBUG)
        log = get_logger("my.module")
        log.info("named_event")
        captured = capsys.readouterr()
        lines = [l for l in captured.err.splitlines() if l.strip()]
        parsed = json.loads(lines[-1])
        assert parsed["logger"] == "my.module"
