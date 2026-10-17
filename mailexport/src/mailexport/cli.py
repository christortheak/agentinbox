"""`agentinbox` CLI: run / doctor / attachment / install / uninstall."""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import DEFAULT_CONFIG_PATH, load_config
from .doctor import output_dir_in_icloud, run_doctor
from .lock import LockHeld

from .labels import CONFIGUI_LABEL, EXPORT_LABEL as LAUNCHD_LABEL, FETCH_LABEL

PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{agentinbox}</string>
    <string>run</string>
    <string>--once</string>
  </array>
  <key>StartInterval</key><integer>{interval}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{logdir}/launchd.out.log</string>
  <key>StandardErrorPath</key><string>{logdir}/launchd.err.log</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
"""

# WatchPaths fetch service: launchd runs the worker (as its own TCC-responsible
# process, so FDA applies) whenever the agent drops a file into requests/.
FETCH_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{agentinbox}</string>
    <string>fetch-worker</string>
  </array>
  <key>WatchPaths</key>
  <array>
    <string>{requests_dir}</string>
  </array>
  <key>RunAtLoad</key><false/>
  <key>ThrottleInterval</key><integer>2</integer>
  <key>StandardOutPath</key><string>{logdir}/fetch.out.log</string>
  <key>StandardErrorPath</key><string>{logdir}/fetch.err.log</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
"""


def _setup_logging(cfg) -> None:
    # Logs live in ~/.agentinbox/logs, never in the agent's folder: the
    # exporter must not append to paths the agent can redirect.
    logdir = cfg.log_dir
    logdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = logging.handlers.RotatingFileHandler(
        logdir / "agentinbox.log", maxBytes=5 * 1024 * 1024, backupCount=3
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler(sys.stderr))


def cmd_run(cfg, args) -> int:
    if output_dir_in_icloud(cfg.output_dir):
        print(f"refusing to run: output dir {cfg.output_dir} resolves inside "
              "iCloud Drive; change output_dir in config.toml", file=sys.stderr)
        return 2
    _setup_logging(cfg)
    log = logging.getLogger("agentinbox")
    from .pipeline import perform_export
    try:
        result = perform_export(cfg, mail_library=getattr(args, "mail_library", None))
        if result["mail_error"]:
            print(f"mail export failed: {result['mail_error']}", file=sys.stderr)
        return result["exit_code"]
    except LockHeld:
        log.info("another run holds the lock; skipping")
        return 0


def cmd_fetch_worker(cfg, args) -> int:
    from .worker import process_requests

    _setup_logging(cfg)
    handled = process_requests(cfg)
    logging.getLogger("fetchworker").info("handled %d request(s)", handled)
    return 0


def cmd_attachment(cfg, args) -> int:
    from .attach import AttachError, fetch_attachment, list_message_attachments

    _setup_logging(cfg)
    try:
        if args.list:
            atts = list_message_attachments(cfg, args.message_id)
            if not atts:
                print("no attachments")
                return 0
            for a in atts:
                size = f"{a['size']}" if a["size"] is not None else "?"
                print(f"{a['index']}\t{a['filename']}\t{a['mime']}\t{size}")
            return 0
        dest = fetch_attachment(
            cfg, args.message_id, index=args.index, filename=args.filename
        )
        print(dest)
        return 0
    except AttachError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


# The settings page. NOT run through the launcher: it must hold no TCC grants,
# and as its own launchd job it doesn't inherit the fetch worker's either.
# No RunAtLoad and no triggers: it starts only via `launchctl kickstart`
# (the agent's open-settings request). Logs live outside the agent's folder,
# and --no-print-link keeps the secret link out of them anyway.
CONFIGUI_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{agentinbox}</string>
    <string>config-ui</string>
    <string>--no-print-link</string>
  </array>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>{logdir}/configui.out.log</string>
  <key>StandardErrorPath</key><string>{logdir}/configui.err.log</string>
  <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
"""


def _python_entry_point() -> Path:
    """The venv's own `agentinbox` script (python, no grants) - never the runner."""
    return Path(sys.prefix) / "bin" / "agentinbox"


def _plist_path(label: str) -> Path:
    return Path("~/Library/LaunchAgents").expanduser() / f"{label}.plist"


