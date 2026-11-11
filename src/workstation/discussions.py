"""Discussions: agents talk to each other on the board, and a discussion ends with a recorded outcome.

A discussion is standalone or attached to a task. Any registered agent can open one, read it and post to it.
Closing records one of three outcomes: `agreed`, `overruled` (by a higher level) or `sent_to_user` (which puts an
item in the owner's inbox). The server only records; it never calls an agent.

Defaults (this project's own, not researched): title 200 characters, a post 2000, a closing note 500,
200 posts per discussion.
"""
from __future__ import annotations

import functools
from typing import Any, Callable

from .principals import AGENTS, can, is_human
from .service import Conflict, Forbidden, Invalid, NotFound, Service
from .store import new_id

OUTCOMES = ("agreed", "overruled", "sent_to_user")
STANCES = ("agree", "disagree", "none")
LIMITS = {"title": 200, "text": 2000, "note": 500}
MAX_POSTS = 200


def _text(value: Any, what: str, limit: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise Invalid(f"{what} must be text")
    if len(value) > limit:
        raise Invalid(f"{what} is longer than {limit} characters")
    if required and not value.strip():
        raise Invalid(f"{what} is required")
    return value


def participants(d: dict) -> list[str]:
    """The agents that have taken part: the opener, then everyone who posted, in order. The owner is not counted."""
    out: list[str] = []
    for who in [d["opened_by"], *[p["author"] for p in d["posts"]]]:
        if who in AGENTS and not is_human(who) and who not in out:
            out.append(who)
    return out


def stances(d: dict) -> dict[str, str]:
    """Each participant's latest stance; a participant who never took one is `none`."""
    latest = {pid: "none" for pid in participants(d)}
    for p in d["posts"]:
        if p["author"] in latest and p["stance"] != "none":
            latest[p["author"]] = p["stance"]
    return latest


def card(d: dict, full: bool = False) -> dict:
    out = {"id": d["id"], "title": d["title"], "status": d["status"], "task_id": d.get("task_id"),
           "project_id": d.get("project_id"), "opened_by": d["opened_by"], "participants": participants(d),
           "stances": stances(d), "post_count": len(d["posts"]), "created_at": d["created_at"],
           "updated_at": d["updated_at"], "outcome": d.get("outcome"), "note": d.get("note"),
           "closed_by": d.get("closed_by"), "closed_at": d.get("closed_at"), "ask_id": d.get("ask_id"),
           "version": d["version"]}
    if full:
        out["posts"] = list(d["posts"])
    return out


def atomic_method(fn):
    @functools.wraps(fn)
    def wrapper(self, *a, **k):
        with self.svc.store.batch():
            return fn(self, *a, **k)
    return wrapper


class Discussions:
    def __init__(self, svc: Service):
        self.svc = svc
        # Who may close a discussion as overruled; replaced by the levels rule. Until then only the owner may.
        self.may_overrule: Callable[[str, dict], bool] = lambda actor, d: is_human(actor)

    @property
    def s(self):
        return self.svc.s

    def get(self, discussion_id: str) -> dict:
        d = self.s.discussions.get(discussion_id)
        if not d:
            raise NotFound(f"discussion {discussion_id}")
        return d

    def _need(self, actor: str) -> None:
        if not can(actor, "discuss"):
            raise Forbidden(f"{actor} may not take part in discussions")

    def list(self, status: str | None = None) -> list[dict]:
        rows = [d for d in self.s.discussions.values() if not status or status == "all" or d["status"] == status]
        return [card(d) for d in sorted(rows, key=lambda d: d["updated_at"], reverse=True)]

    @atomic_method
    def open(self, actor: str, *, title: str, text: str = "", stance: str = "none", task_id: str | None = None,
             project_id: str | None = None, idempotency_key: str | None = None) -> tuple[dict, str]:
        svc = self.svc
        fp = svc._fp(op="discussion", actor=actor, title=title, text=text, stance=stance, task_id=task_id,
                     project_id=project_id)
        if (dup := svc._dup(idempotency_key, "discussion_opened", fp=fp)):
            return self.get(dup["payload"]["id"]), "duplicate"
        self._need(actor)
        title = _text(title, "title", LIMITS["title"]).strip()
        _text(text, "text", LIMITS["text"], required=False)
        if stance not in STANCES:
            raise Invalid(f"stance must be one of {list(STANCES)}")
        if task_id and task_id not in self.s.tasks:
            raise Invalid(f"unknown task {task_id}")
        if task_id and not project_id:
            project_id = self.s.tasks[task_id]["project_id"]
        svc._need_project(project_id)
        did = new_id("disc")
        svc.store.append("discussion_opened", actor, {"id": did, "title": title, "task_id": task_id,
                                                     "project_id": project_id},
                         idempotency_key=idempotency_key, project_id=project_id, request_hash=fp)
        if text.strip():
            self._post(actor, did, text, stance)
        return self.s.discussions[did], "created"

    def _post(self, actor: str, did: str, text: str, stance: str, key: str | None = None, fp: str | None = None):
        self.svc.store.append("discussion_posted", actor, {"id": new_id("dpost"), "discussion_id": did, "text": text,
                                                          "stance": stance}, idempotency_key=key, request_hash=fp)

    @atomic_method
    def post(self, actor: str, discussion_id: str, text: str, *, stance: str = "none",
             idempotency_key: str | None = None) -> tuple[dict, str]:
        svc = self.svc
        fp = svc._fp(op="dpost", actor=actor, discussion_id=discussion_id, text=text, stance=stance)
        if (dup := svc._dup(idempotency_key, "discussion_posted", fp=fp, discussion_id=discussion_id)):
            return self.get(dup["payload"]["discussion_id"]), "duplicate"
        self._need(actor)
        d = self.get(discussion_id)
        _text(text, "text", LIMITS["text"])
        if stance not in STANCES:
            raise Invalid(f"stance must be one of {list(STANCES)}")
        if d["status"] != "open":
            raise Conflict(f"discussion already closed ({d['outcome']})")
        if len(d["posts"]) >= MAX_POSTS:
            raise Conflict(f"a discussion holds at most {MAX_POSTS} posts; close it or open a new one")
        self._post(actor, discussion_id, text, stance, idempotency_key, fp)
        return d, "posted"

    @atomic_method
    def close(self, actor: str, discussion_id: str, *, outcome: str, note: str = "") -> dict:
        self._need(actor)
        d = self.get(discussion_id)
        if outcome not in OUTCOMES:
            raise Invalid(f"outcome must be one of {list(OUTCOMES)}")
        _text(note, "note", LIMITS["note"], required=False)
        if d["status"] != "open":
            raise Conflict(f"discussion already closed ({d['outcome']})")
        if not is_human(actor) and actor not in participants(d):
            raise Forbidden("only an agent that took part in a discussion can close it")
        ask_id = None
        if outcome == "agreed":
            st = stances(d)
            if len(st) < 2 or any(v != "agree" for v in st.values()):
                raise Conflict("agreed needs at least two agents, each with agree as their latest stance")
        elif outcome == "overruled":
            if not self.may_overrule(actor, d):
                raise Forbidden(f"{actor} cannot overrule this discussion")
        else:  # sent_to_user
            if is_human(actor):
                raise Invalid("the owner is already the one it would be sent to")
            ask, _ = self.svc.create_ask(actor, title=f"Unsettled: {d['title']}"[:LIMITS["title"]], kind="question",
                                         text=_summary(d, note), blocked="the discussion", urgency="normal",
                                         project_id=d.get("project_id"), task_id=d.get("task_id"),
                                         discussion_id=d["id"], url=f"#/discussions/{d['id']}")
            ask_id = ask["id"]
        self.svc.store.append("discussion_closed", actor, {"id": d["id"], "outcome": outcome, "note": note,
                                                          "ask_id": ask_id}, project_id=d.get("project_id"))
        return d


def _summary(d: dict, note: str) -> str:
    st = stances(d)
    lines = [f"{AGENTS[p]['display']}: {s}" for p, s in st.items()]
    out = "They did not settle this. Positions: " + "; ".join(lines) + "."
    if note.strip():
        out += f" Note from the closing agent: {note.strip()}"
    return out[:2000]

