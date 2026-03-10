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
import resource
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .metrics import update_metrics
from .output import start_exporter_server, write_textfile
from .prober import ProbeResult, run_probe

log = logging.getLogger("chatmail_prober")


def _avg_ms(rtts_ms):
    return sum(rtts_ms) / len(rtts_ms) if rtts_ms else 0.0


def read_relay_list(path):
    """Read relay domains from a config file (one per line, # comments)."""
    relays = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                relays.append(line)
    if not relays:
        raise SystemExit(f"No relays found in {path}")
    return relays


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="chatmail-prober",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "relays",
        help="path to relay list file (one domain per line)",
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
        help="increase logging verbosity (show debug messages)",
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
    return parser.parse_args(argv)


def scan_relays(relays, args):
    """Self-probe all relays sequentially, print ranked by avg RTT, then exit."""
    log.info("Scanning %d relays...", len(relays))
    cache_dir = Path(args.cache_dir).expanduser()

    results = {}
    for r in relays:
        log.info("Probing %s...", r)
        results[r] = run_probe(r, r, args.count, args.ping_interval,
                               str(cache_dir / "scan"), args.timeout)

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
                        str(cache_dir / "alive-check"), args.timeout): r
            for r in relays
        }
        dead = set()
        for future in as_completed(futures):
            relay = futures[future]
            result = future.result()
            update_metrics(result)
            if result.error:
                log.warning("DEAD %s: %s", relay, result.error)
                dead.add(relay)
            else:
                log.info("OK   %s (%.0fms)", relay, result.rtts_ms[0] if result.rtts_ms else 0)

    alive = [r for r in relays if r not in dead]
    if dead:
        log.warning("%d relay(s) unreachable, skipping from matrix: %s", len(dead), ", ".join(dead))
    return alive


def run_round(relays, args, executors):
    """Run one complete probe round across all relay pairs.

    Pairs are distributed round-robin across the per-worker executors so each
    worker's single thread accesses its own accounts dir sequentially, avoiding
    deltachat-rpc-server DB lock contention.
    """
    pairs = [(s, d) for s in relays for d in relays]
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
            future = executor.submit(
                run_probe, src, dst, args.count, args.ping_interval, str(worker_dir), args.timeout,
            )
            all_futures[future] = (src, dst)

    completed = 0
    for future in as_completed(all_futures):
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
        if result.error:
            log.warning("[%d/%d] %s -> %s: ERROR %s", completed, len(pairs), src, dst, result.error)
        else:
            log.info(
                "[%d/%d] %s -> %s: %d/%d received, avg %.0fms, loss %.1f%%",
                completed, len(pairs), src, dst, result.received, result.sent,
                _avg_ms(result.rtts_ms), result.loss,
            )

    elapsed = time.time() - round_start
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

    # Raise the fd soft limit to the hard limit so large relay matrices
    # don't hit the default 1024 cap when deltachat-rpc-server opens many DBs.
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < hard:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
            log.debug("Raised fd limit %d -> %d", soft, hard)
    except (ValueError, OSError):
        pass

    relays = read_relay_list(args.relays)
    log.info("Loaded %d relays: %s", len(relays), ", ".join(relays))

    cache_dir = Path(args.cache_dir).expanduser()
    cache_dir.mkdir(parents=True, exist_ok=True)

    if args.scan:
        scan_relays(relays, args)
        return

    log.info(
        "Pairs: %d, count: %d, interval: %ds, workers: %d",
        len(relays) ** 2, args.count, args.interval, args.workers,
    )

    relays = check_relays_alive(relays, args)
    if not relays:
        raise SystemExit("No reachable relays -- aborting")
    log.info("%d relay(s) alive, starting matrix probe", len(relays))

    if args.port:
        start_exporter_server(args.port)

    # Create executors once; reused across rounds to keep worker threads warm.
    executors = [ThreadPoolExecutor(max_workers=1) for _ in range(args.workers)]
    try:
        while True:
            elapsed = run_round(relays, args, executors)

            if args.textfile:
                write_textfile(args.textfile)

            if args.once:
                break

            remaining = max(0, args.interval - elapsed)
            if remaining == 0:
                log.warning(
                    "Probe round took %.0fs, exceeds interval %ds — starting next immediately",
                    elapsed, args.interval,
                )
            else:
                log.info("Sleeping %.0fs until next round", remaining)
                time.sleep(remaining)
    except KeyboardInterrupt:
        log.info("Interrupted, writing partial metrics")
        if args.textfile:
            write_textfile(args.textfile)
    finally:
        for ex in executors:
            ex.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    main()
