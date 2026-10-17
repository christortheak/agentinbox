from email.message import EmailMessage

from conftest import build_emlx, plain_message
from mailexport.emlx import read_emlx
from mailexport.normalize import (
    extract_body, list_attachments, message_record, message_uid,
)


def _record(tmp_path, msg, flags=1, **kw):
    p = tmp_path / "m.emlx"
    p.write_bytes(build_emlx(msg, flags=flags))
    e = read_emlx(p)
    defaults = dict(
        account_display="Test", account_email="me@example.com",
        mailbox="INBOX", rel_path="acct/INBOX.mbox/x/1.emlx",
        mtime=1_700_000_000.0, tz="America/New_York", body_chars=20_000,
    )
    defaults.update(kw)
    return message_record(e, **defaults)


def test_uid_differs_by_account():
    a = message_uid("a@x.com", "<same@id>", "p")
    b = message_uid("b@x.com", "<same@id>", "p")
    assert a != b and len(a) == 16
    assert all(c in "0123456789abcdef" for c in a)


def test_uid_path_fallback():
    assert message_uid("a@x.com", None, "p1") != message_uid("a@x.com", None, "p2")
    assert message_uid("a@x.com", "  ", "p1") == message_uid("a@x.com", None, "p1")


def test_record_fields(tmp_path):
    rec, body = _record(tmp_path, plain_message(), flags=(1 << 0) | (1 << 2))
    assert rec["subject"] == "Hello"
    assert rec["from"] == ["Ann Example <ann@example.com>"]
    assert rec["flags"]["read"] and rec["flags"]["answered"]
    assert rec["date_utc"] == "2026-08-24T13:30:00+00:00"
    assert rec["date_local"].startswith("2026-08-24T09:30:00")
    assert rec["message_id"] == "<abc123@example.com>"
    assert body == "Just checking in."


def test_signature_strip(tmp_path):
    msg = plain_message(body="Real content here.\n\n-- \nAnn\nVP of Things\n")
    rec, body = _record(tmp_path, msg)
    assert body == "Real content here."


def test_body_cap(tmp_path):
    msg = plain_message(body="z" * 5000)
    rec, body = _record(tmp_path, msg, body_chars=100)
    assert body.startswith("z" * 100)
    assert "[truncated by mailexport]" in body


def test_encrypted(tmp_path):
    msg = EmailMessage()
    msg["From"] = "x@example.com"
    msg["Subject"] = "secret"
    msg["Content-Type"] = "application/pkcs7-mime; smime-type=enveloped-data"
    msg.set_payload("AAAA")
    rec, body = _record(tmp_path, msg)
    assert rec["encrypted"] is True
    assert body is None


def test_attachment_metadata(tmp_path):
    msg = plain_message()
    msg.add_attachment(b"PDF" * 100, maintype="application", subtype="pdf",
                       filename="q3 report.pdf")
    msg.add_attachment(b"IMG", maintype="image", subtype="png", filename="pic.png")
    rec, _ = _record(tmp_path, msg)
    atts = rec["attachments"]
    assert [a["index"] for a in atts] == [0, 1]
    assert atts[0]["filename"] == "q3 report.pdf"
    assert atts[0]["mime"] == "application/pdf"
    assert atts[0]["size"] == 300
    assert atts[1]["mime"] == "image/png"


def test_html_body_fallback(tmp_path):
    msg = EmailMessage()
    msg["From"] = "x@example.com"
    msg["Subject"] = "h"
    msg["Date"] = "Mon, 24 Aug 2026 09:30:00 -0400"
    msg.set_content("<p>Hi</p><img src='http://t.example/p.gif'>", subtype="html")
    rec, body = _record(tmp_path, msg)
    assert body == "Hi"
