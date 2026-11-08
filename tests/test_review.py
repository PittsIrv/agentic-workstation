"""The weekly review on two weeks of fixture history, with a controlled clock."""
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from workstation import review as weekly
from workstation import store as store_mod
from workstation.principals import configure_owner
from workstation.service import Service
from workstation.store import Store

GOLDEN = Path(__file__).parent / "fixtures" / "weekly-review.md"
TZ = "America/New_York"  # EST (UTC-5) from 2026-11-01, so the week of Nov 2-8 runs 05:00Z Nov 2 to 05:00Z Nov 9
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def at(day, hhmm="12:00"):
    return datetime.fromisoformat(f"2026-{day}T{hhmm if hhmm.count(':') == 2 else hhmm + ':00'}+00:00")


@pytest.fixture
def world(tmp_path, monkeypatch):
    configure_owner("owner", "Owner")  # other tests may have configured a different owner in this process
    clock = {"now": at("10-18")}
    monkeypatch.setattr(store_mod, "now_iso", lambda: clock["now"].isoformat(timespec="seconds"))
    svc = Service(Store(tmp_path / "w.db"))

    def go(day, hhmm="12:00"):
        clock["now"] = at(day, hhmm)

    def task(day, title, owner="owner", project="general", status="ready"):
        go(day)
        return svc.create_task("owner", title=title, project_id=project, owner=owner, status=status)[0]["id"]

    def move(tid, day, hhmm, status):
        go(day, hhmm)
        svc.update_task("owner", tid, expected_version=svc.s.tasks[tid]["version"], status=status)

    def finish(tid, day, hhmm):
        move(tid, day, hhmm, "in_progress")
        go(day, hhmm)
        svc.link_artifact("owner", tid, kind="doc", title="notes", url="https://example.com/n")
        move(tid, day, hhmm, "needs_review")
        move(tid, day, hhmm, "done")

    def decision(day, question, project="general"):
        go(day)
        return svc.create_decision("owner", question=question, project_id=project,
                                   options=[{"label": "Yes"}, {"label": "No"}])["id"]

    def resolve(did, day, hhmm="12:00"):
        go(day, hhmm)
        d = svc.s.decisions[did]
        svc.resolve_decision("owner", did, status="approved", option_id=d["options"][0]["id"])

    go("10-18")
    svc.upsert_project("owner", "general", "General")
    svc.upsert_project("owner", "site", "Website")
    # tasks
    t_old = task("10-20", "Old and quiet", status="in_progress")
    t_busy = task("10-30", "Touched midweek")
    move(t_busy, "11-03", "12:00", "in_progress")
    t_early = task("11-02", "Finished early in the week", owner="claude", project="site")
    finish(t_early, "11-03", "12:00")
    t_edge_in = task("11-05", "Done at 23:30 on the last day")
    finish(t_edge_in, "11-09", "04:30")
    t_edge_out = task("11-06", "Done just after midnight")
    finish(t_edge_out, "11-09", "05:30")
    # done, then reopened inside the week: not accepted; done, reopened after the week: still accepted that week
    t_reopened = task("11-02", "Reopened the same week")
    finish(t_reopened, "11-04", "09:00")
    move(t_reopened, "11-06", "09:00", "in_progress")
    t_later = task("11-02", "Reopened the week after")
    finish(t_later, "11-04", "09:30")
    move(t_later, "11-12", "09:00", "in_progress")
    # decisions
    d_resolved = decision("10-25", "Adopt SSE?")
    resolve(d_resolved, "11-04")
    d_old = decision("10-28", "Never answered")
    d_late = decision("11-05", "Answered next week")
    resolve(d_late, "11-12")
    # agent activity
    go("11-01", "00:30")
    th = svc.create_thread("owner", title="Hello", project_id="general")["id"]
    svc.post_message("owner", th, "@claude @gpt hi")
    for day, hhmm, who in (("11-01", "00:45", "claude"), ("11-03", "10:00", "claude"), ("11-04", "10:00", "claude"),
                           ("11-05", "10:00", "gpt")):
        go(day, hhmm)
        svc.post_message(who, th, f"{who} reply")
    go("11-05", "10:00")
    assert svc.record_refusal("claude", "POST /api/decisions/{id}/resolve", "only the owner resolves decisions")
    go("11-05", "10:00:30")
    assert not svc.record_refusal("claude", "POST /api/decisions/{id}/resolve", "again within a minute")
    go("11-12", "10:00")
    svc.record_refusal("gpt", "POST /api/tasks/{id}", "later")
    return svc


def week(world, end="2026-11-08", now=NOW):
    return weekly.build(world, TZ, date.fromisoformat(end), now)


def titles(rows, key="title"):
    return sorted(r[key] for r in rows)


