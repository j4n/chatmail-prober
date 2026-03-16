"""chatmail-prober: Smokeping-style Prometheus exporter for chatmail relay interop.

Periodically probes all pairs of configured chatmail relays using cmping
and exposes round-trip time histograms, counters, and success gauges
as Prometheus metrics.
"""

import builtins

# Save real print before prober.py's cmping import monkey-patches builtins.print.
_print = builtins.print

import argparse
import gc
import logging
import os
import resource
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .metrics import (
    clear_stale_labels, last_round_timestamp, round_duration_seconds,
    update_metrics,
)
from .output import start_exporter_server, write_textfile
from .prober import ProbeResult, run_probe

log = logging.getLogger("chatmail_prober")

AUTO_FETCH_URL = "https://chatmail.at/relays"


def _avg_ms(rtts_ms):
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
    import re
    import urllib.request
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
        help="relay list file(s) (one domain per line); at least one of this or --auto-fetch required",
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
        default=60,
        help="per-pair receive timeout in seconds (default: 60)",
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
        help="increase verbosity: -v debug, -vv cmping errors/stats, -vvv cmping events",
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
    return parser.parse_args(argv)


def scan_relays(relays, args):
    """Self-probe all relays in parallel, print ranked by avg RTT, then exit."""
    log.info("Scanning %d relays...", len(relays))
    cache_dir = Path(args.cache_dir).expanduser()

    results = {}
    with ThreadPoolExecutor(max_workers=min(len(relays), args.workers)) as pool:
        futures = {
            pool.submit(run_probe, r, r, args.count, args.ping_interval,
                        str(cache_dir / "scan" / r), args.timeout, args.verbose): r
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

    _print("\nScan results (fastest first):\n")
    _print(f"  {'Rank':<5} {'Relay':<40} {'Avg RTT':>10} {'Loss':>8} {'Samples':>8}")
    _print(f"  {'-'*5} {'-'*40} {'-'*10} {'-'*8} {'-'*8}")
    for i, relay in enumerate(ranked, 1):
        r = results[relay]
        if r.error:
            _print(f"  {i:<5} {relay:<40} {'DEAD':>10} {'':>8} {'':>8}")
        else:
            marker = " <--" if i <= args.top else ""
            _print(f"  {i:<5} {relay:<40} {_avg_ms(r.rtts_ms):>9.0f}ms {r.loss:>7.1f}% {len(r.rtts_ms):>8}{marker}")
    _print()


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


def check_relays_alive(relays, args):
    """Run a single self-probe (relay→itself, count=1) for each relay in parallel.

    Returns the list of relays that succeeded, in original order.
    Dead relays are logged as warnings and excluded from the matrix.
    """
    log.info("Checking %d relays with self-probe...", len(relays))
    cache_dir = Path(args.cache_dir).expanduser()

    with ThreadPoolExecutor(max_workers=min(len(relays), args.workers)) as pool:
        futures = {
            pool.submit(run_probe, r, r, 1, args.ping_interval,
                        str(cache_dir / "alive-check" / r), args.timeout, args.verbose): r
            for r in relays
        }
        dead = set()
        completed = set()
        try:
            for future in as_completed(futures, timeout=args.timeout * 2):
                relay = futures[future]
                completed.add(relay)
                result = future.result()
                if result.error:
                    log.warning("DEAD %s: %s", relay, result.error)
                    dead.add(relay)
                else:
                    log.info("OK   %s (%.0fms)", relay, result.rtts_ms[0] if result.rtts_ms else 0)
                remaining = [r for r in relays if r not in completed and r not in dead]
                if remaining:
                    names = ", ".join(remaining[:5])
                    suffix = "..." if len(remaining) > 5 else ""
                    log.info("  %d remaining: %s%s", len(remaining), names, suffix)
        except TimeoutError:
            for future, relay in futures.items():
                if relay not in completed and relay not in dead:
                    log.warning("TIMEOUT %s: alive check exceeded global deadline", relay)
                    dead.add(relay)
                    future.cancel()

    alive = [r for r in relays if r not in dead]
    if dead:
        log.warning("%d relay(s) unreachable, skipping from matrix: %s", len(dead), ", ".join(dead))
    return alive


def run_round(relays, args, executors, shutdown_event=None, textfile=None,
              exclude=None):
    """Run one complete probe round across all relay pairs.

    Pairs are distributed round-robin across the per-worker executors so each
    worker's single thread accesses its own accounts dir sequentially, avoiding
    deltachat-rpc-server DB lock contention.

    If shutdown_event is set during the round, the loop breaks immediately
    without recording metrics for in-flight probes (which would show spurious
    errors from killed rpc-server processes).
    """
    clear_stale_labels(relays)
    pairs = [(s, d) for s in relays for d in relays
             if not exclude or (s, d) not in exclude]
    log.info("Starting probe round: %d pairs, %d workers", len(pairs), args.workers)
    round_start = time.time()

    cache_dir = Path(args.cache_dir).expanduser()

    # Partition pairs round-robin: pair i goes to worker i % workers
    worker_pairs = [[] for _ in range(args.workers)]
    for i, pair in enumerate(pairs):
        worker_pairs[i % args.workers].append(pair)

    all_futures = {}
    for worker_id, executor in enumerate(executors):
        worker_dir = cache_dir / f"worker-{worker_id}"
        for src, dst in worker_pairs[worker_id]:
            try:
                future = executor.submit(
                    run_probe, src, dst, args.count, args.ping_interval, str(worker_dir), args.timeout, args.verbose,
                )
            except RuntimeError:
                # Executor was shut down (e.g. by signal handler).
                break
            all_futures[future] = (src, dst)

    completed = 0
    for future in as_completed(all_futures):
        if shutdown_event and shutdown_event.is_set():
            break
        completed += 1
        src, dst = all_futures[future]
        try:
            result = future.result()
        except Exception as exc:
            log.exception("Worker crashed for %s -> %s", src, dst)
            result = ProbeResult(src, dst, error=str(exc))
        update_metrics(result)
        if completed % 50 == 0:
            gc.collect()
            if textfile:
                write_textfile(textfile)
        if result.error:
            log.warning("[%d/%d] %s -> %s: ERROR %s", completed, len(pairs), src, dst, result.error)
        else:
            log.info(
                "[%d/%d] %s -> %s: %d/%d received, avg %.0fms, loss %.1f%%",
                completed, len(pairs), src, dst, result.received, result.sent,
                _avg_ms(result.rtts_ms), result.loss,
            )

    elapsed = time.time() - round_start
    last_round_timestamp.set(time.time())
    round_duration_seconds.set(elapsed)
    log.info("Probe round complete in %.1fs", elapsed)
    return elapsed


def main(argv=None):
    args = parse_args(argv)

    # Root logger stays at WARNING — prevents deltachat-rpc-client's internal
    # event logging from flooding the output when -v is used.
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Our logger defaults to INFO so progress is always visible.
    if args.quiet:
        log.setLevel(logging.WARNING)
    elif args.verbose >= 1:
        log.setLevel(logging.DEBUG)
    else:
        log.setLevel(logging.INFO)

    # At -vv or higher, restore print so cmping's own output is visible.
    if args.verbose >= 2:
        builtins.print = _print

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
    shutdown_event = threading.Event()
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
        log.info("SIGUSR1 received -- will exit after current round completes")

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGUSR1, _handle_usr1)

    relays = check_relays_alive(relays, args)
    if not relays:
        raise SystemExit("No reachable relays -- aborting")
    log.info("%d relay(s) alive, starting matrix probe", len(relays))

    if args.port:
        start_exporter_server(args.port)

    # Create executors; reused across rounds to keep worker threads warm.
    executors.extend(ThreadPoolExecutor(max_workers=1) for _ in range(args.workers))

    try:
        while not shutdown_event.is_set():
            elapsed = run_round(relays, args, executors, shutdown_event,
                                textfile=args.textfile, exclude=exclude)

            if args.textfile:
                write_textfile(args.textfile)

            if args.once or stop_after_round.is_set():
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
        for ex in executors:
            ex.shutdown(wait=not shutdown_event.is_set(), cancel_futures=True)
        if shutdown_event.is_set():
            # Worker threads may be stuck on I/O with killed rpc-servers.
            # os._exit bypasses atexit thread-join and lets the OS reap zombies.
            os._exit(0)


if __name__ == "__main__":
    main()
