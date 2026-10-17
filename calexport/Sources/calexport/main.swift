// calexport - EventKit exporter for AgentInbox.
// Invoked by the `agentinbox` wrapper, which owns config parsing; this tool
// takes flags only.
//
// Read paths: calendar events, reminders.
// Write paths: EXACTLY TWO, both reminders-only - `reminder-add` and
// `reminder-complete`. Calendars and mail are never written. No delete, no
// edit. Keep it that way.

import EventKit
import Foundation
import CryptoKit

let VERSION = "0.3.0"

struct Options {
    var command: String = "export"
    var output: String = ""
    var pastDays: Int = 1
    var futureDays: Int = 21
    var timezone: String = "America/New_York"
    var include: [String] = []
    var includeAt: [(title: String, source: String)] = []   // title in one account only
    var exclude: [String] = []
    var roles: [String: String] = [:]
    var checkAccess = false
    // reminders export
    var reminders = false
    var remindersCompletedDays: Int = 7
    var remindersInclude: [String] = []
    // reminder-add / reminder-complete
    var title: String = ""
    var list: String = ""
    var notes: String = ""
    var due: String = ""
    var reminderId: String = ""
    // v0.3: duplicate-check calendars, near-term agenda, trusted state dir
    var lookup: [String] = []
    var lookupAt: [(title: String, source: String)] = []   // title in one account only
    var agendaDays: Int = 21
    var stateDir: String = ""
}

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write(("calexport: " + msg + "\n").data(using: .utf8)!)
    exit(1)
}

func parseArgs() -> Options {
    var opts = Options()
    var args = Array(CommandLine.arguments.dropFirst())
    if let first = args.first, !first.hasPrefix("--") {
        opts.command = first
        args.removeFirst()
    }
    func next(_ flag: String) -> String {
        guard !args.isEmpty else { fail("\(flag) needs a value") }
        return args.removeFirst()
    }
    while !args.isEmpty {
        let a = args.removeFirst()
        switch a {
        case "--output": opts.output = next(a)
        case "--past-days": opts.pastDays = Int(next(a)) ?? 1
        case "--future-days": opts.futureDays = Int(next(a)) ?? 21
        case "--timezone": opts.timezone = next(a)
        case "--include": opts.include.append(next(a))
        case "--include-at":
            let t = next(a)
            opts.includeAt.append((title: t, source: next(a)))
        case "--exclude": opts.exclude.append(next(a))
        case "--role":
            let v = next(a)
            if let eq = v.firstIndex(of: "=") {
                opts.roles[String(v[..<eq])] = String(v[v.index(after: eq)...])
            }
        case "--reminders": opts.reminders = true
        case "--reminders-completed-days": opts.remindersCompletedDays = Int(next(a)) ?? 7
        case "--reminders-include": opts.remindersInclude.append(next(a))
        case "--title": opts.title = next(a)
        case "--list": opts.list = next(a)
        case "--notes": opts.notes = next(a)
        case "--due": opts.due = next(a)
        case "--id": opts.reminderId = next(a)
        case "--lookup": opts.lookup.append(next(a))
        case "--lookup-at":
            let t = next(a)
            opts.lookupAt.append((title: t, source: next(a)))
        case "--agenda-days": opts.agendaDays = Int(next(a)) ?? 21
        case "--state-dir": opts.stateDir = next(a)
        case "--check-access": opts.checkAccess = true
        case "--version": print(VERSION); exit(0)
        default: fail("unknown argument \(a)")
        }
    }
    return opts
}

func requestEventAccess(_ store: EKEventStore) -> Bool {
    let sem = DispatchSemaphore(value: 0)
    var granted = false
    if #available(macOS 14.0, *) {
        store.requestFullAccessToEvents { g, _ in granted = g; sem.signal() }
    } else {
        store.requestAccess(to: .event) { g, _ in granted = g; sem.signal() }
    }
    sem.wait()
    return granted
}

func requestReminderAccess(_ store: EKEventStore) -> Bool {
    let sem = DispatchSemaphore(value: 0)
    var granted = false
    if #available(macOS 14.0, *) {
        store.requestFullAccessToReminders { g, _ in granted = g; sem.signal() }
    } else {
        store.requestAccess(to: .reminder) { g, _ in granted = g; sem.signal() }
    }
    sem.wait()
    return granted
}

