import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from conftest import build_emlx, plain_message
from mailexport.config import Config, MailConfig
from mailexport.runner import prune_attachments, run_mail_export

ACCT_UUID = "11111111-2222-3333-4444-555555555555"


def _cfg(tmp_path):
    cfg = Config()
    cfg.output_dir = tmp_path / "out"
    cfg.mail = MailConfig(accounts={ACCT_UUID: "Test Account <me@example.com>"})
    return cfg


def _recent_date(days_ago=1):
    return format_datetime(datetime.now(timezone.utc) - timedelta(days=days_ago))


def _write_msg(store, name, msg, flags=1):
    p = store["messages"] / name
    p.write_bytes(build_emlx(msg, flags=flags))
    return p


def test_end_to_end_and_determinism(tmp_store, tmp_path):
    cfg = _cfg(tmp_path)
    _write_msg(tmp_store, "1.emlx",
               plain_message(subject="A", message_id="<a@x>", date=_recent_date(1)))
    _write_msg(tmp_store, "2.emlx",
               plain_message(subject="B", message_id="<b@x>", date=_recent_date(2)),
               flags=(1 << 0) | (1 << 4))

    s1 = run_mail_export(cfg, mail_library=tmp_store["library"])
    assert s1["count"] == 2 and s1["parsed"] == 2
    assert s1["store_version"] == "V10"
    assert not s1["errors"]

    index = (cfg.output_dir / "mail" / "index.jsonl").read_bytes()
    mds = sorted(p.name for p in (cfg.output_dir / "mail" / "messages").iterdir())
    assert len(mds) == 2

    # Byte-identical on a no-change second run; nothing reparsed.
    s2 = run_mail_export(cfg, mail_library=tmp_store["library"])
    assert s2["parsed"] == 0 and s2["kept"] == 2
    assert (cfg.output_dir / "mail" / "index.jsonl").read_bytes() == index

    # Flag status survives into the export.
    lines = [json.loads(l) for l in index.decode().splitlines()]
    flagged = [r for r in lines if r["subject"] == "B"][0]
    assert flagged["flags"]["flagged"] and flagged["flags"]["read"]


def test_deletion_removes_only_that_export(tmp_store, tmp_path):
    cfg = _cfg(tmp_path)
    p1 = _write_msg(tmp_store, "1.emlx",
                    plain_message(subject="A", message_id="<a@x>", date=_recent_date()))
    _write_msg(tmp_store, "2.emlx",
               plain_message(subject="B", message_id="<b@x>", date=_recent_date()))
    run_mail_export(cfg, mail_library=tmp_store["library"])
    before = sorted(p.name for p in (cfg.output_dir / "mail" / "messages").iterdir())

    p1.unlink()
    s = run_mail_export(cfg, mail_library=tmp_store["library"])
    assert s["count"] == 1 and s["removed"] == 1
    after = sorted(p.name for p in (cfg.output_dir / "mail" / "messages").iterdir())
    assert len(before) == 2 and len(after) == 1
    assert set(after) < set(before)


def test_window_and_deleted_flag(tmp_store, tmp_path):
    cfg = _cfg(tmp_path)
    _write_msg(tmp_store, "old.emlx",
               plain_message(subject="Old", message_id="<old@x>", date=_recent_date(120)))
    _write_msg(tmp_store, "del.emlx",
               plain_message(subject="Del", message_id="<del@x>", date=_recent_date(1)),
               flags=(1 << 1))  # deleted bit
    _write_msg(tmp_store, "new.emlx",
               plain_message(subject="New", message_id="<new@x>", date=_recent_date(1)))
    s = run_mail_export(cfg, mail_library=tmp_store["library"])
    assert s["count"] == 1
    index = (cfg.output_dir / "mail" / "index.jsonl").read_text()
    assert "New" in index and "Old" not in index and "Del" not in index


def test_oversized_headers_only(tmp_store, tmp_path):
    cfg = _cfg(tmp_path)
    cfg.mail.max_bytes = 10_000
    _write_msg(tmp_store, "big.emlx",
               plain_message(subject="Big", message_id="<big@x>",
                             date=_recent_date(), body="x" * 50_000))
    s = run_mail_export(cfg, mail_library=tmp_store["library"])
    assert s["count"] == 1
    rec = json.loads((cfg.output_dir / "mail" / "index.jsonl").read_text())
    assert rec["truncated"] is True


def test_prune_attachments_respects_retention(tmp_store, tmp_path):
    import os, time
    cfg = _cfg(tmp_path)
    _write_msg(tmp_store, "1.emlx",
               plain_message(subject="A", message_id="<a@x>", date=_recent_date()))
    run_mail_export(cfg, mail_library=tmp_store["library"])
    state = json.loads((cfg.state_dir / "mail.json").read_text())
    live_id = next(iter(state["messages"]))

    fresh = cfg.output_dir / "attachments" / live_id / "keep.pdf"
    fresh.parent.mkdir(parents=True)
    fresh.write_bytes(b"k")
    stale_dir = cfg.output_dir / "attachments" / "deadbeefdeadbeef"
    stale_dir.mkdir(parents=True)
    stale = stale_dir / "gone.pdf"
    stale.write_bytes(b"g")
    old = time.time() - 30 * 86400
    os.utime(stale, (old, old))

    removed = prune_attachments(cfg)
    assert removed == 1
    assert fresh.exists() and not stale.exists()
