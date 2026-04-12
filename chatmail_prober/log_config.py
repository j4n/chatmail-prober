"""Structured logging configuration for chatmail-prober.

Call configure_logging() once at startup (in main()) before any log calls.
All modules then use get_logger(__name__) instead of logging.getLogger(__name__).

Two rendering modes:
  tty=True  — structlog.dev.ConsoleRenderer: colourised human-readable text
              (used when stderr is a real terminal)
  tty=False — structlog.processors.JSONRenderer: one JSON object per line
              (used under systemd/journald, file redirection, or in CI)

Third-party libraries (deltachat-rpc-client, prometheus_client, etc.) continue
to use the standard library logging module.  Their records are captured by a
stdlib handler that re-formats them consistently alongside structlog output.
"""

import logging
import sys

import structlog


def configure_logging(
    tty: bool | None = None,
    level: int = logging.INFO,
) -> None:
    """Configure structlog and the stdlib root logger.

    Args:
        tty:   Force renderer selection.  None (default) auto-detects via
               sys.stderr.isatty().  True → ConsoleRenderer, False → JSON.
        level: Minimum log level for the chatmail_prober logger and the
               structlog pipeline.  Root logger stays at WARNING to suppress
               noisy third-party libraries.
    """
    if tty is None:
        tty = sys.stderr.isatty()

    # ------------------------------------------------------------------
    # Shared processors: run for every log record regardless of renderer
    # ------------------------------------------------------------------
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
    ]

    if tty:
        # Human-readable colourised output for interactive terminals
        renderer = structlog.dev.ConsoleRenderer()
    else:
        # Machine-readable JSON for systemd/journald, file sinks, CI
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=shared_processors + [
            # PrepareForRenderingProcessor must be last before the renderer
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    # ------------------------------------------------------------------
    # Stdlib handler: captures both structlog-routed records and records
    # from third-party libraries, formats them with the same renderer.
    # ------------------------------------------------------------------
    formatter = structlog.stdlib.ProcessorFormatter(
        # foreign_pre_chain: applied to records that did NOT come through
        # structlog (i.e. plain stdlib loggers from third-party code).
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    # Use a StreamHandler that resolves sys.stderr at emit-time, not at
    # construction-time.  This lets pytest's capsys fixture intercept output
    # during tests (pytest swaps sys.stderr after handler creation).
    class _DynamicStderrHandler(logging.StreamHandler):
        @property
        def stream(self):
            return sys.stderr
        @stream.setter
        def stream(self, value):
            pass  # ignore the StreamHandler.__init__ assignment

    handler = _DynamicStderrHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Remove any handlers added by a previous configure_logging() call
    # (important in tests where configure_logging is called repeatedly).
    root.handlers.clear()
    root.addHandler(handler)
    # Root logger must be at least as permissive as the requested level so
    # that structlog loggers under any namespace (including test namespaces)
    # can emit records.  Third-party noise is suppressed by keeping their
    # individual loggers at WARNING, not by raising the root level.
    root.setLevel(min(level, logging.WARNING))

    # Our own logger hierarchy gets the requested level.
    logging.getLogger("chatmail_prober").setLevel(level)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog BoundLogger bound to *name*.

    Drop-in replacement for logging.getLogger(__name__) in all
    chatmail_prober modules.
    """
    return structlog.get_logger(name)
