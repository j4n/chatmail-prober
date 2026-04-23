"""Probe orchestration: alive checks, probe rounds, and relay scanning."""

import gc
import os
import signal
import subprocess
import time
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    as_completed,
)
from pathlib import Path

import structlog

from .log_config import get_logger
from .metrics import (
    clear_stale_labels, clear_stale_relay_labels,
    last_round_timestamp, rounds_total,
    relay_status, round_duration_seconds,
    update_metrics, verify_relay_status,
)
from .output import write_textfile
from .prober import ProbeResult, RelayPool, run_probe

log = get_logger(__name__)

# Pre-lowercased keywords indicating an RPC transport failure (not an
# application-level error).  Used to decide when to reopen a RelayContext.
_RPC_CRASH_KEYWORDS = (
    "brokenpipe", "connectionreset", "eoferror", "process",
    "rpc server closed", "rpc process",
)


def _avg_ms(rtts_ms: list[float]) -> float:
    return sum(rtts_ms) / len(rtts_ms) if rtts_ms else 0.0


def kill_stale_rpc_servers(cache_dir, graceful=True):
    """Kill orphaned deltachat-rpc-server processes matching our cache_dir.

    graceful=True sends SIGTERM first (lets sqlite close WAL cleanly);
    graceful=False goes straight to SIGKILL (used during signal-handler shutdown).
    """
    cache_str = str(cache_dir)
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"deltachat-rpc-server.*{cache_str}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return
        pids = [int(p) for p in result.stdout.strip().split()]
        if graceful:
            for pid in pids:
                log.warning("Sending SIGTERM to stale deltachat-rpc-server (PID %d)", pid)
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    continue
            time.sleep(2)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
                log.warning("Sent SIGKILL to deltachat-rpc-server (PID %d)", pid)
            except ProcessLookupError:
                pass  # already exited from SIGTERM
    except (FileNotFoundError, ValueError):
        pass