func atomicWrite(_ data: Data, to path: URL) throws {
    let dir = path.deletingLastPathComponent()
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let tmp = dir.appendingPathComponent(".tmp-\(UUID().uuidString)")
    try data.write(to: tmp)
    _ = try FileManager.default.replaceItemAt(path, withItemAt: tmp)
}

func hexId(_ basis: String) -> String {
    let digest = SHA256.hash(data: Data(basis.utf8))
    return digest.map { String(format: "%02x", $0) }.joined().prefix(16).lowercased()
}

func participantStatus(_ s: EKParticipantStatus) -> String {
    switch s {
    case .accepted: return "accepted"
    case .declined: return "declined"
    case .tentative: return "tentative"
    case .pending: return "pending"
    case .delegated: return "delegated"
    case .completed: return "completed"
    case .inProcess: return "in-process"
    default: return "unknown"
    }
}

func eventStatus(_ s: EKEventStatus) -> String {
    switch s {
    case .confirmed: return "confirmed"
    case .tentative: return "tentative"
    case .canceled: return "canceled"
    default: return "none"
    }
}

func emailFromParticipant(_ p: EKParticipant) -> String? {
    let u = p.url.absoluteString
    return u.hasPrefix("mailto:") ? String(u.dropFirst(7)) : nil
}

func formatOrganizer(_ p: EKParticipant?) -> Any {
    guard let p = p else { return NSNull() }
    let name = p.name ?? ""
    if let email = emailFromParticipant(p) {
        return name.isEmpty ? email : name + " <" + email + ">"
    }
    return name.isEmpty ? NSNull() : name
}

func writeJSON(_ obj: Any, to url: URL, what: String) {
    do {
        let data = try JSONSerialization.data(withJSONObject: obj,
                                              options: [.prettyPrinted, .sortedKeys])
        try atomicWrite(data + Data("\n".utf8), to: url)
    } catch { fail("writing \(what): \(error)") }
}

// ---- main ----

let opts = parseArgs()
let store = EKEventStore()

if opts.checkAccess {
    // Status only - never prompt here. A prompt triggered from a terminal
    // attributes the grant to the terminal app, not the exporter; the first
    // real prompt must come from a launchd-run export (launchctl kickstart).
    let evStatus = EKEventStore.authorizationStatus(for: .event)
    let remStatus = EKEventStore.authorizationStatus(for: .reminder)
    func ok(_ s: EKAuthorizationStatus) -> Bool {
        if #available(macOS 14.0, *) { return s == .fullAccess }
        return s == .authorized
    }
    print("calendar access: \(ok(evStatus) ? "granted" : "not granted (status \(evStatus.rawValue))")")
    print("reminders access: \(ok(remStatus) ? "granted" : "not granted (status \(remStatus.rawValue))")")
    if ok(evStatus) { exit(0) }
    if evStatus == .notDetermined {
        fail("calendar access not requested yet. Trigger the first export via "
             + "launchd so the grant attaches to the exporter, then approve the "
             + "prompt: launchctl kickstart -k gui/$(id -u)/com.agentinbox.export")
    }
    fail("calendar access denied. Enable Full Calendar Access for the exporter "
         + "in System Settings > Privacy & Security > Calendars.")
}

guard let tz = TimeZone(identifier: opts.timezone) else { fail("bad timezone \(opts.timezone)") }
var cal = Calendar(identifier: .gregorian)
cal.timeZone = tz
let now = Date()

let isoFmt = ISO8601DateFormatter()
isoFmt.timeZone = tz
isoFmt.formatOptions = [.withInternetDateTime]
let dayFmt = DateFormatter()
dayFmt.locale = Locale(identifier: "en_US_POSIX")
dayFmt.timeZone = tz
dayFmt.dateFormat = "yyyy-MM-dd"

func reminderDueString(_ comps: DateComponents?) -> Any {
    guard let comps = comps, let date = cal.date(from: comps) else { return NSNull() }
    if comps.hour != nil { return isoFmt.string(from: date) }
    return dayFmt.string(from: date)
}

