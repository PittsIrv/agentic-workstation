"""Levels derived from the model through an editable table, and the rules they enforce."""
import json
from pathlib import Path

import pytest

from conftest import H, make
from workstation import cli, config as configuration
from workstation.levels import Levels
from workstation.principals import AGENTS


def agent_headers(c, name):
    return {"Authorization": "Bearer " + (c.app.state.ws.data / "secrets" / f"{name}.token").read_text()}


def add(c, tokens, name, model=None, **kw):
    r = c.post("/api/agents", headers=H(tokens), json={"id": name, "model": model, **kw})
    assert r.status_code == 200, r.text
    return agent_headers(c, name)


@pytest.fixture
def team(tmp_path, agents):
    """Two agents on different top models (same level), one on a middle model, one on a small model."""
    app, c, tokens = make(tmp_path, agents)
    app.state.ws.svc.upsert_project("owner", "workstation", "Workstation build")
    h = {"codex": add(c, tokens, "codex", "gpt-6-astra"), "cc": add(c, tokens, "cc", "Claude Fable 5.2"),
         "mid": add(c, tokens, "mid", "claude-opus-5-5-20261101"), "low": add(c, tokens, "low", "claude-haiku-5-5")}
    return app, c, tokens, h


def task(c, h, who, owner, **kw):
    return c.post("/api/tasks", headers=h[who], json={"title": "Do it", "project_id": "workstation", "owner": owner,
                                                      "status": "ready", **kw})


# ---- the table ------------------------------------------------------------------------------------------------
def test_level_comes_from_the_model_through_the_table():
    lv = Levels()
    assert lv.of_model("gpt-6-astra") == lv.of_model("Claude Fable 5.2") == lv.of_model("claude-fable-5-2") == 3
    assert lv.of_model("claude-opus-5-5-20261101") == 2          # longest prefix at a "-" boundary
    assert lv.of_model("gpt-6-astra-high") == 3 and lv.of_model("gpt-6") == 2
    assert lv.of_model("claude-haiku-5-5") == 1
    assert lv.of_model("some-model-nobody-listed") == lv.default == 1  # unknown models: the default level
    assert lv.of_model("") == lv.of_model(None) == 1
    assert lv.of_model("gpt-6x") == 1                            # a prefix must end at a boundary


def test_the_table_is_editable_config_not_logic(tmp_path):
    (tmp_path / "config.toml").write_text('[levels]\ndefault = 2\n[levels.models]\n"my-local-model" = 5\n"gpt-6-astra" = 1\n')
    cfg = configuration.load(tmp_path)
    lv = Levels(cfg.levels_models, cfg.levels_default)
    assert lv.of_model("my-local-model-q4") == 5 and lv.of_model("gpt-6-astra") == 1  # yours win over the shipped entries
    assert lv.of_model("claude-fable-5-2") == 3                                       # untouched entries stay
    assert lv.of_model("unlisted") == 2                                               # your default for unknown models
    for bad in ('[levels]\ndefault = 0\n', '[levels.models]\n"x" = "high"\n', '[levels.models]\n"x" = 100\n'):
        (tmp_path / "config.toml").write_text(bad)
        with pytest.raises(ValueError):
            configuration.load(tmp_path)


def test_shipped_defaults_are_documented_in_the_example_config():
    text = (Path(__file__).parent.parent / "config.example.toml").read_text()
    assert "[levels]" in text and "unlisted" in text


def test_levels_follow_the_model_and_cannot_be_set_per_agent(team):
    app, c, tokens, h = team
    got = c.get("/api/levels", headers=H(tokens)).json()["agents"]
    assert (got["codex"]["level"], got["cc"]["level"], got["mid"]["level"], got["low"]["level"]) == (3, 3, 2, 1)
    r = c.post("/api/agents", headers=H(tokens), json={"id": "pushy", "model": "claude-haiku-5-5", "level": 9})
    assert r.status_code == 200
    assert c.get("/api/levels", headers=H(tokens)).json()["agents"]["pushy"]["level"] == 1  # a "level" field is ignored
    # changing the model changes the level; an agent cannot change anyone's model
    assert c.post("/api/agents/low/model", headers=h["low"], json={"model": "gpt-6-astra"}).status_code == 403
    assert c.post("/api/agents/low/model", headers=H(tokens), json={"model": "gpt-6-astra"}).json()["level"] == 3
    assert c.get("/api/me", headers=h["low"]).json()["level"] == 3
    assert c.post("/api/agents/claude/model", headers=H(tokens), json={"model": "x"}).status_code == 422  # built-in: from config


def test_built_in_agents_take_their_model_from_config(tmp_path, agents):
    (tmp_path / "config.toml").write_text('[agents.claude]\nmodel = "claude-fable-5-2"\n[agents.gpt]\nmodel = "gpt-6-astra"\n')
    app, c, tokens = make(tmp_path, agents)
    lv = c.get("/api/levels", headers=H(tokens)).json()["agents"]
    assert lv["claude"] == {"model": "claude-fable-5-2", "level": 3} and lv["gpt"]["level"] == 3
    app.state.ws.close()


