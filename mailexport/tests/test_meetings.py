import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from mailexport.config import Config
from mailexport.meetings import build_meetings, similar_titles

TZ = ZoneInfo("America/New_York")


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _inv(uid, summary, start, end, method="REQUEST", **kw):
    d = {"uid": uid, "summary": summary, "start": _iso(start), "end": _iso(end),
         "all_day": False, "location": None, "organizer": "Pat <pat@example.edu>",
         "status": None, "sequence": 0, "recurring": False, "series_until": None,
         "reply_partstat": None, "tz_assumed": False, "method": method}
    d.update(kw)
    return d


def _rec(id_, invites, mailbox="INBOX", message_id=None, in_reply_to=None,
         account="me@example.edu", date="2026-10-01T12:00:00+00:00"):
    return {"id": id_, "account": account, "mailbox": mailbox, "invites": invites,
            "message_id": message_id, "in_reply_to": in_reply_to, "references": [],
            "from": ["Pat <pat@example.edu>"], "subject": "subj " + id_, "date_utc": date}


def test_similar_titles():
    assert similar_titles("Budget review FY27", "FY27 budget review")
    assert similar_titles("Dissertation defense", "Defense: dissertation (Smith)")
    assert not similar_titles("Budget review", "Lunch with Sam")
    assert not similar_titles("Meeting", "Call")  # stopwords only


def test_build_meetings_statuses(tmp_path):
    cfg = Config()
    cfg.output_dir = tmp_path / "out"
    cfg.calendar.include = ["Work"]
    (cfg.output_dir / "calendar").mkdir(parents=True)
    now = datetime.now(timezone.utc)
    d = (now + timedelta(days=30)).astimezone(TZ).replace(hour=14, minute=0, second=0,
                                                          microsecond=0)
    one = timedelta(hours=1)
    events = [
        {"title": "Budget review", "start": _iso(d), "end": _iso(d + one),
         "all_day": False, "calendar": "Work", "external_id": "UID-EXACT"},
        {"title": "FY27 Planning Retreat", "start": _iso(d + timedelta(days=1)),
         "end": _iso(d + timedelta(days=1) + one), "all_day": False,
         "calendar": "Work", "external_id": None},
        {"title": "Old thing", "start": _iso(d + timedelta(days=2)),
         "end": _iso(d + timedelta(days=2) + one), "all_day": False,
         "calendar": "Calendar", "external_id": "UID-CANCELLED"},
    ]
    (cfg.output_dir / "calendar" / "events.json").write_text(json.dumps(events))
    # Accepted work invite that landed on the Exchange account's calendar only.
    work = d + timedelta(days=4)
    (cfg.output_dir / "calendar" / "lookup.json").write_text(json.dumps([
        {"title": "Quarterly Planning Review", "start": _iso(work),
         "end": _iso(work + one), "all_day": False, "calendar": "Calendar",
         "external_id": "AAMkAD-exchange-item-id"}]))

    records = [
        _rec("a" * 16, [_inv("UID-EXACT", "Budget review", d, d + one)]),
        _rec("b" * 16, [_inv("UID-OTHER", "Planning retreat FY27",
                             d + timedelta(days=1, minutes=5),
                             d + timedelta(days=1, minutes=65))]),
        _rec("c" * 16, [_inv("UID-MISSING", "Advisory board", d + timedelta(days=3),
                             d + timedelta(days=3) + one)],
             message_id="<adv@example.edu>"),
        _rec("d" * 16, [_inv("UID-CANCELLED", "Old thing", d + timedelta(days=2),
                             d + timedelta(days=2) + one, method="CANCEL")]),
        _rec("9" * 16, [_inv("UID-WORKCAL", "Quarterly Planning Review Meeting",
                             work, work + one)]),
        _rec("e" * 16, [_inv("UID-PAST", "Long ago", now - timedelta(days=10),
                             now - timedelta(days=10) + one)]),
        _rec("f" * 16, [_inv("UID-FAR", "Next year", now + timedelta(days=500),
                             now + timedelta(days=500) + one)]),
        # The user's RSVP in Sent, and a reply in the advisory-board thread.
        _rec("1" * 16, [_inv("UID-MISSING", "Advisory board", d + timedelta(days=3),
                             d + timedelta(days=3) + one, method="REPLY",
                             reply_partstat="ACCEPTED")], mailbox="Sent"),
        _rec("2" * 16, [], mailbox="Sent", in_reply_to="<adv@example.edu>"),
    ]
    summary = build_meetings(cfg, records, now=now)
    items = {i["uid"]: i for i in json.loads(
        (cfg.output_dir / "calendar" / "invites.json").read_text())}

    assert items["UID-EXACT"]["on_calendar"] == "yes"
    assert items["UID-OTHER"]["on_calendar"] == "likely"
    missing = items["UID-MISSING"]
    assert missing["on_calendar"] == "no"
    assert missing["your_response"] == "ACCEPTED"
    assert missing["you_replied_in_thread"] is True
    assert missing["message_id"] == "c" * 16
    assert items["UID-CANCELLED"]["status"] == "cancelled"
    assert "UID-PAST" not in items
    assert items["UID-FAR"]["on_calendar"] == "unknown"  # beyond the calendar window
    assert items["UID-WORKCAL"]["on_calendar"] == "elsewhere"
    assert items["UID-WORKCAL"]["matched_event"]["calendar"] == "Calendar"
    assert summary == {"future_invites": 6, "not_on_calendar": 1,
                       "only_on_other_calendar": 1, "cancelled_still_on_calendar": 1}

    md = (cfg.output_dir / "calendar" / "unscheduled.md").read_text()
    assert "## Not on any calendar" in md and "Advisory board" in md
    assert "## On another calendar, not on Work" in md
    assert "Quarterly Planning Review Meeting" in md
    assert "you responded accepted" in md
    assert "## Cancelled but still on a calendar" in md and "Old thing" in md


