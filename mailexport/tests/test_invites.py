from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from mailexport.invites import extract_invites, invite_relevant_until, parse_ics

TZ = "America/New_York"

EXCHANGE_INVITE = (
    "BEGIN:VCALENDAR\r\n"
    "METHOD:REQUEST\r\n"
    "PRODID:Microsoft Exchange Server 2010\r\n"
    "VERSION:2.0\r\n"
    "BEGIN:VTIMEZONE\r\nTZID:Eastern Standard Time\r\nEND:VTIMEZONE\r\n"
    "BEGIN:VEVENT\r\n"
    "ORGANIZER;CN=\"Dean, Pat\":mailto:pat@example.edu\r\n"
    "ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;CN=Me:mailto:me@example.edu\r\n"
    "SUMMARY;LANGUAGE=en-US:Budget review\\, FY27 planning\r\n"
    "DTSTART;TZID=Eastern Standard Time:20261203T140000\r\n"
    "DTEND;TZID=Eastern Standard Time:20261203T150000\r\n"
    "UID:040000008200E00074C5B7101A82E00800000000ABCDEF\r\n"
    "SEQUENCE:2\r\n"
    "LOCATION:Room 101\\; Main\r\n"
    " Building\r\n"
    "BEGIN:VALARM\r\nTRIGGER:-PT15M\r\nEND:VALARM\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def test_exchange_invite_windows_tz_and_folding():
    [ev] = parse_ics(EXCHANGE_INVITE, TZ)
    assert ev["method"] == "REQUEST"
    assert ev["summary"] == "Budget review, FY27 planning"
    assert ev["location"] == "Room 101; MainBuilding"  # folded continuation joined
    assert ev["organizer"] == "Dean, Pat <pat@example.edu>"
    assert ev["start"].startswith("2026-12-03T14:00:00-05:00")
    assert ev["end"].startswith("2026-12-03T15:00:00-05:00")
    assert ev["sequence"] == 2
    assert ev["tz_assumed"] is False
    assert ev["all_day"] is False


def test_utc_allday_duration_floating_unknown_tz():
    ics = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:a\nSUMMARY:UTC\n"
           "DTSTART:20261110T180000Z\nDURATION:PT45M\nEND:VEVENT\n"
           "BEGIN:VEVENT\nUID:b\nSUMMARY:Holiday\nDTSTART;VALUE=DATE:20261126\n"
           "DTEND;VALUE=DATE:20261128\nEND:VEVENT\n"
           "BEGIN:VEVENT\nUID:c\nSUMMARY:Floating\nDTSTART:20261110T090000\nEND:VEVENT\n"
           "BEGIN:VEVENT\nUID:d\nSUMMARY:Odd\nDTSTART;TZID=Mars/Olympus:20261110T090000\n"
           "END:VEVENT\nEND:VCALENDAR\n")
    a, b, c, d = parse_ics(ics, TZ)
    assert a["start"] == "2026-11-10T18:00:00+00:00"
    assert a["end"] == "2026-11-10T18:45:00+00:00"
    assert b["all_day"] and b["start"] == "2026-11-26" and b["end"] == "2026-11-28"
    assert c["tz_assumed"] is True and c["start"].startswith("2026-11-10T09:00:00-05:00")
    assert d["tz_assumed"] is True


def test_cancel_reply_and_garbage():
    cancel = ("BEGIN:VCALENDAR\nMETHOD:CANCEL\nBEGIN:VEVENT\nUID:x\nSTATUS:CANCELLED\n"
              "SUMMARY:Gone\nDTSTART:20261201T150000Z\nEND:VEVENT\nEND:VCALENDAR\n")
    [ev] = parse_ics(cancel, TZ)
    assert ev["method"] == "CANCEL" and ev["status"] == "CANCELLED"
    reply = ("BEGIN:VCALENDAR\nMETHOD:REPLY\nBEGIN:VEVENT\nUID:x\n"
             "ATTENDEE;PARTSTAT=ACCEPTED:mailto:me@x.com\n"
             "DTSTART:20261201T150000Z\nEND:VEVENT\nEND:VCALENDAR\n")
    [ev] = parse_ics(reply, TZ)
    assert ev["method"] == "REPLY" and ev["reply_partstat"] == "ACCEPTED"
    assert parse_ics("not a calendar at all\n:::\nBEGIN:VEVENT\n", TZ) == []
    assert parse_ics("BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:garbage\nEND:VEVENT\n", TZ) == []


def test_extract_from_message_dedupes_inline_and_attached():
    msg = EmailMessage()
    msg["From"] = "pat@example.edu"
    msg["Subject"] = "Invitation"
    msg.set_content("You're invited")
    msg.add_alternative(EXCHANGE_INVITE, subtype="calendar")
    msg.add_attachment(EXCHANGE_INVITE.encode(), maintype="application",
                       subtype="ics", filename="invite.ics")
    invites = extract_invites(msg, TZ)
    assert len(invites) == 1
    assert invites[0]["summary"] == "Budget review, FY27 planning"


def _rec(invites):
    return {"invites": invites}


def test_relevance():
    now = datetime.now(timezone.utc)
    fut = (now + timedelta(days=40)).isoformat(timespec="seconds")
    past = (now - timedelta(days=40)).isoformat(timespec="seconds")
    base = {"all_day": False, "recurring": False, "series_until": None, "method": "REQUEST"}
    assert invite_relevant_until(_rec([dict(base, start=fut, end=fut)])) > now
    assert invite_relevant_until(_rec([dict(base, start=past, end=past)])) < now
    # Open-ended recurring series that started 40 days ago stays relevant.
    assert invite_relevant_until(_rec([dict(base, start=past, end=past, recurring=True)])) > now
    # Replies don't count as meetings.
    assert invite_relevant_until(_rec([dict(base, start=fut, end=fut, method="REPLY")])) is None
    assert invite_relevant_until({"invites": []}) is None
