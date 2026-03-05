# chatmail-prober

Smokeping-style Prometheus exporter for [chatmail](https://chatmail.at) relay interoperability monitoring.

Periodically probes all pairs of configured chatmail relays using [cmping](https://github.com/chatmail/cmping) and exposes round-trip time histograms, counters, and success gauges as Prometheus metrics.

## Quick start

```bash
git clone --recurse-submodules <repo-url>
cd chatmail-prober

# Create a relay list (see https://chatmail.at/relays for public relays)
cat > relays.txt << EOF
host1.example
host2.example
host3.example
EOF

# Install and run (pick one)
make install                                          # pip + venv
.venv/bin/chatmail-prober --relays relays.txt -v

# — or with uv —
uv run chatmail-prober --relays relays.txt -v
```

Then `curl http://localhost:9740/metrics` to see the metrics.

## How it works

For N relays, chatmail-prober tests all N² ordered pairs (including self-loops A→A as baseline health checks). Each pair runs `cmping` with a configurable number of pings, producing individual round-trip time measurements that feed into Prometheus histograms — giving you smokeping-style latency distribution data.

Pairs are probed in parallel using a thread pool. Each pair gets its own accounts directory to avoid the deltachat-rpc-server database lock, and accounts are reused across rounds.

## Metrics

| Metric | Type | Description |
|---|---|---|
| `cmping_requests_total` | Counter | Ping messages sent |
| `cmping_responses_total` | Counter | Ping messages received |
| `cmping_response_duration_seconds` | Histogram | Per-message round-trip time |
| `cmping_send_errors_total` | Counter | Failed probe rounds |
| `cmping_probe_success` | Gauge | 1 if last round succeeded, 0 if not |
| `cmping_account_setup_seconds` | Gauge | Account setup time |

All metrics have `source` and `destination` labels.

Histogram buckets are tuned for email round-trips: 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60 seconds.

## Output modes

chatmail-prober supports two output modes (or both simultaneously):

```bash
# HTTP exporter (default, Prometheus scrapes this)
chatmail-prober --relays relays.txt --port 9740

# Textfile for node_exporter's textfile collector
chatmail-prober --relays relays.txt --port 0 --textfile /var/lib/prometheus/node-exporter/cmping.prom

# Both
chatmail-prober --relays relays.txt --port 9740 --textfile /var/lib/prometheus/node-exporter/cmping.prom
```

## CLI options

```
--relays PATH       Relay list file, one domain per line (required)
--port PORT         HTTP listen port (default: 9740, 0 to disable)
--textfile PATH     Write .prom file for node_exporter textfile collector
--interval SECS     Seconds between probe rounds (default: 900 = 15min)
--count N           Pings per pair per round (default: 10)
--ping-interval S   Seconds between pings within a probe (default: 1.1)
--workers N         Max concurrent probe threads (default: 5)
--cache-dir PATH    Per-pair accounts directory (default: ~/.cache/chatmail-prober)
--timeout SECS      Per-pair timeout (default: 120)
--once              Run one round then exit
-v / -vv            Increase logging verbosity
```

## Deployment

### systemd

```ini
[Unit]
Description=chatmail-prober
After=network-online.target

[Service]
ExecStart=/opt/chatmail-prober/.venv/bin/chatmail-prober --relays /etc/chatmail-prober/relays.txt
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

### Prometheus scrape config

```yaml
- job_name: cmping
  scrape_interval: 5m
  static_configs:
    - targets: ['localhost:9740']
```

## Development

```bash
# With make (creates .venv automatically)
make install-dev
make test

# — or with uv —
uv sync
uv run pytest tests/ --ignore=tests/test_live.py

# Live tests (against real relays)
CMPING_LIVE_TEST=nine.testrun.org,tarpit.fun .venv/bin/pytest tests/test_live.py -v
```

## cmping dependency

chatmail-prober depends on a [fork of cmping](cmping-src/) (branch `make-importable`) that adds library-friendly APIs:

- `CMPingError` exception instead of `sys.exit(1)`
- `Pinger.results` list with per-message RTT data
- `accounts_dir` parameter on `perform_ping()` for parallel probing
- Timing data (`account_setup_time`, etc.) on the Pinger object
