import json
import subprocess

from mailexport.config import Config
from mailexport.lock import try_lock
from mailexport import worker


class _Proc:
    def __init__(self, code):
        self.returncode, self.stdout, self.stderr = code, "", ""


def _cfg(tmp_path):
    cfg = Config()
    cfg.output_dir = tmp_path / "out"
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def test_open_settings_kickstarts_once_then_cools_down(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(worker.subprocess, "run",
                        lambda args, **kw: calls.append(args) or _Proc(0))
    cfg = _cfg(tmp_path)
    r = worker._handle(cfg, {"action": "open-settings"})
    assert r["ok"] and r["opened"] is True
    assert calls and calls[0][:2] == ["launchctl", "kickstart"]
    assert calls[0][2].endswith("/com.agentinbox.configui")
    assert "http" not in json.dumps(r) and "t=" not in json.dumps(r)   # no secret link
    r2 = worker._handle(cfg, {"action": "open-settings"})
    assert not r2["ok"] and "try again" in r2["error"]
    assert len(calls) == 1


def test_open_settings_already_open_disabled_and_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(worker.subprocess, "run", lambda args, **kw: _Proc(0))
    cfg = _cfg(tmp_path)
    with try_lock(cfg.state_dir / "configui.lock") as got:
        assert got
        r = worker._handle(cfg, {"action": "open-settings"})
        assert r["ok"] and r["opened"] is False and "already open" in r["note"]

    cfg.settings_page.agent_may_open = False
    r = worker._handle(cfg, {"action": "open-settings"})
    assert not r["ok"] and "turned off" in r["error"] and "config-ui" in r["error"]

    cfg.settings_page.agent_may_open = True
    monkeypatch.setattr(worker.subprocess, "run", lambda args, **kw: _Proc(113))
    r = worker._handle(cfg, {"action": "open-settings"})
    assert not r["ok"] and "isn't installed" in r["error"]


def test_launchd_started_page_exits_if_one_is_open(tmp_path):
    from mailexport.configui import run
    from mailexport.config import load_config
    p = tmp_path / "config.toml"
    p.write_text(f'output_dir = "{tmp_path}/out"\n')
    lock = load_config(p).state_dir / "configui.lock"
    with try_lock(lock) as got:
        assert got
        # Would block serving forever if it didn't bail out.
        assert run(p, open_browser=False, print_link=False) == 0


def test_settings_job_never_uses_the_launcher():
    from mailexport.cli import CONFIGUI_PLIST_TEMPLATE, _python_entry_point
    plist = CONFIGUI_PLIST_TEMPLATE.format(label="com.agentinbox.configui",
                                           agentinbox=_python_entry_point(), logdir="/x")
    assert "agentinbox-runner" not in plist
    assert "--no-print-link" in plist
    assert "<key>RunAtLoad</key><false/>" in plist
    assert "WatchPaths" not in plist and "StartInterval" not in plist


def test_page_toggle_round_trips():
    from mailexport.configui import build_config
    data, _ = build_config({"calendar": {}}, {"agent": {"may_open_settings": False}}, [])
    assert data["settings_page"]["agent_may_open"] is False
