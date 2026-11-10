"""Agent kinds the owner adds (codex, claude-code, muse, ...): names, tokens, instructions, owner-only limits, restart."""
import json

import pytest

from conftest import H, make, wait
from workstation import cli, principals
from workstation.principals import AGENTS

HOSTILE = "<script>alert(1)</script> <img src=x onerror=alert(2)> [x](javascript:alert(3)) \" onmouseover=\"alert(4)"


def add(c, tokens, name="muse", who="owner", **kw):
    return c.post("/api/agents", headers=H(tokens, who), json={"id": name, **kw})


def as_agent(c, tokens, name):
    """The token the server wrote for `name`, usable as a header."""
    return {"Authorization": "Bearer " + (c.app.state.ws.data / "secrets" / f"{name}.token").read_text()}


def test_the_owner_adds_an_agent_and_gets_a_token_and_instructions(ws):
    app, c, tokens = ws
    r = add(c, tokens, "muse", display="Muse")
    assert r.status_code == 200 and r.json()["has_token"] is True
    text = r.json()["instructions"]
    assert "--as muse" in text and "AGENT_ID" not in text and "WORKSTATION_DIR" not in text
    assert (app.state.ws.data / "secrets" / "muse.token").exists()
    assert "ws_" not in json.dumps(r.json())  # the token value is never returned
    me = c.get("/api/me", headers=as_agent(c, tokens, "muse")).json()
    assert me["id"] == "muse" and me["display"] == "Muse" and me["kind"] == "agent"
    setup = c.get("/api/setup", headers=H(tokens)).json()
    row = next(a for a in setup["agents"] if a["id"] == "muse")
    assert row["custom"] and row["has_token"] and "--as muse" in row["instructions"]
    assert {"claude", "gpt"} <= {a["id"] for a in setup["agents"]}
    home = c.get("/api/home", headers=H(tokens)).json()
    assert any(a["id"] == "muse" and a["runtime"] == "board-pickup" for a in home["agents"])


@pytest.mark.parametrize("name", ["Muse", "1muse", "mu se", "mu_se", "x" * 33, "", "claude", "gpt", "gemini", "system", "owner",
                                  "../etc", "muse\n", "<b>"])
def test_names_are_validated_and_built_ins_cannot_be_replaced(ws, name):
    app, c, tokens = ws
    assert add(c, tokens, name).status_code == 422
    assert not any(a.get("custom") for a in AGENTS.values())


def test_a_name_can_be_added_only_once_and_display_and_role_are_checked(ws):
    app, c, tokens = ws
    assert add(c, tokens, "codex").status_code == 200
    assert add(c, tokens, "codex").status_code == 422
    assert add(c, tokens, "a", display="x" * 41).status_code == 422
    assert add(c, tokens, "b", display="bad\x1b[2Jname").status_code == 422
    assert add(c, tokens, "c", role="r" * 201).status_code == 422
    assert add(c, tokens, "claude-code").status_code == 200  # hyphens are fine


def test_a_custom_agent_submits_an_item_and_reads_its_own_state(ws):
    app, c, tokens = ws
    add(c, tokens, "muse", display="Muse")
    h = as_agent(c, tokens, "muse")
    r = c.post("/api/asks", headers=h, json={"title": "Which branch?", "kind": "question", "idempotency_key": "k"})
    assert r.status_code == 200 and r.json()["ask"]["asked_by"] == "muse"
    mine = c.get("/api/needs-now", headers=h).json()
    assert mine["for"] == "muse" and mine["mine"][0]["title"] == "Which branch?" and mine["mine"][0]["state"] == "waiting"
    ask_id = r.json()["ask"]["id"]
    c.post(f"/api/asks/{ask_id}/answer", headers=H(tokens), json={"resolution": "answered", "note": "main"}).raise_for_status()
    done = c.get("/api/asks?status=closed", headers=h).json()
    assert done[0]["note"] == "main"
    inbox = c.get("/api/inbox", headers=H(tokens)).json()
    assert inbox["items"] == []


def test_a_custom_agent_works_on_a_task_assigned_to_it_and_cannot_close_it(ws):
    app, c, tokens = ws
    add(c, tokens, "muse")
    task = c.post("/api/tasks", headers=H(tokens), json={"title": "Draft", "project_id": "workstation", "owner": "muse",
                                                         "status": "ready"}).json()["task"]
    h = as_agent(c, tokens, "muse")
    ok = c.patch(f"/api/tasks/{task['id']}", headers=h, json={"changes": {"status": "in_progress"}})
    assert ok.status_code == 200
    assert c.post(f"/api/tasks/{task['id']}/artifacts", headers=h,
                  json={"kind": "doc", "title": "notes", "url": "https://example.com/n"}).status_code == 200
    assert c.patch(f"/api/tasks/{task['id']}", headers=h, json={"changes": {"status": "needs_review"}}).status_code == 200
    assert c.patch(f"/api/tasks/{task['id']}", headers=h, json={"changes": {"status": "done"}}).status_code == 403
    assert c.patch(f"/api/tasks/{task['id']}", headers=h, json={"changes": {"owner": "claude"}}).status_code == 403


