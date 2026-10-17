import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from zoneinfo import ZoneInfo

import pytest

from conftest import build_emlx, plain_message
from mailexport.config import Config, MailConfig
from mailexport.proposals import ProposalError, fold, propose_event
from mailexport.runner import prune_outputs, run_mail_export

TZ = ZoneInfo("America/New_York")
ACCT_UUID = "11111111-2222-3333-4444-555555555555"


def _cfg(tmp_path, formats=("ics",)):
    cfg = Config()
    cfg.output_dir = tmp_path / "out"
    cfg.proposals.formats = list(formats)
    (cfg.output_dir / "proposals").mkdir(parents=True)
    return cfg


def _day(n):
    return (datetime.now(TZ).date() + timedelta(days=n))


def _ics_lines(path):
    raw = open(path, "rb").read()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    return raw.decode().split("\r\n")


def test_timed_proposal_ics_is_safe_and_correct(tmp_path):
    cfg = _cfg(tmp_path)
    d = _day(20)
    res = propose_event(cfg, {
        "title": "Budget review, FY27; final",
        "start": f"{d}T14:00", "end": f"{d}T15:30",
        "location": "Room 101", "notes": "Bring slides\nand the draft",
    })
    assert res["ok"] and res["ics_path"].endswith(".ics")
    assert "google_url" not in res and "outlook_url" not in res
    lines = _ics_lines(res["ics_path"])
    text = "\n".join(lines)
    assert "METHOD:PUBLISH" in lines
    assert "ATTENDEE" not in text and "ORGANIZER" not in text
    assert "SUMMARY:Budget review\\, FY27\\; final" in lines
    start_utc = datetime(d.year, d.month, d.day, 14, 0, tzinfo=TZ).astimezone(timezone.utc)
    assert f"DTSTART:{start_utc:%Y%m%dT%H%M%SZ}" in lines
    end_utc = start_utc + timedelta(minutes=90)
    assert f"DTEND:{end_utc:%Y%m%dT%H%M%SZ}" in lines
    assert all(len(l.encode()) <= 75 for l in lines)
    unfolded = text.replace("\n ", "")
    assert "Bring slides\\nand the draft" in unfolded
    assert "Proposed by AgentInbox" in unfolded


def test_allday_multiday_and_links(tmp_path):
    cfg = _cfg(tmp_path, formats=("ics", "google", "outlook"))
    d1, d2 = _day(40), _day(42)
    res = propose_event(cfg, {"title": "Conference & travel", "start": str(d1),
                              "end": str(d2)})
    lines = _ics_lines(res["ics_path"])
    assert f"DTSTART;VALUE=DATE:{d1:%Y%m%d}" in lines
    assert f"DTEND;VALUE=DATE:{d2 + timedelta(days=1):%Y%m%d}" in lines  # exclusive
    assert res["end"] == str(d2)
    g = res["google_url"]
    assert g.startswith("https://calendar.google.com/calendar/render?action=TEMPLATE")
    assert "text=Conference%20%26%20travel" in g
    assert f"dates={d1:%Y%m%d}%2F{d2 + timedelta(days=1):%Y%m%d}" in g
    o = res["outlook_url"]
    assert o.startswith("https://outlook.office.com/calendar/0/deeplink/compose?")
    assert "allday=true" in o


def test_validation(tmp_path):
    cfg = _cfg(tmp_path)
    d = _day(5)
    bad = [
        {"title": "", "start": f"{d}T10:00"},
        {"title": "x", "start": "next tuesday"},
        {"title": "x", "start": f"{d}T10:00", "end": f"{d}T09:00"},
        {"title": "x", "start": f"{d}T10:00", "end": str(d)},
        {"title": "x", "start": str(d), "end": str(d + timedelta(days=20))},
        {"title": "x", "start": f"{_day(400)}T10:00"},
        {"title": "x", "start": f"{_day(-5)}T10:00"},
        {"title": "x", "start": f"{d}T10:00", "timezone": "Mars/Base"},
        {"title": "x", "start": f"{d}T10:00", "source_message_id": "../etc"},
        {"title": "x", "start": f"{d}T10:00", "source_message_id": "0" * 16},
    ]
    for req in bad:
        with pytest.raises(ProposalError):
            propose_event(cfg, req)
    cfg.proposals.enabled = False
    with pytest.raises(ProposalError, match="disabled"):
        propose_event(cfg, {"title": "x", "start": f"{d}T10:00"})


def test_provenance_and_conflicts(tmp_store, tmp_path):
    cfg = _cfg(tmp_path)
    cfg.mail = MailConfig(accounts={ACCT_UUID: "Test <me@example.com>"})
    (tmp_store["messages"] / "1.emlx").write_bytes(build_emlx(plain_message(
        subject="Lunch next week?", frm="Sam Lee <sam@example.com>",
        date=format_datetime(datetime.now(timezone.utc) - timedelta(days=1)))))
    run_mail_export(cfg, mail_library=tmp_store["library"])
    msg_id = next(iter(json.loads((cfg.state_dir / "mail.json").read_text())["messages"]))

    d = _day(7)
    busy_s = datetime(d.year, d.month, d.day, 12, 30, tzinfo=TZ)
    (cfg.output_dir / "calendar").mkdir(exist_ok=True)
    (cfg.output_dir / "calendar" / "events.json").write_text(json.dumps([
        {"title": "Office hours", "start": busy_s.isoformat(),
         "end": (busy_s + timedelta(hours=1)).isoformat(), "all_day": False,
         "calendar": "Work"}]))
    res = propose_event(cfg, {"title": "Lunch with Sam", "start": f"{d}T12:00",
                              "source_message_id": msg_id})
    assert res["source"]["from"] == "Sam Lee <sam@example.com>"
    assert res["conflicts"][0]["title"] == "Office hours"
    unfolded = open(res["ics_path"]).read().replace("\r\n ", "")
    assert "Sam Lee <sam@example.com>" in unfolded and "Lunch next week?" in unfolded


def test_fold_multibyte():
    line = "SUMMARY:" + "é" * 80
    folded = fold(line)
    for part in folded.split("\r\n"):
        assert len(part.encode("utf-8")) <= 75
    assert folded.replace("\r\n ", "") == line


def test_prune_past_proposals(tmp_path):
    cfg = _cfg(tmp_path)
    p = cfg.output_dir / "proposals"
    old = p / f"{_day(-3)}-old-aaaaaaaa.ics"
    new = p / f"{_day(3)}-new-bbbbbbbb.ics"
    old.write_text("x")
    new.write_text("x")
    prune_outputs(cfg)
    assert not old.exists() and new.exists()
