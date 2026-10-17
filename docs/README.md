# AgentInbox — Setup and Operations (for humans)

Read-only mail & calendar export per [agent-inbox-spec.md](../agent-inbox-spec.md).
This README covers installing it, which privacy grants go where, and how to
verify the read-only design.

## Install

```bash
chmod +x install.sh && ./install.sh
```

That creates `~/.agentinbox/venv`, installs the `agentinbox` CLI, builds and
signs `calexport` and the launcher (with your Developer ID if you have one,
otherwise ad-hoc), writes `~/.config/agentinbox/config.toml` with your time zone
if it doesn't exist, and installs the three launchd jobs. With ad-hoc signing,
any future rebuild means redoing steps 1 and 2 below.

Then, in order:

1. **Full Disk Access** → grant to `~/.agentinbox/bin/agentinbox-runner` only.
   System Settings → Privacy & Security → Full Disk Access → **+** →
   Cmd+Shift+G → paste the path. Do **not** grant it to Terminal, Claude, or
   any python binary — python holds no TCC grants in this design.
2. **Calendar access** → trigger the first export *via launchd* — running it
   from a terminal would attribute the TCC grant to the terminal app instead
   of the exporter:
   ```bash
   launchctl kickstart -k gui/$(id -u)/com.agentinbox.export
   ```
   Approve the "Full Calendar Access" prompt when it appears. The tool only
   ever calls read APIs; macOS 14+ simply has no read-only tier.
3. `agentinbox doctor` → everything should be green, including the
   "interpreter is a real copy" check and the iCloud-Drive check.
4. **Keep Mail.app running.** macOS Mail does not sync when closed, and its
   junk filter is client-side. Add Mail to Login Items; it can stay hidden.
   If it quits, `manifest.json` flips `stale: true` within a few intervals.

## TCC grant map — the launcher model

| Binary | Grant | Why |
|---|---|---|
| `~/.agentinbox/bin/agentinbox-runner` | Full Disk Access + Calendars + Reminders | sole grant holder |
| python (any), `calexport` | **none** | children inherit via the runner |
| everything else | none | — |

`agentinbox-runner` is a ~50-line signed C program that forks and execs the
fixed `agentinbox` entry point and nothing else. TCC authorizes reads against
the *responsible process*: when launchd runs a job whose executable is the
runner, the python/calexport children inherit its grants. Run from anywhere
else, responsibility falls to that caller's host app (no grants) — so
"author a python script and run it" yields nothing, and even running the
runner directly yields nothing. The grants are only usable through the
launchd jobs, which only execute the exporter.

Consequence: manual exports go through launchd too —
`launchctl kickstart -k gui/$(id -u)/com.agentinbox.export` — and on-demand
actions through the `requests/` mailbox. `agentinbox run --once` from a bare
terminal will fail FDA by design (doctor explains this).

## Verifying the write surface

- `grep -rlnE "^(import|from) (smtplib|socket|http|urllib|requests)" mailexport/src/`
  lists only `configui.py` (the settings page, bound to localhost): the
  exporter, worker and pipeline have no network code. Calendar links are built
  with a local percent-encoder, not urllib.
- Every file operation in the agent's folder goes through `safefs.py`, which
  never follows a symlink and never reads a FIFO; the exporter can't be tricked
  into touching files outside the folder.
- `grep -n "store.save\|store.remove" calexport/Sources/calexport/main.swift`
  shows exactly two `store.save` calls (reminder-add, reminder-complete) and
  no `store.remove`. Calendars and mail have no write paths at all.
- Everything the agent can cause is listed in the worker's docstring
  (`mailexport/src/mailexport/worker.py`): files in its own folder,
  reminder create/complete, pins, and bounded preferences.
- Worker-trusted state (the message-id to file map, pins, preferences, run
  lock, calendar metadata) lives in `~/.agentinbox/`, outside the agent's
  folder. Cowork can write anywhere in its folder, so nothing the worker
  trusts may live there.

## Meetings, proposals and preferences

- **Retention.** Mail is kept for `mail.days` (90). Emails carrying an invite
  for a future meeting are kept until the meeting passes, and the agent can
  pin a plain-text scheduling email until a date. Each message's `retained`
  field says why it's still there.
- **Invite cross-check.** Every run parses invites in exported mail and
  compares them with your calendars by iCalendar UID, else by time and title.
  Results: `calendar/invites.json` and `calendar/unscheduled.md`.
