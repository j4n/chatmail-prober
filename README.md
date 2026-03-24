# chatmail-prober

Smokeping-style Prometheus exporter for [chatmail](https://chatmail.at) relay interoperability monitoring.

Periodically probes all pairs of configured chatmail relays using direct
1:1 Delta Chat messages and exposes round-trip time histograms, counters,
and success gauges as Prometheus metrics.

Inspired by:
- https://oss.oetiker.ch/smokeping/
- https://github.com/SuperQ/smokeping_prober

## Quick start

```bash
git clone <repo-url>
cd chatmail-prober

# Create a relay list (see https://chatmail.at/relays for public relays)
cat > relays.txt << EOF
host1.example
host2.example
host3.example
EOF

# Install and run (pick one)
uv run chatmail-prober relays.txt

# pip + venv
make install
.venv/bin/chatmail-prober relays.txt
```

## Output modes

chatmail-prober supports two output modes (or both simultaneously):

```bash
# Textfile for node_exporter's textfile collector
chatmail-prober relays.txt --textfile /var/lib/prometheus/node-exporter/chatmail-prober.prom

# HTTP exporter (for Prometheus to scrape directly)
chatmail-prober relays.txt --port 9740
```
And look at `curl http://localhost:9740/metrics` to see the metrics.

## CLI options

```
--auto-fetch PATH    fetch relay list from https://chatmail.at/relays and write
                     to PATH before starting
RELAYS               Relay list file(s), one domain per line (positional arg)
--scan               Self-probe all relays, print ranked by RTT, then exit
--top N              Number of fastest relays to highlight in --scan output (default: 10)
--port PORT          HTTP listen port (default: off, e.g. --port 9740)
--textfile PATH      Write .prom file for node_exporter textfile collector
--interval SECS      Seconds between probe rounds (default: 900 = 15min)
--count N            Pings per pair per round (default: 5)
--ping-interval S    Seconds between pings within a probe (default: 0.1)
--timeout SECS       Per-pair receive timeout in seconds (default: 60)
--workers N          Max concurrent probe threads (default: 5)
--cache-dir PATH     Base dir for per-worker account directories (default: ~/.cache/chatmail-prober)
--reset              Remove all account dirs, force fresh account creation
--exclude PATH       File of pairs to skip: "src->dst" per line (# comments)
--once               Run one round then exit
-v                   Debug logging (-vv for debug+rpc/deltachat events)
-q / --quiet         Suppress progress output (only show warnings/errors)
```

Progress is shown at INFO level by default (no flags needed). Use `-q` in cron/systemd
where you only want errors.

A SIGUSR1 signal (`kill -USR1`) stops the service after finishing the current probing round.

A SIGUSR2 signal (`kill -USR2`) cycles verbosity: quiet -> normal -> debug -> debug+rpc -> quiet.

## Metrics

| Metric                         | Type    | Description                                         |
|--------------------------------|---------|-----------------------------------------------------|
| `cmping_probe_success`         | Gauge   | 1 if last round had zero loss, 0 otherwise          |
| `cmping_probe_loss_ratio`      | Gauge   | Fraction of pings lost (0.0 = none, 1.0 = all)      |
| `cmping_rtt_median_seconds`    | Gauge   | Median round-trip time                              |
| `cmping_rtt_p90_seconds`       | Gauge   | 90th-percentile round-trip time (slowest)           |
| `cmping_rtt_p10_seconds`       | Gauge   | 10th-percentile round-trip time (fastest)           |
| `cmping_rtt_stddev_seconds`    | Gauge   | Standard deviation of round-trip times              |
| `cmping_send_errors_total`     | Counter | Failed probe rounds (timeout, crash, setup failure) |
| `cmping_account_setup_seconds` | Gauge   | Time spent on account setup                         |
| `cmping_last_round_completion_timestamp` | Gauge | Unix timestamp of last completed round  |
| `cmping_round_duration_seconds` | Gauge   | Wall-clock time of last completed round              |

Per-pair metrics have `source`, `destination`, and `probe_type` labels.
`probe_type` is `"self"` when source equals destination, `"cross"` otherwise.
The last two (round-level) metrics have no labels.

On probe error, RTT gauges are set to NaN so dashboards show a gap
instead of stale values from the previous successful round.

## Operation Overview

For N relays, chatmail-prober tests all N^2 ordered pairs (including
self-loops). Each pair sends a configurable number of direct 1:1 pings
via deltachat-rpc-client, producing individual round-trip time measurements
visualized in two Grafana dashboards.

### Per-worker account directories

Each worker thread gets its own RelayPool with an isolated accounts directory
(`worker-0/`, `worker-1/`, etc.). Within a worker, accounts are reused across
probes: `get_relay_account()` returns an already-online account when one is
available, skipping the setup wait. With W workers and N relays there are up
to W*N account directories and W*N rpc-server processes.

### Pre-flight alive check and --scan

Before starting the matrix, chatmail-prober runs a single self-probe on each relay in
parallel. Dead relays are excluded with a warning rather than failing the whole run.
This means a TLS outage on one relay does not invalidate an entire round.

This can be invoked standalone with `--scan` to print a list ranked by RTT.

### Burst-mode probing

N pings are sent at a short interval (default 0.1s) so all of them are in-flight at
once. The prober then waits up to `--timeout` seconds for replies. Loss means the
message genuinely arrived late or was dropped -- not that we stopped waiting too soon.
This cuts probe wall-time from count*interval (e.g. 10*1.1s = 11s) down to roughly
one round-trip, making large matrix runs practical.

## Deployment

### systemd

Unit files are in `systemd/`:
- `chatmail-prober.service`: The prober,  writes metrics to `/var/tmp/chatmail-prober.prom`
- `chatmail-prober-prom-copy.path`: a path-activated trigger for
- `chatmail-prober-prom-copy.service`: a oneshot service that copies the file into node-exporter's textfile directory on each write.


```bash
# 1. Create home dir and system user (home doubles as uv install root)
sudo mkdir /opt/chatmail-prober
sudo useradd -r -s /usr/sbin/nologin -d /opt/chatmail-prober chatmail-prober
sudo chown chatmail-prober:chatmail-prober /opt/chatmail-prober

# 2. Clone the repo and install dependencies as the service user
sudo -u chatmail-prober git clone \
    https://github.com/j4n/chatmail-prober \
    /opt/chatmail-prober/chatmail-prober
sudo -u chatmail-prober sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
sudo -u chatmail-prober sh -c 'cd /opt/chatmail-prober/chatmail-prober && make install'

# 3. Install and start the systemd units
sudo cp /opt/chatmail-prober/chatmail-prober/systemd/chatmail-prober.service \
       /opt/chatmail-prober/chatmail-prober/systemd/chatmail-prober-prom-copy.path \
       /opt/chatmail-prober/chatmail-prober/systemd/chatmail-prober-prom-copy.service \
       /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chatmail-prober.service
sudo systemctl enable --now chatmail-prober-prom-copy.path
```

```bash
# Graceful restart (waits for the current probe round to finish):
sudo systemctl reload chatmail-prober
```

### Example Prometheus direct scrape config

```yaml
- job_name: cmping
  scrape_interval: 5m
  static_configs:
    - targets: ['localhost:9740']
```

## Grafana dashboards

Two dashboards are included in `grafana/`:

- **dashboard-inter.json** -- Cross-relay overview: pair matrix, probe results table, per-pair RTT panels
- **dashboard-single.json** -- Single relay inspection: self-probe smokeping plot, peer connectivity timelines, per-peer smokeping detail

Import via Grafana UI (Dashboards > Import > Upload JSON) or provision them.

The smokeping panels show a median RTT line with a gray p10-p90 band. The median line color follows RTT thresholds: dark-green < 2s, green < 6s, light-green < 24s, yellow < 48s, red < 60s, dark-red >= 60s.

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
# With uv
uv sync
uv run pytest tests/                                    # all tests (live tests use relays.txt.example)
uv run pytest tests/ --ignore=tests/test_live.py        # unit tests only, no network
uv run pytest tests/test_live.py -v                     # live tests, defaults to relays.txt.example
CMPING_LIVE_TEST=custom.relay uv run pytest tests/test_live.py -v  # override relays

# Or with make (creates .venv automatically)
make install-dev
make test
```

## cmping-src submodule

The `cmping-src/` directory is a git submodule containing the standalone
[cmping](https://github.com/chatmail/cmping) CLI tool. chatmail-prober
no longer depends on it at runtime -- the needed direct-ping logic has been
vendored into `chatmail_prober/prober.py`. The submodule remains in the repo
as cmping is its own project.
