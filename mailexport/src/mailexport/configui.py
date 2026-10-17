"""`agentinbox config-ui` - a local, human-only web page for editing config.toml.

This is the ONE network listener in AgentInbox, so its guardrails matter:

- Started only by a person from Terminal. launchd never runs it, and nothing
  in the exporter, worker or pipeline imports this module.
- Listens on 127.0.0.1 only, on a random port.
- Host header must be exactly 127.0.0.1:<port> (defeats DNS rebinding).
- The page needs a one-time secret in its URL; every API call must carry it in
  the X-AgentInbox-Token header. Other websites can't add custom headers to
  cross-site requests without a CORS preflight, which this server never
  approves. No cookies are used. A present Origin header must be our own.
- Strict CSP, no framing, no referrer, no caching.
- Exits when you click Done, or after 15 idle minutes.
- It cannot change the output folder, state folder, binaries, account
  mapping or schedule: those are carried over from the existing file verbatim.
"""
from __future__ import annotations

import copy
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import tomllib
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import DEFAULT_CONFIG_PATH, load_config
from .output import atomic_write
from .preferences import LINK_FORMATS, PreferenceError, describe, reset_preference

USES = ("home", "shared", "other", "lookup", "off")
ROLE_OF_USE = {"home": "mine", "shared": "shared", "other": "other"}
USE_OF_ROLE = {"mine": "home", "shared": "shared", "other": "other"}
MAX_BODY = 256_000
BACKUPS_KEPT = 5


# ---------------------------------------------------------------- state ----

def load_calendar_list(cfg) -> list[dict]:
    """All calendars seen by the last export (title, source, type)."""
    try:
        with open(cfg.state_dir / "calendar-meta.json") as f:
            meta = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    out = []
    for c in meta.get("all_calendars", []):
        if isinstance(c, dict) and isinstance(c.get("title"), str):
            out.append({"title": c["title"], "source": str(c.get("source") or ""),
                        "type": str(c.get("type") or "")})
    return out


def _ref_matches(ref, title: str, source: str) -> bool:
    if isinstance(ref, str):
        return ref == title
    if isinstance(ref, dict):
        return ref.get("title") == title and (ref.get("source") in (None, source))
    return False


def calendar_use(raw_cal: dict, title: str, source: str) -> str:
    """How the current config treats one calendar."""
    include = raw_cal.get("include", []) or []
    roles = raw_cal.get("roles", {}) or {}
    qualified = {r["title"] for r in include if isinstance(r, dict) and r.get("source")}
    included = any(
        (isinstance(r, dict) and r.get("title") == title and r.get("source") == source)
        or (isinstance(r, str) and r == title and title not in qualified)
        or (isinstance(r, dict) and r.get("title") == title and not r.get("source"))
        for r in include)
    if included:
        titles = {r if isinstance(r, str) else r.get("title") for r in include}
        if title in roles:
            return USE_OF_ROLE.get(roles[title], "other")
        return "home" if (len(titles) == 1 and not roles) else "other"
    if any(_ref_matches(r, title, source) for r in raw_cal.get("lookup", []) or []):
        return "lookup"
    if not include and title not in (raw_cal.get("exclude") or []):
        return "other"   # "all calendars minus exclude" mode
    return "off"


def _unmatched(refs, calendars) -> list:
    return [r for r in refs or []
            if not any(_ref_matches(r, c["title"], c["source"]) for c in calendars)]


