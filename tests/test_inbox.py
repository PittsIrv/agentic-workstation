"""The agent-submitted inbox: submitting, ordering, answering, limits and hostile text."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import H, make
from workstation import cli, inbox
from workstation.principals import AGENTS

HOSTILE = ("<script>alert(1)</script> <img src=x onerror=alert(2)> [x](javascript:alert(3)) "
           "\" onmouseover=\"alert(4)\" ' onfocus='alert(5)' </script>")


def ask(c, tokens, who="claude", **kw):
    body = {"title": "Approve the release", "kind": "approval", **kw}
    return c.post("/api/asks", headers=H(tokens, who), json=body)


def titles(c, tokens):
    return [i["title"] for i in c.get("/api/inbox", headers=H(tokens)).json()["items"]]


def test_two_agents_submit_and_the_owner_sees_both(ws):
    app, c, tokens = ws
    r = ask(c, tokens, "claude", title="Pick a name", kind="question", blocked="the release",
            cost_of_waiting="the build sits idle", urgency="high", project_id="workstation")
    assert r.status_code == 200 and r.json()["outcome"] == "created"
    assert ask(c, tokens, "gpt", title="Review PR 12", kind="review").status_code == 200
    items = c.get("/api/inbox", headers=H(tokens)).json()["items"]
    assert {i["asked_by"] for i in items} == {"claude", "gpt"}
    first = items[0]
    assert first["title"] == "Pick a name" and first["asked_by"] == "claude"  # asked_by comes from the token
    assert first["blocked"] == "the release" and first["cost_of_waiting"] == "the build sits idle"
    assert first["waiting_s"] >= 0 and first["project"] == "Workstation build"


def test_author_comes_from_the_token_not_the_body(ws):
    app, c, tokens = ws
    r = c.post("/api/asks", headers=H(tokens, "gpt"), json={"title": "t", "asked_by": "owner", "created_by": "claude"})
    assert r.json()["ask"]["asked_by"] == "gpt"


def test_ordering_overdue_then_urgency_then_deadline_then_longest_wait(ws):
    app, c, tokens = ws
    svc, now = app.state.ws.svc, datetime.now(timezone.utc)
    past, soon, later = ((now + timedelta(hours=h)).isoformat() for h in (-2, 3, 30))
    mk = lambda title, **kw: ask(c, tokens, title=title, kind="question", **kw).raise_for_status()
    mk("low-old", urgency="low")
    mk("normal-no-date")
    mk("normal-later", due_at=later)
    mk("normal-soon", due_at=soon)
    mk("high-no-date", urgency="high")
    mk("low-overdue", urgency="low", due_at=past)
    assert titles(c, tokens) == ["low-overdue", "high-no-date", "normal-soon", "normal-later", "normal-no-date", "low-old"]
    # equal urgency and no deadline: the one that has waited longest comes first
    for a in svc.s.asks.values():
        if a["title"] == "normal-no-date":
            a["created_at"] = (now - timedelta(days=2)).isoformat()
        if a["title"] == "low-old":
            a["created_at"] = (now - timedelta(days=9)).isoformat()
    ib = c.get("/api/inbox", headers=H(tokens)).json()["items"]
    assert next(i for i in ib if i["title"] == "normal-no-date")["waiting_s"] >= 2 * 86400 - 5
    assert titles(c, tokens)[-2:] == ["normal-no-date", "low-old"]


def test_ties_break_by_wait_time():
    now = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
    mk = lambda i, hours: {"id": i, "urgency": "normal", "due_at": None, "waiting_since": (now - timedelta(hours=hours)).isoformat()}
    ordered = sorted([mk("a", 1), mk("b", 5), mk("c", 3)], key=lambda e: inbox.order_key(e, now))
    assert [e["id"] for e in ordered] == ["b", "c", "a"]


def test_pending_decisions_and_review_tasks_join_the_same_list(ws):
    app, c, tokens = ws
    c.post("/api/decisions", headers=H(tokens, "claude"), json={
        "question": "Which database?", "options": [{"label": "a"}, {"label": "b"}], "project_id": "workstation",
        "urgency": "high"}).raise_for_status()
    t = c.post("/api/tasks", headers=H(tokens, "claude"), json={"title": "Ship it", "project_id": "workstation",
                                                                 "owner": "claude", "status": "in_progress", "priority": "P3"}).json()["task"]
    c.post(f"/api/tasks/{t['id']}/artifacts", headers=H(tokens, "claude"),
           json={"kind": "doc", "title": "d", "url": "https://example.com/d"}).raise_for_status()
    c.patch(f"/api/tasks/{t['id']}", headers=H(tokens, "claude"), json={"changes": {"status": "needs_review"}}).raise_for_status()
    ask(c, tokens, title="Normal question", kind="question").raise_for_status()
    items = c.get("/api/inbox", headers=H(tokens)).json()["items"]
    assert [(i["source"], i["title"]) for i in items] == [
        ("decision", "Which database?"), ("ask", "Normal question"), ("task", "Ship it")]
    assert items[2]["urgency"] == "low" and items[2]["kind"] == "review"


def test_the_owner_answers_and_the_agent_reads_the_answer(ws):
    app, c, tokens = ws
    a = ask(c, tokens, kind="question", title="Which port?").json()["ask"]
    assert c.post(f"/api/asks/{a['id']}/answer", headers=H(tokens), json={"resolution": "answered", "note": "8770"}).status_code == 200
    assert titles(c, tokens) == []
    mine = c.get("/api/asks?status=closed", headers=H(tokens, "claude")).json()
    assert mine[0]["resolution"] == "answered" and mine[0]["note"] == "8770"
    assert c.get("/api/inbox", headers=H(tokens)).json()["answered"][0]["id"] == a["id"]
    assert c.post(f"/api/asks/{a['id']}/answer", headers=H(tokens), json={"resolution": "dismissed"}).status_code == 409


def test_resolution_must_fit_the_kind(ws):
    app, c, tokens = ws
    appr = ask(c, tokens, kind="approval").json()["ask"]["id"]
    q = ask(c, tokens, kind="question", title="q").json()["ask"]["id"]
    ans = lambda i, **b: c.post(f"/api/asks/{i}/answer", headers=H(tokens), json=b).status_code
    assert ans(appr, resolution="answered", note="x") == 422
    assert ans(q, resolution="approved") == 422
    assert ans(q, resolution="answered", note="  ") == 422
    assert ans(appr, resolution="declined") == 200
    assert ans(q, resolution="dismissed") == 200


def test_agents_cannot_answer_and_the_attempt_is_logged(ws):
    app, c, tokens = ws
    a = ask(c, tokens).json()["ask"]
    for who in ("claude", "gpt", "gemini"):
        assert c.post(f"/api/asks/{a['id']}/answer", headers=H(tokens, who),
                      json={"resolution": "approved"}).status_code == 403
    assert c.get("/api/inbox", headers=H(tokens, "claude")).status_code == 403  # the owner's list is the owner's
    assert titles(c, tokens) == ["Approve the release"]
    refused = [e for e in app.state.ws.store.state.events if e["event_type"] == "action_refused"]
    assert refused


def test_an_agent_sees_only_its_own_items_and_can_withdraw_only_those(ws):
    app, c, tokens = ws
    mine = ask(c, tokens, "claude", title="mine").json()["ask"]
    theirs = ask(c, tokens, "gpt", title="theirs").json()["ask"]
    assert [a["title"] for a in c.get("/api/asks", headers=H(tokens, "claude")).json()] == ["mine"]
    assert len(c.get("/api/asks", headers=H(tokens)).json()) == 2
    assert c.post(f"/api/asks/{theirs['id']}/withdraw", headers=H(tokens, "claude")).status_code == 403
    assert c.post(f"/api/asks/{mine['id']}/withdraw", headers=H(tokens)).status_code == 403  # not even the owner
    assert c.post(f"/api/asks/{mine['id']}/withdraw", headers=H(tokens, "claude")).status_code == 200
    assert titles(c, tokens) == ["theirs"]
    assert c.post(f"/api/asks/{mine['id']}/withdraw", headers=H(tokens, "claude")).status_code == 409


def test_idempotency_and_the_event_endpoint(ws):
    app, c, tokens = ws
    body = {"title": "once", "kind": "question", "idempotency_key": "k1"}
    h = H(tokens, "claude")
    assert c.post("/api/asks", headers=h, json=body).json()["outcome"] == "created"
    assert c.post("/api/asks", headers=h, json=body).json()["outcome"] == "duplicate"
    assert c.post("/api/asks", headers=h, json={**body, "title": "changed"}).status_code == 409
    ev = {"event_type": "ask_created", "idempotency_key": "e1", "project_id": "workstation",
          "payload": {"title": "via events", "kind": "review"}}
    assert c.post("/api/events", headers=H(tokens, "gemini"), json=ev).json()["outcome"] == "created"
    assert c.post("/api/events", headers=H(tokens, "gemini"), json=ev).json()["outcome"] == "duplicate"
    assert sorted(titles(c, tokens)) == ["once", "via events"]


def test_the_open_item_cap_is_per_agent_and_configurable(tmp_path, agents):
    (tmp_path / "config.toml").write_text("[inbox]\nmax_open_per_agent = 2\n")
    app, c, tokens = make(tmp_path, agents)
    for i in range(2):
        assert ask(c, tokens, "claude", title=f"t{i}").status_code == 200
    r = ask(c, tokens, "claude", title="one too many")
    assert r.status_code == 409 and "limit 2" in r.json()["error"]
    assert ask(c, tokens, "gpt", title="other agent is unaffected").status_code == 200
    first = next(iter(app.state.ws.store.state.asks))
    c.post(f"/api/asks/{first}/withdraw", headers=H(tokens, "claude")).raise_for_status()
    assert ask(c, tokens, "claude", title="room again").status_code == 200


def test_default_cap_and_bad_config(tmp_path, agents):
    app, c, tokens = make(tmp_path, agents)
    assert app.state.ws.svc.max_open_asks == 20
    app.state.ws.close()
    (tmp_path / "config.toml").write_text("[inbox]\nmax_open_per_agent = 0\n")
    with pytest.raises(ValueError, match="max_open_per_agent"):
        make(tmp_path, agents)


@pytest.mark.parametrize("field,value", [
    ("title", "x" * 201), ("text", "x" * 2001), ("blocked", "x" * 301), ("cost_of_waiting", "x" * 301),
    ("title", "   "), ("kind", "chat"), ("urgency", "now"), ("project_id", "nope"), ("task_id", "task_nope"),
    ("url", "javascript:alert(1)"), ("due_at", "tomorrow"), ("due_at", "2026-11-11T10:00:00")])
def test_invalid_items_are_refused(ws, field, value):
    app, c, tokens = ws
    r = c.post("/api/asks", headers=H(tokens, "claude"), json={"title": "t", field: value})
    assert r.status_code == 422, r.text
    assert not app.state.ws.store.state.asks


def test_non_text_fields_are_refused(ws):
    app, c, tokens = ws
    for field in ("title", "text", "blocked", "cost_of_waiting"):
        assert c.post("/api/asks", headers=H(tokens, "claude"), json={"title": "t", field: {"a": 1}}).status_code == 422


def test_hostile_text_is_stored_as_plain_text_and_never_executed_server_side(ws):
    app, c, tokens = ws
    r = ask(c, tokens, title=HOSTILE[:200], text=HOSTILE, blocked=HOSTILE[:300], cost_of_waiting=HOSTILE[:300],
            url="https://example.test/\" onmouseover=\"alert(1)")
    assert r.status_code == 200
    got = c.get("/api/inbox", headers=H(tokens)).json()["items"][0]
    assert got["text"] == HOSTILE and got["blocked"] == HOSTILE[:300]
    a = c.post(f"/api/asks/{got['id']}/answer", headers=H(tokens), json={"resolution": "approved", "note": HOSTILE})
    assert a.status_code == 200 and a.json()["note"] == HOSTILE


def test_unknown_token_and_no_token_are_refused(ws):
    app, c, tokens = ws
    assert c.post("/api/asks", json={"title": "t"}).status_code == 401
    assert c.post("/api/asks", headers={"Authorization": "Bearer ws_nope"}, json={"title": "t"}).status_code == 401


def test_submitting_gives_agents_no_owner_only_action(ws):
    """The new capability is only 'ask the owner'. Every owner-only route still refuses an agent token."""
    app, c, tokens = ws
    a = ask(c, tokens).json()["ask"]
    assert all("ask_owner" in AGENTS[x]["capabilities"] for x in ("claude", "gpt", "gemini"))
    for x in ("claude", "gpt", "gemini"):
        h = H(tokens, x)
        assert c.post(f"/api/asks/{a['id']}/answer", headers=h, json={"resolution": "dismissed"}).status_code == 403
        assert c.post("/api/threads", headers=h, json={"title": "t", "project_id": "workstation"}).status_code == 403
    assert app.state.ws.svc.s.asks[a["id"]]["status"] == "open"


def test_asks_survive_a_restart(tmp_path, agents):
    app, c, tokens = make(tmp_path, agents)
    a = ask(c, tokens, kind="question", title="persist me").json()["ask"]
    c.post(f"/api/asks/{a['id']}/answer", headers=H(tokens), json={"resolution": "answered", "note": "ok"}).raise_for_status()
    ask(c, tokens, title="still open").raise_for_status()
    app.state.ws.close()
    app2, c2, tokens2 = make(tmp_path, agents)
    assert titles(c2, tokens2) == ["still open"]
    assert c2.get("/api/inbox", headers=H(tokens2)).json()["answered"][0]["note"] == "ok"


def test_the_cli_submits_lists_answers_and_withdraws(ws, monkeypatch, capsys, tmp_path):
    app, c, tokens = ws

    def client(args, who):
        c.headers.update(H(tokens, who))
        return c
    monkeypatch.setattr(cli, "client", client)
    base = ["--data", str(tmp_path)]

    def run(*argv):
        cli.main(base + list(argv))
        return json.loads(capsys.readouterr().out)

    r = run("ask", "Run the migration?", "--as", "claude", "--kind", "approval", "--blocked", "deploy",
            "--cost", "release slips a day", "--urgency", "high", "--text", "details here", "--key", "cli-1")
    assert r["outcome"] == "created" and r["ask"]["asked_by"] == "claude"
    assert run("ask", "Run the migration?", "--as", "claude", "--kind", "approval", "--blocked", "deploy",
               "--cost", "release slips a day", "--urgency", "high", "--text", "details here",
               "--key", "cli-1")["outcome"] == "duplicate"
    assert run("ask", "Second", "--as", "gpt", "--kind", "review")["ask"]["asked_by"] == "gpt"
    assert [a["title"] for a in run("asks", "--as", "claude")] == ["Run the migration?"]
    assert [i["title"] for i in run("asks", "--inbox")["items"]] == ["Run the migration?", "Second"]
    aid = r["ask"]["id"]
    assert run("answer", aid, "--resolution", "approved", "--note", "go")["resolution"] == "approved"
    assert run("asks", "--as", "claude", "--status", "closed")[0]["note"] == "go"
    other = run("ask", "Third", "--as", "claude")["ask"]["id"]
    assert run("withdraw", other, "--as", "claude")["status"] == "withdrawn"
