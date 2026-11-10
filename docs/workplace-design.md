# The workplace: design for the corrected direction

Status: proposal for the owner to confirm. **[Mine]** marks a recommendation, **[Yours]** an open choice for the owner. Every "exists" below was checked in the code on 2026-11-10.

## Direction

The workspace is the shared place between a person and their agents. Codex, Claude Code, Muse and any other agent (and an optional personal assistant) do the planning, reminding, timing and talking. The workspace only holds the board they share and shows it to the person. Test for every feature: would an agent or assistant do this if it could read or write the board? If yes, expose the board and stop.

## 1. The workplace in one picture

| Who | Reads from the board | Writes to the board |
|---|---|---|
| The person (owner) | everything, in one front end | answers, accepts or cancels tasks, assigns, opens threads, adds agents |
| An agent (Codex, Claude Code, Muse, ...) | its own items and answers, tasks by id, threads it is in, mentions addressed to it | items for the person (`ws ask`), tasks and their status, artifacts, reviews, replies |
| A personal assistant (optional) | `ws now` / `GET /api/needs-now` (what needs the person, with why) | whatever an agent may write, under its own identity |
| The workspace | nothing outside the board | nothing outside its own data folder; calls no agent and no network service |

Exists: owner-only limits, three fixed identities (`claude`, `gpt`, `gemini`), added identities (this change), tasks, threads with mentions, decisions, the inbox, `needs-now`, the weekly review.

## 2. Agents working with each other

| Step | How, today | Status |
|---|---|---|
| A assigns a task to B | A creates a task with `owner: B` (`ws event`, event `task_created`) | exists for `claude` and every added agent. `gpt` and `gemini` can only suggest or create tasks for the person |
| B learns it has work | B asks the board when it starts and between steps; its instructions say so. Mentions: `ws inbox --as B`, then `ws claim`. Tasks: B must be told the task id (a mention or a note) | **gap**: B cannot list tasks assigned to it (`GET /api/tasks` is owner only) |
| B works | `ws task <id> --as B --status in_progress`, link evidence (`--url`, `--commit`) | exists |
| B asks a question | to the person: `ws ask`. To another agent: there is no such object; an agent's @mention in a thread is only a suggestion the person can accept | **gap, choice [Yours]**: let an agent's mention dispatch another agent, with the thread's turn budget as the cap, or keep the person in the loop |
| B hands back | B sets `needs_review` with an artifact; a reviewer agent records a review (`review_record`) | exists. Only the person accepts `done` |
| The person sees and steers | Home ("Your agents": working on, waiting on you), Tasks, Threads, Inbox; reassign, cancel, accept, answer | exists |

How an agent learns of work without the server calling it: the server runs nothing for it. The agent asks the board at session start and between steps, because its instructions (`docs/agent-instructions/AGENT.snippet.md`) tell it to. An idle agent needs its own scheduler to look; that belongs to the agent, not here.

**Worked example (run against a live local server, fake runners, scratch data folder).** `$WS` is `ws --data <folder>`.

```
$WS agent add codex --display Codex      # also: agent add muse --display Muse
$WS event task.json --as codex           # Codex assigns "Draft the migration plan" to muse   -> status ready
GET /api/tasks as muse                   # 403: an agent cannot list tasks (the gap)
person writes "@muse please take <task-id>" in a thread (the page does this)
$WS inbox --as muse                      # one queued mention, not failed as "offline"
$WS claim <dispatch> --as muse           # prints the prompt
$WS task <id> --as muse --status in_progress
$WS task <id> --as muse --url https://example.com/migration-plan --kind doc --title Plan
$WS task <id> --as muse --status needs_review
$WS ask "Can the migration run during business hours?" --as muse --kind question --blocked "rollout" --key m1
$WS answer <ask> --note "No, after 21:00"        # the person
$WS asks --as muse --status closed               # Muse reads: answered, "No, after 21:00"
printf "Plan drafted. Rollout after 21:00." | $WS reply <dispatch> --as muse
POST /api/tasks/<id>/reviews as codex    # approve (no CLI command yet)
$WS task <id> --as codex --status done   # 403: codex may only update its own tasks
$WS task <id> --status done              # the person accepts
```

Result: the thread shows the person's message, a notice that Muse works through the board, and Muse's reply; the task is `done`. Two steps needed raw HTTP (reading a task by id, recording a review).

## 3. Talking to agents

The workspace's part: a place to address one agent or several (`@muse @codex ...` in a thread), and to show each reply as a message by its author. It exists: threads, mentions, replies from `ws reply`. The agent's part: reading the mention, thinking, answering.

- The person addresses agents in the thread box. Several mentions in one message dispatch each.
- A reply appears as a message from that agent when it posts it (`ws reply`). For `claude` and `gpt` the server can also run the local CLI itself when it is installed (tools off / read-only sandbox). **Flag for the Router**: that is a server-side agent call that exists today (PR before this work); under the corrected direction it should be a deliberate opt-in, not a default. Added agents never get it.
- Speech **[Mine]**: no code. The operating system's dictation (macOS Dictation, or the phone keyboard's microphone) types into the existing message box, and nothing is recorded or sent by the workspace. Dictation by the OS may use the vendor's cloud and needs the microphone permission the person grants to the OS.
- Optional later **[Yours]**: a microphone button using the browser's speech recognition. In Chrome that sends audio to a cloud speech service (a network service) and asks for microphone permission each site; Safari uses its own. Not built; I recommend waiting.

