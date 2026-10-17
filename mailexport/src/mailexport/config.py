"""Load and validate ~/.config/agentinbox/config.toml, then apply the agent's
bounded preferences (see preferences.py) on top."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("~/.config/agentinbox/config.toml").expanduser()
DEFAULT_PREFERENCES_PATH = Path("~/.agentinbox/preferences.json").expanduser()
# Worker-trusted bookkeeping (id -> source path map, pins, run lock). Lives
# OUTSIDE the agent's folder: Cowork can write anywhere in its folder, and a
# tampered id map could point the fetch worker at out-of-scope mail.
DEFAULT_STATE_DIR = Path("~/.agentinbox/state").expanduser()


def _default_state_dir() -> Path:
    return DEFAULT_STATE_DIR


def _default_prefs_path() -> Path:
    return DEFAULT_PREFERENCES_PATH


@dataclass
class MailConfig:
    days: int = 90
    mailboxes: list[str] = field(default_factory=lambda: ["INBOX", "Sent"])
    body_chars: int = 20_000
    max_bytes: int = 2_000_000
    exclude_accounts: list[str] = field(default_factory=list)
    # Manual AccountUUID -> "Name <email>" fallback when Accounts.plist discovery fails.
    accounts: dict[str, str] = field(default_factory=dict)


@dataclass
class AttachmentsConfig:
    max_bytes: int = 52_428_800
    retention_days: int = 7


@dataclass
class CalendarConfig:
    past_days: int = 1
    future_days: int = 365          # events.json horizon (duplicate checking)
    agenda_days: int = 21           # agenda.md stays near-term
    include: list[str] = field(default_factory=list)       # titles
    # Account-qualified include entries, for titles that repeat across
    # accounts ("Work" in iCloud and in Google). Their titles are also in
    # `include`; only these accounts' calendars of that title are exported.
    include_at: list[tuple[str, str]] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    roles: dict[str, str] = field(default_factory=dict)
    # Calendars exported to lookup.json with minimal fields, only so the agent
    # can tell whether a meeting is already booked (e.g. the Exchange calendar
    # where accepted work invites land). Each entry is (title, account) where
    # account None means "any account with that title". In config.toml an
    # entry is either "Title" or { title = "Title", source = "Account" }.
    lookup: list[tuple[str, str | None]] = field(default_factory=list)


@dataclass
class RemindersConfig:
    enabled: bool = True
    include: list[str] = field(default_factory=list)  # lists; empty = all
    completed_days: int = 7
    allow_create: bool = True
    allow_complete: bool = True


@dataclass
class ProposalsConfig:
    enabled: bool = True
    # Any of "ics", "google", "outlook". Agent-adjustable via preferences.
    formats: list[str] = field(default_factory=lambda: ["ics"])


@dataclass
class PreferencesConfig:
    allow_agent_changes: bool = True   # human-only kill switch
    path: Path = field(default_factory=_default_prefs_path)


@dataclass
class SettingsPageConfig:
    agent_may_open: bool = True     # human-only: may the agent pop the page open?
    cooldown_seconds: int = 120     # minimum gap between agent-requested opens


@dataclass
class Config:
    output_dir: Path = Path("~/AgentInbox/Inbox").expanduser()
    state_dir: Path = field(default_factory=_default_state_dir)
    timezone: str = "America/New_York"
    mail: MailConfig = field(default_factory=MailConfig)
    attachments: AttachmentsConfig = field(default_factory=AttachmentsConfig)
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    reminders: RemindersConfig = field(default_factory=RemindersConfig)
    proposals: ProposalsConfig = field(default_factory=ProposalsConfig)
    preferences: PreferencesConfig = field(default_factory=PreferencesConfig)
    settings_page: SettingsPageConfig = field(default_factory=SettingsPageConfig)
    interval_seconds: int = 1800
    calexport_bin: Path | None = None
    source_path: Path | None = None
    # Filled by preferences.apply_preferences: key -> "default"|"config"|"preference"
    pref_sources: dict[str, str] = field(default_factory=dict)
    pref_errors: list[str] = field(default_factory=list)
    onboarded: bool = False

    @property
    def log_dir(self) -> Path:
        """Logs live next to trusted state, never in the agent's folder."""
        return self.state_dir.parent / "logs"


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p))


