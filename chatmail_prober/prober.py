"""Direct-ping logic for chatmail relay probing.

Vendors the minimal subset of cmping needed for 1:1 direct pinging
with deltachat-rpc-client.  No group mode, no CLI output, no verbose
gating -- just structured logging.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import os
import queue
import random
import statistics
import shutil
import string
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from deltachat_rpc_client import AttrDict, DeltaChat, EventType, Rpc


def _ensure_venv_on_path():
    """Add the venv's bin dir to PATH so deltachat-rpc-server is found."""
    bin_dir = str(Path(sys.executable).parent)
    path = os.environ.get("PATH", "")
    if bin_dir not in path.split(os.pathsep):
        os.environ["PATH"] = bin_dir + os.pathsep + path


_ensure_venv_on_path()

from chatmail_prober.log_config import get_logger
log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Vendored helpers (from cmping.py, simplified)
# ---------------------------------------------------------------------------

class PingError(Exception):
    """Raised when a probe encounters a non-recoverable error."""


# Error categories that won't resolve by waiting longer -- fail fast.
_FATAL_CATEGORIES = frozenset({"dns", "tls", "auth", "connection_refused"})


def is_ip_address(host: str) -> bool:
    """Check if the given host is an IP address."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def generate_credentials() -> tuple[str, str]:
    """Generate random username and password for IP-based login."""
    chars = string.ascii_lowercase + string.digits
    username = "".join(random.choices(chars, k=12))
    password = "".join(random.choices(chars, k=20))
    return username, password


def create_qr_url(domain_or_ip: str) -> str:
    """Create either a dcaccount or dclogin URL based on input type."""
    if is_ip_address(domain_or_ip):
        username, password = generate_credentials()
        encoded_password = urllib.parse.quote(password, safe="")
        qr_url = (
            f"dclogin:{username}@{domain_or_ip}/?"
            f"p={encoded_password}&v=1&ip=993&sp=465&ic=3&ss=default"
        )
        return qr_url
    else:
        return f"dcaccount:{domain_or_ip}"


# ---------------------------------------------------------------------------
# RelayContext / AccountMaker -- manage RPC lifecycle and account creation
# ---------------------------------------------------------------------------

_CLOSE_TIMEOUT = 5


def _ensure_process_dead(rpc):
    """Kill the rpc-server child process if it is still running."""
    proc = getattr(rpc, "process", None)
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    except Exception:
        pass


def _close_rpc(rpc, label):
    try:
        rpc.__exit__(None, None, None)
    except Exception as e:
        log.warning("cleanup failed for %s: %s", label, e)


def _close_rpc_with_kill(rpc, label):
    closer = threading.Thread(
        target=_close_rpc, args=(rpc, label), daemon=True)
    closer.start()
    closer.join(timeout=_CLOSE_TIMEOUT)
    if closer.is_alive():
        log.warning("close timed out for %s, killing process", label)
    _ensure_process_dead(rpc)


class RelayContext:
    """Context for a relay: RPC connection, DeltaChat instance, AccountMaker.

    Can be used as a context manager for automatic cleanup, or managed
    manually via open()/close() for long-lived relay pools.
    """

    def __init__(self, relay: str, accounts_dir: str | Path) -> None:
        self.relay = relay
        self.accounts_dir = Path(accounts_dir)
        self.rpc = None
        self.dc = None
        self.maker = None

    def open(self) -> RelayContext:
        """Start the RPC server and initialize DeltaChat + AccountMaker."""
        if self.accounts_dir.exists() and not self.accounts_dir.joinpath("accounts.toml").exists():
            shutil.rmtree(self.accounts_dir)
        rpc = Rpc(accounts_dir=self.accounts_dir)
        rpc.__enter__()  # Rpc has no public open(); __enter__ is the intended API
        self.rpc = rpc
        self.dc = DeltaChat(self.rpc)
        self.maker = AccountMaker(self.dc)
        return self

    def close(self) -> None:
        """Shut down the RPC server, ensuring the child process is always killed.

        Rpc.close() can hang indefinitely (events_thread.join() with no
        timeout, blocking RPC calls).  We run it in a daemon thread with
        a deadline, then unconditionally kill the child process.
        """
        rpc = self.rpc
        if rpc is not None:
            self.rpc = None
            self.dc = None
            self.maker = None
            _close_rpc_with_kill(rpc, self.relay)

    def __enter__(self) -> RelayContext:
        return self.open()

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False


class AccountMaker:
    """Creates and manages deltachat accounts on a relay."""

    def __init__(self, dc: DeltaChat) -> None:
        self.dc = dc
        self.online = []

    def wait_account_online(self, account, timeout=None):
        """Wait for a single account to reach IMAP_INBOX_IDLE."""
        addr = account.get_config("addr") or account.get_config("configured_addr") or "unknown"
        log.debug("join_start", addr=addr)
        join_start = time.time()
        deadline = time.time() + timeout if timeout is not None else None
        eq = account._rpc.get_queue(account.id)
        while True:
            if deadline is not None and time.time() >= deadline:
                raise PingError(f"Timeout waiting for {addr} to come online")
            try:
                event = AttrDict(eq.get(timeout=1.0))
            except queue.Empty:
                continue
            if event.kind == EventType.IMAP_INBOX_IDLE:
                log.debug("join_done", addr=addr,
                          elapsed_s=round(time.time() - join_start, 3))
                return
            elif event.kind == EventType.ERROR:
                log.warning("ERROR during profile setup: %s", event.msg)
                if _classify_error(event.msg) in _FATAL_CATEGORIES:
                    raise PingError(event.msg)

    def _add_online(self, account):
        account.set_config("bot", "1")
        account.set_config("delete_device_after", "3600")
        account.set_config("delete_server_after", "3600")
        account.start_io()
        self.online.append(account)

    _MAX_ACCOUNTS_PER_DOMAIN = 3

    @staticmethod
    def _account_domain(account):
        """Extract the relay domain from an account, checking both
        configured_addr (fully configured) and addr (partially configured).
        Returns (domain, is_configured) or (None, False).
        """
        configured = account.get_config("configured_addr")
        if configured and "@" in configured:
            return configured.split("@")[1], True
        addr = account.get_config("addr")
        if addr and "@" in addr:
            return addr.split("@")[1], False
        return None, False

    def get_relay_account(self, domain, exclude=None, worker_id=None):
        """Get or create an account for domain.

        Returns (account, was_online) -- was_online=True means the account
        was already running and does not need wait_account_online().
        """
        _exclude = exclude or ()
        _wid = f"w{worker_id}" if worker_id is not None else "w?"

        # Reuse an already-online account for this domain.
        for account in self.online:
            if account in _exclude:
                continue
            addr = account.get_config("configured_addr")
            if addr and addr.split("@")[1] == domain:
                log.info("account_reused", relay=domain, addr=addr,
                         worker=_wid)
                return account, True

        # New account needed -- emit setup phase events.
        log.debug("setup_start", relay=domain, worker=_wid)
        setup_start = time.time()

        # Scan all accounts in the DB for this domain.  Track both
        # fully-configured and partially-configured (ghost) accounts
        # so ghosts count toward the per-domain limit.
        found = None
        found_unconfigured = None
        domain_count = 0
        unconfigured_count = 0
        for account in self.dc.get_all_accounts():
            if account in _exclude:
                continue
            acct_domain, is_configured = self._account_domain(account)
            if acct_domain != domain:
                continue
            domain_count += 1
            if not is_configured:
                unconfigured_count += 1
            if account not in self.online:
                if is_configured:
                    if found is None:
                        found = account
                else:
                    if found_unconfigured is None:
                        found_unconfigured = account

        if found is not None:
            addr = found.get_config("configured_addr") or "unknown"
            log.info("account_resumed", relay=domain, addr=addr,
                     worker=_wid, total=domain_count)
        elif found_unconfigured is not None:
            found = found_unconfigured
            log.info("account_resuming_unconfigured", relay=domain,
                     worker=_wid, total=domain_count,
                     unconfigured=unconfigured_count)
        else:
            if domain_count >= self._MAX_ACCOUNTS_PER_DOMAIN:
                raise PingError(
                    f"Too many accounts for {domain} ({domain_count}, "
                    f"{unconfigured_count} unconfigured), "
                    f"refusing to create more (limit {self._MAX_ACCOUNTS_PER_DOMAIN})"
                )
            found = self.dc.add_account()
            qr_url = create_qr_url(domain)
            found.set_config_from_qr(qr_url)
            from .metrics import account_creations_total
            account_creations_total.labels(relay=domain).inc()
            reason = "no_accounts" if domain_count == 0 else "all_online"
            log.warning("account_created", relay=domain,
                        total=domain_count + 1,
                        unconfigured=unconfigured_count,
                        reason=reason, worker=_wid)

        self._add_online(found)
        log.debug("setup_done", relay=domain, worker=_wid,
                  elapsed_s=round(time.time() - setup_start, 3))
        return found, False


# ---------------------------------------------------------------------------
# Pinger -- simplified for single receiver, 1:1 chat
# ---------------------------------------------------------------------------

class Pinger:
    """Sends ping messages via 1:1 chat and collects RTTs."""

    def __init__(self, sender, receiver, count, interval):
        self.sender = sender
        self.receiver = receiver
        self.count = count
        self.interval = interval

        self.addr1 = sender.get_config("addr")
        self.addr2 = receiver.get_config("addr")
        self.relay1 = self.addr1.split("@")[1]
        self.relay2 = self.addr2.split("@")[1]

        contact = sender.create_contact(receiver)
        self.chat = contact.create_chat()

        log.debug(
            "PING %s -> %s count=%d interval=%ss",
            self.relay1, self.relay2, count, interval,
        )
        ALPHANUMERIC = string.ascii_lowercase + string.digits
        self.tx = "".join(random.choices(ALPHANUMERIC, k=30))
        self.sent = 0
        self.received = 0
        self.results = []  # list of (seq, ms_duration)
        self.account_setup_time = 0.0
        self.message_time = 0.0
        self.deadline = None
        self._stop_event = threading.Event()
        self._send_thread = threading.Thread(target=self.send_pings, daemon=True)
        self._send_thread.start()

    @property
    def loss(self):
        expected = self.sent
        return 0.0 if expected == 0 else (1 - self.received / expected) * 100

    def send_pings(self):
        """Send pings at regular intervals (runs in a daemon thread)."""
        try:
            for seq in range(self.count):
                if self.deadline is not None and time.time() >= self.deadline:
                    break
                text = f"{self.tx} {time.time():.4f} {seq:17}"
                self.chat.send_text(text)
                self.sent += 1
                time.sleep(self.interval)
        except Exception as e:
            log.warning("send_pings error on %s -> %s: %s", self.relay1, self.relay2, e)
        finally:
            self._stop_event.set()

    def receive(self) -> Generator[tuple[int, float], None, None]:
        """Receive ping responses, yielding (seq, ms_duration) pairs."""
        num_pending = self.count
        received_seqs: set[int] = set()

        account_queue = self.receiver._rpc.get_queue(self.receiver.id)
        try:
            while num_pending > 0:
                if self.deadline is None and self._stop_event.is_set():
                    self.deadline = time.time() + 60.0
                if self.deadline is not None and time.time() >= self.deadline:
                    break
                try:
                    item = account_queue.get(timeout=1.0)
                    event = AttrDict(item)
                except queue.Empty:
                    continue

                if event.kind == EventType.INCOMING_MSG:
                    msg = self.receiver.get_message_by_id(event.msg_id)
                    text = msg.get_snapshot().text
                    parts = text.strip().split()
                    if len(parts) == 3 and parts[0] == self.tx:
                        seq = int(parts[2])
                        if seq not in received_seqs:
                            ms_duration = (time.time() - float(parts[1])) * 1000
                            self.received += 1
                            num_pending -= 1
                            received_seqs.add(seq)
                            yield seq, ms_duration
                elif event.kind == EventType.ERROR:
                    log.warning("ERROR during receive: %s", event.msg)
        finally:
            # Signal the sender to stop even if the caller breaks early,
            # preventing send_pings from blocking indefinitely on sleep.
            self._stop_event.set()


# ---------------------------------------------------------------------------
# _perform_direct_ping -- orchestrates a single 1:1 probe
# ---------------------------------------------------------------------------

def _perform_direct_ping(relay_contexts, source, dest, count, interval, timeout):
    """Run a direct 1:1 ping between two relays using shared contexts."""
    sender_ctx = relay_contexts[source]
    receiver_ctx = relay_contexts[dest]
    if sender_ctx.maker is None:
        raise PingError(f"Relay context for {source} is closed (being reopened)")
    if receiver_ctx.maker is None:
        raise PingError(f"Relay context for {dest} is closed (being reopened)")
    sender_maker = sender_ctx.maker
    receiver_maker = receiver_ctx.maker

    account_setup_start = time.time()

    try:
        sender, sender_was_online = sender_maker.get_relay_account(source)
    except Exception as e:
        raise PingError(
            f"Failed to setup sender profile on {source}: {type(e).__name__}: {e}"
        ) from e

    # For self-loops (source==dest), exclude sender so we get a different account.
    exclude = (sender,) if source == dest else None
    try:
        receiver, receiver_was_online = receiver_maker.get_relay_account(dest, exclude=exclude)
    except Exception as e:
        raise PingError(
            f"Failed to setup receiver profile on {dest}: {type(e).__name__}: {e}"
        ) from e

    # Only wait for accounts that were just brought online; already-online
    # accounts had their IMAP_INBOX_IDLE event consumed on a previous probe.
    needs_wait = []
    if not sender_was_online:
        needs_wait.append((sender_maker, sender))
    if not receiver_was_online:
        needs_wait.append((receiver_maker, receiver))

    if needs_wait:
        # Use a small executor so exceptions propagate cleanly via future.result()
        # instead of being collected in a shared list from daemon threads.
        with ThreadPoolExecutor(max_workers=len(needs_wait)) as pool:
            futures = [
                pool.submit(maker.wait_account_online, acct, timeout)
                for maker, acct in needs_wait
            ]
        # Executor.__exit__ waits for all futures; re-raise the first error.
        first_exc = next(
            (f.exception() for f in futures if f.exception() is not None), None
        )
        if first_exc is not None:
            raise PingError(
                f"Timeout or error waiting for profiles to be online: {first_exc}"
            ) from first_exc

    account_setup_time = time.time() - account_setup_start

    # Message phase
    message_start = time.time()
    pinger = Pinger(sender, receiver, count, interval)
    if timeout is not None:
        pinger.deadline = time.time() + timeout

    for seq, ms_duration in pinger.receive():
        pinger.results.append((seq, ms_duration))

    pinger._send_thread.join(timeout=2.0)
    message_time = time.time() - message_start

    pinger.account_setup_time = account_setup_time
    pinger.message_time = message_time
    return pinger


# ---------------------------------------------------------------------------
# RelayPool -- single shared RPC server for all relays in a worker
# ---------------------------------------------------------------------------

class RelayPool:
    """One RPC server per worker, shared across all relay domains.

    Exposes .maker / .rpc / .dc so it can duck-type as a relay context
    in _perform_direct_ping (which does relay_contexts[source].maker).
    """

    def __init__(self, cache_dir):
        self._cache_dir = Path(cache_dir)
        self.rpc = None
        self.dc = None
        self.maker = None
        self._relays = set()

    def _start_rpc(self):
        """Start the shared RPC server for all relays in this pool."""
        accts_dir = self._cache_dir
        if accts_dir.exists() and not accts_dir.joinpath("accounts.toml").exists():
            # Detect old per-relay layout (worker-N/relay.domain/accounts.toml).
            # Refuse to wipe -- accounts are recoverable via the migration script.
            has_subdirs = any(
                (d / "accounts.toml").exists()
                for d in accts_dir.iterdir()
                if d.is_dir()
            )
            if has_subdirs:
                raise SystemExit(
                    f"Old per-relay account layout detected in {accts_dir}.\n"
                    "Refusing to start. Choose one:\n"
                    "  scripts/migrate_accounts.py <cache_dir> --apply  "
                    "(preserve accounts)\n"
                    "  chatmail-prober ... --reset all                  "
                    "(start fresh)"
                )
        accts_dir.mkdir(parents=True, exist_ok=True)
        rpc = Rpc(accounts_dir=accts_dir)
        rpc.__enter__()
        self.rpc = rpc
        self.dc = DeltaChat(rpc)
        self.maker = AccountMaker(self.dc)

    def _shutdown_rpc(self):
        """Shut down the shared RPC server with a timeout, then kill."""
        rpc = self.rpc
        if rpc is None:
            return
        self.rpc = None
        self.dc = None
        self.maker = None
        _close_rpc_with_kill(rpc, self._cache_dir.name)

    def open_all(self, relays):
        """Ensure the shared RPC server is running, register relays."""
        if self.rpc is None:
            self._start_rpc()
        self._relays.update(relays)

    def contexts(self):
        """Return relay -> context dict; all relays share this pool."""
        return {r: self for r in self._relays}

    def reopen(self):
        """Restart the shared RPC server."""
        self._shutdown_rpc()
        self._start_rpc()
        log.info("pool: reopened shared rpc-server in %s", self._cache_dir)

    def prune(self, active_relays):
        """Forget relays no longer in the active set."""
        stale = self._relays - set(active_relays)
        if stale:
            log.info("pool: pruned relay(s): %s", ", ".join(sorted(stale)))
        self._relays &= set(active_relays)

    def close(self):
        """Close the shared RPC server."""
        self._shutdown_rpc()
        self._relays.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# ---------------------------------------------------------------------------
# ProbeResult / run_probe -- public API for the exporter
# ---------------------------------------------------------------------------

_FAILURE_CATEGORY_MAP: tuple[tuple[tuple[str, ...], str], ...] = (
    (("timeout", "timed out", "deadline"),       "timeout"),
    (("connection refused", "connectionrefused"), "connection_refused"),
    (("name or service not known", "getaddrinfo",
      "dns resolution", "no such host", "nxdomain"), "dns"),
    (("ssl", "certificate"),                       "tls"),
    (("auth", "authentication"),                   "auth"),
    (("failed to setup",),                         "setup"),
)


def _classify_error(error: str | None) -> str | None:
    """Map a probe error string to a failure category label.

    Returns one of ``"timeout"``, ``"connection_refused"``, ``"dns"``,
    ``"tls"``, ``"auth"``, ``"setup"``, or ``"unknown"``; returns ``None``
    when *error* is ``None`` (i.e. the probe succeeded).
    """
    if error is None:
        return None
    lower = error.lower()
    for keywords, category in _FAILURE_CATEGORY_MAP:
        if any(kw in lower for kw in keywords):
            return category
    return "unknown"


@dataclass
class ProbeResult:
    source: str
    destination: str
    sent: int = 0
    received: int = 0
    loss: float = 100.0  # used in log output and CLI; metrics recomputes from sent/received
    rtts_ms: list[float] = field(default_factory=list)
    account_setup_time: float = 0.0
    message_time: float = 0.0
    error: str | None = None
    failure_category: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.failure_category = _classify_error(self.error)

    # ------------------------------------------------------------------
    # Latency distribution helpers (all in milliseconds)
    # ------------------------------------------------------------------

    @property
    def avg_ms(self) -> float | None:
        """Mean RTT in ms, or None if no samples."""
        return statistics.mean(self.rtts_ms) if self.rtts_ms else None

    @property
    def p50_ms(self) -> float | None:
        """Median (p50) RTT in ms, or None if no samples."""
        return statistics.median(self.rtts_ms) if self.rtts_ms else None

    def _quantile(self, n: int) -> float | None:
        """Return the last cut-point of n-quantiles (i.e. p(100*(n-1)/n))."""
        if not self.rtts_ms:
            return None
        if len(self.rtts_ms) < 2:
            return self.rtts_ms[0]
        return statistics.quantiles(self.rtts_ms, n=n, method="inclusive")[-1]

    @property
    def p90_ms(self) -> float | None:
        """90th-percentile RTT in ms, or None if no samples."""
        return self._quantile(10)

    @property
    def p95_ms(self) -> float | None:
        """95th-percentile RTT in ms, or None if no samples."""
        return self._quantile(20)

    @property
    def p99_ms(self) -> float | None:
        """99th-percentile RTT in ms, or None if no samples."""
        return self._quantile(100)

    @property
    def mdev_ms(self) -> float | None:
        """Mean deviation (stddev) of RTTs in ms, or None if no samples."""
        if not self.rtts_ms:
            return None
        if len(self.rtts_ms) < 2:
            return 0.0
        return statistics.stdev(self.rtts_ms)


def run_probe(
    source: str,
    dest: str,
    count: int = 5,
    interval: float = 0.1,
    accounts_dir: str | Path = "~/.cache/chatmail-prober/worker-0",
    timeout: float = 60.0,
    relay_contexts: dict | None = None,
) -> ProbeResult:
    """Run a single direct-ping probe between two relays.

    When relay_contexts is provided (dict of relay -> open RelayContext),
    uses shared RPC connections.  Otherwise creates temporary contexts.
    """
    try:
        if relay_contexts is not None:
            pinger = _perform_direct_ping(relay_contexts, source, dest,
                                          count, interval, timeout)
        else:
            # Create temporary contexts for standalone use
            accounts_dir = Path(accounts_dir).expanduser()
            relays = list({source, dest})
            tmp_contexts = {}
            try:
                for relay in relays:
                    ctx = RelayContext(relay, accounts_dir / relay)
                    ctx.open()
                    tmp_contexts[relay] = ctx
                pinger = _perform_direct_ping(tmp_contexts, source, dest,
                                              count, interval, timeout)
            finally:
                for ctx in tmp_contexts.values():
                    ctx.close()

        return ProbeResult(
            source=source,
            destination=dest,
            sent=pinger.sent,
            received=pinger.received,
            loss=pinger.loss,
            rtts_ms=[rtt for _, rtt in pinger.results],
            account_setup_time=pinger.account_setup_time,
            message_time=pinger.message_time,
        )
    except PingError as e:
        result = ProbeResult(source=source, destination=dest, error=str(e))
        log.debug("probe_failed", src=source, dst=dest,
                  failure_category=result.failure_category, error=str(e))
        return result
    except Exception as e:
        result = ProbeResult(source=source, destination=dest, error=str(e))
        log.exception("probe_failed", src=source, dst=dest,
                      failure_category=result.failure_category, error=str(e))
        return result
