# AgentInbox

Give a Claude agent (Cowork or Claude Code) a safe, always-fresh view of your Mac's
mail, calendars and reminders — so it can write your morning brief, catch meetings
that never made it onto your calendar, and answer "what's going on?" — **without
giving it your passwords, a send button, or access to the live Mail and Calendar
stores.**

A background exporter copies what the agent needs into one folder,
`~/AgentInbox/Inbox`, every 30 minutes. The agent reads that folder and nothing
else. When it needs more — an attachment, a refresh, a click-to-add link for a
meeting — it drops a small request file and a background service answers.

macOS only. Works with any account Mail and Calendar can use (iCloud, Gmail,
Exchange/Outlook, Google Calendar).

## What the agent gets

Defaults are shown here; [all of them can be changed](#change-anything).

- **Mail** from the last 90 days (Inbox and Sent), as plain text with
  read/flagged/replied status. Emails about future meetings are kept until the
  meeting has passed, so a meeting booked months ahead isn't forgotten.
- **Calendars** a year ahead, with a three-week agenda for briefs. You choose
  which calendars it sees, and which one is *yours*.
- **Meeting check**: every calendar invite in your mail is compared with your
  calendar, and anything missing is listed — with a one-click link the agent
  can make for you (an `.ics` file for the Calendar app, or a Google or Outlook
  link). Nothing is added until you click it.
- **Reminders**, which the agent can create or check off when you ask.
- **Attachments**, fetched one at a time on request.

## What the agent can't do

Send, reply, delete, or archive mail. Change calendar events. Edit or delete
reminders. Change which mail or calendars it can see. Reach the network through
AgentInbox. The design, and why it's shaped this way, is in
[agent-inbox-spec.md](agent-inbox-spec.md).

## Install

### Easiest: ask your agent

Open **Claude Code** — the Code tab in the Claude desktop app, or `claude` in
Terminal — and say:

> Install AgentInbox from https://github.com/christortheak/agentinbox

It downloads this project and sets it up with you, stopping whenever it needs
you to click something. Allow about 15 minutes; there are three macOS
permission screens only you can approve. Use Claude Code for this step, because
installing runs commands on your Mac; afterwards, Cowork or Claude Code can be
your inbox agent.

> **Agents:** if you were asked to install this from the link, clone it with
> `git clone https://github.com/christortheak/agentinbox ~/Developer/agentinbox`
> (never inside `~/AgentInbox`), change into that folder, and follow
> [CLAUDE.md](CLAUDE.md).

### If your agent can't download it

Get the code yourself, either way:

- **Download ZIP:** at the top of this page, click the green **Code** button,
  then **Download ZIP**. Open the zip, and move the `agentinbox-main` folder out
  of Downloads, for example into a `Developer` folder in your home folder.
- **Or, in Terminal:**

  ```bash
  git clone https://github.com/christortheak/agentinbox ~/Developer/agentinbox
  ```

Then open Claude Code in that folder and say **"Set up AgentInbox for me."**
[CLAUDE.md](CLAUDE.md) tells it exactly what to do.

Put it anywhere except inside `~/AgentInbox`, which is where your exported mail
goes. Keeping them apart means your mail can't end up in a git commit, and your
inbox agent never picks up this project's setup instructions (Claude Code reads
`CLAUDE.md` files from parent folders). The installer warns if they overlap.

### By hand

```bash
cd ~/Developer/agentinbox      # or wherever you put it
./install.sh
```

Then follow the steps it prints. The ones only you can do:

1. **Full Disk Access** for one small program, `~/.agentinbox/bin/agentinbox-runner`
   (System Settings › Privacy & Security › Full Disk Access). Don't grant it to
   Terminal, Claude, or Python.
2. **Approve** the Calendar and Reminders prompts on the first export.
3. **Choose your calendars** on the settings page: `~/.agentinbox/venv/bin/agentinbox config-ui`
4. **Set Calendar's default calendar** (Calendar › Settings › General) to your
   home calendar, so meeting links you open land there.
