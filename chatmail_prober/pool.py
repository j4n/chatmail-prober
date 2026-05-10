"""RelayPool: one shared RPC server per worker, multiple relay domains."""

from __future__ import annotations

import contextlib
from pathlib import Path

from deltachat_rpc_client import DeltaChat, Rpc

from chatmail_prober.accounts import AccountMaker
from chatmail_prober.log_config import get_logger

log = get_logger(__name__)


class RelayPool:
    """One RPC server per worker, shared across all relay domains.

    Exposes .maker / .rpc / .dc so it can duck-type as a relay context
    in _perform_direct_ping (which does relay_contexts[source].maker).
    """

    def __init__(self, cache_dir: str | Path) -> None:
        self._cache_dir = Path(cache_dir)
        self.rpc: Rpc | None = None
        self.dc: DeltaChat | None = None
        self.maker: AccountMaker | None = None
        self._relays: set[str] = set()

    def _start_rpc(self) -> None:
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
        rpc = Rpc(accounts_dir=str(accts_dir))
        rpc.start()
        self.rpc = rpc
        self.dc = DeltaChat(rpc)
        self.maker = AccountMaker(self.dc)

    def _shutdown_rpc(self) -> None:
        """Shut down the shared RPC server. systemd handles hangs via TimeoutStopSec."""
        rpc, self.rpc = self.rpc, None
        self.dc = None
        self.maker = None
        if rpc is not None:
            with contextlib.suppress(Exception):
                rpc.close()

    def open_all(self, relays: list[str]) -> None:
        """Ensure the shared RPC server is running, register relays."""
        if self.rpc is None:
            self._start_rpc()
        self._relays.update(relays)

    def contexts(self) -> dict[str, RelayPool]:
        """Return relay -> context dict; all relays share this pool."""
        return {r: self for r in self._relays}

    def reopen(self) -> None:
        """Restart the shared RPC server."""
        self._shutdown_rpc()
        self._start_rpc()
        log.info("pool: reopened shared rpc-server in %s", self._cache_dir)

    def prune(self, active_relays: list[str]) -> None:
        """Forget relays no longer in the active set."""
        stale = self._relays - set(active_relays)
        if stale:
            log.info("pool: pruned relay(s): %s", ", ".join(sorted(stale)))
        self._relays &= set(active_relays)

    def close(self) -> None:
        """Close the shared RPC server."""
        self._shutdown_rpc()
        self._relays.clear()

    def __enter__(self) -> RelayPool:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
