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

**Exists:** nothing. Home shows only what was recorded in the workstation.

**Needed:** per-day focused-time and app/category totals for today and the week, from a source the person already has. First source: Daybook (a Mac Screen Time collector), read at run time from its local data folder, read-only. **(proposal)** a small `TimeSource` interface with one method returning day totals (`day`, `seconds`, `category`/`app` label), so another source can be added without touching the rest. When Daybook is absent or unreadable the panel says "not connected" and how to connect; nothing else breaks. Where Daybook keeps its data and what it needs (macOS permissions) is read from Daybook's own documentation at slice time, not assumed here.

**Data and where it lives:** read at request time, held in memory, never copied into the event log or the repo.

## 3. Reminders: what needs you now

**Exists:** nothing; the inbox ordering in part 1 is the input.

**Rule (proposal), the simplest one unlikely to annoy:** surface an item when (a) it is high urgency, overdue, or has waited longer than a configurable time, and (b) the person is not in focused work (from part 2), and (c) it is outside quiet hours, and (d) today's reminder count is under a daily cap. Otherwise stay silent; the Inbox badge still shows the count. Every number is configurable with a named default **(default, to be set in slice 3)**: minutes waited, quiet hours, daily cap, what counts as focus. One reminder per item per day at most. A muted or ignored reminder is the failure to avoid, so fewer, later reminders beat more.

**Surface:** `GET /api/needs-now` and `ws needs-now` (so a planning agent can ask), plus a local notification path **(proposal)** through the macOS notification centre on Macs and nothing elsewhere, off unless enabled. No model-based planner.

## 4. For other people

Setup is `git clone`, `uv sync`, `uv run ws serve`, `uv run ws token <agent>`. Ready-to-copy instructions live in `docs/agent-instructions/`: an `AGENTS.md` snippet for Codex, and a skill plus a `CLAUDE.md` snippet for Claude Code. They teach the submit, withdraw and read-the-answer commands. Slice 3 adds the "what needs the person now" command to the same files. Codex's sandbox may block localhost or the token file; the snippet tells the agent to say so rather than retry.

## What stays local, never in the repo

The database, tokens, config, screen-time data, local paths, bills, names. All live under `data/` (git-ignored) or in the source application. Tests and docs use invented fixtures only.

## Order of slices (proposal)

1. **Inbox** (this slice): asks, one ordered list, answer in place, agent instructions.
2. **Time panel**: `TimeSource` interface, Daybook reader, "not connected" state, today and this week.
3. **Reminders**: `needs-now` computation from 1 and 2, quiet hours and daily cap, notification path, instructions updated.
4. Later: asks in the weekly review; extra agent ids; billing or cost source behind the same interface style.
