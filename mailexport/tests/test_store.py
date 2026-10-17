import pytest

from mailexport.store import (
    StoreError, find_store_root, partial_attachments_dir, resolve_mailboxes,
)


def test_find_highest_v(tmp_path):
    lib = tmp_path / "Mail"
    for name in ("V9", "V10", "V11", "MailData"):
        (lib / name).mkdir(parents=True)
    assert find_store_root(lib).name == "V11"


def test_no_store(tmp_path):
    (tmp_path / "Mail").mkdir()
    with pytest.raises(StoreError):
        find_store_root(tmp_path / "Mail")


def _mk(acct, *parts):
    d = acct.joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_resolve_gmail_and_exchange_names(tmp_path):
    acct = tmp_path / "acct"
    _mk(acct, "INBOX.mbox")
    _mk(acct, "[Gmail].mbox", "Sent Mail.mbox")
    _mk(acct, "Sent Items.mbox")   # Exchange-style
    _mk(acct, "Junk.mbox")
    found = resolve_mailboxes(acct, ["INBOX", "Sent"])
    assert len(found["INBOX"]) == 1
    names = sorted(p.name for p in found["Sent"])
    assert names == ["Sent Items.mbox", "Sent Mail.mbox"]
    assert "Junk" not in found


def test_partial_attachments_dir(tmp_path):
    p = tmp_path / "Data" / "0" / "Messages" / "12345.partial.emlx"
    d = partial_attachments_dir(p)
    assert d == tmp_path / "Data" / "0" / "Attachments" / "12345"
    p2 = tmp_path / "Data" / "0" / "Messages" / "99.emlx"
    assert partial_attachments_dir(p2).name == "99"