def test_week_of_nov_2_to_8_counts(world):
    rv = week(world)
    assert (rv["start"], rv["end"], rv["tz"]) == ("2026-11-02", "2026-11-08", TZ)
    assert [d["question"] for d in rv["decisions_resolved"]] == ["Adopt SSE?"]
    assert rv["decisions_resolved"][0]["outcome"] == "Yes"
    assert titles(rv["tasks_accepted"]) == ["Done at 23:30 on the last day", "Finished early in the week",
                                             "Reopened the week after"]
    assert titles(rv["stale_tasks"]) == ["Old and quiet"] and rv["stale_tasks"][0]["idle_days"] == 19
    assert [(d["question"], d["age_days"]) for d in rv["pending_decisions"]] == [("Never answered", 11), ("Answered next week", 3)]
    by = {a["id"]: a for a in rv["agents"]}
    assert (by["claude"]["replies"], by["claude"]["tasks_done"], by["claude"]["refusals"]) == (2, 1, 1)
    assert (by["gpt"]["replies"], by["gpt"]["refusals"]) == (1, 0)
    roll = {p["id"]: p for p in rv["projects"]}
    assert (roll["general"]["decisions_resolved"], roll["general"]["tasks_accepted"], roll["site"]["tasks_accepted"]) == (1, 2, 1)
    assert roll["general"]["stale"] == 1 and roll["general"]["pending_decisions"] == 2


def test_week_boundaries_follow_the_configured_zone(world):
    rv = week(world)
    assert "Done just after midnight" not in titles(rv["tasks_accepted"])
    next_week = week(world, "2026-11-15", datetime(2026, 11, 16, 12, tzinfo=timezone.utc))
    assert titles(next_week["tasks_accepted"]) == ["Done just after midnight"]
    assert [d["question"] for d in next_week["decisions_resolved"]] == ["Answered next week"]
    assert {a["id"]: a["refusals"] for a in next_week["agents"]}["gpt"] == 1
    # the same history in UTC: Nov 9 04:30Z and 05:30Z both fall after the week ends at 00:00Z
    utc = weekly.build(world, "UTC", date(2026, 11, 8), NOW)
    assert titles(utc["tasks_accepted"]) == ["Finished early in the week", "Reopened the week after"]
    assert titles(weekly.build(world, "UTC", date(2026, 11, 15), datetime(2026, 11, 16, tzinfo=timezone.utc))["tasks_accepted"]) == [
        "Done at 23:30 on the last day", "Done just after midnight"]


def test_past_weeks_are_rebuilt_from_history_not_current_state(world):
    old = week(world, "2026-11-01", datetime(2026, 11, 2, 12, tzinfo=timezone.utc))
    # the task finished on Nov 3 was still open on Nov 1; the decision resolved on Nov 4 was still pending
    assert "Finished early in the week" not in titles(old["tasks_accepted"])
    assert "Adopt SSE?" in [d["question"] for d in old["pending_decisions"]]
    assert {p["id"]: p["open"] for p in old["projects"]}["site"] == 0  # not created yet


def test_default_window_ends_today_and_has_links(world):
    rv = weekly.build(world, TZ, None, NOW)
    assert (rv["start"], rv["end"], rv["next"]) == ("2026-11-04", "2026-11-10", None)
    assert rv["prev"] == "2026-11-03"
    assert titles(rv["tasks_accepted"]) == ["Done at 23:30 on the last day", "Done just after midnight",
                                             "Reopened the week after"]  # "Reopened the same week" is open again at the end
    mid = week(world)
    assert (mid["prev"], mid["next"]) == ("2026-11-01", "2026-11-10")


def test_future_end_is_clamped_to_today(world):
    assert weekly.build(world, TZ, date(2027, 1, 1), NOW)["end"] == "2026-11-10"


def test_markdown_review_matches_the_golden_file(world):
    r = weekly.render_review(week(world))
    assert r["filename"] == "2026-11-08-weekly-review.md"
    assert r["markdown"] == GOLDEN.read_text()


# ---- API, refusals, CLI ---------------------------------------------------------------------------

from conftest import H  # noqa: E402


def test_review_api_is_read_only_for_every_identity_and_validates_the_date(ws):
    app, c, tokens = ws
    for who in ("owner", "claude", "gpt"):
        r = c.get("/api/review", headers=H(tokens, who))
        assert r.status_code == 200 and r.json()["tz"] == "UTC" and len(r.json()["agents"]) == 3
        assert c.get("/api/review/record", headers=H(tokens, who)).json()["markdown"].startswith("# Weekly review: ")
    assert c.get("/api/review").status_code == 401
    assert c.get("/api/review?end=not-a-date", headers=H(tokens)).status_code == 422
    assert c.post("/api/review", headers=H(tokens)).status_code == 405


