"""Turn a parsed emlx into the normalized export record."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from zoneinfo import ZoneInfo

from .emlx import Emlx
from .invites import extract_invites

_ENCRYPTED_TYPES = {
    "multipart/encrypted",
    "application/pkcs7-mime",
    "application/x-pkcs7-mime",
}


def message_uid(account_email: str, message_id: str | None, fallback: str) -> str:
    """Filename-safe, collision-safe id: same Message-ID in two accounts differs;
    missing Message-ID falls back to the store-relative path."""
    basis = message_id.strip() if message_id and message_id.strip() else fallback
    return hashlib.sha256(f"{account_email}:{basis}".encode()).hexdigest()[:16]


def _addrs(msg: EmailMessage, header: str) -> list[str]:
    raw = msg.get_all(header)
    if not raw:
        return []
    out = []
    for name, addr in getaddresses([str(v) for v in raw]):
        if not addr:
            continue
        out.append(f"{name} <{addr}>" if name else addr)
    return out


def message_date(msg: EmailMessage, mtime: float) -> datetime:
    """Date header (spoofable but usually right), falling back to file mtime."""
    raw = msg.get("Date")
    if raw:
        try:
            dt = parsedate_to_datetime(str(raw))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            pass
    return datetime.fromtimestamp(mtime, tz=timezone.utc)


def is_encrypted(msg: EmailMessage) -> bool:
    return msg.get_content_type() in _ENCRYPTED_TYPES


def iter_attachment_parts(msg: EmailMessage):
    """Deterministic attachment enumeration; the ordinal is the fetch index."""
    for part in msg.walk():
        if part.is_multipart():
            continue
        fname = part.get_filename()
        cd = part.get_content_disposition()
        if cd == "attachment" or (fname and part.get_content_maintype() != "text"):
            yield part


def list_attachments(msg: EmailMessage) -> list[dict]:
    out = []
    for i, part in enumerate(iter_attachment_parts(msg)):
        size = None
        apple_len = part.get("X-Apple-Content-Length")
        if apple_len:
            try:
                size = int(str(apple_len).strip())
            except ValueError:
                pass
        if size is None:
            try:
                payload = part.get_payload(decode=True)
                size = len(payload) if payload else None
            except Exception:
                size = None
        out.append({
            "index": i,
            "filename": part.get_filename() or f"attachment-{i}",
            "mime": part.get_content_type(),
            "size": size,
        })
    return out


def extract_body(msg: EmailMessage, body_chars: int) -> str | None:
    from .htmltext import html_to_text

    if is_encrypted(msg):
        return None
    plain = None
    html = None
    for part in msg.walk():
        if part.is_multipart():
            continue
        if part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        try:
            if ctype == "text/plain" and plain is None:
                plain = part.get_content()
            elif ctype == "text/html" and html is None:
                html = part.get_content()
        except Exception:
            continue
    text = plain if plain is not None else (html_to_text(html) if html is not None else "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Trivial trailing-signature strip only ("-- " convention).
    idx = text.rfind("\n-- \n")
    if idx > 0 and len(text) - idx < 2500:
        text = text[:idx]

    text = text.strip()
    if len(text) > body_chars:
        text = text[:body_chars] + "\n\n[truncated by mailexport]"
    return text


def message_record(
    emlx: Emlx,
    *,
    account_display: str,
    account_email: str,
    mailbox: str,
    rel_path: str,
    mtime: float,
    tz: str,
    body_chars: int,
    attachments_dir=None,
) -> tuple[dict, str | None]:
    """Returns (record-without-body, body_text)."""
    msg = emlx.message
    message_id = str(msg.get("Message-ID") or "").strip() or None
    dt = message_date(msg, mtime)
    local = dt.astimezone(ZoneInfo(tz))
    refs_raw = str(msg.get("References") or "")
    encrypted = is_encrypted(msg)

    record = {
        "id": message_uid(account_email, message_id, rel_path),
        "message_id": message_id,
        "account": (f"{account_display} <{account_email}>"
                    if account_display and account_display != account_email
                    else account_email),
        "mailbox": mailbox,
        "date_utc": dt.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "date_local": local.isoformat(timespec="seconds"),
        "from": _addrs(msg, "From"),
        "to": _addrs(msg, "To"),
        "cc": _addrs(msg, "Cc"),
        "subject": str(msg.get("Subject") or ""),
        "in_reply_to": str(msg.get("In-Reply-To") or "").strip() or None,
        "references": refs_raw.split() if refs_raw.strip() else [],
        "flags": emlx.flags,
        "flag_color": emlx.flag_color,
        "attachments": list_attachments(msg),
        "truncated": emlx.truncated,
        "encrypted": encrypted,
        "invites": ([] if (emlx.truncated or encrypted)
                    else extract_invites(msg, tz, attachments_dir)),
    }
    body = None if (emlx.truncated or encrypted) else extract_body(msg, body_chars)
    return record, body
