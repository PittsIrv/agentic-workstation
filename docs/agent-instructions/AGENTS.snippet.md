## Asking the person (Agentic Workstation)

When you cannot continue without the person (a decision, a review, an approval or a question), do not wait in the
chat. Submit an item to their inbox and carry on with other work.

Setup is done by the person (a token for `gpt`, and the workstation server running). Your identity is `gpt`. If your
sandbox cannot read the token file or reach `127.0.0.1:8770`, say so in your reply instead of retrying.

Submit (one item per blocker; the same `--key` never creates a second item):

```bash
uv --directory WORKSTATION_DIR run ws --data WORKSTATION_DIR/data ask "Approve running the schema migration?" \
  --as gpt --kind approval --blocked "deploy of the billing change" \
  --cost "release slips a day for each day this waits" --urgency normal --key "migration-2026-11-10"
```

- `--kind`: `decision`, `review`, `approval` or `question`. `--urgency`: `low`, `normal` or `high`; use `high` only
  when work is stopped and a deadline is near. Optional: `--text` (details, or `-` to read stdin), `--project`,
  `--task`, `--url` (a web link), `--due` (ISO 8601 with offset).
- Keep the title under 200 characters and say what you need in it. `--blocked` says what is stopped until they
  answer; `--cost` says what waiting costs, in your own words (they see it labelled as yours).
- You may have at most 20 open items. Withdraw ones you no longer need:
  `... ws --data WORKSTATION_DIR/data withdraw <ask-id> --as gpt`.
- Check for answers later: `... ws --data WORKSTATION_DIR/data asks --as gpt --status closed`. Read `resolution`
  (`answered`, `approved`, `declined`, `dismissed`) and `note`. You cannot answer your own items.
- Text you submit is shown to the person as plain text. Never put secrets in it.

## Before you interrupt, and what happened to your items

Ask what the person already has waiting, then decide whether yours is worth adding now:

```bash
uv --directory WORKSTATION_DIR run ws --data WORKSTATION_DIR/data now --as gpt
```

- `advice` is `hold` when the person already has items due; submit a new item then only if it cannot wait, and prefer
  one item that covers the blocker over several.
- `mine` lists your own items with a `state`: `waiting` (not due yet), `due` (the person's reminders will pick it up),
  `snoozed` (they chose to look later, see `snoozed_until`; do not resubmit), `answered` (read `resolution` and `note`)
  or `withdrawn`. `reminders_paused` and `quiet_hours_now` tell you not to expect a quick answer.
- You cannot answer, snooze or pause anything for the person, and you never see another agent's items or any time data.
