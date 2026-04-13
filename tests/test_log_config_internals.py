"""Tests verifying that log_config internal helpers are importable at module level.

These tests act as a contract that _shorten_level, _extract_rpc_fields, and
_DynamicStderrHandler are module-level definitions (not closures buried inside
configure_logging()), making them independently testable and avoiding re-creation
on every configure_logging() call.

RED phase: all tests fail until the helpers are promoted to module scope.
"""

import json
import logging

import pytest

from chatmail_prober import log_config


class TestShortenLevelIsModuleLevel:
    """_shorten_level must be importable from log_config at module level."""

    def test_shorten_level_exists_at_module_level(self):
        assert hasattr(log_config, "_shorten_level"), (
            "_shorten_level must be a module-level function, not a closure"
        )

    def test_shorten_level_is_callable(self):
        assert callable(log_config._shorten_level)

    def test_shorten_level_renames_warning_to_warn(self):
        event_dict = {"level": "warning", "event": "test"}
        result = log_config._shorten_level(None, "warning", event_dict)
        assert result["level"] == "warn"

    def test_shorten_level_leaves_other_levels_unchanged(self):
        for level in ("info", "debug", "error", "critical"):
            event_dict = {"level": level, "event": "test"}
            result = log_config._shorten_level(None, level, event_dict)
            assert result["level"] == level, f"level {level!r} should be unchanged"

    def test_shorten_level_is_pure_no_side_effects(self):
        """Calling _shorten_level must not modify the original dict in-place
        in a way that breaks re-use (it may modify in-place, but must return
        the same dict)."""
        event_dict = {"level": "warning", "event": "test"}
        result = log_config._shorten_level(None, "warning", event_dict)
        assert result is event_dict  # structlog processors return the same dict


class TestExtractRpcFieldsIsModuleLevel:
    """_extract_rpc_fields must be importable from log_config at module level."""

    def test_extract_rpc_fields_exists_at_module_level(self):
        assert hasattr(log_config, "_extract_rpc_fields"), (
            "_extract_rpc_fields must be a module-level function, not a closure"
        )

    def test_extract_rpc_fields_is_callable(self):
        assert callable(log_config._extract_rpc_fields)

    def test_extract_rpc_fields_lifts_version_from_record(self):
        record = logging.LogRecord(
            name="root", level=logging.INFO,
            pathname="", lineno=0, msg="rpc_ready", args=(), exc_info=None,
        )
        record._rpc_version = "v2.48.0"
        event_dict = {"_record": record, "event": "rpc_ready"}
        result = log_config._extract_rpc_fields(None, "info", event_dict)
        assert result.get("version") == "v2.48.0"

    def test_extract_rpc_fields_no_op_when_no_record(self):
        event_dict = {"event": "something_else"}
        result = log_config._extract_rpc_fields(None, "info", event_dict)
        assert "version" not in result

    def test_extract_rpc_fields_no_op_when_no_rpc_version(self):
        record = logging.LogRecord(
            name="root", level=logging.INFO,
            pathname="", lineno=0, msg="other", args=(), exc_info=None,
        )
        event_dict = {"_record": record, "event": "other"}
        result = log_config._extract_rpc_fields(None, "info", event_dict)
        assert "version" not in result


class TestDynamicStderrHandlerIsModuleLevel:
    """_DynamicStderrHandler must be importable from log_config at module level."""

    def test_dynamic_stderr_handler_exists_at_module_level(self):
        assert hasattr(log_config, "_DynamicStderrHandler"), (
            "_DynamicStderrHandler must be a module-level class, not defined inside configure_logging()"
        )

    def test_dynamic_stderr_handler_is_a_class(self):
        import inspect
        assert inspect.isclass(log_config._DynamicStderrHandler)

    def test_dynamic_stderr_handler_stream_resolves_sys_stderr(self):
        import sys
        handler = log_config._DynamicStderrHandler()
        assert handler.stream is sys.stderr

    def test_dynamic_stderr_handler_stream_setter_is_noop(self):
        """Setting .stream must not change what the handler resolves to."""
        import sys
        handler = log_config._DynamicStderrHandler()
        handler.stream = "something_else"  # must be silently ignored
        assert handler.stream is sys.stderr
