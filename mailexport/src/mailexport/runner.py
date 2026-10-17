"""The mail export run: scan, incremental parse, write, prune."""
from __future__ import annotations

import json
import logging
import os
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .accounts import Account, discover_accounts, infer_account_from_messages
from . import safefs
from .config import Config
from .emlx import EmlxError, read_emlx
from .invites import invite_relevant_until
from .normalize import message_date, message_record
from .output import (
    atomic_write, ensure_layout, remove_message_md,
    write_agent_readme_if_missing, write_index, write_message_md,
)
from .pins import load_pins
from .store import (account_dirs, find_store_root, iter_message_files,
                    partial_attachments_dir, resolve_mailboxes)

log = logging.getLogger("mailexport")

# v3: records carry `invites`; state moved out of the agent's folder.
# v4: invites also read from .partial.emlx external attachment folders.
STATE_VERSION = 4


def state_path(cfg: Config) -> Path:
    return cfg.state_dir / "mail.json"


def load_state(cfg: Config) -> dict:
    try:
        with open(state_path(cfg)) as f:
            state = json.load(f)
        if state.get("version") == STATE_VERSION:
            return state
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"version": STATE_VERSION, "messages": {}}


def migrate_legacy_state(output_dir: Path) -> None:
    """Older versions kept bookkeeping in <output>/state, inside the agent's
    folder. Remove it; the new state dir is authoritative. Symlink-safe: a
    planted link is removed, its target never touched."""
    with safefs.open_dir(output_dir, "", create=False) as root:
        if root is None:
            return
        try:
            st = os.stat("state", dir_fd=root, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(st.st_mode):
            safefs.unlink_at(root, "state")
            return
        with safefs.open_dir(output_dir, "state", create=False) as legacy:
            if legacy is None:
                return
            for name in ("mail.json", "calendar-meta.json", "run.lock"):
                safefs.unlink_at(legacy, name)
        try:
            os.rmdir("state", dir_fd=root)
        except OSError:
            pass  # something else in there; leave it


def retention_reason(rec: dict, cutoff_iso: str, pins: dict, now: datetime) -> str | None:
    """Why a message stays exported: inside the window, pinned by the agent,
    or tied to a meeting that hasn't happened yet."""
    if rec["date_utc"] >= cutoff_iso:
        return "window"
    if rec["id"] in pins:
        return "pinned"
    until = invite_relevant_until(rec)
    if until is not None and until >= now:
        return "meeting"
    return None


def run_mail_export(cfg: Config, mail_library: Path | None = None) -> dict:
    """Returns a summary dict for the manifest. Raises StoreError when the
    store is unreadable (missing FDA) - callers must not touch existing output
    in that case."""
    started = time.time()
    errors: list[str] = []

    store_root = find_store_root(mail_library) if mail_library else find_store_root()
    accounts = discover_accounts(store_root, cfg.mail.accounts)

    ensure_layout(cfg.output_dir)
    cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    migrate_legacy_state(cfg.output_dir)
    write_agent_readme_if_missing(cfg.output_dir)

    state = load_state(cfg)
    old_by_src: dict[str, dict] = {m["src"]: m for m in state["messages"].values()}

    # Previously inferred accounts, so empty mailboxes aren't re-walked each run.
    for uuid, email in state.get("inferred_accounts", {}).items():
        if uuid not in accounts:
            accounts[uuid] = Account(uuid=uuid, display=email, email=email,
                                     discovered=False)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=cfg.mail.days)
    cutoff_iso = cutoff.isoformat(timespec="seconds")
    pins = load_pins(cfg)
    new_messages: dict[str, dict] = {}
    reasons: dict[str, str] = {}
    by_account: dict[str, int] = {}
    newest_mtime = 0.0
    parsed = kept = 0

    def keep(entry: dict, reason: str) -> None:
        rec = entry["record"]
        new_messages[rec["id"]] = entry
        reasons[rec["id"]] = reason
        by_account[rec["account"]] = by_account.get(rec["account"], 0) + 1

    for acct_dir in account_dirs(store_root):
        acct = accounts.get(acct_dir.name)
        if acct is None:
            acct = infer_account_from_messages(acct_dir)
            if acct is None:
                errors.append(
                    f"account dir {acct_dir.name} has no name/email mapping and "
                    "inference from message headers failed; add it to "
                    "[mail.accounts] in config.toml"
                )
                continue
            accounts[acct_dir.name] = acct
            log.info("inferred account %s = %s from message headers "
                     "(override in [mail.accounts] if wrong)",
                     acct_dir.name, acct.email)
        if acct.email.lower() in cfg.mail.exclude_accounts:
            continue
        resolved = resolve_mailboxes(acct_dir, cfg.mail.mailboxes)
        for canonical, mbox_dirs in resolved.items():
            for mbox_dir in mbox_dirs:
                for f in iter_message_files(mbox_dir):
                    try:
                        st = f.stat()
                    except FileNotFoundError:
                        continue  # deleted mid-run; next run reconciles
                    newest_mtime = max(newest_mtime, st.st_mtime)
                    src = str(f)
                    prev = old_by_src.get(src)
                    if prev is not None and prev["mtime"] == st.st_mtime:
                        # Unchanged: reuse the stored record, re-check retention.
                        reason = retention_reason(prev["record"], cutoff_iso, pins, now)
                        if reason and prev["record"]["id"] not in new_messages:
                            keep(prev, reason)
                            kept += 1
                        continue
                    def keep_last_good() -> None:
                        # A read can fail while Mail is mid-write. Keep the last
                        # good export rather than treating it as deleted.
                        nonlocal kept
                        if prev is not None and prev["record"]["id"] not in new_messages:
                            reason = retention_reason(prev["record"], cutoff_iso, pins, now)
                            if reason:
                                keep(prev, reason)
                                kept += 1

                    if prev is None:
                        # Cheap header-only look first: messages outside the
                        # window are skipped without parsing their bodies.
                        try:
                            head = read_emlx(f, max_message_bytes=0)
                        except (EmlxError, OSError) as e:
                            errors.append(f"parse failed: {f.name}: {e}")
                            continue
                        if head.flags["deleted"]:
                            continue
                        # Old messages never exported before are not resurrected;
                        # retention beyond the window applies to known messages.
                        if message_date(head.message, st.st_mtime) < cutoff:
                            continue
                    try:
                        emlx = read_emlx(f, max_message_bytes=cfg.mail.max_bytes)
                    except (EmlxError, OSError) as e:
                        errors.append(f"parse failed: {f.name}: {e}")
                        keep_last_good()
                        continue
                    if emlx.flags["deleted"]:
                        continue
                    try:
                        rec, body = message_record(
                            emlx,
                            account_display=acct.display,
                            account_email=acct.email,
                            mailbox=canonical,
                            rel_path=str(f.relative_to(store_root)),
                            mtime=st.st_mtime,
                            tz=cfg.timezone,
                            body_chars=cfg.mail.body_chars,
                            attachments_dir=(partial_attachments_dir(f)
                                             if f.name.endswith(".partial.emlx") else None),
                        )
                    except Exception as e:
                        errors.append(f"normalize failed: {f.name}: {e}")
                        keep_last_good()
                        continue
                    if rec["id"] in new_messages:
                        continue  # same message seen twice (duplicate delivery)
                    reason = retention_reason(rec, cutoff_iso, pins, now)
                    if reason is None:
                        continue
                    write_message_md(rec, body, cfg.output_dir)
                    keep({"src": src, "mtime": st.st_mtime, "record": rec}, reason)
                    parsed += 1

    # Remove exports whose source vanished or aged out.
    removed = 0
    for msg_id in set(state["messages"]) - set(new_messages):
        remove_message_md(msg_id, cfg.output_dir)
        removed += 1

    write_index([dict(m["record"], retained=reasons[i])
                 for i, m in new_messages.items()], cfg.output_dir)
    inferred = {a.uuid: a.email for a in accounts.values() if not a.discovered}
    state = {"version": STATE_VERSION, "messages": new_messages,
             "inferred_accounts": inferred}
    atomic_write(state_path(cfg), (json.dumps(state, sort_keys=True) + "\n").encode())

    beyond = sum(1 for r in reasons.values() if r != "window")
    return {
        "count": len(new_messages),
        "parsed": parsed,
        "kept": kept,
        "removed": removed,
        "retained_beyond_window": beyond,
        "by_account": dict(sorted(by_account.items())),
        "newest_source_mtime": (
            datetime.fromtimestamp(newest_mtime, tz=timezone.utc).isoformat(timespec="seconds")
            if newest_mtime else None
        ),
        "store_version": store_root.name,
        "duration_seconds": round(time.time() - started, 1),
        "errors": errors,
    }


