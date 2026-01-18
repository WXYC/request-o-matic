#!/usr/bin/env python3
"""
Create a PostHog dashboard for Request-O-Matic telemetry.

Usage:
    python scripts/create_posthog_dashboard.py

Required environment variables:
    POSTHOG_PERSONAL_API_KEY - Your personal API key (from Settings → Personal API Keys)
    POSTHOG_PROJECT_ID - Your project ID (from the URL or /api/projects/)
    POSTHOG_HOST - PostHog host (default: https://us.i.posthog.com)

The script creates a dashboard with insights for:
- Request duration by step (bar chart)
- API call distribution (pie chart)
- Request duration over time (line chart)
- Search type breakdown (pie chart)
- Step success rate (table)
- Slowest requests (table)
"""
import json
import os
import sys
from typing import Any

import httpx

# Configuration
POSTHOG_HOST = os.getenv("POSTHOG_HOST", "https://us.i.posthog.com")
POSTHOG_PERSONAL_API_KEY = os.getenv("POSTHOG_PERSONAL_API_KEY")
POSTHOG_PROJECT_ID = os.getenv("POSTHOG_PROJECT_ID")

DASHBOARD_NAME = "Request-O-Matic Performance"
DASHBOARD_DESCRIPTION = "End-to-end performance metrics for the /request endpoint"


