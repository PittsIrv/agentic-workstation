import json
import time

import pytest
from fastapi.testclient import TestClient

from workstation.app import create_app
from workstation.principals import Tokens
from workstation.runners import Fake, Unconnected


class Recorder:
    """Fake agent: remembers every prompt, answers with a structured block."""

    def __init__(self, name, position, options, verdict=None):
        self.name, self.position, self.options, self.verdict, self.prompts = name, position, options, verdict, []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        block = {"position": self.position, "options": self.options, "verdict": self.verdict or "none",
                 "proposed_tasks": []}
        return f"{self.name} view: {self.position}\n```json\n{json.dumps(block)}\n```"


@pytest.fixture
def agents():
    return {"claude": Recorder("Claude", "Build the local core", ["Local core", "OpenAgents backend"]),
            "gpt": Recorder("GPT", "Local core, but use SQLite", ["Local core", "Fork the UI"], verdict="approve")}


def make(tmp_path, agents, runners=None):
    runners = runners or {"claude": Fake(agents["claude"]), "gpt": Fake(agents["gpt"]),
                          "gemini": Unconnected("not connected")}
    app = create_app(tmp_path, runners, allowed_hosts=("testserver", "127.0.0.1"))
    c = TestClient(app)
    tokens = {}
    for who in ("owner", "claude", "gpt", "gemini"):
        tokens[who] = app.state.ws.owner_file.read_text() if who == "owner" else Tokens(tmp_path / "tokens.json").issue(who)
    return app, c, tokens


def H(tokens, who="owner"):
    return {"Authorization": f"Bearer {tokens[who]}"}


def wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.fixture
def ws(tmp_path, agents):
    app, c, tokens = make(tmp_path, agents)
    app.state.ws.svc.upsert_project("owner", "workstation", "Workstation build", goal="Unified agent workstation",
                                    stage="MVP")
    return app, c, tokens
