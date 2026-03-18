"""Wraps cmping's perform_ping() for use by the exporter."""

import argparse
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

# Silence cmping's own logging output.  cmping now uses the "cmping"
# logger for all progress/debug messages and print() only for primary
# CLI output (RTT lines, statistics).  Setting its logger to WARNING
# suppresses the info/debug chatter without the old builtins.print
# monkey-patch.
logging.getLogger("cmping").setLevel(logging.WARNING)


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
    count: int = 5,
    interval: float = 0.1,
    accounts_dir: str | Path = "~/.cache/chatmail-prober/worker-0",
    timeout: float = 60.0,
    verbose: int = 0,
) -> ProbeResult:
    """Run a single cmping probe between two relays.

    accounts_dir should be a per-worker directory so the worker's single
    thread accesses it sequentially, avoiding deltachat-rpc-server DB locks.

    verbose levels (passed through to cmping):
      0 = silent (default)
      1 = errors and basic stats
      2 = full addresses in stats
      3 = all deltachat events (very noisy)
    """
    accounts_dir = Path(accounts_dir).expanduser()

    # cmping verbose level: -vv (prober) -> verbose=1 (cmping errors),
    # -vvv (prober) -> verbose=3 (cmping full events).
    cmping_verbose = 0
    if verbose >= 3:
        cmping_verbose = 3
    elif verbose >= 2:
        cmping_verbose = 1

    args = argparse.Namespace(
        relay1=source,
        relay2=dest,
        count=count,
        interval=interval,
        verbose=cmping_verbose,
        numrecipients=1,
        reset=False,
    )

    try:
        pinger = perform_ping(args, accounts_dir=accounts_dir, timeout=timeout)
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
