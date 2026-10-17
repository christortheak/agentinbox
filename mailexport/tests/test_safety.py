"""Adversarial tests: the agent can write anything in its folder, including
symlinks and FIFOs. The exporter (which holds Full Disk Access) must never
follow them to read, write or delete anything outside the folder."""
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest

from conftest import build_emlx, plain_message
from mailexport import safefs
from mailexport.config import Config, MailConfig
from mailexport.output import ensure_layout, write_agent_readme_if_missing
from mailexport.runner import migrate_legacy_state, prune_outputs, run_mail_export
from mailexport.worker import process_requests

ACCT_UUID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _clear_repairs():
    safefs.REPAIRS.clear()
    yield
    safefs.REPAIRS.clear()


def _cfg(tmp_path):
    cfg = Config()
    cfg.output_dir = tmp_path / "out"
    cfg.mail = MailConfig(accounts={ACCT_UUID: "Test <me@example.com>"})
    ensure_layout(cfg.output_dir)
    return cfg


def _victim(tmp_path):
    """A folder outside the agent's area, with files a link might expose."""
    v = tmp_path / "victim"
    (v / "sub").mkdir(parents=True)
    (v / "keep.json").write_text('{"action": "refresh"}')
    (v / "sub" / "precious.txt").write_text("precious")
    old = time.time() - 30 * 86400
    for f in (v / "keep.json", v / "sub" / "precious.txt"):
        os.utime(f, (old, old))
    return v


def _intact(v):
    return ((v / "keep.json").read_text() == '{"action": "refresh"}'
            and (v / "sub" / "precious.txt").read_text() == "precious")


def _replace_with_link(path: Path, target: Path):
    if path.is_dir() and not path.is_symlink():
        import shutil
        shutil.rmtree(path)
    path.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("folder", ["attachments", "results", "requests", "proposals"])
def test_prune_never_follows_a_linked_folder(tmp_path, folder):
    cfg, v = _cfg(tmp_path), _victim(tmp_path)
    _replace_with_link(cfg.output_dir / folder, v)
    prune_outputs(cfg)
    assert _intact(v)


def test_linked_attachment_subfolder_removes_only_the_link(tmp_path):
    cfg, v = _cfg(tmp_path), _victim(tmp_path)
    (cfg.output_dir / "attachments" / ("a" * 16)).symlink_to(v / "sub", target_is_directory=True)
    prune_outputs(cfg)
    assert _intact(v)
    assert not (cfg.output_dir / "attachments" / ("a" * 16)).exists()


def test_layout_repair_replaces_links_with_real_folders(tmp_path):
    cfg, v = _cfg(tmp_path), _victim(tmp_path)
    _replace_with_link(cfg.output_dir / "results", v)
    _replace_with_link(cfg.output_dir / "mail", v)
    ensure_layout(cfg.output_dir)
    for sub in ("results", "mail", "mail/messages"):
        p = cfg.output_dir / sub
        assert p.is_dir() and not p.is_symlink()
    assert _intact(v) and not (v / "messages").exists()
    assert any("results" in r for r in safefs.REPAIRS)


def test_worker_never_processes_or_deletes_through_a_linked_requests_folder(tmp_path):
    cfg, v = _cfg(tmp_path), _victim(tmp_path)
    _replace_with_link(cfg.output_dir / "requests", v)
    assert process_requests(cfg) == 0
    assert _intact(v)
    assert not (cfg.output_dir / "requests").is_symlink()


def test_linked_or_fifo_request_files_are_refused(tmp_path):
    cfg, v = _cfg(tmp_path), _victim(tmp_path)
    (cfg.output_dir / "requests" / "link.json").symlink_to(v / "keep.json")
    os.mkfifo(cfg.output_dir / "requests" / "pipe.json")
    done = threading.Event()
    t = threading.Thread(target=lambda: (process_requests(cfg), done.set()), daemon=True)
    t.start()
    assert done.wait(10), "worker blocked on a FIFO"
    for name in ("link.json", "pipe.json"):
        r = json.loads((cfg.output_dir / "results" / name).read_text())
        assert not r["ok"] and "regular file" in r["error"]
        assert not os.path.lexists(cfg.output_dir / "requests" / name)
    assert _intact(v)


