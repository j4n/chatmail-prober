"""Per-relay iroh-relay health check.

Resolves a relay's iroh-relay URL from IMAP METADATA
(`/shared/vendor/deltachat/irohrelay`) via the imap_metadata helper,
then performs a plain HTTP GET to verify the relay is reachable.

The iroh-relay speaks HTTP (no auth, no protocol handshake required
for a liveness probe); a 2xx response is enough signal that the
service is up.  Mirror shape of turn.py so the orchestration glue is
symmetric.
"""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from .imap_metadata import (
    ImapMetadataError,
    creds_from_account,
    fetch_metadata_entry,
)
from .log_config import get_logger

log = get_logger(__name__)

# Verbatim from core src/imap.rs:1517-1525 (fetch_metadata).
IROH_METADATA_KEY = "/shared/vendor/deltachat/irohrelay"


class IrohStatus(IntEnum):
    """Sentinel values exposed via cmping_relay_iroh_status."""
    OK = 1
    DOWN = 0
    NO_METADATA = -2
    IMAP_FAILED = -3
    TIMEOUT = -5


@dataclass(frozen=True, slots=True)
class IrohResult:
    """Outcome of one iroh-relay probe round."""
    status: IrohStatus
    url: str | None = None
    latency_s: float | None = None
    http_status: int | None = None
    error: str | None = None


def resolve_iroh_url(
    account: Any, imap_timeout: float = 15.0,
) -> str | None:
    """Fetch the iroh-relay URL from the account's IMAP METADATA.

    Returns None if the server replies but does not advertise the entry
    (NIL or absent).  Raises ImapMetadataError on connect/login failure
    so the caller can distinguish IMAP_FAILED (-3) from NO_METADATA (-2).
    """
    creds = creds_from_account(account)
    if creds is None:
        return None
    return fetch_metadata_entry(creds, IROH_METADATA_KEY, timeout=imap_timeout)


_USER_AGENT = "chatmail-prober/1.0"


def check_iroh(url: str, timeout: float = 15.0) -> IrohResult:
    """HTTP GET `url`, classify outcome into an IrohResult."""
    start = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed = time.monotonic() - start
            code = resp.status
            status = IrohStatus.OK if 200 <= code < 300 else IrohStatus.DOWN
            return IrohResult(
                status=status, url=url,
                latency_s=elapsed, http_status=code,
                error=None if status == IrohStatus.OK else f"http {code}",
            )
    except urllib.error.HTTPError as e:
        return IrohResult(
            status=IrohStatus.DOWN, url=url,
            latency_s=time.monotonic() - start,
            http_status=e.code, error=f"http {e.code}",
        )
    except socket.timeout:
        return IrohResult(status=IrohStatus.TIMEOUT, url=url, error="timeout")
    except urllib.error.URLError as e:
        # socket.timeout wrapped by URLError in older Python; check too.
        if isinstance(e.reason, socket.timeout):
            return IrohResult(status=IrohStatus.TIMEOUT, url=url, error="timeout")
        return IrohResult(status=IrohStatus.DOWN, url=url, error=str(e.reason))


def check_relay_iroh(
    account: Any, imap_timeout: float = 15.0, http_timeout: float = 15.0,
) -> IrohResult:
    """End-to-end: resolve URL via IMAP, probe via HTTP.

    One-call entry point that maps each failure mode to its sentinel.
    """
    try:
        url = resolve_iroh_url(account, imap_timeout=imap_timeout)
    except ImapMetadataError as e:
        return IrohResult(status=IrohStatus.IMAP_FAILED, error=str(e))
    if not url:
        return IrohResult(status=IrohStatus.NO_METADATA)
    return check_iroh(url, timeout=http_timeout)
