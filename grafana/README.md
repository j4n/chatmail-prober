# Grafana dashboards

This directory contains the Grafana dashboard JSON files and a helper script
for generating Smokeping-style panels.

| File | Purpose |
|---|---|
| `dashboard-inter.json` | Cross-relay RTT matrix — pair heatmap, probe results table, per-pair Smokeping panels |
| `dashboard-single.json` | Single relay deep-dive — self-probe Smokeping plot, peer connectivity timelines |
| `smokeping_panel.py` | Generator for reusable Smokeping-style panel JSON |

Import dashboards via **Grafana UI → Dashboards → Import → Upload JSON file**,
or provision them via the standard Grafana provisioning directory.

## ESnet Matrix Panel

The cross-relay matrix view uses the
[ESnet Matrix Panel](https://github.com/j4n/esnet-matrix-panel/)
Grafana plugin for the N×N relay heatmap.

Install it from the link above before importing `dashboard-inter.json`.