func selectedReminderLists() -> [EKCalendar] {
    let all = store.calendars(for: .reminder)
    if opts.remindersInclude.isEmpty { return all }
    return all.filter { opts.remindersInclude.contains($0.title) }
}

func fetchReminders(in lists: [EKCalendar]) -> [EKReminder] {
    guard !lists.isEmpty else { return [] }
    let pred = store.predicateForReminders(in: lists)
    let sem = DispatchSemaphore(value: 0)
    var out: [EKReminder] = []
    store.fetchReminders(matching: pred) { rs in out = rs ?? []; sem.signal() }
    sem.wait()
    return out
}

// ---- reminder-add ----
if opts.command == "reminder-add" {
    guard !opts.title.isEmpty else { fail("--title is required") }
    guard requestReminderAccess(store) else { fail("reminders access denied") }
    let lists = store.calendars(for: .reminder)
    var target: EKCalendar? = nil
    if !opts.list.isEmpty {
        target = lists.first { $0.title == opts.list }
        if target == nil { fail("no reminder list titled '\(opts.list)'") }
    } else {
        target = store.defaultCalendarForNewReminders() ?? lists.first
    }
    guard let listCal = target else { fail("no reminder list available") }
    let r = EKReminder(eventStore: store)
    r.calendar = listCal
    r.title = opts.title
    var notes = opts.notes
    if !notes.isEmpty { notes += "\n" }
    notes += "[created via AgentInbox]"   // provenance marker, always stamped
    r.notes = notes
    if !opts.due.isEmpty {
        var comps: DateComponents
        let dfDay = DateFormatter()
        dfDay.locale = Locale(identifier: "en_US_POSIX"); dfDay.timeZone = tz
        dfDay.dateFormat = "yyyy-MM-dd"
        let dfMin = DateFormatter()
        dfMin.locale = Locale(identifier: "en_US_POSIX"); dfMin.timeZone = tz
        dfMin.dateFormat = "yyyy-MM-dd'T'HH:mm"
        if let d = dfMin.date(from: opts.due) {
            comps = cal.dateComponents([.year, .month, .day, .hour, .minute], from: d)
        } else if let d = dfDay.date(from: opts.due) {
            comps = cal.dateComponents([.year, .month, .day], from: d)
        } else {
            fail("bad --due '\(opts.due)' (use yyyy-MM-dd or yyyy-MM-dd'T'HH:mm)")
        }
        r.dueDateComponents = comps
    }
    do { try store.save(r, commit: true) } catch { fail("save failed: \(error)") }
    print(r.calendarItemIdentifier)
    exit(0)
}

// ---- reminder-complete ----
if opts.command == "reminder-complete" {
    guard !opts.reminderId.isEmpty else { fail("--id is required") }
    guard requestReminderAccess(store) else { fail("reminders access denied") }
    guard let item = store.calendarItem(withIdentifier: opts.reminderId),
          let r = item as? EKReminder else {
        fail("no reminder with id \(opts.reminderId)")
    }
    if r.isCompleted {
        print("already-completed")
        exit(0)
    }
    r.isCompleted = true
    do { try store.save(r, commit: true) } catch { fail("save failed: \(error)") }
    print("completed")
    exit(0)
}

// ---- export (default) ----
guard opts.command == "export" else { fail("unknown command \(opts.command)") }
guard !opts.output.isEmpty else { fail("--output is required") }
guard requestEventAccess(store) else {
    fail("calendar access denied; run `calexport --check-access` for guidance")
}

var warnings: [String] = []

// Calendar selection per spec: non-empty include wins exactly; else all minus exclude.
let all = store.calendars(for: .event)
var selected: [EKCalendar]
if !opts.include.isEmpty || !opts.includeAt.isEmpty {
    selected = all.filter { c in
        opts.include.contains(c.title)
            || opts.includeAt.contains { $0.title == c.title && $0.source == (c.source?.title ?? "") }
    }
    for want in opts.include where !all.contains(where: { $0.title == want }) {
        warnings.append("warning: include entry '\(want)' matches no calendar")
    }
    for want in opts.includeAt where !all.contains(where: {
        $0.title == want.title && ($0.source?.title ?? "") == want.source }) {
        warnings.append("warning: include entry '\(want.title)' in account '\(want.source)' matches no calendar")
    }
    let dupes = Dictionary(grouping: selected, by: { $0.title }).filter { $1.count > 1 }
    for (title, cals) in dupes {
        warnings.append("warning: \(cals.count) calendars titled '\(title)' (all exported)")
    }
} else {
    selected = all.filter { !opts.exclude.contains($0.title) }
}