def check_relays_alive(relays, args, cache_dir, previously_dead=None,
                       unreachable_relays=None, alive_pool=None):
    """Self-probe each relay in parallel; return (alive_list, dead_dict).

    Transient failures (timeout, unknown) are retried up to 2 times unless
    the relay was already dead last round.  Relays from unreachable_relays
    are checked but excluded from the alive list unless they recover.

    When alive_pool (a RelayPool) is provided, its shared contexts are reused
    across rounds instead of creating throwaway accounts each time.
    """
    if previously_dead is None:
        previously_dead = set()
    unreachable_set: set[str] = set(unreachable_relays or [])
    # Include unreachable relays in the alive check so we can detect recovery.
    all_check_relays = list(relays) + [r for r in unreachable_set if r not in relays]

    # Pre-open shared contexts so accounts are reused across rounds.
    if alive_pool is not None:
        alive_pool.open_all(all_check_relays)
        relay_contexts = alive_pool.contexts()
    else:
        relay_contexts = None

    def _submit_probe(executor, relay):
        if relay_contexts is not None:
            return executor.submit(
                run_probe, relay, relay, 1, args.ping_interval,
                timeout=args.timeout // 2, relay_contexts=relay_contexts)
        return executor.submit(
            run_probe, relay, relay, 1, args.ping_interval,
            str(cache_dir / "alive-check" / relay), args.timeout // 2)

    with ThreadPoolExecutor(max_workers=min(len(all_check_relays), args.workers)) as pool:
        futures = {
            _submit_probe(pool, r): r
            for r in all_check_relays
        }
        dead = {}  # relay -> error string
        completed = set()
        actual_workers = min(len(all_check_relays), args.workers)
        batches = -(-len(all_check_relays) // actual_workers)  # ceil division
        deadline = args.timeout * (batches + 1)
        check_start = time.monotonic()
        timeout_at = time.time() + deadline
        log.warning("alive_check_start",
                     count=len(all_check_relays), workers=actual_workers,
                     timeout_at=time.strftime('%H:%M:%S', time.localtime(timeout_at)))
        try:
            for future in as_completed(futures, timeout=deadline):
                relay = futures[future]
                completed.add(relay)
                result = future.result()
                with structlog.contextvars.bound_contextvars(relay=relay):
                    if result.error:
                        log.warning("relay_dead", error=result.error)
                        dead[relay] = result.error
                        if alive_pool is not None and any(
                            kw in result.error.lower() for kw in _RPC_CRASH_KEYWORDS
                        ):
                            try:
                                alive_pool.reopen(relay)
                                relay_contexts.update(alive_pool.contexts())
                            except Exception as e:
                                log.warning("alive_reopen_failed", relay=relay, error=str(e))
                    else:
                        log.info("relay_ok",
                                 rtt_ms=round(result.rtts_ms[0]) if result.rtts_ms else 0)
                if len(completed) % 10 == 0 or len(completed) == len(all_check_relays):
                    remaining = [r for r in all_check_relays if r not in completed and r not in dead]
                    if remaining:
                        log.info("alive_check_progress",
                                 remaining=len(remaining),
                                 pending=remaining[:5])
        except FuturesTimeoutError:
            elapsed = time.monotonic() - check_start
            for future, relay in futures.items():
                if relay not in completed and relay not in dead:
                    with structlog.contextvars.bound_contextvars(relay=relay):
                        log.warning("relay_timeout",
                                    deadline_s=round(elapsed))
                    dead[relay] = "timeout"
                    future.cancel()

    # Retry relays that failed with transient errors (timeout, unknown).
    # Persistent errors (genuine DNS, auth, TLS, connection refused) are
    # not retried since they won't resolve by waiting.  Relays that were
    # already dead last round are also skipped -- if they didn't recover
    # between rounds, retrying within the same window won't help.
    max_retries = 2
    retry_delay = 5
    # Cache DNS-based verify_relay_status results to avoid redundant lookups.
    _status_cache: dict[tuple[str, str | None], int] = {}

    def _cached_status(relay, error_str):
        key = (relay, error_str)
        if key not in _status_cache:
            _status_cache[key] = verify_relay_status(relay, error_str)
        return _status_cache[key]

    def _is_transient(relay, error_str):
        return _cached_status(relay, error_str) in (-1, 0)

    retryable = {r: err for r, err in dead.items()
                 if r not in previously_dead
                 and _is_transient(r, err)}
    skipped = {r for r in dead if r in previously_dead
               and _is_transient(r, dead[r])}
    if skipped:
        log.info("Skipping retries for %d previously-dead relay(s): %s",
                 len(skipped), ", ".join(skipped))
    if retryable:
        log.warning("alive_check_retrying",
                    count=len(retryable), relays=list(retryable))
    for attempt in range(1, max_retries + 1):
        if not retryable:
            break
        time.sleep(retry_delay)
        log.info("alive_check_retry_attempt",
                 attempt=attempt, max_retries=max_retries, count=len(retryable))
        with ThreadPoolExecutor(max_workers=min(len(retryable), args.workers)) as pool:
            retry_futures = {
                _submit_probe(pool, r): r
                for r in retryable
            }
            try:
                for future in as_completed(retry_futures, timeout=args.timeout * 2):
                    relay = retry_futures[future]
                    result = future.result()
                    with structlog.contextvars.bound_contextvars(
                        relay=relay, attempt=attempt, max_retries=max_retries
                    ):
                        if result.error:
                            log.warning("relay_retry_dead", error=result.error)
                            dead[relay] = result.error
                        else:
                            log.info("relay_retry_ok",
                                     rtt_ms=round(result.rtts_ms[0]) if result.rtts_ms else 0)
                            del dead[relay]
                            retryable.pop(relay, None)
            except FuturesTimeoutError:
                for future, relay in retry_futures.items():
                    if relay in retryable and relay in dead:
                        with structlog.contextvars.bound_contextvars(relay=relay):
                            log.warning("relay_retry_timeout",
                                        attempt=attempt, max_retries=max_retries)
        # Re-evaluate retryable with updated errors (new error string -> new cache entry)
        retryable = {r: dead[r] for r in retryable
                     if r in dead and _is_transient(r, dead[r])}

    # Build alive list: normal relays that passed + unreachable relays that recovered.
    alive = [r for r in relays if r not in dead]
    recovered_unreachable = [r for r in unreachable_set if r not in dead]
    if recovered_unreachable:
        log.warning("relay_recovered_from_unreachable",
                    count=len(recovered_unreachable),
                    relays=recovered_unreachable)
        alive = alive + recovered_unreachable
    recovered = set(previously_dead) - set(dead)
    if recovered:
        log.warning("relays_recovered", count=len(recovered), relays=list(recovered))
    if dead:
        log.warning("relays_unreachable", count=len(dead), relays=list(dead))
    # Update per-relay status metric; remove labels for relays dropped from config.
    # Include unreachable relays in the status metric so their recovery is visible.
    all_known = list(relays) + list(unreachable_set)
    clear_stale_relay_labels(all_known)
    for r in all_known:
        relay_status.labels(relay=r).set(_cached_status(r, dead.get(r)))
    elapsed = time.monotonic() - check_start
    log.warning("alive_check_complete",
                elapsed_s=round(elapsed, 1),
                online=len(alive), total=len(all_known))
    return alive, dead  # dead: dict[str, str | None]  (host -> error)


def run_round(relays, args, executors, worker_pools, shutdown_event,
              textfile=None, exclude=None):
    """Run one complete probe round across all relay pairs."""
    clear_stale_labels(relays)
    pairs = [(s, d) for s in relays for d in relays
             if not exclude or (s, d) not in exclude]
    log.info("probe_round_start", pairs=len(pairs), workers=args.workers)
    round_start = time.time()

    # Ensure all worker pools have contexts for all relays.
    for pool in worker_pools:
        pool.open_all(relays)

    worker_pairs = [[] for _ in range(args.workers)]
    for i, pair in enumerate(pairs):
        worker_pairs[i % args.workers].append(pair)

    # Capture per-worker relay contexts so we can update them if a relay is reopened
    worker_relay_contexts = {}
    all_futures = {}
    for worker_id, executor in enumerate(executors):
        relay_contexts = worker_pools[worker_id].contexts()
        worker_relay_contexts[worker_id] = relay_contexts
        for src, dst in worker_pairs[worker_id]:
            try:
                future = executor.submit(
                    run_probe, src, dst, args.count, args.ping_interval,
                    timeout=args.timeout,
                    relay_contexts=relay_contexts,
                )
            except RuntimeError:
                # Executor was shut down (e.g. by signal handler).
                break
            all_futures[future] = (src, dst, worker_id)

    completed = 0
    failed = 0
    round_results: list[ProbeResult] = []
    reopen_count = {}  # relay -> number of reopens this round
    _reopen_limit = 3
    for future in as_completed(all_futures):
        if shutdown_event and shutdown_event.is_set():
            break
        completed += 1
        src, dst, worker_id = all_futures[future]
        with structlog.contextvars.bound_contextvars(
            src=src, dst=dst, worker_id=worker_id
        ):
            try:
                result = future.result()
            except Exception as exc:
                log.exception("worker_crash")
                result = ProbeResult(src, dst, error=str(exc))
            round_results.append(result)
            update_metrics(result)
            if completed % 50 == 0:
                gc.collect()
                if textfile:
                    write_textfile(textfile)
            if result.error:
                failed += 1
                log.warning(
                    "probe_failed",
                    n=completed, total=len(pairs), error=result.error,
                )
                if any(kw in result.error.lower() for kw in _RPC_CRASH_KEYWORDS):
                    pool = worker_pools[worker_id]
                    for relay in (src, dst):
                        prior = reopen_count.get(relay, 0)
                        if prior >= _reopen_limit:
                            log.debug("skipping_reopen", relay=relay, prior_reopens=prior)
                            continue
                        try:
                            pool.reopen(relay)
                            reopen_count[relay] = prior + 1
                            # Update the captured relay_contexts dict so other running probes
                            # in this worker see the freshly reopened context, not the old closed one.
                            worker_relay_contexts[worker_id].update(pool.contexts())
                        except Exception as reopen_err:
                            log.warning("reopen_failed", relay=relay, error=str(reopen_err))
            else:
                log.info(
                    "probe_ok",
                    n=completed, total=len(pairs),
                    sent=result.sent, recv=result.received,
                    avg_ms=round(_avg_ms(result.rtts_ms)),
                    loss_pct=result.loss,
                )

    elapsed = time.time() - round_start
    last_round_timestamp.set(time.time())
    round_duration_seconds.set(elapsed)
    rounds_total.inc()
    success_count = completed - failed
    success_rate = 100.0 * success_count / completed if completed > 0 else 0.0
    avg_ms_per_pair = int(elapsed * 1000 / completed) if completed > 0 else 0
    log.warning("round_complete",
                success_count=success_count, total=completed,
                success_rate_pct=round(success_rate, 1),
                avg_ms_per_pair=avg_ms_per_pair,
                elapsed_s=round(elapsed, 1))
    return elapsed, round_results


def scan_relays(relays, args, cache_dir):
    """Self-probe all relays in parallel, print ranked by avg RTT, then exit."""
    log.info("Scanning %d relays...", len(relays))

    results = {}
    with ThreadPoolExecutor(max_workers=min(len(relays), args.workers)) as pool:
        futures = {
            pool.submit(run_probe, r, r, args.count, args.ping_interval,
                        str(cache_dir / "scan" / r), args.timeout): r
            for r in relays
        }
        for future in as_completed(futures):
            relay = futures[future]
            results[relay] = future.result()
            if results[relay].error:
                log.info("DEAD %s: %s", relay, results[relay].error)
            else:
                log.info("OK   %s (%.0fms)", relay, _avg_ms(results[relay].rtts_ms))

    ranked = sorted(
        relays,
        key=lambda r: _avg_ms(results[r].rtts_ms) if results[r].rtts_ms else float("inf"),
    )

    print("\nScan results (fastest first):\n")
    print(f"  {'Rank':<5} {'Relay':<40} {'Avg RTT':>10} {'Loss':>8} {'Samples':>8}")
    print(f"  {'-'*5} {'-'*40} {'-'*10} {'-'*8} {'-'*8}")
    for i, relay in enumerate(ranked, 1):
        r = results[relay]
        if r.error:
            print(f"  {i:<5} {relay:<40} {'DEAD':>10} {'':>8} {'':>8}")
        else:
            marker = " <--" if i <= args.top else ""
            print(f"  {i:<5} {relay:<40} {_avg_ms(r.rtts_ms):>9.0f}ms {r.loss:>7.1f}% {len(r.rtts_ms):>8}{marker}")
    print()
