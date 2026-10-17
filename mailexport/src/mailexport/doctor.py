"""`agentinbox doctor` - environment and permission checks with actionable output.

Under the launcher model, TCC grants (Full Disk Access, Calendars) belong to
the signed `agentinbox-runner` binary and apply only when launchd runs the
jobs. A doctor invoked from a terminal therefore *cannot* read the Mail store
or query calendar access directly - that is the design working. For those
checks it falls back to the evidence of the last launchd run (manifest.json).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .accounts import discover_accounts
from .config import Config
from .manifest import STALE_INTERVALS
from .store import StoreError, account_dirs, find_store_root

ICLOUD_ROOT = Path("~/Library/Mobile Documents").expanduser()
RUNNER = Path("~/.agentinbox/bin/agentinbox-runner").expanduser()

KICKSTART = "launchctl kickstart -k gui/$(id -u)/com.agentinbox.export"


def find_calexport(cfg: Config) -> Path | None:
    if cfg.calexport_bin and cfg.calexport_bin.exists():
        return cfg.calexport_bin
    default = Path("~/.agentinbox/bin/calexport").expanduser()
    if default.exists():
        return default
    which = shutil.which("calexport")
    return Path(which) if which else None


def output_dir_in_icloud(output_dir: Path) -> bool:
    try:
        resolved = output_dir.resolve()
    except OSError:
        return False
    return str(resolved).startswith(str(ICLOUD_ROOT.resolve()))


def _read_manifest(cfg: Config) -> dict | None:
    from . import safefs
    raw = safefs.read_file(cfg.output_dir, "manifest.json")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def run_doctor(cfg: Config) -> int:
    ok = True

    def check(passed: bool, label: str, fix: str = "") -> None:
        nonlocal ok
        mark = "ok " if passed else "FAIL"
        print(f"[{mark}] {label}")
        if not passed:
            ok = False
            if fix:
                print(f"       -> {fix}")

    def note(label: str) -> None:
        print(f"[note] {label}")

    manifest = _read_manifest(cfg)

    # Interpreter basics. Python needs no TCC grants under the launcher model,
    # so a symlinked venv is fine - grants belong to the runner alone.
    check(sys.version_info >= (3, 11), f"python >= 3.11 (running {sys.version.split()[0]})",
          "install Python 3.11+ and recreate the venv")
    exe = Path(sys.executable)
    if exe.is_symlink():
        note(f"venv python is a symlink to {exe.resolve()} - fine: python holds "
             "no TCC grants; the launcher does")

    # Launcher binary: the one and only TCC grant target.
    check(RUNNER.exists(), f"launcher binary present ({RUNNER})",
          "run ./install.sh to build and sign agentinbox-runner")
    if RUNNER.exists():
        r = subprocess.run(["codesign", "--verify", "--strict", str(RUNNER)],
                           capture_output=True, text=True)
        check(r.returncode == 0, "launcher binary signature valid",
              "re-run ./install.sh to rebuild and re-sign it")

    # Full Disk Access. Direct read succeeds only if THIS process is granted
    # (it should not be); otherwise verify via the last launchd run.
    direct_root = None
    try:
        direct_root = find_store_root()
    except StoreError:
        pass
    if direct_root is not None:
        note("Mail store readable directly from this terminal - expected only "
             "if you granted FDA to a python binary or this terminal; the "
             "launcher model needs neither")
        check(True, f"Mail store found: {direct_root}")
        accounts = discover_accounts(direct_root, cfg.mail.accounts)
        dirs = account_dirs(direct_root)
        unmapped = [d.name for d in dirs if d.name not in accounts]
        check(not unmapped,
              f"all account dirs mapped to names/emails ({len(accounts)} mapped)",
              f"add [mail.accounts] entries for: {', '.join(unmapped)}")
    else:
        mail_errors = [e for e in (manifest or {}).get("errors", [])
                       if "Full Disk Access" in e or "parse failed" in e]
        fda_errors = [e for e in mail_errors if "Full Disk Access" in e]
        mail_count = (manifest or {}).get("mail", {}).get("count", 0)
        verified = manifest is not None and mail_count > 0 and not fda_errors
        check(verified,
              f"Mail export works under launchd "
              f"(last run: {mail_count} messages)" if manifest else
              "Mail export works under launchd (no run recorded yet)",
              f"grant Full Disk Access to {RUNNER} in System Settings > "
              f"Privacy & Security > Full Disk Access, then run: {KICKSTART}")

    # Calendar access. --check-access from a terminal reflects the terminal's
    # attribution, so treat manifest evidence as authoritative when present.
    cal = find_calexport(cfg)
    check(cal is not None, f"calexport binary present ({cal or 'not found'})",
          "build it: calexport/build.sh, or set calexport_bin in config.toml")
    cal_manifest = (manifest or {}).get("calendar", {})
    cal_errors = [e for e in (manifest or {}).get("errors", [])
                  if "calexport" in e or "calendar access" in e]
    if cal_manifest.get("calendars") and not cal_errors:
        check(True, f"calendar export works under launchd (last run: "
                    f"{cal_manifest.get('count', 0)} events from "
                    f"{len(cal_manifest['calendars'])} calendar(s))")
    elif cal:
        r = subprocess.run([str(cal), "--check-access"], capture_output=True,
                           text=True, timeout=30)
        check(r.returncode == 0, "calendar access granted",
              (r.stderr or r.stdout).strip()
              or f"trigger the first export and approve the prompt: {KICKSTART}")

    # Has the user chosen a home calendar (one that actually exported)?
    if manifest is not None:
        home = cal_manifest.get("home") or []
        exported = cal_manifest.get("calendars") or []
        check(bool(home) and any(t in exported for t in home),
              f"home calendar chosen ({', '.join(home) or 'none'})",
              "open the settings page and set your Home calendar: "
              "~/.agentinbox/venv/bin/agentinbox config-ui")

    # Fetch service (WatchPaths agent) loaded?
    r = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/com.agentinbox.fetch"],
        capture_output=True,
    )
    check(r.returncode == 0, "fetch service (com.agentinbox.fetch) loaded",
          "run `agentinbox install` to (re)install the launchd agents")

    # Settings page job: loaded, and NOT running through the launcher (it
    # must never inherit Full Disk Access).
    r = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/com.agentinbox.configui"],
        capture_output=True, text=True,
    )
    check(r.returncode == 0, "settings page job (com.agentinbox.configui) loaded",
          "run `agentinbox install` to (re)install the launchd agents")
    if r.returncode == 0:
        check("agentinbox-runner" not in r.stdout,
              "settings page job runs without Full Disk Access",
              "reinstall with `agentinbox install`; the settings page must not run "
              "through agentinbox-runner")

    # Output dir constraints.
    check(not output_dir_in_icloud(cfg.output_dir),
          f"output dir outside iCloud Drive ({cfg.output_dir})",
          "set output_dir in config.toml to a non-synced location")
    if cfg.output_dir.exists():
        mode = cfg.output_dir.stat().st_mode & 0o777
        check(mode == 0o700, f"output dir permissions 0700 (found {oct(mode)})",
              f"chmod 700 {cfg.output_dir}")

    # Staleness: is Mail.app actually syncing? (manifest-based)
    if manifest is not None:
        newest = manifest.get("mail", {}).get("newest_source_mtime")
        if newest:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(newest)).total_seconds()
            check(age < STALE_INTERVALS * cfg.interval_seconds,
                  f"Mail store is fresh (newest message {int(age / 60)} min old "
                  "at last run)",
                  "Mail.app must stay running (add it as a login item); macOS "
                  "Mail does not sync in the background")

    print()
    print("doctor: all checks passed" if ok else "doctor: FAILING - fix the items above")
    return 0 if ok else 1
