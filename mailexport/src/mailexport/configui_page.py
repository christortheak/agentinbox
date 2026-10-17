"""The single page served by `agentinbox config-ui`. No external resources;
all data is inserted as text nodes, never as HTML."""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgentInbox settings</title>
<style>
:root{--bg:#f6f6f4;--card:#fff;--fg:#1d1d1b;--muted:#6b6b66;--line:#e2e1dc;--accent:#2f5d8a;
--warn-bg:#fff6e0;--warn:#7a5300;--err-bg:#fdecec;--err:#8a1f1f;--ok:#2e6b3a;--chip:#eef2f6}
@media (prefers-color-scheme:dark){:root{--bg:#191918;--card:#232321;--fg:#ecebe6;--muted:#a3a29b;
--line:#363632;--accent:#8db6e0;--warn-bg:#3a2f12;--warn:#f0cf7c;--err-bg:#3d1c1c;--err:#f3a3a3;
--ok:#8fd19e;--chip:#2c3440}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line);
padding:12px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header h1{font-size:17px;margin:0;flex:1 1 auto}
#status{color:var(--muted);font-size:13px;flex:1 1 100%}
main{max-width:860px;margin:0 auto;padding:16px}
section{scroll-margin-top:90px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:0 0 16px}
h2{font-size:15px;margin:0 0 4px}
p.help{color:var(--muted);margin:0 0 12px;font-size:13px}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:7px 6px;border-top:1px solid var(--line);vertical-align:middle;font-size:14px}
th{font-weight:600;color:var(--muted);font-size:12px;border-top:0}
td.muted,span.muted{color:var(--muted);font-size:13px}
select,input[type=number],input[type=text]{font:inherit;color:var(--fg);background:var(--bg);
border:1px solid var(--line);border-radius:6px;padding:5px 8px}
input[type=number]{width:90px}
label.row{display:flex;gap:10px;align-items:center;margin:6px 0}
label.row span.l{min-width:220px}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--fg);
border-radius:6px;padding:6px 12px;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
@media (prefers-color-scheme:dark){button.primary{color:#111}}
button.small{padding:2px 8px;font-size:13px}
.chip{display:inline-flex;gap:6px;align-items:center;background:var(--chip);border-radius:999px;
padding:3px 6px 3px 10px;margin:3px 4px 3px 0;font-size:13px}
.chip button{border:0;background:transparent;padding:0 4px;font-size:14px}
.note{background:var(--warn-bg);color:var(--warn);border-radius:6px;padding:8px 10px;margin:8px 0;font-size:13px}
.err{background:var(--err-bg);color:var(--err);border-radius:6px;padding:8px 10px;margin:8px 0;font-size:13px}
.ok{color:var(--ok)}
footer{position:sticky;bottom:0;background:var(--bg);border-top:1px solid var(--line);
padding:12px 16px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
#msg{flex:1 1 100%;font-size:13px}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;margin:0;font-size:13px}
dt{color:var(--muted)} dd{margin:0;overflow-wrap:anywhere}
.acct{margin:8px 0 2px;font-weight:600;font-size:13px}
@media (max-width:600px){label.row{flex-wrap:wrap} label.row span.l{min-width:0;flex:1 1 100%}
.hide-sm{display:none}}
</style></head>
<body>
<header><h1>AgentInbox settings</h1><button id="done">Done</button><div id="status">Loading…</div></header>
<main id="app"></main>
<footer><button class="primary" id="save">Save</button>
<button id="save-run">Save and update the export now</button><div id="msg"></div></footer>
<script>
"use strict";
const TOKEN = "__TOKEN__";
history.replaceState(null, "", "/");   // drop the secret from the address bar
let S = null, mailboxes = [], excluded = [];

async function api(path, body) {
  const o = {headers: {"X-AgentInbox-Token": TOKEN}};
  if (body !== undefined) { o.method = "POST"; o.headers["Content-Type"] = "application/json";
                            o.body = JSON.stringify(body); }
  const r = await fetch(path, o);
  if (!r.ok) throw new Error(r.status + " " + (await r.text()));
  return r.json();
}
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (["checked", "value", "disabled", "selected"].includes(k)) e[k] = v;
    else e.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false)
    e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return e;
}
const $ = id => document.getElementById(id);
function num(id, value, min, max) { return h("input", {type: "number", id, value, min, max}); }
function box(id, checked) { return h("input", {type: "checkbox", id, checked: !!checked}); }
function row(label, control, extra) { return h("label", {class: "row"}, h("span", {class: "l"}, label), control, extra || null); }
function fmtTime(iso) { if (!iso) return "never"; const d = new Date(iso); return d.toLocaleString(); }

function overrideNote(key, label) {
  const s = S.agent.settings[key];
  if (!s || s.source !== "preference") return null;
  const v = Array.isArray(s.value) ? s.value.join(", ") : s.value;
  return h("div", {class: "note"},
    `The agent set ${label} to ${v}${s.changed_at ? " on " + fmtTime(s.changed_at) : ""}, which overrides the value here. `,
    h("button", {class: "small", onclick: () => resetPref(key)}, "Use my value"));
}

const USE_LABELS = [["home", "Home: where everything goes"], ["shared", "Shared: readable, not home"],
  ["other", "Following: readable"], ["lookup", "Duplicate check: title and time only"], ["off", "Off"]];
const USE_ORDER = {home: 0, shared: 1, other: 2, lookup: 3, off: 4};

function renderCalendars() {
  const rows = [...S.calendars].sort((a, b) => (USE_ORDER[a.use] - USE_ORDER[b.use]) ||
    a.title.localeCompare(b.title) || a.source.localeCompare(b.source));
  const body = rows.length ? rows.map((c, i) => h("tr", {},
      h("td", {}, c.title,
        c.count > 1 ? h("div", {class: "muted"}, `${c.count} calendars with this name in this account (set together)`) : null,
        c.title_repeats ? h("div", {class: "muted"}, "same name in another account") : null),
      h("td", {class: "muted"}, c.source),
      h("td", {class: "muted hide-sm"}, c.type),
      h("td", {}, h("select", {"data-title": c.title, "data-source": c.source, class: "cal-use"},
        USE_LABELS.map(([v, l]) => h("option", {value: v, selected: v === c.use}, l))))))
    : [h("tr", {}, h("td", {colspan: 4, class: "muted"}, "No calendar list yet: run an export first."))];
  const unmatched = [...S.unmatched_include.map(r => ["include", r]), ...S.unmatched_lookup.map(r => ["lookup", r])];
  return h("section", {},
    h("h2", {}, "Calendars"),
    h("p", {class: "help"}, "Home is your own calendar: the agent proposes missing meetings for it, and only it counts as \"on your calendar\". " +
      "Shared and Following calendars are exported in full and shown in the agenda. Duplicate check exports only titles and times, so the agent can tell a meeting is already booked somewhere."),
    h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Calendar"), h("th", {}, "Account"),
      h("th", {class: "hide-sm"}, "Type"), h("th", {}, "Use"))), h("tbody", {}, body)),
    unmatched.length ? h("div", {class: "note"}, "These config entries match no calendar in the last export. Keep them?",
      unmatched.map(([kind, r], i) => h("label", {class: "row"},
        h("input", {type: "checkbox", checked: true, class: "keep", "data-kind": kind, "data-ref": JSON.stringify(r)}),
        typeof r === "string" ? r : `${r.title} (${r.source || "any account"})`, h("span", {class: "muted"}, kind === "lookup" ? "duplicate check" : "exported")))) : null);
}

