"""GNOME calendar access via the GNOME Shell calendar D-Bus service.

`org.gnome.Shell.CalendarServer` is the service the GNOME top-bar clock uses to
show events. It aggregates every calendar configured in GNOME / Online Accounts,
so reading from it gives the same unified agenda the desktop shows — with no
per-account auth and no extra CLI.

The service is push-based: call `SetTimeRange(since, until, force_reload)` and it
emits `EventsAddedOrUpdated((a(ssxxa{sv})))` signals carrying
`(id, summary, start_unix, end_unix, extras)`. We add a match rule, drive the
range, and drain the matching signals for a short window (the backend serves from
cache, so they arrive within a few hundred ms).

Pure-Python (jeepney) — no PyGObject / system bindings required.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from jeepney import DBusAddress, MatchRule, message_bus, new_method_call
from jeepney.io.blocking import open_dbus_connection

_ADDR = DBusAddress(
    "/org/gnome/Shell/CalendarServer",
    bus_name="org.gnome.Shell.CalendarServer",
    interface="org.gnome.Shell.CalendarServer",
)

# Max wait for the *first* batch of events after driving the range. A forced
# reload has to round-trip the calendar backends, so allow a couple seconds.
_FIRST_WAIT = 2.5
# Once events start arriving, stop after this long with no further signal —
# multiple calendars emit separately, so we wait a beat for stragglers.
_SETTLE = 0.4


@dataclass
class Event:
    summary: str
    start: datetime  # local, tz-aware
    end: datetime  # local, tz-aware
    all_day: bool

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "all_day": self.all_day,
            "day": self.start.date().isoformat(),
        }


def _session_bus_address() -> str:
    """Resolve the session bus address without relying on the caller's env.

    MCP clients launch servers with a whitelisted environment that often omits
    `DBUS_SESSION_BUS_ADDRESS`, so fall back to the well-known per-user socket at
    `$XDG_RUNTIME_DIR/bus` (a.k.a. /run/user/<uid>/bus). jeepney accepts a full
    `unix:path=...` address in place of the "SESSION" keyword.
    """
    addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if addr:
        return addr
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return f"unix:path={runtime_dir}/bus"


def _local_tz():
    return datetime.now().astimezone().tzinfo


def _is_all_day(start_unix: int, end_unix: int, start_dt: datetime) -> bool:
    return (
        start_dt.hour == 0
        and start_dt.minute == 0
        and start_dt.second == 0
        and end_unix > start_unix
        and (end_unix - start_unix) % 86_400 == 0
    )


def fetch_events(since_unix: int, until_unix: int) -> list[Event]:
    """Return events overlapping [since_unix, until_unix), sorted by start.

    Raises on D-Bus failure (service missing, no session bus, etc.); callers
    should decide how to surface that.
    """
    tz = _local_tz()
    by_id: dict[str, Event] = {}

    # Note: no `sender=` here. The bus delivers the signal from the service's
    # unique name (":1.NN"), and jeepney's local filter matches the message's
    # literal sender header — a well-known name would never match. interface +
    # member + path is specific enough.
    rule = MatchRule(
        type="signal",
        interface="org.gnome.Shell.CalendarServer",
        member="EventsAddedOrUpdated",
        path="/org/gnome/Shell/CalendarServer",
    )

    with open_dbus_connection(bus=_session_bus_address()) as conn:
        # Ask the bus to route matching signals to us, then start buffering them
        # locally before we trigger the range change (avoids a race where the
        # reply arrives before the filter is in place).
        conn.send_and_get_reply(message_bus.AddMatch(rule))
        with conn.filter(rule) as queue:
            # Fire-and-forget the range change (don't block on its method return,
            # or the receive loop would miss the signals it triggers). force_reload
            # must be True: a fresh client gets no emission for an already-loaded
            # range otherwise — the server only reports what's *new* to it.
            conn.send(new_method_call(_ADDR, "SetTimeRange", "xxb", (since_unix, until_unix, True)))

            # Wait up to _FIRST_WAIT for the first batch; once events flow, stop
            # after a _SETTLE gap (or the hard cap) so we don't burn the full wait.
            hard_deadline = time.monotonic() + _FIRST_WAIT
            got_any = False
            while True:
                now = time.monotonic()
                timeout = _SETTLE if got_any else hard_deadline - now
                timeout = min(timeout, hard_deadline - now)
                if timeout <= 0:
                    break
                try:
                    msg = conn.recv_until_filtered(queue, timeout=timeout)
                except TimeoutError:
                    break
                got_any = True
                for ev in msg.body[0]:
                    ev_id, summary, start_unix, end_unix = ev[0], ev[1], ev[2], ev[3]
                    if not ev_id or not start_unix:
                        continue
                    start_dt = datetime.fromtimestamp(start_unix, tz)
                    end_dt = datetime.fromtimestamp(end_unix, tz)
                    by_id[ev_id] = Event(
                        summary=summary or "(untitled)",
                        start=start_dt,
                        end=end_dt,
                        all_day=_is_all_day(start_unix, end_unix, start_dt),
                    )

    events = sorted(by_id.values(), key=lambda e: (e.start, e.end))
    return events


def agenda(days: int = 7) -> list[Event]:
    """Events from the start of today through `days` days ahead."""
    tz = _local_tz()
    start_of_today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    since = int(start_of_today.timestamp())
    until = int((start_of_today + timedelta(days=days + 1)).timestamp())
    return fetch_events(since, until)


def events_between(start: date, end: date) -> list[Event]:
    """Events from the start of `start` through the end of `end` (inclusive)."""
    tz = _local_tz()
    since_dt = datetime(start.year, start.month, start.day, tzinfo=tz)
    end_dt = datetime(end.year, end.month, end.day, tzinfo=tz) + timedelta(days=1)
    return fetch_events(int(since_dt.timestamp()), int(end_dt.timestamp()))


if __name__ == "__main__":
    # Smoke test: print the next week's agenda as the backend sees it.
    now = datetime.now().astimezone()
    print(f"Now: {now.isoformat()}")
    evs = agenda(7)
    print(f"{len(evs)} event(s):")
    for e in evs:
        kind = "all-day" if e.all_day else f"{e.start:%a %H:%M}-{e.end:%H:%M}"
        print(f"  [{kind}] {e.summary}")