def test_results_written_through_a_linked_folder_land_in_a_real_one(tmp_path):
    cfg, v = _cfg(tmp_path), _victim(tmp_path)
    (cfg.output_dir / "requests" / "r.json").write_text('{"action": "nope"}')
    _replace_with_link(cfg.output_dir / "results", v)
    process_requests(cfg)          # repairs the layout first
    assert (cfg.output_dir / "results" / "r.json").is_file()
    assert not (v / "r.json").exists()


def test_fifo_readme_doesnt_hang_and_gets_replaced(tmp_path):
    cfg = _cfg(tmp_path)
    os.mkfifo(cfg.output_dir / "README.md")
    done = threading.Event()
    threading.Thread(target=lambda: (write_agent_readme_if_missing(cfg.output_dir), done.set()),
                     daemon=True).start()
    assert done.wait(10)
    assert (cfg.output_dir / "README.md").is_file()


def test_linked_messages_folder_doesnt_receive_exports(tmp_store, tmp_path):
    cfg, v = _cfg(tmp_path), _victim(tmp_path)
    _replace_with_link(cfg.output_dir / "mail" / "messages", v)
    (tmp_store["messages"] / "1.emlx").write_bytes(build_emlx(plain_message(
        date=format_datetime(datetime.now(timezone.utc) - timedelta(days=1)))))
    run_mail_export(cfg, mail_library=tmp_store["library"])
    assert list(v.glob("*.md")) == []
    assert len(list((cfg.output_dir / "mail" / "messages").glob("*.md"))) == 1


def test_legacy_state_link_is_removed_not_followed(tmp_path):
    cfg, v = _cfg(tmp_path), _victim(tmp_path)
    (v / "mail.json").write_text("{}")
    (cfg.output_dir / "state").symlink_to(v, target_is_directory=True)
    migrate_legacy_state(cfg.output_dir)
    assert (v / "mail.json").exists() and _intact(v)
    assert not os.path.lexists(cfg.output_dir / "state")


def test_calendar_publish_through_a_linked_folder(tmp_path):
    from mailexport.pipeline import publish_calexport
    cfg, v = _cfg(tmp_path), _victim(tmp_path)
    staged = cfg.state_dir / "calexport-out" / "calendar"
    staged.mkdir(parents=True)
    (staged / "events.json").write_text("[]")
    _replace_with_link(cfg.output_dir / "calendar", v)
    publish_calexport(cfg)
    assert not (v / "events.json").exists()
    assert (cfg.output_dir / "calendar" / "events.json").read_text() == "[]"


# ---- review findings beyond symlinks ----

def test_transient_read_failure_keeps_last_good_export(tmp_store, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    p = tmp_store["messages"] / "1.emlx"
    p.write_bytes(build_emlx(plain_message(
        date=format_datetime(datetime.now(timezone.utc) - timedelta(days=1)))))
    run_mail_export(cfg, mail_library=tmp_store["library"])
    [md] = list((cfg.output_dir / "mail" / "messages").glob("*.md"))
    later = time.time() + 5
    os.utime(p, (later, later))            # Mail touched it...

    import mailexport.runner as runner
    from mailexport.emlx import EmlxError

    def flaky(*a, **kw):
        raise EmlxError("short read while Mail was writing")
    monkeypatch.setattr(runner, "read_emlx", flaky)
    s = run_mail_export(cfg, mail_library=tmp_store["library"])  # ...and the read failed
    assert md.exists() and s["count"] == 1 and s["removed"] == 0
    assert any("parse failed" in e for e in s["errors"])


def test_old_messages_are_not_fully_parsed(tmp_store, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    (tmp_store["messages"] / "old.emlx").write_bytes(build_emlx(plain_message(
        message_id="<old@x>", date=format_datetime(datetime.now(timezone.utc) - timedelta(days=400)))))
    import mailexport.runner as runner
    calls = []
    real = runner.read_emlx
    monkeypatch.setattr(runner, "read_emlx",
                        lambda f, max_message_bytes=None: calls.append(max_message_bytes)
                        or real(f, max_message_bytes=max_message_bytes))
    run_mail_export(cfg, mail_library=tmp_store["library"])
    assert calls == [0]      # header-only look, no full parse


def test_bad_ids_never_build_paths(tmp_path):
    from mailexport.output import remove_message_md
    cfg, v = _cfg(tmp_path), _victim(tmp_path)
    (v / "x.md").write_text("keep")
    remove_message_md("../../victim/x", cfg.output_dir)
    assert (v / "x.md").read_text() == "keep"