def api_request(
    method: str,
    endpoint: str,
    data: dict | None = None,
) -> dict[str, Any]:
    """Make an authenticated request to the PostHog API."""
    url = f"{POSTHOG_HOST}{endpoint}"
    headers = {
        "Authorization": f"Bearer {POSTHOG_PERSONAL_API_KEY}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=30.0) as client:
        if method == "GET":
            response = client.get(url, headers=headers)
        elif method == "POST":
            response = client.post(url, headers=headers, json=data)
        elif method == "PATCH":
            response = client.patch(url, headers=headers, json=data)
        else:
            raise ValueError(f"Unsupported method: {method}")

        if response.status_code >= 400:
            print(f"API Error ({response.status_code}): {response.text}")
            response.raise_for_status()

        return response.json()


def create_dashboard() -> dict:
    """Create a new dashboard."""
    print(f"Creating dashboard: {DASHBOARD_NAME}")

    data = {
        "name": DASHBOARD_NAME,
        "description": DASHBOARD_DESCRIPTION,
        "pinned": True,
    }

    result = api_request("POST", f"/api/projects/{POSTHOG_PROJECT_ID}/dashboards/", data)
    print(f"  Created dashboard ID: {result['id']}")
    return result


def create_insight(name: str, query: dict, description: str = "") -> dict:
    """Create a new insight."""
    print(f"  Creating insight: {name}")

    data = {
        "name": name,
        "description": description,
        "query": query,
    }

    result = api_request("POST", f"/api/projects/{POSTHOG_PROJECT_ID}/insights/", data)
    return result


def add_insight_to_dashboard(dashboard_id: int, insight_id: int) -> None:
    """Add an insight to a dashboard as a tile."""
    print(f"    Adding insight {insight_id} to dashboard {dashboard_id}")

    # Get current dashboard to find existing tiles
    dashboard = api_request("GET", f"/api/projects/{POSTHOG_PROJECT_ID}/dashboards/{dashboard_id}/")
    tiles = dashboard.get("tiles", [])

    # Calculate position for new tile (simple grid layout)
    row = len(tiles) // 2
    col = len(tiles) % 2

    new_tile = {
        "insight": insight_id,
        "layouts": {
            "sm": {"x": col * 6, "y": row * 5, "w": 6, "h": 5},
            "xs": {"x": 0, "y": row * 5, "w": 1, "h": 5},
        },
    }

    tiles.append(new_tile)

    api_request(
        "PATCH",
        f"/api/projects/{POSTHOG_PROJECT_ID}/dashboards/{dashboard_id}/",
        {"tiles": tiles},
    )


def build_insights() -> list[dict]:
    """Build the insight definitions for our telemetry."""
    return [
        {
            "name": "Average Duration by Step",
            "description": "Mean duration in milliseconds for each request processing step",
            "query": {
                "kind": "TrendsQuery",
                "series": [
                    {
                        "kind": "EventsNode",
                        "event": "request_parse",
                        "math": "avg",
                        "math_property": "duration_ms",
                        "name": "Parse",
                    },
                    {
                        "kind": "EventsNode",
                        "event": "request_album_lookup",
                        "math": "avg",
                        "math_property": "duration_ms",
                        "name": "Album Lookup",
                    },
                    {
                        "kind": "EventsNode",
                        "event": "request_library_search",
                        "math": "avg",
                        "math_property": "duration_ms",
                        "name": "Library Search",
                    },
                    {
                        "kind": "EventsNode",
                        "event": "request_compilation_search",
                        "math": "avg",
                        "math_property": "duration_ms",
                        "name": "Compilation Search",
                    },
                    {
                        "kind": "EventsNode",
                        "event": "request_artwork_fetch",
                        "math": "avg",
                        "math_property": "duration_ms",
                        "name": "Artwork Fetch",
                    },
                    {
                        "kind": "EventsNode",
                        "event": "request_slack_post",
                        "math": "avg",
                        "math_property": "duration_ms",
                        "name": "Slack Post",
                    },
                ],
                "trendsFilter": {"display": "ActionsBar"},
                "dateRange": {"date_from": "-7d"},
            },
        },
        {
            "name": "Total Request Duration Over Time",
            "description": "Average total request duration trend",
            "query": {
                "kind": "TrendsQuery",
                "series": [
                    {
                        "kind": "EventsNode",
                        "event": "request_completed",
                        "math": "avg",
                        "math_property": "total_duration_ms",
                        "name": "Avg Duration (ms)",
                    },
                    {
                        "kind": "EventsNode",
                        "event": "request_completed",
                        "math": "p90",
                        "math_property": "total_duration_ms",
                        "name": "P90 Duration (ms)",
                    },
                ],
                "trendsFilter": {"display": "ActionsLineGraph"},
                "dateRange": {"date_from": "-7d"},
                "interval": "day",
            },
        },
        {
            "name": "API Call Distribution",
            "description": "Breakdown of external API calls by service",
            "query": {
                "kind": "TrendsQuery",
                "series": [
                    {
                        "kind": "EventsNode",
                        "event": "request_completed",
                        "math": "sum",
                        "math_property": "api_calls.groq",
                        "name": "Groq",
                    },
                    {
                        "kind": "EventsNode",
                        "event": "request_completed",
                        "math": "sum",
                        "math_property": "api_calls.discogs",
                        "name": "Discogs",
                    },
                    {
                        "kind": "EventsNode",
                        "event": "request_completed",
                        "math": "sum",
                        "math_property": "api_calls.slack",
                        "name": "Slack",
                    },
                ],
                "trendsFilter": {"display": "ActionsPie"},
                "dateRange": {"date_from": "-7d"},
            },
        },
        {
            "name": "Search Type Breakdown",
            "description": "How requests are resolved (direct, fallback, compilation, etc.)",
            "query": {
                "kind": "TrendsQuery",
                "series": [
                    {
                        "kind": "EventsNode",
                        "event": "request_completed",
                        "math": "total",
                        "name": "Requests",
                    },
                ],
                "breakdownFilter": {
                    "breakdown": "search_type",
                    "breakdown_type": "event",
                },
                "trendsFilter": {"display": "ActionsPie"},
                "dateRange": {"date_from": "-7d"},
            },
        },
        {
            "name": "Request Volume",
            "description": "Number of requests processed over time",
            "query": {
                "kind": "TrendsQuery",
                "series": [
                    {
                        "kind": "EventsNode",
                        "event": "request_completed",
                        "math": "total",
                        "name": "Requests",
                    },
                ],
                "trendsFilter": {"display": "ActionsLineGraph"},
                "dateRange": {"date_from": "-7d"},
                "interval": "day",
            },
        },
        {
            "name": "Results Found Rate",
            "description": "Percentage of requests that found library results",
            "query": {
                "kind": "TrendsQuery",
                "series": [
                    {
                        "kind": "EventsNode",
                        "event": "request_completed",
                        "math": "total",
                        "name": "With Results",
                        "properties": [
                            {
                                "key": "results_count",
                                "value": 0,
                                "operator": "gt",
                                "type": "event",
                            }
                        ],
                    },
                    {
                        "kind": "EventsNode",
                        "event": "request_completed",
                        "math": "total",
                        "name": "No Results",
                        "properties": [
                            {
                                "key": "results_count",
                                "value": 0,
                                "operator": "exact",
                                "type": "event",
                            }
                        ],
                    },
                ],
                "trendsFilter": {"display": "ActionsPie"},
                "dateRange": {"date_from": "-7d"},
            },
        },
    ]


def main():
    """Create the PostHog dashboard with all insights."""
    # Validate configuration
    if not POSTHOG_PERSONAL_API_KEY:
        print("Error: POSTHOG_PERSONAL_API_KEY environment variable is required")
        print("  Get it from PostHog → Settings → Personal API Keys")
        sys.exit(1)

    if not POSTHOG_PROJECT_ID:
        print("Error: POSTHOG_PROJECT_ID environment variable is required")
        print("  Find it in your PostHog URL or via /api/projects/")
        sys.exit(1)

    print(f"PostHog Host: {POSTHOG_HOST}")
    print(f"Project ID: {POSTHOG_PROJECT_ID}")
    print()

    # Create dashboard
    dashboard = create_dashboard()
    dashboard_id = dashboard["id"]

    # Create and add insights
    insights = build_insights()
    print(f"\nCreating {len(insights)} insights...")

    for insight_def in insights:
        insight = create_insight(
            name=insight_def["name"],
            query=insight_def["query"],
            description=insight_def.get("description", ""),
        )
        add_insight_to_dashboard(dashboard_id, insight["id"])

    print()
    print("=" * 60)
    print("Dashboard created successfully!")
    print(f"View it at: {POSTHOG_HOST}/project/{POSTHOG_PROJECT_ID}/dashboard/{dashboard_id}")
    print("=" * 60)


if __name__ == "__main__":
    main()
