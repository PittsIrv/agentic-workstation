# Unified workspace: design note

Goal: one local place for the person's time, an inbox where their agents (Codex, Claude Code, others) submit
"this needs you" items, and reminders at the right moment. Open source, usable by anyone running either CLI.

Items marked **(proposal)** are recommendations, not decisions. Defaults marked **(default)** are this project's own
choices, named and configurable where stated; none comes from research.

## Standing limits

- No real agent call from the server. No new owner-only action opened to agents. The server never writes into other repos.
- Public repo: no personal data. Integrations read local data at run time, work on another person's machine, or switch off cleanly with a message that says how to connect.
- Nothing needs a paid service. No macOS permission is worked around; where one is needed the panel says what to grant and stops.

## 1. Agent-submitted items for the person

**Existed before this slice:** pending decision cards (agents may propose; options, urgency, due date; only the owner resolves), tasks that reach `needs_review` or `blocked`, an `agent_blocked` event that only appears in Activity, an event intake endpoint, tokens per agent. Home showed decisions and tasks separately. There was no way for an agent to say "I need a review / approval / answer", no record of what is blocked or what waiting costs, and no single list.

**Added (slice 1):** an *ask*: kind (`decision`, `review`, `approval`, `question`), title, details, what is blocked, what leaving it costs (the agent's words), urgency, optional project, task, link, deadline. Submitted with `ws ask`, `POST /api/asks` or the `ask_created` event. The Inbox page and `GET /api/inbox` list asks together with pending decisions and tasks in `needs_review` or `blocked`, with who asked and how long it has waited. The owner answers asks in the Inbox (approve, decline, dismiss, or free text); agents read the outcome with `ws asks --status closed`. Real decision cards keep their own page (they have options and create a task); the inbox links to them.

Order **(default)**: overdue first; then high, normal, low urgency; then the earliest deadline; then the item that has waited longest. A decision's urgency is its own field; a task's comes from priority (P0 and P1 high, P2 normal, P3 low).

Limits **(default)**: title 200 characters, details 2000, blocked and cost 300; 20 open items per agent (`[inbox] max_open_per_agent`). Text is stored and shown as plain text; links must be `http(s)` or in-app.

**Still missing:** agent identities are the fixed set `claude`, `gpt` (Codex) and `gemini`; an arbitrary third agent has no id **(proposal: later, a configured list of extra agent ids, a design decision for the owner)**. Asks are not in the weekly review yet. No expiry for asks nobody answers.

## 2. Time: where it went

**Status (slice 2, built):** `TimeSource` interface plus a Daybook reader in `src/workstation/timesources.py`; `GET /api/time`, `ws time` and a Time page. Daybook is read through its own loopback API (`/api/health`, `/api/data`), not its files, so this program needs no macOS permission and Daybook's labels and overlap handling are reused. Finding that matters for slice 3: Daybook records finished days (yesterday, collected after 8 AM) and its hourly sessions need a separate exporter with Full Disk Access, so today's live focus is **not** available without a permission the user must grant in Daybook. Slice 3 should therefore not depend on "in focused work right now" from Daybook; options are in the Router note.

**Exists (before slice 2):** nothing. Home shows only what was recorded in the workstation.

**Needed:** per-day focused-time and app/category totals for today and the week, from a source the person already has. First source: Daybook (a Mac Screen Time collector), read at run time from its local data folder, read-only. **(proposal)** a small `TimeSource` interface with one method returning day totals (`day`, `seconds`, `category`/`app` label), so another source can be added without touching the rest. When Daybook is absent or unreadable the panel says "not connected" and how to connect; nothing else breaks. Where Daybook keeps its data and what it needs (macOS permissions) is read from Daybook's own documentation at slice time, not assumed here.

**Data and where it lives:** read at request time, held in memory, never copied into the event log or the repo.

## 3. Reminders: what needs you now

**Status (slice 3, built):** `src/workstation/reminders.py`, `GET /api/needs-now`, `ws now`, `ws remind`, snooze and pause (events `item_snoozed`, `reminders_paused`, `reminders_resumed`, `reminder_sent` in the log), a settings table under Set up, the reminder log on Activity, a banner.

**What needs you now:** one function over the inbox. An item is *due* if overdue, or its deadline is within `deadline_window_minutes` **(default 24 h)**, or it has waited at least the minutes for its urgency **(default 30 high, 240 normal, 1440 low)**. Snoozed items are left out until the snooze ends. Each item carries `reasons` (overdue, due_soon, waited, and `blocks` with the agent's words, labelled) and a `why` sentence. Home, the API, `ws now` and `ws remind` use this one list.

**Reminder rule (conservative):** due, and not paused, and outside quiet hours **(default 21:00 to 08:00 local, may cross midnight)**, and under the daily cap **(default 3 per local day)**, and not reminded within `repeat_minutes` **(default 1440)**, never once answered or snoozed. Highest-ranked first, up to the remaining cap. Time zone is the display zone the review page uses. Because the program cannot see the owner's current activity, the rule leans quiet: fewer, later reminders. Prior-work check: timing interruptions well is an open problem and assistants over-interrupt, so there is no learned timing and no scoring.

**Delivery:** (a) in the page: a banner while something needs the owner (hidden in quiet hours and while paused; it records nothing and does not use the cap, since the owner is looking); (b) `ws remind` for a schedule: prints due reminders and records them (the cap and repeat interval apply to what it records); `--notify` also asks the system for a notification with the text passed as arguments, never inside a script. A system notification may need a permission the user grants themselves (macOS: notifications for Script Editor); the log records that a notification was requested, not that it was shown. The server sends nothing off the machine and makes no agent call.

**For agents:** `GET /api/needs-now` / `ws now --as <agent>` returns only that agent's items with a state (waiting, due, snoozed, answered, withdrawn), counts of the owner's due and waiting items, `advice` (hold or clear), and whether reminders are paused or in quiet hours. No other agent's titles and no time data. Snooze, pause, run, settings and the log are owner only.

**Reminder log:** each recorded reminder with what happened to its item: answered within `acted_window_minutes` **(default 120)**, answered later, snoozed, or no action yet. No model, no score.

**Not done:** using Daybook's yesterday pattern for timing (optional, the owner's call); a learned or per-item snooze suggestion; reminders for items an agent expires; edit settings in the page (they are in the config file).

## 4. For other people

Setup is `git clone`, `uv sync`, `uv run ws serve`, `uv run ws token <agent>`. Ready-to-copy instructions live in `docs/agent-instructions/`: an `AGENTS.md` snippet for Codex, and a skill plus a `CLAUDE.md` snippet for Claude Code. They teach the submit, withdraw and read-the-answer commands. Slice 3 adds the "what needs the person now" command to the same files. Codex's sandbox may block localhost or the token file; the snippet tells the agent to say so rather than retry.

## The front end: one shell (slice 2, built)

Requirement from the owner: one front end, easy to use. Everything below is **(proposal)** unless stated. The look stays the existing one (same fonts, colours, spacing); what changes is structure.

One shell, one navigation, on every screen: `Home · Inbox · Decisions · Tasks · Threads · Review · Time · Activity · Set up`. The inbox count shows on Inbox. On a phone the navigation wraps and every section stacks in the same order.

```
Home
+--------------------------------------------------------------+
| Needs you now                         3 waiting · All in Inbox|
|  high  Approve the schema migration?   Claude · waited 2 h    |
|        Blocked: deploy   If it waits: release slips           |
|        [note........] [Approve] [Decline] [Dismiss]           |
|  ...up to 5 rows, same row and buttons as the Inbox page...   |
+------------------------------+-------------------------------+
| Your time                    | Your agents                   |
| latest recorded day, 7 bars, | Claude  ready                 |
| split Focus/Ent./Other       |  waiting on you: 2 items      |
| (or "Not connected" + how)   |  working on: 1 task           |
|                              | GPT ...                       |
+------------------------------+-------------------------------+
| Spend: not connected. Nothing reports agent cost yet.        |
+--------------------------------------------------------------+
| Work: tasks waiting on you, threads, projects, tickets,      |
| what this dashboard can see                                  |
+--------------------------------------------------------------+
```

- **Answering is the same everywhere:** inbox rows, decision cards and review tasks use one row pattern; the answer buttons sit in the row, risky ones ask for a second click. A decision's options and a review task's Accept/Send back appear in place in the Inbox and on Home, so nothing needs a page change.
- **What moves where:** pending decisions and tasks to review leave Home's separate lists and appear in "Needs you now"; the **Decisions** view lists all decisions (pending and resolved); **Tasks**, **Threads**, **Review**, **Activity** keep their pages inside the same shell; the old Time page becomes the Home "Your time" section plus its own full view; "What this dashboard can see" moves under Set up.
- **First run:** while no item has ever been submitted, Home opens with a "Get started" strip instead of an empty inbox: (1) create a token for each agent (a button, no command), (2) copy the instructions for Codex or Claude Code (already filled in with this install's paths), (3) run one test command and watch the page confirm the first item. The same steps stay available under **Set up**.
- **Empty states** say what to do next, not only that there is nothing.
- **Spend** is a place, not a feature: it says no source is connected. A cost source is a later slice.
- **No command for daily use:** answering, approving, accepting, reviewing, reading time and starting threads are all in the page. Still needing a command: starting the server, the one-time sign-in link, and the agents' own submissions.
- **Agent text** (titles, details, blocked, cost, task and decision text, scope names) is shown as text everywhere, including the new Home sections and Set up; the browser test loads each of these pages with hostile text.

## What stays local, never in the repo

The database, tokens, config, screen-time data, local paths, bills, names. All live under `data/` (git-ignored) or in the source application. Tests and docs use invented fixtures only.

## Order of slices (proposal)

1. **Inbox** (this slice): asks, one ordered list, answer in place, agent instructions.
2. **Time panel**: `TimeSource` interface, Daybook reader, "not connected" state, today and this week.
3. **Reminders** (built): `needs-now`, quiet hours, daily cap, snooze and pause, `ws remind`, instructions updated.
4. Later: asks in the weekly review; extra agent ids; billing or cost source behind the same interface style.