def _calendar_refs(entries, errors: list[str], key: str) -> list[tuple[str, str | None]]:
    """Accept "Title" or {title = "...", source = "..."}; drop anything else."""
    out: list[tuple[str, str | None]] = []
    for e in entries or []:
        if isinstance(e, str) and e.strip():
            out.append((e, None))
        elif isinstance(e, dict) and isinstance(e.get("title"), str) and e["title"].strip():
            src = e.get("source")
            if src is not None and not (isinstance(src, str) and src.strip()):
                errors.append(f"config: calendar.{key} entry {e!r} has an empty source; ignored")
                continue
            out.append((e["title"], src))
        else:
            errors.append(f"config: calendar.{key} entry {e!r} is not a title or "
                          "{ title = ..., source = ... }; ignored")
    return out


def load_config(path: Path | None = None, *, apply_prefs: bool = True) -> Config:
    path = path or DEFAULT_CONFIG_PATH
    cfg = Config()
    data: dict = {}
    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)
        cfg.source_path = path

    if "output_dir" in data:
        cfg.output_dir = _expand(data["output_dir"])
    if "timezone" in data:
        cfg.timezone = data["timezone"]
    if "state_dir" in data:
        cfg.state_dir = _expand(data["state_dir"])
    if "calexport_bin" in data:
        cfg.calexport_bin = _expand(data["calexport_bin"])

    m = data.get("mail", {})
    cfg.mail = MailConfig(
        days=int(m.get("days", 90)),
        mailboxes=list(m.get("mailboxes", ["INBOX", "Sent"])),
        body_chars=int(m.get("body_chars", 20_000)),
        max_bytes=int(m.get("max_bytes", 2_000_000)),
        exclude_accounts=[a.lower() for a in m.get("exclude_accounts", [])],
        accounts=dict(m.get("accounts", {})),
    )
    a = data.get("attachments", {})
    cfg.attachments = AttachmentsConfig(
        max_bytes=int(a.get("max_bytes", 52_428_800)),
        retention_days=int(a.get("retention_days", 7)),
    )
    c = data.get("calendar", {})
    inc_refs = _calendar_refs(c.get("include", []), cfg.pref_errors, "include")
    cfg.calendar = CalendarConfig(
        past_days=int(c.get("past_days", 1)),
        future_days=int(c.get("future_days", 365)),
        agenda_days=int(c.get("agenda_days", 21)),
        include=list(dict.fromkeys(t for t, _ in inc_refs)),
        include_at=[(t, src) for t, src in inc_refs if src],
        exclude=list(c.get("exclude", [])),
        roles=dict(c.get("roles", {})),
        lookup=_calendar_refs(c.get("lookup", []), cfg.pref_errors, "lookup"),
    )
    r = data.get("reminders", {})
    cfg.reminders = RemindersConfig(
        enabled=bool(r.get("enabled", True)),
        include=list(r.get("include", [])),
        completed_days=int(r.get("completed_days", 7)),
        allow_create=bool(r.get("allow_create", True)),
        allow_complete=bool(r.get("allow_complete", True)),
    )
    p = data.get("proposals", {})
    cfg.proposals = ProposalsConfig(
        enabled=bool(p.get("enabled", True)),
        formats=list(p.get("formats", ["ics"])),
    )
    pr = data.get("preferences", {})
    cfg.preferences = PreferencesConfig(
        allow_agent_changes=bool(pr.get("allow_agent_changes", True)),
        path=_expand(pr["path"]) if "path" in pr else _default_prefs_path(),
    )
    sp = data.get("settings_page", {})
    cfg.settings_page = SettingsPageConfig(
        agent_may_open=bool(sp.get("agent_may_open", True)),
        cooldown_seconds=max(30, int(sp.get("cooldown_seconds", 120))),
    )
    cfg.interval_seconds = int(data.get("schedule", {}).get("interval_seconds", 1800))

    if apply_prefs:
        from .preferences import apply_preferences
        apply_preferences(cfg, data)
    return cfg
