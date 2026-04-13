"""Tests for selective --reset [DOMAIN...] behaviour.

Cache layout under test:
  cache_dir/
    worker-0/
      nine.testrun.org/   <- RelayContext accounts dir
      mailchat.pl/
    worker-1/
      nine.testrun.org/
      mailchat.pl/
    alive-check/
      nine.testrun.org/
      mailchat.pl/

--reset alone  : wipe all worker-* dirs, leave alive-check untouched
--reset D...   : wipe D from every worker-* dir AND from alive-check
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from chatmail_prober.__main__ import parse_args, reset_accounts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cache(tmp_path: Path, workers: int = 2,
                domains: tuple[str, ...] = ("nine.testrun.org", "mailchat.pl")) -> Path:
    """Create a fake cache directory tree."""
    cache = tmp_path / "cache"
    for i in range(workers):
        for d in domains:
            (cache / f"worker-{i}" / d).mkdir(parents=True)
    for d in domains:
        (cache / "alive-check" / d).mkdir(parents=True)
    return cache


# ---------------------------------------------------------------------------
# parse_args: --reset flag shape
# ---------------------------------------------------------------------------

class TestResetArgParsing:
    def test_reset_all_keyword(self, tmp_path):
        relay_file = tmp_path / "r.txt"
        relay_file.write_text("nine.testrun.org\n")
        args = parse_args([str(relay_file), "--reset", "all"])
        assert args.reset == ["all"]

    def test_reset_with_domains(self, tmp_path):
        relay_file = tmp_path / "r.txt"
        relay_file.write_text("nine.testrun.org\n")
        args = parse_args([str(relay_file), "--reset", "nine.testrun.org", "mailchat.pl"])
        assert args.reset == ["nine.testrun.org", "mailchat.pl"]

    def test_reset_default_is_none(self, tmp_path):
        relay_file = tmp_path / "r.txt"
        relay_file.write_text("nine.testrun.org\n")
        args = parse_args([str(relay_file)])
        assert args.reset is None

    def test_reset_bare_raises_system_exit(self, tmp_path):
        """--reset with no args must exit with an error message."""
        relay_file = tmp_path / "r.txt"
        relay_file.write_text("nine.testrun.org\n")
        with pytest.raises(SystemExit) as exc_info:
            parse_args([str(relay_file), "--reset"])
        assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# reset_accounts() function
# ---------------------------------------------------------------------------

class TestResetAccountsFunction:
    def test_full_reset_removes_all_worker_dirs(self, tmp_path):
        cache = _make_cache(tmp_path)
        reset_accounts(cache, domains=["all"])
        assert not (cache / "worker-0").exists()
        assert not (cache / "worker-1").exists()

    def test_full_reset_preserves_alive_check(self, tmp_path):
        cache = _make_cache(tmp_path)
        reset_accounts(cache, domains=["all"])
        assert (cache / "alive-check").exists()

    def test_selective_reset_removes_domain_from_workers(self, tmp_path):
        cache = _make_cache(tmp_path)
        reset_accounts(cache, domains=["nine.testrun.org"])
        assert not (cache / "worker-0" / "nine.testrun.org").exists()
        assert not (cache / "worker-1" / "nine.testrun.org").exists()

    def test_selective_reset_preserves_other_domain_in_workers(self, tmp_path):
        cache = _make_cache(tmp_path)
        reset_accounts(cache, domains=["nine.testrun.org"])
        assert (cache / "worker-0" / "mailchat.pl").exists()
        assert (cache / "worker-1" / "mailchat.pl").exists()

    def test_selective_reset_also_clears_alive_check(self, tmp_path):
        cache = _make_cache(tmp_path)
        reset_accounts(cache, domains=["nine.testrun.org"])
        assert not (cache / "alive-check" / "nine.testrun.org").exists()

    def test_selective_reset_preserves_other_domain_in_alive_check(self, tmp_path):
        cache = _make_cache(tmp_path)
        reset_accounts(cache, domains=["nine.testrun.org"])
        assert (cache / "alive-check" / "mailchat.pl").exists()

    def test_selective_reset_multiple_domains(self, tmp_path):
        cache = _make_cache(tmp_path)
        reset_accounts(cache, domains=["nine.testrun.org", "mailchat.pl"])
        assert not (cache / "worker-0" / "nine.testrun.org").exists()
        assert not (cache / "worker-0" / "mailchat.pl").exists()

    def test_selective_reset_unknown_domain_is_noop(self, tmp_path):
        cache = _make_cache(tmp_path)
        # Should not raise even if domain has no cached accounts
        reset_accounts(cache, domains=["unknown.example"])
        assert (cache / "worker-0" / "nine.testrun.org").exists()

    def test_full_reset_empty_cache_is_noop(self, tmp_path):
        cache = tmp_path / "empty-cache"
        cache.mkdir()
        reset_accounts(cache, domains=["all"])  # must not raise