def test_owner_only_actions_return_403_to_a_custom_agent_and_are_logged(ws):
    app, c, tokens = ws
    add(c, tokens, "muse")
    h = as_agent(c, tokens, "muse")
    a = c.post("/api/asks", headers=h, json={"title": "t", "kind": "question"}).json()["ask"]
    task = c.post("/api/tasks", headers=H(tokens), json={"title": "T", "project_id": "workstation", "owner": "claude"}).json()["task"]
    dec = c.post("/api/decisions", headers=h, json={"question": "q", "project_id": "workstation",
                                                    "options": [{"label": "a"}, {"label": "b"}]}).json()
    calls = [("post", f"/api/asks/{a['id']}/answer", {"resolution": "answered"}),
             ("post", "/api/agents", {"id": "evil"}),
             ("post", "/api/setup/token/claude", None),
             ("get", "/api/setup", None),
             ("post", "/api/projects", {"id": "p", "name": "p"}),
             ("post", "/api/threads", {"title": "x", "project_id": "workstation"}),
             ("post", "/api/snooze", {"key": f"ask:{a['id']}", "minutes": 5}),
             ("post", "/api/reminders/pause", {"minutes": 5}),
             ("post", "/api/login-codes", None),
             ("get", "/api/tasks", None),
             ("get", "/api/inbox", None),
             ("post", f"/api/decisions/{dec['id']}/resolve", {"status": "approved", "option_id": dec["options"][0]["id"]}),
             ("patch", f"/api/tasks/{task['id']}", {"changes": {"status": "done"}})]
    for method, url, body in calls:
        r = getattr(c, method)(url, headers=h, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, (method, url, r.status_code)
    assert not (set(AGENTS) & {"evil"})
    refused = [e for e in app.state.ws.store.state.events if e["event_type"] == "action_refused" and e["actor"] == "muse"]
    assert refused


def test_a_mention_of_a_custom_agent_waits_for_it_to_pick_up(ws):
    app, c, tokens = ws
    add(c, tokens, "muse")
    th = c.post("/api/threads", headers=H(tokens), json={"title": "t", "project_id": "workstation"}).json()
    c.post(f"/api/threads/{th['id']}/messages", headers=H(tokens), json={"text": "@muse what do you think?"}).raise_for_status()
    h = as_agent(c, tokens, "muse")
    wait(lambda: c.get("/api/dispatches", headers=h).json())
    d = c.get("/api/dispatches", headers=h).json()[0]
    assert d["status"] == "queued" and not d["error"]  # not failed as "offline"
    claimed = c.post(f"/api/dispatches/{d['id']}/claim", headers=h).json()
    r = c.post(f"/api/dispatches/{d['id']}/reply", headers=h, json={"text": "Use the board.", "attempt": claimed["attempt"]})
    assert r.status_code == 200
    msgs = c.get(f"/api/threads/{th['id']}", headers=H(tokens)).json()["messages"]
    assert any(m["author"] == "muse" and "Use the board." in m["text"] for m in msgs)


def test_hostile_names_are_stored_as_plain_text_and_served_as_data(ws):
    app, c, tokens = ws
    plain = "".join(ch for ch in HOSTILE if ch.isprintable())
    assert add(c, tokens, "muse", display=plain[:40], role=plain).status_code == 200
    p = c.get("/api/principals", headers=H(tokens)).json()["principals"]["muse"]
    assert p["display"] == plain[:40] and p["role"] == plain
    assert c.get("/", headers=H(tokens)).headers["content-type"].startswith("text/html")


def test_added_agents_survive_a_restart_and_a_hand_edited_file_cannot_take_a_reserved_name(tmp_path, agents):
    app, c, tokens = make(tmp_path, agents)
    add(c, tokens, "muse", display="Muse")
    app.state.ws.close()
    (tmp_path / "agents.json").write_text(json.dumps({"muse": {"display": "Muse", "role": "r"},
                                                      "claude": {"display": "Imposter", "role": "r"},
                                                      "bad name": {"display": "x", "role": "r"}, "junk": 5}))
    app2, c2, tokens2 = make(tmp_path, agents)
    assert AGENTS["muse"]["display"] == "Muse" and AGENTS["claude"]["display"] == "Claude"
    assert "bad name" not in AGENTS and "junk" not in AGENTS
    app2.state.ws.close()


def test_review_lists_added_agents(ws):
    app, c, tokens = ws
    add(c, tokens, "muse", display="Muse")
    rev = c.get("/api/review", headers=H(tokens)).json()
    assert "muse" in [a["id"] for a in rev["agents"]]


def test_the_cli_adds_an_agent_and_a_running_server_picks_it_up(ws, capsys, monkeypatch):
    app, c, tokens = ws
    data = app.state.ws.data
    cli.main(["--data", str(data), "agent", "add", "codex", "--display", "Codex"])
    out = capsys.readouterr().out
    assert "added codex" in out and "--as codex" in out and "ws_" not in out  # instructions, never the token
    assert (data / "secrets" / "codex.token").exists()
    # the running server learns of it from agents.json, with no restart
    me = c.get("/api/me", headers=as_agent(c, tokens, "codex"))
    assert me.status_code == 200 and me.json()["display"] == "Codex"
    cli.main(["--data", str(data), "agent", "list"])
    assert "codex\tCodex\tadded" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["--data", str(data), "agent", "add", "claude"])


def test_the_instructions_template_has_no_unfilled_placeholders():
    from workstation.app import agent_instructions
    import tempfile
    text = agent_instructions(tempfile.gettempdir(), "muse")
    assert text and "AGENT_ID" not in text and "WORKSTATION_DIR" not in text and "--as muse" in text