def prune_outputs(cfg: Config) -> int:
    """Delete fetched attachments past retention or whose message aged out,
    stale request/result files, and proposals for events already past.

    Runs with Full Disk Access over a folder the agent can write, so it works
    only through directory descriptors opened without following symlinks: a
    folder replaced by a link to somewhere else is never traversed, and a
    planted link is removed as a link, its target untouched."""
    out = cfg.output_dir
    removed = 0
    now = time.time()

    with safefs.open_dir(out, "attachments", create=False) as afd:
        if afd is not None:
            live_ids = set(load_state(cfg)["messages"])
            horizon = now - cfg.attachments.retention_days * 86400
            for e in safefs.entries(afd):
                if not e.is_dir(follow_symlinks=False):
                    removed += safefs.unlink_at(afd, e.name)   # links/files don't belong here
                    continue
                with safefs.open_dir(out, Path("attachments") / e.name, create=False) as mfd:
                    if mfd is None:
                        continue
                    for f in safefs.entries(mfd):
                        if f.is_dir(follow_symlinks=False):
                            continue
                        old = f.stat(follow_symlinks=False).st_mtime < horizon
                        if e.name not in live_ids or old:
                            removed += safefs.unlink_at(mfd, f.name)
                try:
                    os.rmdir(e.name, dir_fd=afd)
                except OSError:
                    pass  # not empty

    stale = now - 2 * 86400
    for sub in ("requests", "results"):
        with safefs.open_dir(out, sub, create=False) as dfd:
            if dfd is None:
                continue
            for e in safefs.entries(dfd):
                if e.is_dir(follow_symlinks=False):
                    continue
                if e.stat(follow_symlinks=False).st_mtime < stale:
                    removed += safefs.unlink_at(dfd, e.name)

    # Proposal filenames start with the event's local date.
    yesterday = (datetime.now(ZoneInfo(cfg.timezone)).date() - timedelta(days=1)).isoformat()
    with safefs.open_dir(out, "proposals", create=False) as pfd:
        if pfd is not None:
            for e in safefs.entries(pfd):
                if (not e.is_dir(follow_symlinks=False) and e.name.endswith(".ics")
                        and e.name[:10] < yesterday):
                    removed += safefs.unlink_at(pfd, e.name)
    return removed


# Back-compat name used by older callers/tests.
prune_attachments = prune_outputs
