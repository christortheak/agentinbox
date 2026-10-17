"""Bounded, agent-adjustable preferences.

The agent may change a short allowlist of presentation/retention settings by
writing a request (`set-preference`). The fetch worker validates the key and
value against SCHEMA and writes ~/.agentinbox/preferences.json - a file the
agent cannot reach. Effective values = preference if set, else config.toml,
else built-in default.

Everything security-relevant stays human-only in config.toml: output folder,
binaries, accounts, mailboxes, write permissions, size caps, schedule. In
particular, `mailboxes` is deliberately NOT here: widening mailbox scope
widens what mail the agent can see, so only the user may do it.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("preferences")

LINK_FORMATS = ("ics", "google", "outlook")

# key -> (kind, bounds/choices, config-section, config-key, human description)
SCHEMA: dict[str, dict[str, Any]] = {
    "mail_days": {
        "kind": "int", "min": 7, "max": 365,
        "config": ("mail", "days"),
        "describe": "how many days of mail to keep (7-365)",
    },
    "calendar_future_days": {
        "kind": "int", "min": 7, "max": 365,
        "config": ("calendar", "future_days"),
        "describe": "how far ahead the calendar export looks (7-365 days)",
    },
    "reminders_completed_days": {
        "kind": "int", "min": 0, "max": 30,
        "config": ("reminders", "completed_days"),
        "describe": "how many days of completed reminders to show (0-30)",
    },
    "calendar_link_format": {
        "kind": "formats", "choices": LINK_FORMATS,
        "config": ("proposals", "formats"),
        "describe": "click-to-add format(s) for proposed events: ics, google, outlook",
    },
    "onboarded": {
        "kind": "bool",
        "config": None,
        "describe": "set true once the user has reviewed these preferences",
    },
}

HUMAN_ONLY_HINTS = {
    "mailboxes": (
        "mailboxes can only be changed by the user. Tell them exactly how: see "
        "'Adding a mailbox' in README.md, and use the `mailboxes` request to "
        "get the exact names their store uses."
    ),
}


class PreferenceError(Exception):
    pass


def _get(cfg, key: str):
    if key == "mail_days":
        return cfg.mail.days
    if key == "calendar_future_days":
        return cfg.calendar.future_days
    if key == "reminders_completed_days":
        return cfg.reminders.completed_days
    if key == "calendar_link_format":
        return list(cfg.proposals.formats)
    if key == "onboarded":
        return cfg.onboarded
    raise KeyError(key)


def _set(cfg, key: str, value) -> None:
    if key == "mail_days":
        cfg.mail.days = value
    elif key == "calendar_future_days":
        cfg.calendar.future_days = value
    elif key == "reminders_completed_days":
        cfg.reminders.completed_days = value
    elif key == "calendar_link_format":
        cfg.proposals.formats = list(value)
    elif key == "onboarded":
        cfg.onboarded = value
    else:
        raise KeyError(key)


def validate(key: str, value) -> Any:
    """Return the normalized value or raise PreferenceError."""
    if key in HUMAN_ONLY_HINTS:
        raise PreferenceError(HUMAN_ONLY_HINTS[key])
    spec = SCHEMA.get(key)
    if spec is None:
        raise PreferenceError(
            f"unknown or human-only setting {key!r}; agent-adjustable settings "
            f"are: {', '.join(sorted(SCHEMA))}")
    kind = spec["kind"]
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise PreferenceError(f"{key} must be a whole number")
        try:
            n = int(value)
        except ValueError:
            raise PreferenceError(f"{key} must be a whole number") from None
        if not spec["min"] <= n <= spec["max"]:
            raise PreferenceError(f"{key} must be between {spec['min']} and {spec['max']}")
        return n
    if kind == "bool":
        if not isinstance(value, bool):
            raise PreferenceError(f"{key} must be true or false")
        return value
    if kind == "formats":
        items = value.split(",") if isinstance(value, str) else value
        if not isinstance(items, list) or not items:
            raise PreferenceError(f"{key} must be one or more of {', '.join(LINK_FORMATS)}")
        out = []
        for it in items:
            s = str(it).strip().lower()
            if s not in spec["choices"]:
                raise PreferenceError(
                    f"{key}: {s!r} is not one of {', '.join(LINK_FORMATS)}")
            if s not in out:
                out.append(s)
        return [f for f in LINK_FORMATS if f in out]  # canonical order
    raise PreferenceError(f"unsupported kind for {key}")


def _read_file(path: Path) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("values"), dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"version": 1, "values": {}, "changed_at": {}}


def apply_preferences(cfg, config_data: dict) -> None:
    """Overlay stored preferences onto cfg and record each setting's source."""
    stored = _read_file(cfg.preferences.path)["values"]
    for key, spec in SCHEMA.items():
        src = "default"
        if spec["config"]:
            section, ckey = spec["config"]
            if ckey in config_data.get(section, {}):
                src = "config"
        if key in stored:
            try:
                _set(cfg, key, validate(key, stored[key]))
                src = "preference"
            except PreferenceError as e:
                cfg.pref_errors.append(f"ignored stored preference {key}: {e}")
        cfg.pref_sources[key] = src
    # Proposal formats are validated even when they come from config.toml.
    try:
        cfg.proposals.formats = validate("calendar_link_format", cfg.proposals.formats)
    except PreferenceError as e:
        cfg.pref_errors.append(f"config proposals.formats invalid ({e}); using ics")
        cfg.proposals.formats = ["ics"]


