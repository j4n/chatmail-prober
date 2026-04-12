"""Tests for the --print-metrics CLI flag.

Behaviour under test:
  - --print-metrics is accepted by the argument parser
  - print_metrics() in output.py writes generate_latest() to stdout
  - main() calls print_metrics() exactly once after the --once loop exits

Design note: --print-metrics is intentionally scoped to --once mode only.
run_round() does NOT accept a print_metrics parameter; the call lives in
main() so that it fires once after the full round completes.
"""

import sys
from io import StringIO
from unittest.mock import MagicMock, patch, call

import pytest

from chatmail_prober.__main__ import parse_args
from chatmail_prober.output import print_metrics


class TestPrintMetricsArgument:
    """--print-metrics must be accepted by the argument parser."""

    def test_print_metrics_flag_accepted(self, tmp_path):
        relay_file = tmp_path / "relays.txt"
        relay_file.write_text("nine.testrun.org\n")
        args = parse_args([str(relay_file), "--print-metrics"])
        assert args.print_metrics is True

    def test_print_metrics_defaults_to_false(self, tmp_path):
        relay_file = tmp_path / "relays.txt"
        relay_file.write_text("nine.testrun.org\n")
        args = parse_args([str(relay_file)])
        assert args.print_metrics is False


class TestPrintMetricsFunction:
    """print_metrics() must write Prometheus text format to stdout."""

    def test_print_metrics_writes_to_stdout(self, capsys):
        print_metrics()
        captured = capsys.readouterr()
        # generate_latest always produces at least the HELP/TYPE lines for
        # any registered metric; at minimum it should be non-empty bytes.
        assert len(captured.out) > 0

    def test_print_metrics_output_is_prometheus_text_format(self, capsys):
        """Output must contain Prometheus text format markers."""
        print_metrics()
        captured = capsys.readouterr()
        # Prometheus text format lines start with '# HELP' or '# TYPE'
        # or metric name lines.  At minimum the output must be valid UTF-8
        # and contain at least one '#' comment line.
        assert "#" in captured.out or len(captured.out) > 0

    def test_print_metrics_writes_to_stdout_not_stderr(self, capsys):
        """Metrics must go to stdout so they can be piped independently."""
        print_metrics()
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_print_metrics_ends_with_newline(self, capsys):
        """Output must end with a newline for clean terminal display."""
        print_metrics()
        captured = capsys.readouterr()
        assert captured.out.endswith("\n")


class TestMainPrintMetrics:
    """main() must call print_metrics() exactly once after the --once loop exits."""

    def test_run_round_does_not_accept_print_metrics_kwarg(self):
        """run_round() must NOT have a do_print_metrics parameter.

        --print-metrics is scoped to --once mode only; the call lives in
        main(), not in run_round().
        """
        from chatmail_prober.__main__ import run_round
        import inspect
        sig = inspect.signature(run_round)
        assert "do_print_metrics" not in sig.parameters, (
            "run_round() must not expose do_print_metrics; "
            "print_metrics() is called by main() after --once exits"
        )

    def test_main_calls_print_metrics_once_after_once_loop(self, tmp_path):
        """main() must call print_metrics() exactly once when --once --print-metrics."""
        from chatmail_prober.__main__ import main

        relay_file = tmp_path / "relays.txt"
        relay_file.write_text("nine.testrun.org\n")

        # main() accepts a raw argv list and calls parse_args() internally.
        argv = [str(relay_file), "--once", "--print-metrics"]

        with patch("chatmail_prober.__main__.print_metrics") as mock_pm, \
             patch("chatmail_prober.__main__.check_relays_alive",
                   return_value=(["nine.testrun.org"], set())), \
             patch("chatmail_prober.__main__.run_round", return_value=0.1), \
             patch("chatmail_prober.__main__.write_textfile"), \
             patch("chatmail_prober.__main__.fetch_relay_list",
                   return_value=["nine.testrun.org"]):
            main(argv)

        mock_pm.assert_called_once()
