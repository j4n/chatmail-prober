"""cli_summary — compact table summary for --print / --once mode.

Renders a human-readable table to *out* (default: sys.stdout) after a
single probe round completes.  Each probe pair occupies one row::

    Route                                    Sent  Recv  Loss    p50    p90    p99   mdev  Setup   Msg
    nine.testrun.org -> nine.testrun.org        3     3   0.0%  2271   2316   2326    431   4.8s   3.2s
    nine.testrun.org -> mailchat.pl             3     3   0.0%  2571   2614   2624    443   4.8s   3.2s
    mailchat.pl      -> nine.testrun.org        3     3   0.0%  2860   3486   3627    785   4.8s   3.2s
    mailchat.pl      -> mailchat.pl             3     3   0.0%  2719   3356   3499    826   4.8s   3.2s

    Failures (0)

    Alive: 2  Dead: 0  Probes: 4/4 ok  Elapsed: 9.5s
"""
from __future__ import annotations

import sys
from collections import defaultdict
from typing import IO, Sequence

from chatmail_prober.prober import ProbeResult

# Column widths (characters).  Route column is computed dynamically.
_W_SENT  = 5
_W_RECV  = 5
_W_LOSS  = 6
_W_RTT   = 6   # p50 / p90 / p99 / mdev
_W_TIME  = 10  # Setup / Msg (e.g. "12345.67ms")


def _rtt(value: float | None) -> str:
    """Format an RTT value (ms) as a right-aligned integer string, or '-'."""
    return "-" if value is None else f"{value:.0f}"


def _time(value: float) -> str:
    """Format a timing value (seconds) as milliseconds: '123.23ms'."""
    return f"{value * 1000:.2f}ms"


def _loss(value: float) -> str:
    """Format a loss percentage to one decimal place."""
    return f"{value:.1f}%"


def render(
    results: Sequence[ProbeResult],
    alive_relays: Sequence[str],
    dead_relays: Sequence[str],
    elapsed_s: float,
    out: IO[str] | None = None,
) -> None:
    """Render a compact table summary to *out*.

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

    ok_results     = [r for r in results if r.error is None]
    failed_results = [r for r in results if r.error is not None]

    # ------------------------------------------------------------------
    # Compute route column width so all rows line up.
    # ------------------------------------------------------------------
    route_strs = [f"{r.source} -> {r.destination}" for r in results]
    route_w = max((len(s) for s in route_strs), default=5)
    route_w = max(route_w, len("Route"))

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------
    header = (
        f"{'Route':<{route_w}}"
        f"  {'Sent':>{_W_SENT}}"
        f"  {'Recv':>{_W_RECV}}"
        f"  {'Loss':>{_W_LOSS}}"
        f"  {'p50':>{_W_RTT}}"
        f"  {'p90':>{_W_RTT}}"
        f"  {'p99':>{_W_RTT}}"
        f"  {'mdev':>{_W_RTT}}"
        f"  {'Setup':>{_W_TIME}}"
        f"  {'Msg':>{_W_TIME}}"
    )
    separator = "-" * len(header)
    out.write(f"\n{header}\n{separator}\n")

    # ------------------------------------------------------------------
    # Rows
    # ------------------------------------------------------------------
    for route, r in zip(route_strs, results):
        if r.error is None:
            # Successful probe — show full stats.
            row = (
                f"{route:<{route_w}}"
                f"  {r.sent:>{_W_SENT}}"
                f"  {r.received:>{_W_RECV}}"
                f"  {_loss(r.loss):>{_W_LOSS}}"
                f"  {_rtt(r.p50_ms):>{_W_RTT}}"
                f"  {_rtt(r.p90_ms):>{_W_RTT}}"
                f"  {_rtt(r.p99_ms):>{_W_RTT}}"
                f"  {_rtt(r.mdev_ms):>{_W_RTT}}"
                f"  {_time(r.account_setup_time):>{_W_TIME}}"
                f"  {_time(r.message_time):>{_W_TIME}}"
            )
        else:
            # Failed probe — show category in the p50 column, dashes elsewhere.
            category = r.failure_category or "unknown"
            row = (
                f"{route:<{route_w}}"
                f"  {r.sent:>{_W_SENT}}"
                f"  {r.received:>{_W_RECV}}"
                f"  {'100.0%':>{_W_LOSS}}"
                f"  {category:>{_W_RTT}}"
                f"  {'-':>{_W_RTT}}"
                f"  {'-':>{_W_RTT}}"
                f"  {'-':>{_W_RTT}}"
                f"  {'-':>{_W_TIME}}"
                f"  {'-':>{_W_TIME}}"
            )
        out.write(f"{row}\n")

    # ------------------------------------------------------------------
    # Failures block (grouped by category, below the table)
    # ------------------------------------------------------------------
    out.write("\n")
    if failed_results:
        by_category: dict[str, list[ProbeResult]] = defaultdict(list)
        for r in failed_results:
            by_category[r.failure_category or "unknown"].append(r)
        out.write(f"Failures ({len(failed_results)})\n")
        for category, items in sorted(by_category.items()):
            pairs = ", ".join(f"{r.source} -> {r.destination}" for r in items)
            out.write(f"  {category}: {pairs}\n")
        out.write("\n")

    # ------------------------------------------------------------------
    # Summary footer
    # ------------------------------------------------------------------
    total   = len(results)
    ok_cnt  = len(ok_results)
    alive_n = len(alive_relays)
    dead_n  = len(dead_relays)
    out.write(
        f"Alive: {alive_n}  Dead: {dead_n}"
        f"  Probes: {ok_cnt}/{total} ok"
        f"  Elapsed: {elapsed_s:.1f}s\n"
    )