function renderWindows() {
  return h("section", {},
    h("h2", {}, "Calendar window"),
    h("p", {class: "help"}, "How far the calendar export reaches. The agenda the brief reads stays short; the full window is for spotting duplicates."),
    row("Days back", num("past_days", S.calendar.past_days, 0, 30)),
    row("Days ahead", num("future_days", S.calendar.future_days, 1, 1460)),
    overrideNote("calendar_future_days", "days ahead"),
    row("Agenda length (days)", num("agenda_days", S.calendar.agenda_days, 1, 90)));
}

function renderProposals() {
  const f = S.proposals.formats;
  return h("section", {},
    h("h2", {}, "Meeting proposals"),
    h("p", {class: "help"}, "When mail mentions a meeting that isn't on your home calendar, the agent can make a click-to-add link. Nothing is added until you click it."),
    row("Allow proposals", box("prop_enabled", S.proposals.enabled)),
    h("div", {}, h("span", {class: "muted"}, "Link type: "),
      S.link_formats.map(x => h("label", {style: "margin-right:14px"}, box("fmt_" + x, f.includes(x)), " ",
        {ics: ".ics file (Calendar app)", google: "Google Calendar link", outlook: "Outlook web link"}[x]))),
    overrideNote("calendar_link_format", "the link type"),
    h("p", {class: "help", style: "margin-top:10px"}, ".ics files open in Calendar, which adds them to its default calendar: set Calendar > Settings > General > Default Calendar to your home calendar. " +
      "Google and Outlook links add to those services' own calendars and send the meeting details to them when clicked."));
}