- **Your calendar.** The `calendar.include` calendars are where everything should end up. A meeting found only on another calendar counts as "elsewhere" and still gets proposed. Calendar.app puts imported events in its default calendar, so set Calendar > Settings > General > Default Calendar to your calendar; Google/Outlook links can't land in an iCloud calendar.
- **Duplicate-check calendars.** `calendar.lookup` lists calendars exported
  as title/time only, so the agent can tell whether a meeting is booked
  without seeing those calendars' details. Put the calendars where accepted
  invites land here (typically your Exchange/Google account calendars).
  `manifest.json` → `calendar.available` lists every calendar's exact title.
- **Proposals.** The agent can request a click-to-add event. The `.ics` file
  carries no attendees and uses METHOD:PUBLISH, so opening it can never send
  an invitation or RSVP; Calendar asks which calendar to add it to. Google and
  Outlook links are opt-in and send the details to that provider when clicked.
- **Preferences.** The agent may change `mail_days`, `calendar_future_days`
  (7-365), `reminders_completed_days` (0-30) and `calendar_link_format`
  when you ask it to. They're stored in `~/.agentinbox/preferences.json` and
  override this config; `manifest.json` shows each value and its source.
  Set `[preferences] allow_agent_changes = false` to turn this off. Delete the
  preferences file to reset everything to your config.
- **Mailboxes stay yours.** The agent can't add mailboxes, but it can run the
  `mailboxes` diagnostic and tell you the exact name to add to
  `mail.mailboxes`.

## Settings page

```bash
~/.agentinbox/venv/bin/agentinbox config-ui
```

Opens a local page in your browser for the settings you'd otherwise edit by
hand:

- Every calendar from the last export, each set to Home, Shared, Following,
  Duplicate check, or Off.
- Calendar window, proposal link type, mail days, mailboxes and excluded
  accounts. "Load mailbox names from Mail" asks the export service for your
  real mailbox names.
- Reminder permissions and attachment limits.
- What the agent has changed, with reset buttons.

You can also ask the agent to open it ("open my settings"). The agent sends an
`open-settings` request and the page pops open in your browser on your Mac.
The agent never sees the page or its link, so it still can't change anything
there. It opens at most one page at a time, with a two-minute cooldown, and
`[settings_page] agent_may_open = false` (or the checkbox on the page) turns
this off.

It's the only part of AgentInbox that opens a network port, so it's guarded:
it listens only on your Mac, needs the secret link it prints, refuses requests
from other websites, and closes when you click Done or after 15 idle minutes.
It can't change the output folder, binaries, account mapping or schedule.
Every save keeps the previous config as `config.toml.bak-<time>`; your own
comments in the file are replaced with standard ones.

## Day-to-day

- `launchctl kickstart -k gui/$(id -u)/com.agentinbox.export` — run an export
  now. (A bare `agentinbox run --once` from a terminal has no Full Disk Access
  by design; see the TCC section.)
- `agentinbox doctor` — health check.
- `agentinbox fetch-worker` — drain pending request files (normally run by
  launchd via WatchPaths).
- `agentinbox uninstall` — remove both launchd agents.
- Logs: `~/.agentinbox/logs/agentinbox.log` (rotated at 5 MB). Logs stay out of
  the agent's folder on purpose.

## Agent-side setup

Scope the agent to the **output directory only** (`~/AgentInbox/Inbox`) — the
source repo above it stays invisible, and the agent needs **no execute
permission at all**. On-demand actions go through the request mailbox: the
agent writes a JSON file into `Inbox/requests/`, the `com.agentinbox.fetch`
launchd service (WatchPaths) runs the worker, and the answer appears in
`Inbox/results/` within seconds.

Why not just let the agent run the CLI? TCC attributes Full Disk Access to
the *responsible process* — for an agent-spawned command that's the host app
(Terminal, Claude), which deliberately has no FDA, so the fetch would be
denied. launchd-launched workers are their own responsible process, so the
venv python's grant applies.

- **Cowork**: create a project whose only folder is `~/AgentInbox/Inbox/`.
  Disable all connectors and browser access. Add a scheduled morning-brief
  task using [brief-prompt.md](brief-prompt.md).
- **Claude Code**: run with `~/AgentInbox/Inbox` as the working directory,
  sandboxed to read/write there only, network denied.

The generated `Inbox/README.md` tells the agent the export is read-only,
documents the request protocol, and that message/event/attachment content is
untrusted data.
