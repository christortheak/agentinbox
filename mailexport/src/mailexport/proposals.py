"""Click-to-add event proposals (`propose-event` request).

The agent never writes to the calendar. It asks the worker to build a proposal;
the user clicks it and chooses whether (and where) to add it.

Safety properties of every generated .ics:
  - METHOD:PUBLISH and no ATTENDEE/ORGANIZER lines: opening it can never send
    an invitation or an RSVP on the user's behalf.
  - Times in UTC (or VALUE=DATE for all-day): no VTIMEZONE ambiguity.
  - Proper escaping, CRLF line endings, 75-octet folding (RFC 5545).
  - Provenance in DESCRIPTION: which email it came from, and that AgentInbox
    only proposed it.
Google/Outlook links carry the same details in the URL, so they reach that
provider when clicked; they're opt-in via the calendar_link_format preference.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .meetings import load_calendar_events, overlapping
from .output import atomic_write

MAX_TITLE = 200
MAX_LOCATION = 300
MAX_NOTES = 2000
MAX_SPAN = timedelta(days=14)
_TIMED = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID_RE = re.compile(r"^[0-9a-f]{16}$")


class ProposalError(Exception):
    pass


# ---- RFC 5545 helpers ----

def ics_escape(s: str) -> str:
    return (s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n"))


def fold(line: str) -> str:
    """Fold to <=75 octets per physical line without splitting UTF-8 chars."""
    out, cur, cur_len, limit = [], "", 0, 75
    for ch in line:
        n = len(ch.encode("utf-8"))
        if cur_len + n > limit:
            out.append(cur)
            cur, cur_len, limit = " " + ch, 1 + n, 75
        else:
            cur += ch
            cur_len += n
    out.append(cur)
    return "\r\n".join(out)


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(*, uid: str, title: str, start, end, all_day: bool,
              location: str | None, description: str) -> bytes:
    now = datetime.now(timezone.utc)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//AgentInbox//Proposed event//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_utc(now)}",
    ]
    if all_day:
        lines += [f"DTSTART;VALUE=DATE:{start:%Y%m%d}",
                  f"DTEND;VALUE=DATE:{end:%Y%m%d}"]
    else:
        lines += [f"DTSTART:{_utc(start)}", f"DTEND:{_utc(end)}"]
    lines.append(f"SUMMARY:{ics_escape(title)}")
    if location:
        lines.append(f"LOCATION:{ics_escape(location)}")
    lines += [f"DESCRIPTION:{ics_escape(description)}",
              "STATUS:CONFIRMED", "TRANSP:OPAQUE",
              "END:VEVENT", "END:VCALENDAR"]
    return ("\r\n".join(fold(l) for l in lines) + "\r\n").encode("utf-8")


# ---- URL helpers (no urllib import: keeps the "no network code" grep clean) ----

_SAFE = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~")


def _q(s: str) -> str:
    return "".join(chr(b) if b in _SAFE else f"%{b:02X}" for b in s.encode("utf-8"))


def _query(pairs: list[tuple[str, str]]) -> str:
    return "&".join(f"{_q(k)}={_q(v)}" for k, v in pairs)


def google_url(title, start, end, all_day, location, details) -> str:
    if all_day:
        dates = f"{start:%Y%m%d}/{end:%Y%m%d}"
    else:
        dates = f"{_utc(start)}/{_utc(end)}"
    pairs = [("action", "TEMPLATE"), ("text", title), ("dates", dates),
             ("details", details[:1000])]
    if location:
        pairs.append(("location", location))
    return "https://calendar.google.com/calendar/render?" + _query(pairs)


def outlook_url(title, start, end, all_day, location, details) -> str:
    if all_day:
        s, e = start.isoformat(), end.isoformat()
    else:
        s = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        e = end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pairs = [("path", "/calendar/action/compose"), ("rru", "addevent"),
             ("subject", title), ("startdt", s), ("enddt", e),
             ("allday", "true" if all_day else "false"), ("body", details[:1000])]
    if location:
        pairs.append(("location", location))
    return "https://outlook.office.com/calendar/0/deeplink/compose?" + _query(pairs)


# ---- request handling ----

def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "event"


def propose_event(cfg, req: dict) -> dict:
    from . import safefs
    safefs.register_root(cfg.output_dir)
    if not cfg.proposals.enabled:
        raise ProposalError("event proposals are disabled in config")
    title = str(req.get("title") or "").strip()
    if not title or len(title) > MAX_TITLE:
        raise ProposalError(f"title required (1-{MAX_TITLE} chars)")
    location = req.get("location")
    if location is not None:
        location = str(location).strip()[:MAX_LOCATION] or None
    notes = str(req.get("notes") or "")
    if len(notes) > MAX_NOTES:
        raise ProposalError(f"notes too long (max {MAX_NOTES} chars)")

    tzname = str(req.get("timezone") or cfg.timezone)
    try:
        tz = ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError):
        raise ProposalError(f"unknown timezone {tzname!r}") from None

    start_s = str(req.get("start") or "")
    end_s = req.get("end")
    if _TIMED.match(start_s):
        all_day = False
        start = datetime.strptime(start_s, "%Y-%m-%dT%H:%M").replace(tzinfo=tz)
        if end_s is None:
            end = start + timedelta(hours=1)
        elif _TIMED.match(str(end_s)):
            end = datetime.strptime(str(end_s), "%Y-%m-%dT%H:%M").replace(tzinfo=tz)
        else:
            raise ProposalError("end must use the same format as start (yyyy-MM-ddTHH:mm)")
        if end <= start:
            raise ProposalError("end must be after start")
        first_day = start.date()
    elif _DATE.match(start_s):
        all_day = True
        start = date.fromisoformat(start_s)
        if end_s is None:
            last = start
        elif _DATE.match(str(end_s)):
            last = date.fromisoformat(str(end_s))   # inclusive last day
        else:
            raise ProposalError("end must use the same format as start (yyyy-MM-dd)")
        if last < start:
            raise ProposalError("end must not be before start")
        end = last + timedelta(days=1)              # iCalendar end is exclusive
        first_day = start
    else:
        raise ProposalError("start must be yyyy-MM-ddTHH:mm (timed) or yyyy-MM-dd (all day)")

    span = (end - start) if not all_day else timedelta(days=(end - start).days)
    if span > MAX_SPAN:
        raise ProposalError("events longer than 14 days are not supported")
    today = datetime.now(ZoneInfo(cfg.timezone)).date()
    if not (today - timedelta(days=1) <= first_day <= today + timedelta(days=366)):
        raise ProposalError("start must be between yesterday and a year from now")

    # Provenance: the email this came from, looked up in trusted state.
    source = None
    src_id = req.get("source_message_id")
    if src_id is not None:
        if not _ID_RE.match(str(src_id)):
            raise ProposalError("source_message_id must be an exported message id")
        from .runner import load_state
        entry = load_state(cfg)["messages"].get(str(src_id))
        if entry is None:
            raise ProposalError("source_message_id not found in the current export")
        r = entry["record"]
        source = {"message_id": r["id"], "from": (r.get("from") or [None])[0],
                  "subject": r.get("subject"), "date": r.get("date_local")}

    desc_parts = [notes.strip()] if notes.strip() else []
    if source:
        desc_parts.append(f"From email: {source['from']} / \"{source['subject']}\" "
                          f"({source['date']})")
    desc_parts.append("Proposed by AgentInbox from your mail. It was not added to any "
                      "calendar until you chose to add it.")
    description = "\n\n".join(desc_parts)

    basis = f"{title}|{start}|{end}|{src_id or ''}"
    h = hashlib.sha256(basis.encode()).hexdigest()[:16]
    uid = f"agentinbox-{h}@agentinbox.local"

    result: dict = {"ok": True, "action": "propose-event", "title": title,
                    "all_day": all_day, "formats": list(cfg.proposals.formats)}
    if all_day:
        result["start"], result["end"] = start.isoformat(), (end - timedelta(days=1)).isoformat()
    else:
        result["start"] = start.isoformat(timespec="minutes")
        result["end"] = end.isoformat(timespec="minutes")
    if source:
        result["source"] = source

    if "ics" in cfg.proposals.formats:
        name = f"{first_day.isoformat()}-{_slug(title)}-{h[:8]}.ics"
        path = cfg.output_dir / "proposals" / name
        atomic_write(path, build_ics(uid=uid, title=title, start=start, end=end,
                                     all_day=all_day, location=location,
                                     description=description), mode=0o644)
        result["ics_path"] = str(path)
    if "google" in cfg.proposals.formats:
        result["google_url"] = google_url(title, start, end, all_day, location, description)
    if "outlook" in cfg.proposals.formats:
        result["outlook_url"] = outlook_url(title, start, end, all_day, location, description)

    # Help the agent avoid duplicates: what's already on the calendar then.
    if all_day:
        z = ZoneInfo(cfg.timezone)
        s_dt = datetime.combine(start, datetime.min.time(), tzinfo=z)
        e_dt = datetime.combine(end, datetime.min.time(), tzinfo=z)
    else:
        s_dt, e_dt = start, end
    result["conflicts"] = [
        {"title": ev.get("title"), "start": ev.get("start"), "end": ev.get("end"),
         "calendar": ev.get("calendar")}
        for ev in overlapping(load_calendar_events(cfg), s_dt, e_dt)
    ][:10]
    return result
