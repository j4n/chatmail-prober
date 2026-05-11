"""Pure parser for `turnutils_uclient` output.

No prometheus, no rpc, no prober-specific deps -- safe to import from the
standalone textfile collector under scripts/ as well as from the in-process
prober.  All time values are normalized to seconds at parse time.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

# -D channel padding, -X IPv4 relay, -c no RTCP, -y loopback (both endpoints).
# -g (DONT_FRAGMENT) is omitted because at least one chatmail coturn rejects
# it with TURN error 420 (Unknown Attribute).
UCLIENT_FLAGS: tuple[str, ...] = ("-D", "-X", "-c", "-y")

# Default subprocess timeout for one uclient run.
DEFAULT_TIMEOUT = 30


def build_uclient_argv(
    host: str,
    port: int,
    username: str,
    credential: str,
) -> list[str]:
    """Assemble the turnutils_uclient command line.

    `host` is a bare hostname or IPv4/IPv6 address (no brackets, no port);
    port is passed via `-p`.
    """
    return [
        "turnutils_uclient", host,
        "-p", str(port),
        *UCLIENT_FLAGS,
        "-u", username,
        "-w", credential,
        "-e", host,
    ]


# Compiled once at module load.
_CONNECT_RE   = re.compile(r"Total connect time is (\d+)")
_TRANSMIT_RE  = re.compile(r"Total transmit time is (\d+)")
_LOST_RE      = re.compile(r"Total lost packets (\d+)")
_DROPPED_RE   = re.compile(r"total send dropped (\d+)")
_RTT_RE       = re.compile(
    r"Average round trip delay ([\d.]+) ms; min = (\d+) ms, max = (\d+) ms"
)
_JITTER_RE    = re.compile(
    r"Average jitter ([\d.]+) ms; min = (\d+) ms, max = (\d+) ms"
)


@dataclass(frozen=True, slots=True)
class TurnRun:
    """Outcome of one turnutils_uclient invocation.

    All time fields are seconds (ms output of uclient is converted here so
    downstream metric and dashboard code never sees milliseconds).
    """
    ok: bool
    returncode: int
    error: str | None = None
    connect_s: float | None = None
    transmit_s: float | None = None
    rtt_avg_s: float | None = None
    rtt_min_s: float | None = None
    rtt_max_s: float | None = None
    jitter_avg_s: float | None = None
    jitter_min_s: float | None = None
    jitter_max_s: float | None = None
    lost_packets: int | None = None
    send_dropped: int | None = None


def _ms_to_s(m: re.Match[str] | None, group: int = 1) -> float | None:
    """Convert a ms-valued regex group to seconds, or None if no match."""
    return float(m.group(group)) / 1000.0 if m else None


def parse_uclient_output(stdout: str, stderr: str, returncode: int) -> TurnRun:
    """Parse combined stdout+stderr from turnutils_uclient into a TurnRun."""
    out = stdout + stderr
    rtt    = _RTT_RE.search(out)
    jitter = _JITTER_RE.search(out)
    lost    = _LOST_RE.search(out)
    dropped = _DROPPED_RE.search(out)
    return TurnRun(
        ok=(returncode == 0),
        returncode=returncode,
        connect_s    = _ms_to_s(_CONNECT_RE.search(out)),
        transmit_s   = _ms_to_s(_TRANSMIT_RE.search(out)),
        lost_packets = int(lost.group(1)) if lost else None,
        send_dropped = int(dropped.group(1)) if dropped else None,
        rtt_avg_s    = _ms_to_s(rtt, 1),
        rtt_min_s    = _ms_to_s(rtt, 2),
        rtt_max_s    = _ms_to_s(rtt, 3),
        jitter_avg_s = _ms_to_s(jitter, 1),
        jitter_min_s = _ms_to_s(jitter, 2),
        jitter_max_s = _ms_to_s(jitter, 3),
    )


def run_uclient(
    host: str,
    port: int,
    username: str,
    credential: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> TurnRun:
    """Run turnutils_uclient against (host, port) with the given credentials.

    Classifies environmental failures (missing binary, subprocess timeout)
    into a TurnRun with ok=False and a descriptive error tag.
    """
    argv = build_uclient_argv(host, port, username, credential)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError:
        return TurnRun(ok=False, returncode=-1, error="binary-missing")
    except subprocess.TimeoutExpired:
        return TurnRun(ok=False, returncode=-1, error="timeout")
    return parse_uclient_output(proc.stdout, proc.stderr, proc.returncode)
