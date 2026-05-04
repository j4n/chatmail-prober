#!/usr/bin/env python3
"""cleanup_accounts.py -- analyze and trim stale accounts from the cache dir.

Supports both directory layouts:
  - New (flat):  worker-N/accounts.toml  (one DB per worker, multiple domains)
  - Old (split): worker-N/relay.domain/accounts.toml  (one DB per relay)

Identifies accounts exceeding the per-domain limit and removes excess entries.

Usage:
    # Dry-run (default): report what would be cleaned
    uv run python scripts/cleanup_accounts.py /var/lib/chatmail-prober

    # Actually clean up (service must be stopped first)
    uv run python scripts/cleanup_accounts.py /var/lib/chatmail-prober --apply

Exit codes:
    0  no excess accounts found (or cleanup applied successfully)
    1  excess accounts found (dry-run) or errors during cleanup
    2  refused to run (RPC servers still running)
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]
from pathlib import Path


def check_rpc_servers(cache_dir: Path) -> list[int]:
    """Return PIDs of deltachat-rpc-server processes using this cache dir."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"deltachat-rpc-server.*{cache_dir}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return []
        return [int(p) for p in result.stdout.strip().split() if p.strip()]
    except (FileNotFoundError, ValueError):
        return []


def parse_accounts_toml(path: Path) -> tuple[dict, list[dict]]:
    """Parse accounts.toml, return (top-level dict, list of account dicts).

    Format:
        selected_account = 2
        next_id = 3
        accounts_order = [1, 2]
        [[accounts]]
        id = 1
        dir = "uuid-string"
        uuid = "uuid-string"
    """
    with open(path, "rb") as f:
        data = tomllib.load(f)
    accounts = data.get("accounts", [])
    return data, accounts


def write_accounts_toml(path: Path, keep: dict) -> None:
    """Write accounts.toml keeping only one account entry."""
    acct = keep
    lines = [
        f"selected_account = {acct['id']}",
        f"next_id = {acct['id'] + 1}",
        f"accounts_order = [\n    {acct['id']},\n]",
        "",
        "[[accounts]]",
        f"id = {acct['id']}",
        f'dir = "{acct["dir"]}"',
        f'uuid = "{acct["uuid"]}"',
    ]
    path.write_text("\n".join(lines) + "\n")


