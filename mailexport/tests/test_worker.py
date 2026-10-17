import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from conftest import build_emlx, plain_message
from mailexport.config import Config, MailConfig
from mailexport.runner import run_mail_export
from mailexport.worker import process_requests

ACCT_UUID = "11111111-2222-3333-4444-555555555555"


def _setup(tmp_store, tmp_path):
    cfg = Config()
    cfg.output_dir = tmp_path / "out"
    cfg.mail = MailConfig(accounts={ACCT_UUID: "Test <me@example.com>"})
    msg = plain_message(
        date=format_datetime(datetime.now(timezone.utc) - timedelta(days=1)))
    msg.add_attachment(b"PDFBYTES", maintype="application", subtype="pdf",
                       filename="doc.pdf")
    (tmp_store["messages"] / "1.emlx").write_bytes(build_emlx(msg))
    run_mail_export(cfg, mail_library=tmp_store["library"])
    state = json.loads((cfg.state_dir / "mail.json").read_text())
    return cfg, next(iter(state["messages"]))


def _request(cfg, name, payload):
    (cfg.output_dir / "requests").mkdir(parents=True, exist_ok=True)
    (cfg.output_dir / "requests" / name).write_text(json.dumps(payload))


def _result(cfg, name):
    return json.loads((cfg.output_dir / "results" / name).read_text())


def test_list_and_fetch_requests(tmp_store, tmp_path):
    cfg, msg_id = _setup(tmp_store, tmp_path)
    _request(cfg, "r1.json", {"action": "list", "message_id": msg_id})
    _request(cfg, "r2.json", {"action": "fetch", "message_id": msg_id, "index": 0})
    assert process_requests(cfg) == 2

    r1 = _result(cfg, "r1.json")
    assert r1["ok"] and r1["attachments"][0]["filename"] == "doc.pdf"
    r2 = _result(cfg, "r2.json")
    assert r2["ok"]
    from pathlib import Path
    assert Path(r2["path"]).read_bytes() == b"PDFBYTES"
    # Requests are consumed.
    assert not list((cfg.output_dir / "requests").glob("*.json"))


def test_bad_requests_get_error_results(tmp_store, tmp_path):
    cfg, msg_id = _setup(tmp_store, tmp_path)
    _request(cfg, "bad1.json", {"action": "fetch", "message_id": "../../etc"})
    (cfg.output_dir / "requests" / "bad2.json").write_text("{not json")
    _request(cfg, "bad3.json", {"action": "launch_missiles"})
    _request(cfg, "big.json", {"action": "list", "message_id": "x" * 5000})
    assert process_requests(cfg) == 4
    assert not _result(cfg, "bad1.json")["ok"]
    assert "bad JSON" in _result(cfg, "bad2.json")["error"]
    assert "unknown action" in _result(cfg, "bad3.json")["error"]
    assert "too large" in _result(cfg, "big.json")["error"]


def _stub_calexport(tmp_path, script_body):
    stub = tmp_path / "fake-calexport"
    stub.write_text("#!/bin/bash\n" + script_body)
    stub.chmod(0o755)
    return stub


def test_reminder_add_and_complete(tmp_store, tmp_path):
    cfg, _ = _setup(tmp_store, tmp_path)
    cfg.calexport_bin = _stub_calexport(
        tmp_path, 'if [ "$1" = "reminder-add" ]; then echo FAKE-ID-123; '
                  'elif [ "$1" = "reminder-complete" ]; then echo completed; fi\n')
    _request(cfg, "add.json", {"action": "reminder-add", "title": "Call dean",
                               "due": "2026-09-02", "notes": "re: committee"})
    _request(cfg, "done.json", {"action": "reminder-complete", "id": "FAKE-ID-123"})
    assert process_requests(cfg) == 2
    add = _result(cfg, "add.json")
    assert add["ok"] and add["id"] == "FAKE-ID-123"
    done = _result(cfg, "done.json")
    assert done["ok"] and done["result"] == "completed"


