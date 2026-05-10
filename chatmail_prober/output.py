"""Output modes: HTTP exporter and/or textfile for node_exporter."""

import contextlib
import sys
from pathlib import Path

from prometheus_client import REGISTRY, generate_latest, start_http_server

from .log_config import get_logger
from .metrics import CMPING_REGISTRY

log = get_logger(__name__)


def start_exporter_server(port: int) -> None:
    """Start the Prometheus HTTP metrics server.

    Registers our custom collectors into the default REGISTRY so the
    HTTP endpoint includes both cmping_* and process_* metrics.
    """
    for collector in list(CMPING_REGISTRY._names_to_collectors.values()):
        with contextlib.suppress(ValueError):  # already registered
            REGISTRY.register(collector)
    start_http_server(port)
    log.info("Prometheus HTTP server listening on :%d", port)


def print_metrics() -> None:
    """Print current metrics in Prometheus text format to stdout.

    Intended for interactive debugging with ``--once --print-metrics``.
    Output goes to stdout so it can be piped or grepped independently
    of the structured JSON log stream on stderr.
    """
    sys.stdout.write(generate_latest(CMPING_REGISTRY).decode("utf-8"))
    sys.stdout.flush()


def write_textfile(path: str) -> None:
    """Write current metrics to a .prom file atomically.

    Uses CMPING_REGISTRY (no process_* metrics) to avoid collisions
    with node_exporter's own process collectors.
    """
    target = Path(path)
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_bytes(generate_latest(CMPING_REGISTRY))
        tmp.chmod(0o644)
        tmp.replace(target)
        log.debug("Wrote metrics to %s", path)
    except OSError:
        log.exception("Failed to write textfile %s", path)
        with contextlib.suppress(OSError):
            tmp.unlink()
