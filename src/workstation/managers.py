"""The manager view: the owner's conversations with the top-level agents, and lower-level work folded under them.

Managers are the agents at the highest level among registered agents (a level comes from the model, see levels.py).
Work done by any other agent is not listed on the page by default: it is counted and summarised in one line under the
manager that assigned it (the task's creator), or under "Other work" when no manager assigned it (the owner or a
lower-level agent did). The owner can still open any agent's work and talk to any agent directly.
"""
from __future__ import annotations

from . import inbox
from .levels import Levels
from .principals import AGENTS
from .service import Service

OPEN_STATES = ("ready", "in_progress", "blocked", "needs_review")
SHOWN_PER_AGENT = 5
CONVERSATIONS_PER_MANAGER = 5


def _agents(svc: Service, levels: Levels) -> list[dict]:
    return [{"id": pid, "display": a["display"], "model": a.get("model", ""), "level": levels.of(pid),
             "role": a["role"]} for pid, a in AGENTS.items() if a["kind"] == "agent"]


def _one_line(tasks: list[dict]) -> str:
    if not tasks:
        return "Nothing open"
    parts = []
    for status, label in (("in_progress", "working"), ("ready", "queued"), ("needs_review", "waiting for review"),
                          ("blocked", "blocked")):
        n = sum(t["status"] == status for t in tasks)
        if n:
            parts.append(f"{n} {label}")
    return f"{len(tasks)} open task{'s' if len(tasks) != 1 else ''}: " + ", ".join(parts)


def _lower(svc: Service, tasks: list[dict], by_id: dict[str, dict]) -> dict:
    per: dict[str, list[dict]] = {}
    for t in tasks:
        per.setdefault(t["owner"], []).append(t)
    agents = []
    for pid, ts in sorted(per.items(), key=lambda kv: kv[0]):
        ts = sorted(ts, key=lambda t: (t["status"] != "in_progress", t["updated_at"]))
        agents.append({"id": pid, "display": by_id[pid]["display"], "level": by_id[pid]["level"],
                       "model": by_id[pid]["model"], "count": len(ts), "line": _one_line(ts),
                       "tasks": [{"id": t["id"], "title": t["title"], "status": t["status"],
                                  "project": svc.s.projects.get(t["project_id"], {}).get("name", t["project_id"])}
                                 for t in ts[:SHOWN_PER_AGENT]], "more": max(0, len(ts) - SHOWN_PER_AGENT)})
    return {"count": len(tasks), "line": _one_line(tasks), "agents": agents}


def _conversations(svc: Service, manager: str) -> list[dict]:
    out = []
    for th in sorted(svc.s.threads.values(), key=lambda t: t["updated_at"], reverse=True):
        if manager not in th["participants"]:
            continue
        msgs = svc.s.messages[th["id"]]
        last = next((m for m in reversed(msgs) if m.get("kind") != "notice"), None)
        waiting = sum(1 for d in svc.s.dispatches.values()
                      if d.get("thread_id") == th["id"] and d["agent"] == manager and d["status"] in ("queued", "running"))
        out.append({"id": th["id"], "title": th["title"], "updated_at": th["updated_at"], "messages": len(msgs),
                    "waiting_for_reply": waiting,
                    "last": {"author": last["author"], "author_display": AGENTS.get(last["author"], {}).get("display", last["author"]),
                             "text": last["text"][:140]} if last else None})
        if len(out) >= CONVERSATIONS_PER_MANAGER:
            break
    return out


def manager_view(svc: Service, levels: Levels) -> dict:
    agents = _agents(svc, levels)
    by_id = {a["id"]: a for a in agents}
    top = max((a["level"] for a in agents), default=None)
    manager_ids = [a["id"] for a in agents if a["level"] == top]
    open_tasks = [t for t in svc.s.tasks.values() if t["status"] in OPEN_STATES and t["owner"] in by_id
                  and t["owner"] not in manager_ids]
    work = {m: [t for t in open_tasks if t["created_by"] == m] for m in manager_ids}
    other = [t for t in open_tasks if t["created_by"] not in manager_ids]
    waiting = {a["id"]: a for a in inbox.agent_work(svc)}
    managers = []
    for m in manager_ids:
        managers.append({**by_id[m], "waiting_on_owner": waiting.get(m, {}).get("waiting_on_owner", 0),
                         "own_open_tasks": sum(1 for t in svc.s.tasks.values() if t["owner"] == m and t["status"] in OPEN_STATES),
                         "conversations": _conversations(svc, m), "lower": _lower(svc, work[m], by_id)})
    return {"manager_level": top, "managers": managers, "other": _lower(svc, other, by_id), "agents": agents}
