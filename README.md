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

# -- or with uv --
uv run chatmail-prober --relays relays.txt -v
```

Then `curl http://localhost:9740/metrics` to see the metrics.

## How it works

For N relays, chatmail-prober tests all N^2 ordered pairs (including self-loops A->A as
baseline health checks). Each pair runs `cmping` with a configurable number of pings,
producing individual round-trip time measurements that feed into Prometheus histograms --
giving you smokeping-style latency distribution data.

### Per-worker account directories

Accounts are organised by worker, not by pair. With W workers and N relays there are
W*N account directories instead of 2*N^2. At 30 relays and 10 workers that is 300
accounts vs 1800. Each worker runs one thread that processes its assigned pairs
sequentially, so its account directories are never accessed concurrently -- this avoids
deltachat-rpc-server database lock contention without needing semaphores.

### Burst-mode probing

N pings are sent at a short interval (default 0.1s) so all of them are in-flight at
once. The prober then waits up to `--timeout` seconds for replies. Loss means the
message genuinely arrived late or was dropped -- not that we stopped waiting too soon.
This cuts probe wall-time from count*interval (e.g. 10*1.1s = 11s) down to roughly
one round-trip, making large matrix runs practical.

### Pre-flight alive check

Before starting the matrix, chatmail-prober runs a single self-probe on each relay in
parallel. Dead relays are excluded with a warning rather than failing the whole run.
This means a TLS outage on one relay does not invalidate an entire round.

### Relay selection with --scan

`--scan` probes each relay against itself, ranks them by average RTT, and exits. Use
this to pick the fastest subset for a matrix run:

```bash
chatmail-prober --relays all-relays.txt --scan --top 10
```

See `relay_speedtest.md` for results across all known public chatmail relays.

## Metrics

| Metric | Type | Description |
|---|---|---|
| `cmping_probe_success` | Gauge | 1 if last round had zero loss, 0 otherwise |
| `cmping_probe_loss_ratio` | Gauge | Fraction of pings lost (0.0 = none, 1.0 = all) |
| `cmping_rtt_median_seconds` | Gauge | Median round-trip time |
| `cmping_rtt_p90_seconds` | Gauge | 90th-percentile round-trip time |
| `cmping_rtt_p10_seconds` | Gauge | 10th-percentile round-trip time |
| `cmping_rtt_stddev_seconds` | Gauge | Standard deviation of round-trip times |
| `cmping_send_errors_total` | Counter | Failed probe rounds (timeout, crash, setup failure) |
| `cmping_account_setup_seconds` | Gauge | Time spent on account setup |

All metrics have `source`, `destination`, and `probe_type` labels.
`probe_type` is `"self"` when source equals destination, `"cross"` otherwise.

## Output modes

chatmail-prober supports two output modes (or both simultaneously):

```bash
# HTTP exporter (Prometheus scrapes this directly)
chatmail-prober --relays relays.txt --port 9740

# Textfile for node_exporter's textfile collector
chatmail-prober --relays relays.txt --textfile /var/lib/prometheus/node-exporter/cmping.prom

# Both
chatmail-prober --relays relays.txt --port 9740 --textfile /var/lib/prometheus/node-exporter/cmping.prom
```

## CLI options

```
--relays PATH        Relay list file, one domain per line (required)
--scan               Self-probe all relays, print ranked by RTT, then exit
--top N              Number of fastest relays to highlight in --scan output (default: 10)
--port PORT          HTTP listen port (default: off, e.g. --port 9740)
--textfile PATH      Write .prom file for node_exporter textfile collector
--interval SECS      Seconds between probe rounds (default: 900 = 15min)
--count N            Pings per pair per round (default: 5)
--ping-interval S    Seconds between pings within a probe (default: 0.1)
--timeout SECS       Per-pair receive timeout in seconds (default: 45)
--workers N          Max concurrent probe threads (default: 5)
--cache-dir PATH     Base dir for per-worker account directories (default: ~/.cache/chatmail-prober)
--once               Run one round then exit
-v                   Show debug messages from chatmail_prober (root logger stays at WARNING)
-q / --quiet         Suppress progress output (only show warnings/errors)
```

Progress is shown at INFO level by default (no flags needed). Use `-q` in cron/systemd
where you only want errors.

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

## Grafana dashboards

Three dashboards are included in `grafana/`:

- **dashboard-intra.json** -- Self-probe overview: per-relay health, state timeline, median/p90/stddev RTT panels
- **dashboard-inter.json** -- Cross-relay overview: pair matrix, probe results table, per-pair RTT panels
- **dashboard-single.json** -- Single relay focus: self-probe smokeping plot, peer connectivity timelines, per-peer smokeping detail

Import via Grafana UI (Dashboards > Import > Upload JSON) or provision them. All dashboards use a `$datasource` variable -- select your Prometheus instance after import.

The smokeping panels show a median RTT line with a gray p10-p90 band. The median line color follows RTT thresholds: green < 1s, blue < 3s, yellow < 5s, orange < 10s, red >= 10s.

### Smokeping panel generator

`grafana/smokeping_panel.py` generates reusable smokeping-style panel JSON:

```python
from grafana.smokeping_panel import smokeping_panel

panel = smokeping_panel(
    title="My RTT",
    metric_filter='source="relay.example", probe_type="self"',
    gridPos={"h": 10, "w": 24, "x": 0, "y": 0},
)
```

## Development

```bash
# With make (creates .venv automatically)
make install-dev
make test

# -- or with uv --
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
- `timeout` parameter on `perform_ping()` for deadline-based receive
- Timing data (`account_setup_time`, etc.) on the Pinger object
