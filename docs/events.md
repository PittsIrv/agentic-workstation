# Event intake contract (`POST /api/events`)

For assistants and bridges that report into the workstation. Authenticate with the sender's own token (`Authorization: Bearer …`, from `ws token <id>`); the author is taken from the token, never from the body.

```json
{
  "event_type": "task_created",
  "idempotency_key": "gmail:<message-id>:interview_scheduled",
  "project_id": "job-search",
  "entity_key": "company:<name>|role:<role>",
  "source_system": "gmail",
  "occurred_at": "2026-11-06T12:00:00-05:00",
  "provenance": {"confidence": "verified_from_source", "source": "<link or description>"},
  "payload": {"title": "...", "next_action": "...", "owner": "owner",
              "due_at": "2026-11-11T21:00:00", "due_tz": "America/New_York",
              "evidence_url": "https://...", "why": "..."}
}
```

| `event_type` | Effect | Who may send it |
|---|---|---|
| `task_created` | New task, or `merged` into the open task with the same `project_id` + `entity_key` | Claude (any owner), Gemini (owner must be the owner), GPT (becomes an `inbox` suggestion for the owner), owner |
| `task_updated` / `task_completed` | Change a task by `payload.id` or `entity_key`; `expected_version` for optimistic concurrency | Owner; agents only for their own tasks, never `done`/`canceled` |
| `decision_required` | Pending decision card (≥ 2 options) | Owner, Claude, GPT |
| `decision_resolved` | Approve / reject / defer | Owner only |
| `ask_created` | Item in the owner's inbox (`title`, `kind`: decision, review, approval or question; optional `text`, `blocked`, `cost_of_waiting`, `urgency`, `task_id`, `due_at`, `url`). Limits and ordering: README, Inbox | Owner, Claude, GPT, Gemini |
| `agent_blocked` | Log the block; with `payload.task_id`, set that task `blocked` | Owner, Claude, GPT, Gemini |
| `thread_mention` | Post `payload.text` to `payload.thread_id`; mentions dispatch only if the sender is the owner | Owner (agents' mentions stay suggestions) |

Rules:
- `idempotency_key` is required and scoped to the sender. A repeat returns `"outcome": "duplicate"` and writes nothing.
- A due time without a UTC offset needs `due_tz` (IANA). Nothing is guessed.
- Responses: `created`, `duplicate`, `merged`, `updated`, `recorded`; errors are 401 (no/unknown token), 403 (not allowed), 404, 409 (version or state conflict), 422 (invalid).