function chips() {
  const wrap = $("mb-chips"); wrap.replaceChildren();
  for (const m of mailboxes) wrap.append(h("span", {class: "chip"}, m,
    h("button", {title: "Remove", onclick: () => { mailboxes = mailboxes.filter(x => x !== m); chips(); }}, "×")));
  if (!mailboxes.length) wrap.append(h("span", {class: "muted"}, "No mailboxes: the agent would see no mail."));
}
function exclChips() {
  const wrap = $("ex-chips"); wrap.replaceChildren();
  for (const a of excluded) wrap.append(h("span", {class: "chip"}, a,
    h("button", {title: "Include again", onclick: () => { excluded = excluded.filter(x => x !== a); exclChips(); renderDiag(); }}, "×")));
  if (!excluded.length) wrap.append(h("span", {class: "muted"}, "None"));
}
function addMailbox(name) {
  name = name.trim(); if (!name) return;
  if (!mailboxes.some(m => m.toLowerCase() === name.toLowerCase())) mailboxes.push(name);
  chips(); renderDiag();
}
function renderDiag() {
  const d = S.mailbox_diag, wrap = $("mb-diag"); wrap.replaceChildren();
  if (!d) { wrap.append(h("p", {class: "muted"}, "Load the mailbox names from your mail store to pick from them.")); return; }
  for (const a of d.accounts) {
    const who = a.mapped_as || a.uuid;
    wrap.append(h("div", {class: "acct"}, who, " ",
      excluded.includes((a.mapped_as || "").toLowerCase()) ? h("span", {class: "muted"}, "(excluded)")
        : a.mapped_as ? h("button", {class: "small", onclick: () => { excluded.push(a.mapped_as.toLowerCase()); exclChips(); renderDiag(); }}, "Exclude account") : null));
    wrap.append(h("div", {}, a.mailboxes.map(m => {
      const on = m.exported || mailboxes.some(x => x.toLowerCase() === m.config_name.toLowerCase());
      return h("span", {class: "chip"}, m.config_name, on ? h("span", {class: "ok"}, "✓")
        : h("button", {title: "Export this mailbox", onclick: () => addMailbox(m.config_name)}, "+"));
    })));
  }
}

function renderMail() {
  return h("section", {},
    h("h2", {}, "Mail"),
    h("p", {class: "help"}, "Which mailboxes the agent can read is your decision alone; the agent can't change it. Standard names cover each provider's variants: Sent also means Sent Items and Gmail's Sent Mail."),
    row("Days of mail to keep", num("mail_days", S.mail.days, 1, 3650)),
    overrideNote("mail_days", "days of mail"),
    h("div", {class: "muted"}, "Exported mailboxes"), h("div", {id: "mb-chips"}),
    h("div", {style: "margin:6px 0"}, h("input", {type: "text", id: "mb-add", placeholder: "Add a mailbox by name"}),
      " ", h("button", {class: "small", onclick: () => { addMailbox($("mb-add").value); $("mb-add").value = ""; }}, "Add")),
    h("div", {class: "muted", style: "margin-top:10px"}, "Excluded accounts"), h("div", {id: "ex-chips"}),
    h("div", {style: "margin:10px 0 4px"}, h("button", {id: "load-mb", onclick: loadMailboxes}, "Load mailbox names from Mail"),
      " ", h("span", {class: "muted", id: "mb-status"}, S.mailbox_diag ? "Showing the last list loaded." : "")),
    h("div", {id: "mb-diag"}));
}

