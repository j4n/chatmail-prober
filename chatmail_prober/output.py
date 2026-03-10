"""Output modes: HTTP exporter and/or textfile for node_exporter."""

import logging
import os
import tempfile

from prometheus_client import REGISTRY, start_http_server, generate_latest

from .metrics import CMPING_REGISTRY

log = logging.getLogger(__name__)


def start_exporter_server(port):
    """Start the Prometheus HTTP metrics server.

    Registers our custom collectors into the default REGISTRY so the
    HTTP endpoint includes both cmping_* and process_* metrics.
    """
    for collector in list(CMPING_REGISTRY._names_to_collectors.values()):
        try:
            REGISTRY.register(collector)
        except ValueError:
            pass  # already registered
    start_http_server(port)
    log.info("Prometheus HTTP server listening on :%d", port)


def write_textfile(path):
    """Write current metrics to a .prom file atomically.

    Uses CMPING_REGISTRY (no process_* metrics) to avoid collisions
    with node_exporter's own process collectors.
    """
    output = generate_latest(CMPING_REGISTRY)
    dirpath = os.path.dirname(path)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dirpath, suffix=".tmp")
        try:
            os.write(fd, output)
            os.fchmod(fd, 0o644)
        finally:
            os.close(fd)
        os.rename(tmp_path, path)
        log.debug("Wrote metrics to %s", path)
    except OSError:
        log.exception("Failed to write textfile %s", path)
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