def set_preference(cfg, key: str, value) -> Any:
    """Validate and persist one preference. value=None clears it (reverts to
    config/default). Returns the stored normalized value (or None)."""
    if not cfg.preferences.allow_agent_changes:
        raise PreferenceError(
            "the user has disabled agent preference changes "
            "([preferences] allow_agent_changes = false in config.toml)")
    if value is not None:
        value = validate(key, value)
    elif key not in SCHEMA:
        validate(key, None)  # raises the right message
    path = cfg.preferences.path
    data = _read_file(path)
    if value is None:
        data["values"].pop(key, None)
    else:
        data["values"][key] = value
    data.setdefault("changed_at", {})[key] = (
        datetime.now(timezone.utc).isoformat(timespec="seconds"))
    from .output import atomic_write
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode(),
                 mode=0o600)
    log.info("preference %s set to %r by agent request", key, value)
    return value


def describe(cfg) -> dict:
    """Effective values, where each came from, and what the agent may set."""
    changed = _read_file(cfg.preferences.path).get("changed_at", {})
    out = {}
    for key, spec in SCHEMA.items():
        entry = {
            "value": _get(cfg, key),
            "source": cfg.pref_sources.get(key, "default"),
            "allowed": spec["describe"],
        }
        if key in changed:
            entry["changed_at"] = changed[key]
        out[key] = entry
    return {
        "agent_may_change": cfg.preferences.allow_agent_changes,
        "settings": out,
        "human_only": ["mailboxes", "accounts", "output folder", "write permissions",
                       "size caps", "schedule", "calendars exported"],
    }


def reset_preference(cfg, key: str | None = None) -> None:
    """Human action (config UI): clear one agent preference, or all of them.
    Deliberately not gated by allow_agent_changes - that switch limits the
    agent, not the user."""
    if key is not None and key not in SCHEMA:
        raise PreferenceError(f"unknown preference {key!r}")
    path = cfg.preferences.path
    data = _read_file(path)
    if key is None:
        data["values"], data["changed_at"] = {}, {}
    else:
        data["values"].pop(key, None)
        data.setdefault("changed_at", {}).pop(key, None)
    from .output import atomic_write
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode(),
                 mode=0o600)
    log.info("preference %s reset by the user", key or "(all)")
