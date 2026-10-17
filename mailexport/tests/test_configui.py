import http.client
import json
import os
import threading
import tomllib

import pytest

from mailexport.config import load_config
from mailexport.configui import (ConfigUI, Invalid, build_config, calendar_use,
                                 current_state, render_toml, save)
from mailexport.preferences import reset_preference, set_preference

CALS = [
    {"title": "Work", "source": "iCloud", "type": "caldav"},
    {"title": "Work", "source": "Google", "type": "caldav"},
    {"title": "Partner", "source": "iCloud", "type": "caldav"},
    {"title": "Team Fixtures", "source": "iCloud", "type": "subscription"},
    {"title": "Calendar", "source": "Exchange", "type": "exchange"},
    {"title": "Calendar", "source": "iCloud", "type": "caldav"},
]

RAW = {
    "output_dir": "/tmp/agentinbox-test-out",
    "calexport_bin": "/opt/agentinbox/calexport",
    "mail": {"days": 90, "mailboxes": ["INBOX", "Sent"], "exclude_accounts": [],
             "accounts": {"AAAAAAAA": "Local Outbox <local-outbox@mail.local>"}},
    "calendar": {"include": ["Work", "Partner"], "lookup": [{"title": "Calendar", "source": "Exchange"}],
                 "roles": {"Work": "mine", "Partner": "shared"}},
    "schedule": {"interval_seconds": 1800},
}

ROWS = [
    {"title": "Work", "source": "iCloud", "use": "home"},
    {"title": "Work", "source": "Google", "use": "off"},
    {"title": "Partner", "source": "iCloud", "use": "shared"},
    {"title": "Team Fixtures", "source": "iCloud", "use": "other"},
    {"title": "Calendar", "source": "Exchange", "use": "lookup"},
    {"title": "Calendar", "source": "iCloud", "use": "off"},
]


def test_calendar_use_reads_current_config():
    cal = RAW["calendar"]
    assert calendar_use(cal, "Work", "iCloud") == "home"
    assert calendar_use(cal, "Work", "Google") == "home"   # unqualified title = both
    assert calendar_use(cal, "Partner", "iCloud") == "shared"
    assert calendar_use(cal, "Calendar", "Exchange") == "lookup"
    assert calendar_use(cal, "Calendar", "iCloud") == "off"
    assert calendar_use(cal, "Team Fixtures", "iCloud") == "off"


def test_build_and_render_roundtrip(tmp_path):
    data, warnings = build_config(RAW, {"calendars": ROWS,
                                        "proposals": {"enabled": True, "formats": ["google", "ics"]},
                                        "mail": {"days": "120", "mailboxes": ["INBOX", "Sent", "Archive"]}},
                                  CALS)
    assert warnings == []
    cal = data["calendar"]
    assert cal["include"] == [{"title": "Work", "source": "iCloud"}, "Partner", "Team Fixtures"]
    assert cal["roles"] == {"Work": "mine", "Partner": "shared", "Team Fixtures": "other"}
    assert cal["lookup"] == [{"title": "Calendar", "source": "Exchange"}]
    assert data["proposals"]["formats"] == ["ics", "google"]

    text = render_toml(data)
    assert tomllib.loads(text)["mail"]["accounts"] == RAW["mail"]["accounts"]
    p = tmp_path / "config.toml"
    p.write_text(text)
    cfg = load_config(p)
    assert cfg.calendar.include == ["Work", "Partner", "Team Fixtures"]
    assert cfg.calendar.include_at == [("Work", "iCloud")]
    assert cfg.calendar.lookup == [("Calendar", "Exchange")]
    assert cfg.mail.days == 120 and cfg.mail.mailboxes == ["INBOX", "Sent", "Archive"]
    # Security-relevant settings are carried over untouched.
    assert str(cfg.output_dir) == RAW["output_dir"]
    assert str(cfg.calexport_bin) == RAW["calexport_bin"]
    assert cfg.interval_seconds == 1800
    assert not cfg.pref_errors


def test_validation_errors():
    bad = [
        {"calendars": ROWS[:2] + [{"title": "Work", "source": "Google", "use": "shared"}]},
        {"calendars": [{"title": "Nope", "source": "iCloud", "use": "home"}]},
        {"calendars": [dict(r, use="off") for r in ROWS]},
        {"calendars": [dict(ROWS[0], use="admin")]},
        {"mail": {"days": 0}},
        {"mail": {"mailboxes": []}},
        {"mail": {"mailboxes": ["INBOX", 7]}},
        {"proposals": {"formats": []}},
        {"proposals": {"formats": ["ics", "yahoo"]}},
        {"attachments": {"max_mb": 9000}},
        {"agent": {"allow_agent_changes": "yes"}},
        "not an object",
    ]
    for payload in bad:
        with pytest.raises(Invalid):
            build_config(RAW, payload, CALS)
    # Payload can't smuggle in security settings: unknown keys are ignored.
    data, _ = build_config(RAW, {"output_dir": "/Users/x/Library/Mobile Documents",
                                 "calexport_bin": "/tmp/evil", "state_dir": "/tmp"}, CALS)
    assert data["output_dir"] == RAW["output_dir"]
    assert data["calexport_bin"] == RAW["calexport_bin"] and "state_dir" not in data


