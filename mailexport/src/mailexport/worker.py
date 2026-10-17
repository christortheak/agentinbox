"""Fetch worker: services JSON request files the agent drops into
<output>/requests/.

Why this exists: TCC attributes Full Disk Access to the *responsible process*.
A CLI spawned by the agent inherits responsibility from the agent's host app
(Terminal, Claude), which deliberately has no FDA - so agent-spawned fetches
would be denied. A launchd WatchPaths agent, by contrast, is its own
responsible process, so this worker runs with the venv python's own grants.
The agent needs no execute permission at all: write a request, read a result.

Request (one JSON object, < 4 KB, filename *.json):
    {"action": "list",    "message_id": "<id>"}
    {"action": "fetch",   "message_id": "<id>", "index": 0}
    {"action": "fetch",   "message_id": "<id>", "filename": "doc.pdf"}
    {"action": "refresh"}
    {"action": "mailboxes"}
    {"action": "propose-event", "title": "...", "start": "yyyy-MM-ddTHH:mm" | "yyyy-MM-dd",
     "end": ..., "location": "...", "notes": "...", "source_message_id": "<id>",
     "timezone": "<IANA, optional>"}
    {"action": "pin",   "message_id": "<id>", "until": "yyyy-MM-dd", "note": "..."}
    {"action": "unpin", "message_id": "<id>"}
    {"action": "reminder-add", "title": "...", "due": "yyyy-MM-dd[THH:mm]",
     "list": "...", "notes": "..."}                     # due/list/notes optional
    {"action": "reminder-complete", "id": "<reminder id from reminders.json>"}
    {"action": "preferences"}
    {"action": "set-preference", "key": "<allowlisted key>", "value": ... | null}
    {"action": "open-settings"}       # pops the settings page open on the Mac
Result: <output>/results/<same-basename>.json
    {"ok": true, ...} or {"ok": false, "error": "..."}

Writes outside the agent's own folder are limited to: reminder-add and
reminder-complete (Reminders.app; config-gated, provenance-stamped, no edit or
delete), pins and preferences (worker-owned files in ~/.agentinbox, validated
against fixed bounds). propose-event writes only a file in proposals/; nothing
reaches a calendar until the user opens it and chooses to add it. Mailboxes,
accounts, binaries and permissions are never agent-changeable.
"""
from __future__ import annotations

import json
import os
import logging
import re
import stat
import subprocess
from pathlib import Path

from .attach import AttachError, fetch_attachment, list_message_attachments
from .config import Config
from .lock import LockHeld
from .output import atomic_write

log = logging.getLogger("fetchworker")

MAX_REQUEST_BYTES = 4096
_REMINDER_ID_RE = re.compile(r"^[\w:\-./]{5,300}$")
_DUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$")
_MSG_ID_RE = re.compile(r"^[0-9a-f]{16}$")

HOW_TO_ADD_MAILBOX = (
    "Only the user can add mailboxes. Tell them: (1) open the config file in "
    "TextEdit with:  open -e ~/.config/agentinbox/config.toml  (2) in the [mail] "
    "section, add the mailbox's config_name to the mailboxes list, for example "
    "mailboxes = [\"INBOX\", \"Sent\", \"Archive\"]  - a name applies to every "
    "account that has a mailbox by that name, and standard names like Archive "
    "match each provider's variants; (3) save, then either wait for the next "
    "scheduled export (within 30 minutes) or run:  launchctl kickstart -k "
    "gui/$(id -u)/com.agentinbox.export  Adding Junk or Trash is possible but "
    "not recommended.")


