"""Direct-ping logic for chatmail relay probing.

Vendors the minimal subset of cmping needed for 1:1 direct pinging
with deltachat-rpc-client.  No group mode, no CLI output, no verbose
gating -- just structured logging.
"""

import ipaddress
import logging
import os
import queue
import random
import shutil
import string
import sys
import threading
import time
import urllib.parse
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

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vendored helpers (from cmping.py, simplified)
# ---------------------------------------------------------------------------

class PingError(Exception):
    """Raised when a probe encounters a non-recoverable error."""
    pass


def is_ip_address(host):
    """Check if the given host is an IP address."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def generate_credentials():
    """Generate random username and password for IP-based login.

    Returns:
        tuple: (username, password) where username is 12 chars and password is 20 chars
    """
    chars = string.ascii_lowercase + string.digits
    username = "".join(random.choices(chars, k=12))
    password = "".join(random.choices(chars, k=20))
    return username, password


def create_qr_url(domain_or_ip):
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

class RelayContext:
    """Context for a relay: RPC connection, DeltaChat instance, AccountMaker.

    Can be used as a context manager for automatic cleanup, or managed
    manually via open()/close() for long-lived relay pools.
    """

    def __init__(self, relay, accounts_dir):
        self.relay = relay
        self.accounts_dir = Path(accounts_dir)
        self.rpc = None
        self.dc = None
        self.maker = None

    def open(self):
        """Start the RPC server and initialize DeltaChat + AccountMaker."""
        if self.accounts_dir.exists() and not self.accounts_dir.joinpath("accounts.toml").exists():
            shutil.rmtree(self.accounts_dir)
        self.rpc = Rpc(accounts_dir=self.accounts_dir)
        self.rpc.__enter__()
        self.dc = DeltaChat(self.rpc)
        self.maker = AccountMaker(self.dc)
        return self

    def close(self):
        """Shut down the RPC server."""
        if self.rpc is not None:
            try:
                self.rpc.__exit__(None, None, None)
            except Exception as e:
                log.warning("cleanup failed for %s: %s", self.relay, e)
            self.rpc = None
            self.dc = None
            self.maker = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()
        return False


class AccountMaker:
    """Creates and manages deltachat accounts on a relay."""

    def __init__(self, dc):
        self.dc = dc
        self.online = []

    def wait_all_online(self, timeout=None):
        deadline = time.time() + timeout if timeout is not None else None
        remaining = list(self.online)
        while remaining:
            ac = remaining.pop()
            eq = ac._rpc.get_queue(ac.id)
            while True:
                if deadline is not None and time.time() >= deadline:
                    addr = ac.get_config("addr")
                    raise PingError(f"Timeout waiting for {addr} to come online")
                try:
                    event = AttrDict(eq.get(timeout=1.0))
                except queue.Empty:
                    continue
                if event.kind == EventType.IMAP_INBOX_IDLE:
                    break
                elif event.kind == EventType.ERROR:
                    log.warning("ERROR during profile setup: %s", event.msg)

    def _add_online(self, account):
        account.set_config("bot", "1")
        account.start_io()
        self.online.append(account)

    def get_relay_account(self, domain):
        for account in self.dc.get_all_accounts():
            addr = account.get_config("configured_addr")
            if addr is not None:
                addr_domain = addr.split("@")[1] if "@" in addr else None
                if addr_domain == domain:
                    if account not in self.online:
                        break
        else:
            account = self.dc.add_account()
            qr_url = create_qr_url(domain)
            try:
                account.set_config_from_qr(qr_url)
            except Exception as e:
                log.error("Failed to configure profile on %s: %s", domain, e)
                raise

        try:
            self._add_online(account)
        except Exception as e:
            log.error("Failed to bring profile online for %s: %s", domain, e)
            raise

        return account


# ---------------------------------------------------------------------------
# Pinger -- simplified for single receiver, 1:1 chat
# ---------------------------------------------------------------------------

class Pinger:
    """Sends ping messages via 1:1 chat and receives responses.

    Simplified from cmping's group-mode Pinger: exactly one receiver,
    creates its own 1:1 chat, no thread pool for receivers.
    """

    def __init__(self, sender, receiver, count, interval):
        self.sender = sender
        self.receiver = receiver
        self.count = count
        self.interval = interval

        self.addr1 = sender.get_config("addr")
        self.addr2 = receiver.get_config("addr")
        self.relay1 = self.addr1.split("@")[1]
        self.relay2 = self.addr2.split("@")[1]

        # Create 1:1 chat
        contact = sender.create_contact(receiver)
        self.chat = contact.create_chat()

        log.info(
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
        """Send ping messages to the 1:1 chat at regular intervals."""
        for seq in range(self.count):
            if self.deadline is not None and time.time() >= self.deadline:
                break
            text = f"{self.tx} {time.time():.4f} {seq:17}"
            self.chat.send_text(text)
            self.sent += 1
            time.sleep(self.interval)
        self._stop_event.set()

    def receive(self):
        """Receive ping messages from the single receiver.

        Yields:
            tuple: (seq, ms_duration) for each received message
        """
        num_pending = self.count
        received_seqs = set()

        # Poll receiver's event queue directly -- no thread pool needed
        # for a single receiver.
        account_queue = self.receiver._rpc.get_queue(self.receiver.id)
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


# ---------------------------------------------------------------------------
# _perform_direct_ping -- orchestrates a single 1:1 probe
# ---------------------------------------------------------------------------

def _perform_direct_ping(relay_contexts, source, dest, count, interval, timeout):
    """Run a direct 1:1 ping between two relays using shared contexts.

    Args:
        relay_contexts: dict mapping relay name -> open RelayContext
        source: source relay domain
        dest: destination relay domain
        count: number of pings to send
        interval: seconds between pings
        timeout: per-phase timeout in seconds

    Returns:
        Pinger with results populated.
    """
    sender_maker = relay_contexts[source].maker
    receiver_maker = relay_contexts[dest].maker

    account_setup_start = time.time()

    try:
        sender = sender_maker.get_relay_account(source)
    except Exception as e:
        raise PingError(f"Failed to setup sender profile on {source}: {e}") from e

    try:
        receiver = receiver_maker.get_relay_account(dest)
    except Exception as e:
        raise PingError(f"Failed to setup receiver profile on {dest}: {e}") from e

    # Wait for both accounts to come online
    unique_relays = list({source, dest})
    all_makers = [relay_contexts[r].maker for r in unique_relays]
    errors = []

    def _wait(maker):
        try:
            maker.wait_all_online(timeout=timeout)
        except Exception as e:
            errors.append(e)

    threads = []
    for maker in all_makers:
        t = threading.Thread(target=_wait, args=(maker,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    if errors:
        raise PingError(
            f"Timeout or error waiting for profiles to be online: {errors[0]}"
        ) from errors[0]

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
# RelayPool -- manages one RelayContext per relay domain
# ---------------------------------------------------------------------------

class RelayPool:
    """Manages one RelayContext per relay domain.

    Contexts are opened once and shared across all probes in a round.
    Uses per-relay accounts dirs (cache_dir/relay) instead of per-worker.
    """

    def __init__(self, cache_dir):
        self._cache_dir = Path(cache_dir)
        self._contexts = {}

    def open_all(self, relays):
        """Pre-open contexts for all relays.  Fails fast on errors."""
        for relay in relays:
            if relay not in self._contexts:
                ctx = RelayContext(relay, self._cache_dir / relay)
                ctx.open()
                self._contexts[relay] = ctx

    def contexts(self):
        """Return relay -> RelayContext dict (read-only after open_all)."""
        return dict(self._contexts)

    def reopen(self, relay):
        """Close and reopen a single relay's context (e.g. after RPC crash)."""
        old = self._contexts.pop(relay, None)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        ctx = RelayContext(relay, self._cache_dir / relay)
        ctx.open()
        self._contexts[relay] = ctx
        log.info("pool: reopened context for %s", relay)

    def close(self):
        """Close all managed contexts."""
        for ctx in self._contexts.values():
            ctx.close()
        self._contexts.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# ---------------------------------------------------------------------------
# ProbeResult / run_probe -- public API for the exporter
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    source: str
    destination: str
    sent: int = 0
    received: int = 0
    loss: float = 100.0
    rtts_ms: list[float] = field(default_factory=list)
    account_setup_time: float = 0.0
    message_time: float = 0.0
    error: str | None = None


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
        log.warning("Probe %s -> %s failed: %s", source, dest, e)
        return ProbeResult(source=source, destination=dest, error=str(e))
    except Exception as e:
        log.exception("Unexpected error probing %s -> %s", source, dest)
        return ProbeResult(source=source, destination=dest, error=str(e))
