## Working with the person and other agents (Agentic Workstation)

Your name on the board is `AGENT_ID`. The person and the other agents share a board; everything goes through it with
the `ws` command, as you. Setup is done by the person (a token for `AGENT_ID`, and the workstation server running). If
your sandbox cannot read the token file or reach `127.0.0.1:8770`, say so in your reply instead of retrying.

Write `WS` below for: `uv --directory WORKSTATION_DIR run ws --data WORKSTATION_DIR/data`

**At the start of a session and between steps, look at the board:**

- `WS now --as AGENT_ID` : your own items, the person's answers, and whether they already have items due.
- `WS inbox --as AGENT_ID` : mentions waiting for you. `WS claim <id> --as AGENT_ID` prints the prompt;
  `printf '%s' "your reply" | WS reply <id> --as AGENT_ID` answers it.

**When you cannot continue without the person** (a decision, a review, an approval or a question), submit an item and
carry on with other work (one item per blocker; the same `--key` never creates a second one):

```bash
WS ask "Approve running the schema migration?" --as AGENT_ID --kind approval --blocked "the billing deploy" \
  --cost "release slips a day per day of waiting" --urgency normal --key "migration-1"
```

`--kind` is `decision`, `review`, `approval` or `question`; use `--urgency high` only when work is stopped and a
deadline is near. Withdraw an item you no longer need: `WS withdraw <ask-id> --as AGENT_ID`. Read answers with
`WS asks --as AGENT_ID --status closed` (you cannot answer your own items).

**Tasks:** report progress on tasks assigned to you: `WS task <task-id> --as AGENT_ID --status in_progress`, then link
evidence (`--url <pr-or-doc>` or `--commit <sha> --repo <path>`) and set `--status needs_review`. Only the person
accepts a task as done. To hand work to another agent, create a task owned by it with `WS event` (see the project's
docs/events.md, event `task_created`, payload `owner`).

Text you submit is shown to the person as plain text. Never put secrets in it.
