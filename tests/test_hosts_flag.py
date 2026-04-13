"""Tests for the --hosts / -H flag, IPv6 auto-bracketing, and short flag aliases."""
from __future__ import annotations

import os
import tempfile

import pytest

from chatmail_prober.__main__ import parse_args


# ---------------------------------------------------------------------------
# --hosts / -H
# ---------------------------------------------------------------------------

class TestHostsFlag:
    def test_long_form_accepted(self, tmp_path):
        relay_file = tmp_path / "r.txt"
        relay_file.write_text("nine.testrun.org\n")
        args = parse_args([str(relay_file), "--hosts", "a.example,b.example"])
        assert args.hosts == "a.example,b.example"

    def test_short_form_accepted(self, tmp_path):
        relay_file = tmp_path / "r.txt"
        relay_file.write_text("nine.testrun.org\n")
        args = parse_args([str(relay_file), "-H", "a.example,b.example"])
        assert args.hosts == "a.example,b.example"

    def test_defaults_to_none(self, tmp_path):
        relay_file = tmp_path / "r.txt"
        relay_file.write_text("nine.testrun.org\n")
        args = parse_args([str(relay_file)])
        assert args.hosts is None

    def test_single_host(self, tmp_path):
        relay_file = tmp_path / "r.txt"
        relay_file.write_text("nine.testrun.org\n")
        args = parse_args([str(relay_file), "--hosts", "a.example"])
        assert args.hosts == "a.example"

    def test_whitespace_in_value_preserved(self, tmp_path):
        """Whitespace trimming happens in main(), not in parse_args."""
        relay_file = tmp_path / "r.txt"
        relay_file.write_text("nine.testrun.org\n")
        args = parse_args([str(relay_file), "--hosts", " a.example , b.example "])
        assert "a.example" in args.hosts
        assert "b.example" in args.hosts


# ---------------------------------------------------------------------------
# IPv6 auto-bracketing
# ---------------------------------------------------------------------------

class TestIPv6Bracketing:
    """_bracket_ipv6(host) must wrap bare IPv6 addresses in square brackets."""

    def _bracket(self, host: str) -> str:
        from chatmail_prober.__main__ import _bracket_ipv6
        return _bracket_ipv6(host)

    def test_bare_ipv6_gets_brackets(self):
        assert self._bracket("::1") == "[::1]"

    def test_full_ipv6_gets_brackets(self):
        assert self._bracket("2001:db8::1") == "[2001:db8::1]"

    def test_already_bracketed_unchanged(self):
        assert self._bracket("[::1]") == "[::1]"

    def test_ipv4_unchanged(self):
        assert self._bracket("192.168.1.1") == "192.168.1.1"

    def test_hostname_unchanged(self):
        assert self._bracket("nine.testrun.org") == "nine.testrun.org"

    def test_hosts_flag_stores_raw_string(self, tmp_path):
        """args.hosts stores the raw comma string; bracketing happens in main()."""
        relay_file = tmp_path / "r.txt"
        relay_file.write_text("nine.testrun.org\n")
        args = parse_args([str(relay_file), "-H", "::1,nine.testrun.org"])
        assert "::1" in args.hosts


# ---------------------------------------------------------------------------
# Short flag aliases — one round-trip test covers all aliases
# ---------------------------------------------------------------------------

class TestShortAliases:
    """All short flags must wire to the correct long-form attribute."""

    def _args(self, *flags):
        with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False) as f:
            f.write("nine.testrun.org\n")
            name = f.name
        try:
            return parse_args([name] + list(flags))
        finally:
            os.unlink(name)

    def test_all_short_aliases(self):
        args = self._args("-1", "-p", "-m", "-n", "3", "-t", "30", "-w", "8", "-i", "60")
        assert args.once is True
        assert args.print_summary is True
        assert args.print_metrics is True
        assert args.count == 3
        assert args.timeout == 30
        assert args.workers == 8
        assert args.interval == 60
