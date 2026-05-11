"""IMAP METADATA (RFC 5464) reader for the chatmail prober.

deltachat-rpc-client does not expose every IMAP METADATA entry that
chatmail relays publish: notably `/shared/vendor/deltachat/irohrelay`
has no rpc accessor.  Rather than wait for a core patch, this module
opens a parallel IMAP4_SSL session using the credentials stored in
the already-configured account and issues GETMETADATA directly.

The helper is intentionally tiny -- one entry per call, no caching --
because METADATA reads happen at most once per relay per alive-check
cycle (default 24h) and adding ~30 lines of stdlib code beats taking
a dependency on imapclient.
"""

from __future__ import annotations

import imaplib
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from .log_config import get_logger

log = get_logger(__name__)

# imaplib's Commands dict pre-dates RFC 5464; register METADATA verbs so
# _simple_command doesn't reject them with KeyError.  Permitted states
# match the RFC: any post-authentication state.
imaplib.Commands.setdefault("GETMETADATA", ("AUTH", "SELECTED"))
imaplib.Commands.setdefault("SETMETADATA", ("AUTH", "SELECTED"))


class ImapMetadataError(Exception):
    """Raised when the IMAP transport (connect/login) fails.

    Distinct from `None` returns of `fetch_metadata_entry`, which mean
    "IMAP session worked, server did not advertise this entry".
    """


@dataclass(frozen=True, slots=True)
class ImapCreds:
    """IMAP credentials harvested from a configured deltachat Account."""
    host: str
    port: int
    user: str
    password: str


def creds_from_account(account: Any) -> ImapCreds | None:
    """Derive IMAP credentials from a chatmail-configured account.

    chatmail's QR setup leaves configured_mail_server / _port / _user
    empty and relies on the standard layout:
      host = domain of configured_addr
      port = 993 (IMAPS)
      user = full configured_addr
      password = mail_pw

    We follow that same derivation here instead of reading the
    configured_mail_* keys (which would only be populated if a user
    overrode them by hand).  Returns None if addr/password are
    missing -- i.e. the account is not configured yet.
    """
    addr = account.get_config("configured_addr") or account.get_config("addr")
    password = account.get_config("mail_pw")
    if not addr or not password or "@" not in addr:
        return None
    host = addr.split("@", 1)[1]
    return ImapCreds(host=host, port=993, user=addr, password=password)


# Captures `<entry> "value"`.  NIL and absent entries fall through to None.
# Literal form ({N}\r\n<bytes>) arrives as a tuple, handled separately.
@lru_cache(maxsize=8)
def _quoted_value_pattern(entry: str) -> re.Pattern[bytes]:
    return re.compile(
        re.escape(entry).encode() + rb'\s+"((?:[^"\\]|\\.)*)"',
    )


def _extract_from_line(line: Any, entry: str) -> str | None:
    """Return the value of `entry` from one METADATA response line, or None.

    `line` is whatever imaplib produced: either bytes (no literal) or
    a (bytes, bytes) tuple (literal value).
    """
    if isinstance(line, tuple) and len(line) >= 2:
        header, literal = line[0], line[1]
        if entry.encode() in header and isinstance(literal, (bytes, bytearray)):
            return bytes(literal).decode("utf-8", errors="replace")
        return None
    if not isinstance(line, (bytes, bytearray)):
        return None
    blob = bytes(line)
    m = _quoted_value_pattern(entry).search(blob)
    if m:
        return (
            m.group(1)
            .decode("utf-8", errors="replace")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )
    return None  # NIL or entry absent -> no value


def fetch_metadata_entry(
    creds: ImapCreds, entry: str, timeout: float = 15.0,
) -> str | None:
    """Open an IMAP session, GETMETADATA `entry`, return its value or None.

    Returns None if the server replies but does not advertise `entry`
    (or returns NIL).  Raises ImapMetadataError on connect/login or
    other transport failure.
    """
    try:
        conn = imaplib.IMAP4_SSL(creds.host, creds.port, timeout=timeout)
    except (OSError, imaplib.IMAP4.error) as e:
        raise ImapMetadataError(f"imap connect failed: {e}") from e
    try:
        try:
            conn.login(creds.user, creds.password)
            typ, _ = conn._simple_command("GETMETADATA", '""', entry)
            if typ != "OK":
                raise ImapMetadataError(f"GETMETADATA returned {typ}")
            _, lines = conn._untagged_response(typ, [None], "METADATA")
        except (OSError, imaplib.IMAP4.error) as e:
            raise ImapMetadataError(f"imap session failed: {e}") from e
        # Real-world response shapes from chatmail (Dovecot), captured live:
        #   value present : [(b'"" (/path {N}', b'<N bytes>'), b')']
        #   value is NIL  : [b'"" (/path NIL)']
        #   entry absent  : []
        for line in lines or ():
            value = _extract_from_line(line, entry)
            if value is not None:
                return value
        return None
    finally:
        try:
            conn.logout()
        except (OSError, imaplib.IMAP4.error) as e:
            log.debug("imap_logout_failed", error=str(e))
