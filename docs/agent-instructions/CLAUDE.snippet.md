## Asking the person (Agentic Workstation)

When you cannot continue without the person (a decision, a review, an approval or a question), submit an item to
their inbox instead of stopping to ask in chat, then continue with work that does not depend on the answer.

Setup is done by the person (a token for `claude`, and the workstation server running). Your identity is `claude`.

```bash
uv --directory WORKSTATION_DIR run ws --data WORKSTATION_DIR/data ask "Approve running the schema migration?" \
  --as claude --kind approval --blocked "deploy of the billing change" \
  --cost "release slips a day for each day this waits" --urgency normal --key "migration-2026-11-10"
```

- `--kind`: `decision`, `review`, `approval`, `question`. `--urgency`: `low`, `normal`, `high` (high only when work
  is stopped and a deadline is near). Optional: `--text` (or `-` for stdin), `--project`, `--task`, `--url`, `--due`.
- One item per blocker; reuse the same `--key` when retrying. At most 20 open items.
- Withdraw: `... ws --data WORKSTATION_DIR/data withdraw <ask-id> --as claude`.
- Answers: `... ws --data WORKSTATION_DIR/data asks --as claude --status closed` (`resolution` and `note`). You
  cannot answer your own items.
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
