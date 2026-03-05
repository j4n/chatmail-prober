"""Tests for textfile output (atomic write)."""

import os

from chatmail_prober.output import write_textfile
from chatmail_prober.prober import ProbeResult
from chatmail_prober.metrics import update_metrics


class TestWriteTextfile:
    def test_writes_valid_prom_file(self, tmp_path):
        # Feed some data into metrics first
        result = ProbeResult("a.test", "b.test", sent=3, received=3, loss=0.0,
                             rtts_ms=[100.0, 200.0, 300.0])
        update_metrics(result)

        out = tmp_path / "test.prom"
        write_textfile(str(out))

        content = out.read_text()
        assert "cmping_requests_total" in content
        assert "cmping_response_duration_seconds" in content
        assert content.endswith("\n")

    def test_atomic_write_no_partial_file(self, tmp_path):
        out = tmp_path / "test.prom"
        write_textfile(str(out))

        # No leftover .tmp files
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []

    def test_overwrites_existing_file(self, tmp_path):
        out = tmp_path / "test.prom"
        out.write_text("old content")

        write_textfile(str(out))
        content = out.read_text()
        assert "old content" not in content
        assert "# HELP" in content
