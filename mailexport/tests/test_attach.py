import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from conftest import build_emlx, plain_message
from mailexport.attach import AttachError, fetch_attachment, list_message_attachments
from mailexport.config import Config, MailConfig
from mailexport.runner import run_mail_export

ACCT_UUID = "11111111-2222-3333-4444-555555555555"


def _setup(tmp_store, tmp_path, msg, name="1.emlx"):
    cfg = Config()
    cfg.output_dir = tmp_path / "out"
    cfg.mail = MailConfig(accounts={ACCT_UUID: "Test <me@example.com>"})
    (tmp_store["messages"] / name).write_bytes(build_emlx(msg))
    run_mail_export(cfg, mail_library=tmp_store["library"])
    state = json.loads((cfg.state_dir / "mail.json").read_text())
    return cfg, next(iter(state["messages"]))


def _msg_with_attachment(data=b"%PDF-1.4 fake", filename="report.pdf"):
    msg = plain_message(date=format_datetime(datetime.now(timezone.utc) - timedelta(days=1)))
    msg.add_attachment(data, maintype="application", subtype="pdf", filename=filename)
    return msg


def test_list_and_fetch_by_index(tmp_store, tmp_path):
    cfg, msg_id = _setup(tmp_store, tmp_path, _msg_with_attachment())
    atts = list_message_attachments(cfg, msg_id)
    assert atts[0]["filename"] == "report.pdf"
    dest = fetch_attachment(cfg, msg_id, index=0)
    assert dest == cfg.output_dir / "attachments" / msg_id / "report.pdf"
    assert dest.read_bytes() == b"%PDF-1.4 fake"
    assert (dest.stat().st_mode & 0o777) == 0o600


def test_fetch_by_filename_and_sanitization(tmp_store, tmp_path):
    cfg, msg_id = _setup(
        tmp_store, tmp_path, _msg_with_attachment(filename="../../evil.pdf"))
    dest = fetch_attachment(cfg, msg_id, filename="../../evil.pdf")
    assert dest.parent == cfg.output_dir / "attachments" / msg_id
    assert "/" not in dest.name and not dest.name.startswith(".")


def test_size_cap(tmp_store, tmp_path):
    cfg, msg_id = _setup(tmp_store, tmp_path, _msg_with_attachment(data=b"X" * 2000))
    cfg.attachments.max_bytes = 100
    with pytest.raises(AttachError, match="over the .* cap|over the cap"):
        fetch_attachment(cfg, msg_id, index=0)


def test_rejects_paths_and_unknown_ids(tmp_store, tmp_path):
    cfg, msg_id = _setup(tmp_store, tmp_path, _msg_with_attachment())
    with pytest.raises(AttachError, match="not a valid message id"):
        fetch_attachment(cfg, "../../etc/passwd", index=0)
    with pytest.raises(AttachError, match="unknown message id"):
        fetch_attachment(cfg, "0" * 16, index=0)


def test_partial_external_attachment(tmp_store, tmp_path):
    # Message whose attachment part has no payload; bytes live in Attachments/.
    msg = _msg_with_attachment(data=b"", filename="big.pdf")
    for part in msg.walk():
        if part.get_filename() == "big.pdf":
            part.set_payload("")
            part["X-Apple-Content-Length"] = "9"
    cfg, msg_id = _setup(tmp_store, tmp_path, msg, name="777.partial.emlx")
    ext = tmp_store["messages"].parent / "Attachments" / "777" / "2"
    ext.mkdir(parents=True)
    (ext / "big.pdf").write_bytes(b"REALBYTES")
    dest = fetch_attachment(cfg, msg_id, index=0)
    assert dest.read_bytes() == b"REALBYTES"
