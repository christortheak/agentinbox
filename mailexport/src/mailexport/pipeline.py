"""The full export pipeline (mail + calendar + prune + manifest), shared by
`agentinbox run` and the fetch worker's "refresh" action."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .config import Config
from .doctor import find_calexport
from .lock import run_lock
from .manifest import write_manifest
from .runner import load_state, prune_outputs, run_mail_export
from .store import StoreError

log = logging.getLogger("agentinbox")


def run_calexport(cfg: Config) -> list[str]:
    """Invoke the Swift binary; the wrapper owns TOML parsing so calexport
    just takes flags."""
    binary = find_calexport(cfg)
    if binary is None:
        return ["calexport binary not found; calendar export skipped"]
    # calexport writes into trusted state; publish_calexport() then copies the
    # files into the agent's folder without following symlinks.
    staging = cfg.state_dir / "calexport-out"
    args = [
        str(binary),
        "--output", str(staging),
        "--past-days", str(cfg.calendar.past_days),
        "--future-days", str(cfg.calendar.future_days),
        "--agenda-days", str(cfg.calendar.agenda_days),
        "--timezone", cfg.timezone,
        "--state-dir", str(cfg.state_dir),
    ]
    for title, source in cfg.calendar.lookup:
        if source:
            args += ["--lookup-at", title, source]   # only that account's calendar
        else:
            args += ["--lookup", title]
    qualified = {t for t, _ in cfg.calendar.include_at}
    for title in cfg.calendar.include:
        if title not in qualified:
            args += ["--include", title]
    for title, source in cfg.calendar.include_at:
        args += ["--include-at", title, source]
    for title in cfg.calendar.exclude:
        args += ["--exclude", title]
    for title, role in cfg.calendar.roles.items():
        args += ["--role", f"{title}={role}"]
    if cfg.reminders.enabled:
        args += ["--reminders",
                 "--reminders-completed-days", str(cfg.reminders.completed_days)]
        for title in cfg.reminders.include:
            args += ["--reminders-include", title]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        return [f"calexport failed to run: {e}"]
    if r.returncode != 0:
        return [f"calexport exited {r.returncode}: {(r.stderr or r.stdout).strip()[:500]}"]
    publish_calexport(cfg)
    return []


CALEXPORT_FILES = ("calendar/events.json", "calendar/agenda.md", "calendar/lookup.json",
                   "reminders/reminders.json", "reminders/reminders.md")


def publish_calexport(cfg: Config) -> None:
    from . import safefs
    staging = cfg.state_dir / "calexport-out"
    for rel in CALEXPORT_FILES:
        src = staging / rel
        try:
            data = src.read_bytes()
        except FileNotFoundError:
            continue
        safefs.write_file(cfg.output_dir, rel, data, mode=0o644)


def perform_export(cfg: Config, mail_library: Path | None = None) -> dict:
    """Run everything under the lock. Raises LockHeld if a run is in progress.
    Returns {"exit_code", "mail_summary", "cal_errors", "manifest"}."""
    from .output import ensure_layout
    ensure_layout(cfg.output_dir)      # repairs planted symlinks before anything writes
    with run_lock(cfg.state_dir):
        mail_summary = None
        mail_failed = False
        try:
            mail_summary = run_mail_export(cfg, mail_library=mail_library)
            log.info("mail: %s exported (%s new, %s removed) in %ss",
                     mail_summary["count"], mail_summary["parsed"],
                     mail_summary["removed"], mail_summary["duration_seconds"])
        except StoreError as e:
            # Missing FDA etc: exit nonzero without touching existing output.
            log.error("mail export failed: %s", e)
            mail_failed = True
            mail_error = str(e)

        cal_errors = run_calexport(cfg)
        for err in cal_errors:
            log.error("%s", err)

        # Cross-check invites in mail against the calendars just exported.
        meetings_summary = None
        if not mail_failed:
            try:
                from .meetings import build_meetings
                records = [m["record"] for m in load_state(cfg)["messages"].values()]
                meetings_summary = build_meetings(cfg, records)
                log.info("meetings: %s future invites, %s not on calendar",
                         meetings_summary["future_invites"],
                         meetings_summary["not_on_calendar"])
            except Exception as e:  # never let this block the export
                log.exception("meeting cross-check failed")
                cal_errors.append(f"meeting cross-check failed: {e}")

        pruned = prune_outputs(cfg)
        if pruned:
            log.info("pruned %d stale output file(s)", pruned)

        manifest = None
        if not mail_failed:
            manifest = write_manifest(cfg, mail_summary, cal_errors, meetings_summary)
            if manifest["stale"]:
                log.warning("export is stale: %s", manifest["stale_hint"])
        return {
            "exit_code": 1 if (mail_failed or cal_errors) else 0,
            "mail_summary": mail_summary,
            "mail_error": mail_error if mail_failed else None,
            "cal_errors": cal_errors,
            "manifest": manifest,
        }