def _bootstrap(path: Path) -> str | None:
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)],
                   capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)],
                       capture_output=True, text=True)
    return None if r.returncode == 0 else r.stderr.strip()


def cmd_install(cfg, args) -> int:
    from .output import ensure_layout
    ensure_layout(cfg.output_dir)
    # The launchd jobs run through the signed launcher when present, so TCC
    # grants attach to it alone and python holds no grants.
    runner = Path("~/.agentinbox/bin/agentinbox-runner").expanduser()
    if runner.exists():
        agentinbox_bin = runner
    else:
        agentinbox_bin = Path(sys.argv[0]).resolve()
        print("warning: launcher binary not found; launchd jobs will run python "
              "directly, which requires granting Full Disk Access to the python "
              "binary itself. Run ./install.sh to build the launcher.",
              file=sys.stderr)
    logdir = cfg.log_dir
    logdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    plists = {
        LAUNCHD_LABEL: PLIST_TEMPLATE.format(
            label=LAUNCHD_LABEL, agentinbox=agentinbox_bin,
            interval=cfg.interval_seconds, logdir=logdir),
        FETCH_LABEL: FETCH_PLIST_TEMPLATE.format(
            label=FETCH_LABEL, agentinbox=agentinbox_bin,
            requests_dir=cfg.output_dir / "requests", logdir=logdir),
        CONFIGUI_LABEL: CONFIGUI_PLIST_TEMPLATE.format(
            label=CONFIGUI_LABEL, agentinbox=_python_entry_point(),
            logdir=logdir),
    }
    for label, content in plists.items():
        path = _plist_path(label)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        err = _bootstrap(path)
        if err:
            print(f"launchctl bootstrap failed for {label}: {err}", file=sys.stderr)
            return 1
        print(f"installed {label}: {path}")
    print(f"export runs every {cfg.interval_seconds}s; fetch service watches "
          f"{cfg.output_dir / 'requests'}")
    print("now run: agentinbox doctor")
    return 0


def cmd_uninstall(cfg, args) -> int:
    uid = os.getuid()
    for label in (LAUNCHD_LABEL, FETCH_LABEL, CONFIGUI_LABEL):
        path = _plist_path(label)
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)],
                       capture_output=True)
        if path.exists():
            path.unlink()
        print(f"removed {label}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentinbox")
    parser.add_argument("--config", type=Path, default=None,
                        help=f"config file (default {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run the export now")
    p_run.add_argument("--once", action="store_true", help="(default behavior)")
    p_run.add_argument("--mail-library", type=Path, default=None,
                       help=argparse.SUPPRESS)  # testing: fake ~/Library/Mail

    sub.add_parser("doctor", help="check permissions and environment")

    p_att = sub.add_parser("attachment", help="list or fetch one attachment")
    p_att.add_argument("message_id")
    g = p_att.add_mutually_exclusive_group()
    g.add_argument("--list", action="store_true")
    g.add_argument("--index", type=int)
    g.add_argument("--filename")

    sub.add_parser("install", help="install the launchd agents")
    sub.add_parser("uninstall", help="remove the launchd agents")
    sub.add_parser("fetch-worker", help="service pending request files (run by launchd)")

    p_ui = sub.add_parser("config-ui", help="open the settings page in your browser")
    p_ui.add_argument("--no-open", action="store_true", help="print the link only")
    p_ui.add_argument("--port", type=int, default=0, help=argparse.SUPPRESS)
    p_ui.add_argument("--idle-minutes", type=int, default=15, help=argparse.SUPPRESS)
    p_ui.add_argument("--no-print-link", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    if args.command == "run":
        return cmd_run(cfg, args)
    if args.command == "doctor":
        return run_doctor(cfg)
    if args.command == "attachment":
        return cmd_attachment(cfg, args)
    if args.command == "install":
        return cmd_install(cfg, args)
    if args.command == "uninstall":
        return cmd_uninstall(cfg, args)
    if args.command == "fetch-worker":
        return cmd_fetch_worker(cfg, args)
    if args.command == "config-ui":
        # The only network listener in AgentInbox: imported here and nowhere else.
        from .configui import run
        return run(args.config, open_browser=not args.no_open, port=args.port,
                   idle_minutes=args.idle_minutes, print_link=not args.no_print_link)
    return 2


if __name__ == "__main__":
    sys.exit(main())