def analyze_dir(cache_dir: Path) -> list[dict]:
    """Walk the cache dir and find all accounts.toml files.

    Handles both layouts:
    - New: worker-N/accounts.toml (flat, one DB per worker)
    - Old: worker-N/relay.domain/accounts.toml (split, one DB per relay)
    """
    results = []
    for pool_dir in sorted(cache_dir.iterdir()):
        if not pool_dir.is_dir():
            continue
        # New flat layout: accounts.toml directly in pool dir
        flat_toml = pool_dir / "accounts.toml"
        if flat_toml.exists():
            try:
                _data, accounts = parse_accounts_toml(flat_toml)
            except Exception as e:
                results.append({
                    "pool": pool_dir.name,
                    "relay": "(shared)",
                    "path": flat_toml,
                    "error": str(e),
                })
                continue
            results.append({
                "pool": pool_dir.name,
                "relay": "(shared)",
                "path": flat_toml,
                "accounts": accounts,
                "count": len(accounts),
            })
            continue
        # Old per-relay layout: accounts.toml in relay subdirs
        for relay_dir in sorted(pool_dir.iterdir()):
            if not relay_dir.is_dir():
                continue
            toml_path = relay_dir / "accounts.toml"
            if not toml_path.exists():
                continue
            try:
                _data, accounts = parse_accounts_toml(toml_path)
            except Exception as e:
                results.append({
                    "pool": pool_dir.name,
                    "relay": relay_dir.name,
                    "path": toml_path,
                    "error": str(e),
                })
                continue
            results.append({
                "pool": pool_dir.name,
                "relay": relay_dir.name,
                "path": toml_path,
                "accounts": accounts,
                "count": len(accounts),
            })
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Analyze and clean up excess accounts in chatmail-prober cache")
    parser.add_argument("cache_dir", type=Path,
                        help="path to chatmail-prober cache directory")
    parser.add_argument("--apply", action="store_true",
                        help="actually remove excess accounts (default: dry-run)")
    args = parser.parse_args(argv)

    cache_dir = args.cache_dir.resolve()
    if not cache_dir.is_dir():
        print(f"error: {cache_dir} is not a directory", file=sys.stderr)
        return 1

    if args.apply:
        pids = check_rpc_servers(cache_dir)
        if pids:
            print(f"error: {len(pids)} deltachat-rpc-server process(es) still running "
                  f"(PIDs: {', '.join(map(str, pids))})", file=sys.stderr)
            print("stop the service first: systemctl stop chatmail-prober", file=sys.stderr)
            return 2

    results = analyze_dir(cache_dir)
    if not results:
        print(f"no accounts.toml files found in {cache_dir}")
        return 0

    # Summary
    total_files = len([r for r in results if "error" not in r])
    total_accounts = sum(r.get("count", 0) for r in results)
    excess_entries = [r for r in results if r.get("count", 0) > 1]
    error_entries = [r for r in results if "error" in r]
    total_excess = sum(r["count"] - 1 for r in excess_entries)

    print(f"Cache dir: {cache_dir}")
    print(f"  accounts.toml files: {total_files}")
    print(f"  total accounts:      {total_accounts}")
    print(f"  files with excess:   {len(excess_entries)}")
    print(f"  excess accounts:     {total_excess}")
    if error_entries:
        print(f"  parse errors:        {len(error_entries)}")
    print()

    # Per-pool breakdown
    pools = {}
    for r in results:
        pool = r["pool"]
        if pool not in pools:
            pools[pool] = {"files": 0, "accounts": 0, "excess": 0}
        if "error" not in r:
            pools[pool]["files"] += 1
            pools[pool]["accounts"] += r["count"]
            pools[pool]["excess"] += max(0, r["count"] - 1)

    print(f"  {'Pool':<20} {'Files':>6} {'Accounts':>9} {'Excess':>7}")
    print(f"  {'-'*20} {'-'*6} {'-'*9} {'-'*7}")
    for pool, stats in sorted(pools.items()):
        print(f"  {pool:<20} {stats['files']:>6} {stats['accounts']:>9} {stats['excess']:>7}")
    print()

    if error_entries:
        print("Parse errors:")
        for r in error_entries:
            print(f"  {r['pool']}/{r['relay']}: {r['error']}")
        print()

    if not excess_entries:
        print("No cleanup needed.")
        return 0

    # Detail: files with excess accounts
    print("Excess accounts:")
    for r in excess_entries:
        accounts = r["accounts"]
        keep = min(accounts, key=lambda a: a["id"])
        remove = [a for a in accounts if a["id"] != keep["id"]]
        remove_ids = ",".join(str(a["id"]) for a in remove)
        print(f"  {r['pool']}/{r['relay']}: {r['count']} accounts, "
              f"keep={keep['id']}, remove={remove_ids}")

    if not args.apply:
        print(f"\nDry run: would remove {total_excess} excess account(s).")
        print("Re-run with --apply to execute.")
        return 1 if total_excess > 0 else 0

    # Apply cleanup
    cleaned = 0
    errors = 0
    for r in excess_entries:
        accounts = r["accounts"]
        relay_dir = r["path"].parent
        keep = min(accounts, key=lambda a: a["id"])

        # Back up accounts.toml
        backup = r["path"].with_suffix(".toml.bak")
        shutil.copy2(r["path"], backup)

        for acct in accounts:
            if acct["id"] == keep["id"]:
                continue
            acct_dir = relay_dir / acct["dir"]
            if acct_dir.is_dir():
                try:
                    shutil.rmtree(acct_dir)
                    print(f"  removed {acct_dir}")
                    cleaned += 1
                except Exception as e:
                    print(f"  error removing {acct_dir}: {e}", file=sys.stderr)
                    errors += 1
            else:
                print(f"  skipped {acct_dir} (not found)")
                cleaned += 1

        try:
            write_accounts_toml(r["path"], keep)
            print(f"  rewrote {r['path']} (kept account {keep['id']})")
        except Exception as e:
            print(f"  error rewriting {r['path']}: {e}", file=sys.stderr)
            errors += 1

    print(f"\nCleaned {cleaned} excess account(s), {errors} error(s).")
    return 1 if errors > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
