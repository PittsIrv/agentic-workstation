"""What needs the owner now, and the first reminder rule.

One computation, `needs_now`, over the inbox (agent asks, pending decisions, tasks waiting for review or blocked).
The Home page, `GET /api/needs-now`, `ws now` and `ws remind` all read it, so they cannot disagree.

An item is *due* when any of these holds (thresholds are named defaults in `config.REMINDER_DEFAULTS`, ours, not researched):
  overdue    its deadline has passed
  due soon   its deadline is within `deadline_window_minutes`
  waited     it has waited at least the minutes set for its urgency
A snoozed item is left out until its snooze ends. A due item is *reminded* only when all of these hold:
  reminders are not paused, it is outside quiet hours, today's reminders are under `daily_cap`,
  and this item was not reminded within `repeat_minutes`.
Nothing here knows what the owner is doing right now; that is why the rule is conservative.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import inbox as inbox_view
from .principals import AGENTS, is_human
from .service import Conflict, Forbidden, Invalid, NotFound

MAX_SNOOZE_MIN = 14 * 24 * 60
MAX_PAUSE_MIN = 14 * 24 * 60
CHANNELS = ("print", "notification")
SETTING_LABELS = {
    "wait_minutes_high": "Minutes a high-urgency item may wait before it is due",
    "wait_minutes_normal": "Minutes a normal-urgency item may wait before it is due",
    "wait_minutes_low": "Minutes a low-urgency item may wait before it is due",
    "deadline_window_minutes": "A deadline closer than this many minutes makes an item due",
    "quiet_start": "Quiet hours start (no reminders), local time",
    "quiet_end": "Quiet hours end, local time",
    "daily_cap": "Most reminders recorded per local day",
    "repeat_minutes": "The same item is not reminded again within this many minutes",
    "acted_window_minutes": "Reminder log: acted on means answered within this many minutes",
}


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def in_quiet(now: datetime, tz: ZoneInfo, start: str, end: str) -> bool:
    """True when the local time is inside quiet hours. Start later than end means across midnight; equal means none."""
    t = now.astimezone(tz)
    cur, a, b = t.hour * 60 + t.minute, _hm(start), _hm(end)
    if a == b:
        return False
    return a <= cur < b if a < b else (cur >= a or cur < b)


def span(seconds: float) -> str:
    m = int(abs(seconds) // 60)
    if m < 60:
        return f"{m} min"
    if m < 48 * 60:
        return f"{m // 60} h" + (f" {m % 60} min" if m % 60 and m < 6 * 60 else "")
    return f"{m // 1440} days"


def reasons_for(e: dict, cfg: dict, now: datetime) -> list[dict]:
    """Why this item is due now (empty when it is not due). Text built here from numbers; agent words are quoted apart."""
    out = []
    if e.get("due_at"):
        left = (_dt(e["due_at"]) - now).total_seconds()
        if left < 0:
            out.append({"code": "overdue", "text": f"deadline passed {span(left)} ago"})
        elif left <= cfg["deadline_window_minutes"] * 60:
            out.append({"code": "due_soon", "text": f"deadline in {span(left)}"})
    limit = cfg[f"wait_minutes_{e['urgency']}"]
    if e["waiting_s"] >= limit * 60:
        out.append({"code": "waited", "text": f"waited {span(e['waiting_s'])}; {e['urgency']} urgency is due after {span(limit * 60)}"})
    if out and e.get("blocked"):
        out.append({"code": "blocks", "text": "blocks (agent's words): " + e["blocked"], "agent_words": True})
    return out


def _pause_state(svc, now: datetime) -> dict | None:
    p = svc.s.pause
    if not p or (p.get("until") and _dt(p["until"]) <= now):
        return None
    return p


def compact(e: dict) -> dict:
    return {k: v for k, v in e.items() if k not in ("decision", "task")}


def needs_now(svc, cfg: dict, tz_name: str, now: datetime | None = None, full: bool = False) -> dict:
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(tz_name)
    ib = inbox_view.inbox(svc, now)
    items, snoozed, waiting = [], [], 0
    for e in ib["items"]:
        r = reasons_for(e, cfg, now)
        if e["snoozed_until"]:
            snoozed.append(compact(e))
        elif r:
            e["reasons"], e["why"] = r, "; ".join(x["text"] for x in r)
            items.append(e if full else compact(e))
        else:
            waiting += 1
    last: dict[str, datetime] = {}
    today = now.astimezone(tz).date()
    sent_today = 0
    for r in svc.s.reminders:
        at = _dt(r["at"])
        last[r["key"]] = max(at, last.get(r["key"], at))
        sent_today += at.astimezone(tz).date() == today
    cap = cfg["daily_cap"]
    paused, quiet = _pause_state(svc, now), in_quiet(now, tz, cfg["quiet_start"], cfg["quiet_end"])
    held = "paused" if paused else "quiet_hours" if quiet else "daily_cap" if sent_today >= cap else None
    fresh = [e for e in items if e["key"] not in last
             or (now - last[e["key"]]).total_seconds() >= cfg["repeat_minutes"] * 60]
    for e in items:
        e["reminded_at"] = last[e["key"]].isoformat(timespec="seconds") if e["key"] in last else None
    reminder_due = [] if held else [e["key"] for e in fresh[:max(0, cap - sent_today)]]
    return {"now": now.isoformat(timespec="seconds"), "tz": tz_name, "items": items, "snoozed": snoozed,
            "waiting_not_due": waiting,
            "reminders": {"paused": paused, "quiet_now": quiet, "quiet_hours": f"{cfg['quiet_start']} to {cfg['quiet_end']}",
                          "sent_today": sent_today, "daily_cap": cap, "held": held, "due": reminder_due}}


def agent_view(svc, who: str, cfg: dict, tz_name: str, now: datetime | None = None) -> dict:
    """For an agent that wants to know whether to interrupt: only its own items' states, plus how many items already
    wait on the owner and whether reminders are held. No other agent's titles, no screen-time data."""
    now = now or datetime.now(timezone.utc)
    full = needs_now(svc, cfg, tz_name, now)
    due_keys = {e["key"] for e in full["items"]}
    snz = {e["key"]: e["snoozed_until"] for e in full["snoozed"]}
    mine = []
    ib = inbox_view.inbox(svc, now)
    for e in ib["items"]:
        if e["asked_by"] != who:
            continue
        state = "snoozed" if e["key"] in snz else "due" if e["key"] in due_keys else "waiting"
        row = {"key": e["key"], "id": e["id"], "source": e["source"], "title": e["title"], "state": state,
               "waiting_s": e["waiting_s"]}
        if state == "snoozed":
            row["snoozed_until"] = snz[e["key"]]
        mine.append(row)
    for a in inbox_view.asks_for(svc, who, "closed")[:inbox_view.RECENT_ANSWERED]:
        mine.append({"key": f"ask:{a['id']}", "id": a["id"], "source": "ask", "title": a["title"],
                     "state": "withdrawn" if a["status"] == "withdrawn" else "answered", "resolution": a["resolution"],
                     "note": a["note"], "closed_at": a["closed_at"]})
    rm = full["reminders"]
    return {"now": full["now"], "for": who, "owner_items_due": len(full["items"]),
            "owner_items_waiting": len(full["items"]) + full["waiting_not_due"] + len(full["snoozed"]),
            "reminders_paused": bool(rm["paused"]), "quiet_hours_now": rm["quiet_now"],
            "advice": ("hold: the owner already has items due; add yours only if it cannot wait"
                       if full["items"] else "clear: nothing is due on the owner"),
            "mine": mine}


