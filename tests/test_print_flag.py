"""Tests for the --print CLI flag.

Behaviour under test:
  - --print is accepted by the argument parser; defaults to False
  - --once alone does NOT print the cli_summary
  - --once --print DOES print the cli_summary
  - --print without --once is silently ignored (no crash)
"""

from unittest.mock import patch

import pytest

from chatmail_prober.__main__ import parse_args


class TestPrintArgument:
    """--print must be accepted by the argument parser."""

    def test_print_flag_accepted(self, tmp_path):
        relay_file = tmp_path / "relays.txt"
        relay_file.write_text("nine.testrun.org\n")
        args = parse_args([str(relay_file), "--print"])
        assert args.print_summary is True

    def test_print_defaults_to_false(self, tmp_path):
        relay_file = tmp_path / "relays.txt"
        relay_file.write_text("nine.testrun.org\n")
        args = parse_args([str(relay_file)])
        assert args.print_summary is False


class TestPrintSummaryBehaviour:
    """render_summary must only be called when --print is given."""

    def _run_main_once(self, tmp_path, extra_flags=()):
        from chatmail_prober.__main__ import main

        relay_file = tmp_path / "relays.txt"
        relay_file.write_text("nine.testrun.org\n")
        argv = [str(relay_file), "--once"] + list(extra_flags)

        with patch("chatmail_prober.__main__.check_relays_alive",
                   return_value=(["nine.testrun.org"], set())), \
             patch("chatmail_prober.__main__.run_round",
                   return_value=(0.1, [])), \
             patch("chatmail_prober.__main__.render_summary") as mock_render, \
             patch("chatmail_prober.__main__.print_metrics"), \
             patch("chatmail_prober.__main__.write_textfile"), \
             patch("chatmail_prober.__main__.fetch_relay_list",
                   return_value=["nine.testrun.org"]):
            main(argv)

        return mock_render

    def test_once_without_print_does_not_call_render_summary(self, tmp_path):
        """--once alone must NOT call render_summary."""
        mock_render = self._run_main_once(tmp_path)
        mock_render.assert_not_called()

    def test_once_with_print_calls_render_summary(self, tmp_path):
        """--once --print must call render_summary exactly once."""
        mock_render = self._run_main_once(tmp_path, extra_flags=["--print"])
        mock_render.assert_called_once()

    def test_render_summary_receives_correct_args(self, tmp_path):
        """render_summary must be called with (results, alive, dead, elapsed_s=...)."""
        from chatmail_prober.__main__ import main

        relay_file = tmp_path / "relays.txt"
        relay_file.write_text("nine.testrun.org\n")
        argv = [str(relay_file), "--once", "--print"]

        with patch("chatmail_prober.__main__.check_relays_alive",
                   return_value=(["nine.testrun.org"], set())), \
             patch("chatmail_prober.__main__.run_round",
                   return_value=(1.5, [])) as mock_round, \
             patch("chatmail_prober.__main__.render_summary") as mock_render, \
             patch("chatmail_prober.__main__.print_metrics"), \
             patch("chatmail_prober.__main__.write_textfile"), \
             patch("chatmail_prober.__main__.fetch_relay_list",
                   return_value=["nine.testrun.org"]):
            main(argv)

        call_kwargs = mock_render.call_args
        # elapsed_s must be the float returned by run_round
        assert call_kwargs.kwargs.get("elapsed_s") == 1.5
