---
name: ask-the-person
description: Use when you are blocked on the person (a decision, a review, an approval or a question) and the Agentic Workstation is running. Submits an item to their inbox, checks what the person already has waiting before interrupting, and reads the answer later.
---

# Ask the person

Setup is done by the person: a token for `claude` and the workstation server running. Your identity is `claude`.

Submit one item per blocker, then continue with work that does not depend on the answer:

```bash
uv --directory WORKSTATION_DIR run ws --data WORKSTATION_DIR/data ask "<what you need, under 200 characters>" \
  --as claude --kind <decision|review|approval|question> --blocked "<what is stopped>" \
  --cost "<what waiting costs>" --urgency <low|normal|high> --key "<stable key for this blocker>"
```

- `high` only when work is stopped and a deadline is near. Optional: `--text` (or `-` for stdin), `--project`,
  `--task`, `--url`, `--due` (ISO 8601 with offset).
- Reuse the same `--key` when retrying: it never creates a second item. At most 20 open items.
- Withdraw one you no longer need: `... ws --data WORKSTATION_DIR/data withdraw <ask-id> --as claude`.
- Read answers: `... ws --data WORKSTATION_DIR/data asks --as claude --status closed`; use `resolution`
  (`answered`, `approved`, `declined`, `dismissed`) and `note`. You cannot answer your own items.
- Everything you submit is shown as plain text. Never include secrets.

## Before you interrupt, and what happened to your items

Ask what the person already has waiting, then decide whether yours is worth adding now:

```bash
uv --directory WORKSTATION_DIR run ws --data WORKSTATION_DIR/data now --as claude
```

- `advice` is `hold` when the person already has items due; submit a new item then only if it cannot wait, and prefer
  one item that covers the blocker over several.
- `mine` lists your own items with a `state`: `waiting` (not due yet), `due` (the person's reminders will pick it up),
  `snoozed` (they chose to look later, see `snoozed_until`; do not resubmit), `answered` (read `resolution` and `note`)
  or `withdrawn`. `reminders_paused` and `quiet_hours_now` tell you not to expect a quick answer.
- You cannot answer, snooze or pause anything for the person, and you never see another agent's items or any time data.