def _read_raw(cfg_path: Path) -> dict:
    try:
        with open(cfg_path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def current_state(cfg_path: Path) -> dict:
    raw = _read_raw(cfg_path)
    cfg = load_config(cfg_path)
    calendars = load_calendar_list(cfg)
    raw_cal = raw.get("calendar", {})
    # Calendars with the same name in the same account can't be told apart by
    # config (or by the user), so they share one row.
    same = Counter((c["title"], c["source"]) for c in calendars)
    rows, seen = [], set()
    for c in calendars:
        key = (c["title"], c["source"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(dict(c, use=calendar_use(raw_cal, c["title"], c["source"]),
                         count=same[key]))
    counts = Counter(c["title"] for c in calendars)
    for r in rows:
        r["title_repeats"] = counts[r["title"]] > r["count"]
    from . import safefs
    try:
        man = json.loads(safefs.read_file(cfg.output_dir, "manifest.json") or b"{}")
    except json.JSONDecodeError:
        man = {}
    try:
        with open(cfg.state_dir / "configui-mailboxes.json") as f:
            mailbox_diag = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        mailbox_diag = None
    return {
        "config_path": str(cfg_path),
        "calendars": rows,
        "unmatched_include": _unmatched(raw_cal.get("include"), calendars),
        "unmatched_lookup": _unmatched(raw_cal.get("lookup"), calendars),
        "calendar": {"past_days": cfg.calendar.past_days,
                     "future_days": raw_cal.get("future_days", cfg.calendar.future_days),
                     "agenda_days": cfg.calendar.agenda_days},
        "mail": {"days": raw.get("mail", {}).get("days", cfg.mail.days),
                 "mailboxes": list(cfg.mail.mailboxes),
                 "exclude_accounts": list(cfg.mail.exclude_accounts)},
        "proposals": {"enabled": cfg.proposals.enabled,
                      "formats": raw.get("proposals", {}).get("formats", ["ics"])},
        "reminders": {"enabled": cfg.reminders.enabled,
                      "allow_create": cfg.reminders.allow_create,
                      "allow_complete": cfg.reminders.allow_complete,
                      "completed_days": raw.get("reminders", {}).get(
                          "completed_days", cfg.reminders.completed_days),
                      "include": list(cfg.reminders.include)},
        "attachments": {"max_mb": round(cfg.attachments.max_bytes / 1_048_576),
                        "retention_days": cfg.attachments.retention_days},
        "agent": dict(describe(cfg), may_open_settings=cfg.settings_page.agent_may_open),
        "fixed": {"output_dir": str(cfg.output_dir), "state_dir": str(cfg.state_dir),
                  "timezone": cfg.timezone,
                  "interval_minutes": cfg.interval_seconds // 60,
                  "accounts": dict(raw.get("mail", {}).get("accounts", {}))},
        "manifest": {"generated_at": man.get("generated_at"), "stale": man.get("stale"),
                     "mail_count": man.get("mail", {}).get("count"),
                     "events": man.get("calendar", {}).get("count"),
                     "errors": man.get("errors", [])},
        "mailbox_diag": mailbox_diag,
        "link_formats": list(LINK_FORMATS),
    }


# --------------------------------------------------------------- saving ----

class Invalid(Exception):
    pass


def _int(errors, label, v, lo, hi):
    if isinstance(v, bool):
        errors.append(f"{label} must be a whole number")
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        errors.append(f"{label} must be a whole number")
        return None
    if not lo <= n <= hi:
        errors.append(f"{label} must be between {lo} and {hi}")
        return None
    return n


def _bool(errors, label, v):
    if not isinstance(v, bool):
        errors.append(f"{label} must be on or off")
        return None
    return v


def _strs(errors, label, v, max_len=200):
    if not isinstance(v, list):
        errors.append(f"{label} must be a list")
        return None
    out = []
    for s in v:
        if not isinstance(s, str) or not s.strip() or len(s) > max_len:
            errors.append(f"{label}: {s!r} is not a valid entry")
            return None
        if s.strip() not in out:
            out.append(s.strip())
    return out


def _ref_ok(r) -> bool:
    return (isinstance(r, str) and r.strip() != "") or (
        isinstance(r, dict) and isinstance(r.get("title"), str) and r["title"].strip()
        and set(r) <= {"title", "source"}
        and (r.get("source") is None or isinstance(r.get("source"), str)))


def build_config(raw: dict, payload: dict, calendars: list[dict]) -> tuple[dict, list[str]]:
    """Return (new config data, warnings). Raises Invalid(list of errors)."""
    if not isinstance(payload, dict):
        raise Invalid(["request body must be a JSON object"])
    data = copy.deepcopy(raw)
    errors: list[str] = []
    warnings: list[str] = []

    # Calendars -> include / roles / lookup
    if "calendars" in payload:
        known = {(c["title"], c["source"]) for c in calendars}
        counts = Counter(c["title"] for c in calendars)
        include, lookup, roles = [], [], {}
        rows = payload["calendars"]
        if not isinstance(rows, list):
            errors.append("calendars must be a list")
            rows = []
        for r in rows:
            if not isinstance(r, dict):
                errors.append("bad calendar row")
                continue
            t, src, use = r.get("title"), r.get("source"), r.get("use")
            if (t, src) not in known:
                errors.append(f"unknown calendar {t!r} ({src})")
                continue
            if use not in USES:
                errors.append(f"{t!r}: unknown setting {use!r}")
                continue
            ref = {"title": t, "source": src} if counts[t] > 1 else t
            if use in ROLE_OF_USE:
                role = ROLE_OF_USE[use]
                if roles.get(t, role) != role:
                    errors.append(f"calendars titled {t!r} in different accounts must "
                                  "have the same role")
                roles[t] = role
                include.append(ref)
            elif use == "lookup":
                lookup.append(ref)
        raw_cal = raw.get("calendar", {})
        raw_roles = raw_cal.get("roles", {}) or {}
        for key, target in (("keep_include", include), ("keep_lookup", lookup)):
            for ref in payload.get(key, []) or []:
                if not _ref_ok(ref):
                    errors.append(f"bad kept entry {ref!r}")
                    continue
                target.append(ref)
                t = ref if isinstance(ref, str) else ref["title"]
                if target is include and t in raw_roles and t not in roles:
                    roles[t] = raw_roles[t]
        if not include:
            errors.append("choose at least one calendar to export (your home calendar)")
        elif "mine" not in roles.values():
            warnings.append("No calendar is set to Home. Proposals and the meeting "
                            "check need a home calendar.")
        cal = data.setdefault("calendar", {})
        cal["include"], cal["lookup"], cal["exclude"] = include, lookup, []
        cal["roles"] = roles

    c = payload.get("calendar")
    if isinstance(c, dict):
        cal = data.setdefault("calendar", {})
        for key, lo, hi in (("past_days", 0, 30), ("future_days", 1, 1460),
                            ("agenda_days", 1, 90)):
            if key in c:
                n = _int(errors, f"calendar {key.replace('_', ' ')}", c[key], lo, hi)
                if n is not None:
                    cal[key] = n

    m = payload.get("mail")
    if isinstance(m, dict):
        mail = data.setdefault("mail", {})
        if "days" in m:
            n = _int(errors, "mail days", m["days"], 1, 3650)
            if n is not None:
                mail["days"] = n
        if "mailboxes" in m:
            boxes = _strs(errors, "mailboxes", m["mailboxes"], 100)
            if boxes is not None:
                if not boxes:
                    errors.append("choose at least one mailbox")
                mail["mailboxes"] = boxes
                if any(b.lower() in ("junk", "trash", "spam") for b in boxes):
                    warnings.append("Junk or Trash is exported: the agent will see that mail.")
        if "exclude_accounts" in m:
            ex = _strs(errors, "excluded accounts", m["exclude_accounts"])
            if ex is not None:
                mail["exclude_accounts"] = [e.lower() for e in ex]

    p = payload.get("proposals")
    if isinstance(p, dict):
        prop = data.setdefault("proposals", {})
        if "enabled" in p:
            b = _bool(errors, "proposals", p["enabled"])
            if b is not None:
                prop["enabled"] = b
        if "formats" in p:
            f = p["formats"]
            if not isinstance(f, list) or not f or any(x not in LINK_FORMATS for x in f):
                errors.append("choose at least one link format")
            else:
                prop["formats"] = [x for x in LINK_FORMATS if x in f]

    r = payload.get("reminders")
    if isinstance(r, dict):
        rem = data.setdefault("reminders", {})
        for key in ("enabled", "allow_create", "allow_complete"):
            if key in r:
                b = _bool(errors, f"reminders {key.replace('_', ' ')}", r[key])
                if b is not None:
                    rem[key] = b
        if "completed_days" in r:
            n = _int(errors, "completed reminders days", r["completed_days"], 0, 365)
            if n is not None:
                rem["completed_days"] = n
        if "include" in r:
            lists = _strs(errors, "reminder lists", r["include"], 100)
            if lists is not None:
                rem["include"] = lists

    a = payload.get("attachments")
    if isinstance(a, dict):
        att = data.setdefault("attachments", {})
        if "max_mb" in a:
            n = _int(errors, "attachment size limit (MB)", a["max_mb"], 1, 500)
            if n is not None:
                att["max_bytes"] = n * 1_048_576
        if "retention_days" in a:
            n = _int(errors, "attachment retention days", a["retention_days"], 1, 90)
            if n is not None:
                att["retention_days"] = n

    g = payload.get("agent")
    if isinstance(g, dict) and "allow_agent_changes" in g:
        b = _bool(errors, "agent preference changes", g["allow_agent_changes"])
        if b is not None:
            data.setdefault("preferences", {})["allow_agent_changes"] = b
    if isinstance(g, dict) and "may_open_settings" in g:
        b = _bool(errors, "agent may open settings", g["may_open_settings"])
        if b is not None:
            data.setdefault("settings_page", {})["agent_may_open"] = b

    if errors:
        raise Invalid(errors)
    return data, warnings


# ------------------------------------------------------------ rendering ----

_COMMENTS = {
    ("", "output_dir"): "the agent's folder; NOT inside iCloud Drive",
    ("mail", "days"): "emails tied to future meetings are kept longer automatically",
    ("mail", "mailboxes"): "only you can change this: it decides what mail the agent sees",
    ("mail", "exclude_accounts"): "email addresses, case-insensitive",
    ("attachments", "max_bytes"): "cap for on-demand attachment fetches",
    ("calendar", "future_days"): "events.json horizon, for duplicate checking",
    ("calendar", "agenda_days"): "agenda.md stays near-term",
    ("calendar", "include"): "exported in full; role decides home/shared/other",
    ("calendar", "lookup"): "duplicate-check only: title and time",
    ("calendar", "exclude"): "used only when include is empty",
    ("reminders", "allow_create"): "agent may create reminders",
    ("reminders", "allow_complete"): "agent may check reminders off",
    ("proposals", "formats"): "any of \"ics\", \"google\", \"outlook\"",
    ("preferences", "allow_agent_changes"): "false = the agent cannot change any preference",
    ("settings_page", "agent_may_open"): "may the agent pop this settings page open for you",
}
_SECTION_COMMENTS = {
    "calendar.roles": "mine = home (where everything goes); shared = readable, not home; "
                      "other = calendars you follow",
    "mail.accounts": "manual account mapping (wins over discovery)",
}
_ORDER = ["mail", "attachments", "calendar", "reminders", "proposals", "preferences",
          "settings_page", "schedule"]


def _key(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k, ensure_ascii=False)


def _val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_val(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_val(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"can't write {type(v).__name__} to TOML")


def _line(section: str, k: str, v) -> str:
    s = f"{_key(k)} = {_val(v)}"
    c = _COMMENTS.get((section, k))
    return f"{s}  # {c}" if c else s


def render_toml(data: dict) -> str:
    out = ["# AgentInbox configuration. Human-only: the agent cannot edit this file.",
           f"# Last written by `agentinbox config-ui` on {datetime.now():%Y-%m-%d %H:%M}.",
           "# Earlier versions are kept next to this file as config.toml.bak-*.", ""]
    for k, v in data.items():
        if not isinstance(v, dict):
            out.append(_line("", k, v))
    sections = [k for k, v in data.items() if isinstance(v, dict)]
    sections.sort(key=lambda s: (_ORDER.index(s) if s in _ORDER else len(_ORDER), s))
    for sec in sections:
        body = data[sec]
        out += ["", f"[{_key(sec)}]"]
        for k, v in body.items():
            if not isinstance(v, dict):
                out.append(_line(sec, k, v))
        for sub, sv in body.items():
            if isinstance(sv, dict):
                name = f"{sec}.{sub}"
                out.append("")
                if name in _SECTION_COMMENTS:
                    out.append(f"# {_SECTION_COMMENTS[name]}")
                out.append(f"[{_key(sec)}.{_key(sub)}]")
                for k, v in sv.items():
                    out.append(f"{_key(k)} = {_val(v)}")
    return "\n".join(out) + "\n"


def save(cfg_path: Path, payload: dict) -> dict:
    raw = _read_raw(cfg_path)
    cfg = load_config(cfg_path)
    try:
        data, warnings = build_config(raw, payload, load_calendar_list(cfg))
    except Invalid as e:
        return {"ok": False, "errors": e.args[0]}
    text = render_toml(data)
    tomllib.loads(text)                     # must round-trip before we write it
    backup = None
    if cfg_path.exists():
        backup = cfg_path.with_name(f"{cfg_path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(cfg_path, backup)
        olds = sorted(cfg_path.parent.glob(f"{cfg_path.name}.bak-*"))
        for old in olds[:-BACKUPS_KEPT]:
            old.unlink()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(cfg_path, text.encode("utf-8"), mode=0o600)
    try:
        load_config(cfg_path)
    except Exception as e:                   # never leave a broken config behind
        if backup:
            shutil.copy2(backup, cfg_path)
        return {"ok": False, "errors": [f"saved config didn't load ({e}); restored backup"]}
    return {"ok": True, "warnings": warnings, "backup": str(backup) if backup else None}


def request_mailboxes(cfg, timeout: float = 90.0) -> dict:
    """Ask the launchd fetch service (which has Full Disk Access) for the
    store's mailbox names, through the same request folder the agent uses."""
    from . import safefs
    name = f"configui-mailboxes-{secrets.token_hex(4)}.json"
    out = cfg.output_dir
    safefs.write_file(out, Path("requests") / name, b'{"action": "mailboxes"}\n')
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = safefs.read_file(out, Path("results") / name, max_bytes=5_000_000)
        if raw is not None:
            try:
                result = json.loads(raw)
            except json.JSONDecodeError:
                time.sleep(0.5)
                continue
            safefs.remove_file(out, Path("results") / name)
            if result.get("ok"):
                cfg.state_dir.mkdir(parents=True, exist_ok=True)
                atomic_write(cfg.state_dir / "configui-mailboxes.json",
                             json.dumps(result).encode())
            return result
        time.sleep(0.5)
    safefs.remove_file(out, Path("requests") / name)
    return {"ok": False, "error": "the fetch service didn't answer; is it installed?"}


# --------------------------------------------------------------- server ----

class ConfigUI:
    def __init__(self, cfg_path: Path, port: int = 0, idle_minutes: int = 15):
        self.cfg_path = cfg_path
        self.token = secrets.token_urlsafe(32)
        self.idle_seconds = idle_minutes * 60
        self.last_activity = time.time()
        ui = self

        class Handler(_Handler):
            app = ui

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/?t={self.token}"

    def touch(self):
        self.last_activity = time.time()

    def stop(self):
        threading.Thread(target=self.httpd.shutdown, daemon=True).start()

    def serve(self):
        def watchdog():
            while True:
                time.sleep(15)
                if time.time() - self.last_activity > self.idle_seconds:
                    self.stop()
                    return
        threading.Thread(target=watchdog, daemon=True).start()
        try:
            self.httpd.serve_forever()
        finally:
            self.httpd.server_close()


class _Handler(BaseHTTPRequestHandler):
    app: ConfigUI
    server_version = "agentinbox-config"
    sys_version = ""

    def log_message(self, fmt, *args):  # keep Terminal quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; connect-src 'self'; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj).encode())

    def _host_ok(self) -> bool:
        return self.headers.get("Host") == f"127.0.0.1:{self.app.port}"

    def _token_ok(self, given: str | None) -> bool:
        return bool(given) and hmac.compare_digest(given, self.app.token)

    def _api_ok(self) -> bool:
        if not self._token_ok(self.headers.get("X-AgentInbox-Token")):
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin == f"http://127.0.0.1:{self.app.port}"

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        parts = urlsplit(self.path)
        if parts.path == "/":
            given = parse_qs(parts.query).get("t", [None])[0]
            if not self._token_ok(given):
                return self._send(403, b"Open the link printed in Terminal.", "text/plain")
            self.app.touch()
            from .configui_page import PAGE
            return self._send(200, PAGE.replace("__TOKEN__", self.app.token).encode(),
                              "text/html; charset=utf-8")
        if parts.path == "/api/state":
            if not self._api_ok():
                return self._send(403, b"forbidden", "text/plain")
            self.app.touch()
            return self._json(200, current_state(self.app.cfg_path))
        return self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if not self._host_ok() or not self._api_ok():
            return self._send(403, b"forbidden", "text/plain")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self._send(415, b"json only", "text/plain")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            return self._send(413, b"too large", "text/plain")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"ok": False, "errors": ["bad JSON"]})
        self.app.touch()
        path = urlsplit(self.path).path
        cfg_path = self.app.cfg_path
        if path == "/api/save":
            return self._json(200, save(cfg_path, body))
        if path == "/api/reset-preference":
            try:
                reset_preference(load_config(cfg_path), body.get("key"))
            except PreferenceError as e:
                return self._json(200, {"ok": False, "errors": [str(e)]})
            return self._json(200, {"ok": True})
        if path == "/api/mailboxes":
            return self._json(200, request_mailboxes(load_config(cfg_path)))
        if path == "/api/run-export":
            r = subprocess.run(["launchctl", "kickstart",
                                f"gui/{os.getuid()}/com.agentinbox.export"],
                               capture_output=True, text=True)
            return self._json(200, {"ok": r.returncode == 0,
                                    "error": r.stderr.strip() or None})
        if path == "/api/done":
            self._json(200, {"ok": True})
            self.app.stop()
            return None
        return self._send(404, b"not found", "text/plain")


def run(cfg_path: Path | None = None, *, open_browser: bool = True, port: int = 0,
        idle_minutes: int = 15, print_link: bool = True) -> int:
    """print_link=False is how the launchd job runs it (started at the agent's
    request): the secret link goes only to the browser, never to a log."""
    from .lock import try_lock
    cfg_path = cfg_path or DEFAULT_CONFIG_PATH
    lock_path = load_config(cfg_path).state_dir / "configui.lock"
    with try_lock(lock_path) as got:
        if not got and not print_link:
            print("settings page already open; not opening another", flush=True)
            return 0
        ui = ConfigUI(cfg_path, port=port, idle_minutes=idle_minutes)
        if print_link:
            print("AgentInbox settings are open at:\n")
            print(f"  {ui.url}\n")
            print(f"Only this link works. The page closes when you click Done, or after "
                  f"{idle_minutes} idle minutes. Ctrl-C stops it now.", flush=True)
        if open_browser:
            subprocess.run(["open", ui.url], check=False)
        try:
            ui.serve()
        except KeyboardInterrupt:
            pass
    print("Settings page closed.", flush=True)
    return 0