def test_save_backups_and_permissions(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(render_toml(RAW))
    cfg = load_config(p)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    (cfg.state_dir / "calendar-meta.json").write_text(json.dumps({"all_calendars": CALS}))
    for i in range(7):
        r = save(p, {"mail": {"days": 30 + i}})
        assert r["ok"], r
    assert load_config(p).mail.days == 36
    assert (os.stat(p).st_mode & 0o777) == 0o600
    assert len(list(tmp_path.glob("config.toml.bak-*"))) <= 5
    r = save(p, {"mail": {"days": -1}})
    assert not r["ok"] and load_config(p).mail.days == 36   # unchanged on error


def test_state_and_reset_preference(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(render_toml(RAW))
    cfg = load_config(p)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    (cfg.state_dir / "calendar-meta.json").write_text(json.dumps({"all_calendars": CALS}))
    set_preference(cfg, "mail_days", 200)
    st = current_state(p)
    assert st["mail"]["days"] == 90                       # the user's own value
    assert st["agent"]["settings"]["mail_days"]["source"] == "preference"
    assert {r["title"]: r["use"] for r in st["calendars"] if r["source"] == "iCloud"}["Partner"] == "shared"
    reset_preference(load_config(p), "mail_days")
    assert load_config(p).mail.days == 90


# ---- the HTTP guardrails ----

@pytest.fixture
def server(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(render_toml(RAW))
    cfg = load_config(p)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    (cfg.state_dir / "calendar-meta.json").write_text(json.dumps({"all_calendars": CALS}))
    ui = ConfigUI(p)
    t = threading.Thread(target=ui.serve, daemon=True)
    t.start()
    yield ui, p
    ui.stop()
    t.join(timeout=5)


def _req(ui, method, path, *, host=None, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", ui.port, timeout=10)
    c.putrequest(method, path, skip_host=True)
    c.putheader("Host", host or f"127.0.0.1:{ui.port}")
    for k, v in (headers or {}).items():
        c.putheader(k, v)
    data = json.dumps(body).encode() if body is not None else b""
    if body is not None:
        c.putheader("Content-Length", str(len(data)))
    c.endheaders(data if body is not None else None)
    r = c.getresponse()
    out = (r.status, dict(r.getheaders()), r.read())
    c.close()
    return out


def test_server_guardrails(server):
    ui, p = server
    tok = {"X-AgentInbox-Token": ui.token}
    js = {"Content-Type": "application/json"}

    assert _req(ui, "GET", "/")[0] == 403                          # no secret
    assert _req(ui, "GET", "/?t=wrong")[0] == 403
    st, hdrs, body = _req(ui, "GET", f"/?t={ui.token}")
    assert st == 200 and b"AgentInbox settings" in body
    assert "frame-ancestors 'none'" in hdrs["Content-Security-Policy"]
    assert hdrs["Referrer-Policy"] == "no-referrer"
    # DNS rebinding: right secret, wrong Host.
    assert _req(ui, "GET", f"/?t={ui.token}", host=f"evil.example:{ui.port}")[0] == 403

    assert _req(ui, "GET", "/api/state")[0] == 403                 # no header
    st, _, body = _req(ui, "GET", "/api/state", headers=tok)
    assert st == 200 and json.loads(body)["calendars"]

    # Cross-site writes: foreign Origin, or a "simple" text/plain request.
    assert _req(ui, "POST", "/api/save", headers={**tok, **js, "Origin": "https://evil.example"},
                body={"mail": {"days": 30}})[0] == 403
    assert _req(ui, "POST", "/api/save", headers={**tok, "Content-Type": "text/plain"},
                body={"mail": {"days": 30}})[0] == 415
    assert _req(ui, "POST", "/api/save", headers=js, body={"mail": {"days": 30}})[0] == 403
    assert load_config(p).mail.days == 90                          # nothing changed

    st, _, body = _req(ui, "POST", "/api/save",
                       headers={**tok, **js, "Origin": f"http://127.0.0.1:{ui.port}"},
                       body={"mail": {"days": 45}})
    assert st == 200 and json.loads(body)["ok"]
    assert load_config(p).mail.days == 45

    assert _req(ui, "POST", "/api/done", headers={**tok, **js}, body={})[0] == 200


def test_same_name_same_account_is_one_row(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(render_toml(RAW))
    cfg = load_config(p)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cals = CALS + [{"title": "Family", "source": "iCloud", "type": "caldav"}] * 2
    (cfg.state_dir / "calendar-meta.json").write_text(json.dumps({"all_calendars": cals}))
    rows = [r for r in current_state(p)["calendars"] if r["title"] == "Family"]
    assert len(rows) == 1 and rows[0]["count"] == 2 and rows[0]["title_repeats"] is False