let windowStart = cal.date(byAdding: .day, value: -opts.pastDays, to: cal.startOfDay(for: now))!
let windowEnd = cal.date(byAdding: .day, value: opts.futureDays + 1, to: cal.startOfDay(for: now))!

func role(for calendarTitle: String) -> String {
    if let r = opts.roles[calendarTitle] { return r }
    if opts.roles.isEmpty && Set(opts.include + opts.includeAt.map { $0.title }).count == 1 { return "mine" }
    return "other"
}

var events: [[String: Any]] = []
if !selected.isEmpty {
    // Pass calendars explicitly; never fetch-all-and-filter.
    let predicate = store.predicateForEvents(withStart: windowStart, end: windowEnd,
                                             calendars: selected)
    for ev in store.events(matching: predicate) {
        let startISO: String
        let endISO: String
        if ev.isAllDay {
            startISO = dayFmt.string(from: ev.startDate)
            endISO = dayFmt.string(from: ev.endDate)
        } else {
            startISO = isoFmt.string(from: ev.startDate)
            endISO = isoFmt.string(from: ev.endDate)
        }
        var notes = ev.notes ?? ""
        if notes.count > 2000 { notes = String(notes.prefix(2000)) + " [truncated]" }
        let attendees: [[String: Any]] = (ev.attendees ?? []).map { p in
            ["name": p.name ?? NSNull() as Any,
             "email": emailFromParticipant(p) ?? NSNull() as Any,
             "status": participantStatus(p.participantStatus)]
        }
        let calTitle = ev.calendar.title
        events.append([
            "id": hexId(ev.calendarItemIdentifier + "|" + startISO),
            "event_identifier": ev.calendarItemIdentifier,
            // iCalendar UID for Exchange/CalDAV events: lets the mail side
            // match an invite to the event it created.
            "external_id": ev.calendarItemExternalIdentifier ?? NSNull() as Any,
            "calendar": calTitle,
            "calendar_source": ev.calendar.source?.title ?? "unknown",
            "role": role(for: calTitle),
            "title": ev.title ?? "",
            "start": startISO,
            "end": endISO,
            "all_day": ev.isAllDay,
            "location": ev.location ?? NSNull() as Any,
            "notes": notes.isEmpty ? NSNull() as Any : notes,
            "attendees": attendees,
            "organizer": formatOrganizer(ev.organizer),
            "status": eventStatus(ev.status),
            "recurring": ev.hasRecurrenceRules || ev.isDetached,
            "url": ev.url?.absoluteString ?? NSNull() as Any,
        ])
    }
}

events.sort {
    let a = ($0["start"] as! String, $0["id"] as! String)
    let b = ($1["start"] as! String, $1["id"] as! String)
    return a < b
}

let outDir = URL(fileURLWithPath: (opts.output as NSString).expandingTildeInPath)
writeJSON(events, to: outDir.appendingPathComponent("calendar/events.json"),
          what: "events.json")

// agenda.md - grouped by day, role shown per event.
let headerFmt = DateFormatter()
headerFmt.locale = Locale(identifier: "en_US_POSIX")
headerFmt.timeZone = tz
headerFmt.dateFormat = "EEEE, MMMM d, yyyy"
let timeFmt = DateFormatter()
timeFmt.locale = Locale(identifier: "en_US_POSIX")
timeFmt.timeZone = tz
timeFmt.dateFormat = "HH:mm"

let agendaEnd = min(windowEnd,
                    cal.date(byAdding: .day, value: opts.agendaDays + 1,
                             to: cal.startOfDay(for: now))!)
let agendaEndDay = dayFmt.string(from: agendaEnd)
var agenda = "# Agenda\n\nWindow: \(dayFmt.string(from: windowStart)) to \(agendaEndDay) (\(opts.timezone)). "
    + "events.json covers through \(dayFmt.string(from: windowEnd)).\n"