def test_reminder_validation_and_gating(tmp_store, tmp_path):
    cfg, _ = _setup(tmp_store, tmp_path)
    cfg.calexport_bin = _stub_calexport(tmp_path, "echo SHOULD-NOT-RUN\n")
    _request(cfg, "r1.json", {"action": "reminder-add", "title": ""})
    _request(cfg, "r2.json", {"action": "reminder-add", "title": "x",
                              "due": "tomorrow"})
    _request(cfg, "r3.json", {"action": "reminder-complete", "id": "a;rm -rf /"})
    process_requests(cfg)
    assert "title required" in _result(cfg, "r1.json")["error"]
    assert "bad due" in _result(cfg, "r2.json")["error"]
    assert "bad reminder id" in _result(cfg, "r3.json")["error"]

    cfg.reminders.allow_create = False
    cfg.reminders.allow_complete = False
    _request(cfg, "r4.json", {"action": "reminder-add", "title": "nope"})
    _request(cfg, "r5.json", {"action": "reminder-complete", "id": "FAKE-ID-123"})
    process_requests(cfg)
    assert "disabled in config" in _result(cfg, "r4.json")["error"]
    assert "disabled in config" in _result(cfg, "r5.json")["error"]


def test_worker_never_reads_outside_ids(tmp_store, tmp_path):
    cfg, _ = _setup(tmp_store, tmp_path)
    _request(cfg, "p.json", {"action": "fetch",
                             "message_id": "/etc/passwd", "index": 0})
    process_requests(cfg)
    assert "not a valid message id" in _result(cfg, "p.json")["error"]


def test_pin_preferences_and_propose_requests(tmp_store, tmp_path):
    from datetime import date as _date
    cfg, msg_id = _setup(tmp_store, tmp_path)
    soon = (_date.today() + timedelta(days=20)).isoformat()
    _request(cfg, "p1.json", {"action": "pin", "message_id": msg_id, "until": soon,
                              "note": "meeting in text"})
    _request(cfg, "p2.json", {"action": "pin", "message_id": "f" * 16, "until": soon})
    _request(cfg, "p3.json", {"action": "pin", "message_id": msg_id, "until": "2099-01-01"})
    _request(cfg, "s1.json", {"action": "set-preference", "key": "mail_days", "value": 120})
    _request(cfg, "s2.json", {"action": "set-preference", "key": "mailboxes",
                              "value": ["INBOX", "Archive"]})
    _request(cfg, "s3.json", {"action": "set-preference", "key": "calendar_link_format",
                              "value": ["ics", "google"]})
    _request(cfg, "s4.json", {"action": "set-preference", "key": "calexport_bin",
                              "value": "/tmp/evil"})
    _request(cfg, "s5.json", {"action": "preferences"})
    _request(cfg, "z1.json", {"action": "propose-event", "title": "Coffee",
                              "start": f"{(_date.today() + timedelta(days=9))}T09:30",
                              "source_message_id": msg_id})
    _request(cfg, "u1.json", {"action": "unpin", "message_id": msg_id})
    (cfg.output_dir / "requests" / "n1.json").write_text('["not", "an", "object"]')
    assert process_requests(cfg) == 11

    assert _result(cfg, "p1.json")["ok"] and _result(cfg, "p1.json")["until"] == soon
    assert "unknown message id" in _result(cfg, "p2.json")["error"]
    assert "between today" in _result(cfg, "p3.json")["error"]
    assert _result(cfg, "s1.json")["effective"]["value"] == 120
    assert "only be changed by the user" in _result(cfg, "s2.json")["error"]
    assert _result(cfg, "s3.json")["value"] == ["ics", "google"]
    assert "human-only" in _result(cfg, "s4.json")["error"]
    prefs = _result(cfg, "s5.json")
    assert prefs["settings"]["mail_days"]["source"] == "preference"
    assert "mailboxes" in prefs["human_only"]
    z = _result(cfg, "z1.json")   # processed after s3, so google link included
    assert z["ok"] and z["ics_path"] and z["google_url"].startswith("https://calendar.google")
    assert z["source"]["message_id"] == msg_id
    assert _result(cfg, "u1.json")["was_pinned"] is True
    assert "JSON object" in _result(cfg, "n1.json")["error"]
