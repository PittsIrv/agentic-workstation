"""Append-only event log (SQLite) with in-memory projections.

Every change to tasks, decisions, threads and messages is an event. The `events` table is
the audit trail; the projections in `State` are rebuilt from it on start. `batch()` groups
events into one transaction (e.g. approve a decision + create its task), and callbacks
registered with `after_commit()` run only once that transaction is durable.
"""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("workstation.store")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%y%m%d%H%M%S')}{secrets.token_hex(3)}"


TASK_STATES = ["inbox", "ready", "in_progress", "blocked", "needs_review", "done", "canceled"]
TASK_TRANSITIONS = {
    "inbox": {"ready", "in_progress", "canceled"},
    "ready": {"in_progress", "blocked", "canceled", "inbox"},
    "in_progress": {"blocked", "needs_review", "ready", "canceled"},
    "blocked": {"in_progress", "ready", "canceled"},
    "needs_review": {"in_progress", "done", "blocked", "canceled"},
    "done": {"in_progress"},
    "canceled": {"inbox", "ready"},
}
DECISION_STATES = ["pending", "approved", "rejected", "deferred", "superseded"]


@dataclass
class State:
    tasks: dict[str, dict] = field(default_factory=dict)
    decisions: dict[str, dict] = field(default_factory=dict)
    threads: dict[str, dict] = field(default_factory=dict)
    messages: dict[str, list[dict]] = field(default_factory=dict)  # thread_id -> ordered posts
    dispatches: dict[str, dict] = field(default_factory=dict)
    projects: dict[str, dict] = field(default_factory=dict)
    idempotency: dict[str, str] = field(default_factory=dict)  # key -> event_id
    entity_index: dict[str, str] = field(default_factory=dict)  # "project|entity_key" -> task_id
    events: list[dict] = field(default_factory=list)


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY,
  event_id TEXT NOT NULL UNIQUE,
  idempotency_key TEXT UNIQUE,
  event_type TEXT NOT NULL,
  actor TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  body TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._connect()
        self.db.executescript(SCHEMA)
        self._depth = 0
        self._after: list[Callable[[], None]] = []
        self._load()

    def _load(self) -> None:
        """Replay into a fresh state and swap it in whole, so readers never see a half-built one."""
        with self.lock:
            fresh = State()
            for (body,) in self.db.execute("SELECT body FROM events ORDER BY seq"):
                self._apply(json.loads(body), fresh)
            self.state = fresh

    @contextmanager
    def batch(self):
        """All events appended inside commit together or not at all."""
        with self.lock:
            if self._depth == 0:
                self.db.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self._abort()
                raise
            self._depth -= 1
            if self._depth == 0:
                try:
                    self.db.execute("COMMIT")
                except BaseException:
                    self._abort()
                    raise
                after, self._after = self._after, []
                for fn in after:
                    try:
                        fn()
                    except Exception:  # the command is already durable; a hook failure must not undo that
                        log.exception("after-commit hook failed")

    def _connect(self) -> None:
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")

    def _abort(self) -> None:
        """Roll back and rebuild memory, which had applied the rolled-back events. If the connection
        cannot be shown clean, replace it so the replay reads only committed events."""
        if self.db.in_transaction:
            try:
                self.db.execute("ROLLBACK")
            except sqlite3.Error:
                pass  # SQLite may already have rolled back (e.g. disk full)
        if self.db.in_transaction:
            try:
                self.db.close()
            finally:
                self._connect()
        self._after.clear()
        self._load()

    def after_commit(self, fn: Callable[[], None]) -> None:
        with self.lock:
            if self._depth:
                self._after.append(fn)
            else:
                fn()

    # ---- writing -------------------------------------------------------------------------
    def append(self, event_type: str, actor: str, payload: dict[str, Any], *,
               idempotency_key: str | None = None, source_system: str = "workstation",
               project_id: str | None = None, entity_key: str | None = None,
               occurred_at: str | None = None, provenance: dict | None = None) -> tuple[dict, bool]:
        """Record one event. Returns (event, duplicate). A repeated idempotency key returns the
        original event and writes nothing."""
        with self.batch():
            if idempotency_key and idempotency_key in self.state.idempotency:
                eid = self.state.idempotency[idempotency_key]
                return next(e for e in self.state.events if e["event_id"] == eid), True
            ev = {
                "event_id": new_id("evt"),
                "seq": len(self.state.events) + 1,
                "event_type": event_type,
                "actor": actor,
                "source_system": source_system,
                "project_id": project_id,
                "entity_key": entity_key,
                "idempotency_key": idempotency_key,
                "occurred_at": occurred_at or now_iso(),
                "recorded_at": now_iso(),
                "payload": payload,
                "provenance": provenance or {"confidence": "reported"},
            }
            self.db.execute("INSERT INTO events (seq, event_id, idempotency_key, event_type, actor, recorded_at, body)"
                            " VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (ev["seq"], ev["event_id"], idempotency_key, event_type, actor, ev["recorded_at"],
                             json.dumps(ev, ensure_ascii=False)))
            self._apply(ev)
            return ev, False

    def backup(self, out: Path) -> Path:
        """Consistent online copy (SQLite backup API), then reopened to check it replays."""
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            dst = sqlite3.connect(out)
            self.db.backup(dst)
            dst.close()
            expected = len(self.state.events)
        copy = Store(out)
        if len(copy.state.events) != expected:
            raise RuntimeError(f"backup {out} replayed {len(copy.state.events)} of {expected} events")
        copy.db.close()
        return out

    def export_jsonl(self, out: Path) -> int:
        with self.lock:
            rows = [body for (body,) in self.db.execute("SELECT body FROM events ORDER BY seq")]
        Path(out).write_text("".join(r + "\n" for r in rows))
        return len(rows)

    # ---- projections ---------------------------------------------------------------------
    def _touch(self, rec: dict, ev: dict) -> None:
        rec["version"] = rec.get("version", 0) + 1
        rec["updated_at"] = ev["recorded_at"]
        rec.setdefault("history", []).append(
            {"event_id": ev["event_id"], "type": ev["event_type"], "actor": ev["actor"], "at": ev["recorded_at"],
             "status": rec.get("status")})

    def _apply(self, ev: dict, s: State | None = None) -> None:
        s = self.state if s is None else s
        p, t = ev["payload"], ev["event_type"]
        s.events.append(ev)
        if ev.get("idempotency_key"):
            s.idempotency[ev["idempotency_key"]] = ev["event_id"]

        if t == "project_upserted":
            rec = s.projects.setdefault(p["id"], {"id": p["id"], "created_at": ev["recorded_at"]})
            rec.update({k: v for k, v in p.items() if k != "id"})
            self._touch(rec, ev)
        elif t == "task_created":
            rec = {**p, "created_at": ev["recorded_at"], "created_by": ev["actor"], "origin_event": ev["event_id"],
                   "source_system": ev["source_system"], "provenance": ev["provenance"],
                   "artifacts": p.get("artifacts", []), "reviews": []}
            s.tasks[p["id"]] = rec
            if ev.get("entity_key"):
                s.entity_index[f"{ev.get('project_id')}|{ev['entity_key']}"] = p["id"]
            self._touch(rec, ev)
        elif t in ("task_updated", "task_completed"):
            rec = s.tasks[p["id"]]
            rec.update({k: v for k, v in p.items() if k not in ("id", "artifact", "review")})
            if p.get("artifact"):
                rec["artifacts"].append(p["artifact"])
            if p.get("review"):
                rec["reviews"].append(p["review"])
            self._touch(rec, ev)
        elif t == "decision_required":
            rec = {**p, "status": "pending", "created_at": ev["recorded_at"], "created_by": ev["actor"],
                   "origin_event": ev["event_id"]}
            s.decisions[p["id"]] = rec
            self._touch(rec, ev)
        elif t == "decision_resolved":
            rec = s.decisions[p["id"]]
            rec.update({k: v for k, v in p.items() if k != "id"})
            rec["resolved_by"], rec["resolved_at"] = ev["actor"], ev["recorded_at"]
            self._touch(rec, ev)
        elif t == "thread_created":
            rec = {**p, "created_at": ev["recorded_at"], "created_by": ev["actor"]}
            s.threads[p["id"]] = rec
            s.messages[p["id"]] = []
            self._touch(rec, ev)
        elif t == "thread_updated":
            rec = s.threads[p["id"]]
            rec.update({k: v for k, v in p.items() if k != "id"})
            self._touch(rec, ev)
        elif t == "message_posted":
            msgs = s.messages[p["thread_id"]]
            msgs.append({**p, "seq": len(msgs) + 1, "author": ev["actor"], "at": ev["recorded_at"],
                         "event_id": ev["event_id"]})
            self._touch(s.threads[p["thread_id"]], ev)
        elif t in ("thread_mention", "agent_invocation_started", "agent_invocation_failed", "agent_replied"):
            rec = s.dispatches.setdefault(p["dispatch_id"], {"id": p["dispatch_id"], "created_at": ev["recorded_at"]})
            if t in ("agent_invocation_started", "agent_replied"):
                rec.pop("awaiting_pickup", None)  # a later attempt supersedes an earlier failure
                rec.pop("error", None)
            rec.update({k: v for k, v in p.items() if k != "dispatch_id"})
            rec["status"] = {"thread_mention": "queued", "agent_invocation_started": "running",
                             "agent_invocation_failed": "failed", "agent_replied": "replied"}[t]
            if t == "agent_invocation_failed" and p.get("awaiting_pickup"):
                rec["status"] = "queued"  # runner failed; still claimable by a session
            self._touch(rec, ev)
        # agent_blocked and anything unknown stay in the log only; they surface in Activity.

    # ---- reading -------------------------------------------------------------------------
    def task_for_entity(self, project_id: str | None, entity_key: str) -> dict | None:
        tid = self.state.entity_index.get(f"{project_id}|{entity_key}")
        return self.state.tasks.get(tid) if tid else None
