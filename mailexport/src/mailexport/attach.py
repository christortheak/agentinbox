"""On-demand attachment fetch (`agentinbox attachment`).

Accepts only exported message ids - never filesystem paths - so the agent
cannot traverse into the Mail store. Writes only under <output>/attachments/.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .config import Config
from .emlx import read_emlx
from .normalize import iter_attachment_parts, list_attachments
from .store import partial_attachments_dir

log = logging.getLogger("attachfetch")

_ID_RE = re.compile(r"^[0-9a-f]{16}$")


class AttachError(Exception):
    pass


def _in_mail_store(src: Path) -> bool:
    """Defense in depth: the source must be an .emlx inside a Mail store
    (.../Mail/V<n>/...), whatever the id map says."""
    try:
        resolved = src.resolve()
    except OSError:
        return False
    if not resolved.name.endswith(".emlx"):
        return False
    return any(re.fullmatch(r"V\d+", p.name) and p.parent.name == "Mail"
               for p in resolved.parents)


def _resolve_source(cfg: Config, msg_id: str) -> Path:
    if not _ID_RE.match(msg_id):
        raise AttachError(f"not a valid message id: {msg_id!r}")
    from .runner import load_state
    state = load_state(cfg)
    if not state.get("messages"):
        raise AttachError("no export state found; the export has not run yet")
    entry = state["messages"].get(msg_id)
    if entry is None:
        raise AttachError(f"unknown message id {msg_id} (aged out or never exported)")
    src = Path(entry["src"])
    if not _in_mail_store(src):
        raise AttachError(f"refusing source outside the Mail store for {msg_id}")
    if not src.exists():
        raise AttachError(f"source message no longer exists in the Mail store: {msg_id}")
    return src


def _sanitize(filename: str) -> str:
    name = filename.replace("/", "_").replace("\\", "_").replace("\x00", "")
    name = name.strip().lstrip(".")
    return name or "attachment"


def list_message_attachments(cfg: Config, msg_id: str) -> list[dict]:
    src = _resolve_source(cfg, msg_id)
    emlx = read_emlx(src)
    return list_attachments(emlx.message)


def fetch_attachment(
    cfg: Config, msg_id: str, *, index: int | None = None, filename: str | None = None
) -> Path:
    if index is None and filename is None:
        raise AttachError("specify --index or --filename (or --list)")
    src = _resolve_source(cfg, msg_id)
    emlx = read_emlx(src)
    parts = list(iter_attachment_parts(emlx.message))
    meta = list_attachments(emlx.message)

    chosen = None
    if index is not None:
        if not (0 <= index < len(parts)):
            raise AttachError(f"index {index} out of range; message has {len(parts)} attachment(s)")
        chosen = index
    else:
        for i, m in enumerate(meta):
            if m["filename"] == filename:
                chosen = i
                break
        if chosen is None:
            raise AttachError(f"no attachment named {filename!r}; use --list")

    part = parts[chosen]
    out_name = _sanitize(meta[chosen]["filename"])
    declared = meta[chosen]["size"]
    if declared is not None and declared > cfg.attachments.max_bytes:
        raise AttachError(
            f"attachment is {declared} bytes, over the {cfg.attachments.max_bytes} byte cap"
        )

    payload = None
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        payload = None

    if not payload:
        # .partial.emlx: bytes live in the store's Attachments/<msgnum>/<part>/ dir.
        ext_dir = partial_attachments_dir(src)
        candidate = None
        if ext_dir.is_dir():
            matches = [p for p in sorted(ext_dir.rglob("*")) if p.is_file()]
            named = [p for p in matches if p.name == meta[chosen]["filename"]]
            candidate = named[0] if named else (matches[0] if len(matches) == 1 else None)
        if candidate is None:
            raise AttachError(
                "attachment bytes are not in the local store (Mail may not have "
                "downloaded it; open the message in Mail.app once)"
            )
        if candidate.stat().st_size > cfg.attachments.max_bytes:
            raise AttachError(
                f"attachment is {candidate.stat().st_size} bytes, over the cap"
            )
        payload = candidate.read_bytes()

    if len(payload) > cfg.attachments.max_bytes:
        raise AttachError(f"attachment is {len(payload)} bytes, over the cap")

    from . import safefs
    from .output import atomic_write
    safefs.register_root(cfg.output_dir)
    dest = cfg.output_dir / "attachments" / msg_id / out_name
    atomic_write(dest, payload, mode=0o600)   # symlink-safe, creates the folder
    log.info("fetched %s/%s (%d bytes)", msg_id, out_name, len(payload))
    return dest
