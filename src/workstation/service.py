"""Commands. Each validates identity, permission and state, then records events in the Store.

Nothing here talks to a model or the network; agent invocation lives in `runners.py`.
"""
from __future__ import annotations

import functools
import hashlib
import json
import re
import subprocess
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .levels import Levels
from .principals import AGENTS, by_handle, can, is_human
from .principals import owner as owner_id
from . import store as _store
from .store import DECISION_STATES, TASK_STATES, TASK_TRANSITIONS, Store, new_id

MENTION_RE = re.compile(r"(?<![\w@./])@([A-Za-z][\w-]*)")
DEFAULT_BUDGET = {"max_agent_turns": 8, "critique_rounds": 1}
ARTIFACT_KINDS = {"commit", "pr", "doc", "review", "test_run", "other"}
REVIEW_VERDICTS = {"approve", "changes_requested", "comment"}
PRIORITIES = {"P0", "P1", "P2", "P3"}
TEXT_FIELDS = {"title", "next_action", "blocked_reason", "note", "why"}

# Asks: items an agent submits because it needs the owner. The kinds, urgency levels, length limits and the
# open-item cap are this project's defaults, not research results; the cap is configurable ([inbox]).
ASK_KINDS = ("decision", "review", "approval", "question")
ASK_URGENCY = ("low", "normal", "high")
ASK_LIMITS = {"title": 200, "text": 2000, "blocked": 300, "cost_of_waiting": 300, "note": 2000}
DEFAULT_MAX_OPEN_ASKS = 20
ASK_RESOLUTIONS = {"approval": ("approved", "declined", "dismissed")}
ASK_RESOLUTIONS_OTHER = ("answered", "dismissed")


OPTION_STOP = {"a", "an", "the", "of", "to", "for", "with", "and", "or", "in", "on", "as", "by", "it", "we", "use", "using"}
OPTION_MERGE_AT = 0.6


def option_tokens(label: str) -> frozenset:
    """Content words of an option label: lowercase, punctuation and filler words dropped, plural 's' stripped."""
    words = re.findall(r"[a-z0-9]+", label.lower())
    return frozenset(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words if w not in OPTION_STOP)


def same_option(a: frozenset, b: frozenset) -> bool:
    """Two labels are one option when their content words overlap by at least OPTION_MERGE_AT (Jaccard)."""
    return bool(a or b) and len(a & b) / len(a | b) >= OPTION_MERGE_AT

class Forbidden(Exception): ...
class Invalid(Exception): ...
class Conflict(Exception): ...
class NotFound(Exception): ...


def parse_due(due_at: str | None, due_tz: str | None) -> tuple[str | None, str | None]:
    """Deadlines keep their zone. A naive time needs an explicit IANA zone; nothing is guessed."""
    if not due_at:
        return None, due_tz
    try:
        dt = datetime.fromisoformat(due_at)
    except ValueError as e:
        raise Invalid(f"due_at is not ISO 8601: {due_at}") from e
    if dt.tzinfo is None:
        if not due_tz:
            raise Invalid("due_at has no UTC offset; send an offset or due_tz (IANA, e.g. America/New_York)")
        try:
            dt = dt.replace(tzinfo=ZoneInfo(due_tz))
        except (ValueError, KeyError) as e:  # ZoneInfoNotFoundError is a KeyError
            raise Invalid(f"unknown time zone {due_tz}") from e
    return dt.isoformat(), due_tz


def check_url(url: Any, field: str = "url") -> str | None:
    """Links shown to the owner must be web links or in-app anchors; anything else could run script."""
    if url is None or url == "":
        return None
    if not isinstance(url, str) or not re.match(r"^(https?://|#/)", url):
        raise Invalid(f"{field} must start with http://, https:// or #/")
    return url


def check_fields(fields: dict[str, Any]) -> None:
    if "title" in fields and not (isinstance(fields["title"], str) and fields["title"].strip()):
        raise Invalid("title must be non-empty text")
    for k, v in fields.items():
        if k in TEXT_FIELDS and v is not None and not isinstance(v, str):
            raise Invalid(f"{k} must be text")
        if k == "priority" and (not isinstance(v, str) or v not in PRIORITIES):
            raise Invalid(f"priority must be one of {sorted(PRIORITIES)}")
        if k == "status" and (not isinstance(v, str) or v not in TASK_STATES):
            raise Invalid(f"status must be one of {sorted(TASK_STATES)}")
        if k in ("due_at", "due_tz") and v is not None and not isinstance(v, str):
            raise Invalid(f"{k} must be text")


def mentions_in(text: str) -> list[str]:
    out = []
    for h in MENTION_RE.findall(text or ""):
        pid = by_handle(h)
        if pid and pid not in out:
            out.append(pid)
    return out


