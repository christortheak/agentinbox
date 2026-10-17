"""Extract calendar invites (text/calendar parts, .ics attachments) from mail.

Stdlib-only iCalendar reading: just enough of RFC 5545 for meeting invites -
line unfolding, property parameters, DTSTART/DTEND/DURATION in UTC, floating,
TZID (IANA or Windows names, as Exchange sends), and all-day dates.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_ICS_BYTES = 200_000
MAX_EVENTS_PER_MESSAGE = 20

# Exchange/Outlook send Windows zone names. Common ones -> IANA.
WINDOWS_TZ = {
    "eastern standard time": "America/New_York",
    "us eastern standard time": "America/Indiana/Indianapolis",
    "central standard time": "America/Chicago",
    "mountain standard time": "America/Denver",
    "us mountain standard time": "America/Phoenix",
    "pacific standard time": "America/Los_Angeles",
    "alaskan standard time": "America/Anchorage",
    "hawaiian standard time": "Pacific/Honolulu",
    "atlantic standard time": "America/Halifax",
    "newfoundland standard time": "America/St_Johns",
    "utc": "UTC",
    "coordinated universal time": "UTC",
    "gmt standard time": "Europe/London",
    "greenwich standard time": "Atlantic/Reykjavik",
    "w. europe standard time": "Europe/Berlin",
    "central europe standard time": "Europe/Budapest",
    "central european standard time": "Europe/Warsaw",
    "romance standard time": "Europe/Paris",
    "e. europe standard time": "Europe/Chisinau",
    "fle standard time": "Europe/Kiev",
    "gtb standard time": "Europe/Bucharest",
    "russian standard time": "Europe/Moscow",
    "turkey standard time": "Europe/Istanbul",
    "israel standard time": "Asia/Jerusalem",
    "georgian standard time": "Asia/Tbilisi",
    "caucasus standard time": "Asia/Yerevan",
    "azerbaijan standard time": "Asia/Baku",
    "arabian standard time": "Asia/Dubai",
    "india standard time": "Asia/Kolkata",
    "china standard time": "Asia/Shanghai",
    "singapore standard time": "Asia/Singapore",
    "tokyo standard time": "Asia/Tokyo",
    "korea standard time": "Asia/Seoul",
    "aus eastern standard time": "Australia/Sydney",
    "new zealand standard time": "Pacific/Auckland",
    "sa pacific standard time": "America/Bogota",
    "e. south america standard time": "America/Sao_Paulo",
    "south africa standard time": "Africa/Johannesburg",
}

# Fragments seen in Google/Outlook display-style TZIDs like
# "(UTC-05:00) Eastern Time (US & Canada)".
_TZ_HINTS = [
    ("eastern", "America/New_York"), ("central time", "America/Chicago"),
    ("mountain", "America/Denver"), ("arizona", "America/Phoenix"),
    ("pacific", "America/Los_Angeles"), ("london", "Europe/London"),
    ("tbilisi", "Asia/Tbilisi"), ("paris", "Europe/Paris"),
    ("berlin", "Europe/Berlin"), ("amsterdam", "Europe/Amsterdam"),
]


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _parse_line(line: str) -> tuple[str, dict[str, str], str] | None:
    """NAME;P1=V1;P2="V:2":VALUE -> (NAME, {P1: V1, P2: V:2}, VALUE)."""
    in_quote = False
    colon = -1
    for i, ch in enumerate(line):
        if ch == '"':
            in_quote = not in_quote
        elif ch == ":" and not in_quote:
            colon = i
            break
    if colon < 0:
        return None
    head, value = line[:colon], line[colon + 1:]
    parts: list[str] = []
    buf, in_quote = "", False
    for ch in head:
        if ch == '"':
            in_quote = not in_quote
            continue
        if ch == ";" and not in_quote:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    name = parts[0].strip().upper()
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.strip().upper()] = v.strip()
    return name, params, value


def _unescape(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out).strip()


def resolve_tz(tzid: str | None, default_tz: str) -> tuple[ZoneInfo, bool]:
    """Returns (zone, assumed). assumed=True when we fell back to default."""
    if tzid:
        t = tzid.strip().strip('"')
        try:
            return ZoneInfo(t), False
        except (ZoneInfoNotFoundError, ValueError):
            pass
        win = WINDOWS_TZ.get(t.lower())
        if win:
            return ZoneInfo(win), False
        low = t.lower()
        for frag, zone in _TZ_HINTS:
            if frag in low:
                return ZoneInfo(zone), False
    return ZoneInfo(default_tz), bool(tzid)


def _parse_dt(value: str, params: dict, default_tz: str):
    """Returns (datetime-aware | date, all_day, tz_assumed)."""
    v = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", v):
        return datetime.strptime(v[:8], "%Y%m%d").date(), True, False
    m = re.fullmatch(r"(\d{8}T\d{6})(Z?)", v)
    if not m:
        raise ValueError(f"unparseable date-time {v!r}")
    naive = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")
    if m.group(2) == "Z":
        return naive.replace(tzinfo=timezone.utc), False, False
    tz, assumed = resolve_tz(params.get("TZID"), default_tz)
    if "TZID" not in params:
        assumed = True  # floating time: interpret in the user's zone
    return naive.replace(tzinfo=tz), False, assumed


_DUR_RE = re.compile(
    r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def _parse_duration(v: str) -> timedelta | None:
    m = _DUR_RE.match(v.strip().upper())
    if not m:
        return None
    sign, w, d, h, mi, s = m.groups()
    td = timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0),
                   minutes=int(mi or 0), seconds=int(s or 0))
    return -td if sign == "-" else td


def _person(value: str, params: dict) -> str | None:
    addr = value.strip()
    if addr.lower().startswith("mailto:"):
        addr = addr[7:]
    cn = params.get("CN", "").strip()
    if not addr and not cn:
        return None
    return f"{cn} <{addr}>" if cn and addr else (addr or cn)


def parse_ics(text: str, default_tz: str) -> list[dict]:
    """Parse VEVENTs from one iCalendar object. Never raises on bad input;
    malformed events are skipped."""
    method = None
    events: list[dict] = []
    stack: list[str] = []
    cur: dict | None = None
    for line in _unfold(text[:MAX_ICS_BYTES]):
        parsed = _parse_line(line)
        if not parsed:
            continue
        name, params, value = parsed
        if name == "BEGIN":
            stack.append(value.strip().upper())
            if stack[-1] == "VEVENT" and len(events) < MAX_EVENTS_PER_MESSAGE:
                cur = {"_props": []}
            continue
        if name == "END":
            comp = value.strip().upper()
            if stack and stack[-1] == comp:
                stack.pop()
            if comp == "VEVENT" and cur is not None:
                ev = _finish_event(cur, default_tz)
                if ev:
                    events.append(ev)
                cur = None
            continue
        if stack == ["VCALENDAR"] and name == "METHOD":
            method = value.strip().upper()
        elif cur is not None and stack and stack[-1] == "VEVENT":
            cur["_props"].append((name, params, value))
    for ev in events:
        ev["method"] = method
    return events


def _finish_event(cur: dict, default_tz: str) -> dict | None:
    props: dict[str, tuple[dict, str]] = {}
    attendees = []
    for name, params, value in cur["_props"]:
        if name == "ATTENDEE":
            attendees.append((params, value))
        elif name not in props:
            props[name] = (params, value)
    if "DTSTART" not in props:
        return None
    try:
        start, all_day, assumed = _parse_dt(props["DTSTART"][1], props["DTSTART"][0], default_tz)
        if "DTEND" in props:
            end, _, a2 = _parse_dt(props["DTEND"][1], props["DTEND"][0], default_tz)
            assumed = assumed or a2
        elif "DURATION" in props and _parse_duration(props["DURATION"][1]) is not None:
            end = start + _parse_duration(props["DURATION"][1])
        else:
            end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
    except (ValueError, OverflowError):
        return None

    rrule = props.get("RRULE", ({}, ""))[1]
    series_until = None
    m = re.search(r"UNTIL=(\d{8}(?:T\d{6}Z?)?)", rrule.upper())
    if m:
        try:
            u = m.group(1)
            series_until = (datetime.strptime(u[:8], "%Y%m%d").date().isoformat())
        except ValueError:
            series_until = None

    partstat = None
    if attendees:
        partstat = attendees[0][0].get("PARTSTAT", "").upper() or None

    def text(key):
        return _unescape(props[key][1]) if key in props else None

    seq = props.get("SEQUENCE", ({}, "0"))[1].strip()
    return {
        "uid": (props.get("UID", ({}, ""))[1].strip() or None),
        "summary": text("SUMMARY") or "",
        "start": start.isoformat() if all_day else start.isoformat(timespec="seconds"),
        "end": end.isoformat() if all_day else end.isoformat(timespec="seconds"),
        "all_day": all_day,
        "location": text("LOCATION"),
        "organizer": (_person(props["ORGANIZER"][1], props["ORGANIZER"][0])
                      if "ORGANIZER" in props else None),
        "status": (props.get("STATUS", ({}, ""))[1].strip().upper() or None),
        "sequence": int(seq) if seq.isdigit() else 0,
        "recurring": bool(rrule),
        "series_until": series_until,
        "reply_partstat": partstat,
        "tz_assumed": assumed,
    }


def _localize(iso: str, all_day: bool, tz: str) -> str:
    if all_day:
        return iso
    return datetime.fromisoformat(iso).astimezone(ZoneInfo(tz)).isoformat(timespec="seconds")


def _external_calendar_texts(attachments_dir) -> list[str]:
    """For .partial.emlx messages Mail stores attachment bytes outside the
    message, under .../Attachments/<msgnum>/<part>/<file>. Return the text of
    any small file there that is an iCalendar object."""
    out = []
    try:
        files = sorted(p for p in attachments_dir.rglob("*") if p.is_file())
    except OSError:
        return out
    for f in files[:50]:
        try:
            if f.stat().st_size > MAX_ICS_BYTES:
                continue
            raw = f.read_bytes()
        except OSError:
            continue
        if b"BEGIN:VCALENDAR" in raw[:4096].upper():
            out.append(raw.decode("utf-8", errors="replace"))
    return out


def extract_invites(msg, default_tz: str, attachments_dir=None) -> list[dict]:
    """All invite VEVENTs in a message, times localized to default_tz,
    deduplicated (Exchange often sends the same invite inline and attached).
    attachments_dir: the message's external attachment folder, for calendar
    parts whose bytes Mail stored outside a .partial.emlx."""
    out: list[dict] = []
    seen = set()
    try:
        parts = list(msg.walk())
    except Exception:
        return []
    texts: list[str] = []
    needs_external = False
    for part in parts:
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        fname = (part.get_filename() or "").lower()
        if ctype not in ("text/calendar", "application/ics") and not fname.endswith(".ics"):
            continue
        try:
            raw = part.get_payload(decode=True)
            if not raw or not raw.strip():
                needs_external = True
                continue
            charset = part.get_content_charset() or "utf-8"
            texts.append(raw[:MAX_ICS_BYTES].decode(charset, errors="replace"))
        except Exception:
            continue
    if needs_external and attachments_dir is not None:
        texts.extend(_external_calendar_texts(attachments_dir))
    for text in texts:
        for ev in parse_ics(text, default_tz):
            try:
                ev["start"] = _localize(ev["start"], ev["all_day"], default_tz)
                ev["end"] = _localize(ev["end"], ev["all_day"], default_tz)
            except ValueError:
                continue
            key = (ev["uid"], ev["start"], ev["method"])
            if key in seen:
                continue
            seen.add(key)
            out.append(ev)
    return out


def invite_relevant_until(rec: dict) -> datetime | None:
    """Latest moment any meeting in this message still matters, or None.
    Replies (METHOD:REPLY) are responses, not meetings, and don't count."""
    best = None
    for inv in rec.get("invites") or []:
        if inv.get("method") == "REPLY":
            continue
        try:
            if inv["all_day"]:
                end = datetime.combine(date.fromisoformat(inv["end"]),
                                       datetime.min.time(), tzinfo=timezone.utc)
            else:
                end = datetime.fromisoformat(inv["end"])
            if inv.get("recurring"):
                if inv.get("series_until"):
                    end = max(end, datetime.combine(
                        date.fromisoformat(inv["series_until"]),
                        datetime.max.time(), tzinfo=timezone.utc))
                else:  # open-ended series: relevant for a year from its start
                    start = (datetime.combine(date.fromisoformat(inv["start"]),
                                              datetime.min.time(), tzinfo=timezone.utc)
                             if inv["all_day"] else datetime.fromisoformat(inv["start"]))
                    end = max(end, start + timedelta(days=365))
        except (KeyError, ValueError):
            continue
        if best is None or end > best:
            best = end
    return best
