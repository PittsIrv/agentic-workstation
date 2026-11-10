"""The owner's inbox: everything an agent is waiting on the owner for, most urgent first.

Four sources, one list: items agents submit (asks), decisions still pending, and tasks in `needs_review`
or `blocked`. Pure functions of the current state and a clock, so ordering is testable.

Ordering (this project's default, not a researched rule): overdue first, then high, normal and low urgency;
inside a group the earliest deadline, then the item that has waited longest. Urgency of a decision is its own
`urgency` field; of a task, P0 and P1 are high, P2 normal, P3 low.
"""
from __future__ import annotations

from datetime import datetime

from .principals import AGENTS
from .service import Service
from . import views

RANK = {"high": 0, "normal": 1, "low": 2}
TASK_URGENCY = {"P0": "high", "P1": "high", "P2": "normal", "P3": "low"}
RECENT_ANSWERED = 15


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _name(pid: str) -> str:
    return AGENTS.get(pid, {}).get("display", pid)


def ask_card(svc: Service, a: dict) -> dict:
    return {
        "source": "ask", "id": a["id"], "kind": a["kind"], "title": a["title"], "text": a["text"],
        "blocked": a["blocked"], "cost_of_waiting": a["cost_of_waiting"], "urgency": a["urgency"],
        "asked_by": a["asked_by"], "asked_by_display": _name(a["asked_by"]), "project_id": a.get("project_id"),
        "project": svc.s.projects.get(a.get("project_id") or "", {}).get("name"), "task_id": a.get("task_id"),
        "due_at": a.get("due_at"), "due_tz": a.get("due_tz"), "url": a.get("url"), "status": a["status"],
        "created_at": a["created_at"], "resolution": a.get("resolution"), "note": a.get("note"),
        "closed_by": a.get("closed_by"), "closed_at": a.get("closed_at"), "version": a["version"],
        "link": f"#/inbox",
    }


def _waiting_since_status(t: dict) -> str:
    """When the task entered its current status (the start of the trailing run in its history)."""
    since = t["created_at"]
    for h in reversed(t["history"]):
        if h.get("status") != t["status"]:
            break
        since = h["at"]
    return since


def _entries(svc: Service) -> list[dict]:
    out = [dict(ask_card(svc, a), waiting_since=a["created_at"]) for a in svc.s.asks.values() if a["status"] == "open"]
    for d in svc.s.decisions.values():
        if d["status"] == "pending":
            out.append({"source": "decision", "id": d["id"], "kind": "decision", "title": d["question"],
                        "text": d.get("context") or "", "blocked": "", "cost_of_waiting": "",
                        "urgency": "high" if d.get("urgency") == "high" else "normal",
                        "asked_by": d["created_by"], "asked_by_display": _name(d["created_by"]),
                        "project_id": d.get("project_id"),
                        "project": svc.s.projects.get(d.get("project_id") or "", {}).get("name"),
                        "due_at": d.get("due_at"), "due_tz": d.get("due_tz"), "link": f"#/decisions/{d['id']}",
                        "waiting_since": d["created_at"], "decision": views.decision_card(d)})
    for t in svc.s.tasks.values():
        if t["status"] in ("needs_review", "blocked"):
            review = t["status"] == "needs_review"
            out.append({"source": "task", "id": t["id"], "kind": "review" if review else "blocked", "title": t["title"],
                        "text": t.get("next_action") or "",
                        "blocked": "" if review else (t.get("blocked_reason") or ""), "cost_of_waiting": "",
                        "urgency": TASK_URGENCY.get(t.get("priority") or "P2", "normal"),
                        "asked_by": t["owner"], "asked_by_display": _name(t["owner"]),
                        "project_id": t["project_id"], "project": svc.s.projects.get(t["project_id"], {}).get("name"),
                        "due_at": t.get("due_at"), "due_tz": t.get("due_tz"), "link": f"#/tasks/{t['id']}",
                        "waiting_since": _waiting_since_status(t), "task": views.task_card(svc, t)})
    return out


def order_key(e: dict, now: datetime):
    overdue = bool(e.get("due_at")) and _dt(e["due_at"]) < now
    return (0 if overdue else 1, RANK[e["urgency"]], e["due_at"] and _dt(e["due_at"]).timestamp() or float("inf"),
            _dt(e["waiting_since"]).timestamp(), e["id"])


def inbox(svc: Service, now: datetime) -> dict:
    items = sorted(_entries(svc), key=lambda e: order_key(e, now))
    for e in items:
        e["waiting_s"] = max(0, int((now - _dt(e["waiting_since"])).total_seconds()))
        e["overdue"] = bool(e.get("due_at")) and _dt(e["due_at"]) < now
    closed = sorted((a for a in svc.s.asks.values() if a["status"] != "open"), key=lambda a: a["closed_at"], reverse=True)
    return {"now": now.isoformat(timespec="seconds"), "items": items,
            "answered": [ask_card(svc, a) for a in closed[:RECENT_ANSWERED]]}


def asks_for(svc: Service, who: str, status: str | None) -> list[dict]:
    """The asks a caller may see: the owner sees all, an agent only its own (including the owner's answer)."""
    human = AGENTS.get(who, {}).get("kind") == "human"
    rows = [a for a in svc.s.asks.values() if (human or a["asked_by"] == who)
            and (not status or status == "all" or (a["status"] == "open") == (status == "open"))]
    return [ask_card(svc, a) for a in sorted(rows, key=lambda a: a["created_at"], reverse=True)]


def agent_work(svc: Service) -> list[dict]:
    """Per agent: what it is waiting on the owner for and what it is working on. Counts and short titles only."""
    out = []
    for pid, a in AGENTS.items():
        if a["kind"] != "agent":
            continue
        asks = [x for x in svc.s.asks.values() if x["asked_by"] == pid and x["status"] == "open"]
        decisions = [d for d in svc.s.decisions.values() if d["status"] == "pending" and d["created_by"] == pid]
        reviews = [t for t in svc.s.tasks.values() if t["owner"] == pid and t["status"] == "needs_review"]
        mine = [t for t in svc.s.tasks.values() if t["owner"] == pid and t["status"] in ("in_progress", "ready")]
        mine.sort(key=lambda t: (t["status"] != "in_progress", t["updated_at"]), reverse=False)
        out.append({"id": pid, "display": a["display"], "waiting_on_owner": len(asks) + len(decisions) + len(reviews),
                    "working_on": [{"id": t["id"], "title": t["title"], "status": t["status"]} for t in mine[:3]],
                    "working_count": len(mine)})
    return out
