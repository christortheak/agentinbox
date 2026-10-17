"""Parse Apple Mail .emlx / .partial.emlx files.

Format: first line is a decimal byte count N; the next N bytes are an
RFC 5322 message; the remainder is an XML/binary plist with message flags.
"""
from __future__ import annotations

import plistlib
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesHeaderParser, BytesParser
from pathlib import Path

# emlx plist "flags" bitfield (reverse-engineered, widely documented).
FLAG_BITS = {
    "read": 1 << 0,
    "deleted": 1 << 1,
    "answered": 1 << 2,
    "encrypted": 1 << 3,
    "flagged": 1 << 4,
    "draft": 1 << 6,
    "forwarded": 1 << 8,
    "redirected": 1 << 9,
    "junk": 1 << 24,
}

# Best-effort: Mail's flag-color order as shown in its UI.
FLAG_COLORS = ["red", "orange", "yellow", "green", "blue", "purple", "gray"]

_HEADER_PROBE = 65536


class EmlxError(Exception):
    pass


@dataclass
class Emlx:
    message: EmailMessage
    plist: dict = field(default_factory=dict)
    truncated: bool = False  # True when only headers were parsed (oversized)

    @property
    def flags(self) -> dict[str, bool]:
        bits = int(self.plist.get("flags", 0))
        return {name: bool(bits & mask) for name, mask in FLAG_BITS.items()}

    @property
    def flag_color(self) -> str | None:
        c = self.plist.get("color")
        if isinstance(c, int) and 0 <= c < len(FLAG_COLORS):
            return FLAG_COLORS[c]
        return None


def read_emlx(path: Path, max_message_bytes: int | None = None) -> Emlx:
    with open(path, "rb") as f:
        first = f.readline()
        try:
            n = int(first.strip())
        except ValueError as e:
            raise EmlxError(f"{path}: bad byte-count line {first[:40]!r}") from e
        if max_message_bytes is not None and n > max_message_bytes:
            head = f.read(min(n, _HEADER_PROBE))
            msg = BytesHeaderParser(policy=policy.default).parsebytes(head)
            truncated = True
            f.seek(len(first) + n)
        else:
            raw = f.read(n)
            if len(raw) != n:
                raise EmlxError(f"{path}: short read ({len(raw)} of {n} bytes)")
            msg = BytesParser(policy=policy.default).parsebytes(raw)
            truncated = False
        rest = f.read()
    plist: dict = {}
    if rest.strip():
        try:
            plist = plistlib.loads(rest)
        except Exception:
            plist = {}  # flags are best-effort; the message itself parsed fine
    return Emlx(message=msg, plist=plist, truncated=truncated)
