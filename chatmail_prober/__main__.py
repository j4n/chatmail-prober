"""chatmail-prober: Smokeping-style Prometheus exporter for chatmail relay interop.

Periodically probes all pairs of configured chatmail relays and exposes
round-trip time histograms, counters, and success gauges as Prometheus metrics.
"""

import argparse
import gc
import logging
import os
import re
import resource
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    as_completed,
)
from pathlib import Path

import structlog

from .log_config import configure_logging, get_logger
from .metrics import (
    clear_stale_labels, clear_stale_relay_labels,
    is_transient_alive_error, last_round_timestamp,
    relay_status, relay_status_value,
    round_duration_seconds, update_metrics, verify_relay_status,
)
from .cli_summary import render as render_summary
from .output import print_metrics, start_exporter_server, write_textfile
from .prober import ProbeResult, RelayPool, run_probe

log = get_logger(__name__)
_app_log = get_logger("chatmail_prober")

AUTO_FETCH_URL = "https://chatmail.at/relays"


class _SupprRpcClosedFilter(logging.Filter):
    """Suppress 'RPC server closed' errors during shutdown only.

    During shutdown, the event loop thread may try to read from the closed RPC
    server and raise an error. This is expected and not actionable, so we filter
    it out. During normal operation these errors indicate real RPC crashes and
    should be visible for diagnostics.
    """
    def __init__(self, shutdown_event):
        super().__init__()
        self._shutdown_event = shutdown_event

    def filter(self, record):
        if (self._shutdown_event.is_set()
                and "RPC server closed" in str(record.getMessage())):
            return False  # suppress during shutdown
        return True


def _avg_ms(rtts_ms: list[float]) -> float:
    return sum(rtts_ms) / len(rtts_ms) if rtts_ms else 0.0


def read_relay_list(paths):
    """Read relay domains from one or more files (one per line, # comments)."""
    seen = set()
    relays = []
    for path in paths:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and line not in seen:
                    seen.add(line)
                    relays.append(line)
    if not relays:
        raise SystemExit(f"No relays found in {paths}")
    return relays


def read_exclude_list(path):
    """Read pair exclusions from a file.

    Format: one "source->destination" per line.  # comments and blank lines
    are ignored.  Returns a set of (source, destination) tuples.
    """
    excludes = set()
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "->" not in line:
                log.warning("Ignoring malformed exclude line: %s", line)
                continue
            src, dst = line.split("->", 1)
            excludes.add((src.strip(), dst.strip()))
    log.info("Loaded %d pair exclusion(s) from %s", len(excludes), path)
    return excludes


def fetch_relay_list(url, dest):
    """Fetch relay domains from url, write to dest (one domain per line).

    Parses chatmail.at/relays HTML: extracts text from <a class="hilite"> tags.
    """
    with urllib.request.urlopen(url, timeout=30) as resp:
        html = resp.read().decode()
    domains = re.findall(r'class="hilite">([^<]+)', html)
    if not domains:
        raise SystemExit(f"No relay domains found at {url}")
    Path(dest).write_text("\n".join(domains) + "\n")
    log.info("Fetched %d relays from %s -> %s", len(domains), url, dest)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="chatmail-prober",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "relays",
        nargs="*",
        help="relay list file(s) (one domain per line, # comments); at least one of this or --auto-fetch required",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="HTTP listen port for /metrics (0 = disabled, e.g. --port 9740)",
    )
    parser.add_argument(
        "--textfile",
        default=None,
        help="path to write .prom file for node_exporter textfile collector",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=900,
        help="seconds between probe rounds (default: 900 = 15min)",
    )
    parser.add_argument(
        "--alive-check-interval",
        type=int,
        default=86400,
        help="seconds between relay alive re-checks (default: 86400 = 24h, 0 = every round)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=5,
        help="number of pings per pair per round (default: 5)",
    )
    parser.add_argument(
        "--ping-interval",
        type=float,
        default=0.1,
        help="seconds between individual pings within a probe (default: 0.1)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=90,
        help="per-pair receive timeout in seconds (default: 90)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=5,
        help="max concurrent probe threads (default: 5)",
    )
    parser.add_argument(
        "--cache-dir",
        default="~/.cache/chatmail-prober",
        help="base dir for per-pair accounts (default: ~/.cache/chatmail-prober)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one probe round then exit (useful for testing)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="count",
        default=0,
        help="increase verbosity (-v debug, -vv rpc/deltachat events)",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="suppress progress output (only show warnings/errors)",
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="run self-probes on all relays, print ranked by RTT, then exit",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="number of fastest relays to highlight in --scan output (default: 10)",
    )
    parser.add_argument(
        "--auto-fetch",
        default=None,
        metavar="PATH",
        help=f"fetch relay list from {AUTO_FETCH_URL} and write to PATH before starting",
    )
    parser.add_argument(
        "--exclude",
        default=None,
        metavar="PATH",
        help='file of pairs to skip, one per line: "src->dst" (# comments allowed)',
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="remove all account directories to force fresh account creation",
    )
    parser.add_argument(
        "--print-metrics",
        action="store_true",
        default=False,
        help="print Prometheus metrics to stdout after --once exits (requires --once)",
    )
    parser.add_argument(
        "--print",
        dest="print_summary",
        action="store_true",
        default=False,
        help="print gocmping-style summary to stdout after --once exits (requires --once)",
    )
    return parser.parse_args(argv)