## 4. Any agent, not three fixed ones

Exists after this change: the owner adds an agent kind by name from **Set up > Add another agent** or `ws agent add <name> [--display D] [--role R]`. The name is lowercase letters, digits, `-`, up to 32, not `owner`, `claude`, `gpt`, `gemini` or `system`. The result: a stored identity (`<data>/agents.json`), a token file (`<data>/secrets/<name>.token`, never printed), and copy-ready instructions with the name and folders filled in. It appears under **Your agents** ("works through the board"). Capabilities **[Mine]**: the peer set `claude` has, plus recording reviews; no owner-only action. Mentions of it wait for it to pick them up; the server never runs it.

**Muse.** I read the vendor's documentation and take Muse to be **Muse Code, Meta's terminal coding agent** (beta, macOS and Linux). Sources: Meta's launch post `research.meta.ai/blog/introducing-muse-code-and-muse-spark-1-2` and the docs at `dev.meta.ai/docs` (configuration and extending pages). **[Yours]** to confirm this is the tool you meant; I found nothing in this group's notes that names it. What the docs say it needs:

- Project rules: `AGENTS.md` (then `CLAUDE.md`, `.agents/AGENTS.md`, `.claude/CLAUDE.md`). Project files load only after the workspace is trusted; user-level rules always load. So the generated instructions work as they are.
- Skills: it also scans `~/.claude/skills` and `~/.codex/skills`. Our Claude Code skill `ask-the-person` says `--as claude`, so Muse could pick it up and submit as `claude`. Keep that skill out of Muse's path; give Muse its own generated instructions.
- It runs commands inside an OS sandbox with approvals. Whether the sandbox lets it reach `127.0.0.1:8770` and read `data/secrets/muse.token` is **not stated in the pages I read**; test once. A third-party article mentions a `--sandbox-network` flag; unverified.
- MCP is supported but not needed; the board is plain commands. Headless runs (`muse exec`) exist for an agent-side scheduler.

## 5. The personal assistant and the phone

The board runs on the person's machine and listens on `127.0.0.1` only; the server also refuses other host names. So today **nothing reaches a phone**. Options:

| Option | What reaches the phone | Security | Cost |
|---|---|---|---|
| **A. The assistant carries it [Mine]** | The assistant (on the same machine) runs `ws now` and sends a notification through its own channel. The board is never exposed. | Board stays local. The assistant's channel carries titles of items, which the person chooses. | Whatever the assistant costs; the workspace adds none |
| B. Same Wi-Fi | The page itself, if the server listened on the local network | Needs a listen-address change, a host allowlist change and sign-in over unencrypted HTTP on the network. Not built, not recommended | None |
| C. Private tunnel (a mesh VPN or tunnel service) | The page, from anywhere, through an encrypted link | Strong if only the person's devices join; the sign-in code still applies. A service account is required | Free tiers exist for some services; terms not checked here. **Money: [Yours]** |
| D. Hosted relay | Notifications or a copy of the board through a server someone runs | Board data leaves the machine; a new party to trust and secure | Hosting cost. **[Yours]**, and I advise against it now |

**[Yours]** to choose; nothing is built for this now. A needs one thing from the workspace: a cheap way for the assistant to ask "what changed since I last looked" (slice 2 below).

## 6. What the workspace should not do

- Decide when, how often or through which channel to remind the person.
- Plan, prioritise or schedule the person's day.
- Call an agent or a model itself (the existing `claude`/`gpt` runners should become opt-in).
- Record, store or send speech.
- Read mail, calendar or screen time as part of its job (the Time page reads Daybook locally and stores nothing; keep it that way).
- Listen on the network, or hold accounts, by default.
- Open an owner-only action to an agent: answering, accepting, reassigning, cancelling, settings, adding agents.
- Hold personal data in the repository.

The built-in reminder rule from PR #12 stays in the code, **off by default** (`[reminders] enabled = true` turns it on), described as an optional local fallback for people with no assistant. The reminder log, banner, pause and `ws remind` appear only when it is on. `ws now` and `GET /api/needs-now` stay on: they are how an assistant or an agent reads the board. **[Yours]**: delete the rule later if it goes unused.

## 7. The next two slices (smallest first) **[Mine]**

1. **Agents work from the board without HTTP.** `ws mine --as <agent>` (tasks assigned to me, via a new read-only `GET /api/my-tasks`), `ws task-new` (assign a task, optionally to another agent), `ws review` (record a review), `ws task <id> --show`. Removes the gap and both raw-HTTP steps in the example.
2. **A change feed for assistants.** `GET /api/changes?since=<seq>` and `ws changes --since N`: what changed on the board that concerns this identity (the person gets everything; an agent gets its own). Read-only; it is what a phone assistant or an idle agent polls.

After that, the choices that are yours: agent-to-agent mentions, whether an assigning agent may accept work, the phone path.
