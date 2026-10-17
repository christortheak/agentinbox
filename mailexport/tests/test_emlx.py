from email.message import EmailMessage

from conftest import build_emlx, plain_message
from mailexport.emlx import read_emlx


def test_plain_message(tmp_path):
    p = tmp_path / "1.emlx"
    p.write_bytes(build_emlx(plain_message(), flags=(1 << 0) | (1 << 4)))
    e = read_emlx(p)
    assert e.message["Subject"] == "Hello"
    assert e.flags["read"] and e.flags["flagged"]
    assert not e.flags["answered"] and not e.flags["deleted"]
    assert not e.truncated


def test_flag_bits_answered_forwarded_junk(tmp_path):
    p = tmp_path / "1.emlx"
    bits = (1 << 2) | (1 << 8) | (1 << 24)
    p.write_bytes(build_emlx(plain_message(), flags=bits))
    e = read_emlx(p)
    assert e.flags["answered"] and e.flags["forwarded"] and e.flags["junk"]
    assert not e.flags["read"]


def test_flag_color(tmp_path):
    p = tmp_path / "1.emlx"
    p.write_bytes(build_emlx(plain_message(), flags=1 << 4, extra_plist={"color": 3}))
    assert read_emlx(p).flag_color == "green"
    p.write_bytes(build_emlx(plain_message(), flags=1 << 4))
    assert read_emlx(p).flag_color is None


def test_multipart(tmp_path):
    msg = plain_message(body="text part")
    msg.add_attachment(b"PDFDATA", maintype="application", subtype="pdf",
                       filename="report.pdf")
    p = tmp_path / "2.emlx"
    p.write_bytes(build_emlx(msg))
    e = read_emlx(p)
    assert e.message.is_multipart()


def test_html_only(tmp_path):
    msg = EmailMessage()
    msg["From"] = "x@example.com"
    msg["Subject"] = "h"
    msg.set_content("<p>Hi <b>there</b></p><img src='http://t.co/pix'>",
                    subtype="html")
    p = tmp_path / "3.emlx"
    p.write_bytes(build_emlx(msg))
    e = read_emlx(p)
    assert e.message.get_content_type() == "text/html"


def test_oversized_headers_only(tmp_path):
    msg = plain_message(body="x" * 500_000)
    p = tmp_path / "4.emlx"
    p.write_bytes(build_emlx(msg, flags=1))
    e = read_emlx(p, max_message_bytes=100_000)
    assert e.truncated
    assert e.message["Subject"] == "Hello"
    assert e.flags["read"]  # plist still parsed after seeking past the body


def test_partial_emlx_parses(tmp_path):
    p = tmp_path / "5.partial.emlx"
    p.write_bytes(build_emlx(plain_message()))
    e = read_emlx(p)
    assert e.message["Subject"] == "Hello"


def test_bad_count_line(tmp_path):
    p = tmp_path / "6.emlx"
    p.write_bytes(b"notanumber\nhello")
    import pytest
    from mailexport.emlx import EmlxError
    with pytest.raises(EmlxError):
        read_emlx(p)
