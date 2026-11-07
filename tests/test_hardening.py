"""Idempotency by content, owner-only routes, and browser-level write checks."""
import pytest

from conftest import H

Q = {"question": "q", "options": [{"label": "a"}, {"label": "b"}], "project_id": "workstation"}
AGENTS = ("claude", "gpt", "gemini")


def test_a_key_reused_with_a_different_body_is_refused(ws):
    app, c, tokens = ws
    h = H(tokens, "claude")
    task = {"title": "a", "project_id": "workstation", "owner": "claude", "idempotency_key": "k1"}
    assert c.post("/api/tasks", headers=h, json=task).json()["outcome"] == "created"
    assert c.post("/api/tasks", headers=h, json=task).json()["outcome"] == "duplicate"
    assert c.post("/api/tasks", headers=h, json={**task, "title": "changed"}).status_code == 409
    assert c.post("/api/tasks", headers=h, json={**task, "next_action": "other"}).status_code == 409
    assert len([t for t in app.state.ws.store.state.tasks.values()]) == 1

    tid = next(iter(app.state.ws.store.state.tasks))
    art = {"kind": "doc", "title": "t", "url": "https://example.com/a", "idempotency_key": "k2"}
    assert c.post(f"/api/tasks/{tid}/artifacts", headers=h, json=art).status_code == 200
    assert c.post(f"/api/tasks/{tid}/artifacts", headers=h, json=art).status_code == 200
    assert c.post(f"/api/tasks/{tid}/artifacts", headers=h, json={**art, "title": "other"}).status_code == 409

    patch = {"idempotency_key": "k3", "changes": {"status": "ready"}}
    assert c.patch(f"/api/tasks/{tid}", headers=h, json=patch).status_code == 200
    assert c.patch(f"/api/tasks/{tid}", headers=h, json={**patch, "changes": {"status": "in_progress"}}).status_code == 409

    q = {**Q, "idempotency_key": "k4"}
    assert c.post("/api/decisions", headers=H(tokens, "gpt"), json=q).status_code == 200
    assert c.post("/api/decisions", headers=H(tokens, "gpt"), json=q).status_code == 200
    assert c.post("/api/decisions", headers=H(tokens, "gpt"), json={**q, "question": "other"}).status_code == 409

    th = c.post("/api/threads", headers=H(tokens), json={"title": "t", "project_id": "workstation"}).json()
    msg = {"text": "hello", "idempotency_key": "k5"}
    assert c.post(f"/api/threads/{th['id']}/messages", headers=H(tokens), json=msg).status_code == 200
    assert c.post(f"/api/threads/{th['id']}/messages", headers=H(tokens), json=msg).status_code == 200
    assert c.post(f"/api/threads/{th['id']}/messages", headers=H(tokens), json={**msg, "text": "bye"}).status_code == 409


def test_event_intake_refuses_a_changed_body_under_the_same_key(ws):
    app, c, tokens = ws
    ev = {"event_type": "task_created", "idempotency_key": "e1", "project_id": "workstation",
          "payload": {"title": "From mail"}}
    assert c.post("/api/events", headers=H(tokens, "gemini"), json=ev).json()["outcome"] == "created"
    assert c.post("/api/events", headers=H(tokens, "gemini"), json=ev).json()["outcome"] == "duplicate"
    changed = {**ev, "payload": {"title": "Different"}}
    assert c.post("/api/events", headers=H(tokens, "gemini"), json=changed).status_code == 409


@pytest.fixture
def pending(ws):
    """A pending decision, a task in needs_review with evidence, and a canceled task."""
    app, c, tokens = ws
    d = c.post("/api/decisions", headers=H(tokens), json=Q).json()
    mk = lambda **kw: c.post("/api/tasks", headers=H(tokens), json={"title": "x", "project_id": "workstation",
                                                                     "owner": "claude", **kw}).json()["task"]
    review = mk(status="in_progress")
    c.post(f"/api/tasks/{review['id']}/artifacts", headers=H(tokens, "claude"),
           json={"kind": "doc", "title": "t", "url": "https://example.com/a"})
    c.patch(f"/api/tasks/{review['id']}", headers=H(tokens, "claude"), json={"changes": {"status": "needs_review"}})
    gone = mk()
    c.patch(f"/api/tasks/{gone['id']}", headers=H(tokens), json={"changes": {"status": "canceled"}})
    return d["id"], review["id"], gone["id"]


