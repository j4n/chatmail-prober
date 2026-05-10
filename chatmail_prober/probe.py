"""Probe orchestration: a single source -> dest measurement.

Owns _perform_direct_ping (sets up a 1:1 chat across two relay
contexts) and run_probe (the public entry point that wraps the result
in a ProbeResult). Per-relay RPC, account creation, the wire-protocol
Pinger, and the worker-shared RelayPool live in their own modules.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from deltachat_rpc_client import Rpc  # noqa: F401

# Back-compat re-exports: AccountMaker / RelayPool / Rpc / qr helpers used
# to live here, and existing tests + scripts still patch / import them
# under chatmail_prober.probe.<Name>.
from chatmail_prober.accounts import (  # noqa: F401
    AccountMaker,
    create_qr_url,
    generate_credentials,
    is_ip_address,
)
from chatmail_prober.errors import (
    _FAILURE_CATEGORY_MAP,
    _FATAL_CATEGORIES,
    PingError,
    _classify_error,
)
from chatmail_prober.log_config import get_logger
from chatmail_prober.pinger import Pinger
from chatmail_prober.pool import RelayPool  # noqa: F401
from chatmail_prober.rpc import RelayContext

log = get_logger(__name__)


__all__ = [
    "AccountMaker",
    "PingError",
    "Pinger",
    "ProbeResult",
    "RelayContext",
    "RelayPool",
    "Rpc",
    "_FAILURE_CATEGORY_MAP",
    "_FATAL_CATEGORIES",
    "_classify_error",
    "_perform_direct_ping",
    "create_qr_url",
    "generate_credentials",
    "is_ip_address",
    "run_probe",
]


# ---------------------------------------------------------------------------
# _perform_direct_ping -- orchestrates a single 1:1 probe
# ---------------------------------------------------------------------------

def _perform_direct_ping(
    relay_contexts: Mapping[str, "RelayContext | RelayPool"],
    source: str,
    dest: str,
    count: int,
    interval: float,
    timeout: float,
) -> Pinger:
    """Run a direct 1:1 ping between two relays using shared contexts."""
    sender_ctx = relay_contexts[source]
    receiver_ctx = relay_contexts[dest]
    if sender_ctx.maker is None:
        raise PingError(f"Relay context for {source} is closed (being reopened)")
    if receiver_ctx.maker is None:
        raise PingError(f"Relay context for {dest} is closed (being reopened)")
    sender_maker = sender_ctx.maker
    receiver_maker = receiver_ctx.maker

    account_setup_start = time.time()

    try:
        sender, sender_was_online = sender_maker.get_relay_account(source)
    except Exception as e:
        raise PingError(
            f"Failed to setup sender profile on {source}: {type(e).__name__}: {e}"
        ) from e

    # For self-loops (source==dest), exclude sender so we get a different account.
    exclude = (sender,) if source == dest else None
    try:
        receiver, receiver_was_online = receiver_maker.get_relay_account(dest, exclude=exclude)
    except Exception as e:
        raise PingError(
            f"Failed to setup receiver profile on {dest}: {type(e).__name__}: {e}"
        ) from e

    # Only wait for accounts that were just brought online; already-online
    # accounts had their IMAP_INBOX_IDLE event consumed on a previous probe.
    needs_wait = []
    if not sender_was_online:
        needs_wait.append((sender_maker, sender))
    if not receiver_was_online:
        needs_wait.append((receiver_maker, receiver))

    if needs_wait:
        # Use a small executor so exceptions propagate cleanly via future.result()
        # instead of being collected in a shared list from daemon threads.
        with ThreadPoolExecutor(max_workers=len(needs_wait)) as pool:
            futures = [
                pool.submit(maker.wait_account_online, acct, timeout)
                for maker, acct in needs_wait
            ]
        # Executor.__exit__ waits for all futures; re-raise the first error.
        first_exc = next(
            (f.exception() for f in futures if f.exception() is not None), None
        )
        if first_exc is not None:
            raise PingError(
                f"Timeout or error waiting for profiles to be online: {first_exc}"
            ) from first_exc

    account_setup_time = time.time() - account_setup_start

    # Message phase
    message_start = time.time()
    pinger = Pinger(sender, receiver, count, interval, timeout=timeout)
    with pinger:
        for seq, ms_duration in pinger.receive():
            pinger.results.append((seq, ms_duration))
    message_time = time.time() - message_start

    pinger.account_setup_time = account_setup_time
    pinger.message_time = message_time
    return pinger


# ---------------------------------------------------------------------------
# ProbeResult / run_probe -- public API for the exporter
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    source: str
    destination: str
    sent: int = 0
    received: int = 0
    loss: float = 100.0  # used in log output and CLI; metrics recomputes from sent/received
    rtts_ms: list[float] = field(default_factory=list)
    account_setup_time: float = 0.0
    message_time: float = 0.0
    error: str | None = None
    failure_category: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.failure_category = _classify_error(self.error)

    # Latency distribution (all values in milliseconds, None if no samples).
    # All percentile reads share one quantiles() pass via _stats. Single-sample
    # case returns the lone value for every percentile and 0.0 for mdev.

    @cached_property
    def _stats(self) -> dict[str, float | None]:
        rtts = self.rtts_ms
        if not rtts:
            return dict.fromkeys(("avg", "p50", "p90", "p95", "p99", "mdev"))
        if len(rtts) < 2:
            v = rtts[0]
            return {"avg": v, "p50": v, "p90": v, "p95": v, "p99": v, "mdev": 0.0}
        qs = statistics.quantiles(rtts, n=100, method="inclusive")
        return {
            "avg":  statistics.mean(rtts),
            "p50":  statistics.median(rtts),
            "p90":  qs[89],
            "p95":  qs[94],
            "p99":  qs[98],
            "mdev": statistics.stdev(rtts),
        }

    @property
    def avg_ms(self) -> float | None: return self._stats["avg"]
    @property
    def p50_ms(self) -> float | None: return self._stats["p50"]
    @property
    def p90_ms(self) -> float | None: return self._stats["p90"]
    @property
    def p95_ms(self) -> float | None: return self._stats["p95"]
    @property
    def p99_ms(self) -> float | None: return self._stats["p99"]
    @property
    def mdev_ms(self) -> float | None: return self._stats["mdev"]


def run_probe(
    source: str,
    dest: str,
    count: int = 5,
    interval: float = 0.1,
    accounts_dir: str | Path = "~/.cache/chatmail-prober/worker-0",
    timeout: float = 60.0,
    relay_contexts: Mapping[str, "RelayContext"] | Mapping[str, "RelayPool"] | None = None,
) -> ProbeResult:
    """Run a single direct-ping probe between two relays.

    When relay_contexts is provided (dict of relay -> open RelayContext),
    uses shared RPC connections.  Otherwise creates temporary contexts.
    """
    try:
        if relay_contexts is not None:
            pinger = _perform_direct_ping(relay_contexts, source, dest,
                                          count, interval, timeout)
        else:
            # Create temporary contexts for standalone use
            accounts_dir = Path(accounts_dir).expanduser()
            with ExitStack() as stack:
                tmp_contexts = {
                    r: stack.enter_context(RelayContext(r, accounts_dir / r))
                    for r in {source, dest}
                }
                pinger = _perform_direct_ping(tmp_contexts, source, dest,
                                              count, interval, timeout)

        return ProbeResult(
            source=source,
            destination=dest,
            sent=pinger.sent,
            received=pinger.received,
            loss=pinger.loss,
            rtts_ms=[rtt for _, rtt in pinger.results],
            account_setup_time=pinger.account_setup_time,
            message_time=pinger.message_time,
        )
    except PingError as e:
        result = ProbeResult(source=source, destination=dest, error=str(e))
        log.debug("probe_failed", src=source, dst=dest,
                  failure_category=result.failure_category, error=str(e))
        return result
    except Exception as e:
        result = ProbeResult(source=source, destination=dest, error=str(e))
        log.exception("probe_failed", src=source, dst=dest,
                      failure_category=result.failure_category, error=str(e))
        return result
