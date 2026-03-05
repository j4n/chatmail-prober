"""chatmail-prober: Smokeping-style Prometheus exporter for chatmail relay interop.

Periodically probes all pairs of configured chatmail relays using cmping
and exposes round-trip time histograms, counters, and success gauges
as Prometheus metrics.
"""

import argparse
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .metrics import update_metrics
from .output import start_exporter_server, write_textfile
from .prober import run_probe

log = logging.getLogger("chatmail_prober")


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
        "--relays",
        required=True,
        help="path to relay list file (one domain per line)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9740,
        help="HTTP listen port for /metrics (default: 9740, 0 to disable)",
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
        default=10,
        help="number of pings per pair per round (default: 10)",
    )
    parser.add_argument(
        "--ping-interval",
        type=float,
        default=1.1,
        help="seconds between individual pings within a probe (default: 1.1)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="per-pair timeout in seconds (default: 120)",
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
        help="increase logging verbosity",
    )
    return parser.parse_args(argv)


def run_round(relays, args):
    """Run one complete probe round across all relay pairs."""
    pairs = [(s, d) for s in relays for d in relays]
    log.info("Starting probe round: %d pairs, %d workers", len(pairs), args.workers)
    round_start = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                run_probe, src, dst, args.count, args.ping_interval, args.cache_dir
            ): (src, dst)
            for src, dst in pairs
        }
        for future in as_completed(futures):
            src, dst = futures[future]
            result = future.result()
            update_metrics(result)
            if result.error:
                log.warning("%s -> %s: ERROR %s", src, dst, result.error)
            else:
                avg_ms = (
                    sum(result.rtts_ms) / len(result.rtts_ms)
                    if result.rtts_ms
                    else 0
                )
                log.info(
                    "%s -> %s: %d/%d received, avg %.0fms, loss %.1f%%",
                    src, dst, result.received, result.sent, avg_ms, result.loss,
                )

    elapsed = time.time() - round_start
    log.info("Probe round complete in %.1fs", elapsed)
    return elapsed


def main(argv=None):
    args = parse_args(argv)

    level = logging.WARNING
    if args.verbose >= 2:
        level = logging.DEBUG
    elif args.verbose >= 1:
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    relays = read_relay_list(args.relays)
    log.info("Loaded %d relays: %s", len(relays), ", ".join(relays))
    log.info(
        "Pairs: %d, count: %d, interval: %ds, workers: %d",
        len(relays) ** 2, args.count, args.interval, args.workers,
    )

    cache_dir = Path(args.cache_dir).expanduser()
    cache_dir.mkdir(parents=True, exist_ok=True)

    if args.port:
        start_exporter_server(args.port)

    while True:
        elapsed = run_round(relays, args)

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


if __name__ == "__main__":
    main()