5. **Keep Mail running** (add it to Login Items). Mail only syncs while open.

Check everything with `~/.agentinbox/venv/bin/agentinbox doctor`.

### Requirements

- macOS 14 Sonoma or later, with Mail, Calendar and Reminders set up
- Xcode Command Line Tools (`xcode-select --install`)
- Python 3.11+ (python.org, Homebrew, or uv)
- Optional: an Apple Developer ID certificate. Without one the installer signs
  "ad-hoc", which works; after any future rebuild you'd re-grant Full Disk
  Access and re-approve the prompts.

## Connect your agent

- **Cowork**: create a project whose only folder is `~/AgentInbox/Inbox`. Turn
  off connectors and browser access for it. Optionally schedule a morning brief
  using [docs/brief-prompt.md](docs/brief-prompt.md).
- **Claude Code**: run it in `~/AgentInbox/Inbox`, sandboxed to that folder with
  network access denied.

The folder contains its own `README.md` that tells the agent the rules: mail
content is untrusted, how to make requests, and what it may and may not do.
On its first session it will offer to walk you through a few preferences.

## Change anything

Just ask your inbox agent. On its first session it offers to walk you through
the main settings.

- **Things the agent can change for you**, within fixed limits, when you ask:
  "keep six months of mail", "only look three months ahead", "use Google
  Calendar links", "show completed reminders for a month".
- **Everything else is changed on the settings page**, which only you can use.
  Most of it decides what the agent can see or do, so the agent can't change
  it itself. Ask the agent to **"open settings"** and the page opens in your
  browser on your Mac (or run `agentinbox config-ui`).

| Setting | Default | How to change |
|---|---|---|
| Mail kept | 90 days, plus emails about upcoming meetings | ask the agent (7–365 days) |
| Calendar look-ahead | 1 year | ask the agent (7–365 days) |
| Meeting link type | `.ics` file for the Calendar app | ask the agent (`.ics`, Google, Outlook, or several) |
| Completed reminders shown | 7 days | ask the agent (0–30 days) |
| Agenda length in briefs | 3 weeks | settings page |
| Which calendars, and which is your home calendar | chosen at setup | settings page |
| Mailboxes | Inbox and Sent | settings page |
| Excluded accounts | none | settings page |
| Agent may create / check off reminders | on | settings page |
| Attachment size limit, kept for | 50 MB, 7 days | settings page |
| Agent may change the settings above, or open the page | on | settings page |
| How often the export runs | every 30 minutes | `~/.config/agentinbox/config.toml`, then re-run `./install.sh` |

Google and Outlook links send the meeting's details to that service when you
click them, and only add to that service's calendar. `.ics` files stay on your
Mac and open in Calendar, which adds them to its default calendar.

## Day to day

| Task | How |
|---|---|
| Change settings | ask the agent, or ask it to "open settings" |
| Export now | `launchctl kickstart -k gui/$(id -u)/com.agentinbox.export` |
| Health check | `agentinbox doctor` |
| Logs | `~/.agentinbox/logs/agentinbox.log` |

(`agentinbox` lives at `~/.agentinbox/venv/bin/agentinbox`.)

## Uninstall

```bash
~/.agentinbox/venv/bin/agentinbox uninstall
rm -rf ~/.agentinbox ~/.config/agentinbox ~/AgentInbox/Inbox
```

Then remove `agentinbox-runner` from Full Disk Access, and the Calendar and
Reminders entries, in System Settings › Privacy & Security.

## Development

```bash
python3 -m pip install pytest
python3 -m pytest mailexport/tests
cd calexport && swift build
```

The spec, [agent-inbox-spec.md](agent-inbox-spec.md), is the source of truth for
behavior and security requirements; [docs/README.md](docs/README.md) is the
operations guide.

## Status

A personal project shared as-is, tested on one Mac with iCloud, Gmail and
Exchange accounts. Mail's on-disk format is undocumented and can change with
macOS updates; `agentinbox doctor` is the first thing to run if something looks
off. See [LICENSE](LICENSE).
