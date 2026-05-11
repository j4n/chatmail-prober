"""Per-relay TURN health check.

Resolves a relay's TURN coordinates via Account.ice_servers(), runs
turnutils_uclient against them, and turns the parsed output into a
TurnResult ready to feed into prometheus.  Parsing and the subprocess
invocation live in turn_parse so the standalone textfile collector
script can reuse them without pulling in any prober deps.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Literal

from .log_config import get_logger
from .turn_parse import TurnRun, run_uclient

log = get_logger(__name__)


class TurnStatus(IntEnum):
    """Sentinel values exposed via cmping_relay_turn_status."""
    OK = 1
    DOWN = 0
    PARSE_ERROR = -2
    BINARY_MISSING = -4
    TIMEOUT = -5


TurnEndpointKind = Literal["self", "fallback"]
TurnResolved = tuple[str, int, str, str, TurnEndpointKind]

# Verbatim from core src/calls.rs:741-761 (create_fallback_ice_servers).
# Used when a relay does not publish its own TURN metadata and core
# substitutes this public fallback.
FALLBACK_TURN: tuple[str, int, str, str] = (
    "turn.delta.chat", 3478, "public", "o4tR7yG4rG2slhXqRUf9zgmHz",
)


@dataclass(frozen=True, slots=True)
class TurnResult:
    """TurnRun plus the labels needed to write metrics."""
    run: TurnRun
    endpoint_kind: TurnEndpointKind

    @property
    def status_code(self) -> TurnStatus:
        match self.run.error:
            case "binary-missing":
                return TurnStatus.BINARY_MISSING
            case "timeout":
                return TurnStatus.TIMEOUT
            case _:
                return TurnStatus.OK if self.run.ok else TurnStatus.DOWN


def _parse_turn_url(url: str) -> tuple[str, int] | None:
    """Parse a `turn:<host>:<port>` ICE URL into (host, port).

    Handles three shapes core emits (calls.rs:712-721):
      - turn:<ipv4>:<port>
      - turn:[<ipv6>]:<port>
      - turn:<hostname>:<port>   (in case core ever stops resolving)

    Returns None when the URL is not a TURN URL or cannot be parsed.
    """
    if not url.startswith("turn:"):
        return None
    rest = url[len("turn:"):]
    if rest.startswith("["):
        end = rest.find("]")
        if end == -1:
            return None
        host = rest[1:end]
        after = rest[end + 1:]
        if not after.startswith(":"):
            return None
        try:
            return host, int(after[1:])
        except ValueError:
            return None
    # IPv4 / hostname: split on last colon.
    sep = rest.rfind(":")
    if sep == -1:
        return None
    try:
        return rest[:sep], int(rest[sep + 1:])
    except ValueError:
        return None


def _is_ipv4(host: str) -> bool:
    """True if `host` is a valid IPv4 literal."""
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv4Address)
    except ValueError:
        return False


def _extract_turn_endpoint(entry: dict[str, Any]) -> tuple[str, int] | None:
    """Pick a (host, port) from an ice_servers() entry.

    Prefers IPv4 URLs because we run uclient with -X (force IPv4 relay).
    Falls back to the first usable TURN URL if no IPv4 is present.
    """
    urls = entry.get("urls") or []
    ipv4: tuple[str, int] | None = None
    any_url: tuple[str, int] | None = None
    for u in urls:
        if not isinstance(u, str):
            continue
        parsed = _parse_turn_url(u)
        if parsed is None:
            continue
        if any_url is None:
            any_url = parsed
        if _is_ipv4(parsed[0]):
            ipv4 = parsed
            break
    return ipv4 or any_url


def resolve_relay_turn(
    account: Any, relay_domain: str,
) -> TurnResolved | None:
    """Resolve (host, port, username, credential, endpoint_kind) for one relay.

    Returns None if Account.ice_servers() is empty or malformed (caller
    reports status=-2).  Otherwise returns either:
      - the relay's own TURN entry, endpoint_kind="self"
      - the public fallback, endpoint_kind="fallback"

    Fallback detection: core publishes the fallback with username="public"
    (calls.rs:756).  Self-published TURN credentials use the expiry
    timestamp as the username, which is always a numeric string.
    """
    try:
        servers = account.ice_servers()
    except Exception as e:
        log.warning("turn_ice_servers_failed", relay=relay_domain, error=str(e))
        return None
    if not isinstance(servers, list) or not servers:
        return None

    self_entry: dict[str, Any] | None = None
    fallback_entry: dict[str, Any] | None = None
    for entry in servers:
        if not isinstance(entry, dict):
            continue
        user = entry.get("username")
        if user is None:
            continue  # STUN entry, no credentials
        if user == "public":
            fallback_entry = entry
        elif isinstance(user, str) and user.isdigit():
            self_entry = entry

    chosen = self_entry or fallback_entry
    if chosen is None:
        return None

    endpoint = _extract_turn_endpoint(chosen)
    user = chosen.get("username")
    cred = chosen.get("credential")
    if endpoint is None or not user or not cred:
        return None
    host, port = endpoint
    kind: TurnEndpointKind = "self" if chosen is self_entry else "fallback"
    return host, port, user, cred, kind


def check_turn(resolved: TurnResolved, timeout: float = 30.0) -> TurnResult:
    """Run turnutils_uclient against a resolved (host, port, user, cred, kind)."""
    host, port, user, cred, kind = resolved
    return TurnResult(
        run=run_uclient(host, port, user, cred, timeout=timeout),
        endpoint_kind=kind,
    )
