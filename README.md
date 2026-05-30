# gnome-calendar-mcp

A small [MCP](https://modelcontextprotocol.io) server that exposes your **GNOME
calendar** to Claude Code — so Claude can see what's on your schedule, help you
plan, and add events for you.

It reads the same aggregated agenda the GNOME top-bar clock shows, via the
`org.gnome.Shell.CalendarServer` D-Bus service. That means **every calendar you've
added through GNOME Online Accounts** (Google, Exchange, etc.) is included
automatically, with no per-account auth and no extra CLI — if it shows up in
GNOME Calendar, it shows up here.

## Tools

- **`get_agenda(days=7)`** — today's date, timezone, and all events from the start
  of today through `days` days ahead. The natural starting point for planning.
- **`list_events(start, end)`** — events between two ISO dates (`YYYY-MM-DD`),
  inclusive. Pass the same date twice for a single day.
- **`list_calendars()`** — all calendars and which are writable (a create target).
- **`create_event(summary, start, end?, location?, description?, calendar?)`** —
  add an event. `start`/`end` are `YYYY-MM-DDTHH:MM` (local) for timed events or
  `YYYY-MM-DD` for all-day. Defaults to the local **Personal** calendar; pass a
  calendar name to target another.

Each event read back has: `summary`, `start`/`end` (ISO local), `all_day`, `day`.

### Creating events

Reads come from the GNOME Shell aggregator (read-only), so creates go through
**Evolution Data Server** — the backend GNOME Calendar itself uses. Typically the
local **Personal** calendar and any **Google** calendars you own are writable;
subscriptions (webcal) and birthdays are read-only. An event created on a Google
calendar is pushed up to Google by EDS server-side.

`create_event` does **not** add attendees and does **not** send invitations — it
creates the event on your calendar only. (Invitations would need a deliberate
follow-up: either attendees on a groupware calendar, or emailing an iCalendar
invite.)

## How it works

The GNOME Shell calendar service is push-based: you call
`SetTimeRange(since, until, force_reload)` and it emits `EventsAddedOrUpdated`
signals with the events. The server drives that range and drains the signals for
a short window. Notable details (see `gcal.py`):

- **`force_reload=True` is required.** A fresh client gets no emission for a range
  the service already has loaded — it only reports what's *new* to it.
- **The D-Bus match rule omits `sender`.** The signal arrives from the service's
  unique bus name (`:1.NN`); a local filter keyed on the well-known name never
  matches.
- **The session bus address is resolved internally** from `XDG_RUNTIME_DIR`
  (falling back to `/run/user/<uid>/bus`), because MCP clients launch servers
  with a whitelisted environment that usually drops `DBUS_SESSION_BUS_ADDRESS`.

Dependencies are pure-Python (`mcp` + `jeepney`) so the whole thing installs and
runs under `uv` with no system bindings.

## Requirements

- A GNOME session with the Shell calendar service running (i.e. you're logged
  into GNOME). The server talks to your session bus.
- [`uv`](https://docs.astral.sh/uv/).

## Use with Claude Code

Registered as a user-scope MCP server:

```sh
claude mcp add gnome-calendar --scope user -- \
  uv run --project /path/to/gnome-calendar-mcp gnome-calendar-mcp
```

Then any Claude Code session can use the tools. The companion `/plan` command
(`~/.claude/commands/plan.md`) calls `get_agenda` and walks you through planning
your day.

## Develop / test

```sh
# Print the next week's agenda straight from the backend:
uv run python src/gnome_calendar_mcp/gcal.py

# Run the stdio server directly:
uv run gnome-calendar-mcp
```
