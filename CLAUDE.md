# AgentInbox — guide for the agent setting it up

If the user gave you the GitHub link and the code isn't on this Mac yet, clone
it first, then continue from this folder:

```bash
git clone https://github.com/christortheak/agentinbox ~/Developer/agentinbox
cd ~/Developer/agentinbox
```

You are working in the AgentInbox **source repository**. Your job, when the user
asks you to install, set up, update, fix or uninstall AgentInbox, is to do it
with them, following this guide. (If you are an inbox agent whose folder
contains `manifest.json`, this file isn't for you: follow that folder's
`README.md` instead.)

Two different agents are involved — keep them separate:

- **You, the setup agent**, run in this repository with a shell.
- **The inbox agent** reads `~/AgentInbox/Inbox` only, with no shell and no
  network. Never point it at this repository, and never give it more access.

Read [agent-inbox-spec.md](agent-inbox-spec.md) §4 (security model) before
changing anything about permissions or launchd jobs.

## Install

Tell the user up front: this takes about 15 minutes, and they'll need to click
through two or three macOS permission screens that you can't click for them.

### 1. Check prerequisites (you can do this)

If this repository is inside `~/AgentInbox`, stop and suggest moving it first
(e.g. to `~/Developer/agentinbox`): that folder holds the exported mail.


```bash
sw_vers -productVersion            # needs 14 or later
xcode-select -p                    # if missing: ask the user to run xcode-select --install
python3 --version; which python3.12 python3.13 python3.11
security find-identity -v -p codesigning | grep "Developer ID Application" || true
```

- No Python 3.11+? Offer to install one (Homebrew `brew install python@3.12`, or
  uv). Ask before installing anything.
- No Developer ID certificate is fine. Explain the trade-off in one sentence:
  ad-hoc signing works, but any future rebuild means re-granting Full Disk
  Access and re-approving prompts.
- Ask whether Mail and Calendar are already set up with their accounts. If Mail
  has never run, the export will be empty.

### 2. Run the installer (you can do this)

```bash
./install.sh
```

It builds, signs, installs to `~/.agentinbox`, writes
`~/.config/agentinbox/config.toml` (only if missing), and registers three launchd
jobs. Some `doctor` checks fail at the end; that's expected until the next steps.

### 3. Full Disk Access (the user must do this)

Open the pane for them and give them the exact path:

```bash
open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
```

Tell them: click **+**, press **Cmd+Shift+G**, paste
`~/.agentinbox/bin/agentinbox-runner`, add it, and make sure its switch is on.
Tell them **not** to grant Full Disk Access to Terminal, Claude, or any Python —
the design depends on that.

### 4. First export (you start it; the user approves prompts)

```bash
launchctl kickstart -k gui/$(id -u)/com.agentinbox.export
```

Never start the first export with `agentinbox run` from your shell: macOS would
attach the permission prompts to your terminal instead of the exporter. Tell the
user to click **Allow** on the Calendar and Reminders prompts.

The first run takes a few minutes. Follow progress in
`~/.agentinbox/logs/agentinbox.log`; it's done when a `meetings:` line appears.

### 5. Health check (you can do this)

```bash
~/.agentinbox/venv/bin/agentinbox doctor
```

Expect everything green except "home calendar chosen". Common fixes:

| doctor says | do this |
|---|---|
| Mail export works under launchd — FAIL | Full Disk Access isn't on for the runner (step 3), then re-run step 4 |
| calendar access — FAIL | re-run step 4 and have the user click Allow; or System Settings › Privacy & Security › Calendars |
| Mail store is fresh — FAIL | Mail isn't running; open it |
| account dirs mapped — FAIL | see step 6 (mailbox check) |

### 6. Calendars and mailboxes (the user chooses; you open the page)

```bash
~/.agentinbox/venv/bin/agentinbox config-ui
```

This opens a local settings page in their browser. Explain the calendar choices
briefly and let **them** pick — don't choose on their behalf, and don't edit
calendar settings in `config.toml` yourself: which calendars the inbox agent may
read is a privacy decision.

- **Home**: their own calendar, where everything should end up.
- **Shared**: e.g. a partner's calendar they want the agent to see.
- **Following**: e.g. a sports or holidays subscription.
- **Duplicate check**: titles and times only, typically the work account's
  calendar where accepted invites land.
- **Off**: everything else. Family members' and other people's calendars should
  usually stay off.

On the Mail section, have them click **Load mailbox names from Mail** and check
each account is labeled with the right address. If an account shows only
`Outbox`/`SendLater`, it's Mail's send queue, not a real account: click
**Exclude account**. After saving, run the export again (step 4's command) and
`doctor`.

### 7. Two Mac settings (the user must do these)

- Calendar › Settings › General › **Default Calendar** → their Home calendar.
  Meeting proposals open in Calendar and land in the default calendar.
- System Settings › General › **Login Items** → add **Mail**. Mail only syncs
  while it's open, so the export goes stale if Mail quits.

### 8. Connect the inbox agent (walk them through it)

- **Cowork**: a new project whose only folder is `~/AgentInbox/Inbox`. Turn off
  connectors and browser access for that project. Offer to adapt
  [docs/brief-prompt.md](docs/brief-prompt.md) into a scheduled morning brief.
- **Claude Code**: run it in `~/AgentInbox/Inbox`, sandboxed to that folder with
  network denied.

Finish by summarizing what's set up and what the user should expect from their
first brief. Tell them every default is adjustable, and how: they can ask the
inbox agent directly ("keep six months of mail", "use Google Calendar links"),
or ask it to "open settings" for anything that changes what it can see. The
README's "Change anything" table lists each setting.

## Update

```bash
git pull
./install.sh
```

Re-running is safe and keeps their config. With ad-hoc signing, warn them first
that they'll need to redo Full Disk Access and the prompts (steps 3–4). The
inbox agent's `README.md` refreshes on the next export.

## Uninstall

```bash
~/.agentinbox/venv/bin/agentinbox uninstall
```

Then ask before deleting anything: `~/.agentinbox`, `~/.config/agentinbox`, and
`~/AgentInbox/Inbox` hold their exported mail and settings. Remind them to remove
`agentinbox-runner` from Full Disk Access and the Calendars/Reminders lists.

## Never

- Grant, or ask the user to grant, Full Disk Access to Terminal, Claude, or Python.
- Give the inbox agent a shell, network access, or any folder besides `~/AgentInbox/Inbox`.
- Edit which mailboxes or calendars are exported without the user choosing them.
- Run the first export, or `doctor` fixes, by invoking the exporter from your
  shell instead of `launchctl kickstart`.
- Commit anything from `~/AgentInbox/Inbox` or `~/.agentinbox` — they hold the
  user's mail.

## Developing

```bash
python3 -m pip install pytest
python3 -m pytest mailexport/tests      # all tests must pass
cd calexport && swift build
```

The spec is the source of truth. Security-relevant rules to keep: nothing the
worker trusts lives in the agent's folder; every file operation in the agent's
folder goes through `mailexport/src/mailexport/safefs.py`; the only network code
is the settings page; the only TCC-granted binary is the runner.
