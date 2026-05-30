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

import configparser
import os
import re
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from icalendar import Event as ICalEvent
from jeepney import DBusAddress, MatchRule, message_bus, new_method_call
from jeepney.io.blocking import open_dbus_connection
from jeepney.low_level import HeaderFields, MessageType

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


# --------------------------------------------------------------------------- #
# Writing events (create only) via Evolution Data Server.
#
# The Shell CalendarServer we read from is read-only, so creates go through EDS,
# the backend GNOME Calendar itself uses. GOA/Google calendars are registered in
# the *live registry* (Sources5), not as files on disk — so we enumerate the
# registry, read each source's real UID + keyfile Data, and open writable ones
# through the calendar factory. New events land in EDS and sync to their backend
# (a created event on a Google calendar propagates to Google).
# --------------------------------------------------------------------------- #

_SOURCE_MANAGER = "/org/gnome/evolution/dataserver/SourceManager"
_SOURCE_IFACE = "org.gnome.evolution.dataserver.Source"
_PROPS_IFACE = "org.freedesktop.DBus.Properties"
_CAL_IFACE = "org.gnome.evolution.dataserver.Calendar"
_LOCAL_BACKEND = "local"

_CAL_FACTORY_PATH = "/org/gnome/evolution/dataserver/CalendarFactory"
_CAL_FACTORY_IFACE = "org.gnome.evolution.dataserver.CalendarFactory"

# EDS suffixes its bus names with an ABI version that bumps on incompatible
# changes (Sources5, Calendar8). Rather than hardcode the number — which would
# hard-fail the day a future EDS bumps it — we discover the live name at runtime
# and keep these as fallbacks for the (unexpected) case where enumeration finds
# nothing. The "stem" is the part before the version digits.
_EDS_PREFIX = "org.gnome.evolution.dataserver."
_SOURCES_STEM = "Sources"
_CALENDAR_STEM = "Calendar"
_SOURCES_DEST_FALLBACK = f"{_EDS_PREFIX}{_SOURCES_STEM}5"
_CALENDAR_DEST_FALLBACK = f"{_EDS_PREFIX}{_CALENDAR_STEM}8"

# Resolved bus names are global to the session bus and only change across an EDS
# restart (which means a new process), so caching them for the process lifetime
# is safe and avoids re-enumerating on every call.
_resolved_dest: dict[str, str] = {}


def _discover_dest(conn, stem: str, fallback: str) -> str:
    """Return the highest-versioned live EDS bus name for `stem`.

    Enumerates both activatable and currently-owned names (the factories are
    D-Bus activated, so they show up in ListActivatableNames even when idle) and
    picks the largest `<prefix><stem><N>`. Falls back to `fallback` if nothing
    matches or the bus can't be queried.
    """
    if stem in _resolved_dest:
        return _resolved_dest[stem]
    pattern = re.compile(rf"^{re.escape(_EDS_PREFIX + stem)}(\d+)$")
    best_n, best = -1, None
    for lister in (message_bus.ListActivatableNames(), message_bus.ListNames()):
        try:
            names = conn.send_and_get_reply(lister).body[0]
        except Exception:
            continue
        for name in names:
            m = pattern.match(name)
            if m and int(m.group(1)) > best_n:
                best_n, best = int(m.group(1)), name
    _resolved_dest[stem] = best or fallback
    return _resolved_dest[stem]


def _sources_dest(conn) -> str:
    return _discover_dest(conn, _SOURCES_STEM, _SOURCES_DEST_FALLBACK)


def _factory(conn) -> DBusAddress:
    name = _discover_dest(conn, _CALENDAR_STEM, _CALENDAR_DEST_FALLBACK)
    return DBusAddress(_CAL_FACTORY_PATH, bus_name=name, interface=_CAL_FACTORY_IFACE)


@dataclass
class Calendar:
    uid: str
    name: str
    backend: str
    writable: bool | None  # None = couldn't determine

    def to_dict(self) -> dict:
        return {"name": self.name, "backend": self.backend, "writable": self.writable}


def _call(conn, addr, method, signature=None, body=()):
    """Send a method call and return its body, raising on a D-Bus error reply."""
    msg = new_method_call(addr, method) if signature is None else new_method_call(
        addr, method, signature, body
    )
    reply = conn.send_and_get_reply(msg)
    if reply.header.message_type == MessageType.error:
        name = reply.header.fields.get(HeaderFields.error_name, "D-Bus error")
        detail = reply.body[0] if reply.body else ""
        raise RuntimeError(f"{name}: {detail}")
    return reply.body


def _get_prop(conn, object_path, iface, prop):
    addr = DBusAddress(object_path, bus_name=_sources_dest(conn), interface=_PROPS_IFACE)
    body = _call(conn, addr, "Get", "ss", (iface, prop))
    return body[0][1] if body else None  # jeepney decodes a variant to (sig, value)


