"""Generate grafana/dashboard-turn.json.

Run: `uv run python grafana/build_turn_dashboard.py` to refresh the
dashboard JSON.  Kept as a generator (rather than hand-rolled JSON) so
the smokeping helper stays the single source of truth for panel
styling -- editing the helper updates every smokeping panel at once.
"""

from __future__ import annotations

import json
from pathlib import Path

from smokeping_panel import smokeping_panel_minmax


def availability_panel(gridPos):
    """State-timeline of cmping_relay_turn_status grouped by relay."""
    return {
        "title": "TURN availability",
        "description": (
            "Per-relay TURN endpoint health. "
            "1=ok, 0=down, -2=parse-err, -4=no-binary, -5=timeout."
        ),
        "type": "state-timeline",
        "datasource": {"type": "prometheus", "uid": "${datasource}"},
        "gridPos": gridPos,
        "fieldConfig": {
            "defaults": {
                "custom": {"lineWidth": 0, "fillOpacity": 70},
                "mappings": [
                    {"type": "value", "options": {
                        "1":  {"text": "ok",        "color": "green"},
                        "0":  {"text": "down",      "color": "red"},
                        "-2": {"text": "parse-err", "color": "orange"},
                        "-4": {"text": "no-binary", "color": "purple"},
                        "-5": {"text": "timeout",   "color": "yellow"},
                    }},
                ],
            },
        },
        "options": {
            "mergeValues": True,
            "showValue": "auto",
            "alignValue": "left",
            "rowHeight": 0.9,
            "legend": {"displayMode": "list", "placement": "bottom"},
        },
        "targets": [{
            "refId": "A",
            "expr": "cmping_relay_turn_status",
            "legendFormat": "{{relay}} ({{turn_endpoint}})",
        }],
    }


def endpoint_type_panel(gridPos):
    """Stat: how many relays serve their own TURN vs fall back to turn.delta.chat."""
    return {
        "title": "Endpoint type",
        "description": (
            "Relays publishing their own TURN vs falling back to "
            "turn.delta.chat. A sudden swing to fallback means relays "
            "stopped publishing IMAP METADATA."
        ),
        "type": "stat",
        "datasource": {"type": "prometheus", "uid": "${datasource}"},
        "gridPos": gridPos,
        "fieldConfig": {
            "defaults": {"color": {"mode": "thresholds"}, "unit": "short"},
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "values": False},
            "textMode": "value_and_name",
            "colorMode": "background",
        },
        "targets": [{
            "refId": "A",
            "expr": 'count by (turn_endpoint) (cmping_relay_turn_status == 1)',
            "legendFormat": "{{turn_endpoint}}",
        }],
    }


def by_relay_timeseries(title, metric, unit, gridPos, description=""):
    """Generic per-relay timeseries (one line per relay)."""
    return {
        "title": title,
        "description": description,
        "type": "timeseries",
        "datasource": {"type": "prometheus", "uid": "${datasource}"},
        "gridPos": gridPos,
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {"lineWidth": 1, "fillOpacity": 5, "spanNulls": True},
            },
        },
        "options": {
            "tooltip": {"mode": "multi", "sort": "desc"},
            "legend": {"displayMode": "table", "placement": "right",
                        "calcs": ["lastNotNull"]},
        },
        "targets": [{
            "refId": "A",
            "expr": f"{metric}",
            "legendFormat": "{{relay}} ({{turn_endpoint}})",
        }],
    }


def row(title, gridPos, collapsed=False):
    return {
        "type": "row",
        "title": title,
        "collapsed": collapsed,
        "gridPos": gridPos,
        "panels": [],
    }


