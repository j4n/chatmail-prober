# chatmail-prober: Technical Reference

Smokeping-style Prometheus exporter for chatmail relay interoperability.
Probes all pairs of configured chatmail relays via Delta Chat message
round-trips and exports RTT histograms, loss ratios, and timing metrics.

**Viewing this with rendered diagrams**: Either through GitHub or, for example, https://www.mermditor.dev/editor

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Repository Layout](#2-repository-layout)
3. [Component Architecture](#3-component-architecture)
4. [Probe Lifecycle](#4-probe-lifecycle)
5. [Thread Model](#5-thread-model)
6. [Worker Isolation](#6-worker-isolation)
7. [Metrics Pipeline](#7-metrics-pipeline)
8. [Shutdown Sequence](#8-shutdown-sequence)
9. [cmping Library Internals](#9-cmping-library-internals)
10. [Grafana Dashboards](#10-grafana-dashboards)
11. [Configuration Reference](#11-configuration-reference)
12. [Development Guide](#12-development-guide)
13. [Known Failure Modes](#13-known-failure-modes)
14. [Deployment](#14-deployment)

---

## 1. System Overview

```mermaid
graph LR
    subgraph Relays["Chatmail Relays (N domains)"]
        R1[relay-a.example]
        R2[relay-b.example]
        RN[relay-n.example]
    end

    subgraph Prober["chatmail-prober process"]
        MAIN["Orchestrator
            __main__.py"]
        PROBE["run_probe
            prober.py"]
        CMPING["perform_ping
            cmping-src/cmping.py"]
        DCSERVER["deltachat-rpc-server
            subprocess per probe"]
        METRICS["Prometheus Metrics
            metrics.py"]
        HTTP["HTTP /metrics
            port 9740"]
        TEXTFILE[".prom textfile
            node_exporter"]
    end

    subgraph Grafana
        PROM[Prometheus]
        DASH[Grafana Dashboards]
    end

    MAIN -->|"N^2 pairs"| PROBE
    PROBE --> CMPING
    CMPING --> DCSERVER
    DCSERVER <-->|SMTP/IMAP| R1
    DCSERVER <-->|SMTP/IMAP| R2
    DCSERVER <-->|SMTP/IMAP| RN
    PROBE -->|ProbeResult| METRICS
    METRICS --> HTTP
    METRICS --> TEXTFILE
    HTTP --> PROM
    TEXTFILE --> PROM
    PROM --> DASH
```

The prober generates **N^2 pairs** (including self-loops A->A for baseline)
from N relays, distributes them across W worker threads, and repeats every
`--interval` seconds.

---

## 2. Repository Layout

```
chatmail-prober/
├── chatmail_prober/         # Main package
│   ├── __main__.py          # CLI, orchestration, signal handling
│   ├── prober.py            # run_probe() wrapper + ProbeResult dataclass
│   ├── metrics.py           # Prometheus metric objects + update_metrics()
│   └── output.py            # HTTP server + atomic textfile writer
├── cmping-src/              # Git submodule: fork of cmping library
│   └── cmping.py            # perform_ping(), Pinger, AccountMaker
├── tests/
│   ├── test_main.py         # Orchestration unit tests (mocked probes)
│   ├── test_metrics.py      # Metric computation unit tests
│   ├── test_prober.py       # run_probe() unit tests (mocked perform_ping)
│   ├── test_output.py       # Textfile writer unit tests
│   ├── test_thread_leak.py  # Thread cleanup tests (no mocking, real threads)
│   └── test_live.py         # Live integration tests (CMPING_LIVE_TEST=...)
├── grafana/
│   ├── dashboard-inter.json # Cross-relay RTT matrix
│   ├── dashboard-single.json# Single-relay smokeping view
│   └── smokeping_panel.py   # Panel JSON generator
├── pyproject.toml           # Project metadata (uv/hatchling)
├── Makefile                 # install / install-dev / test / clean
```

---

## 3. Component Architecture

```mermaid
graph TD
    subgraph chatmail_prober
        MAIN["__main__.py
            parse_args()
            check_relays_alive()
            run_round()
            main()"]
        PROBER["prober.py
            ProbeResult
            run_probe()"]
        METRICS["metrics.py
            update_metrics()
            CMPING_REGISTRY"]
        OUTPUT["output.py
            start_exporter_server()
            write_textfile()"]
    end

    subgraph cmping_src["cmping-src (editable install)"]
        CMPING["cmping.py
            perform_ping()
            Pinger
            AccountMaker"]
    end

    subgraph deltachat["deltachat-rpc-client (library)"]
        RPC["Rpc
            DeltaChat
            Account"]
    end

    DCSERVER["deltachat-rpc-server
        subprocess"]

    MAIN -->|"run_probe(src, dst, ...)"| PROBER
    PROBER -->|"perform_ping(args, accounts_dir)"| CMPING
    CMPING -->|"Rpc.__enter__()"| RPC
    RPC -->|"subprocess.Popen"| DCSERVER
    PROBER -->|ProbeResult| MAIN
    MAIN -->|update_metrics| METRICS
    METRICS --> OUTPUT

    style DCSERVER fill:#f9f,stroke:#333
```

### Key design decisions

- **cmping is a library fork**: upstream cmping is a CLI tool that calls
  `sys.exit()`. This fork replaces exits with `CMPingError` and adds a
  `perform_ping()` API (accounts_dir, timeout, returns Pinger object).
- **print is suppressed**: `prober.py` monkey-patches `builtins.print` to a
  no-op at import time so cmping's stdout output is silenced in daemon mode.
  Restored at `-vv` and above.
- **One deltachat-rpc-server subprocess per probe**: each `perform_ping()`
  call starts (and cleans up) its own `deltachat-rpc-server` process.

---

## 4. Probe Lifecycle

One end-to-end probe between `src` and `dst`:

```mermaid
sequenceDiagram
    participant W as Worker Thread
    participant RP as run_probe()
    participant PP as perform_ping()
    participant RPC1 as Rpc(src)
    participant RPC2 as Rpc(dst)
    participant DC1 as deltachat-rpc-server (src)
    participant DC2 as deltachat-rpc-server (dst)
    participant SM as SMTP/IMAP relays

    W->>RP: run_probe(src, dst, count, ...)
    RP->>PP: perform_ping(args, accounts_dir, timeout)

    Note over PP,DC2: Phase 1 -- Account Setup (timed)
    PP->>RPC1: Rpc.__enter__()
    RPC1->>DC1: subprocess.Popen
    PP->>RPC2: Rpc.__enter__()
    RPC2->>DC2: subprocess.Popen
    PP->>DC1: create/reuse sender account on src
    PP->>DC2: create/reuse receiver account on dst
    PP->>DC1: wait for IMAP_INBOX_IDLE
    PP->>DC2: wait for IMAP_INBOX_IDLE

    Note over PP,DC2: Phase 2 -- Group Join (timed)
    PP->>DC1: create group, add receiver, send init msg
    DC1->>SM: SMTP -> src relay -> dst relay -> IMAP
    SM->>DC2: deliver invitation
    PP->>DC2: wait for receiver to join group

    Note over PP,DC2: Phase 3 -- Ping/Pong (timed)
    PP->>DC1: Pinger._send_thread starts
    loop count times
        DC1->>SM: send "tx_id timestamp seq"
        SM->>DC2: deliver
        DC2->>PP: receiver_thread: INCOMING_MSG event
        PP->>PP: RTT = (now - timestamp) * 1000
    end

    PP->>RPC1: Rpc.__exit__() -> terminate DC1
    PP->>RPC2: Rpc.__exit__() -> terminate DC2
    PP-->>RP: Pinger (sent, received, loss, results[])
    RP-->>W: ProbeResult
```

### Probe message format

```
<30-char alphanum tx_id> <unix_timestamp_float> <seq_number_padded_17>
e.g.: "abcdefghijklmnopqrstuvwxyz0123 1734567890.1234            0"
```

RTT is computed by the receiver: `(time.time() - float(parts[1])) * 1000` ms.

### Timing phases stored in ProbeResult

| Field | Measures |
|---|---|
| `account_setup_time` | Rpc start + IMAP_INBOX_IDLE wait |
| `group_join_time` | Group creation + receiver acceptance |
| `message_time` | Full send+receive loop |

---

## 5. Thread Model

Two workers running concurrently, each with an active Pinger:

```mermaid
graph TD
    MT["Main Thread
        run_round loop
        signal handlers"]

    subgraph Executors["Worker Executors  (one ThreadPoolExecutor per --workers)"]
        W0["Worker-0 Thread
            sequential pairs"]
        W1["Worker-1 Thread
            sequential pairs"]
        WN["..."]
    end

    MT --> W0 & W1 & WN

    subgraph Probe0["Pinger: relay-A to relay-B  (Worker-0, active)"]
        ST0["_send_thread
            daemon
            sends N pings
            exits at deadline"]
        RT0["receiver_thread
            daemon
            polls account_queue
            checks stop_event"]
        subgraph RPC0["per Rpc context  (one per relay: x2 for cross, x1 for self)"]
            EV0["events_thread
                daemon"]
            RW0["reader_thread
                writer_thread"]
        end
    end

    subgraph Probe1["Pinger: relay-C to relay-D  (Worker-1, active)"]
        ST1["_send_thread
            daemon
            sends N pings
            exits at deadline"]
        RT1["receiver_thread
            daemon
            polls account_queue
            checks stop_event"]
        subgraph RPC1["per Rpc context  (one per relay: x2 for cross, x1 for self)"]
            EV1["events_thread
                daemon"]
            RW1["reader_thread
                writer_thread"]
        end
    end

    W0 --> ST0 & RT0 & RPC0
    W1 --> ST1 & RT1 & RPC1
```

### Thread lifetimes

| Thread | Created by | Lifetime | Cleanup |
|---|---|---|---|
| Worker threads | `ThreadPoolExecutor(max_workers=1)` | Entire process | `executor.shutdown()` |
| `_send_thread` | `Pinger.__init__` | Until all pings sent (or deadline) | `join(timeout=2.0)` in `perform_ping` |
| `receiver_thread` | `Pinger.receive()` | Until `stop_event.set()` | `finally: stop_event.set(); join(timeout=2.0)` |
| `events_thread` | `Rpc.start()` | Until `Rpc.close()` | `Rpc.close()` joins it |
| `reader_thread` | `Rpc.start()` | Until stdout closed | `Rpc.close()` joins it |
| `writer_thread` | `Rpc.start()` | Until stdin closed | `Rpc.close()` joins it |

---

## 6. Worker Isolation

Pairs are distributed **round-robin** across W workers so each worker
processes its pairs sequentially:

```mermaid
graph LR
    subgraph Pairs["N^2 = 9 pairs (3 relays)"]
        direction TB
        P0["pair 0: A->A"]
        P1["pair 1: A->B"]
        P2["pair 2: A->C"]
        P3["pair 3: B->A"]
        P4["pair 4: B->B"]
        P5["pair 5: B->C"]
        P6["pair 6: C->A"]
        P7["pair 7: C->B"]
        P8["pair 8: C->C"]
    end

    subgraph W0["Worker-0  cache/worker-0/"]
        P0 --> P3 --> P6
    end
    subgraph W1["Worker-1  cache/worker-1/"]
        P1 --> P4 --> P7
    end
    subgraph W2["Worker-2  cache/worker-2/"]
        P2 --> P5 --> P8
    end
```

Why sequential within a worker? `deltachat-rpc-server` uses SQLite and holds
an exclusive lock on `accounts.lock`.  If two probes for the same relay ran
concurrently under the same accounts_dir, the second would fail with a lock
error.  Per-worker directories ensure each `Rpc` context sees only its own
accounts.

### Account directory layout

```
~/.cache/chatmail-prober/
├── worker-0/
│   ├── relay-a.example/     # Rpc accounts_dir for relay-a
│   │   ├── accounts.toml
│   │   └── ...
│   └── relay-b.example/
├── worker-1/
│   └── ...
└── alive-check/             # used only during pre-flight alive check
```

---

## 7. Metrics Pipeline

```mermaid
flowchart LR
    PR["ProbeResult
        src, dst
        rtts_ms list
        loss
        error"]

    subgraph update_metrics
        direction TB
        CHK{error?}
        ERR["increment send_errors_total
            set probe_success=0
            set loss_ratio=1.0"]
        OK["compute median/p10/p90/stddev
            set probe_success=1 if loss==0
            set loss_ratio
            set account_setup_seconds"]
    end

    subgraph CMPING_REGISTRY["CMPING_REGISTRY (custom)"]
        M1[cmping_rtt_median_seconds]
        M2[cmping_rtt_stddev_seconds]
        M3[cmping_rtt_p90_seconds]
        M4[cmping_rtt_p10_seconds]
        M5[cmping_probe_success]
        M6[cmping_probe_loss_ratio]
        M7[cmping_send_errors_total]
        M8[cmping_account_setup_seconds]
    end

    PR --> CHK
    CHK -->|yes| ERR
    CHK -->|no| OK
    ERR --> M5
    ERR --> M6
    ERR --> M7
    ERR -->|"set NaN"| M1
    ERR -->|"set NaN"| M2
    ERR -->|"set NaN"| M3
    ERR -->|"set NaN"| M4
    OK --> M1
    OK --> M2
    OK --> M3
    OK --> M4
    OK --> M5
    OK --> M6
    OK --> M8

    M1 & M2 & M3 & M4 & M5 & M6 & M7 & M8 -->|"text/plain"| HTTP[":9740/metrics"]
    M1 & M2 & M3 & M4 & M5 & M6 & M7 & M8 -->|"atomic write"| TEXTFILE[".prom file"]
```

### Labels

Per-pair metrics carry three labels:

| Label | Values |
|---|---|
| `source` | source relay domain |
| `destination` | destination relay domain |
| `probe_type` | `"self"` if src==dst, `"cross"` otherwise |

Round-level metrics (`cmping_last_round_completion_timestamp`,
`cmping_round_duration_seconds`) have no labels.

### Error behavior

On probe error, RTT gauges (median, p10, p90, stddev) and
`account_setup_seconds` are set to NaN so Grafana panels show a gap
instead of stale values from the previous successful round.

### Stale label cleanup

At the start of each round, `clear_stale_labels()` removes metric label
sets for relays no longer in the active set.  This prevents label
cardinality from growing unbounded when relays are removed.

### Registry split

Two registries exist to avoid double-counting process metrics:

- `CMPING_REGISTRY`: only cmping metrics -- used for textfile output (no
  `process_*` or `python_*` collectors that node_exporter would also expose)
- default `REGISTRY`: everything -- used by the HTTP endpoint

---

## 8. Shutdown Sequence

```mermaid
stateDiagram-v2
    [*] --> Running: process starts
    Running --> GracefulShutdown: SIGINT / SIGTERM (first)
    Running --> WaitForRound: SIGUSR1
    WaitForRound --> GracefulShutdown: current round finishes

    GracefulShutdown --> Cleanup: shutdown_event set
    Cleanup --> Exit0: 5s timer fires os._exit(0)

    Running --> HardKill: SIGINT (second)
    HardKill --> [*]: os._exit(1)

    note right of GracefulShutdown
        Cancels pending futures.
        Kills stale rpc-server processes.
        Worker threads may be stuck in I/O;
        os._exit() bypasses thread join,
        letting the OS reap zombies.
    end note
```

The 5-second timer in the signal handler fires `os._exit(0)` as a backstop if
the main thread stays stuck in `as_completed()` after all futures are cancelled.

---

## 9. cmping Library Internals

```mermaid
classDiagram
    class perform_ping {
        +args: Namespace
        +accounts_dir: Path
        +timeout: float
        ---
        Creates RelayContext per relay
        Orchestrates 3 timed phases
        Cleans up RPC in finally
    }

    class RelayContext {
        +rpc: Rpc
        +dc: DeltaChat
        +maker: AccountMaker
    }

    class AccountMaker {
        +dc: DeltaChat
        +verbose: int
        +get_relay_account(domain) Account
        +wait_all_online(timeout)
    }

    class Pinger {
        +sent: int
        +received: int
        +loss: float
        +results: list
        +account_setup_time: float
        +group_join_time: float
        +message_time: float
        +deadline: float
        ---
        +send_pings() thread
        +receive() generator
    }

    class Rpc {
        +process: Popen
        +event_queues: dict
        +start()
        +close()
        +wait_for_event(account_id) dict
        +get_queue(account_id) Queue
    }

    perform_ping --> RelayContext : creates per relay
    RelayContext --> AccountMaker
    RelayContext --> Rpc
    perform_ping --> Pinger : creates for phase 3
    Pinger --> Rpc : get_queue() for receiver threads
```

### perform_ping phases

```mermaid
flowchart TD
    A[perform_ping called] --> B

    subgraph B["Phase 1: Account Setup"]
        B1["Rpc.__enter__ per relay
            spawns deltachat-rpc-server"]
        B2["create/reuse accounts
            via AccountMaker"]
        B3["wait_profiles_online_multi
            block until IMAP_INBOX_IDLE"]
        B1 --> B2 --> B3
    end

    B --> C

    subgraph C["Phase 2: Group Join"]
        C1["sender creates group
            adds receiver accounts"]
        C2["send init message
            promotes group"]
        C3["wait_for_receivers_to_join
            per-receiver threads + spinner"]
        C1 --> C2 --> C3
    end

    C --> D

    subgraph D["Phase 3: Ping/Pong"]
        D1["Pinger.__init__
            start _send_thread
            set deadline"]
        D2["Pinger.receive
            start receiver_threads
            yield RTT measurements"]
        D3["pinger._send_thread.join timeout=2s"]
        D1 --> D2 --> D3
    end

    D --> E["finally: Rpc.__exit__ per relay
        terminate deltachat-rpc-server"]
    E --> F[return Pinger]
```

---

## 10. Grafana Dashboards

| File | Purpose | Key panels |
|---|---|---|
| `dashboard-inter.json` | Cross-relay RTT matrix | Heatmap, pair table, per-pair smokeping |
| `dashboard-single.json` | Single relay deep-dive | Smokeping plot, peer RTT breakdown |

All dashboards use a `$datasource` template variable to select the Prometheus
instance.  RTT panels use three queries (p10, median, p90) to draw the
Smokeping-style gray band + colored median line.

### Smokeping panel query pattern

```promql
# Median (solid line, color = threshold)
cmping_rtt_median_seconds{probe_type="cross", source="$relay"}

# p90 (upper band edge, hidden)
cmping_rtt_p90_seconds{...}

# p10 (lower band edge, hidden)
cmping_rtt_p10_seconds{...}
```

Thresholds: dark-green <2s, green <6s, light-green <24s, yellow <48s, red <60s, dark-red >=60s.

---

## 11. Configuration Reference

```
chatmail-prober [relays_file ...] [options]
```

| Flag | Default | Description |
|---|---|---|
| `relays` | (optional) | One or more relay list files; merged and deduplicated |
| `--auto-fetch PATH` | `None` | Fetch relay list from upstream URL, write to PATH, add to sources |
| `--port` | `0` | HTTP /metrics listen port (0 = disabled) |
| `--textfile` | `None` | Path for node_exporter textfile .prom output |
| `--interval` | `900` | Seconds between probe rounds |
| `--count` | `5` | Pings per pair per round |
| `--ping-interval` | `0.1` | Seconds between individual pings within a probe |
| `--timeout` | `60` | Per-pair receive timeout in seconds |
| `--workers` | `5` | Concurrent worker threads |
| `--cache-dir` | `~/.cache/chatmail-prober` | Root for per-worker account dirs |
| `--exclude PATH` | `None` | File of pairs to skip: `src->dst` per line |
| `--once` | false | Run one round then exit (useful with --textfile in cron) |
| `--scan` | false | Self-probe all relays in parallel, print ranked by RTT, exit |
| `--top N` | `10` | Relays to highlight in --scan output |
| `-v` / `-vv` / `-vvv` | 0 | Verbosity: debug / cmping errors / cmping events |
| `-q` | false | Quiet: suppress progress, show only warnings/errors |

At least one of `relays` or `--auto-fetch` must be provided.  When both are
given, domains from all sources are merged and deduplicated.  The default
fetch URL is `https://chatmail.at/relays` (parses `<a class="hilite">` entries).

### Relay list format

```
# Lines starting with # are ignored
# Blank lines are ignored
nine.testrun.org
mehl.cloud
tarpit.fun
```

### Exclude list format

```
# Skip known-broken pairs (one per line)
broken-relay.example -> mehl.cloud
tarpit.fun -> broken-relay.example
```

---

## 12. Development Guide

### Setup

```bash
uv sync --dev      # creates .venv, installs cmping-src editable + pytest
# or:
make install-dev
```

### Running tests

```bash
uv run python -m pytest tests/ --ignore=tests/test_live.py -v   # all unit tests
make test                                                        # same via Makefile

# Live integration tests (requires real relay access):
CMPING_LIVE_TEST=nine.testrun.org uv run python -m pytest tests/test_live.py -v
CMPING_LIVE_TEST=nine.testrun.org,mehl.cloud uv run python -m pytest tests/test_live.py -v
```

### Test categories

| File | Requires network | Mocks |
|---|---|---|
| `test_main.py` | No | `run_probe` monkeypatched |
| `test_metrics.py` | No | Fresh prometheus registry |
| `test_prober.py` | No | `perform_ping` monkeypatched |
| `test_output.py` | No | None |
| `test_thread_leak.py` | No | None -- real threads + queues |
| `test_live.py` | Yes | None -- full stack |

### cmping-src changes

Changes to `cmping-src/` must be PR-quality:
- Preserve existing CLI behavior exactly
- Only add new opt-in functionality
- Keep comments minimal and non-rephrased
- Commit inside the submodule first, then update the parent pointer

cmping is an editable install (`pyproject.toml: cmping = { path = "cmping-src", editable = true }`),
so changes to `cmping-src/cmping.py` are immediately reflected without reinstall.

### Adding a metric

1. Add a `Gauge`/`Counter` to `metrics.py` using `CMPING_REGISTRY`
2. Set it in `update_metrics()` in `metrics.py`
3. Add the label set to the fixture in `tests/test_metrics.py`

### Debugging high thread counts

```bash
# Count live threads in the running process
cat /proc/<pid>/status | grep Threads   # should stay near constant across rounds

# Safe graceful stop (waits for current round to finish)
kill -USR1 <pid>

# Verbose probe output for one round
chatmail-prober relays.txt --once -vvv 2>&1 | head -200
```

---

## 13. Known Failure Modes

### Probe round exceeds interval

**Symptom**: Log line `"Probe round took Xs, exceeds interval Ys -- starting next immediately"`.

**Cause**: N^2 pairs with high --timeout, slow relays, or too few workers.

**Tuning**: Reduce `--timeout`, increase `--workers`, or reduce N (fewer relays).
The round time is roughly `ceil(N^2 / workers) * avg_probe_time`.

### Dead relay contaminates metrics

**Symptom**: A relay is down but metrics still show last round's values.

**Handling**: The pre-flight `check_relays_alive()` runs a count=1 self-probe
on every relay and excludes dead ones from the matrix for that round.
Dead relays appear in `cmping_send_errors_total` and `cmping_probe_success=0`.

#### Fixed

### MemoryError / "can't start new thread"

**Symptom**: `MemoryError` in `threading._after_fork`, followed by
`can't start new thread` errors mid-round.

**Root cause**: Thread accumulation. Either:
- Receiver threads not exiting (fixed: was `queue.get()` with no timeout)
- Overall heap growth across many rounds (GC every 50 pairs is in place)

**Diagnosis**:
```bash
cat /proc/<pid>/status | grep Threads   # should stay near constant
```

**Mitigation**: Restart the process if thread count grows unexpectedly.
The fix in `cmping.py:Pinger.receive()` addresses the primary cause.

### deltachat-rpc-server orphans after crash

**Symptom**: `accounts.lock` still held, next startup fails to acquire lock.

**Handling**: On startup, `__main__.py` runs:
```python
_kill_stale_rpc_servers(cache_dir)   # pgrep -f deltachat-rpc-server.*cache_dir
for lock in cache_dir.rglob("accounts.lock"):
    lock.unlink(missing_ok=True)
```

### File descriptor exhaustion (fixed)

**Symptom**: `OSError: [Errno 24] Too many open files` from deltachat or IMAP.

**Handling**: On startup, fd soft limit is raised to the hard limit:
```python
resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
```

Systemd units may also need `LimitNOFILE=65536` in the `[Service]` section.

---

## 14. Deployment

Three unit files live in `systemd/`:

| File | Type | Purpose |
|---|---|---|
| `chatmail-prober.service` | service | Long-running prober daemon |
| `chatmail-prober-prom-copy.path` | path | Watches the `.prom` textfile for writes |
| `chatmail-prober-prom-copy.service` | service (oneshot) | Copies `.prom` into node-exporter's directory |

### System user

The prober runs as a dedicated unprivileged system account whose home
directory doubles as the uv install root:

```bash
sudo useradd -r -s /usr/sbin/nologin -d /opt/chatmail-prober chatmail-prober
```

### Paths

| Path | Purpose |
|---|---|
| `/opt/chatmail-prober/` | Home dir for the service user; uv installs to `.local/bin/uv` here |
| `/opt/chatmail-prober/chatmail-prober/` | Git repo (`WorkingDirectory`) |
| `/var/lib/chatmail-prober/relays.txt` | Relay list cache written by `--auto-fetch` on each startup |
| `/var/lib/chatmail-prober/` | Per-worker account cache and state (created by `StateDirectory=`) |
| `/var/tmp/chatmail-prober.prom` | Textfile written atomically by the prober |
| `/var/lib/prometheus/node-exporter/chatmail-prober.prom` | Destination for node-exporter |

### Bootstrap

```bash
# Create home dir and system user (home doubles as uv install root)
sudo mkdir /opt/chatmail-prober
sudo useradd -r -s /usr/sbin/nologin -d /opt/chatmail-prober chatmail-prober
sudo chown chatmail-prober:chatmail-prober /opt/chatmail-prober

# Clone the repo and install dependencies as the service user
sudo -u chatmail-prober git clone --recurse-submodules \
    https://github.com/chatmail/chatmail-prober \
    /opt/chatmail-prober/chatmail-prober
sudo -u chatmail-prober sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
sudo -u chatmail-prober sh -c 'cd /opt/chatmail-prober/chatmail-prober && make install'
```

### Installing systemd units

```bash
sudo cp /opt/chatmail-prober/chatmail-prober/systemd/chatmail-prober.service \
       /opt/chatmail-prober/chatmail-prober/systemd/chatmail-prober-prom-copy.path \
       /opt/chatmail-prober/chatmail-prober/systemd/chatmail-prober-prom-copy.service \
       /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chatmail-prober.service
sudo systemctl enable --now chatmail-prober-prom-copy.path
```

`StateDirectory=chatmail-prober` causes systemd to create and own
`/var/lib/chatmail-prober` on first start; no manual `mkdir` is needed.

The path unit fires `chatmail-prober-prom-copy.service` immediately each
time `/var/tmp/chatmail-prober.prom` is modified (including the atomic
rename the prober uses), so node-exporter always sees a fresh file within
seconds of a completed round.

### Graceful restart

`ExecReload=kill -s USR1 $MAINPID` wires SIGUSR1 into the standard reload
interface.  SIGUSR1 tells the running prober to exit cleanly after the
current probe round finishes; `Restart=always` then starts a fresh process.

```bash
# Wait for the current round to finish, then restart:
sudo systemctl reload chatmail-prober

# Check status / recent log output:
systemctl status chatmail-prober
journalctl -u chatmail-prober -f
```

`check_relays_alive()` runs at the start of every round, so previously-dead
relays are rechecked automatically without manual intervention.