function renderReminders() {
  const r = S.reminders;
  return h("section", {},
    h("h2", {}, "Reminders"),
    row("Export reminders", box("rem_enabled", r.enabled)),
    row("Agent may create reminders", box("rem_create", r.allow_create)),
    row("Agent may check reminders off", box("rem_complete", r.allow_complete)),
    row("Completed reminders shown (days)", num("rem_days", r.completed_days, 0, 365)),
    overrideNote("reminders_completed_days", "completed reminders shown"),
    row("Only these lists", h("input", {type: "text", id: "rem_lists", value: r.include.join(", "), placeholder: "all lists", style: "flex:1"})));
}

function renderAttachments() {
  return h("section", {},
    h("h2", {}, "Attachments"),
    row("Largest attachment the agent may fetch (MB)", num("att_mb", S.attachments.max_mb, 1, 500)),
    row("Delete fetched copies after (days)", num("att_days", S.attachments.retention_days, 1, 90)));
}

function renderAgent() {
  const st = S.agent.settings;
  const labels = {mail_days: "Days of mail", calendar_future_days: "Calendar days ahead",
    reminders_completed_days: "Completed reminders shown", calendar_link_format: "Link type", onboarded: "Preferences reviewed"};
  return h("section", {},
    h("h2", {}, "What the agent may change"),
    h("p", {class: "help"}, "The agent can adjust these few settings when you ask it to, within fixed limits. Everything else on this page is yours alone."),
    row("Allow the agent to change these", box("allow_agent", S.agent.agent_may_change)),
    row("Allow the agent to open this page for you", box("allow_open", S.agent.may_open_settings)),
    h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Setting"), h("th", {}, "Value"), h("th", {}, "Set by"), h("th", {}, ""))),
      h("tbody", {}, Object.entries(st).map(([k, s]) => h("tr", {},
        h("td", {}, labels[k] || k),
        h("td", {}, Array.isArray(s.value) ? s.value.join(", ") : String(s.value)),
        h("td", {class: "muted"}, s.source === "preference" ? "agent" + (s.changed_at ? ", " + fmtTime(s.changed_at) : "")
          : s.source === "config" ? "you" : "default"),
        h("td", {}, s.source === "preference" ? h("button", {class: "small", onclick: () => resetPref(k)}, "Reset") : null))))),
    h("div", {style: "margin-top:8px"}, h("button", {class: "small", onclick: () => resetPref(null)}, "Reset all agent changes")));
}

function renderFixed() {
  const f = S.fixed, accts = Object.entries(f.accounts);
  return h("section", {},
    h("h2", {}, "Fixed settings"),
    h("p", {class: "help"}, "Shown for reference. Change these by editing " + S.config_path + " (schedule changes also need `agentinbox install`)."),
    h("dl", {}, h("dt", {}, "Agent's folder"), h("dd", {}, f.output_dir),
      h("dt", {}, "Private state"), h("dd", {}, f.state_dir),
      h("dt", {}, "Time zone"), h("dd", {}, f.timezone),
      h("dt", {}, "Export runs every"), h("dd", {}, f.interval_minutes + " minutes"),
      accts.length ? [h("dt", {}, "Account mapping"), h("dd", {}, accts.map(([u, v]) => h("div", {}, v, " ", h("span", {class: "muted"}, u.slice(0, 8) + "…"))))] : null));
}

function render() {
  const m = S.manifest;
  $("status").textContent = `Last export ${fmtTime(m.generated_at)} · ${m.mail_count ?? "?"} messages · ${m.events ?? "?"} events` +
    (m.stale ? " · STALE: is Mail running?" : "");
  mailboxes = [...S.mail.mailboxes]; excluded = [...S.mail.exclude_accounts];
  $("app").replaceChildren(renderCalendars(), renderWindows(), renderProposals(), renderMail(),
    renderReminders(), renderAttachments(), renderAgent(), renderFixed());
  chips(); exclChips(); renderDiag();
}

