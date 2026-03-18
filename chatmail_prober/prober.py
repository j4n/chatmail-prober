"""Wraps cmping's perform_ping() for use by the exporter."""

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from cmping import CMPingError, RelayContext, perform_ping, perform_ping_with_contexts


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


def _cmping_verbose(verbose):
    """Map prober verbosity to cmping verbosity level."""
    if verbose >= 3:
        return 3
    elif verbose >= 2:
        return 1
    return 0


class RelayPool:
    """Manages one RelayContext per relay domain.

    Contexts are opened once and shared across all probes in a round.
    Uses per-relay accounts dirs (cache_dir/relay) instead of per-worker.
    """

    def __init__(self, cache_dir, verbose=0):
        self._cache_dir = Path(cache_dir)
        self._verbose = verbose
        self._contexts = {}

    def open_all(self, relays):
        """Pre-open contexts for all relays.  Fails fast on errors."""
        for relay in relays:
            if relay not in self._contexts:
                ctx = RelayContext(relay, self._cache_dir / relay,
                                  verbose=self._verbose)
                ctx.open()
                self._contexts[relay] = ctx

    def contexts(self):
        """Return relay -> RelayContext dict (read-only after open_all)."""
        return dict(self._contexts)

    def close(self):
        """Close all managed contexts."""
        for ctx in self._contexts.values():
            ctx.close()
        self._contexts.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


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
    relay_contexts: dict | None = None,
) -> ProbeResult:
    """Run a single cmping probe between two relays.

    When relay_contexts is provided (dict of relay -> open RelayContext),
    uses perform_ping_with_contexts() for shared RPC connections.
    Otherwise falls back to perform_ping() with accounts_dir.

    verbose levels (passed through to cmping):
      0 = silent (default)
      1 = errors and basic stats
      2 = full addresses in stats
      3 = all deltachat events (very noisy)
    """
    cmping_verbose = _cmping_verbose(verbose)

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
        if relay_contexts is not None:
            pinger = perform_ping_with_contexts(args, relay_contexts,
                                                timeout=timeout)
        else:
            accounts_dir = Path(accounts_dir).expanduser()
            pinger = perform_ping(args, accounts_dir=accounts_dir,
                                  timeout=timeout)
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
