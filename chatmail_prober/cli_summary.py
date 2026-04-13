"""cli_summary — gocmping-style terminal summary for --once mode.

Renders a human-readable block to *out* (default: sys.stdout) after a
single probe round completes.  The output mirrors the gocmping layout:

    Packet Statistics
      route           : src -> dst
      transmitted     : 5 / received : 5 / loss : 0.00%

    RTT Statistics (ms)
      min: 900  p50: 1050  p90: 1180  p95: 1190  p99: 1198  avg: 1050  mdev: 108

    Timing
      account setup   : 3.50s
      message send/recv : 2.10s

    Failures (1)
      timeout         : a.example -> b.example

    Summary
      alive: 2  dead: 1  probes: 4/5 ok  elapsed: 42.3s
"""
from __future__ import annotations

import sys
from collections import defaultdict
from typing import IO, Sequence

from chatmail_prober.prober import ProbeResult


def _fmt(value: float | None, decimals: int = 0) -> str:
    """Format a float to *decimals* places, or 'n/a' if None."""
    if value is None:
        return "n/a"
    return f"{value:.{decimals}f}"


def _section(title: str, out: IO[str]) -> None:
    out.write(f"\n{title}\n")


def _row(label: str, value: str, out: IO[str], indent: int = 2) -> None:
    pad = " " * indent
    out.write(f"{pad}{label:<22}: {value}\n")


def render(
    results: Sequence[ProbeResult],
    alive_relays: Sequence[str],
    dead_relays: Sequence[str],
    elapsed_s: float,
    out: IO[str] | None = None,
) -> None:
    """Render a gocmping-style summary block to *out*.

    Parameters
    ----------
    results:
        All ``ProbeResult`` objects from the completed round.
    alive_relays:
        Relay hostnames confirmed reachable during the alive check.
    dead_relays:
        Relay hostnames that failed the alive check.
    elapsed_s:
        Total wall-clock time for the round in seconds.
    out:
        File-like object to write to.  Defaults to ``sys.stdout``.
    """
    if out is None:
        out = sys.stdout

    ok_results = [r for r in results if r.error is None]
    failed_results = [r for r in results if r.error is not None]

    # ------------------------------------------------------------------
    # Packet Statistics
    # ------------------------------------------------------------------
    _section("Packet Statistics", out)
    for r in results:
        _row("route", f"{r.source} -> {r.destination}", out)
        loss_str = f"{r.loss:.2f}%"
        _row(
            "transmitted",
            f"{r.sent} / received : {r.received} / loss : {loss_str}",
            out,
        )

    # ------------------------------------------------------------------
    # RTT Statistics
    # ------------------------------------------------------------------
    if ok_results:
        _section("RTT Statistics (ms)", out)
        for r in ok_results:
            if not r.rtts_ms:
                continue
            min_ms = min(r.rtts_ms)
            rtt_line = (
                f"min: {_fmt(min_ms)}  "
                f"p50: {_fmt(r.p50_ms)}  "
                f"p90: {_fmt(r.p90_ms)}  "
                f"p95: {_fmt(r.p95_ms)}  "
                f"p99: {_fmt(r.p99_ms)}  "
                f"avg: {_fmt(r.avg_ms)}  "
                f"mdev: {_fmt(r.mdev_ms)}"
            )
            _row(f"{r.source} -> {r.destination}", rtt_line, out)

    # ------------------------------------------------------------------
    # Timing
    # ------------------------------------------------------------------
    _section("Timing", out)
    if ok_results:
        avg_setup = sum(r.account_setup_time for r in ok_results) / len(ok_results)
        avg_msg = sum(r.message_time for r in ok_results) / len(ok_results)
        _row("account setup", f"{avg_setup:.2f}s", out)
        _row("message send/recv", f"{avg_msg:.2f}s", out)
    else:
        _row("account setup", "n/a", out)
        _row("message send/recv", "n/a", out)

    # ------------------------------------------------------------------
    # Failures
    # ------------------------------------------------------------------
    if failed_results:
        _section(f"Failures ({len(failed_results)})", out)
        by_category: dict[str, list[ProbeResult]] = defaultdict(list)
        for r in failed_results:
            category = r.failure_category or "unknown"
            by_category[category].append(r)
        for category, items in sorted(by_category.items()):
            pairs = ", ".join(f"{r.source} -> {r.destination}" for r in items)
            _row(category, pairs, out)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    _section("Summary", out)
    total_probes = len(results)
    ok_count = len(ok_results)
    _row("alive", str(len(alive_relays)), out)
    _row("dead", str(len(dead_relays)), out)
    _row("probes", f"{ok_count}/{total_probes} ok", out)
    _row("elapsed", f"{elapsed_s:.1f}s", out)
    out.write("\n")
