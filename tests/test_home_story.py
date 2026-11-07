"""Home tells one story: no tile says 'nothing' while another shows something waiting."""
from conftest import H

Q = {"question": "Cache the home view?", "options": [{"label": "a"}, {"label": "b"}], "project_id": "workstation"}


def home(c, tokens):
    return c.get("/api/home", headers=H(tokens)).json()


def test_next_action_is_empty_only_when_nothing_waits(ws):
    _, c, tokens = ws
    assert home(c, tokens)["next"] is None


def test_next_action_names_the_pending_decision_first(ws):
    _, c, tokens = ws
    c.post("/api/tasks", headers=H(tokens), json={"title": "Review draft", "project_id": "workstation", "owner": "owner",
                                                  "status": "ready"})
    c.post("/api/decisions", headers=H(tokens), json=Q)
    h = home(c, tokens)
    assert h["next"]["kind"] == "decision" and h["next"]["text"] == "Cache the home view?"
    assert h["next"]["id"] == h["decisions"][0]["id"]
    assert h["actions"], "the owner task is still listed below"


def test_next_action_falls_back_to_the_owner_task(ws):
    _, c, tokens = ws
    c.post("/api/tasks", headers=H(tokens), json={"title": "Review draft", "project_id": "workstation", "owner": "owner",
                                                  "status": "ready"})
    n = home(c, tokens)["next"]
    assert n["kind"] == "task" and n["id"] == home(c, tokens)["actions"][0]["id"]


def test_next_action_clears_once_the_decision_is_resolved(ws):
    _, c, tokens = ws
    d = c.post("/api/decisions", headers=H(tokens), json=Q).json()
    c.post(f"/api/decisions/{d['id']}/resolve", headers=H(tokens), json={"status": "rejected"})
    assert home(c, tokens)["next"] is None


def test_coverage_follows_live_agent_status(ws):
    app, c, tokens = ws
    cov = {x["source"]: x["state"] for x in home(c, tokens)["coverage"]}
    claude = next(v for k, v in cov.items() if k.startswith("Claude"))
    gemini = next(v for k, v in cov.items() if k.startswith("Gemini"))
    assert claude == "connected" and gemini.startswith("not connected")
    # a Claude CLI that goes away must not still read "connected"
    from workstation import views
    down = [{"id": "claude", "available": False, "reason": "not signed in", "pull_fallback": True},
            {"id": "gpt", "available": True, "reason": "", "pull_fallback": False}]
    cov = {x["source"]: x["state"] for x in views.home(app.state.ws.svc, down)["coverage"]}
    assert next(v for k, v in cov.items() if k.startswith("Claude")) == "not signed in · session pickup"
    assert next(v for k, v in cov.items() if k.startswith("GPT")) == "connected"