def test_an_agent_trying_an_owner_only_action_is_counted_once_per_minute(ws):
    app, c, tokens = ws
    q = {"question": "Q?", "options": [{"label": "a"}, {"label": "b"}], "project_id": "workstation"}
    did = c.post("/api/decisions", headers=H(tokens), json=q).json()["id"]
    for _ in range(3):  # a looping agent
        assert c.post(f"/api/decisions/{did}/resolve", headers=H(tokens, "gpt"), json={"status": "rejected"}).status_code == 403
    assert c.post("/api/threads", headers=H(tokens, "claude"), json={"title": "t", "project_id": "workstation"}).status_code == 403
    rv = c.get("/api/review", headers=H(tokens)).json()
    by = {a["id"]: a for a in rv["agents"]}
    assert by["gpt"]["refusals"] == 1 and by["claude"]["refusals"] == 1
    # the owner is never counted, and a refused request changed nothing
    assert c.get("/api/decisions", headers=H(tokens)).json()[0]["status"] == "pending"


def test_cli_review_prints_and_writes(ws, tmp_path, monkeypatch, capsys):
    app, c, tokens = ws
    from workstation import cli
    monkeypatch.setattr(cli, "client", lambda args, who: type("C", (), {
        "get": staticmethod(lambda path, **kw: c.get(path, headers=H(tokens, who), **kw))})())
    cli.main(["--data", str(app.state.ws.data), "review", "--end", "2026-11-08"])
    assert capsys.readouterr().out.startswith("# Weekly review: 2026-11-02 to 2026-11-08")
    cli.main(["--data", str(app.state.ws.data), "review", "--end", "2026-11-08", "--out", str(tmp_path / "notes") + "/"])
    assert (tmp_path / "notes" / "2026-11-08-weekly-review.md").read_text().startswith("# Weekly review")


# ---- hostile text ---------------------------------------------------------------------------------

from markdown_it import MarkdownIt  # noqa: E402

from test_browser_security import Dom  # noqa: E402
from test_export import HOSTILE_ALL  # noqa: E402


def hostile_review():
    h = HOSTILE_ALL
    rv = {"start": "2026-11-02", "end": "2026-11-08", "tz": TZ, "prev": "2026-11-01", "next": None,
          "decisions_resolved": [{"question": h, "status": "approved", "outcome": h, "on": "2026-11-04", "project": h,
                                  "record": "2026-11-04-x.md", "id": "d1", "by": h, "at": "", "project_id": "p"}],
          "tasks_accepted": [{"title": h, "owner_display": h, "on": "2026-11-03", "project": h, "id": "t1", "owner": "claude",
                              "at": "", "project_id": "p"}],
          "stale_tasks": [{"title": h, "status": "in_progress", "owner_display": h, "idle_days": 9, "project": h, "id": "t2",
                           "owner": "claude", "project_id": "p"}],
          "pending_decisions": [{"question": h, "age_days": 4, "project": h, "id": "d2", "urgency": None, "project_id": "p"}],
          "agents": [{"id": "claude", "display": h, "replies": 1, "tasks_done": 1, "refusals": 1}],
          "projects": [{"id": "p", "name": h, "decisions_resolved": 1, "tasks_accepted": 1, "open": 1, "stale": 1,
                        "pending_decisions": 1}]}
    return weekly.render_review(rv)["markdown"]


def test_hostile_text_cannot_inject_markup_into_a_rendered_review():
    html = MarkdownIt("commonmark", {"html": True}).render(hostile_review())  # raw HTML allowed: the worst case
    dom = Dom()
    dom.feed(html)
    assert set(dom.tags) <= {"h1", "h2", "p", "ul", "li", "strong", "em", "code"}, set(dom.tags)
    assert dom.tags.count("h1") == 1 and dom.tags.count("h2") == 6
    assert not [a for a in dom.attrs if a.startswith("on")] and not dom.urls
    assert hostile_review().count("\n## ") == 6
    assert "onerror" in hostile_review()  # still readable as text


def test_refusal_dedupe_survives_interleaved_events_and_other_agents(world):
    svc = world
    assert svc.record_refusal("gpt", "POST /x", "r") is True      # clock is 2026-11-12 10:00 from the fixture
    assert svc.record_refusal("claude", "POST /x", "r") is True   # another agent is its own stream
    assert svc.record_refusal("gpt", "POST /x", "r") is False     # gpt again within the minute, claude's event in between
    assert svc.record_refusal("gpt", "POST /y", "r") is True      # a different action counts
    assert svc.record_refusal("owner", "POST /x", "r") is False   # the owner is never logged
