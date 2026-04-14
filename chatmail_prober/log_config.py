"""Structured logging configuration for chatmail-prober.

Always renders colourised key=value output via structlog's ConsoleRenderer.
Third-party stdlib loggers are captured and formatted consistently.
"""

import logging
import re
import sys

import structlog


_RPC_READY_RE = re.compile(r"RPC server ready\.")


class _RpcReadyFilter(logging.Filter):
    """Downgrade upstream 'RPC server ready' INFO records to DEBUG."""

    def __init__(self, effective_level: int = logging.INFO) -> None:
        super().__init__()
        self.effective_level = effective_level

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if not _RPC_READY_RE.search(record.getMessage()):
            return True
        if self.effective_level > logging.DEBUG:
            return False  # drop: would be invisible at this level anyway
        record.levelno = logging.DEBUG
        record.levelname = "DEBUG"
        return True


def configure_logging(level: int | str = logging.INFO) -> None:
    """Configure structlog with ConsoleRenderer and the stdlib root logger."""
    if isinstance(level, str):
        level = logging.getLevelName(level.upper())

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
    ]

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(),
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    handler.addFilter(_RpcReadyFilter(effective_level=level))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(min(level, logging.WARNING))
    logging.getLogger("chatmail_prober").setLevel(level)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog BoundLogger bound to *name*."""
    return structlog.get_logger(name)
