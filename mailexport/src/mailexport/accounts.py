"""Account UUID -> (display name, email) discovery."""
from __future__ import annotations

import plistlib
import re
from dataclasses import dataclass
from pathlib import Path

_ADDR_RE = re.compile(r"<?([^\s<>]+@[^\s<>]+)>?")


@dataclass
class Account:
    uuid: str
    display: str
    email: str
    discovered: bool = True  # False when it came from the manual config table


def _emails_from_entry(entry: dict) -> list[str]:
    for key in ("EmailAddresses", "CanonicalEmailAddresses", "EmailAliases"):
        v = entry.get(key)
        if isinstance(v, list) and v:
            return [str(x) for x in v if isinstance(x, str)]
    v = entry.get("Username")
    if isinstance(v, str) and "@" in v:
        return [v]
    return []


def discover_accounts(store_root: Path, manual: dict[str, str]) -> dict[str, Account]:
    """Parse MailData/Accounts.plist; manual config entries override/extend.

    Manual entries: {"<AccountUUID>": "Display Name <email@example.com>"}.
    """
    accounts: dict[str, Account] = {}
    plist_path = store_root / "MailData" / "Accounts.plist"
    if plist_path.exists():
        try:
            with open(plist_path, "rb") as f:
                data = plistlib.load(f)
            entries = data.get("MailAccounts") or data.get("DeliveryAccounts") or []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                uuid = entry.get("uniqueId") or entry.get("UniqueId")
                if not uuid:
                    continue
                atype = str(entry.get("AccountType") or entry.get("Class") or "")
                if atype in ("LocalAccount", "OutboxAccount", "SMTPAccount"):
                    continue
                emails = _emails_from_entry(entry)
                if not emails:
                    continue
                name = str(entry.get("AccountName") or entry.get("DisplayName") or emails[0])
                accounts[str(uuid)] = Account(uuid=str(uuid), display=name, email=emails[0])
        except Exception:
            pass  # fall through to manual mapping

    for uuid, spec in manual.items():
        m = _ADDR_RE.search(spec)
        email = m.group(1) if m else spec
        display = spec.split("<")[0].strip() or email
        accounts[uuid] = Account(uuid=uuid, display=display, email=email, discovered=False)
    return accounts


def recipient_counts(acct_dir: Path, sample: int = 40) -> "Counter[str]":
    """Weighted recipient-address counts across a sample of an account dir's
    messages. Delivered-To / X-Original-To are strong signals; To/Cc weak."""
    from collections import Counter
    from email.utils import getaddresses

    from .emlx import read_emlx
    from .store import iter_message_files

    counts: Counter[str] = Counter()
    seen = 0
    for mbox in sorted(acct_dir.rglob("*.mbox")):
        if seen >= sample:
            break
        for f in iter_message_files(mbox):
            if seen >= sample:
                break
            try:
                # max_message_bytes=0 forces a cheap headers-only parse
                msg = read_emlx(f, max_message_bytes=0).message
            except Exception:
                continue
            seen += 1
            for header, weight in (("Delivered-To", 5), ("X-Original-To", 5),
                                   ("To", 1), ("Cc", 1)):
                raw = msg.get_all(header)
                if not raw:
                    continue
                for _, addr in getaddresses([str(v) for v in raw]):
                    if addr and "@" in addr:
                        counts[addr.lower()] += weight
    return counts


def infer_account_from_messages(acct_dir: Path, sample: int = 40) -> Account | None:
    """Fallback when Accounts.plist yields nothing: the account's own address
    is usually the most frequent recipient across its messages. Can misfire
    on shared/delegated mailboxes - a manual [mail.accounts] entry always
    overrides this."""
    counts = recipient_counts(acct_dir, sample)
    if not counts:
        return None
    email = counts.most_common(1)[0][0]
    return Account(uuid=acct_dir.name, display=email, email=email, discovered=False)