def scan_relays(relays, args):
    """Self-probe all relays in parallel, print ranked by avg RTT, then exit."""
    log.info("Scanning %d relays...", len(relays))
    cache_dir = Path(args.cache_dir).expanduser()

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


def _kill_stale_rpc_servers(cache_dir, graceful=True):
    """Kill orphaned deltachat-rpc-server processes from a previous crash.

    Only targets processes whose command line contains our cache_dir path,
    so unrelated deltachat instances are not affected.

    When graceful=True (default, used at startup), sends SIGTERM first and
    waits briefly for a clean shutdown before escalating to SIGKILL.  This
    lets sqlite close its WAL cleanly.  When graceful=False (used during
    signal-handler shutdown where speed matters), goes straight to SIGKILL.
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


def check_relays_alive(relays, args, previously_dead=None):
    """Run a single self-probe (relay->itself, count=1) for each relay in parallel.

    Returns (alive, dead) where alive is the list of relays that succeeded
    (in original order) and dead is a set of relay domains that failed.
    Dead relays are logged as warnings and excluded from the matrix.

    Relays that fail with transient errors (timeout, unknown) are retried
    up to 2 times -- unless they were already in previously_dead, meaning
    they failed last round too and retrying within the same window is
    unlikely to help.
    """
    if previously_dead is None:
        previously_dead = set()
    cache_dir = Path(args.cache_dir).expanduser()

    with ThreadPoolExecutor(max_workers=min(len(relays), args.workers)) as pool:
        futures = {
            pool.submit(run_probe, r, r, 1, args.ping_interval,
                        str(cache_dir / "alive-check" / r), args.timeout // 2): r
            for r in relays
        }
        dead = {}  # relay -> error string
        completed = set()
        actual_workers = min(len(relays), args.workers)
        batches = -(-len(relays) // actual_workers)  # ceil division
        deadline = args.timeout * (batches + 1)
        check_start = time.monotonic()
        timeout_at = time.time() + deadline
        log.warning("alive_check_start",
                     count=len(relays), workers=actual_workers,
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
                    else:
                        log.info("relay_ok",
                                 rtt_ms=round(result.rtts_ms[0]) if result.rtts_ms else 0)
                remaining = [r for r in relays if r not in completed and r not in dead]
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
    retryable = {r: err for r, err in dead.items()
                 if r not in previously_dead
                 and is_transient_alive_error(r, err)}
    skipped = {r for r in dead if r in previously_dead
               and is_transient_alive_error(r, dead[r])}
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
                pool.submit(run_probe, r, r, 1, args.ping_interval,
                            str(cache_dir / "alive-check" / r),
                            args.timeout // 2): r
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
        # Re-evaluate retryable with updated errors
        retryable = {r: dead[r] for r in retryable
                     if r in dead and is_transient_alive_error(r, dead[r])}

    alive = [r for r in relays if r not in dead]
    dead_set = set(dead)
    recovered = previously_dead - dead_set
    if recovered:
        log.warning("relays_recovered", count=len(recovered), relays=list(recovered))
    if dead:
        log.warning("relays_unreachable", count=len(dead), relays=list(dead))

    # Update per-relay status metric; remove labels for relays dropped from config
    clear_stale_relay_labels(relays)
    for r in relays:
        relay_status.labels(relay=r).set(verify_relay_status(r, dead.get(r)))

    elapsed = time.monotonic() - check_start
    log.warning("alive_check_complete",
                elapsed_s=round(elapsed, 1),
                online=len(alive), total=len(relays))

    return alive, dead_set


def run_round(relays, args, executors, worker_pools, shutdown_event,
              textfile=None, exclude=None):
    """Run one complete probe round across all relay pairs.

    Each worker has its own RelayPool with isolated account directories.
    Accounts persist across probes within a worker and across rounds, so
    only the first probe per relay per worker pays account-creation cost.

    If shutdown_event is set during the round, the loop breaks immediately
    without recording metrics for in-flight probes (which would show spurious
    errors from killed rpc-server processes).
    """
    clear_stale_labels(relays)
    pairs = [(s, d) for s in relays for d in relays
             if not exclude or (s, d) not in exclude]
    log.info("probe_round_start", pairs=len(pairs), workers=args.workers)
    round_start = time.time()

    # Ensure all worker pools have contexts for all relays.
    for pool in worker_pools:
        pool.open_all(relays)

    # Partition pairs round-robin: pair i goes to worker i % workers
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
                # Reopen contexts for relays involved in RPC-level failures
                # so subsequent probes can recover.
                _rpc_keywords = ("BrokenPipe", "ConnectionReset",
                                 "EOFError", "process",
                                 "rpc server closed", "rpc process")
                if any(kw.lower() in result.error.lower() for kw in _rpc_keywords):
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
    success_count = completed - failed
    success_rate = 100.0 * success_count / completed if completed > 0 else 0.0
    avg_ms_per_pair = int(elapsed * 1000 / completed) if completed > 0 else 0
    log.warning("round_complete",
                success_count=success_count, total=completed,
                success_rate_pct=round(success_rate, 1),
                avg_ms_per_pair=avg_ms_per_pair,
                elapsed_s=round(elapsed, 1))
    return elapsed, round_results


def main(argv=None):
    args = parse_args(argv)

    # Determine the app-level log level from CLI flags.
    if args.quiet:
        app_level = logging.WARNING
    elif args.verbose >= 1:
        app_level = logging.DEBUG
    else:
        app_level = logging.INFO

    # Configure structlog pipeline.  Auto-detects TTY for renderer selection:
    # colourised text on a terminal, JSON when running under systemd/journald.
    configure_logging(level=app_level)

    # Created early so the log filter can reference it; also used by signal handlers.
    shutdown_event = threading.Event()

    # Suppress harmless "RPC server closed" errors from event loop during shutdown.
    logging.getLogger().addFilter(_SupprRpcClosedFilter(shutdown_event))

    # -vv also enables root-level DEBUG (rpc/deltachat events)
    if args.verbose >= 2:
        logging.getLogger().setLevel(logging.DEBUG)

    # Raise the fd soft limit to the hard limit so large relay matrices
    # don't hit the default 1024 cap when deltachat-rpc-server opens many DBs.
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < hard:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
            log.debug("Raised fd limit %d -> %d", soft, hard)
    except (ValueError, OSError):
        pass

    relay_files = list(args.relays)
    if args.auto_fetch:
        fetch_relay_list(AUTO_FETCH_URL, args.auto_fetch)
        relay_files.append(args.auto_fetch)
    if not relay_files:
        raise SystemExit("error: at least one relay list file or --auto-fetch is required")
    relays = read_relay_list(relay_files)
    log.info("Loaded %d relays: %s", len(relays), ", ".join(relays))

    exclude = set()
    if args.exclude:
        exclude = read_exclude_list(args.exclude)

    cache_dir = Path(args.cache_dir).expanduser()
    cache_dir.mkdir(parents=True, exist_ok=True)

    if args.reset:
        for child in cache_dir.iterdir():
            if child.is_dir() and child.name != "alive-check":
                shutil.rmtree(child)
                log.info("Reset: removed %s", child)

    if args.scan:
        scan_relays(relays, args)
        return

    total_pairs = len(relays) ** 2 - len(exclude)
    log.info(
        "Pairs: %d, count: %d, interval: %ds, workers: %d",
        total_pairs, args.count, args.interval, args.workers,
    )

    # Clean up orphaned RPC servers and stale locks from previous crashes.
    _kill_stale_rpc_servers(cache_dir)
    for lock in cache_dir.rglob("accounts.lock"):
        lock.unlink(missing_ok=True)
        log.debug("Removed stale lock: %s", lock)

    # Executors start empty; populated after alive check. Signal handlers
    # iterate this list, so an empty list is safe during the alive check.
    executors = []

    # Graceful shutdown: first SIGINT/SIGTERM cancels pending work and kills
    # rpc-server subprocesses to unblock running probes; second kills immediately.
    # SIGUSR1 lets the current round finish, then exits cleanly.
    stop_after_round = threading.Event()
    sigint_count = 0

    def _handle_signal(signum, frame):
        nonlocal sigint_count
        sigint_count += 1
        if sigint_count >= 2:
            log.warning("Second interrupt -- killing immediately")
            os._exit(1)
        shutdown_event.set()
        log.info("Shutting down, killing running probes...")
        for ex in executors:
            ex.shutdown(wait=False, cancel_futures=True)
        _kill_stale_rpc_servers(Path(args.cache_dir).expanduser(), graceful=False)
        # Main thread may be stuck in as_completed(); force exit after 5s.
        threading.Timer(5.0, os._exit, args=(0,)).start()

    def _handle_usr1(signum, frame):
        stop_after_round.set()
        log.warning("SIGUSR1 received -- will exit after current round completes")

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGUSR1, _handle_usr1)

    # Verbosity cycle: quiet -> normal -> debug -> debug+rpc -> quiet ...
    _verbosity_levels = [
        # (log_level, root_level, label)
        (logging.WARNING, logging.WARNING, "quiet"),
        (logging.INFO,    logging.WARNING, "normal"),
        (logging.DEBUG,   logging.WARNING, "debug"),
        (logging.DEBUG,   logging.DEBUG,   "debug+rpc"),
    ]

    # Determine starting position in the cycle from startup flags.
    if args.quiet:
        _verbosity_idx = 0
    elif args.verbose >= 2:
        _verbosity_idx = 3
    elif args.verbose >= 1:
        _verbosity_idx = 2
    else:
        _verbosity_idx = 1

    def _handle_usr2(signum, frame):
        nonlocal _verbosity_idx
        _verbosity_idx = (_verbosity_idx + 1) % len(_verbosity_levels)
        level, root_level, label = _verbosity_levels[_verbosity_idx]
        # Level changes go through the stdlib logger hierarchy; structlog
        # BoundLoggers delegate level checks to the underlying stdlib logger.
        logging.getLogger("chatmail_prober").setLevel(level)
        logging.getLogger().setLevel(root_level)
        # Log at WARNING so it's visible regardless of current level.
        log.warning("SIGUSR2: verbosity -> %s", label)

    signal.signal(signal.SIGUSR2, _handle_usr2)

    all_relays = relays  # preserve full list for periodic re-checks
    previously_dead = set()
    relays, previously_dead = check_relays_alive(all_relays, args)
    if not relays:
        raise SystemExit("No reachable relays -- aborting")
    log.info("continuing with %d/%d relays online, starting matrix probe", len(relays), len(all_relays))
    last_alive_check = time.monotonic()
    log.info("next alive check in %ds", args.alive_check_interval)

    if args.port:
        start_exporter_server(args.port)

    # Create executors; reused across rounds to keep worker threads warm.
    executors.extend(ThreadPoolExecutor(max_workers=1) for _ in range(args.workers))

    # Per-worker RelayPools: each worker gets its own account directory so
    # accounts are reused across probes within the same worker and across rounds.
    worker_pools = [RelayPool(cache_dir / f"worker-{i}") for i in range(args.workers)]

    try:
        while not shutdown_event.is_set():
            # Periodically re-check which relays are alive
            interval = args.alive_check_interval
            if interval == 0 or time.monotonic() - last_alive_check >= interval:
                # Re-fetch relay list from URL if --auto-fetch is configured
                if args.auto_fetch:
                    try:
                        fetch_relay_list(AUTO_FETCH_URL, args.auto_fetch)
                        refreshed = read_relay_list(relay_files)
                        if refreshed != all_relays:
                            added = set(refreshed) - set(all_relays)
                            removed = set(all_relays) - set(refreshed)
                            if added:
                                log.warning("Relay list: %d new: %s", len(added), ", ".join(added))
                            if removed:
                                log.warning("Relay list: %d removed: %s", len(removed), ", ".join(removed))
                            all_relays = refreshed
                    except Exception as e:
                        log.warning("Failed to refresh relay list: %s", e)
                relays, previously_dead = check_relays_alive(
                    all_relays, args, previously_dead=previously_dead)
                last_alive_check = time.monotonic()
                log.info("continuing with %d/%d relays online, next check in %ds", len(relays), len(all_relays), interval)

            elapsed, round_results = run_round(
                relays, args, executors, worker_pools,
                shutdown_event,
                textfile=args.textfile, exclude=exclude,
            )

            if args.textfile:
                write_textfile(args.textfile)

            if args.once or stop_after_round.is_set():
                if args.print_summary:
                    alive_set = set(relays)
                    dead_relays = [r for r in all_relays if r not in alive_set]
                    render_summary(
                        round_results, relays, dead_relays, elapsed_s=elapsed
                    )
                if args.print_metrics:
                    print_metrics()
                break

            remaining = max(0, args.interval - elapsed)
            if remaining == 0:
                log.warning(
                    "Probe round took %.0fs, exceeds interval %ds -- starting next immediately",
                    elapsed, args.interval,
                )
            else:
                log.info("Sleeping %.0fs until next round", remaining)
                # Sleep via Event.wait so signal handler can wake us immediately.
                shutdown_event.wait(timeout=remaining)
    finally:
        if args.textfile:
            log.info("Writing final metrics")
            write_textfile(args.textfile)
        for pool in worker_pools:
            pool.close()
        for ex in executors:
            ex.shutdown(wait=not shutdown_event.is_set(), cancel_futures=True)
        if shutdown_event.is_set():
            # Worker threads may be stuck on I/O with killed rpc-servers.
            # os._exit bypasses atexit thread-join and lets the OS reap zombies.
            os._exit(0)


if __name__ == "__main__":
    main()
