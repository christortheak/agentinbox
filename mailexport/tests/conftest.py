import plistlib
import sys
from email.message import EmailMessage
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def build_emlx(msg: EmailMessage, flags: int = 1, extra_plist: dict | None = None) -> bytes:
    raw = msg.as_bytes()
    plist = {"flags": flags}
    if extra_plist:
        plist.update(extra_plist)
    return str(len(raw)).encode() + b"\n" + raw + plistlib.dumps(plist)


def plain_message(
    subject="Hello",
    body="Just checking in.\n",
    frm="Ann Example <ann@example.com>",
    to="me@example.com",
    message_id="<abc123@example.com>",
    date="Mon, 24 Aug 2026 09:30:00 -0400",
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = frm
    msg["To"] = to
    msg["Subject"] = subject
    if message_id:
        msg["Message-ID"] = message_id
    if date:
        msg["Date"] = date
    msg.set_content(body)
    return msg


@pytest.fixture(autouse=True)
def _isolate_user_paths(tmp_path, monkeypatch):
    """No test may touch the real ~/.agentinbox (state, preferences)."""
    import mailexport.config as config
    monkeypatch.setattr(config, "DEFAULT_STATE_DIR", tmp_path / "trusted-state")
    monkeypatch.setattr(config, "DEFAULT_PREFERENCES_PATH", tmp_path / "prefs.json")
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", tmp_path / "no-config.toml")


@pytest.fixture
def tmp_store(tmp_path):
    """Minimal fake Mail store: V10/<uuid>/INBOX.mbox/<uuid>/Data/0/Messages/."""
    root = tmp_path / "Mail" / "V10"
    acct = root / "11111111-2222-3333-4444-555555555555"
    messages = acct / "INBOX.mbox" / "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE" / "Data" / "0" / "Messages"
    messages.mkdir(parents=True)
    (root / "MailData").mkdir(parents=True)
    return {"library": tmp_path / "Mail", "root": root, "acct": acct, "messages": messages}
