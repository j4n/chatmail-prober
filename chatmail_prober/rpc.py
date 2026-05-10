"""RelayContext: per-relay RPC server lifecycle.

Wraps a single deltachat-rpc-server child process plus the DeltaChat
handle and AccountMaker that share its accounts directory. Long-lived
relay pools manage open()/close() manually; ad-hoc callers can use the
context-manager form.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
from pathlib import Path

from deltachat_rpc_client import DeltaChat, Rpc

from chatmail_prober.accounts import AccountMaker


def _ensure_venv_on_path() -> None:
    """Add the venv's bin dir to PATH so deltachat-rpc-server is found."""
    bin_dir = str(Path(sys.executable).parent)
    path = os.environ.get("PATH", "")
    if bin_dir not in path.split(os.pathsep):
        os.environ["PATH"] = bin_dir + os.pathsep + path


_ensure_venv_on_path()


class RelayContext:
    """Context for a relay: RPC connection, DeltaChat instance, AccountMaker.

    Can be used as a context manager for automatic cleanup, or managed
    manually via open()/close() for long-lived relay pools.
    """

    def __init__(self, relay: str, accounts_dir: str | Path) -> None:
        self.relay = relay
        self.accounts_dir = Path(accounts_dir)
        self.rpc: Rpc | None = None
        self.dc: DeltaChat | None = None
        self.maker: AccountMaker | None = None

    def open(self) -> RelayContext:
        """Start the RPC server and initialize DeltaChat + AccountMaker."""
        if self.accounts_dir.exists() and not self.accounts_dir.joinpath("accounts.toml").exists():
            shutil.rmtree(self.accounts_dir)
        rpc = Rpc(accounts_dir=str(self.accounts_dir))
        rpc.start()
        self.rpc = rpc
        self.dc = DeltaChat(rpc)
        self.maker = AccountMaker(self.dc)
        return self

    def close(self) -> None:
        """Shut down the RPC server. systemd handles hung shutdowns via TimeoutStopSec."""
        rpc, self.rpc = self.rpc, None
        self.dc = None
        self.maker = None
        if rpc is not None:
            with contextlib.suppress(Exception):
                rpc.close()

    def __enter__(self) -> RelayContext:
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()