var byDay: [String: [[String: Any]]] = [:]
for e in events {
    let day = String((e["start"] as! String).prefix(10))
    if day >= agendaEndDay { continue }
    byDay[day, default: []].append(e)
}
for day in byDay.keys.sorted() {
    if let d = dayFmt.date(from: day) {
        agenda += "\n## \(headerFmt.string(from: d))\n\n"
    } else {
        agenda += "\n## \(day)\n\n"
    }
    for e in byDay[day]! {
        let r = e["role"] as! String
        let title = e["title"] as! String
        let calName = e["calendar"] as! String
        var line: String
        if e["all_day"] as! Bool {
            line = "- (all day) [\(r)] \(title) (\(calName))"
        } else {
            let s = isoFmt.date(from: e["start"] as! String).map { timeFmt.string(from: $0) } ?? "?"
            let en = isoFmt.date(from: e["end"] as! String).map { timeFmt.string(from: $0) } ?? "?"
            line = "- \(s)-\(en) [\(r)] \(title) (\(calName))"
        }
        if let loc = e["location"] as? String, !loc.isEmpty {
            line += " - \(loc.split(separator: "\n").first.map(String.init) ?? loc)"
        }
        agenda += line + "\n"
    }
}
do {
    try atomicWrite(Data(agenda.utf8), to: outDir.appendingPathComponent("calendar/agenda.md"))
} catch { fail("writing agenda.md: \(error)") }

// ---- reminders export (best-effort; calendar export never blocks on it) ----
var reminderRecords: [[String: Any]] = []
var reminderLists: [String] = []
if opts.reminders {
    if requestReminderAccess(store) {
        let lists = selectedReminderLists()
        reminderLists = lists.map { $0.title }.sorted()
        let horizon = cal.date(byAdding: .day, value: -opts.remindersCompletedDays, to: now)!
        for r in fetchReminders(in: lists) {
            if r.isCompleted {
                guard let done = r.completionDate, done >= horizon else { continue }
            }
            var notes = r.notes ?? ""
            if notes.count > 2000 { notes = String(notes.prefix(2000)) + " [truncated]" }
            reminderRecords.append([
                "id": r.calendarItemIdentifier,
                "list": r.calendar.title,
                "title": r.title ?? "",
                "notes": notes.isEmpty ? NSNull() as Any : notes,
                "due": reminderDueString(r.dueDateComponents),
                "priority": r.priority,
                "completed": r.isCompleted,
                "completion_date": r.completionDate.map { isoFmt.string(from: $0) } ?? NSNull() as Any,
                "url": r.url?.absoluteString ?? NSNull() as Any,
            ])
        }
        reminderRecords.sort {
            let a = (($0["due"] as? String) ?? "9999", $0["id"] as! String)
            let b = (($1["due"] as? String) ?? "9999", $1["id"] as! String)
            return a < b
        }
        writeJSON(reminderRecords,
                  to: outDir.appendingPathComponent("reminders/reminders.json"),
                  what: "reminders.json")

        var md = "# Reminders\n"
        let today = dayFmt.string(from: now)
        let open = reminderRecords.filter { !($0["completed"] as! Bool) }
        let done = reminderRecords.filter { $0["completed"] as! Bool }
        func section(_ title: String, _ items: [[String: Any]]) {
            guard !items.isEmpty else { return }
            md += "\n## \(title)\n\n"
            for r in items {
                let due = (r["due"] as? String).map { " (due \($0))" } ?? ""
                md += "- \(r["title"] as! String)\(due) [\(r["list"] as! String)] <id:\(r["id"] as! String)>\n"
            }
        }
        section("Overdue", open.filter { (($0["due"] as? String) ?? "9999") < today })
        section("Due today", open.filter { ($0["due"] as? String)?.hasPrefix(today) == true })
        section("Upcoming", open.filter { (($0["due"] as? String) ?? "") > today && !(($0["due"] as? String)?.hasPrefix(today) ?? false) })
        section("No due date", open.filter { $0["due"] as? String == nil })
        section("Recently completed (last \(opts.remindersCompletedDays) days)", done)
        do {
            try atomicWrite(Data(md.utf8), to: outDir.appendingPathComponent("reminders/reminders.md"))
        } catch { fail("writing reminders.md: \(error)") }
    } else {
        warnings.append("warning: reminders access not granted; reminders not exported")
    }
}

