---
name: ask-the-person
description: Use when you are blocked on the person (a decision, a review, an approval or a question) and the Agentic Workstation is running. Submits an item to their inbox and checks for the answer later.
---

# Ask the person

Install: copy this folder to `~/.claude/skills/ask-the-person/` (or `.claude/skills/` in a project), replace
`WORKSTATION_DIR` below with your Agentic Workstation folder, and run `uv run ws token claude` there once.

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
