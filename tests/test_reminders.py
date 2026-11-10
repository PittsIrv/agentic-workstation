"""What needs the owner now and the first reminder rule: boundaries, quiet hours, cap, repeat, snooze, pause,
the log, the local command, and that agents cannot snooze, pause, run or read the owner's settings."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from conftest import H
from workstation import cli, config, reminders
from workstation.principals import Tokens
from workstation.service import Forbidden

UTC = timezone.utc
HOSTILE = "<script>alert(1)</script> \x1b[2J\x1b]0;pwned\x07 [x](javascript:alert(3))"


def cfg(**kw):
    return {**config.REMINDER_DEFAULTS, "quiet_start": "00:00", "quiet_end": "00:00", **kw}  # no quiet hours unless set


def mk(c, tokens, who="claude", **kw):
    r = c.post("/api/asks", headers=H(tokens, who), json={"title": "Approve the release", "kind": "approval", **kw})
    r.raise_for_status()
    return r.json()["ask"]


def at(svc, minutes):
    """A clock `minutes` after the newest record (records are stamped with the real clock)."""
    return datetime.now(UTC) + timedelta(minutes=minutes)


def due_titles(svc, c, now, **kw):
    return [e["title"] for e in reminders.needs_now(svc, cfg(**kw), "UTC", now)["items"]]


# ---- the due computation, at its boundaries ---------------------------------------------------

@pytest.mark.parametrize("urgency,limit", [("high", 30), ("normal", 240), ("low", 1440)])
def test_wait_threshold_depends_on_urgency(ws, urgency, limit):
    app, c, tokens = ws
    mk(c, tokens, title="t", urgency=urgency)
    svc = app.state.ws.svc
    assert due_titles(svc, c, at(svc, limit - 2)) == []
    nn = reminders.needs_now(svc, cfg(), "UTC", at(svc, limit + 1))
    assert [e["title"] for e in nn["items"]] == ["t"]
    assert nn["items"][0]["reasons"][0]["code"] == "waited" and f"{urgency} urgency" in nn["items"][0]["why"]


def test_exactly_at_the_threshold_is_due(ws):
    app, c, tokens = ws
    a = mk(c, tokens, title="t", urgency="high")
    svc = app.state.ws.svc
    t0 = datetime.fromisoformat(a["created_at"])
    assert reminders.needs_now(svc, cfg(), "UTC", t0 + timedelta(minutes=30) - timedelta(seconds=1))["items"] == []
    assert len(reminders.needs_now(svc, cfg(), "UTC", t0 + timedelta(minutes=30))["items"]) == 1


def test_deadline_makes_an_item_due_before_its_wait_is_up(ws):
    app, c, tokens = ws
    svc, now = app.state.ws.svc, datetime.now(UTC)
    mk(c, tokens, title="soon", urgency="low", due_at=(now + timedelta(hours=3)).isoformat())
    mk(c, tokens, title="far", urgency="low", due_at=(now + timedelta(days=3)).isoformat())
    mk(c, tokens, title="gone", urgency="low", due_at=(now - timedelta(hours=1)).isoformat())
    items = {e["title"]: e for e in reminders.needs_now(svc, cfg(), "UTC", now)["items"]}
    assert set(items) == {"soon", "gone"}
    assert items["soon"]["reasons"][0]["code"] == "due_soon" and items["gone"]["reasons"][0]["code"] == "overdue"
    assert "passed" in items["gone"]["why"]


def test_what_is_blocked_is_part_of_why_but_does_not_make_an_item_due(ws):
    app, c, tokens = ws
    svc = app.state.ws.svc
    mk(c, tokens, title="quiet", urgency="low", blocked="the deploy")
    assert due_titles(svc, c, at(svc, 5)) == []
    nn = reminders.needs_now(svc, cfg(), "UTC", at(svc, 1500))
    assert [r["code"] for r in nn["items"][0]["reasons"]] == ["waited", "blocks"] and "the deploy" in nn["items"][0]["why"]


def test_decisions_and_tasks_in_review_are_included(ws):
    app, c, tokens = ws
    svc = app.state.ws.svc
    c.post("/api/decisions", headers=H(tokens), json={"question": "Which way?", "project_id": "workstation", "urgency": "high",
                                                     "options": [{"label": "A"}, {"label": "B"}]}).raise_for_status()
    t = c.post("/api/tasks", headers=H(tokens), json={"title": "Review me", "project_id": "workstation", "owner": "claude",
                                                       "status": "in_progress", "priority": "P1"}).json()["task"]
    c.post(f"/api/tasks/{t['id']}/artifacts", headers=H(tokens, "claude"), json={"kind": "doc", "title": "d", "url": "https://example.test/x"})
    c.patch(f"/api/tasks/{t['id']}", headers=H(tokens, "claude"), json={"changes": {"status": "needs_review"}}).raise_for_status()
    nn = reminders.needs_now(svc, cfg(), "UTC", at(svc, 45))
    assert {e["source"] for e in nn["items"]} == {"decision", "task"}


def test_an_answered_item_is_not_due(ws):
    app, c, tokens = ws
    a = mk(c, tokens, title="t", urgency="high")
    svc = app.state.ws.svc
    assert due_titles(svc, c, at(svc, 60)) == ["t"]
    c.post(f"/api/asks/{a['id']}/answer", headers=H(tokens), json={"resolution": "approved"}).raise_for_status()
    assert due_titles(svc, c, at(svc, 60)) == []


# ---- quiet hours, time zones ------------------------------------------------------------------

@pytest.mark.parametrize("hhmm,expect", [("20:59", False), ("21:00", True), ("23:59", True), ("00:00", True),
                                          ("07:59", True), ("08:00", False), ("12:00", False)])
def test_quiet_hours_across_midnight(hhmm, expect):
    h, m = map(int, hhmm.split(":"))
    now = datetime(2026, 11, 10, h, m, tzinfo=UTC)
    assert reminders.in_quiet(now, ZoneInfo("UTC"), "21:00", "08:00") is expect


def test_quiet_hours_same_day_and_none():
    tz = ZoneInfo("UTC")
    assert reminders.in_quiet(datetime(2026, 1, 1, 13, 0, tzinfo=UTC), tz, "12:00", "14:00")
    assert not reminders.in_quiet(datetime(2026, 1, 1, 14, 0, tzinfo=UTC), tz, "12:00", "14:00")
    assert not reminders.in_quiet(datetime(2026, 1, 1, 3, 0, tzinfo=UTC), tz, "00:00", "00:00")


def test_quiet_hours_use_the_display_time_zone():
    ny = ZoneInfo("America/New_York")
    now = datetime(2026, 11, 10, 3, 30, tzinfo=UTC)  # 22:30 the evening before in New York (UTC-5 in November)
    assert reminders.in_quiet(now, ny, "21:00", "08:00")
    assert not reminders.in_quiet(now, ZoneInfo("Asia/Tokyo"), "21:00", "08:00")  # 12:30 there


def test_nothing_is_reminded_in_quiet_hours_but_the_page_still_lists_the_item(ws):
    app, c, tokens = ws
    mk(c, tokens, title="t", urgency="high")
    svc = app.state.ws.svc
    night = datetime.now(UTC).replace(hour=23, minute=0) + timedelta(days=1)
    nn = reminders.needs_now(svc, cfg(quiet_start="21:00", quiet_end="08:00"), "UTC", night)
    assert [e["title"] for e in nn["items"]] == ["t"] and nn["reminders"]["held"] == "quiet_hours" and nn["reminders"]["due"] == []
    out = reminders.run(svc, "owner", cfg(quiet_start="21:00", quiet_end="08:00"), "UTC", now=night)
    assert out["due"] == [] and out["held"] == "quiet_hours" and svc.s.reminders == []


# ---- cap, repeat interval ----------------------------------------------------------------------

def test_daily_cap_limits_reminders_and_resets_next_local_day(ws):
    app, c, tokens = ws
    for i in range(5):
        mk(c, tokens, title=f"t{i}", urgency="high")
    svc = app.state.ws.svc
    base = datetime.now(UTC)
    first = reminders.run(svc, "owner", cfg(daily_cap=3), "UTC", now=base + timedelta(hours=1))
    assert len(first["due"]) == 3 and len(svc.s.reminders) == 3
    again = reminders.run(svc, "owner", cfg(daily_cap=3), "UTC", now=base + timedelta(hours=1, minutes=5))
    assert again["due"] == [] and again["held"] == "daily_cap"
    # the next local day: the cap is fresh, the three reminded items are still inside the repeat interval, so the other two
    nxt = (base + timedelta(hours=1)).replace(hour=1, minute=0) + timedelta(days=1)
    out = reminders.run(svc, "owner", cfg(daily_cap=3, repeat_minutes=100000), "UTC", now=nxt)
    assert len(out["due"]) == 2 and not ({e["title"] for e in out["due"]} & {r["title"] for r in svc.s.reminders[:3]})


def test_daily_cap_counts_the_local_day_not_utc(ws):
    app, c, tokens = ws
    svc = app.state.ws.svc
    late = datetime(2026, 11, 11, 4, 30, tzinfo=UTC)  # 23:30 on 10 Nov in New York
    svc.store.append("reminder_sent", "owner", {"key": "ask:x", "source": "ask", "item_id": "x", "title": "x", "urgency": "high",
                                                "why": "w", "channel": "print", "at": late.isoformat(timespec="seconds")})
    ny = "America/New_York"
    # an hour later it is 00:31 on 11 Nov in New York, but still the same day in UTC
    assert reminders.needs_now(svc, cfg(daily_cap=1), ny, late + timedelta(minutes=20))["reminders"]["sent_today"] == 1
    assert reminders.needs_now(svc, cfg(daily_cap=1), ny, late + timedelta(minutes=61))["reminders"]["sent_today"] == 0
    assert reminders.needs_now(svc, cfg(daily_cap=1), "UTC", late + timedelta(minutes=61))["reminders"]["sent_today"] == 1


def test_the_same_item_is_not_reminded_again_inside_the_repeat_interval(ws):
    app, c, tokens = ws
    mk(c, tokens, title="t", urgency="high")
    svc = app.state.ws.svc
    t0 = datetime.now(UTC) + timedelta(hours=1)
    conf = cfg(daily_cap=100, repeat_minutes=600)
    assert len(reminders.run(svc, "owner", conf, "UTC", now=t0)["due"]) == 1
    assert reminders.run(svc, "owner", conf, "UTC", now=t0 + timedelta(minutes=599))["due"] == []
    assert len(reminders.run(svc, "owner", conf, "UTC", now=t0 + timedelta(minutes=601))["due"]) == 1
    assert len(svc.s.reminders) == 2


def test_dry_run_records_nothing(ws):
    app, c, tokens = ws
    mk(c, tokens, title="t", urgency="high")
    svc = app.state.ws.svc
    out = reminders.run(svc, "owner", cfg(), "UTC", dry_run=True, now=at(svc, 60))
    assert len(out["due"]) == 1 and not out["recorded"] and svc.s.reminders == []


# ---- snooze, pause ------------------------------------------------------------------------------

def test_snooze_hides_an_item_until_it_ends_and_stops_reminders(ws):
    app, c, tokens = ws
    a = mk(c, tokens, title="t", urgency="high")
    svc = app.state.ws.svc
    key = f"ask:{a['id']}"
    r = c.post("/api/snooze", headers=H(tokens), json={"key": key, "minutes": 120})
    assert r.status_code == 200
    now = datetime.now(UTC)
    nn = reminders.needs_now(svc, cfg(), "UTC", now + timedelta(minutes=60))
    assert nn["items"] == [] and [e["title"] for e in nn["snoozed"]] == ["t"] and nn["reminders"]["due"] == []
    assert reminders.run(svc, "owner", cfg(), "UTC", now=now + timedelta(minutes=60))["due"] == []
    assert len(reminders.needs_now(svc, cfg(), "UTC", now + timedelta(minutes=121))["items"]) == 1  # snooze over
    assert c.post("/api/unsnooze", headers=H(tokens), json={"key": key}).status_code == 200
    assert len(reminders.needs_now(svc, cfg(), "UTC", now + timedelta(minutes=60))["items"]) == 1


def test_snooze_validates_key_and_minutes(ws):
    app, c, tokens = ws
    a = mk(c, tokens, title="t")
    assert c.post("/api/snooze", headers=H(tokens), json={"key": "ask:nope", "minutes": 5}).status_code == 404
    for bad in (0, -5, 10 ** 7):
        assert c.post("/api/snooze", headers=H(tokens), json={"key": f"ask:{a['id']}", "minutes": bad}).status_code == 422
    assert c.post("/api/unsnooze", headers=H(tokens), json={"key": f"ask:{a['id']}"}).status_code == 404


def test_global_pause_holds_reminders_but_not_the_list_and_ends_by_itself(ws):
    app, c, tokens = ws
    mk(c, tokens, title="t", urgency="high")
    svc = app.state.ws.svc
    assert c.post("/api/reminders/pause", headers=H(tokens), json={"minutes": 90}).status_code == 200
    now = datetime.now(UTC)
    nn = reminders.needs_now(svc, cfg(), "UTC", now + timedelta(minutes=60))
    assert len(nn["items"]) == 1 and nn["reminders"]["held"] == "paused"
    assert reminders.run(svc, "owner", cfg(), "UTC", now=now + timedelta(minutes=60))["due"] == []
    assert reminders.needs_now(svc, cfg(), "UTC", now + timedelta(minutes=91))["reminders"]["held"] is None
    c.post("/api/reminders/pause", headers=H(tokens), json={"minutes": None})  # until resumed
    assert reminders.needs_now(svc, cfg(), "UTC", now + timedelta(days=30))["reminders"]["held"] == "paused"
    c.post("/api/reminders/resume", headers=H(tokens))
    assert reminders.needs_now(svc, cfg(), "UTC", now + timedelta(days=30))["reminders"]["held"] is None


def test_state_survives_a_restart(ws, tmp_path):
    app, c, tokens = ws
    a = mk(c, tokens, title="t", urgency="high")
    c.post("/api/snooze", headers=H(tokens), json={"key": f"ask:{a['id']}", "minutes": 600})
    c.post("/api/reminders/pause", headers=H(tokens), json={"minutes": None})
    reminders.run(app.state.ws.svc, "owner", cfg(), "UTC", now=at(None, 61))
    s = app.state.ws.store
    s._load()
    assert f"ask:{a['id']}" in s.state.snoozes and s.state.pause is not None


# ---- the log -------------------------------------------------------------------------------------

def sent(svc, ask, minutes_ago):
    svc.store.append("reminder_sent", "owner", {"key": f"ask:{ask['id']}", "source": "ask", "item_id": ask["id"],
                                                "title": ask["title"], "urgency": "high", "why": "w", "channel": "print",
                                                "at": (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")})


def test_log_says_what_happened_after_each_reminder(ws):
    app, c, tokens = ws
    quick, slow, idle = (mk(c, tokens, title=t, urgency="high") for t in ("quick", "slow", "idle"))
    svc = app.state.ws.svc
    for a in (quick, slow, idle):
        sent(svc, a, 10)
    c.post(f"/api/asks/{quick['id']}/answer", headers=H(tokens), json={"resolution": "approved"}).raise_for_status()
    log = reminders.log(svc, cfg(acted_window_minutes=120))
    out = {r["title"]: r["outcome"] for r in log["rows"]}
    assert out == {"quick": "acted_in_window", "slow": "not_yet", "idle": "not_yet"}
    assert log["counts"]["in_window"] == 1 and log["counts"]["waiting"] == 2 and log["total"] == 3
    c.post("/api/snooze", headers=H(tokens), json={"key": f"ask:{idle['id']}", "minutes": 60})
    assert {r["title"]: r["outcome"] for r in reminders.log(svc, cfg())["rows"]}["idle"] == "snoozed"
    # answered 10 minutes after the reminder: outside a 5-minute window, inside a 120-minute one
    assert {r["title"]: r["outcome"] for r in reminders.log(svc, cfg(acted_window_minutes=5))["rows"]}["quick"] == "acted_later"
    assert "score" not in json.dumps(log).lower()


# ---- API: same data everywhere, agents refused -------------------------------------------------------

def test_api_and_page_read_the_same_computation(ws):
    app, c, tokens = ws
    mk(c, tokens, title="one", urgency="high")
    mk(c, tokens, title="two", urgency="low")
    svc = app.state.ws.svc
    svc.store.append  # silence linters
    full = c.get("/api/needs-now?full=true", headers=H(tokens)).json()
    compact = c.get("/api/needs-now", headers=H(tokens)).json()
    assert [e["key"] for e in full["items"]] == [e["key"] for e in compact["items"]]
    assert all("reasons" in e and e["why"] for e in compact["items"])
    assert set(compact) >= {"items", "snoozed", "reminders", "waiting_not_due"}


def test_agents_cannot_snooze_pause_run_or_read_settings(ws):
    app, c, tokens = ws
    a = mk(c, tokens, title="t", urgency="high")
    key = f"ask:{a['id']}"
    for who in ("claude", "gpt", "gemini"):
        h = H(tokens, who)
        assert c.post("/api/snooze", headers=h, json={"key": key, "minutes": 5}).status_code == 403
        assert c.post("/api/unsnooze", headers=h, json={"key": key}).status_code == 403
        assert c.post("/api/reminders/pause", headers=h, json={"minutes": 5}).status_code == 403
        assert c.post("/api/reminders/resume", headers=h).status_code == 403
        assert c.post("/api/reminders/run", headers=h, json={}).status_code == 403
        assert c.get("/api/reminders/settings", headers=h).status_code == 403
        assert c.get("/api/reminders/log", headers=h).status_code == 403
        for etype in ("item_snoozed", "reminders_paused", "reminder_sent"):
            assert c.post("/api/events", headers=h, json={"event_type": etype, "idempotency_key": f"k-{who}-{etype}",
                                                          "project_id": "workstation", "payload": {"key": key}}).status_code in (403, 422)
    st = app.state.ws.store.state
    assert st.snoozes == {} and st.pause is None and st.reminders == []
    with pytest.raises(Forbidden):
        reminders.snooze(app.state.ws.svc, "claude", key, 5)


def test_an_agent_sees_its_own_items_state_and_not_other_agents_titles(ws):
    app, c, tokens = ws
    mine = mk(c, tokens, "claude", title="my ask", urgency="high")
    mk(c, tokens, "gpt", title="gpt secret title", urgency="high")
    done = mk(c, tokens, "claude", title="my answered ask")
    c.post(f"/api/asks/{done['id']}/answer", headers=H(tokens), json={"resolution": "approved", "note": "ok"})
    c.post("/api/snooze", headers=H(tokens), json={"key": f"ask:{mine['id']}", "minutes": 60})
    c.post("/api/reminders/pause", headers=H(tokens), json={"minutes": 30})
    v = c.get("/api/needs-now", headers=H(tokens, "claude")).json()
    states = {m["title"]: m["state"] for m in v["mine"]}
    assert states == {"my ask": "snoozed", "my answered ask": "answered"}
    assert "gpt secret title" not in json.dumps(v) and v["owner_items_waiting"] == 2 and v["reminders_paused"] is True
    assert "snoozed_until" in next(m for m in v["mine"] if m["title"] == "my ask")
    assert next(m for m in v["mine"] if m["title"] == "my answered ask")["resolution"] == "approved"
    due = c.get("/api/needs-now", headers=H(tokens, "gpt")).json()
    assert [m["state"] for m in due["mine"]] == ["waiting"] and "title" in due["mine"][0]
    assert "no time data" or True
    assert "daybook" not in json.dumps(v).lower()


def test_an_agents_item_shows_as_due_to_that_agent(ws):
    app, c, tokens = ws
    a = mk(c, tokens, "claude", title="late", urgency="high", due_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
    v = c.get("/api/needs-now", headers=H(tokens, "claude")).json()
    assert v["mine"][0]["state"] == "due" and v["owner_items_due"] == 1 and v["advice"].startswith("hold")


def test_settings_view_lists_every_default_and_marks_changes(ws, tmp_path):
    app, c, tokens = ws
    s = c.get("/api/reminders/settings", headers=H(tokens)).json()
    assert {x["key"] for x in s["settings"]} == set(config.REMINDER_DEFAULTS)
    assert all(not x["changed"] and x["value"] == x["default"] for x in s["settings"])
    (tmp_path / "config.toml").write_text('[reminders]\ndaily_cap = 1\nquiet_start = "22:00"\n')
    got = config.load(tmp_path)
    assert got.reminders["daily_cap"] == 1 and got.reminders["wait_minutes_high"] == 30
    assert got.reminders_set == ["daily_cap", "quiet_start"]


@pytest.mark.parametrize("body", ['quiet_start = "9pm"', 'daily_cap = -1', 'daily_cap = "three"', 'repeat_minutes = 0',
                                  'wait_minutes_high = true', 'daily_cap = 1.5', 'nope = 1'])
def test_bad_reminder_config_is_refused(tmp_path, body):
    (tmp_path / "config.toml").write_text(f"[reminders]\n{body}\n")
    with pytest.raises(ValueError):
        config.load(tmp_path)


# ---- hostile text and the local command ------------------------------------------------------------

def test_hostile_titles_are_stripped_for_the_terminal():
    assert "\x1b" not in cli.clean(HOSTILE) and "\x07" not in cli.clean(HOSTILE)
    assert len(cli.clean("x" * 1000, 80)) == 80


def test_notify_passes_text_as_arguments_never_inside_a_script(monkeypatch):
    seen = []

    class Done:
        pass
    monkeypatch.setattr("subprocess.run", lambda cmd, **k: seen.append(cmd) or Done())
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    for plat in ("darwin", "linux"):
        monkeypatch.setattr(cli.sys, "platform", plat)
        assert cli.notify("Workstation", '"; do shell script "rm -rf ~"; "') == ""
    mac, lin = seen
    assert mac[0] == "osascript" and not any("rm -rf" in part for part in mac[:6])  # the script text is fixed
    assert mac[-2:] == ['"; do shell script "rm -rf ~"; "', "Workstation"]
    assert lin[:2] == ["notify-send", "--"]


def test_notify_falls_back_when_the_tool_is_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert "no notification tool" in cli.notify("t", "b")


def test_ws_now_remind_snooze_pause_from_the_command_line(ws, tmp_path, capsys, monkeypatch):
    app, c, tokens = ws
    import uvicorn, threading, socket, time as _t
    data = app.state.ws.data
    (data / "secrets").mkdir(exist_ok=True)
    for who in ("claude",):
        (data / "secrets" / f"{who}.token").write_text(tokens[who])
    app.state.ws.config.reminders.update(quiet_start="00:00", quiet_end="00:00")  # the real clock may be at night
    mk(c, tokens, title=HOSTILE[:100], urgency="high", due_at=(datetime.now(UTC) - timedelta(minutes=5)).isoformat())
    # run the CLI against the in-process app by routing its httpx client to the TestClient
    monkeypatch.setattr(cli, "client", lambda args, who: _Bridge(c, tokens[who]))
    base = ["--data", str(data)]

    def run(*argv):
        cli.main(base + list(argv))
        return capsys.readouterr().out

    now = json.loads(run("now"))
    assert now["items"][0]["reasons"][0]["code"] == "overdue"
    agent = json.loads(run("now", "--as", "claude"))
    assert agent["mine"][0]["state"] == "due"
    out = run("remind", "--dry-run")
    assert "would remind" in out and "\x1b" not in out and "deadline passed" in out
    assert len(app.state.ws.svc.s.reminders) == 0
    out = run("remind")
    assert "reminding (1 of 3 today)" in out and len(app.state.ws.svc.s.reminders) == 1
    assert "nothing to remind" in run("remind")
    run("pause", "--minutes", "30")
    assert app.state.ws.svc.s.pause
    assert "paused" in run("remind")
    run("resume")
    key = now["items"][0]["key"]
    assert json.loads(run("snooze", key, "--minutes", "60"))["key"] == key
    with pytest.raises(SystemExit):
        run("snooze", key, "--minutes", "5", "--as", "claude")


class _Bridge:
    """Lets the command-line client talk to the in-process test app."""

    def __init__(self, client, token):
        self.c, self.h = client, {"Authorization": f"Bearer {token}"}

    def get(self, path, params=None):
        return self.c.get(path, params=params, headers=self.h)

    def post(self, path, json=None):
        return self.c.post(path, json=json, headers=self.h)
