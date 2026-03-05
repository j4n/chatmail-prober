"""Wraps cmping's perform_ping() for use by the exporter."""

import argparse
import contextlib
import io
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from cmping import CMPingError, perform_ping


def _ensure_venv_on_path():
    """Add the venv's bin dir to PATH so deltachat-rpc-server is found."""
    bin_dir = str(Path(sys.executable).parent)
    path = os.environ.get("PATH", "")
    if bin_dir not in path.split(os.pathsep):
        os.environ["PATH"] = bin_dir + os.pathsep + path


_ensure_venv_on_path()

log = logging.getLogger(__name__)


@contextlib.contextmanager
def _suppress_stdout():
    """Suppress all stdout output, including from child threads.

    contextlib.redirect_stdout only replaces sys.stdout, which doesn't
    catch prints from threads that cached the old stdout object. This
    redirects at the file descriptor level so all writes to fd 1 go
    to /dev/null.
    """
    devnull = os.open(os.devnull, os.O_WRONLY)
    old_fd = os.dup(1)
    try:
        os.dup2(devnull, 1)
        sys.stdout = io.TextIOWrapper(os.fdopen(1, "wb", 0))
        yield
    finally:
        sys.stdout.flush()
        os.dup2(old_fd, 1)
        sys.stdout = io.TextIOWrapper(os.fdopen(1, "wb", 0))
        os.close(old_fd)
        os.close(devnull)


@dataclass
class ProbeResult:
    source: str
    destination: str
    sent: int = 0
    received: int = 0
    loss: float = 100.0
    rtts_ms: list[float] = field(default_factory=list)
    account_setup_time: float = 0.0
    group_join_time: float = 0.0
    message_time: float = 0.0
    error: str | None = None


def run_probe(
    source: str,
    dest: str,
    count: int = 10,
    interval: float = 1.1,
    cache_dir: str | Path = "~/.cache/chatmail-prober",
) -> ProbeResult:
    """Run a single cmping probe between two relays.

    Uses a per-pair accounts directory so multiple probes can run
    concurrently without hitting deltachat-rpc-server's DB lock.
    """
    pair_dir = Path(cache_dir).expanduser() / f"{source}--{dest}"

    args = argparse.Namespace(
        relay1=source,
        relay2=dest,
        count=count,
        interval=interval,
        verbose=0,
        numrecipients=1,
        reset=False,
    )

    try:
        with _suppress_stdout():
            pinger = perform_ping(args, accounts_dir=pair_dir)
        return ProbeResult(
            source=source,
            destination=dest,
            sent=pinger.sent,
            received=pinger.received,
            loss=pinger.loss,
            rtts_ms=[rtt for _, rtt, _ in pinger.results],
            account_setup_time=pinger.account_setup_time,
            group_join_time=pinger.group_join_time,
            message_time=pinger.message_time,
        )
    except CMPingError as e:
        log.warning("Probe %s -> %s failed: %s", source, dest, e)
        return ProbeResult(source=source, destination=dest, error=str(e))
    except Exception as e:
        log.exception("Unexpected error probing %s -> %s", source, dest)
        return ProbeResult(source=source, destination=dest, error=str(e))