def test_organizer_is_user(tmp_path):
    cfg = Config()
    cfg.output_dir = tmp_path / "out"
    (cfg.output_dir / "calendar").mkdir(parents=True)
    d = datetime.now(timezone.utc) + timedelta(days=5)
    recs = [_rec("a" * 16, [_inv("U1", "My meeting", d, d + timedelta(hours=1),
                                 organizer="Me <ME@example.edu>")])]
    build_meetings(cfg, recs)
    [item] = json.loads((cfg.output_dir / "calendar" / "invites.json").read_text())
    assert item["organized_by_you"] is True


def test_shared_calendar_is_not_home(tmp_path):
    """A joint event on a spouse's calendar (role shared) doesn't count as
    being on the user's own calendar."""
    from mailexport.meetings import home_calendars
    cfg = Config()
    cfg.output_dir = tmp_path / "out"
    cfg.calendar.include = ["Work", "Partner"]
    cfg.calendar.roles = {"Work": "mine", "Partner": "shared"}
    assert home_calendars(cfg) == ["Work"]
    (cfg.output_dir / "calendar").mkdir(parents=True)
    d = (datetime.now(timezone.utc) + timedelta(days=12)).replace(microsecond=0)
    one = timedelta(hours=2)
    (cfg.output_dir / "calendar" / "events.json").write_text(json.dumps([
        {"title": "Dinner with the Smiths", "start": _iso(d), "end": _iso(d + one),
         "all_day": False, "calendar": "Partner", "role": "shared", "external_id": None}]))
    recs = [_rec("a" * 16, [_inv("U-DINNER", "Dinner with the Smiths", d, d + one)])]
    summary = build_meetings(cfg, recs)
    [item] = json.loads((cfg.output_dir / "calendar" / "invites.json").read_text())
    assert item["on_calendar"] == "elsewhere"
    assert item["matched_event"]["calendar"] == "Partner"
    assert summary["only_on_other_calendar"] == 1
    md = (cfg.output_dir / "calendar" / "unscheduled.md").read_text()
    assert "## On another calendar, not on Work" in md

    cfg.calendar.include, cfg.calendar.roles = ["Work"], {}
    assert home_calendars(cfg) == ["Work"]   # single include, no roles = mine
