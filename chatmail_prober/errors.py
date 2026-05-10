"""Probe failure types and classification.

Kept as a small leaf module so accounts/rpc/probe can all import it
without creating cycles.
"""

from __future__ import annotations


class PingError(Exception):
    """Raised when a probe encounters a non-recoverable error."""


# Error categories that won't resolve by waiting longer -- fail fast.
_FATAL_CATEGORIES = frozenset({"dns", "tls", "auth", "connection_refused"})


# Ordered: first matching category wins. Python preserves dict insertion
# order, so iteration order here is the precedence order.
_FAILURE_CATEGORY_MAP: dict[str, tuple[str, ...]] = {
    "timeout":            ("timeout", "timed out", "deadline"),
    "connection_refused": ("connection refused", "connectionrefused"),
    "dns":                ("name or service not known", "getaddrinfo",
                           "dns resolution", "no such host", "nxdomain"),
    "tls":                ("ssl", "certificate"),
    "auth":               ("auth", "authentication"),
    "setup":              ("failed to setup",),
}


def _classify_error(error: str | None) -> str | None:
    """Map a probe error string to a failure category label.

    Returns one of ``"timeout"``, ``"connection_refused"``, ``"dns"``,
    ``"tls"``, ``"auth"``, ``"setup"``, or ``"unknown"``; returns ``None``
    when *error* is ``None`` (i.e. the probe succeeded).
    """
    if error is None:
        return None
    lower = error.lower()
    for category, keywords in _FAILURE_CATEGORY_MAP.items():
        if any(kw in lower for kw in keywords):
            return category
    return "unknown"
