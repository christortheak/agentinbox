import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from conftest import build_emlx, plain_message
from mailexport.config import Config, MailConfig
from mailexport.pins import remove_pin, set_pin
from mailexport.runner import run_mail_export

ACCT_UUID = "11111111-2222-3333-4444-555555555555"


def _cfg(tmp_path, days):
    cfg = Config()
    cfg.output_dir = tmp_path / "out"
    cfg.mail = MailConfig(days=days, accounts={ACCT_UUID: "Test <me@example.com>"})
    return cfg


def _ics(start: datetime) -> str:
    end = start + timedelta(hours=1)
    f = "%Y%m%dT%H%M%SZ"
    return ("BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nUID:future-1\r\n"
            f"SUMMARY:Far-off meeting\r\nDTSTART:{start:{f}}\r\nDTEND:{end:{f}}\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n")


def _index(cfg):
    return {r["subject"]: r for r in map(json.loads,
            (cfg.output_dir / "mail" / "index.jsonl").read_text().splitlines())}


def test_meeting_and_pin_retention(tmp_store, tmp_path):
    now = datetime.now(timezone.utc)
    old = format_datetime(now - timedelta(days=120))

    invite = plain_message(subject="Invite", message_id="<inv@x>", date=old)
    invite.add_alternative(_ics(now + timedelta(days=30)), subtype="calendar")
    (tmp_store["messages"] / "1.emlx").write_bytes(build_emlx(invite))
    (tmp_store["messages"] / "2.emlx").write_bytes(build_emlx(
        plain_message(subject="Plain", message_id="<plain@x>", date=old)))
    (tmp_store["messages"] / "3.emlx").write_bytes(build_emlx(
        plain_message(subject="Pinned", message_id="<pin@x>", date=old)))

    # First export with a wide window, as if they arrived while recent.
    wide = _cfg(tmp_path, 365)
    run_mail_export(wide, mail_library=tmp_store["library"])
    idx = _index(wide)
    assert set(idx) == {"Invite", "Plain", "Pinned"}
    assert idx["Invite"]["invites"][0]["summary"] == "Far-off meeting"
    assert all(r["retained"] == "window" for r in idx.values())

    plain_md = wide.output_dir / "mail" / "messages" / f"{idx['Plain']['id']}.md"
    assert plain_md.is_file()

    narrow = _cfg(tmp_path, 90)
    set_pin(narrow, idx["Pinned"]["id"],
            (now + timedelta(days=10)).date().isoformat(), "plain-text meeting")
    s = run_mail_export(narrow, mail_library=tmp_store["library"])
    idx = _index(narrow)
    assert set(idx) == {"Invite", "Pinned"}          # Plain aged out
    assert idx["Invite"]["retained"] == "meeting"
    assert idx["Pinned"]["retained"] == "pinned"
    assert s["retained_beyond_window"] == 2
    assert not plain_md.exists()

    remove_pin(narrow, idx["Pinned"]["id"])
    run_mail_export(narrow, mail_library=tmp_store["library"])
    assert set(_index(narrow)) == {"Invite"}


def test_pin_validation(tmp_path):
    cfg = _cfg(tmp_path, 90)
    import pytest
    for bad in ("yesterday", "2020-01-01",
                (datetime.now().date() + timedelta(days=400)).isoformat()):
        with pytest.raises(ValueError):
            set_pin(cfg, "a" * 16, bad, None)


def test_legacy_state_removed_and_state_outside_output(tmp_store, tmp_path):
    cfg = _cfg(tmp_path, 90)
    legacy = cfg.output_dir / "state"
    legacy.mkdir(parents=True)
    (legacy / "mail.json").write_text("{}")
    (legacy / "run.lock").write_text("1")
    (tmp_store["messages"] / "1.emlx").write_bytes(build_emlx(plain_message(
        date=format_datetime(datetime.now(timezone.utc) - timedelta(days=1)))))
    run_mail_export(cfg, mail_library=tmp_store["library"])
    assert not legacy.exists()
    assert (cfg.state_dir / "mail.json").exists()
    assert not str(cfg.state_dir).startswith(str(cfg.output_dir))


def test_invite_in_partial_emlx_external_attachment(tmp_store, tmp_path):
    """Real-world case: Mail keeps the .ics of a .partial.emlx outside the
    message, under Attachments/<msgnum>/<part>/. The parser must find it."""
    now = datetime.now(timezone.utc)
    msg = plain_message(subject="Invitation: Advisory board", message_id="<adv@x>",
                        date=format_datetime(now - timedelta(days=2)))
    msg.add_attachment(b"", maintype="application", subtype="ics", filename="invite.ics")
    for part in msg.walk():
        if part.get_filename() == "invite.ics":
            part.set_payload("")
    (tmp_store["messages"] / "777.partial.emlx").write_bytes(build_emlx(msg))
    ext = tmp_store["messages"].parent / "Attachments" / "777" / "2"
    ext.mkdir(parents=True)
    (ext / "invite.ics").write_text(_ics(now + timedelta(days=45)))
    cfg = _cfg(tmp_path, 90)
    run_mail_export(cfg, mail_library=tmp_store["library"])
    [rec] = _index(cfg).values()
    assert [i["summary"] for i in rec["invites"]] == ["Far-off meeting"]
