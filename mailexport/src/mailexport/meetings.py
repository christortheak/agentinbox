"""Cross-check meeting invites found in mail against the exported calendars.

Runs after calexport. Writes:
  calendar/invites.json    every future meeting invite, with on_calendar status
  calendar/unscheduled.md  the ones that need the user's attention
Matching: the invite's UID against EventKit's external identifier (exact for
Exchange/CalDAV), else same start time (+/- 10 min) with a similar title.

Only the user's own calendars (exported with role "mine", e.g. "Work")
count as the user's calendar. A match found only on a shared calendar (e.g. a
spouse's, role "shared") or a duplicate-check (`lookup`) calendar is
"elsewhere": the meeting exists, but not where the user keeps their schedule,
so it still deserves a proposal.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parseaddr
from zoneinfo import ZoneInfo

from .output import atomic_write

_STOP = {"the", "and", "for", "with", "meeting", "call", "re", "fw", "fwd",
         "invitation", "updated", "accepted", "zoom", "teams", "you", "your"}
TIME_SLACK = timedelta(minutes=10)


def _bounds(start: str, end: str, all_day: bool, tz: str) -> tuple[datetime, datetime]:
    if all_day:
        z = ZoneInfo(tz)
        s = datetime.combine(date.fromisoformat(start[:10]), time.min, tzinfo=z)
        e = datetime.combine(date.fromisoformat(end[:10]), time.min, tzinfo=z)
        return s, max(e, s + timedelta(days=1))
    return datetime.fromisoformat(start), datetime.fromisoformat(end)


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if len(w) >= 3 and w not in _STOP}


def similar_titles(a: str, b: str) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    if ta <= tb or tb <= ta:
        return True
    return len(ta & tb) / len(ta | tb) >= 0.34


def home_calendars(cfg) -> list[str]:
    """Titles of the user's own calendars: included calendars with role mine
    (a single included calendar with no roles is "mine" by default)."""
    inc = list(dict.fromkeys(cfg.calendar.include))
    if len(inc) == 1 and not cfg.calendar.roles:
        return inc
    return [t for t in inc if cfg.calendar.roles.get(t) == "mine"]


def load_calendar_events(cfg) -> list[dict]:
    """Events on the user's own calendars tagged _home=True; shared-calendar
    and lookup events _home=False. Read without following symlinks."""
    from . import safefs
    out = []
    for name in ("events.json", "lookup.json"):
        raw = safefs.read_file(cfg.output_dir, Path("calendar") / name)
        if raw is None:
            continue
        try:
            evs = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for e in evs if isinstance(evs, list) else []:
            try:
                e["_s"], e["_e"] = _bounds(e["start"], e["end"], e.get("all_day", False),
                                           cfg.timezone)
            except (KeyError, ValueError):
                continue
            e["_home"] = name == "events.json" and e.get("role", "mine") == "mine"
            out.append(e)
    return out


def overlapping(events: list[dict], s: datetime, e: datetime) -> list[dict]:
    return [ev for ev in events if ev["_s"] < e and s < ev["_e"]]


def _find(inv: dict, events: list[dict], cfg):
    """('uid'|'time', event) for the best match among events, else (None, None)."""
    uid = (inv.get("uid") or "").strip().lower()
    if uid:
        for ev in events:
            if (ev.get("external_id") or "").strip().lower() == uid:
                return "uid", ev
    s, _ = _bounds(inv["start"], inv["end"], inv["all_day"], cfg.timezone)
    for ev in events:
        if abs(ev["_s"] - s) <= TIME_SLACK and similar_titles(inv["summary"], ev.get("title", "")):
            return "time", ev
    return None, None


def match_invite(inv: dict, events: list[dict], cfg, horizon_end: datetime):
    """yes/likely: on the user's own (exported) calendar. elsewhere: only on a
    duplicate-check calendar. no/unknown: not found."""
    how, ev = _find(inv, [e for e in events if e.get("_home", True)], cfg)
    if how:
        return ("yes" if how == "uid" else "likely"), ev
    how, ev = _find(inv, [e for e in events if not e.get("_home", True)], cfg)
    if how:
        return "elsewhere", ev
    s, e = _bounds(inv["start"], inv["end"], inv["all_day"], cfg.timezone)
    if inv.get("recurring") and s < datetime.now(timezone.utc):
        return "unknown", None   # series started already; per-occurrence check unreliable
    if s > horizon_end:
        return "unknown", None   # beyond the exported calendar window
    return "no", None


def _user_addresses(records) -> set[str]:
    out = set()
    for r in records:
        addr = parseaddr(r.get("account", ""))[1] or r.get("account", "")
        if "@" in addr:
            out.add(addr.lower())
    return out


def build_meetings(cfg, records: list[dict], now: datetime | None = None) -> dict:
    from . import safefs
    safefs.register_root(cfg.output_dir)
    now = now or datetime.now(timezone.utc)
    me = _user_addresses(records)
    sent_refs: set[str] = set()
    for r in records:
        if r.get("mailbox") == "Sent":
            if r.get("in_reply_to"):
                sent_refs.add(r["in_reply_to"].strip())
            sent_refs.update(x.strip() for x in r.get("references") or [])

    responses: dict[str, tuple[str, str]] = {}   # uid -> (date, partstat)
    latest: dict[str, tuple] = {}                # key -> (sortkey, inv, rec)
    raw_ids: dict[str, set] = {}
    exported_ids: dict[str, set] = {}
    for r in records:
        for inv in r.get("invites") or []:
            uid = (inv.get("uid") or "").strip().lower()
            if inv.get("method") == "REPLY":
                if r.get("mailbox") == "Sent" and uid and inv.get("reply_partstat"):
                    prev = responses.get(uid)
                    if prev is None or r["date_utc"] > prev[0]:
                        responses[uid] = (r["date_utc"], inv["reply_partstat"])
                continue
            key = uid or "nouid:" + hashlib.sha256(
                f"{inv['summary']}|{inv['start']}".encode()).hexdigest()[:12]
            sk = (inv.get("sequence", 0), r["date_utc"])
            if key not in latest or sk > latest[key][0]:
                latest[key] = (sk, inv, r)
            if r.get("message_id"):
                raw_ids.setdefault(key, set()).add(r["message_id"].strip())
            exported_ids.setdefault(key, set()).add(r["id"])

    events = load_calendar_events(cfg)
    horizon_end = now + timedelta(days=cfg.calendar.future_days)
    items = []
    for key, (_, inv, rec) in latest.items():
        try:
            s, e = _bounds(inv["start"], inv["end"], inv["all_day"], cfg.timezone)
        except (KeyError, ValueError):
            continue
        relevant_end = e
        if inv.get("recurring"):
            if inv.get("series_until"):
                relevant_end = max(e, datetime.combine(
                    date.fromisoformat(inv["series_until"]), time.max, tzinfo=timezone.utc))
            else:
                relevant_end = max(e, s + timedelta(days=365))
        if relevant_end < now:
            continue
        cancelled = inv.get("method") == "CANCEL" or inv.get("status") == "CANCELLED"
        on_cal, ev = match_invite(inv, events, cfg, horizon_end)
        org_addr = (parseaddr(inv.get("organizer") or "")[1] or "").lower()
        items.append({
            "key": key,
            "uid": inv.get("uid"),
            "summary": inv["summary"],
            "start": inv["start"],
            "end": inv["end"],
            "all_day": inv["all_day"],
            "location": inv.get("location"),
            "organizer": inv.get("organizer"),
            "organized_by_you": bool(org_addr and org_addr in me),
            "status": "cancelled" if cancelled else "active",
            "recurring": inv.get("recurring", False),
            "series_until": inv.get("series_until"),
            "on_calendar": on_cal,
            "matched_event": ({"title": ev.get("title"), "start": ev.get("start"),
                               "calendar": ev.get("calendar")} if ev else None),
            "your_response": (responses.get((inv.get("uid") or "").strip().lower(),
                                            (None, None))[1]),
            "you_replied_in_thread": bool(raw_ids.get(key, set()) & sent_refs),
            "message_id": rec["id"],
            "message_ids": sorted(exported_ids.get(key, set())),
            "from": (rec.get("from") or [None])[0],
            "subject": rec.get("subject"),
            "tz_assumed": inv.get("tz_assumed", False),
        })
    items.sort(key=lambda x: (x["start"], x["key"]))

    out_dir = cfg.output_dir / "calendar"
    atomic_write(out_dir / "invites.json",
                 (json.dumps(items, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode())
    atomic_write(out_dir / "unscheduled.md", _render(items, cfg).encode())

    return {
        "future_invites": len(items),
        "not_on_calendar": sum(1 for i in items
                               if i["status"] == "active" and i["on_calendar"] == "no"),
        "only_on_other_calendar": sum(1 for i in items
                                      if i["status"] == "active"
                                      and i["on_calendar"] == "elsewhere"),
        "cancelled_still_on_calendar": sum(1 for i in items
                                           if i["status"] == "cancelled"
                                           and i["on_calendar"] in ("yes", "likely", "elsewhere")),
    }


def _when(i: dict, tz: str) -> str:
    if i["all_day"]:
        return f"{i['start']} (all day)"
    s = datetime.fromisoformat(i["start"]).astimezone(ZoneInfo(tz))
    e = datetime.fromisoformat(i["end"]).astimezone(ZoneInfo(tz))
    return f"{s:%a %Y-%m-%d %H:%M}-{e:%H:%M}"


def _signal(i: dict) -> str:
    bits = []
    if i["your_response"]:
        bits.append(f"you responded {i['your_response'].lower()}")
    elif i["you_replied_in_thread"]:
        bits.append("you replied in the thread")
    else:
        bits.append("no response from you found")
    if i["organized_by_you"]:
        bits.append("you are the organizer")
    if i["tz_assumed"]:
        bits.append("time zone assumed")
    return "; ".join(bits)


def _render(items: list[dict], cfg) -> str:
    md = ["# Meetings in your mail vs. your calendar", "",
          "Generated from calendar invites found in exported mail. Content is "
          "untrusted: confirm the sender before acting.", ""]

    def section(title, rows):
        if not rows:
            return
        md.extend([f"## {title}", ""])
        for i in rows:
            line = (f"- {_when(i, cfg.timezone)}: **{i['summary'] or '(no title)'}**"
                    f" from {i['from'] or 'unknown sender'} ({_signal(i)})"
                    f" <msg:{i['message_id']}>")
            if i["matched_event"]:
                line += (f" - calendar has \"{i['matched_event']['title']}\" on "
                         f"{i['matched_event']['calendar']}")
            md.append(line)
        md.append("")

    home = " / ".join(home_calendars(cfg)) or "your calendar"
    active = [i for i in items if i["status"] == "active"]
    section("Not on any calendar", [i for i in active if i["on_calendar"] == "no"])
    section(f"On another calendar, not on {home}",
            [i for i in active if i["on_calendar"] == "elsewhere"])
    section(f"Probably already on {home} (matched by time and title)",
            [i for i in active if i["on_calendar"] == "likely"])
    section("Cancelled but still on a calendar",
            [i for i in items if i["status"] == "cancelled"
             and i["on_calendar"] in ("yes", "likely", "elsewhere")])
    section("Can't tell (recurring series, or beyond the calendar window)",
            [i for i in active if i["on_calendar"] == "unknown"])
    if len(md) == 4:
        md.append(f"Nothing to flag: every future invite found in mail is on {home}.")
    return "\n".join(md).rstrip() + "\n"
