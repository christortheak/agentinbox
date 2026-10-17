"""Locate the Mail.app on-disk store, resolve mailboxes, iterate messages."""
from __future__ import annotations

import plistlib
import re
from pathlib import Path

MAIL_LIBRARY = Path("~/Library/Mail").expanduser()

# Canonical config name -> normalized on-disk names that mean the same thing.
MAILBOX_ALIASES: dict[str, set[str]] = {
    "INBOX": {"inbox"},
    "Sent": {"sent", "sent messages", "sent items", "sent mail", "[gmail]/sent mail"},
    "Archive": {"archive"},
    "Drafts": {"drafts", "[gmail]/drafts"},
    "Junk": {"junk", "spam", "junk e-mail", "[gmail]/spam"},
    "Trash": {"trash", "deleted messages", "deleted items", "[gmail]/trash", "[gmail]/bin"},
}

_UUID_RE = re.compile(r"^[0-9A-Fa-f-]{32,40}$")


class StoreError(Exception):
    pass


def find_store_root(mail_library: Path = MAIL_LIBRARY) -> Path:
    """Highest-numbered ~/Library/Mail/V* directory. Never hardcode V10."""
    try:
        candidates = [
            p for p in mail_library.iterdir()
            if p.is_dir() and re.fullmatch(r"V\d+", p.name)
        ]
    except PermissionError as e:
        raise StoreError(
            f"Cannot read {mail_library}: Full Disk Access is missing for this "
            "python binary. Grant it in System Settings > Privacy & Security > "
            "Full Disk Access."
        ) from e
    except FileNotFoundError as e:
        raise StoreError(f"{mail_library} does not exist; is Mail.app set up?") from e
    if not candidates:
        raise StoreError(f"No V* store directory found under {mail_library}")
    return max(candidates, key=lambda p: int(p.name[1:]))


def account_dirs(store_root: Path) -> list[Path]:
    out = []
    for p in store_root.iterdir():
        if p.is_dir() and _UUID_RE.match(p.name) and any(p.glob("*.mbox")):
            out.append(p)
    return sorted(out)


def _mbox_display_name(mbox_dir: Path, account_dir: Path) -> str:
    """'[Gmail].mbox/Sent Mail.mbox' -> '[Gmail]/Sent Mail'; Info.plist wins."""
    info = mbox_dir / "Info.plist"
    if info.exists():
        try:
            with open(info, "rb") as f:
                data = plistlib.load(f)
            for key in ("MailboxName", "DisplayName"):
                v = data.get(key)
                if isinstance(v, str) and v:
                    return v
        except Exception:
            pass
    rel = mbox_dir.relative_to(account_dir)
    return "/".join(part.removesuffix(".mbox") for part in rel.parts)


def resolve_mailboxes(account_dir: Path, wanted: list[str]) -> dict[str, list[Path]]:
    """Map each requested canonical mailbox name to matching .mbox dirs."""
    found: dict[str, list[Path]] = {w: [] for w in wanted}
    for mbox in sorted(account_dir.rglob("*.mbox")):
        if not mbox.is_dir():
            continue
        name = _mbox_display_name(mbox, account_dir)
        norm = name.lower()
        for w in wanted:
            aliases = MAILBOX_ALIASES.get(w, set())
            if norm == w.lower() or norm in aliases:
                found[w].append(mbox)
    return found


def iter_message_files(mbox_dir: Path):
    """All .emlx/.partial.emlx under this mailbox, excluding nested child mailboxes."""
    for pattern in ("*.emlx",):
        for f in sorted(mbox_dir.rglob(pattern)):
            rel = f.relative_to(mbox_dir)
            if any(part.endswith(".mbox") for part in rel.parts[:-1]):
                continue
            if f.parent.name != "Messages":
                continue
            yield f


def partial_attachments_dir(emlx_path: Path) -> Path:
    """For .../Messages/12345.partial.emlx, external attachment files live at
    .../Attachments/12345/<part>/<filename>."""
    stem = emlx_path.name.removesuffix(".emlx").removesuffix(".partial")
    return emlx_path.parent.parent / "Attachments" / stem
