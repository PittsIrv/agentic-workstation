"""The weekly review: what was decided, accepted and left to go stale in a seven-day window.

A window is seven calendar days in the display time zone, from local midnight of its first day to local
midnight after its last day. Past windows are rebuilt from each record's history, so a task that is done
now still counts as open in the week it was open. The Markdown form reuses the decision-record escaping.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .export import md, record_filename
from .principals import AGENTS
from .views import OPEN

STALE_DAYS = 7
WINDOW_DAYS = 7
REVIEWED_AGENTS = ("claude", "gpt", "gemini")


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _state_at(rec: dict, ref: datetime) -> tuple[str, datetime] | None:
    """Status and time of the last change at or before `ref`, from the record's history."""
    seen = [h for h in rec.get("history", []) if _t(h["at"]) <= ref]
    return (seen[-1]["status"], _t(seen[-1]["at"])) if seen else None


def build(svc, tz_name: str, end: date | None = None, now: datetime | None = None) -> dict:
    tz = ZoneInfo(tz_name)
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(tz).date()
    end = min(end or today, today)
    first = end - timedelta(days=WINDOW_DAYS - 1)
    start = datetime.combine(first, time.min, tz)
    stop = datetime.combine(end + timedelta(days=1), time.min, tz)
    ref = min(now, stop)  # "open", "stale" and "pending" are judged as of the end of the window, or now
    in_window = lambda s: s is not None and start <= _t(s) < stop
    name = lambda who: AGENTS.get(who, {}).get("display", who)
    projects = svc.s.projects
    pname = lambda pid: projects.get(pid, {}).get("name") or pid
    local = lambda s: _t(s).astimezone(tz).date().isoformat()

    resolved = []
    for d in svc.s.decisions.values():
        if in_window(d.get("resolved_at")):
            chosen = next((o["label"] for o in d["options"] if o["id"] == d.get("option_id")), None)
            resolved.append({"id": d["id"], "question": d["question"], "status": d["status"], "outcome": chosen,
                             "by": name(d["resolved_by"]), "on": local(d["resolved_at"]), "at": d["resolved_at"],
                             "project_id": d["project_id"], "project": pname(d["project_id"]),
                             "record": record_filename(d, tz_name)})
    resolved.sort(key=lambda r: r["at"])

    accepted, stale = [], []
    for t in svc.s.tasks.values():
        hist = t.get("history", [])
        done_at = None
        for prev, h in zip([None] + hist, hist):
            if h["status"] == "done" and (prev is None or prev["status"] != "done") and in_window(h["at"]):
                done_at = h["at"]
        if done_at:
            accepted.append({"id": t["id"], "title": t["title"], "owner": t["owner"], "owner_display": name(t["owner"]),
                             "on": local(done_at), "at": done_at, "project_id": t["project_id"],
                             "project": pname(t["project_id"])})
        state = _state_at(t, ref)
        if state and state[0] in OPEN and (ref - state[1]) >= timedelta(days=STALE_DAYS):
            stale.append({"id": t["id"], "title": t["title"], "owner": t["owner"], "owner_display": name(t["owner"]),
                          "status": state[0], "idle_days": (ref - state[1]).days, "project_id": t["project_id"],
                          "project": pname(t["project_id"])})
    accepted.sort(key=lambda r: r["at"])
    stale.sort(key=lambda r: (-r["idle_days"], r["id"]))

    pending = []
    for d in svc.s.decisions.values():
        resolved_at = d.get("resolved_at")
        if _t(d["created_at"]) <= ref and (not resolved_at or _t(resolved_at) > ref):
            pending.append({"id": d["id"], "question": d["question"], "urgency": d.get("urgency"),
                            "age_days": (ref - _t(d["created_at"])).days, "project_id": d["project_id"],
                            "project": pname(d["project_id"])})
    pending.sort(key=lambda r: (-r["age_days"], r["id"]))

    events = [e for e in svc.s.events if in_window(e["recorded_at"])]
    agents = []
    for a in REVIEWED_AGENTS:
        agents.append({"id": a, "display": name(a),
                       "replies": sum(e["event_type"] == "message_posted" and e["actor"] == a for e in events),
                       "tasks_done": sum(r["owner"] == a for r in accepted),
                       "refusals": sum(e["event_type"] == "action_refused" and e["actor"] == a for e in events)})

    rollups = []
    for pid in projects:
        open_now = sum(1 for t in svc.s.tasks.values() if t["project_id"] == pid
                       and (s := _state_at(t, ref)) and s[0] in OPEN)
        rollups.append({"id": pid, "name": pname(pid),
                        "decisions_resolved": sum(r["project_id"] == pid for r in resolved),
                        "tasks_accepted": sum(r["project_id"] == pid for r in accepted),
                        "open": open_now, "stale": sum(r["project_id"] == pid for r in stale),
                        "pending_decisions": sum(r["project_id"] == pid for r in pending)})

    later = end + timedelta(days=WINDOW_DAYS)
    nxt = later.isoformat() if later <= today else (today.isoformat() if end < today else None)
    return {"start": first.isoformat(), "end": end.isoformat(), "tz": tz_name,
            "prev": (end - timedelta(days=WINDOW_DAYS)).isoformat(),
            "next": nxt,
            "decisions_resolved": resolved, "tasks_accepted": accepted, "stale_tasks": stale,
            "pending_decisions": pending, "agents": agents, "projects": rollups}


def render_review(rv: dict) -> dict:
    """The review as Markdown for a repo or a weekly note. Returns {"filename", "markdown"}."""
    out = [f"# Weekly review: {rv['start']} to {rv['end']}", "",
           f"Seven days in {rv['tz']}, midnight to midnight. Open, stale and pending are judged at the end of the window; "
           f"stale means no activity for {STALE_DAYS} days or more.", ""]

    def section(title, rows, line):
        out.extend([f"## {title}", ""])
        out.extend([line(r) for r in rows] or ["_None._"])
        out.append("")

    def outcome(r):
        if r["status"] == "approved":
            return f"approved: {md(r['outcome'])}"
        return {"rejected": "rejected", "deferred": "deferred"}.get(r["status"], md(r["status"]))

    section("Decisions resolved", rv["decisions_resolved"],
            lambda r: f"- **{md(r['question'])}** — {outcome(r)} ({r['on']}, {md(r['project'])}). Record: `{r['record']}`")
    section("Tasks accepted", rv["tasks_accepted"],
            lambda r: f"- {md(r['title'])} (owner {md(r['owner_display'])}, {r['on']}, {md(r['project'])})")
    section("Stale tasks", rv["stale_tasks"],
            lambda r: f"- {md(r['title'])} ({md(r['status'].replace('_', ' '))}, owner {md(r['owner_display'])}, "
                      f"{r['idle_days']} days idle, {md(r['project'])})")
    section("Decisions still pending", rv["pending_decisions"],
            lambda r: f"- **{md(r['question'])}** ({r['age_days']} days old, {md(r['project'])})")
    section("Agents", rv["agents"],
            lambda r: f"- **{md(r['display'])}:** replies {r['replies']}, tasks done {r['tasks_done']}, "
                      f"refused attempts {r['refusals']}")
    section("Projects", rv["projects"],
            lambda r: f"- **{md(r['name'])}:** decisions resolved {r['decisions_resolved']}, tasks accepted {r['tasks_accepted']}, "
                      f"open {r['open']}, stale {r['stale']}, pending decisions {r['pending_decisions']}")
    return {"filename": f"{rv['end']}-weekly-review.md", "markdown": "\n".join(out).rstrip("\n") + "\n"}
