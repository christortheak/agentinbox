#!/bin/bash
# AgentInbox installer. Safe to re-run: it rebuilds and reinstalls, and never
# overwrites an existing ~/.config/agentinbox/config.toml.
#
#   ./install.sh                       # auto-detects a Developer ID, else ad-hoc
#   CODESIGN_IDENTITY="Developer ID Application: Name (TEAM)" ./install.sh
#   PYTHON=/path/to/python3.12 ./install.sh
set -euo pipefail
cd "$(dirname "$0")"

VENV="$HOME/.agentinbox/venv"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "error: AgentInbox runs on macOS only." >&2; exit 1
fi
if ! xcode-select -p >/dev/null 2>&1; then
    echo "error: Xcode Command Line Tools are required: xcode-select --install" >&2; exit 1
fi

# 1. Find a Python >= 3.11 (tomllib). System /usr/bin/python3 is usually 3.9.
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for cand in python3.13 python3.12 python3.11 python3; do
        if command -v "$cand" >/dev/null 2>&1; then
            if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; then
                PY=$(command -v "$cand"); break
            fi
        fi
    done
fi
if [ -z "$PY" ]; then
    echo "error: no Python >= 3.11 found. Install one (python.org, Homebrew or uv) and" >&2
    echo "rerun, or set PYTHON=/path/to/python3." >&2
    exit 1
fi
echo "using $PY ($($PY --version))"

# 2. Signing identity. A stable Developer ID keeps macOS privacy grants across
# rebuilds; ad-hoc works, but each rebuild needs the grants again.
IDENTITY="${CODESIGN_IDENTITY:-}"
if [ -z "$IDENTITY" ]; then
    IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null \
        | grep -o '"Developer ID Application: [^"]*"' | head -1 | tr -d '"') || true
fi
if [ -z "$IDENTITY" ]; then
    IDENTITY="-"
    echo "note: no Developer ID certificate found - signing ad-hoc. Works fine;"
    echo "      after any future rebuild, re-grant Full Disk Access and approve"
    echo "      the Calendar/Reminders prompts again."
else
    echo "signing with: $IDENTITY"
fi

# 3. Python environment. Python itself never gets privacy grants (they go on
# the launcher below), so a plain venv is fine.
rm -rf "$VENV"
"$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install -q --upgrade pip
"$VENV/bin/python" -m pip install -q ./mailexport
echo "installed mailexport -> $VENV"

# 4. Calendar/Reminders tool (Swift, EventKit).
./calexport/build.sh "$IDENTITY"

# 5. The launcher - the ONLY binary that gets privacy grants. It runs the fixed
# agentinbox entry point and nothing else.
mkdir -p "$HOME/.agentinbox/bin"
TMPBIN=$(mktemp -t agentinbox-runner)
clang -O2 -Wall -DAGENTINBOX_BIN="\"$VENV/bin/agentinbox\"" \
    -Wl,-sectcreate,__TEXT,__info_plist,launcher/Info.plist \
    launcher/runner.c -o "$TMPBIN"
codesign --force --options runtime \
    --identifier com.agentinbox.runner \
    --entitlements launcher/entitlements.plist \
    --sign "$IDENTITY" "$TMPBIN"
codesign --verify --strict "$TMPBIN"
rm -f "$HOME/.agentinbox/bin/agentinbox-runner"
mv "$TMPBIN" "$HOME/.agentinbox/bin/agentinbox-runner"
chmod 755 "$HOME/.agentinbox/bin/agentinbox-runner"
echo "installed launcher: $HOME/.agentinbox/bin/agentinbox-runner"

# 6. Config (only if there isn't one), with this Mac's time zone.
CONF="$HOME/.config/agentinbox/config.toml"
if [ ! -f "$CONF" ]; then
    mkdir -p "$(dirname "$CONF")"
    TZNAME=$(readlink /etc/localtime 2>/dev/null | sed 's#.*/zoneinfo/##')
    if [ -n "$TZNAME" ]; then
        sed "s#^timezone = .*#timezone = \"$TZNAME\"#" config/config.example.toml > "$CONF"
    else
        cp config/config.example.toml "$CONF"
    fi
    chmod 600 "$CONF"
    echo "created $CONF (time zone: ${TZNAME:-America/New_York})"
fi

# Keep the agent's folder out of this repository: exported mail must never be
# committable, and the inbox agent must not pick up this repo's CLAUDE.md
# (Claude Code reads CLAUDE.md files from parent folders).
OUT=$("$VENV/bin/python" -c 'from mailexport.config import load_config; print(load_config().output_dir)')
REPO=$(pwd -P)
case "$OUT/" in
    "$REPO"/*)
        echo
        echo "WARNING: the agent's folder ($OUT) is inside this repository ($REPO)."
        echo "         Your exported mail could end up in a git commit, and your inbox"
        echo "         agent would read this repository's setup instructions. Move the"
        echo "         repository somewhere else (e.g. ~/Developer/agentinbox) and re-run."
        echo
        ;;
esac

# 7. launchd jobs, then a first health check (some items fail until the
# steps below are done - that's expected).
"$VENV/bin/agentinbox" install
echo
"$VENV/bin/agentinbox" doctor || true

cat <<EOT

Next steps (these need you):

  1. Grant Full Disk Access to the launcher - and to nothing else:
       $HOME/.agentinbox/bin/agentinbox-runner
     System Settings > Privacy & Security > Full Disk Access > "+",
     then press Cmd+Shift+G and paste the path above.

  2. Start the first export through launchd (not from a terminal, so the
     permission prompts attach to the exporter):
       launchctl kickstart -k gui/\$(id -u)/com.agentinbox.export
     Approve the Calendar and Reminders prompts. The first run takes a few minutes.

  3. Choose your calendars and check your mailboxes:
       $VENV/bin/agentinbox config-ui

  4. In Calendar > Settings > General, set Default Calendar to your home
     calendar, so meeting proposals you open land there.

  5. Add Mail to Login Items (System Settings > General > Login Items).
     The export only stays fresh while Mail is running.

  6. Check everything:
       $VENV/bin/agentinbox doctor

  7. Point your agent at ~/AgentInbox/Inbox only (see README.md).
EOT
