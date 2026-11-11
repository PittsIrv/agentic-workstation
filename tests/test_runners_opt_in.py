"""The server runs no agent unless the owner turns runners on."""
import subprocess

import pytest
from fastapi.testclient import TestClient

from conftest import H
from workstation import config as configuration, runners
from workstation.app import create_app
from workstation.principals import Tokens


def boot(tmp_path):
    app = create_app(tmp_path, allowed_hosts=("testserver", "127.0.0.1"))  # no runners passed: the real default
    c = TestClient(app)
    tokens = {"owner": app.state.ws.owner_file.read_text(), "claude": Tokens(tmp_path / "tokens.json").issue("claude")}
    app.state.ws.svc.upsert_project("owner", "workstation", "Workstation build")
    return app, c, tokens


def test_runners_are_off_by_default_and_a_mention_calls_nothing(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("an agent process was started")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    app, c, tokens = boot(tmp_path)
    assert configuration.load(tmp_path).runners_enabled is False
    kinds = {k: type(v).__name__ for k, v in app.state.ws.dispatcher.runners.items()}
    assert kinds["claude"] == kinds["gpt"] == "PullOnly"
    t = c.post("/api/threads", headers=H(tokens), json={"title": "t", "project_id": "workstation"}).json()
    c.post(f"/api/threads/{t['id']}/messages", headers=H(tokens), json={"text": "@claude @gpt hello"}).raise_for_status()
    ds = list(app.state.ws.store.state.dispatches.values())
    assert len(ds) == 2 and {d["status"] for d in ds} == {"queued"}  # waiting for the agents to pick them up
    app.state.ws.close()


def test_turning_runners_on_is_an_explicit_config_choice(tmp_path):
    (tmp_path / "config.toml").write_text("[runners]\nenabled = true\n")
    app, c, tokens = boot(tmp_path)
    kinds = {k: type(v).__name__ for k, v in app.state.ws.dispatcher.runners.items()}
    assert kinds["claude"] == "ClaudeCLI" and kinds["gpt"] == "CodexCLI"  # built, never started here
    app.state.ws.close()
    (tmp_path / "config.toml").write_text('[runners]\nenabled = "yes"\n')
    with pytest.raises(ValueError):
        configuration.load(tmp_path)