function collect() {
  const keep = [...document.querySelectorAll("input.keep")];
  const v = id => $(id).value;
  return {
    calendars: [...document.querySelectorAll("select.cal-use")].map(s => ({title: s.dataset.title, source: s.dataset.source, use: s.value})),
    keep_include: keep.filter(k => k.checked && k.dataset.kind === "include").map(k => JSON.parse(k.dataset.ref)),
    keep_lookup: keep.filter(k => k.checked && k.dataset.kind === "lookup").map(k => JSON.parse(k.dataset.ref)),
    calendar: {past_days: v("past_days"), future_days: v("future_days"), agenda_days: v("agenda_days")},
    mail: {days: v("mail_days"), mailboxes, exclude_accounts: excluded},
    proposals: {enabled: $("prop_enabled").checked, formats: S.link_formats.filter(x => $("fmt_" + x).checked)},
    reminders: {enabled: $("rem_enabled").checked, allow_create: $("rem_create").checked,
                allow_complete: $("rem_complete").checked, completed_days: v("rem_days"),
                include: v("rem_lists").split(",").map(x => x.trim()).filter(Boolean)},
    attachments: {max_mb: v("att_mb"), retention_days: v("att_days")},
    agent: {allow_agent_changes: $("allow_agent").checked, may_open_settings: $("allow_open").checked},
  };
}

function say(nodes) { $("msg").replaceChildren(...[nodes].flat()); }
async function reload() { S = await api("/api/state"); render(); }
async function save(thenRun) {
  say("Saving…");
  try {
    const r = await api("/api/save", collect());
    if (!r.ok) { say(h("div", {class: "err"}, "Not saved:", h("ul", {}, r.errors.map(e => h("li", {}, e))))); return; }
    await reload();
    const out = [h("span", {class: "ok"}, "Saved."), r.backup ? h("span", {class: "muted"}, " Previous version kept at " + r.backup + ".") : null];
    if (r.warnings.length) out.push(h("div", {class: "note"}, r.warnings.join(" ")));
    if (thenRun) {
      const x = await api("/api/run-export", {});
      out.push(h("div", {}, x.ok ? "Export started. It takes a few minutes; the status line updates when you reload."
                                  : "Couldn't start the export: " + (x.error || "unknown error")));
    } else out.push(h("span", {class: "muted"}, " Takes effect at the next export (within 30 minutes)."));
    say(out);
  } catch (e) { say(h("div", {class: "err"}, String(e))); }
}
async function resetPref(key) {
  await api("/api/reset-preference", {key}); await reload();
  say(h("span", {class: "ok"}, key ? "Reset to your value." : "All agent changes reset."));
}
async function loadMailboxes() {
  $("load-mb").disabled = true; $("mb-status").textContent = "Asking the export service… (up to a minute)";
  try {
    const r = await api("/api/mailboxes", {});
    if (!r.ok) { $("mb-status").textContent = r.error || "Failed."; return; }
    const keepM = mailboxes, keepX = excluded;
    S.mailbox_diag = r; renderDiag(); mailboxes = keepM; excluded = keepX;
    $("mb-status").textContent = "Loaded.";
  } catch (e) { $("mb-status").textContent = String(e); }
  finally { $("load-mb").disabled = false; }
}
$("save").addEventListener("click", () => save(false));
$("save-run").addEventListener("click", () => save(true));
$("done").addEventListener("click", async () => {
  try { await api("/api/done", {}); } catch (e) {}
  document.body.replaceChildren(h("main", {}, h("section", {}, h("h2", {}, "Settings closed"),
    h("p", {class: "help"}, "You can close this tab. Run `agentinbox config-ui` to open it again."))));
});
reload().catch(e => say(h("div", {class: "err"}, "Couldn't load settings: " + e)));
</script></body></html>
"""
