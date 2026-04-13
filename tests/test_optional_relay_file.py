"""Tests for the relay file positional argument being optional.

Rules:
  - relay file(s) are NOT required when --hosts is given
  - relay file(s) are NOT required when --reset is given (reset-and-exit)
  - relay file(s) ARE required when none of the above apply (no --auto-fetch either)
"""
from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from chatmail_prober.__main__ import main, parse_args


class TestParseArgsNoRelayFile:
    def test_hosts_flag_needs_no_relay_file(self):
        """parse_args must not error when -H is given without a relay file."""
        args = parse_args(["-H", "nine.testrun.org"])
        assert args.hosts == "nine.testrun.org"
        assert args.relays == []

    def test_reset_needs_no_relay_file(self):
        """parse_args must not error when --reset is given without a relay file."""
        args = parse_args(["--reset", "all"])
        assert args.reset == ["all"]
        assert args.relays == []

    def test_no_relay_file_no_hosts_is_still_valid_at_parse_time(self):
        """parse_args itself must not raise; the error fires in main()."""
        args = parse_args([])
        assert args.relays == []
        assert args.hosts is None


class TestMainNoRelayFile:
    def test_reset_all_without_relay_file_succeeds(self, tmp_path):
        """--reset all with no relay file must reset and exit cleanly."""
        cache = tmp_path / "cache"
        (cache / "worker-0" / "nine.testrun.org").mkdir(parents=True)

        with patch("chatmail_prober.__main__.reset_accounts") as mock_reset:
            with pytest.raises(SystemExit) as exc_info:
                main(["--reset", "all", "--cache-dir", str(cache)])
        # reset_accounts must have been called and exit code must be 0
        mock_reset.assert_called_once()
        assert exc_info.value.code in (0, None)

    def test_reset_domain_without_relay_file_succeeds(self, tmp_path):
        """--reset DOMAIN with no relay file must reset and exit cleanly."""
        cache = tmp_path / "cache"
        (cache / "worker-0" / "nine.testrun.org").mkdir(parents=True)

        with patch("chatmail_prober.__main__.reset_accounts") as mock_reset:
            with pytest.raises(SystemExit) as exc_info:
                main(["--reset", "nine.testrun.org", "--cache-dir", str(cache)])
        mock_reset.assert_called_once()
        assert exc_info.value.code in (0, None)

    def test_hosts_flag_without_relay_file_proceeds(self, tmp_path):
        """--hosts without relay file must load relays from the -H list."""
        cache = tmp_path / "cache"
        cache.mkdir()

        with patch("chatmail_prober.__main__.check_relays_alive",
                   return_value=(["nine.testrun.org"], set())), \
             patch("chatmail_prober.__main__.run_round", return_value=(0.1, [])), \
             patch("chatmail_prober.__main__.render_summary"), \
             patch("chatmail_prober.__main__.write_textfile"), \
             patch("chatmail_prober.__main__.print_metrics"):
            # Should not raise "at least one relay list file" error
            main(["-H", "nine.testrun.org", "--once",
                  "--cache-dir", str(cache)])

    def test_no_relay_source_errors(self, tmp_path):
        """No relay file, no -H, no --auto-fetch must raise SystemExit."""
        cache = tmp_path / "cache"
        cache.mkdir()
        with pytest.raises(SystemExit) as exc_info:
            main(["--cache-dir", str(cache)])
        assert exc_info.value.code != 0