def atomic(fn):
    """Run a command's events in one transaction."""
    @functools.wraps(fn)
    def wrapper(self, *a, **k):
        with self.store.batch():
            return fn(self, *a, **k)
    return wrapper


class Service:
    def __init__(self, store: Store, on_dispatch: Callable[[str], None] | None = None):
        self.store = store
        self.on_dispatch = on_dispatch or (lambda _id: None)
        self.max_open_asks = DEFAULT_MAX_OPEN_ASKS
        self.levels = Levels()  # replaced from config at start
        self._refused_at: dict[tuple[str, str], datetime] = {}  # in memory: stops a loop, not worth persisting

    @property
    def s(self):
        return self.store.state

    # ---- helpers -------------------------------------------------------------------------
    @staticmethod
    def _fp(**request: Any) -> str:
        """Fingerprint of a request's content, stored with the event so a key reused with a different body is refused."""
        return hashlib.sha256(json.dumps(request, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()

    def _dup(self, key: str | None, *event_types: str, fp: str | None = None, **same: Any) -> dict | None:
        """The earlier event for this key, if any. A key reused for another kind of request, another
        resource or a different body is refused rather than answered with an unrelated result."""
        if not (key and key in self.s.idempotency):
            return None
        eid = self.s.idempotency[key]
        ev = next(e for e in self.s.events if e["event_id"] == eid)
        if (event_types and ev["event_type"] not in event_types) or \
                any(ev["payload"].get(k) != v for k, v in same.items()):
            raise Conflict(f"idempotency key {key} was already used for a different request")
        if fp and ev.get("request_hash") and ev["request_hash"] != fp:
            raise Conflict(f"idempotency key {key} was already used with different content")
        return ev

    def _task(self, task_id: str) -> dict:
        t = self.s.tasks.get(task_id)
        if not t:
            raise NotFound(f"task {task_id}")
        return t

    def _thread(self, thread_id: str) -> dict:
        t = self.s.threads.get(thread_id)
        if not t:
            raise NotFound(f"thread {thread_id}")
        return t

    def _need_project(self, project_id: str | None) -> None:
        if project_id and project_id not in self.s.projects:
            raise Invalid(f"unknown project {project_id}")

    def notice(self, thread_id: str, text: str, key: str | None = None) -> None:
        self.store.append("message_posted", "system", {"id": new_id("msg"), "thread_id": thread_id, "text": text,
                                                       "kind": "notice"}, idempotency_key=key)

    # ---- projects ------------------------------------------------------------------------
    def upsert_project(self, actor: str, id: str, name: str, goal: str = "", stage: str = "", milestone: str = "",
                       idempotency_key: str | None = None) -> dict:
        if not is_human(actor):
            raise Forbidden("only the owner edits projects")
        self.store.append("project_upserted", actor, {"id": id, "name": name, "goal": goal, "stage": stage,
                                                      "milestone": milestone}, project_id=id,
                          idempotency_key=idempotency_key)
        return self.s.projects[id]

    @atomic
    def record_refusal(self, actor: str, action: str, reason: str) -> bool:
        """Log that an agent token tried something reserved for the owner (shown in the weekly review).
        The same refusal by the same agent repeated within a minute is logged once (tracked in memory per agent and
        action, so interleaved events and other agents do not defeat it), so a looping agent cannot bloat the log."""
        if is_human(actor) or actor not in AGENTS:
            return False
        now = datetime.fromisoformat(_store.now_iso())
        seen = self._refused_at.get((actor, action))
        if seen and (now - seen).total_seconds() < 60:
            return False
        self._refused_at[(actor, action)] = now
        self.store.append("action_refused", actor, {"action": action, "reason": reason[:200]})
        return True

    # ---- levels: who may assign to and overrule whom ---------------------------------------
    def check_can_assign(self, actor: str, assignee: str) -> None:
        """An agent assigns work only to an agent at its own level or lower (peers may assign to each other)."""
        if assignee == actor or AGENTS[assignee]["kind"] != "agent":
            return
        if self.levels.of(actor) < self.levels.of(assignee):
            raise Forbidden(f"{actor} (level {self.levels.of(actor)}) cannot assign work upward to "
                            f"{assignee} (level {self.levels.of(assignee)}); ask in a discussion or ask the owner")

    # ---- tasks ---------------------------------------------------------------------------
    @atomic
    def create_task(self, actor: str, *, title: str, project_id: str, owner: str | None = None, status: str = "inbox",
                    priority: str = "P2", next_action: str = "", due_at: str | None = None, due_tz: str | None = None,
                    evidence_url: str | None = None, thread_id: str | None = None, decision_id: str | None = None,
                    why: str = "", entity_key: str | None = None, idempotency_key: str | None = None,
                    source_system: str = "workstation", provenance: dict | None = None,
                    occurred_at: str | None = None) -> tuple[dict, str]:
        """Returns (task, outcome) with outcome created | duplicate | merged."""
        fp = self._fp(op="task", actor=actor, project_id=project_id, title=title, owner=owner, status=status,
                      priority=priority, next_action=next_action, due_at=due_at, due_tz=due_tz,
                      evidence_url=evidence_url, thread_id=thread_id, decision_id=decision_id, why=why,
                      entity_key=entity_key)
        if (dup := self._dup(idempotency_key, "task_created", "task_updated", fp=fp)):
            return self._task(dup["payload"]["id"]), "duplicate"
        check_fields({"title": title, "next_action": next_action, "why": why, "priority": priority,
                      "due_at": due_at, "due_tz": due_tz})
        evidence_url = check_url(evidence_url, "evidence_url")
        if not title.strip():
            raise Invalid("title is required")
        self._need_project(project_id)
        owner = owner or owner_id()
        if owner not in AGENTS or AGENTS[owner]["kind"] == "system":
            raise Invalid(f"unknown owner {owner}")
        if status not in TASK_STATES or status in ("done", "canceled"):
            raise Invalid(f"a new task cannot start as {status}")
        suggested = False
        if not is_human(actor) and status not in ("inbox", "ready", "in_progress"):
            raise Forbidden(f"agents create tasks as inbox, ready or in_progress, not {status}")
        if not is_human(actor):
            if can(actor, "task_create", project_id):
                pass
            elif can(actor, "task_create_for_human", project_id):
                if owner != owner_id():
                    raise Forbidden(f"{actor} may only create tasks owned by the owner")
            elif can(actor, "task_suggest", project_id):
                owner, status, suggested = owner_id(), "inbox", True
            else:
                raise Forbidden(f"{actor} may not create tasks in {project_id}")
        if not is_human(actor):
            self.check_can_assign(actor, owner)
        due_at, due_tz = parse_due(due_at, due_tz)

        if entity_key and (existing := self.store.task_for_entity(project_id, entity_key)) \
                and existing["status"] not in ("done", "canceled"):
            if not is_human(actor) and actor not in (existing["created_by"], existing["owner"]):
                raise Forbidden(f"{entity_key} is an open task {actor} neither filed nor owns; ask the owner")
            changes = {k: v for k, v in {"title": title, "next_action": next_action, "due_at": due_at,
                                         "due_tz": due_tz, "evidence_url": evidence_url}.items()
                       if v and existing.get(k) != v}
            self.store.append("task_updated", actor, {"id": existing["id"], **changes},
                              idempotency_key=idempotency_key, source_system=source_system, project_id=project_id,
                              entity_key=entity_key, provenance=provenance, occurred_at=occurred_at, request_hash=fp)
            return existing, "merged"

        tid = new_id("task")
        self.store.append("task_created", actor, {
            "id": tid, "title": title.strip(), "project_id": project_id, "owner": owner, "status": status,
            "priority": priority, "next_action": next_action, "due_at": due_at, "due_tz": due_tz,
            "evidence_url": evidence_url, "thread_id": thread_id, "decision_id": decision_id,
            "why": why, "suggested": suggested, "entity_key": entity_key,
        }, idempotency_key=idempotency_key, source_system=source_system, project_id=project_id,
            entity_key=entity_key, provenance=provenance, occurred_at=occurred_at, request_hash=fp)
        return self.s.tasks[tid], "created"

    @atomic
    def update_task(self, actor: str, task_id: str, *, expected_version: int | None = None,
                    idempotency_key: str | None = None, **changes: Any) -> dict:
        fp = self._fp(op="update", actor=actor, task_id=task_id, expected_version=expected_version, changes=changes)
        if self._dup(idempotency_key, "task_updated", "task_completed", fp=fp, id=task_id):
            return self._task(task_id)
        t = self._task(task_id)
        allowed = {"status", "title", "next_action", "due_at", "due_tz", "priority", "owner", "blocked_reason", "note"}
        if bad := set(changes) - allowed:
            raise Invalid(f"cannot change {sorted(bad)}")
        check_fields(changes)
        if expected_version is not None and expected_version != t["version"]:
            raise Conflict(f"task {task_id} is at version {t['version']}, not {expected_version}")
        human = is_human(actor)
        if not human:
            if not (can(actor, "task_update_own", t["project_id"]) and t["owner"] == actor):
                raise Forbidden(f"{actor} may only update its own tasks")
            if "owner" in changes:
                raise Forbidden("only the owner reassigns tasks")
        payload: dict[str, Any] = {"id": task_id}
        if "due_at" in changes or "due_tz" in changes:
            payload["due_at"], payload["due_tz"] = parse_due(changes.pop("due_at", t.get("due_at")),
                                                             changes.pop("due_tz", t.get("due_tz")))
        new_status = changes.get("status")
        if new_status and new_status != t["status"]:
            if new_status not in TASK_STATES:
                raise Invalid(f"unknown status {new_status}")
            if not human and new_status in ("done", "canceled"):
                raise Forbidden("agents report needs_review with evidence; only the owner accepts or cancels")
            if not human and t["status"] in ("done", "canceled"):
                raise Forbidden("only the owner reopens a closed task")
            if not human and new_status == "needs_review" and not t["artifacts"]:
                raise Invalid("needs_review requires at least one linked artifact")
            if new_status not in TASK_TRANSITIONS[t["status"]]:
                if not human:
                    raise Invalid(f"{t['status']} → {new_status} is not an allowed transition")
                payload["override"] = True
            payload["from_status"] = t["status"]
        if changes.get("owner") and (changes["owner"] not in AGENTS or AGENTS[changes["owner"]]["kind"] == "system"):
            raise Invalid(f"unknown owner {changes['owner']}")
        payload.update(changes)
        etype = "task_completed" if new_status == "done" else "task_updated"
        self.store.append(etype, actor, payload, idempotency_key=idempotency_key, project_id=t["project_id"],
                          request_hash=fp)
        return t

    @atomic
    def link_artifact(self, actor: str, task_id: str, *, kind: str, title: str, url: str | None = None,
                      ref: str | None = None, repo_path: str | None = None,
                      idempotency_key: str | None = None) -> dict:
        fp = self._fp(op="artifact", actor=actor, task_id=task_id, kind=kind, title=title, url=url, ref=ref,
                      repo_path=repo_path)
        if self._dup(idempotency_key, "task_updated", fp=fp, id=task_id):
            return self._task(task_id)
        t = self._task(task_id)
        if kind not in ARTIFACT_KINDS:
            raise Invalid(f"kind must be one of {sorted(ARTIFACT_KINDS)}")
        if not (url or ref):
            raise Invalid("an artifact needs a url or a ref")
        url = check_url(url)
        if not is_human(actor) and not (can(actor, "artifact_link", t["project_id"]) and t["owner"] == actor):
            raise Forbidden(f"{actor} may only link artifacts to its own tasks")
        verification = {"state": "reported", "by": actor}
        if kind == "commit" and ref and repo_path:
            ok = subprocess.run(["git", "-C", repo_path, "cat-file", "-e", f"{ref}^{{commit}}"],
                                capture_output=True).returncode == 0
            verification = {"state": "verified" if ok else "not_found", "by": "git cat-file", "repo": repo_path}
        art = {"id": new_id("art"), "kind": kind, "title": title, "url": url, "ref": ref, "by": actor,
               "verification": verification}
        self.store.append("task_updated", actor, {"id": task_id, "artifact": art}, idempotency_key=idempotency_key,
                          project_id=t["project_id"], request_hash=fp)
        return t

    @atomic
    def record_review(self, actor: str, task_id: str, *, verdict: str, summary: str, message_id: str | None = None,
                      idempotency_key: str | None = None) -> dict:
        fp = self._fp(op="review", actor=actor, task_id=task_id, verdict=verdict, summary=summary,
                      message_id=message_id)
        if self._dup(idempotency_key, "task_updated", fp=fp, id=task_id):
            return self._task(task_id)
        t = self._task(task_id)
        if verdict not in REVIEW_VERDICTS:
            raise Invalid(f"verdict must be one of {sorted(REVIEW_VERDICTS)}")
        if not (is_human(actor) or can(actor, "review_record", t["project_id"])):
            raise Forbidden(f"{actor} may not record reviews")
        review = {"by": actor, "verdict": verdict, "summary": summary, "message_id": message_id,
                  "artifact_count": len(t["artifacts"])}
        self.store.append("task_updated", actor, {"id": task_id, "review": review}, idempotency_key=idempotency_key,
                          project_id=t["project_id"], request_hash=fp)
        return t

    # ---- asks (agent-submitted items for the owner) ----------------------------------------
    def _ask(self, ask_id: str) -> dict:
        a = self.s.asks.get(ask_id)
        if not a:
            raise NotFound(f"ask {ask_id}")
        return a

    @atomic
    def create_ask(self, actor: str, *, title: str, kind: str = "question", text: str = "", blocked: str = "",
                   cost_of_waiting: str = "", urgency: str = "normal", project_id: str | None = None,
                   task_id: str | None = None, due_at: str | None = None, due_tz: str | None = None,
                   url: str | None = None, discussion_id: str | None = None,
                   idempotency_key: str | None = None) -> tuple[dict, str]:
        """An agent says "this needs you". Returns (ask, outcome) with outcome created | duplicate."""
        fp = self._fp(op="ask", actor=actor, title=title, kind=kind, text=text, blocked=blocked,
                      cost_of_waiting=cost_of_waiting, urgency=urgency, project_id=project_id, task_id=task_id,
                      due_at=due_at, due_tz=due_tz, url=url, discussion_id=discussion_id)
        if (dup := self._dup(idempotency_key, "ask_created", fp=fp)):
            return self._ask(dup["payload"]["id"]), "duplicate"
        if not can(actor, "ask_owner", project_id):
            raise Forbidden(f"{actor} may not submit items for the owner")
        for k, v in (("title", title), ("text", text), ("blocked", blocked), ("cost_of_waiting", cost_of_waiting)):
            if not isinstance(v, str):
                raise Invalid(f"{k} must be text")
            if len(v) > ASK_LIMITS[k]:
                raise Invalid(f"{k} is longer than {ASK_LIMITS[k]} characters")
        if not title.strip():
            raise Invalid("title is required")
        if kind not in ASK_KINDS:
            raise Invalid(f"kind must be one of {list(ASK_KINDS)}")
        if urgency not in ASK_URGENCY:
            raise Invalid(f"urgency must be one of {list(ASK_URGENCY)}")
        self._need_project(project_id)
        if task_id and task_id not in self.s.tasks:
            raise Invalid(f"unknown task {task_id}")
        if discussion_id and discussion_id not in self.s.discussions:
            raise Invalid(f"unknown discussion {discussion_id}")
        url = check_url(url)
        due_at, due_tz = parse_due(due_at, due_tz)
        open_now = sum(1 for a in self.s.asks.values() if a["asked_by"] == actor and a["status"] == "open")
        if open_now >= self.max_open_asks:
            raise Conflict(f"{actor} already has {open_now} open items for the owner (limit {self.max_open_asks}); "
                           "wait for answers or withdraw some")
        aid = new_id("ask")
        self.store.append("ask_created", actor, {
            "id": aid, "title": title.strip(), "kind": kind, "text": text, "blocked": blocked,
            "cost_of_waiting": cost_of_waiting, "urgency": urgency, "project_id": project_id, "task_id": task_id,
            "due_at": due_at, "due_tz": due_tz, "url": url, "discussion_id": discussion_id},
            idempotency_key=idempotency_key, project_id=project_id, request_hash=fp)
        return self.s.asks[aid], "created"

    @atomic
    def answer_ask(self, actor: str, ask_id: str, *, resolution: str, note: str = "") -> dict:
        if not is_human(actor):
            raise Forbidden("only the owner answers items in the inbox")
        a = self._ask(ask_id)
        if a["status"] != "open":
            raise Conflict(f"ask already {a['status']}")
        allowed = ASK_RESOLUTIONS.get(a["kind"], ASK_RESOLUTIONS_OTHER)
        if resolution not in allowed:
            raise Invalid(f"a {a['kind']} is closed with one of {list(allowed)}")
        if not isinstance(note, str) or len(note) > ASK_LIMITS["note"]:
            raise Invalid(f"note must be text of at most {ASK_LIMITS['note']} characters")
        if resolution == "answered" and not note.strip():
            raise Invalid("an answer needs text")
        self.store.append("ask_answered", actor, {"id": ask_id, "resolution": resolution, "note": note},
                          project_id=a.get("project_id"))
        return a

    @atomic
    def withdraw_ask(self, actor: str, ask_id: str) -> dict:
        a = self._ask(ask_id)
        if actor != a["asked_by"]:
            raise Forbidden("only the agent that submitted an item can withdraw it")
        if a["status"] != "open":
            raise Conflict(f"ask already {a['status']}")
        self.store.append("ask_withdrawn", actor, {"id": ask_id, "resolution": "withdrawn"},
                          project_id=a.get("project_id"))
        return a

    # ---- decisions -----------------------------------------------------------------------
    @atomic
    def create_decision(self, actor: str, *, question: str, options: list[dict], project_id: str,
                        recommendations: list[dict] | None = None, evidence: list[dict] | None = None,
                        context: str = "", urgency: str = "normal", due_at: str | None = None,
                        due_tz: str | None = None, thread_id: str | None = None, follow_up_owner: str = "claude",
                        idempotency_key: str | None = None) -> dict:
        fp = self._fp(op="decision", actor=actor, question=question, options=options, project_id=project_id,
                      recommendations=recommendations, evidence=evidence, context=context, urgency=urgency,
                      due_at=due_at, due_tz=due_tz, thread_id=thread_id, follow_up_owner=follow_up_owner)
        if (dup := self._dup(idempotency_key, "decision_required", fp=fp)):
            return self.s.decisions[dup["payload"]["id"]]
        if not (is_human(actor) or can(actor, "decision_propose", project_id)):
            raise Forbidden(f"{actor} may not propose decisions")
        self._need_project(project_id)
        opts = [o for o in options if (o.get("label") or "").strip()]
        if len(opts) < 2:
            raise Invalid("a decision needs at least two options")
        if follow_up_owner not in AGENTS or AGENTS[follow_up_owner]["kind"] == "system":
            raise Invalid(f"unknown follow-up owner {follow_up_owner}")
        for e in evidence or []:
            check_url(e.get("url") if isinstance(e, dict) else e, "evidence url")
        due_at, due_tz = parse_due(due_at, due_tz)
        did = new_id("dec")
        payload = {
            "id": did, "question": question.strip(), "context": context, "project_id": project_id,
            "options": [{"id": f"o{i + 1}", "label": o["label"].strip(), "detail": o.get("detail", "")}
                        for i, o in enumerate(opts)],
            "recommendations": recommendations or [], "evidence": evidence or [], "urgency": urgency,
            "due_at": due_at, "due_tz": due_tz, "thread_id": thread_id, "follow_up_owner": follow_up_owner,
        }
        self.store.append("decision_required", actor, payload, idempotency_key=idempotency_key, project_id=project_id,
                          request_hash=fp)
        if thread_id and thread_id in self.s.threads:
            self.notice(thread_id, f"Decision card created: “{payload['question']}”. Only the owner can resolve it.")
        return self.s.decisions[did]

    @atomic
    def resolve_decision(self, actor: str, decision_id: str, *, status: str, option_id: str | None = None,
                         note: str = "", revisit: str = "") -> dict:
        if not is_human(actor):
            raise Forbidden("only the owner resolves decisions")
        d = self.s.decisions.get(decision_id)
        if not d:
            raise NotFound(f"decision {decision_id}")
        if d["status"] != "pending":
            raise Conflict(f"decision already {d['status']}")
        if status not in DECISION_STATES or status == "pending":
            raise Invalid(f"status must be one of {DECISION_STATES[1:]}")
        option = next((o for o in d["options"] if o["id"] == option_id), None)
        if status == "approved" and not option:
            raise Invalid("approving needs an option_id")
        self.store.append("decision_resolved", actor, {"id": decision_id, "status": status, "option_id": option_id,
                                                       "note": note, "revisit": revisit}, project_id=d["project_id"])
        if status == "approved":
            task, _ = self.create_task(
                actor, title=f"Implement: {option['label']}", project_id=d["project_id"], owner=d["follow_up_owner"],
                status="ready", thread_id=d.get("thread_id"), decision_id=decision_id,
                why=f"Approved by {AGENTS[actor]['display']} in decision “{d['question']}”" + (f" — {note}" if note else ""),
                next_action="Plan, implement, link commit/PR and test evidence, then set needs_review",
                idempotency_key=f"decision:{decision_id}:follow-up")
            self.store.append("decision_resolved", actor, {"id": decision_id, "resulting_task": task["id"]},
                              project_id=d["project_id"], idempotency_key=f"decision:{decision_id}:link")
            if d.get("thread_id") in self.s.threads:
                self.notice(d["thread_id"], f"Approved “{option['label']}”. Task {task['id']} assigned to "
                                            f"{AGENTS[d['follow_up_owner']]['display']}.")
        return d

    # ---- threads -------------------------------------------------------------------------
    @atomic
    def create_thread(self, actor: str, *, title: str, project_id: str, task_id: str | None = None,
                      budget: dict | None = None, idempotency_key: str | None = None) -> dict:
        fp = self._fp(op="thread", actor=actor, title=title, project_id=project_id, task_id=task_id, budget=budget)
        if (dup := self._dup(idempotency_key, "thread_created", fp=fp)):
            return self._thread(dup["payload"]["id"])
        if not is_human(actor):
            raise Forbidden("only the owner opens threads")
        self._need_project(project_id)
        tid = new_id("thr")
        self.store.append("thread_created", actor, {
            "id": tid, "title": title.strip(), "project_id": project_id, "task_id": task_id,
            "participants": [actor], "budget": {**DEFAULT_BUDGET, **(budget or {})}, "critique_rounds_used": 0,
        }, idempotency_key=idempotency_key, project_id=project_id, request_hash=fp)
        return self.s.threads[tid]

    def _dispatches_in(self, thread_id: str) -> list[dict]:
        return [d for d in self.s.dispatches.values() if d.get("thread_id") == thread_id]

    def _dispatch(self, actor: str, thread: dict, agent: str, *, mode: str, until_seq: int, trigger_seq: int,
                  target_seq: int | None = None) -> str | None:
        if len(self._dispatches_in(thread["id"])) >= thread["budget"]["max_agent_turns"]:
            self.notice(thread["id"], f"Not sent to {AGENTS[agent]['display']}: this thread used its "
                                      f"{thread['budget']['max_agent_turns']} agent turns. Raise the budget to continue.")
            return None
        if not can(agent, "reply", thread["project_id"]):
            self.notice(thread["id"], f"{AGENTS[agent]['display']} is not a thread participant role "
                                      f"({AGENTS[agent]['role']}); not invoked.")
            return None
        if agent not in thread["participants"]:
            self.store.append("thread_updated", actor, {"id": thread["id"],
                                                        "participants": thread["participants"] + [agent]})
        did = new_id("dsp")
        self.store.append("thread_mention", actor, {
            "dispatch_id": did, "thread_id": thread["id"], "agent": agent, "mode": mode, "requested_by": actor,
            "trigger_seq": trigger_seq, "until_seq": until_seq, "target_seq": target_seq, "attempts": 0,
        }, project_id=thread["project_id"], idempotency_key=f"mention:{thread['id']}:{trigger_seq}:{agent}:{mode}")
        self.store.after_commit(lambda: self.on_dispatch(did))
        return did

    @atomic
    def post_message(self, actor: str, thread_id: str, text: str, *, idempotency_key: str | None = None,
                     dispatch_id: str | None = None, structured: dict | None = None,
                     runtime: dict | None = None) -> dict:
        fp = self._fp(op="message", actor=actor, thread_id=thread_id, text=text, dispatch_id=dispatch_id,
                      structured=structured or {})
        if (dup := self._dup(idempotency_key, "message_posted", fp=fp, thread_id=thread_id)):
            return next(m for m in self.s.messages[thread_id] if m["event_id"] == dup["event_id"])
        th = self._thread(thread_id)
        if not text.strip():
            raise Invalid("empty message")
        human = is_human(actor)
        if not human and not can(actor, "reply", th["project_id"]):
            raise Forbidden(f"{actor} may not post in threads")
        if not human and actor not in th["participants"]:
            raise Forbidden(f"{actor} is not a participant of this thread")
        mentioned = mentions_in(text)
        agents_mentioned = [m for m in mentioned if AGENTS[m]["kind"] == "agent"]
        payload = {"id": new_id("msg"), "thread_id": thread_id, "text": text, "kind": "message",
                   "mentions": mentioned, "dispatch_id": dispatch_id, "structured": structured or {},
                   "runtime": runtime}
        if not human:
            # Agent-written mentions are suggestions for the owner, never auto-dispatched.
            payload["suggested_mentions"] = [m for m in agents_mentioned if m != actor]
        self.store.append("message_posted", actor, payload, idempotency_key=idempotency_key,
                          project_id=th["project_id"], request_hash=fp)
        msg = self.s.messages[thread_id][-1]
        if human:
            for agent in agents_mentioned:
                self._dispatch(actor, th, agent, mode="reply", until_seq=msg["seq"], trigger_seq=msg["seq"])
        return msg

    @atomic
    def cross_review(self, actor: str, thread_id: str) -> list[str]:
        """One bounded critique round: each agent reads the other's latest reply and critiques it."""
        if not is_human(actor):
            raise Forbidden("only the owner starts a critique round")
        th = self._thread(thread_id)
        if th["critique_rounds_used"] >= th["budget"]["critique_rounds"]:
            raise Conflict(f"critique budget used ({th['budget']['critique_rounds']} round)")
        latest = {}
        for m in self.s.messages[thread_id]:
            if AGENTS.get(m["author"], {}).get("kind") == "agent" and m.get("kind") == "message" \
                    and m.get("dispatch_id"):  # unsolicited agent posts are not positions
                latest[m["author"]] = m
        if len(latest) < 2:
            raise Invalid("a critique round needs replies from at least two agents")
        last_seq = self.s.messages[thread_id][-1]["seq"]
        self.store.append("thread_updated", actor, {"id": thread_id,
                                                    "critique_rounds_used": th["critique_rounds_used"] + 1})
        out = []
        for agent in latest:
            for other, m in latest.items():
                if other != agent:
                    if (d := self._dispatch(actor, th, agent, mode="critique", until_seq=last_seq,
                                            trigger_seq=last_seq, target_seq=m["seq"])):
                        out.append(d)
                    break
        return out

    def draft_decision(self, thread_id: str) -> dict:
        """Compose a decision card from the agents' structured replies. Not saved; the owner edits it."""
        th = self._thread(thread_id)
        msgs = self.s.messages[thread_id]
        human_q = next((m["text"] for m in reversed(msgs) if is_human(m["author"])), th["title"])
        options, recs = [], {}
        # Fixed agent order, not arrival order: replies come back concurrently, and the draft must not depend on who was faster.
        order = {a: i for i, a in enumerate(AGENTS)}
        for m in sorted(msgs, key=lambda m: (order.get(m["author"], len(order)), m["seq"])):
            st = m.get("structured") or {}
            if AGENTS.get(m["author"], {}).get("kind") != "agent" or not m.get("dispatch_id"):
                continue
            for o in st.get("options") or []:
                if not (isinstance(o, str) and o.strip()):
                    continue
                key = option_tokens(o)
                exact = " ".join(re.findall(r"[a-z0-9]+", o.lower()))
                # A label made only of filler words has no content words to compare; match it only when identical.
                hit = next((x for x in options if same_option(key, x["_key"]) or
                            (not (key and x["_key"]) and exact == x["_exact"])), None)
                if hit is None:
                    hit = {"label": o.strip(), "sources": [], "_key": key, "_exact": exact}
                    options.append(hit)
                if m["author"] not in {x["agent"] for x in hit["sources"]}:
                    hit["sources"].append({"agent": m["author"], "label": o.strip(), "message_id": m["id"]})
            if st.get("position"):
                recs[m["author"]] = {"agent": m["author"], "summary": st["position"], "message_id": m["id"],
                                     "mode": (self.s.dispatches.get(m.get("dispatch_id") or "", {}) or {}).get("mode")}
        for o in options:
            o.pop("_key")
            o.pop("_exact")
            names = [AGENTS[x["agent"]]["display"] for x in o["sources"]]
            o["detail"] = "Proposed by " + " and ".join(names)
            if len({x["label"].lower() for x in o["sources"]}) > 1:
                o["detail"] += "; worded " + "; ".join(f'"{x["label"]}" ({AGENTS[x["agent"]]["display"]})' for x in o["sources"])
        return {"question": re.sub(MENTION_RE, "", human_q).strip(), "options": options,
                "recommendations": list(recs.values()), "project_id": th["project_id"], "thread_id": thread_id,
                "evidence": [{"label": f"Thread: {th['title']}", "url": f"#/threads/{thread_id}"}]}

    # ---- dispatch lifecycle (called by runners) -------------------------------------------
    def dispatch_started(self, dispatch_id: str, runtime: dict, claimed_at: float | None = None) -> int:
        """Record a new attempt and return its number. Callers check eligibility in the same batch."""
        d = self.s.dispatches[dispatch_id]
        attempt = d.get("attempts", 0) + 1
        payload = {"dispatch_id": dispatch_id, "attempts": attempt, "runtime": runtime}
        if claimed_at is not None:
            payload["claimed_at"] = claimed_at
        self.store.append("agent_invocation_started", d["agent"], payload)
        return attempt

    def _current(self, dispatch_id: str, attempt: int | None) -> dict | None:
        """The dispatch if `attempt` may still finish it; None for a finished one or a stale attempt."""
        d = self.s.dispatches[dispatch_id]
        if d["status"] == "replied" or (attempt is not None and d.get("attempts") != attempt):
            return None
        return d

    @atomic
    def dispatch_failed(self, dispatch_id: str, reason: str, attempt: int | None = None,
                        awaiting_pickup: bool = False) -> None:
        if not (d := self._current(dispatch_id, attempt)):
            return  # a reply already landed, or a newer attempt owns this dispatch
        payload = {"dispatch_id": dispatch_id, "error": reason}
        if awaiting_pickup:
            payload["awaiting_pickup"] = True
        self.store.append("agent_invocation_failed", "system", payload)
        if awaiting_pickup:
            self.notice(d["thread_id"], f"{AGENTS[d['agent']]['display']}'s CLI failed ({reason}); "
                                        "waiting for a session to pick it up.", key=f"wait:{dispatch_id}")
        else:
            self.notice(d["thread_id"], f"{AGENTS[d['agent']]['display']} did not reply: {reason}",
                        key=f"notice:{dispatch_id}:{d.get('attempts', 0)}")

    @atomic
    def dispatch_replied(self, dispatch_id: str, text: str, structured: dict, runtime: dict,
                         context: dict, attempt: int | None = None) -> dict:
        d = self._current(dispatch_id, attempt)
        if not d or d["status"] != "running":
            raise Conflict("this attempt no longer owns the dispatch (finished, or claimed again)")
        msg = self.post_message(d["agent"], d["thread_id"], text, idempotency_key=f"reply:{dispatch_id}",
                                dispatch_id=dispatch_id, structured=structured, runtime=runtime)
        self.store.append("agent_replied", d["agent"], {"dispatch_id": dispatch_id, "message_id": msg["id"],
                                                         "context": context},
                          idempotency_key=f"replied:{dispatch_id}")
        th = self.s.threads[d["thread_id"]]
        verdict = (structured or {}).get("verdict")
        if th.get("task_id") and verdict in REVIEW_VERDICTS and can(d["agent"], "review_record", th["project_id"]):
            self.record_review(d["agent"], th["task_id"], verdict=verdict,
                               summary=(structured.get("position") or text)[:400], message_id=msg["id"],
                               idempotency_key=f"review:{dispatch_id}")
        return msg
