import json

import pytest

from mailexport.config import load_config
from mailexport.preferences import PreferenceError, describe, set_preference, validate


def test_validate_bounds_and_types():
    assert validate("mail_days", 90) == 90
    assert validate("mail_days", "120") == 120
    for bad in (6, 366, True, 1.5, "lots"):
        with pytest.raises(PreferenceError):
            validate("mail_days", bad)
    assert validate("reminders_completed_days", 0) == 0
    with pytest.raises(PreferenceError):
        validate("reminders_completed_days", 31)
    assert validate("calendar_link_format", "Google, ics") == ["ics", "google"]
    assert validate("calendar_link_format", ["outlook", "outlook"]) == ["outlook"]
    for bad in ([], "icalendar", ["ics", "yahoo"]):
        with pytest.raises(PreferenceError):
            validate("calendar_link_format", bad)
    assert validate("onboarded", True) is True
    with pytest.raises(PreferenceError):
        validate("onboarded", "yes")


def test_mailboxes_and_security_settings_are_human_only():
    with pytest.raises(PreferenceError, match="only be changed by the user"):
        validate("mailboxes", ["INBOX", "Archive"])
    for key in ("calexport_bin", "output_dir", "allow_create", "exclude_accounts",
                "state_dir", "max_bytes"):
        with pytest.raises(PreferenceError, match="unknown or human-only"):
            validate(key, "x")


def _write_config(tmp_path, body=""):
    p = tmp_path / "config.toml"
    p.write_text(f'output_dir = "{tmp_path}/out"\n' + body)
    return p


def test_precedence_default_config_preference(tmp_path):
    cfgp = _write_config(tmp_path, "[mail]\ndays = 60\n")
    cfg = load_config(cfgp)
    assert cfg.mail.days == 60 and cfg.pref_sources["mail_days"] == "config"
    assert cfg.calendar.future_days == 365
    assert cfg.pref_sources["calendar_future_days"] == "default"

    set_preference(cfg, "mail_days", 120)
    set_preference(cfg, "calendar_link_format", ["ics", "google"])
    cfg2 = load_config(cfgp)
    assert cfg2.mail.days == 120 and cfg2.pref_sources["mail_days"] == "preference"
    assert cfg2.proposals.formats == ["ics", "google"]
    d = describe(cfg2)["settings"]
    assert d["mail_days"]["value"] == 120 and "changed_at" in d["mail_days"]

    set_preference(cfg2, "mail_days", None)          # clear -> back to config
    cfg3 = load_config(cfgp)
    assert cfg3.mail.days == 60 and cfg3.pref_sources["mail_days"] == "config"


def test_kill_switch_and_bad_stored_values(tmp_path):
    cfgp = _write_config(tmp_path, "[preferences]\nallow_agent_changes = false\n")
    cfg = load_config(cfgp)
    with pytest.raises(PreferenceError, match="disabled"):
        set_preference(cfg, "mail_days", 30)

    # A tampered/invalid stored value is ignored, not applied.
    cfg.preferences.path.parent.mkdir(parents=True, exist_ok=True)
    cfg.preferences.path.write_text(json.dumps(
        {"values": {"mail_days": 100000, "calexport_bin": "/tmp/evil"}}))
    cfg4 = load_config(cfgp)
    assert cfg4.mail.days == 90
    assert any("mail_days" in e for e in cfg4.pref_errors)
    assert cfg4.calexport_bin is None
