"""Output modes: HTTP exporter and/or textfile for node_exporter."""

import logging
import os
import tempfile

from prometheus_client import REGISTRY, start_http_server, generate_latest

log = logging.getLogger(__name__)


def start_exporter_server(port):
    """Start the Prometheus HTTP metrics server."""
    start_http_server(port)
    log.info("Prometheus HTTP server listening on :%d", port)


def write_textfile(path):
    """Write current metrics to a .prom file atomically.

    Uses write-to-temp-then-rename to avoid Prometheus scraping a partial file.
    """
    output = generate_latest(REGISTRY)
    dirpath = os.path.dirname(path)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dirpath, suffix=".tmp")
        try:
            os.write(fd, output)
        finally:
            os.close(fd)
        os.rename(tmp_path, path)
        log.debug("Wrote metrics to %s", path)
    except OSError:
        log.exception("Failed to write textfile %s", path)
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