// ---- lookup.json: minimal fields from duplicate-check calendars ----
// Only title/time/calendar - enough to tell whether a meeting is already
// booked, without exporting notes, attendees or locations.
let selectedIds = Set(selected.map { $0.calendarIdentifier })
func isLookup(_ c: EKCalendar) -> Bool {
    if opts.lookup.contains(c.title) { return true }
    let src = c.source?.title ?? ""
    return opts.lookupAt.contains { $0.title == c.title && $0.source == src }
}
let lookupCals = all.filter { isLookup($0) && !selectedIds.contains($0.calendarIdentifier) }
for want in opts.lookup where !all.contains(where: { $0.title == want }) {
    warnings.append("warning: lookup entry '\(want)' matches no calendar")
}
for want in opts.lookupAt where !all.contains(where: {
    $0.title == want.title && ($0.source?.title ?? "") == want.source }) {
    warnings.append("warning: lookup entry '\(want.title)' in account '\(want.source)' matches no calendar")
}
var lookupEvents: [[String: Any]] = []
if !lookupCals.isEmpty {
    let lp = store.predicateForEvents(withStart: windowStart, end: windowEnd,
                                      calendars: lookupCals)
    for ev in store.events(matching: lp) {
        let s = ev.isAllDay ? dayFmt.string(from: ev.startDate) : isoFmt.string(from: ev.startDate)
        let e = ev.isAllDay ? dayFmt.string(from: ev.endDate) : isoFmt.string(from: ev.endDate)
        lookupEvents.append([
            "id": hexId(ev.calendarItemIdentifier + "|" + s),
            "external_id": ev.calendarItemExternalIdentifier ?? NSNull() as Any,
            "calendar": ev.calendar.title,
            "calendar_source": ev.calendar.source?.title ?? "unknown",
            "title": ev.title ?? "",
            "start": s,
            "end": e,
            "all_day": ev.isAllDay,
            "status": eventStatus(ev.status),
        ])
    }
}
lookupEvents.sort {
    (($0["start"] as! String), ($0["id"] as! String)) < (($1["start"] as! String), ($1["id"] as! String))
}
writeJSON(lookupEvents, to: outDir.appendingPathComponent("calendar/lookup.json"),
          what: "lookup.json")

func calType(_ c: EKCalendar) -> String {
    switch c.type {
    case .local: return "local"
    case .calDAV: return "caldav"
    case .exchange: return "exchange"
    case .subscription: return "subscription"
    case .birthday: return "birthday"
    @unknown default: return "other"
    }
}
let lookupIds = Set(lookupCals.map { $0.calendarIdentifier })
var allCalendars: [[String: Any]] = all.map { c in
    ["title": c.title,
     "source": c.source?.title ?? "unknown",
     "type": calType(c),
     "exported": selectedIds.contains(c.calendarIdentifier),
     "lookup": lookupIds.contains(c.calendarIdentifier)]
}
allCalendars.sort {
    (($0["title"] as! String), ($0["source"] as! String)) < (($1["title"] as! String), ($1["source"] as! String))
}

// calendar-meta.json - feeds manifest.json. Lives in the trusted state dir.
let metaFmt = ISO8601DateFormatter()
let meta: [String: Any] = [
    "version": VERSION,
    "generated_at": metaFmt.string(from: now),
    "count": events.count,
    "calendars": selected.map { $0.title }.sorted(),
    "lookup_calendars": lookupCals.map { "\($0.title) (\($0.source?.title ?? "unknown"))" }.sorted(),
    "lookup_count": lookupEvents.count,
    "all_calendars": allCalendars,
    "reminders_count": reminderRecords.count,
    "reminder_lists": reminderLists,
    "errors": warnings,
]
let stateURL = opts.stateDir.isEmpty
    ? outDir.appendingPathComponent("state")
    : URL(fileURLWithPath: (opts.stateDir as NSString).expandingTildeInPath)
writeJSON(meta, to: stateURL.appendingPathComponent("calendar-meta.json"),
          what: "calendar-meta.json")

for w in warnings {
    FileHandle.standardError.write(Data((w + "\n").utf8))
}
print("calexport: \(events.count) events, \(lookupEvents.count) lookup, \(reminderRecords.count) reminders")
