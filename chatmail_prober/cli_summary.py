"""cli_summary — compact table summary for --print / --once mode.

Renders a human-readable table to *out* (default: sys.stdout) after a
single probe round completes.  Each probe pair occupies one row::

    Route                                    Sent  Recv  Loss    p50    p90    p99   mdev  Setup   Msg
    nine.testrun.org -> nine.testrun.org        3     3   0.0%  2271ms  2316ms  2326ms  431ms  4850.00ms  3240.00ms
    nine.testrun.org -> foobar.com              3     3   0.0%  2571ms  2614ms  2624ms  443ms  4850.00ms  3240.00ms

If any relays failed the alive-check, a second table lists them::

    Dead relays (1)
    Host              Error    Message
    bar.foo.baz       auth     AUTHENTICATIONFAILED: login failed

    Alive: 1/2  Probes: 1/1 ok  Elapsed: 9.5s
"""
from __future__ import annotations

import shutil
import sys
from collections import defaultdict
from typing import IO, Mapping, Sequence

from chatmail_prober.prober import ProbeResult

# Column widths (characters).  Route column is computed dynamically.
_W_SENT  = 5
_W_RECV  = 5
_W_LOSS  = 6
_W_RTT   = 8   # p50 / p90 / p99 / mdev (e.g. "12345ms")
_W_TIME  = 10  # Setup / Msg (e.g. "12345.67ms")

# Dead-relay table column widths (computed dynamically for Host; fixed for others)
_W_ERR_CAT = 12   # Error category (e.g. "auth", "timeout")


def _rtt(value: float | None) -> str:
    """Format an RTT value (ms) as an integer with 'ms' suffix, or '-'."""
    return "-" if value is None else f"{value:.0f}ms"


def _time(value: float) -> str:
    """Format a timing value (seconds) as milliseconds: '123.23ms'."""
    return f"{value * 1000:.2f}ms"


def _loss(value: float) -> str:
    """Format a loss percentage to one decimal place."""
    return f"{value:.1f}%"


def _error_category(error: str | None) -> str:
    """Extract a short category label from a raw error string."""
    if error is None:
        return "unknown"
    e = error.lower()
    if "authenticationfailed" in e or "auth" in e:
        return "auth"
    if "timeout" in e or "deadline" in e or "timed out" in e:
        return "timeout"
    if "name or service not known" in e or "dns" in e or "getaddrinfo" in e:
        return "dns"
    if "certificate" in e or "tls" in e or "ssl" in e:
        return "tls"
    if "connection refused" in e:
        return "refused"
    return "unknown"


def _truncate(s: str, width: int) -> str:
    """Truncate *s* to *width* characters, appending '…' if truncated."""
    if len(s) <= width:
        return s
    return s[: width - 1] + "…"


def render(
    results: Sequence[ProbeResult],
    alive_relays: Sequence[str],
    dead_relays: Mapping[str, str | None],
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
        Mapping of relay hostname → raw error string (or ``None``) for
        every relay that failed the alive check.
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
    # Probe-level failure block (grouped by category, below the table)
    # ------------------------------------------------------------------
    out.write("\n")
    if failed_results:
        by_category: dict[str, list[ProbeResult]] = defaultdict(list)
        for r in failed_results:
            by_category[r.failure_category or "unknown"].append(r)
        out.write(f"Probe failures ({len(failed_results)})\n")
        for category, items in sorted(by_category.items()):
            pairs = ", ".join(f"{r.source} -> {r.destination}" for r in items)
            out.write(f"  {category}: {pairs}\n")
        out.write("\n")

    # ------------------------------------------------------------------
    # Dead-relay table (alive-check failures)
    # ------------------------------------------------------------------
    if dead_relays:
        # Compute column widths dynamically.
        host_w = max(len(h) for h in dead_relays)
        host_w = max(host_w, len("Host"))

        # Truncate the message column to fill the terminal width.
        # Fixed columns: host_w + 2 + _W_ERR_CAT + 2 + len("Message") header.
        term_w = shutil.get_terminal_size(fallback=(0, 0)).columns
        fixed_w = host_w + 2 + _W_ERR_CAT + 2
        msg_w = max(20, term_w - fixed_w) if term_w > 0 else 0  # 0 = no truncation

        dh = f"{'Host':<{host_w}}  {'Error':<{_W_ERR_CAT}}  {'Message'}"
        out.write(f"Dead relays ({len(dead_relays)})\n")
        out.write(f"{dh}\n")
        out.write(f"{'-' * len(dh)}\n")
        for host, error in dead_relays.items():
            category = _error_category(error)
            raw_msg  = error or "(no message)"
            message  = _truncate(raw_msg, msg_w) if msg_w > 0 else raw_msg
            out.write(f"{host:<{host_w}}  {category:<{_W_ERR_CAT}}  {message}\n")
        out.write("\n")

    # ------------------------------------------------------------------
    # Summary footer
    # ------------------------------------------------------------------
    total_relays = len(alive_relays) + len(dead_relays)
    alive_n      = len(alive_relays)
    total_probes = len(results)
    ok_cnt       = len(ok_results)
    out.write(
        f"Alive: {alive_n}/{total_relays}"
        f"  Probes: {ok_cnt}/{total_probes} ok"
        f"  Elapsed: {elapsed_s:.1f}s\n"
    )