# ---- owner actions (recorded as events) -------------------------------------------------

def _owner(actor: str, what: str) -> None:
    if not is_human(actor):
        raise Forbidden(f"only the owner {what}")


def snooze(svc, actor: str, key: str, minutes: int, now: datetime | None = None) -> str:
    _owner(actor, "snoozes items")
    now = now or datetime.now(timezone.utc)
    if not (isinstance(minutes, int) and not isinstance(minutes, bool) and 1 <= minutes <= MAX_SNOOZE_MIN):
        raise Invalid(f"minutes must be a whole number from 1 to {MAX_SNOOZE_MIN}")
    if key not in {e["key"] for e in inbox_view.inbox(svc, now)["items"]}:
        raise NotFound(f"no open inbox item {key}")
    until = (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    svc.store.append("item_snoozed", actor, {"key": key, "until": until})
    return until


def unsnooze(svc, actor: str, key: str) -> None:
    _owner(actor, "unsnoozes items")
    if key not in svc.s.snoozes:
        raise NotFound(f"{key} is not snoozed")
    svc.store.append("item_unsnoozed", actor, {"key": key})


def pause(svc, actor: str, minutes: int | None, now: datetime | None = None) -> dict:
    """Pause all reminders for `minutes`, or until resumed when None."""
    _owner(actor, "pauses reminders")
    now = now or datetime.now(timezone.utc)
    until = None
    if minutes is not None:
        if not (isinstance(minutes, int) and not isinstance(minutes, bool) and 1 <= minutes <= MAX_PAUSE_MIN):
            raise Invalid(f"minutes must be a whole number from 1 to {MAX_PAUSE_MIN}, or empty for until resumed")
        until = (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    svc.store.append("reminders_paused", actor, {"until": until})
    return svc.s.pause


def resume(svc, actor: str) -> None:
    _owner(actor, "resumes reminders")
    svc.store.append("reminders_resumed", actor, {})


def run(svc, actor: str, cfg: dict, tz_name: str, channel: str = "print", dry_run: bool = False,
        now: datetime | None = None) -> dict:
    """Decide which due items to remind about right now and, unless dry_run, record that they were reminded.
    The caller delivers them (print or a local notification). Nothing leaves this process."""
    _owner(actor, "runs reminders")
    if channel not in CHANNELS:
        raise Invalid(f"channel must be one of {list(CHANNELS)}")
    now = now or datetime.now(timezone.utc)
    with svc.store.batch():
        nn = needs_now(svc, cfg, tz_name, now)
        keys = nn["reminders"]["due"]
        due = [e for e in nn["items"] if e["key"] in keys]
        if not dry_run:
            for e in due:
                svc.store.append("reminder_sent", actor, {
                    "key": e["key"], "source": e["source"], "item_id": e["id"], "title": e["title"][:200],
                    "urgency": e["urgency"], "why": e["why"][:400], "channel": channel, "at": nn["now"]})
    return {"now": nn["now"], "recorded": bool(due) and not dry_run, "dry_run": dry_run, "due": due,
            "held": nn["reminders"]["held"], "sent_today": nn["reminders"]["sent_today"] + (0 if dry_run else len(due)),
            "daily_cap": nn["reminders"]["daily_cap"]}


# ---- settings view and the log -------------------------------------------------------------

def settings(cfg: dict, changed: list[str], tz_name: str, svc, now: datetime | None = None) -> dict:
    from .config import REMINDER_DEFAULTS
    now = now or datetime.now(timezone.utc)
    return {"tz": tz_name, "paused": _pause_state(svc, now),
            "settings": [{"key": k, "label": SETTING_LABELS[k], "value": cfg[k], "default": REMINDER_DEFAULTS[k],
                          "changed": k in changed} for k in REMINDER_DEFAULTS],
            "note": "Every default is this project's own placeholder, not a researched number. Change them in "
                    "<data>/config.toml under [reminders] and restart `ws serve`.",
            "limits": "Nothing here knows whether you are in focused work right now, so the rule is conservative: "
                      "fewer, later reminders."}


def _closed_at(svc, key: str, after: datetime) -> tuple[str, str | None]:
    """(what happened, when) for the item behind a reminder, judged from the records."""
    src, _, rid = key.partition(":")
    if src == "ask":
        a = svc.s.asks.get(rid)
        if not a:
            return "unknown", None
        return ("open", None) if a["status"] == "open" else (a["status"], a["closed_at"])
    if src == "decision":
        d = svc.s.decisions.get(rid)
        if not d:
            return "unknown", None
        return ("open", None) if d["status"] == "pending" else ("closed", d.get("resolved_at"))
    t = svc.s.tasks.get(rid)
    if not t:
        return "unknown", None
    for h in t["history"]:
        if _dt(h["at"]) > after and h.get("status") not in ("needs_review", "blocked"):
            return "closed", h["at"]
    return "open", None


def log(svc, cfg: dict, now: datetime | None = None, limit: int = 50) -> dict:
    """Every reminder with what happened to its item afterwards. Facts only; no scoring."""
    now = now or datetime.now(timezone.utc)
    window = cfg["acted_window_minutes"] * 60
    rows, counts = [], {"in_window": 0, "later": 0, "snoozed": 0, "waiting": 0, "gone": 0}
    for r in reversed(svc.s.reminders):
        what, when = _closed_at(svc, r["key"], _dt(r["at"]))
        if when:
            lag = (_dt(when) - _dt(r["at"])).total_seconds()
            outcome = "acted_in_window" if lag <= window else "acted_later"
            bucket = "in_window" if outcome == "acted_in_window" else "later"
        elif what == "unknown":
            outcome, bucket, lag = "item_gone", "gone", None
        elif (u := svc.s.snoozes.get(r["key"])) and _dt(u) > now:
            outcome, bucket, lag = "snoozed", "snoozed", None
        else:
            outcome, bucket, lag = "not_yet", "waiting", None
        counts[bucket] += 1
        rows.append({"at": r["at"], "title": r["title"], "key": r["key"], "urgency": r["urgency"], "why": r["why"],
                     "channel": r["channel"], "outcome": outcome, "ended": what if when else None,
                     "after_s": int(lag) if lag is not None else None, "link": _link(r)})
    return {"window_minutes": cfg["acted_window_minutes"], "total": len(rows), "counts": counts,
            "rows": rows[:limit]}


def _link(r: dict) -> str:
    return {"ask": "#/inbox", "decision": f"#/decisions/{r['item_id']}", "task": f"#/tasks/{r['item_id']}"}.get(r["source"], "#/inbox")
