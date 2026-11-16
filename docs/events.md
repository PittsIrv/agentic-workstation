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
| `ask_created` | Item in the owner's inbox (`title`, `kind`: decision, review, approval or question; optional `text`, `blocked`, `cost_of_waiting`, `urgency`, `task_id`, `due_at`, `url`; for `decision` and `question`: `options` (2–6 objects with `label`, optional `cost`, `risk`, `after`), `recommended` (one option's label), `label` (project or ticket, up to 60 characters)). Limits and ordering: README, Inbox. The owner answers with an option id or free text; the agent reads it back with `GET /api/asks/<id>` or `GET /api/asks?since=<ISO time with UTC offset>`, or blocks on it with `GET /api/asks/<id>/wait` (below) | Owner, Claude, GPT, Gemini |
| `agent_blocked` | Log the block; with `payload.task_id`, set that task `blocked` | Owner, Claude, GPT, Gemini |
| `thread_mention` | Post `payload.text` to `payload.thread_id`; mentions dispatch only if the sender is the owner | Owner (agents' mentions stay suggestions) |

Rules:
- `idempotency_key` is required and scoped to the sender. A repeat returns `"outcome": "duplicate"` and writes nothing.
- A due time without a UTC offset needs `due_tz` (IANA). Nothing is guessed.
- Responses: `created`, `duplicate`, `merged`, `updated`, `recorded`; errors are 401 (no/unknown token), 403 (not allowed), 404, 409 (version or state conflict), 422 (invalid).

## Waiting for the owner's answer (`GET /api/asks/<id>/wait`)

An agent that submitted an item (through `ask_created` here, `POST /api/asks` or `ws ask`) can block on it instead of polling. The request is a long-poll with the sender's own token:

```
GET /api/asks/<ask-id>/wait?timeout=30
→ 200 {"outcome": "answered", "ask": {"id": "...", "status": "closed", "resolution": "answered", "option_id": "o2", "note": "...", ...}}
```

| `outcome` | Meaning | `ws wait` exit status |
|---|---|---|
| `answered` | The owner closed the item. `ask.resolution` is `answered`, `approved`, `declined` or `dismissed`; `ask.option_id` is the chosen option, `ask.note` the owner's text | 0 |
| `timeout` | `timeout` seconds passed and the item is still open. Ask again to keep waiting | 3 |
| `withdrawn` | The agent that submitted the item withdrew it; there will be no answer | 4 |

Rules:
- `timeout` is in seconds, from 0 to 60 (default 30); anything else is 422. An item that is already answered returns at once. For a longer wait, repeat the request: `ws wait <ask-id> --as <id> --timeout <seconds>` does that (default 300 s) and turns the outcome into the exit status above (1 is any error).
- Own items only, exactly like `GET /api/asks/<id>`: an agent that waits on another agent's item gets the same 404 as for an unknown id, immediately, with nothing about the item in it. The owner may wait on any item.
- Waiting only reads. It records no event and gives an agent no way to answer, snooze or change an item.