def build_dashboard():
    panels = []
    y = 0

    panels.append(row("Overview", {"h": 1, "w": 24, "x": 0, "y": y}))
    y += 1
    panels.append(availability_panel({"h": 8, "w": 18, "x": 0, "y": y}))
    panels.append(endpoint_type_panel({"h": 8, "w": 6, "x": 18, "y": y}))
    y += 8

    # Per-relay smokeping row: repeated by $relay.  We define the template
    # panel once with repeat="relay"; Grafana clones it per variable value.
    panels.append(row("Per-relay TURN smokeping", {"h": 1, "w": 24, "x": 0, "y": y}))
    y += 1
    rtt = smokeping_panel_minmax(
        title="TURN RTT -- $relay",
        metric_root="cmping_relay_turn_rtt_seconds",
        metric_filter='relay="$relay"',
        gridPos={"h": 10, "w": 12, "x": 0, "y": y},
    )
    rtt["repeat"] = "relay"
    rtt["repeatDirection"] = "h"
    panels.append(rtt)
    jitter = smokeping_panel_minmax(
        title="TURN Jitter -- $relay",
        metric_root="cmping_relay_turn_jitter_seconds",
        metric_filter='relay="$relay"',
        gridPos={"h": 10, "w": 12, "x": 12, "y": y},
    )
    panels.append(jitter)
    y += 10

    panels.append(row("Loss / connect timing", {"h": 1, "w": 24, "x": 0, "y": y}))
    y += 1
    panels.append(by_relay_timeseries(
        "Lost packets", "cmping_relay_turn_lost_packets", "short",
        {"h": 8, "w": 8, "x": 0, "y": y},
        description="Packets lost during the loopback test, per relay.",
    ))
    panels.append(by_relay_timeseries(
        "Send dropped", "cmping_relay_turn_send_dropped", "short",
        {"h": 8, "w": 8, "x": 8, "y": y},
        description="Packets dropped on send, per relay.",
    ))
    panels.append(by_relay_timeseries(
        "Connect time", "cmping_relay_turn_connect_seconds", "s",
        {"h": 8, "w": 8, "x": 16, "y": y},
        description="TURN allocate latency, per relay.",
    ))
    y += 8

    panels.append(row("Fallback comparison", {"h": 1, "w": 24, "x": 0, "y": y}))
    y += 1
    panels.append({
        "title": "Fallback avg RTT (per relay)",
        "description": (
            "All relays hitting turn.delta.chat should overlap. "
            "A divergent line means that relay's outbound path is "
            "degraded even though the TURN server itself is fine."
        ),
        "type": "timeseries",
        "datasource": {"type": "prometheus", "uid": "${datasource}"},
        "gridPos": {"h": 8, "w": 24, "x": 0, "y": y},
        "fieldConfig": {
            "defaults": {"unit": "s", "custom": {"lineWidth": 1, "spanNulls": True}},
        },
        "options": {
            "tooltip": {"mode": "multi", "sort": "desc"},
            "legend": {"displayMode": "table", "placement": "right",
                        "calcs": ["lastNotNull"]},
        },
        "targets": [{
            "refId": "A",
            "expr": ('cmping_relay_turn_rtt_seconds'
                     '{turn_endpoint="fallback", quantile="avg"}'),
            "legendFormat": "{{relay}}",
        }],
    })

    return {
        "title": "Chatmail TURN health",
        "uid": "chatmail-turn",
        "tags": ["chatmail", "turn"],
        "schemaVersion": 39,
        "timezone": "",
        "refresh": "1m",
        "time": {"from": "now-6h", "to": "now"},
        "templating": {
            "list": [
                {
                    "name": "datasource",
                    "type": "datasource",
                    "query": "prometheus",
                    "current": {},
                },
                {
                    "name": "relay",
                    "type": "query",
                    "datasource": {"type": "prometheus", "uid": "${datasource}"},
                    "query": "label_values(cmping_relay_turn_status, relay)",
                    "refresh": 2,
                    "includeAll": True,
                    "multi": True,
                    "current": {"text": "All", "value": "$__all"},
                },
            ],
        },
        "panels": panels,
    }


def main():
    dashboard = build_dashboard()
    out = Path(__file__).parent / "dashboard-turn.json"
    out.write_text(json.dumps(dashboard, indent=2) + "\n")
    print(f"wrote {out} ({len(dashboard['panels'])} panels)")


if __name__ == "__main__":
    main()
