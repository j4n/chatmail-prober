#!/usr/bin/env python3
"""Probe the public TURN fallback and write a node_exporter textfile.

Thin wrapper around chatmail_prober.turn_parse.run_uclient so the flag
set, regexes, and ms->seconds normalization stay shared with the
in-process prober.

Usage:
    python scripts/turn_textfile_collector.py [collector_dir]

Default collector_dir is /var/lib/prometheus/node-exporter; output file
is <collector_dir>/turn_health.prom (rendered atomically).
"""

from __future__ import annotations

import os
import sys
import tempfile

from prometheus_client import CollectorRegistry, Gauge, generate_latest

from chatmail_prober.turn import FALLBACK_TURN
from chatmail_prober.turn_parse import run_uclient

DEFAULT_COLLECTOR_DIR = "/var/lib/prometheus/node-exporter"


def build_registry() -> CollectorRegistry:
    """Run uclient against the fallback and return a populated registry."""
    host, port, user, pw = FALLBACK_TURN
    run = run_uclient(host, port, user, pw, timeout=30)
    if run.error == "binary-missing":
        sys.exit("turnutils_uclient not found")

    reg = CollectorRegistry()
    labels = ["target"]
    base = {"target": host}

    Gauge("turn_up", "Whether the TURN relay health check succeeded",
          labels, registry=reg).labels(**base).set(1 if run.ok else 0)

    if not run.ok:
        return reg

    scalars = (
        ("turn_connect_time_seconds",  "Time to establish the TURN connection", run.connect_s),
        ("turn_transmit_time_seconds", "Total relay test duration",             run.transmit_s),
    )
    for name, help_text, value in scalars:
        if value is not None:
            Gauge(name, help_text, labels, registry=reg).labels(**base).set(value)
    return reg


def write_textfile(collector_dir: str) -> None:
    if not os.path.isdir(collector_dir) or not os.access(collector_dir, os.W_OK):
        sys.exit(
            f"Error: collector directory '{collector_dir}' does not exist "
            "or is not writable"
        )
    payload = generate_latest(build_registry())
    fd, tmp = tempfile.mkstemp(dir=collector_dir)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.chmod(tmp, 0o644)
        os.replace(tmp, os.path.join(collector_dir, "turn_health.prom"))
    except Exception:
        os.unlink(tmp)
        raise


if __name__ == "__main__":
    write_textfile(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_COLLECTOR_DIR)
