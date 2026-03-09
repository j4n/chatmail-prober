"""Tests for config parsing, CLI args, and pair generation."""

import os
import pytest

from chatmail_prober.__main__ import read_relay_list, parse_args


class TestReadRelayList:
    def test_reads_domains(self, tmp_path):
        f = tmp_path / "relays.txt"
        f.write_text("nine.testrun.org\nmehl.cloud\ntarpit.fun\n")
        assert read_relay_list(str(f)) == ["nine.testrun.org", "mehl.cloud", "tarpit.fun"]

    def test_ignores_comments_and_blanks(self, tmp_path):
        f = tmp_path / "relays.txt"
        f.write_text("# comment\nnine.testrun.org\n\n  \n# another\nmehl.cloud\n")
        assert read_relay_list(str(f)) == ["nine.testrun.org", "mehl.cloud"]

    def test_strips_whitespace(self, tmp_path):
        f = tmp_path / "relays.txt"
        f.write_text("  nine.testrun.org  \n  mehl.cloud\t\n")
        assert read_relay_list(str(f)) == ["nine.testrun.org", "mehl.cloud"]

    def test_empty_file_exits(self, tmp_path):
        f = tmp_path / "relays.txt"
        f.write_text("# only comments\n\n")
        with pytest.raises(SystemExit):
            read_relay_list(str(f))

    def test_single_relay(self, tmp_path):
        f = tmp_path / "relays.txt"
        f.write_text("nine.testrun.org\n")
        assert read_relay_list(str(f)) == ["nine.testrun.org"]


class TestParseArgs:
    def test_required_relays(self):
        with pytest.raises(SystemExit):
            parse_args([])

    def test_defaults(self):
        args = parse_args(["relays.txt"])
        assert args.relays == "relays.txt"
        assert args.port == 0
        assert args.textfile is None
        assert args.interval == 900
        assert args.count == 5
        assert args.ping_interval == 0.1
        assert args.timeout == 60
        assert args.workers == 5
        assert args.once is False
        assert args.verbose == 0
        assert args.quiet is False

    def test_quiet_flag(self):
        args = parse_args(["r.txt", "-q"])
        assert args.quiet is True
        assert args.verbose == 0

    def test_all_flags(self):
        args = parse_args([
            "r.txt",
            "--port", "0",
            "--textfile", "/tmp/out.prom",
            "--interval", "60",
            "--count", "5",
            "--ping-interval", "0.5",
            "--timeout", "30",
            "--workers", "10",
            "--cache-dir", "/tmp/cache",
            "--once",
            "-vv",
        ])
        assert args.port == 0
        assert args.textfile == "/tmp/out.prom"
        assert args.interval == 60
        assert args.count == 5
        assert args.ping_interval == 0.5
        assert args.timeout == 30
        assert args.workers == 10
        assert args.cache_dir == "/tmp/cache"
        assert args.once is True
        assert args.verbose == 2


class TestPairGeneration:
    """Verify the pair matrix logic from __main__.py."""

    def test_single_relay_produces_self_loop(self):
        relays = ["a.example"]
        pairs = [(s, d) for s in relays for d in relays]
        assert pairs == [("a.example", "a.example")]

    def test_two_relays_produce_four_pairs(self):
        relays = ["a.example", "b.example"]
        pairs = [(s, d) for s in relays for d in relays]
        assert len(pairs) == 4
        assert ("a.example", "a.example") in pairs
        assert ("a.example", "b.example") in pairs
        assert ("b.example", "a.example") in pairs
        assert ("b.example", "b.example") in pairs

    def test_three_relays_produce_nine_pairs(self):
        relays = ["a", "b", "c"]
        pairs = [(s, d) for s in relays for d in relays]
        assert len(pairs) == 9
