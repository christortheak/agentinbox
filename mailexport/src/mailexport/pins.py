"""Agent-requested pins: keep a specific exported message past the mail window
until a date (e.g. an email that schedules a meeting in plain text). Stored in
the worker-trusted state dir, never in the agent's folder."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

MAX_PIN_DAYS = 366


def _path(cfg) -> Path:
    return cfg.state_dir / "pins.json"


def _today(cfg) -> date:
    return datetime.now(ZoneInfo(cfg.timezone)).date()


def load_pins(cfg) -> dict[str, dict]:
    """Active pins only (until >= today)."""
    try:
        with open(_path(cfg)) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    today = _today(cfg).isoformat()
    return {k: v for k, v in data.items()
            if isinstance(v, dict) and str(v.get("until", "")) >= today}


def _save(cfg, pins: dict) -> None:
    from .output import atomic_write
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(_path(cfg), (json.dumps(pins, indent=2, sort_keys=True) + "\n").encode())


def set_pin(cfg, msg_id: str, until: str, note: str | None) -> dict:
    try:
        u = date.fromisoformat(until)
    except (TypeError, ValueError):
        raise ValueError("until must be yyyy-MM-dd") from None
    today = _today(cfg)
    if u < today or u > today + timedelta(days=MAX_PIN_DAYS):
        raise ValueError(f"until must be between today and {MAX_PIN_DAYS} days from now")
    pins = load_pins(cfg)  # drops expired entries as a side effect of saving
    entry = {"until": u.isoformat(),
             "pinned_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if note:
        entry["note"] = note
    pins[msg_id] = entry
    _save(cfg, pins)
    return entry


def remove_pin(cfg, msg_id: str) -> bool:
    pins = load_pins(cfg)
    existed = pins.pop(msg_id, None) is not None
    _save(cfg, pins)
    return existed
