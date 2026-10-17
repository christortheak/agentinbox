"""Compose manifest.json after a run; staleness detection."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import Config
from .output import atomic_write

STALE_INTERVALS = 3  # newest source older than 3x the schedule interval => stale


def _read_json(path: Path) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _home(cfg: Config) -> list[str]:
    from .meetings import home_calendars
    return home_calendars(cfg)


def _prefs(cfg: Config) -> dict:
    from .preferences import describe
    return describe(cfg)


def compute_stale(newest_source_mtime_iso: str | None, interval_seconds: int) -> bool:
    if not newest_source_mtime_iso:
        return True
    newest = datetime.fromisoformat(newest_source_mtime_iso)
    age = (datetime.now(timezone.utc) - newest).total_seconds()
    return age > STALE_INTERVALS * interval_seconds


def write_manifest(cfg: Config, mail_summary: dict | None, cal_errors: list[str],
                   meetings_summary: dict | None = None) -> dict:
    cal_meta = _read_json(cfg.state_dir / "calendar-meta.json")
    errors: list[str] = []
    if mail_summary:
        errors += mail_summary.get("errors", [])
    errors += cal_meta.get("errors", [])
    errors += cal_errors
    errors += cfg.pref_errors
    from . import safefs
    safefs.register_root(cfg.output_dir)
    errors += [f"security: {r}" for r in safefs.REPAIRS]

    stale = compute_stale(
        (mail_summary or {}).get("newest_source_mtime"), cfg.interval_seconds
    )
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "store_version": (mail_summary or {}).get("store_version"),
        "exporter_versions": {
            "mailexport": __version__,
            "calexport": cal_meta.get("version"),
        },
        "mail": {
            "count": (mail_summary or {}).get("count", 0),
            "by_account": (mail_summary or {}).get("by_account", {}),
            "newest_source_mtime": (mail_summary or {}).get("newest_source_mtime"),
            "duration_seconds": (mail_summary or {}).get("duration_seconds"),
            "days": cfg.mail.days,
            "mailboxes": list(cfg.mail.mailboxes),
            "retained_beyond_window": (mail_summary or {}).get("retained_beyond_window", 0),
        },
        "calendar": {
            "count": cal_meta.get("count", 0),
            "calendars": cal_meta.get("calendars", []),
            "home": _home(cfg),
            "roles": dict(cfg.calendar.roles),
            "lookup_calendars": cal_meta.get("lookup_calendars", []),
            "lookup_count": cal_meta.get("lookup_count", 0),
            "future_days": cfg.calendar.future_days,
            "agenda_days": cfg.calendar.agenda_days,
            "available": cal_meta.get("all_calendars", []),
        },
        "meetings": meetings_summary or {},
        "reminders": {
            "count": cal_meta.get("reminders_count", 0),
            "lists": cal_meta.get("reminder_lists", []),
        },
        "stale": stale,
        "stale_hint": (
            "newest local message is old; Mail.app is probably not running"
            if stale else None
        ),
        "preferences": _prefs(cfg),
        "errors": errors,
    }
    atomic_write(
        cfg.output_dir / "manifest.json",
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
        mode=0o644,
    )
    return manifest
