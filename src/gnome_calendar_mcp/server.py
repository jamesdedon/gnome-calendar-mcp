"""MCP server exposing the user's GNOME calendar as read-only tools.

Backed by :mod:`gnome_calendar_mcp.gcal`, which reads the same aggregated agenda
the GNOME top-bar clock shows (all Online Accounts calendars included). Runs over
stdio; intended to be launched by Claude Code as an MCP server.
"""

from __future__ import annotations

from datetime import date, datetime

from mcp.server.fastmcp import FastMCP

from . import gcal

mcp = FastMCP("gnome-calendar")


def _safe(fn):
    """Run a backend call, converting D-Bus/parse failures into a structured
    error payload so the model can degrade gracefully instead of seeing a
    traceback."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - surface any backend failure to the model
        return {
            "error": f"Could not read the GNOME calendar service: {e}",
            "events": [],
            "event_count": 0,
        }


@mcp.tool()
def get_agenda(days: int = 7) -> dict:
    """Get the user's calendar agenda from today through `days` days ahead.

    Reads every calendar GNOME aggregates (Google/Exchange/etc. via Online
    Accounts, plus local calendars). Returns today's date and timezone alongside
    the events, so you also know "what day it is" for planning. Use this as the
    starting point when helping the user plan their day or week.

    Each event has: summary, start (ISO local), end (ISO local), all_day, day.
    """

    def run():
        now = datetime.now().astimezone()
        events = gcal.agenda(days)
        return {
            "today": now.date().isoformat(),
            "now": now.isoformat(timespec="minutes"),
            "weekday": now.strftime("%A"),
            "timezone": str(now.tzinfo),
            "days_ahead": days,
            "event_count": len(events),
            "events": [e.to_dict() for e in events],
        }

    return _safe(run)


@mcp.tool()
def list_events(start: str, end: str) -> dict:
    """Get calendar events between two dates, inclusive.

    `start` and `end` are ISO dates ("YYYY-MM-DD"). Useful for follow-up
    questions like "what's on Thursday" or "show me next week" — pass the same
    date for both to get a single day.
    """

    def run():
        s = date.fromisoformat(start)
        e = date.fromisoformat(end)
        events = gcal.events_between(s, e)
        return {
            "start": s.isoformat(),
            "end": e.isoformat(),
            "event_count": len(events),
            "events": [ev.to_dict() for ev in events],
        }

    return _safe(run)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