def _is_writable(conn, uid: str) -> bool | None:
    try:
        obj, bus_name = _call(conn, _factory(conn), "OpenCalendar", "s", (uid,))
        addr = DBusAddress(obj, bus_name=bus_name, interface=_PROPS_IFACE)
        body = _call(conn, addr, "Get", "ss", (_CAL_IFACE, "Writable"))
        return bool(body[0][1])
    except RuntimeError:
        return None


def list_calendars() -> list[Calendar]:
    """Enumerate calendars from the EDS registry, with writability."""
    cals: list[Calendar] = []
    with open_dbus_connection(bus=_session_bus_address()) as conn:
        intro = DBusAddress(
            _SOURCE_MANAGER, bus_name=_sources_dest(conn),
            interface="org.freedesktop.DBus.Introspectable",
        )
        xml = _call(conn, intro, "Introspect")[0]
        nodes = [n.get("name") for n in ET.fromstring(xml).findall("node") if n.get("name")]
        for node in nodes:
            path = f"{_SOURCE_MANAGER}/{node}"
            try:
                data = _get_prop(conn, path, _SOURCE_IFACE, "Data")
            except RuntimeError:
                continue
            if not data:
                continue
            cp = configparser.ConfigParser()
            try:
                cp.read_string(data)
            except configparser.Error:
                continue
            if not cp.has_section("Calendar"):
                continue
            uid = _get_prop(conn, path, _SOURCE_IFACE, "UID")
            if not uid:
                continue
            cals.append(
                Calendar(
                    uid=uid,
                    name=cp.get("Data Source", "DisplayName", fallback=uid),
                    backend=cp.get("Calendar", "BackendName", fallback="?"),
                    writable=_is_writable(conn, uid),
                )
            )
    return cals


def resolve_calendar(name: str | None = None) -> Calendar:
    """Pick a writable calendar by display name (case-insensitive). With no name,
    default to the local Personal calendar (safest: private, no invitations)."""
    writable = [c for c in list_calendars() if c.writable]
    if not writable:
        raise RuntimeError("No writable calendar is available.")
    if name is None:
        for c in writable:
            if c.backend == _LOCAL_BACKEND:
                return c
        return writable[0]
    for c in writable:
        if c.name.lower() == name.lower():
            return c
    available = ", ".join(repr(c.name) for c in writable)
    raise RuntimeError(f"No writable calendar named {name!r}. Writable: {available}.")


def _build_vevent(summary, start, end, all_day, location, description) -> tuple[str, str]:
    ev = ICalEvent()
    uid = f"{uuid.uuid4()}@gnome-calendar-mcp"
    ev.add("uid", uid)
    ev.add("dtstamp", datetime.now(timezone.utc))
    ev.add("summary", summary)
    if all_day:
        # DTEND is exclusive for DATE values, so default to the day after.
        ev.add("dtstart", start)
        ev.add("dtend", end if end is not None else start + timedelta(days=1))
    else:
        # Store as UTC to avoid needing a VTIMEZONE block; clients show local time.
        ev.add("dtstart", start.astimezone(timezone.utc))
        ev.add("dtend", (end if end is not None else start + timedelta(hours=1)).astimezone(timezone.utc))
    if location:
        ev.add("location", location)
    if description:
        ev.add("description", description)
    return ev.to_ical().decode("utf-8"), uid


def create_event(summary, start, end=None, all_day=False, location=None,
                 description=None, calendar=None) -> dict:
    """Create an event on a writable calendar (default: local Personal).

    `start`/`end` are date objects (all-day) or tz-aware datetimes (timed).
    Returns the created uid and the calendar it landed on. No attendees — this
    never sends invitations.
    """
    cal = resolve_calendar(calendar)
    ics, fallback_uid = _build_vevent(summary, start, end, all_day, location, description)
    with open_dbus_connection(bus=_session_bus_address()) as conn:
        obj, bus_name = _call(conn, _factory(conn), "OpenCalendar", "s", (cal.uid,))
        cal_addr = DBusAddress(obj, bus_name=bus_name, interface=_CAL_IFACE)
        try:
            _call(conn, cal_addr, "Open")  # ensure the backend is open on this connection
        except RuntimeError:
            pass
        created = _call(conn, cal_addr, "CreateObjects", "asu", ([ics], 0))
        uids = created[0] if created else []
    return {"uid": uids[0] if uids else fallback_uid, "calendar": cal.name, "backend": cal.backend}


if __name__ == "__main__":
    # Smoke test: list calendars, then print the next week's agenda.
    print("Calendars:")
    for c in list_calendars():
        flag = "writable" if c.writable else ("read-only" if c.writable is False else "unknown")
        print(f"  [{flag:9}] {c.backend:8} {c.name}")
    now = datetime.now().astimezone()
    print(f"\nNow: {now.isoformat()}")
    evs = agenda(7)
    print(f"{len(evs)} event(s):")
    for e in evs:
        kind = "all-day" if e.all_day else f"{e.start:%a %H:%M}-{e.end:%H:%M}"
        print(f"  [{kind}] {e.summary}")