# ---- assignment -----------------------------------------------------------------------------------------------
def test_same_level_agents_on_different_top_models_are_peers(team):
    app, c, tokens, h = team
    assert task(c, h, "codex", "cc").status_code == 200
    assert task(c, h, "cc", "codex").status_code == 200


def test_a_higher_level_assigns_down_and_a_lower_level_cannot_assign_up(team):
    app, c, tokens, h = team
    assert task(c, h, "codex", "low").status_code == 200 and task(c, h, "codex", "mid").status_code == 200
    assert task(c, h, "mid", "low").status_code == 200
    n = len(app.state.ws.store.state.tasks)
    for who, target in (("low", "mid"), ("low", "codex"), ("mid", "cc")):
        r = task(c, h, who, target)
        assert r.status_code == 403 and "upward" in r.json()["error"], (who, target, r.text)
    ev = c.post("/api/events", headers=h["low"], json={"event_type": "task_created", "project_id": "workstation",
                                                       "idempotency_key": "up-1", "payload": {"title": "Up", "owner": "codex", "status": "ready"}})
    assert ev.status_code == 403
    assert len(app.state.ws.store.state.tasks) == n
    assert task(c, h, "low", "low").status_code == 200                       # a task for itself is fine
    assert task(c, h, "low", "owner").status_code == 200                      # and asking the owner is not "upward"


def test_levels_open_no_owner_only_action_to_a_higher_agent(team):
    app, c, tokens, h = team
    tid = task(c, h, "codex", "low").json()["task"]["id"]
    for changes in ({"status": "canceled"}, {"status": "done"}, {"owner": "cc"}):
        assert c.patch(f"/api/tasks/{tid}", headers=h["codex"], json={"changes": changes}).status_code == 403
    assert c.post(f"/api/asks/ask_x/answer", headers=h["cc"], json={"resolution": "answered"}).status_code == 403


# ---- discussions ------------------------------------------------------------------------------------------------
def disagree(c, h, a, b):
    did = c.post("/api/discussions", headers=h[a], json={"title": "Which store?", "text": "SQLite", "stance": "agree"}).json()["discussion"]["id"]
    c.post(f"/api/discussions/{did}/posts", headers=h[b], json={"text": "Files", "stance": "disagree"}).raise_for_status()
    return did


def close(c, h, did, who, outcome="overruled"):
    return c.post(f"/api/discussions/{did}/close", headers=h[who], json={"outcome": outcome, "note": "n"})


def test_a_higher_level_overrules_a_lower_one_and_never_the_reverse(team):
    app, c, tokens, h = team
    did = disagree(c, h, "low", "codex")
    assert close(c, h, did, "low").status_code == 403
    r = close(c, h, did, "codex")
    assert r.status_code == 200 and r.json()["outcome"] == "overruled" and r.json()["closed_by"] == "codex"


def test_peers_cannot_overrule_each_other(team):
    app, c, tokens, h = team
    did = disagree(c, h, "cc", "codex")
    for who in ("cc", "codex"):
        assert close(c, h, did, who).status_code == 403
    # a lower agent joining does not give either peer a win: the other peer is still a participant at the same level
    c.post(f"/api/discussions/{did}/posts", headers=h["low"], json={"text": "me too"}).raise_for_status()
    assert close(c, h, did, "codex").status_code == 403
    assert c.post(f"/api/discussions/{did}/close", headers=H(tokens), json={"outcome": "overruled"}).status_code == 200  # the owner may


def test_an_unsettled_same_level_disagreement_becomes_a_user_inbox_item(team):
    app, c, tokens, h = team
    did = disagree(c, h, "cc", "codex")
    assert close(c, h, did, "cc", "agreed").status_code == 409
    r = close(c, h, did, "cc", "sent_to_user")
    assert r.status_code == 200 and r.json()["outcome"] == "sent_to_user"
    items = c.get("/api/inbox", headers=H(tokens)).json()["items"]
    item = next(i for i in items if i["id"] == r.json()["ask_id"])
    assert item["discussion_id"] == did and item["asked_by"] == "cc" and "Codex: disagree" in item["text"]


# ---- CLI ------------------------------------------------------------------------------------------------------
def test_the_cli_adds_an_agent_with_a_model_lists_levels_and_changes_the_model(tmp_path, agents, capsys):
    base = ["--data", str(tmp_path)]
    cli.main(base + ["agent", "add", "muse", "--model", "claude-haiku-5-5"])
    capsys.readouterr()
    cli.main(base + ["agent", "list"])
    row = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("muse\t"))
    assert "claude-haiku-5-5" in row and "level 1" in row
    cli.main(base + ["agent", "model", "muse", "gpt-6-astra"])
    cli.main(base + ["agent", "list"])
    assert "level 3" in next(l for l in capsys.readouterr().out.splitlines() if l.startswith("muse\t"))
    with pytest.raises(SystemExit):
        cli.main(base + ["agent", "model", "claude", "gpt-6-astra"])
    AGENTS.pop("muse", None)
