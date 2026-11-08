"""Read models for the dashboard: what needs the owner, decisions, project pulse, threads."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .principals import AGENTS, owner
from .service import Service

DISPLAY_TZ = "UTC"  # set from config at start
OPEN = {"inbox", "ready", "in_progress", "blocked", "needs_review"}
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

# Sources the dashboard can see, stated honestly; set from config at start.
COVERAGE: list[dict] = []
TICKETS: dict | None = None  # a tickets.TicketFile, set from config at start


def _bucket(t: dict, now: datetime, tz: ZoneInfo) -> str:
    if not t.get("due_at"):
        return "undated"
    due = datetime.fromisoformat(t["due_at"]).astimezone(tz)
    today = now.astimezone(tz).date()
    if due < now:
        return "overdue"
    if due.date() == today:
        return "today"
    if due.date() <= today + timedelta(days=7):
        return "this_week"  # the next seven days
    return "later"


def action_for(t: dict) -> str:
    if t["status"] == "needs_review":
        return "Review and accept"
    if t["status"] == "blocked":
        return "Unblock: " + (t.get("blocked_reason") or "see task")
    if t.get("suggested"):
        return "Triage suggestion"
    return t.get("next_action") or "Do it"


def task_card(svc: Service, t: dict, now: datetime | None = None, tz: ZoneInfo | None = None) -> dict:
    tz = tz or ZoneInfo(DISPLAY_TZ)
    now = now or datetime.now(tz)
    due_local = datetime.fromisoformat(t["due_at"]).astimezone(tz).strftime("%a %b %d, %H:%M %Z") if t.get("due_at") else None
    return {
        "id": t["id"], "title": t["title"], "status": t["status"], "owner": t["owner"],
        "owner_display": AGENTS[t["owner"]]["display"], "priority": t.get("priority"), "project_id": t["project_id"],
        "project": svc.s.projects.get(t["project_id"], {}).get("name", t["project_id"]),
        "next_action": t.get("next_action") or "", "blocked_reason": t.get("blocked_reason"),
        "action": action_for(t), "bucket": _bucket(t, now, tz), "due_at": t.get("due_at"), "due_local": due_local,
        "due_tz": t.get("due_tz"), "why": t.get("why") or "", "created_by": t["created_by"],
        "source_system": t.get("source_system"), "provenance": t.get("provenance"),
        "evidence_url": t.get("evidence_url"), "thread_id": t.get("thread_id"), "decision_id": t.get("decision_id"),
        "artifacts": t["artifacts"], "reviews": t["reviews"], "version": t["version"], "updated_at": t["updated_at"],
        "suggested": t.get("suggested", False), "history": t["history"],
    }


def home(svc: Service, agents: list[dict], now: datetime | None = None) -> dict:
    tz = ZoneInfo(DISPLAY_TZ)
    now = now or datetime.now(tz)
    open_tasks = [t for t in svc.s.tasks.values() if t["status"] in OPEN]
    mine = [t for t in open_tasks if t["owner"] == owner() or t["status"] in ("needs_review", "blocked")]
    order = {"overdue": 0, "today": 1, "this_week": 2, "undated": 3, "later": 4}
    actions = sorted((task_card(svc, t, now, tz) for t in mine),
                     key=lambda c: (order[c["bucket"]], c["status"] != "needs_review",
                                    PRIORITY_ORDER.get(c["priority"] or "P2", 2), c["due_at"] or "9999"))
    pending = [d for d in svc.s.decisions.values() if d["status"] == "pending"]
    pending.sort(key=lambda d: (d.get("urgency") != "high", d.get("due_at") or "9999", d["created_at"]))
    projects = []
    for p in svc.s.projects.values():
        ts = [t for t in svc.s.tasks.values() if t["project_id"] == p["id"]]
        live = sorted((t for t in ts if t["status"] in OPEN),
                      key=lambda t: (PRIORITY_ORDER.get(t.get("priority") or "P2", 2), t["created_at"]))
        progress = max((h for t in ts for h in t["history"]), key=lambda h: h["at"], default=None)
        projects.append({**{k: p.get(k) for k in ("id", "name", "goal", "stage", "milestone")},
                         "open": len(live), "blocked": sum(t["status"] == "blocked" for t in live),
                         "done": sum(t["status"] == "done" for t in ts),
                         "next": ({"title": live[0]["title"], "owner": AGENTS[live[0]["owner"]]["display"]}
                                  if live else None),
                         "last_progress": progress})
    threads = []
    for th in sorted(svc.s.threads.values(), key=lambda t: t["updated_at"], reverse=True):
        msgs = svc.s.messages[th["id"]]
        last = next((m for m in reversed(msgs) if m.get("kind") != "notice"), None)
        ds = [d for d in svc.s.dispatches.values() if d.get("thread_id") == th["id"]]
        threads.append({"id": th["id"], "title": th["title"], "project_id": th["project_id"],
                        "updated_at": th["updated_at"], "messages": len(msgs),
                        "pending": sum(d["status"] in ("queued", "running") for d in ds),
                        "last": {"author": AGENTS.get(last["author"], {}).get("display", last["author"]),
                                 "text": last["text"][:140]} if last else None})
    decisions = [decision_card(d) for d in pending]
    if decisions:
        d = decisions[0]
        nxt = {"kind": "decision", "id": d["id"], "text": d["question"],
               "meta": f"{len(d['options'])} options · {len(d['recommendations'])} agent position(s)"}
    elif actions:
        a = actions[0]
        nxt = {"kind": "task", "id": a["id"], "text": a["action"],
               "meta": f"{a['title']} · {a['project']}" + (f" · due {a['due_local']}" if a.get("due_local") else "")}
    else:
        nxt = None
    by_agent = {a["id"]: a for a in agents}
    coverage = []
    for c in COVERAGE:
        a = by_agent.get(c.get("agent") or "")
        if not c.get("agent"):
            state = c["state"]
        elif a is None:
            state = "not found"
        else:
            state = "connected" if a["available"] else a["reason"] + (" · session pickup" if a.get("pull_fallback") else "")
        coverage.append({"source": c["source"], "state": state})
    return {"now": now.isoformat(timespec="minutes"), "tz": DISPLAY_TZ, "actions": actions, "next": nxt,
            "decisions": decisions, "projects": projects, "agents": agents,
            "threads": threads, "coverage": coverage,
            "tickets": (TICKETS.read() if TICKETS else {"state": "not_configured", "open": [], "total": 0}), "seq": len(svc.s.events)}


def decision_card(d: dict) -> dict:
    return {k: d.get(k) for k in ("id", "question", "context", "options", "recommendations", "evidence", "urgency",
                                  "due_at", "due_tz", "status", "thread_id", "project_id", "created_by", "created_at",
                                  "option_id", "note", "revisit", "resolved_by", "resolved_at", "resulting_task",
                                  "follow_up_owner", "history", "version")}


def thread_view(svc: Service, thread_id: str) -> dict:
    th = svc.s.threads[thread_id]
    msgs = []
    for m in svc.s.messages[thread_id]:
        d = svc.s.dispatches.get(m.get("dispatch_id") or "")
        msgs.append({**m, "author_display": AGENTS.get(m["author"], {}).get("display", m["author"]),
                     "author_kind": AGENTS.get(m["author"], {}).get("kind", "unknown"),
                     "dispatch": {k: d.get(k) for k in ("id", "mode", "until_seq", "target_seq", "context")} if d else None})
    ds = [{k: d.get(k) for k in ("id", "agent", "mode", "status", "error", "until_seq", "target_seq", "updated_at",
                                  "awaiting_pickup")}
          for d in svc.s.dispatches.values() if d.get("thread_id") == thread_id]
    decisions = [decision_card(d) for d in svc.s.decisions.values() if d.get("thread_id") == thread_id]
    tasks = [task_card(svc, t) for t in svc.s.tasks.values() if t.get("thread_id") == thread_id or t["id"] == th.get("task_id")]
    return {**{k: th[k] for k in ("id", "title", "project_id", "task_id", "participants", "budget",
                                  "critique_rounds_used", "created_at", "updated_at")},
            "project": svc.s.projects.get(th["project_id"], {}).get("name"),
            "messages": msgs, "dispatches": ds, "decisions": decisions, "tasks": tasks,
            "turns_used": len(ds)}


def activity(svc: Service, limit: int = 200, project_id: str | None = None, actor: str | None = None) -> list[dict]:
    out = []
    for e in reversed(svc.s.events):
        if project_id and e.get("project_id") != project_id:
            continue
        if actor and e["actor"] != actor:
            continue
        p = e["payload"]
        out.append({"seq": e["seq"], "type": e["event_type"], "actor": e["actor"],
                    "actor_display": AGENTS.get(e["actor"], {}).get("display", e["actor"]),
                    "at": e["recorded_at"], "source_system": e["source_system"], "project_id": e.get("project_id"),
                    "summary": p.get("title") or p.get("question") or (p.get("text") or "")[:120]
                               or p.get("status") or p.get("error") or "", "ref": p.get("id") or p.get("dispatch_id"),
                    "provenance": e.get("provenance")})
        if len(out) >= limit:
            break
    return out
