"""Reusable smokeping-style panel generator for Grafana dashboards.

Generates a timeseries panel with a gray p10-p90 band and a
threshold-colored median line (dark-green=fast, through dark-red=timeout),
matching the classic smokeping aesthetic.

Usage:
    from smokeping_panel import smokeping_panel

    panel = smokeping_panel(
        title="Self-probe RTT",
        metric_filter='source="$relay", probe_type="self"',
        gridPos={"h": 10, "w": 24, "x": 0, "y": 10},
    )
"""

import json


def smokeping_panel(
    title,
    metric_filter,
    gridPos=None,
    description="Smokeping-style: median RTT line with p10-p90 band.",
):
    """Build a smokeping-style Grafana timeseries panel.

    Args:
        title: Panel title.
        metric_filter: PromQL label filter, e.g. 'source="$relay", probe_type="self"'
        gridPos: Dict with h, w, x, y keys.
        description: Panel description tooltip.

    Returns:
        Dict ready to be inserted into a Grafana dashboard's panels list.
    """
    if gridPos is None:
        gridPos = {"h": 10, "w": 24, "x": 0, "y": 0}

    return {
        "title": title,
        "description": description,
        "type": "timeseries",
        "datasource": {"type": "prometheus", "uid": "${datasource}"},
        "gridPos": gridPos,
        "fieldConfig": {
            "defaults": {
                "unit": "s",
                "custom": {
                    "lineWidth": 1,
                    "fillOpacity": 0,
                    "drawStyle": "line",
                    "spanNulls": True,
                    "scaleDistribution": {"type": "log", "log": 10},
                },
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"color": "dark-green", "value": None},
                        {"color": "green", "value": 2},
                        {"color": "light-green", "value": 6},
                        {"color": "yellow", "value": 24},
                        {"color": "red", "value": 48},
                        {"color": "dark-red", "value": 60},
                    ],
                },
            },
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "p90"},
                    "properties": [
                        {"id": "custom.fillBelowTo", "value": "p10"},
                        {"id": "custom.fillOpacity", "value": 15},
                        {"id": "custom.lineWidth", "value": 0},
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": "#cccccc"}},
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "p10"},
                    "properties": [
                        {"id": "custom.lineWidth", "value": 0},
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": "#cccccc"}},
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "median"},
                    "properties": [
                        {"id": "custom.lineWidth", "value": 2},
                        {"id": "custom.gradientMode", "value": "scheme"},
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": "green"}},
                        {
                            "id": "thresholds",
                            "value": {
                                "mode": "absolute",
                                "steps": [
                                    {"color": "dark-green", "value": None},
                                    {"color": "green", "value": 2},
                                    {"color": "light-green", "value": 6},
                                    {"color": "yellow", "value": 24},
                                    {"color": "red", "value": 48},
                                    {"color": "dark-red", "value": 60},
                                ],
                            },
                        },
                    ],
                },
            ],
        },
        "options": {
            "tooltip": {"mode": "multi", "sort": "desc"},
            "legend": {
                "displayMode": "table",
                "placement": "right",
                "calcs": ["lastNotNull"],
            },
        },
        "targets": [
            {
                "refId": "A",
                "expr": f"cmping_rtt_median_seconds{{{metric_filter}}}",
                "legendFormat": "median",
            },
            {
                "refId": "B",
                "expr": f"cmping_rtt_p90_seconds{{{metric_filter}}}",
                "legendFormat": "p90",
            },
            {
                "refId": "C",
                "expr": f"cmping_rtt_p10_seconds{{{metric_filter}}}",
                "legendFormat": "p10",
            },
        ],
    }


def smokeping_panel_minmax(
    title,
    metric_root,
    metric_filter,
    gridPos=None,
    description="Smokeping-style: avg line with min-max band.",
):
    """Build a min/avg/max smokeping panel for turnutils_uclient-style data.

    Sibling of smokeping_panel() that draws min/avg/max instead of
    p10/median/p90.  Same band fill and threshold-colored center line.

    Args:
        title: Panel title.
        metric_root: Base metric name (e.g. "cmping_relay_turn_rtt_seconds").
        metric_filter: PromQL label filter (without the quantile clause),
            e.g. 'relay="$relay"'.
        gridPos: Dict with h, w, x, y keys.
        description: Panel description tooltip.

    Returns:
        Dict ready to be inserted into a Grafana dashboard's panels list.
    """
    if gridPos is None:
        gridPos = {"h": 10, "w": 24, "x": 0, "y": 0}

    threshold_steps = [
        {"color": "dark-green", "value": None},
        {"color": "green", "value": 2},
        {"color": "light-green", "value": 6},
        {"color": "yellow", "value": 24},
        {"color": "red", "value": 48},
        {"color": "dark-red", "value": 60},
    ]

    return {
        "title": title,
        "description": description,
        "type": "timeseries",
        "datasource": {"type": "prometheus", "uid": "${datasource}"},
        "gridPos": gridPos,
        "fieldConfig": {
            "defaults": {
                "unit": "s",
                "custom": {
                    "lineWidth": 1,
                    "fillOpacity": 0,
                    "drawStyle": "line",
                    "spanNulls": True,
                    "scaleDistribution": {"type": "log", "log": 10},
                },
                "thresholds": {"mode": "absolute", "steps": threshold_steps},
            },
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "max"},
                    "properties": [
                        {"id": "custom.fillBelowTo", "value": "min"},
                        {"id": "custom.fillOpacity", "value": 15},
                        {"id": "custom.lineWidth", "value": 0},
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": "#cccccc"}},
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "min"},
                    "properties": [
                        {"id": "custom.lineWidth", "value": 0},
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": "#cccccc"}},
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "avg"},
                    "properties": [
                        {"id": "custom.lineWidth", "value": 2},
                        {"id": "custom.gradientMode", "value": "scheme"},
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": "green"}},
                        {
                            "id": "thresholds",
                            "value": {"mode": "absolute", "steps": threshold_steps},
                        },
                    ],
                },
            ],
        },
        "options": {
            "tooltip": {"mode": "multi", "sort": "desc"},
            "legend": {
                "displayMode": "table",
                "placement": "right",
                "calcs": ["lastNotNull"],
            },
        },
        "targets": [
            {
                "refId": "A",
                "expr": f'{metric_root}{{{metric_filter}, quantile="max"}}',
                "legendFormat": "max",
            },
            {
                "refId": "B",
                "expr": f'{metric_root}{{{metric_filter}, quantile="avg"}}',
                "legendFormat": "avg",
            },
            {
                "refId": "C",
                "expr": f'{metric_root}{{{metric_filter}, quantile="min"}}',
                "legendFormat": "min",
            },
        ],
    }


if __name__ == "__main__":
    # Demo: print a sample panel as JSON
    panel = smokeping_panel(
        title="Self-probe RTT",
        metric_filter='source="$relay", probe_type="self"',
    )
    print(json.dumps(panel, indent=2))
