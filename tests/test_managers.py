"""The manager view: conversations with the top-level agents; lower-level work folded under them."""
import pytest

from conftest import H, make


def add(c, tokens, name, model):
    r = c.post("/api/agents", headers=H(tokens), json={"id": name, "model": model})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + (c.app.state.ws.data / "secrets" / f"{name}.token").read_text()}


@pytest.fixture
def org(tmp_path, agents):
    (tmp_path / "config.toml").write_text('[agents.claude]\nmodel = "claude-fable-5-2"\n[agents.gpt]\nmodel = "gpt-6-astra"\n')
    app, c, tokens = make(tmp_path, agents)
    app.state.ws.svc.upsert_project("owner", "workstation", "Workstation build")
    h = {"low": add(c, tokens, "scout", "claude-haiku-5-5"), "mid": add(c, tokens, "writer", "claude-opus-5-5")}
    yield app, c, tokens, h
    app.state.ws.close()


def mk(c, headers, owner, title, status="ready"):
    r = c.post("/api/tasks", headers=headers, json={"title": title, "project_id": "workstation", "owner": owner, "status": status})
    assert r.status_code == 200, r.text
    return r.json()["task"]


def view(c, tokens):
    return c.get("/api/managers", headers=H(tokens)).json()


def test_managers_are_the_agents_at_the_highest_level(org):
    app, c, tokens, h = org
    v = view(c, tokens)
    assert v["manager_level"] == 3 and [m["id"] for m in v["managers"]] == ["claude", "gpt"]
    assert {a["id"]: a["level"] for a in v["agents"]} == {"claude": 3, "gpt": 3, "gemini": 2, "scout": 1, "writer": 2}
    assert not any(m["id"] in ("scout", "writer", "gemini") for m in v["managers"])


def test_default_config_has_one_manager_until_the_claude_model_is_set(tmp_path, agents):
    app, c, tokens = make(tmp_path, agents)
    assert [m["id"] for m in view(c, tokens)["managers"]] == ["gpt"]
    app.state.ws.close()


def test_lower_level_work_is_a_count_and_a_line_under_the_manager_that_assigned_it(org):
    app, c, tokens, h = org
    mk(c, H(tokens, "claude"), "scout", "Collect logs", "in_progress")
    mk(c, H(tokens, "claude"), "scout", "Tag logs")
    mk(c, H(tokens, "claude"), "writer", "Draft summary")
    mk(c, H(tokens), "scout", "Owner's own errand")        # assigned by the owner: no manager
    mk(c, H(tokens, "claude"), "claude", "Manager's own")  # a manager's own task is not lower-level work
    v = view(c, tokens)
    claude = next(m for m in v["managers"] if m["id"] == "claude")
    assert claude["lower"]["count"] == 3 and claude["lower"]["line"] == "3 open tasks: 1 working, 2 queued"
    assert [(a["id"], a["count"]) for a in claude["lower"]["agents"]] == [("scout", 2), ("writer", 1)]
    assert claude["lower"]["agents"][0]["tasks"][0]["title"] == "Collect logs"  # working first
    assert claude["own_open_tasks"] == 1
    gpt = next(m for m in v["managers"] if m["id"] == "gpt")
    assert gpt["lower"]["count"] == 0 and gpt["lower"]["line"] == "Nothing open"
    assert v["other"]["count"] == 1 and v["other"]["agents"][0]["tasks"][0]["title"] == "Owner's own errand"


def test_finished_work_leaves_the_view_and_long_lists_are_cut(org):
    app, c, tokens, h = org
    for i in range(7):
        mk(c, H(tokens, "claude"), "scout", f"Job {i}")
    done = mk(c, H(tokens, "claude"), "scout", "Finished")
    c.patch(f"/api/tasks/{done['id']}", headers=H(tokens), json={"changes": {"status": "in_progress"}})
    c.patch(f"/api/tasks/{done['id']}", headers=H(tokens), json={"changes": {"status": "done"}}).raise_for_status()
    a = next(m for m in view(c, tokens)["managers"] if m["id"] == "claude")["lower"]["agents"][0]
    assert a["count"] == 7 and len(a["tasks"]) == 5 and a["more"] == 2


def test_conversations_with_a_manager_are_the_owners_threads_that_address_it(org):
    app, c, tokens, h = org
    t = c.post("/api/threads", headers=H(tokens), json={"title": "Plan the release", "project_id": "workstation"}).json()
    c.post(f"/api/threads/{t['id']}/messages", headers=H(tokens), json={"text": "@gpt what do you think?"}).raise_for_status()
    other = c.post("/api/threads", headers=H(tokens), json={"title": "Unrelated", "project_id": "workstation"}).json()
    c.post(f"/api/threads/{other['id']}/messages", headers=H(tokens), json={"text": "@claude hello"}).raise_for_status()
    v = view(c, tokens)
    gpt = next(m for m in v["managers"] if m["id"] == "gpt")
    claude = next(m for m in v["managers"] if m["id"] == "claude")
    assert [x["title"] for x in gpt["conversations"]] == ["Plan the release"]
    assert [x["title"] for x in claude["conversations"]] == ["Unrelated"]
    assert gpt["conversations"][0]["last"]["text"]  # the last non-notice message is shown


def test_waiting_on_you_counts_per_manager_and_the_view_is_owner_only(org):
    app, c, tokens, h = org
    c.post("/api/asks", headers=H(tokens, "gpt"), json={"title": "Approve?", "kind": "approval"}).raise_for_status()
    assert next(m for m in view(c, tokens)["managers"] if m["id"] == "gpt")["waiting_on_owner"] == 1
    for who in ("claude", "gpt"):
        assert c.get("/api/managers", headers=H(tokens, who)).status_code == 403
    assert c.get("/api/managers").status_code == 401


def test_the_owner_can_still_open_any_agents_work_directly(org):
    app, c, tokens, h = org
    t = mk(c, H(tokens, "claude"), "scout", "Collect logs")
    assert c.get(f"/api/tasks/{t['id']}", headers=H(tokens)).json()["owner"] == "scout"
    assert [x["owner"] for x in c.get("/api/tasks?owner_id=scout", headers=H(tokens)).json()] == ["scout"]
