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


def _parse_when(value: str, tz):
    """Parse an event time. 'YYYY-MM-DD' -> (date, all_day=True); anything with a
    time -> (tz-aware datetime, all_day=False), assuming local tz if none given."""
    try:
        return date.fromisoformat(value), True
    except ValueError:
        pass
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt, False


@mcp.tool()
def list_calendars() -> dict:
    """List the user's calendars and which ones are writable.

    Use this to pick a `calendar` for create_event, or to answer "which calendars
    can I add to?". Writable ones are typically the local "Personal" calendar and
    any Google calendars you own; subscriptions and birthdays are read-only.
    """

    def run():
        cals = [c.to_dict() for c in gcal.list_calendars()]
        return {"calendars": cals, "writable": [c["name"] for c in cals if c["writable"]]}

    return _safe(run)


@mcp.tool()
def create_event(
    summary: str,
    start: str,
    end: str | None = None,
    location: str | None = None,
    description: str | None = None,
    calendar: str | None = None,
) -> dict:
    """Create a calendar event on a writable GNOME calendar.

    - `summary`: event title.
    - `start`: "YYYY-MM-DDTHH:MM" (local time) for a timed event, or "YYYY-MM-DD"
      for an all-day event.
    - `end`: same formats; optional. Defaults to +1 hour (timed) or +1 day
      (all-day).
    - `location`, `description`: optional.
    - `calendar`: writable calendar display name (see `list_calendars`). Defaults
      to the local "Personal" calendar. Pass a Google calendar's name to add it
      there instead.

    Does NOT add attendees and does NOT send invitations. Confirm the details
    (title, date/time, and which calendar) with the user before calling.
    """

    def run():
        # Must come from gcal._local_tz(): `datetime.now().astimezone().tzinfo`
        # is a fixed offset, which silently shifts events outside the current
        # DST period by an hour.
        tz = gcal._local_tz()
        start_val, all_day = _parse_when(start, tz)
        end_val = _parse_when(end, tz)[0] if end else None
        result = gcal.create_event(
            summary, start_val, end_val, all_day, location, description, calendar
        )
        return {
            "created": True,
            **result,
            "start": start_val.isoformat(),
            "end": end_val.isoformat() if end_val else None,
            "all_day": all_day,
        }

    return _safe(run)


@mcp.tool()
def list_objects(calendar: str | None = None) -> dict:
    """List every event on one calendar, including each event's `uid`.

    `list_events` reads the aggregated Shell agenda, which carries no UIDs, so
    use this when you need a target for `update_event` / `delete_event`. Pass a
    calendar display name, or omit it for the default write calendar.
    """

    def run():
        objs = gcal.list_objects(calendar)
        return {"event_count": len(objs), "events": objs}

    return _safe(run)


@mcp.tool()
def update_event(
    uid: str,
    summary: str | None = None,
    description: str | None = None,
    location: str | None = None,
    calendar: str | None = None,
) -> dict:
    """Edit an existing event's summary, description or location, in place.

    `uid` comes from `list_objects`. Only the fields you pass are changed.
    Times are not editable — delete and re-create for a reschedule. Confirm the
    intended edit with the user before calling.
    """

    def run():
        return gcal.update_event(calendar, uid, summary, description, location)

    return _safe(run)


@mcp.tool()
def delete_event(uid: str, calendar: str | None = None, confirm: bool = False) -> dict:
    """Delete one event, identified by `uid` from `list_objects`.

    Destructive and irreversible. `confirm` must be True, and only after the user
    has explicitly agreed to this specific deletion — never infer consent.
    """

    def run():
        return gcal.delete_event(calendar, uid, confirm)

    return _safe(run)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