@pytest.mark.parametrize("agent", AGENTS)
def test_no_agent_token_resolves_a_decision_or_accepts_a_task_by_any_path(ws, pending, agent):
    app, c, tokens = ws
    did, review, gone = pending
    h = H(tokens, agent)
    state = app.state.ws.store.state
    n = len(state.events)
    attempts = [
        c.post(f"/api/decisions/{did}/resolve", headers=h, json={"status": "approved", "option_id": "o1"}),
        c.post(f"/api/decisions/{did}/resolve", headers=h, json={"status": "rejected"}),
        c.post("/api/events", headers=h, json={"event_type": "decision_resolved", "idempotency_key": f"r-{agent}",
                                               "project_id": "workstation",
                                               "payload": {"id": did, "status": "approved", "option_id": "o1"}}),
        c.patch(f"/api/tasks/{review}", headers=h, json={"changes": {"status": "done"}}),
        c.patch(f"/api/tasks/{review}", headers=h, json={"changes": {"status": "canceled"}}),
        c.patch(f"/api/tasks/{review}", headers=h, json={"changes": {"owner": agent}}),
        c.patch(f"/api/tasks/{gone}", headers=h, json={"changes": {"status": "ready"}}),
        c.post("/api/events", headers=h, json={"event_type": "task_completed", "idempotency_key": f"c-{agent}",
                                               "project_id": "workstation", "payload": {"id": review}}),
        c.post("/api/events", headers=h, json={"event_type": "task_updated", "idempotency_key": f"u-{agent}",
                                               "project_id": "workstation",
                                               "payload": {"id": review, "status": "done"}}),
    ]
    assert [r.status_code for r in attempts] == [403] * len(attempts), [r.text for r in attempts]
    assert state.decisions[did]["status"] == "pending"
    assert state.tasks[review]["status"] == "needs_review" and state.tasks[gone]["status"] == "canceled"
    assert len(state.events) == n


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/api/projects", {"id": "p", "name": "p"}),
    ("POST", "/api/threads", {"title": "t", "project_id": "workstation"}),
    ("POST", "/api/threads/thr_x/critique", None),
    ("GET", "/api/threads/thr_x/decision-draft", None),
    ("POST", "/api/agents/claude/probe", None),
    ("POST", "/api/login-codes", None),
    ("GET", "/api/home", None),
])
@pytest.mark.parametrize("agent", AGENTS)
def test_owner_only_routes_refuse_agent_tokens(ws, method, path, body, agent):
    app, c, tokens = ws
    r = c.request(method, path, headers=H(tokens, agent), json=body)
    assert r.status_code == 403


def test_cookie_writes_need_the_header_and_same_origin_on_every_write_route(ws, pending):
    app, c, tokens = ws
    did, review, gone = pending
    c.cookies.set("ws_session", tokens["owner"])
    writes = [
        ("POST", "/api/projects", {"id": "p", "name": "p"}),
        ("POST", "/api/threads", {"title": "t", "project_id": "workstation"}),
        ("POST", "/api/threads/thr_x/messages", {"text": "hi"}),
        ("POST", "/api/threads/thr_x/critique", None),
        ("POST", "/api/decisions", Q),
        ("POST", f"/api/decisions/{did}/resolve", {"status": "approved", "option_id": "o1"}),
        ("POST", "/api/tasks", {"title": "x", "project_id": "workstation"}),
        ("PATCH", f"/api/tasks/{review}", {"changes": {"status": "done"}}),
        ("POST", f"/api/tasks/{review}/artifacts", {"kind": "doc", "title": "t", "url": "https://example.com/b"}),
        ("POST", f"/api/tasks/{review}/reviews", {"verdict": "approve", "summary": "s"}),
        ("POST", "/api/events", {"event_type": "task_created", "idempotency_key": "x", "project_id": "workstation",
                                 "payload": {"title": "t"}}),
        ("POST", "/api/login-codes", None),
        ("POST", "/api/agents/claude/probe", None),
    ]
    n = len(app.state.ws.store.state.events)
    for method, path, body in writes:
        for headers in ({}, {"X-Workstation": "1", "Origin": "https://evil.example"},
                        {"X-Workstation": "1", "Origin": "null"}, {"Origin": "http://testserver"}):
            r = c.request(method, path, headers=headers, json=body)
            assert r.status_code == 403, (method, path, headers, r.status_code)
    assert len(app.state.ws.store.state.events) == n


def test_the_owner_token_is_never_in_a_page_or_response_an_agent_can_fetch(ws, pending):
    app, c, tokens = ws
    secret = tokens["owner"].strip()
    for agent in AGENTS:
        for path in ("/", "/login", "/api/version", "/api/principals", "/api/dispatches", "/api/tasks",
                     "/api/decisions", "/api/activity", "/api/projects"):
            r = c.get(path, headers=H(tokens, agent))
            assert secret not in r.text and secret not in str(r.headers), (agent, path)
    page = c.get("/").text
    assert secret not in page and "owner.token" not in page
    r = c.get("/login?code=nope")
    assert r.status_code == 401 and "set-cookie" not in r.headers


def test_decision_draft_merges_near_duplicate_options_and_names_both_sources(ws, agents):
    app, c, tokens = ws
    agents["claude"].options = ["Build a local core", "Fork the UI", "Wait"]
    agents["gpt"].options = ["Build the local core.", "Use SQLite only", "Hosted service"]
    th = c.post("/api/threads", headers=H(tokens), json={"title": "t", "project_id": "workstation"}).json()
    c.post(f"/api/threads/{th['id']}/messages", headers=H(tokens), json={"text": "@claude @gpt which way?"})
    from conftest import wait
    wait(lambda: sum(1 for m in app.state.ws.store.state.messages[th["id"]] if m["author"] in ("claude", "gpt")) == 2)
    draft = c.get(f"/api/threads/{th['id']}/decision-draft", headers=H(tokens)).json()
    labels = [o["label"] for o in draft["options"]]
    assert labels == ["Build a local core", "Fork the UI", "Wait", "Use SQLite only", "Hosted service"]
    merged = draft["options"][0]
    assert {s["agent"] for s in merged["sources"]} == {"claude", "gpt"}
    assert merged["detail"].startswith("Proposed by Claude and GPT") and "Build the local core." in merged["detail"]
    assert [s["agent"] for s in draft["options"][1]["sources"]] == ["claude"]


def test_option_merge_rule():
    from workstation.service import option_tokens as t, same_option as same
    assert same(t("Local core"), t("the local cores"))
    assert same(t("Use SQLite"), t("SQLite"))
    assert not same(t("Local core"), t("Local core with a hosted sync service"))
    assert not same(t("Fork the UI"), t("Fork the backend"))
    assert not same(t(""), t(""))
