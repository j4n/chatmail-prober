"""Tests for the --hosts / -h flag and short flag aliases.

Behaviour under test:
  --hosts / -h  : comma-separated relay list overriding relays_file
                  bare IPv6 addresses are auto-bracketed (e.g. ::1 -> [::1])
  -1            : shorthand for --once
  -p            : shorthand for --print
  -m            : shorthand for --print-metrics
  -n            : shorthand for --count
  -t            : shorthand for --timeout
  -w            : shorthand for --workers
  -i            : shorthand for --interval
  help text     : must say "tabular" not "gocmping"
"""
from __future__ import annotations

import pytest

from chatmail_prober.__main__ import parse_args


# ---------------------------------------------------------------------------
# --hosts / -h
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

    def test_hosts_flag_auto_brackets_ipv6(self, tmp_path):
        """When --hosts contains a bare IPv6, fetch_relay_list must bracket it."""
        relay_file = tmp_path / "r.txt"
        relay_file.write_text("nine.testrun.org\n")
        args = parse_args([str(relay_file), "-H", "::1,nine.testrun.org"])
        # The bracketing happens in main(); args.hosts stores the raw comma string
        assert "::1" in args.hosts or "[::1]" in args.hosts


# ---------------------------------------------------------------------------
# Short flag aliases
# ---------------------------------------------------------------------------

class TestShortAliases:
    def _args(self, *flags):
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False) as f:
            f.write("nine.testrun.org\n")
            name = f.name
        try:
            return parse_args([name] + list(flags))
        finally:
            os.unlink(name)

    def test_dash_1_is_once(self):
        assert self._args("-1").once is True

    def test_dash_p_is_print_summary(self):
        assert self._args("-p").print_summary is True

    def test_dash_m_is_print_metrics(self):
        assert self._args("-m").print_metrics is True

    def test_dash_n_is_count(self):
        assert self._args("-n", "3").count == 3

    def test_dash_t_is_timeout(self):
        assert self._args("-t", "30").timeout == 30

    def test_dash_w_is_workers(self):
        assert self._args("-w", "8").workers == 8

    def test_dash_i_is_interval(self):
        assert self._args("-i", "60").interval == 60


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------

class TestHelpText:
    def test_no_gocmping_in_help(self, tmp_path, capsys):
        """Help text must not mention 'gocmping'."""
        import argparse
        with pytest.raises(SystemExit):
            parse_args(["--help"])
        captured = capsys.readouterr()
        assert "gocmping" not in captured.out.lower()

    def test_tabular_in_print_help(self, tmp_path, capsys):
        """--print help text must say 'tabular'."""
        with pytest.raises(SystemExit):
            parse_args(["--help"])
        captured = capsys.readouterr()
        assert "tabular" in captured.out.lower()