def _calexport(cfg: Config, args: list[str]) -> tuple[int, str, str]:
    from .doctor import find_calexport
    binary = find_calexport(cfg)
    if binary is None:
        return 1, "", "calexport binary not found"
    try:
        r = subprocess.run([str(binary)] + args, capture_output=True, text=True,
                           timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _open_settings(cfg: Config) -> dict:
    """Ask launchd to start the settings page, which opens in the user's
    browser. The agent never learns the page's secret link and can't reach
    the page anyway (no network), so this hands settings to the user without
    giving the agent any way to change them. Rate-limited, one page at a time."""
    import time
    from .labels import CONFIGUI_LABEL
    from .lock import lock_is_held

    human_cmd = "~/.agentinbox/venv/bin/agentinbox config-ui"
    if not cfg.settings_page.agent_may_open:
        return {"ok": False, "error": "the user has turned off opening settings from "
                f"the agent; they can open it themselves with: {human_cmd}"}
    if lock_is_held(cfg.state_dir / "configui.lock"):
        return {"ok": True, "action": "open-settings", "opened": False,
                "note": "The settings page is already open on the user's Mac."}
    stamp = cfg.state_dir / "configui-opened.json"
    try:
        last = float(json.loads(stamp.read_text()).get("at", 0))
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        last = 0.0
    wait = int(cfg.settings_page.cooldown_seconds - (time.time() - last))
    if wait > 0:
        return {"ok": False, "error": f"the settings page was opened moments ago; "
                f"try again in {wait} seconds, or ask the user to check their browser"}
    r = subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{CONFIGUI_LABEL}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return {"ok": False, "error": "the settings page isn't installed; ask the user "
                "to run ~/.agentinbox/venv/bin/agentinbox install, or to open it "
                f"with: {human_cmd}"}
    atomic_write(stamp, json.dumps({"at": time.time()}).encode())
    log.info("settings page opened at the agent's request")
    return {"ok": True, "action": "open-settings", "opened": True,
            "note": "The settings page is opening in the user's browser on their Mac. "
                    "Only the user can use it; it closes after 15 idle minutes. If "
                    "the user isn't at their Mac, tell them to run: " + human_cmd}


def _handle(cfg: Config, req: dict) -> dict:
    action = req.get("action")
    if action == "list":
        atts = list_message_attachments(cfg, str(req.get("message_id", "")))
        return {"ok": True, "action": "list",
                "message_id": req.get("message_id"), "attachments": atts}
    if action == "fetch":
        index = req.get("index")
        filename = req.get("filename")
        dest = fetch_attachment(
            cfg, str(req.get("message_id", "")),
            index=int(index) if index is not None else None,
            filename=str(filename) if filename is not None else None,
        )
        return {"ok": True, "action": "fetch",
                "message_id": req.get("message_id"), "path": str(dest)}
    if action == "mailboxes":
        # Diagnostic: what the Mail store actually contains, per account dir -
        # mailbox names (exactly as config.toml needs them), whether each is
        # exported now, mapping, and top recipient addresses (no content).
        from .accounts import discover_accounts, recipient_counts
        from .runner import load_state
        from .store import (_mbox_display_name, account_dirs, find_store_root,
                            resolve_mailboxes)

        root = find_store_root()
        accounts = discover_accounts(root, cfg.mail.accounts)
        inferred = load_state(cfg).get("inferred_accounts", {})
        report = []
        for d in account_dirs(root):
            exported_dirs = {p for dirs in resolve_mailboxes(d, cfg.mail.mailboxes).values()
                             for p in dirs}
            boxes = {}
            for m in d.rglob("*.mbox"):
                if m.is_dir():
                    name = _mbox_display_name(m, d)
                    boxes[name] = boxes.get(name, False) or (m in exported_dirs)
            acct = accounts.get(d.name)
            mapped = acct.email if acct else inferred.get(d.name)
            source = ("config/plist" if acct else
                      "inferred" if d.name in inferred else None)
            top = recipient_counts(d, sample=60).most_common(5)
            report.append({
                "uuid": d.name,
                "mapped_as": mapped,
                "mapping_source": source,
                "excluded": bool(mapped and mapped.lower() in cfg.mail.exclude_accounts),
                "mailboxes": [{"config_name": n, "exported": e}
                              for n, e in sorted(boxes.items())],
                "top_recipients": [{"address": a, "weight": w} for a, w in top],
            })
        return {"ok": True, "action": "mailboxes", "store": str(root),
                "configured_mailboxes": list(cfg.mail.mailboxes),
                "accounts": report,
                "how_to_add": HOW_TO_ADD_MAILBOX}
    if action == "reminder-add":
        if not cfg.reminders.allow_create:
            return {"ok": False, "error": "reminder creation is disabled in config"}
        title = str(req.get("title", "")).strip()
        if not title or len(title) > 300:
            return {"ok": False, "error": "title required (1-300 chars)"}
        args = ["reminder-add", "--title", title]
        due = req.get("due")
        if due is not None:
            if not _DUE_RE.match(str(due)):
                return {"ok": False,
                        "error": "bad due (use yyyy-MM-dd or yyyy-MM-ddTHH:mm)"}
            args += ["--due", str(due)]
        lst = req.get("list")
        if lst is not None:
            if not str(lst).strip() or len(str(lst)) > 100:
                return {"ok": False, "error": "bad list name"}
            args += ["--list", str(lst)]
        notes = req.get("notes")
        if notes is not None:
            if len(str(notes)) > 2000:
                return {"ok": False, "error": "notes too long (max 2000 chars)"}
            args += ["--notes", str(notes)]
        code, out, err = _calexport(cfg, args)
        if code != 0:
            return {"ok": False, "error": err or out or "reminder-add failed"}
        log.info("reminder created: %r (id %s)", title, out)
        return {"ok": True, "action": "reminder-add", "id": out, "title": title}

    if action == "reminder-complete":
        if not cfg.reminders.allow_complete:
            return {"ok": False, "error": "reminder completion is disabled in config"}
        rid = str(req.get("id", ""))
        if not _REMINDER_ID_RE.match(rid):
            return {"ok": False, "error": "bad reminder id"}
        code, out, err = _calexport(cfg, ["reminder-complete", "--id", rid])
        if code != 0:
            return {"ok": False, "error": err or out or "reminder-complete failed"}
        log.info("reminder completed: %s (%s)", rid, out)
        return {"ok": True, "action": "reminder-complete", "id": rid, "result": out}

    if action == "propose-event":
        from .proposals import ProposalError, propose_event
        try:
            result = propose_event(cfg, req)
        except ProposalError as e:
            return {"ok": False, "error": str(e)}
        log.info("proposed event %r at %s", result["title"], result["start"])
        return result

    if action == "pin":
        from .pins import set_pin
        from .runner import load_state
        mid = str(req.get("message_id", ""))
        if not _MSG_ID_RE.match(mid):
            return {"ok": False, "error": "not a valid message id"}
        if mid not in load_state(cfg)["messages"]:
            return {"ok": False, "error": f"unknown message id {mid} (aged out or never exported)"}
        note = req.get("note")
        if note is not None and len(str(note)) > 200:
            return {"ok": False, "error": "note too long (max 200 chars)"}
        try:
            entry = set_pin(cfg, mid, str(req.get("until", "")),
                            str(note) if note else None)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        log.info("pinned %s until %s", mid, entry["until"])
        return {"ok": True, "action": "pin", "message_id": mid, **entry}

    if action == "unpin":
        from .pins import remove_pin
        mid = str(req.get("message_id", ""))
        if not _MSG_ID_RE.match(mid):
            return {"ok": False, "error": "not a valid message id"}
        existed = remove_pin(cfg, mid)
        log.info("unpinned %s (existed=%s)", mid, existed)
        return {"ok": True, "action": "unpin", "message_id": mid, "was_pinned": existed}

    if action == "preferences":
        from .preferences import describe
        return {"ok": True, "action": "preferences", **describe(cfg)}

    if action == "set-preference":
        from .preferences import PreferenceError, _get, _set, describe, set_preference
        key = str(req.get("key", ""))
        if "value" not in req:
            return {"ok": False, "error": "value required (null clears the preference)"}
        try:
            stored = set_preference(cfg, key, req["value"])
        except PreferenceError as e:
            return {"ok": False, "error": str(e)}
        # Make later requests in this batch see the new effective value.
        if stored is not None:
            _set(cfg, key, stored)
            cfg.pref_sources[key] = "preference"
        elif cfg.source_path is not None:
            from .config import load_config
            fresh = load_config(cfg.source_path)
            _set(cfg, key, _get(fresh, key))
            cfg.pref_sources[key] = fresh.pref_sources.get(key, "default")
        return {"ok": True, "action": "set-preference", "key": key, "value": stored,
                "effective": describe(cfg)["settings"][key],
                "note": "takes effect on the next export; send {\"action\": \"refresh\"} "
                        "to apply it now"}

    if action == "open-settings":
        return _open_settings(cfg)

    if action == "refresh":
        from .pipeline import perform_export
        try:
            r = perform_export(cfg)
        except LockHeld:
            return {"ok": True, "action": "refresh",
                    "note": "an export is already running; it will finish shortly"}
        summary = r["mail_summary"] or {}
        return {
            "ok": r["exit_code"] == 0,
            "action": "refresh",
            "mail_count": summary.get("count"),
            "errors": ([r["mail_error"]] if r.get("mail_error") else []) + r["cal_errors"],
        }
    return {"ok": False, "error": f"unknown action {action!r}"}


def process_requests(cfg: Config) -> int:
    """Drain <output>/requests/. Symlink-safe: the folders are opened without
    following links (a planted link is replaced with a real folder), and a
    request must be a regular file - a link to some other file is answered
    with an error, never read, and only the link is removed."""
    from . import safefs
    from .output import ensure_layout

    out = cfg.output_dir
    ensure_layout(out)
    handled = 0
    with safefs.open_dir(out, "requests", create=True) as rq:
        names = sorted(e.name for e in safefs.entries(rq)
                       if e.name.endswith(".json") and not e.name.startswith("."))
        for name in names:
            try:
                st = os.stat(name, dir_fd=rq, follow_symlinks=False)
                if not stat.S_ISREG(st.st_mode):
                    result = {"ok": False, "error": "a request must be a regular file"}
                elif st.st_size > MAX_REQUEST_BYTES:
                    result = {"ok": False, "error": "request file too large"}
                else:
                    raw = safefs.read_at(rq, name, MAX_REQUEST_BYTES)
                    if raw is None:
                        result = {"ok": False, "error": "a request must be a regular file"}
                    else:
                        req = json.loads(raw.decode("utf-8", errors="replace"))
                        result = (_handle(cfg, req) if isinstance(req, dict)
                                  else {"ok": False, "error": "request must be a JSON object"})
            except FileNotFoundError:
                continue  # vanished mid-run
            except json.JSONDecodeError as e:
                result = {"ok": False, "error": f"bad JSON: {e}"}
            except AttachError as e:
                result = {"ok": False, "error": str(e)}
            except Exception as e:  # never leave a request unanswered
                log.exception("request %s failed", name)
                result = {"ok": False, "error": f"internal error: {e}"}
            safefs.write_file(out, Path("results") / name,
                              (json.dumps(result, sort_keys=True) + "\n").encode(),
                              mode=0o644)
            safefs.unlink_at(rq, name)
            log.info("request %s -> ok=%s", name, result.get("ok"))
            handled += 1
    return handled
