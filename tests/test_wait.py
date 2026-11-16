"""Blocking wait: an agent blocks on its own item until the owner answers, or a timeout passes.

`GET /api/asks/<id>/wait` is a long-poll; `ws wait` repeats it up to --timeout and reports the result as an exit
status. Waiting is authorised exactly like reading: own items only, another agent's item looks like a missing one.
"""
import json
import re
import socket
import threading
import time

import pytest
import uvicorn

from conftest import H, make, wait as until
from workstation import app as wapp
from workstation import cli

OPTS = [{"label": "Ship now", "cost": "$0", "risk": "untested on Linux"}, {"label": "Wait a day", "cost": "one day"}]


def submit(c, tokens, who="claude", **kw):
    body = {"title": "Release the chart redesign?", "kind": "decision", "options": OPTS, "recommended": "Wait a day", **kw}
    r = c.post("/api/asks", headers=H(tokens, who), json=body)
    r.raise_for_status()
    return r.json()["ask"]


def waiting(c, tokens, ask_id, who="claude", timeout=20):
    """Start a wait in a thread; returns (thread, box) where box gets the response and how long it took."""
    box = {}

    def run():
        t0 = time.monotonic()
        box["r"] = c.get(f"/api/asks/{ask_id}/wait", headers=H(tokens, who), params={"timeout": timeout})
        box["took"] = time.monotonic() - t0
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th, box


def answer(c, tokens, ask_id, **body):
    return c.post(f"/api/asks/{ask_id}/answer", headers=H(tokens), json={"resolution": "answered", **body})


# ---- the four cases: answered, timeout, wrong agent, already answered ---------------------------------

def test_wait_returns_when_the_owner_answers(ws):
    app, c, tokens = ws
    ask = submit(c, tokens)
    th, box = waiting(c, tokens, ask["id"])
    time.sleep(0.6)
    assert th.is_alive(), "the wait must block while the item is open"
    assert answer(c, tokens, ask["id"], option_id="o2", note="after CI").status_code == 200
    th.join(timeout=10)
    assert not th.is_alive(), "the wait did not return after the answer"
    res = box["r"].json()
    assert box["r"].status_code == 200 and res["outcome"] == "answered"
    assert res["ask"]["status"] == "closed" and res["ask"]["option_id"] == "o2" and res["ask"]["note"] == "after CI"
    assert box["took"] < 10, "it returned on the answer, not on the 20 s timeout"


def test_wait_times_out_with_the_item_still_open(ws):
    app, c, tokens = ws
    ask = submit(c, tokens)
    t0 = time.monotonic()
    r = c.get(f"/api/asks/{ask['id']}/wait", headers=H(tokens, "claude"), params={"timeout": 0.5})
    took = time.monotonic() - t0
    assert r.status_code == 200 and r.json()["outcome"] == "timeout" and r.json()["ask"]["status"] == "open"
    assert 0.5 <= took < 5
    assert c.get(f"/api/asks/{ask['id']}", headers=H(tokens, "claude")).json()["status"] == "open"  # waiting changed nothing


def test_wait_on_another_agents_item_looks_like_a_missing_one(ws):
    app, c, tokens = ws
    mine = submit(c, tokens, "claude")
    theirs = submit(c, tokens, "gpt", title="GPT's question")
    t0 = time.monotonic()
    other = c.get(f"/api/asks/{theirs['id']}/wait", headers=H(tokens, "claude"), params={"timeout": 20})
    unknown = c.get("/api/asks/ask_nope/wait", headers=H(tokens, "claude"), params={"timeout": 20})
    assert time.monotonic() - t0 < 5, "a refused wait answers at once; it never holds the request"
    assert other.status_code == unknown.status_code == 404
    # same status and same shape as an unknown id, and as reading it; the only difference is the id the caller sent
    assert other.json()["error"].replace(theirs["id"], "X") == unknown.json()["error"].replace("ask_nope", "X")
    read = c.get(f"/api/asks/{theirs['id']}", headers=H(tokens, "claude"))
    assert other.json() == read.json() and set(other.json()) == set(unknown.json())
    assert "GPT's question" not in other.text and mine["id"] not in other.text
    # the other agent's answer never reaches the waiter either: still 404 once the owner has answered it
    answer(c, tokens, theirs["id"], option_id="o1").raise_for_status()
    assert c.get(f"/api/asks/{theirs['id']}/wait", headers=H(tokens, "claude"), params={"timeout": 0}).status_code == 404
    assert c.get(f"/api/asks/{mine['id']}/wait", params={"timeout": 0}).status_code == 401  # no token


def test_wait_on_an_already_answered_item_returns_at_once(ws):
    app, c, tokens = ws
    ask = submit(c, tokens)
    answer(c, tokens, ask["id"], note="Do it Friday").raise_for_status()
    t0 = time.monotonic()
    r = c.get(f"/api/asks/{ask['id']}/wait", headers=H(tokens, "claude"), params={"timeout": 20})
    assert time.monotonic() - t0 < 5
    assert r.json()["outcome"] == "answered" and r.json()["ask"]["note"] == "Do it Friday"
    assert r.json()["ask"]["resolution"] == "answered" and r.json()["ask"]["option_id"] is None


# ---- edges ---------------------------------------------------------------------------------------------

def test_a_withdrawn_item_ends_the_wait_as_withdrawn(ws):
    app, c, tokens = ws
    ask = submit(c, tokens)
    th, box = waiting(c, tokens, ask["id"])
    time.sleep(0.3)
    c.post(f"/api/asks/{ask['id']}/withdraw", headers=H(tokens, "claude")).raise_for_status()
    th.join(timeout=10)
    assert not th.is_alive() and box["r"].json()["outcome"] == "withdrawn"
    assert box["r"].json()["ask"]["status"] == "withdrawn"


def test_every_owner_resolution_ends_the_wait_and_the_owner_may_wait_on_any_item(ws):
    app, c, tokens = ws
    appr = submit(c, tokens, kind="approval", options=None, recommended=None)
    c.post(f"/api/asks/{appr['id']}/answer", headers=H(tokens), json={"resolution": "declined", "note": "not yet"}).raise_for_status()
    r = c.get(f"/api/asks/{appr['id']}/wait", headers=H(tokens, "claude"), params={"timeout": 5}).json()
    assert r["outcome"] == "answered" and r["ask"]["resolution"] == "declined" and r["ask"]["note"] == "not yet"
    open_one = submit(c, tokens, "gpt", title="Still open")
    own = c.get(f"/api/asks/{open_one['id']}/wait", headers=H(tokens), params={"timeout": 0})
    assert own.status_code == 200 and own.json()["outcome"] == "timeout"  # the owner reads every item, so may wait on it


def test_timeout_must_be_a_number_inside_the_limit(ws):
    app, c, tokens = ws
    ask = submit(c, tokens)
    for bad in ("-1", str(wapp.WAIT_MAX_S + 1), "nan", "inf", "soon"):
        r = c.get(f"/api/asks/{ask['id']}/wait", headers=H(tokens, "claude"), params={"timeout": bad})
        assert r.status_code == 422, bad
    assert c.get(f"/api/asks/{ask['id']}/wait", headers=H(tokens, "claude"), params={"timeout": 0}).json()["outcome"] == "timeout"


def test_waiting_opens_nothing_else_to_an_agent_and_writes_no_event(ws):
    app, c, tokens = ws
    ask = submit(c, tokens)
    before = len(app.state.ws.store.state.events)
    c.get(f"/api/asks/{ask['id']}/wait", headers=H(tokens, "claude"), params={"timeout": 0}).raise_for_status()
    c.get(f"/api/asks/{ask['id']}/wait", headers=H(tokens, "gpt"), params={"timeout": 0})
    assert len(app.state.ws.store.state.events) == before, "a wait only reads"
    for who in ("claude", "gpt"):  # the route adds no way to answer: only GET exists, and answering is still the owner's
        assert c.post(f"/api/asks/{ask['id']}/wait", headers=H(tokens, who), json={}).status_code == 405
        assert answer_as(c, tokens, who, ask["id"]).status_code == 403


def answer_as(c, tokens, who, ask_id):
    return c.post(f"/api/asks/{ask_id}/answer", headers=H(tokens, who), json={"resolution": "answered", "option_id": "o1"})


# ---- the command: `ws wait` and its exit status ----------------------------------------------------------

@pytest.fixture
def run(ws, monkeypatch, capsys, tmp_path):
    """Run `ws ...` against the test app; returns (exit status, printed JSON or None, stderr)."""
    app, c, tokens = ws

    def client(args, who):
        c.headers.update(H(tokens, who))
        return c
    monkeypatch.setattr(cli, "client", client)

    def go(*argv):
        code = 0
        try:
            cli.main(["--data", str(tmp_path), *argv])
        except SystemExit as e:
            code = e.code
        io = capsys.readouterr()
        return code, (json.loads(io.out) if io.out.strip() else None), io.err
    return go


def test_cli_exit_status_answered_timeout_withdrawn_and_missing(ws, run):
    app, c, tokens = ws
    ask = submit(c, tokens)
    code, res, err = run("wait", ask["id"], "--as", "claude", "--timeout", "0.3")
    assert code == cli.EXIT_TIMEOUT == 3 and res["outcome"] == "timeout" and res["ask"]["status"] == "open"
    assert "still open" in err
    answer(c, tokens, ask["id"], option_id="o1").raise_for_status()
    code, res, err = run("wait", ask["id"], "--as", "claude")
    assert code == 0 and res["outcome"] == "answered" and res["ask"]["option_id"] == "o1" and err == ""
    gone = submit(c, tokens, title="Second")
    c.post(f"/api/asks/{gone['id']}/withdraw", headers=H(tokens, "claude")).raise_for_status()
    code, res, _ = run("wait", gone["id"], "--as", "claude")
    assert code == cli.EXIT_WITHDRAWN == 4 and res["outcome"] == "withdrawn"
    code, res, _ = run("wait", ask["id"], "--as", "gpt")  # another agent's item: an error, like an unknown id
    assert isinstance(code, str) and code.startswith("404") and res is None
    assert run("wait", "ask_nope", "--as", "gpt")[0].replace("ask_nope", "X") == code.replace(ask["id"], "X")
    assert run("wait", ask["id"], "--as", "claude", "--timeout", "-5")[0] == 2  # a usage error, not a wait


def test_cli_keeps_asking_in_short_requests_until_its_own_timeout(ws, run, monkeypatch):
    """One request is held for at most WAIT_CHUNK_S, so a long --timeout is several requests, and an answer that
    arrives during a later one is still returned."""
    app, c, tokens = ws
    monkeypatch.setattr(cli, "WAIT_CHUNK_S", 0.2)
    ask = submit(c, tokens)
    calls = []
    real = c.get

    def get(path, **kw):
        calls.append((path, kw["params"]["timeout"]))
        return real(path, **kw)
    monkeypatch.setattr(c, "get", get)
    t0 = time.monotonic()
    code, res, _ = run("wait", ask["id"], "--as", "claude", "--timeout", "0.7")
    assert code == cli.EXIT_TIMEOUT and 0.7 <= time.monotonic() - t0 < 5
    assert len(calls) >= 3 and all(p == f"/api/asks/{ask['id']}/wait" and 0 <= t <= 0.2 for p, t in calls)
    threading.Timer(0.5, lambda: answer(c, tokens, ask["id"], note="go")).start()
    code, res, _ = run("wait", ask["id"], "--as", "claude", "--timeout", "20")
    assert code == 0 and res["ask"]["note"] == "go"


def test_cli_blocks_against_a_real_server_until_the_owner_answers(tmp_path, agents, capsys, monkeypatch):
    """No test client in between: a server on a loopback port, the agent's token file, and `ws wait` as the agent."""
    import httpx
    sock = socket.socket()  # bound here and handed to uvicorn, so no other process can take the port
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    (tmp_path / "config.toml").write_text(f"[server]\nport = {port}\n")
    app, _, tokens = make(tmp_path, agents)
    (tmp_path / "secrets" / "claude.token").write_text(tokens["claude"])
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    until(lambda: server.started, timeout=10)
    monkeypatch.setattr(cli, "URL", None)
    try:
        http = httpx.Client(base_url=f"http://127.0.0.1:{port}")
        ask = submit(http, tokens)
        done = {}

        def agent():
            try:
                cli.main(["--data", str(tmp_path), "wait", ask["id"], "--as", "claude", "--timeout", "30"])
                done["code"] = 0
            except SystemExit as e:
                done["code"] = e.code
        th = threading.Thread(target=agent, daemon=True)
        th.start()
        time.sleep(0.8)
        assert th.is_alive() and not done, "the command blocks while the item is open"
        answer(http, tokens, ask["id"], option_id="o2").raise_for_status()
        th.join(timeout=15)
        assert done == {"code": 0}
        res = json.loads(capsys.readouterr().out)
        assert res["outcome"] == "answered" and res["ask"]["option_id"] == "o2"
        # with the server gone the command fails with status 1 and says why, instead of hanging
        server.should_exit = True
        thread.join(timeout=10)
        with pytest.raises(SystemExit) as e:
            cli.main(["--data", str(tmp_path), "wait", ask["id"], "--as", "claude", "--timeout", "5"])
        assert isinstance(e.value.code, str) and "cannot reach the workstation server" in e.value.code
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        app.state.ws.close()


# ---- the documentation every agent is given ----------------------------------------------------------------

def test_every_agent_instruction_file_and_the_event_contract_document_the_wait():
    from pathlib import Path
    docs = Path(__file__).resolve().parents[1] / "docs"
    files = sorted((docs / "agent-instructions").rglob("*.md"))
    assert len(files) >= 5
    for f in [*files, docs / "events.md"]:
        text = f.read_text()
        assert " wait <ask-id> --as " in text, f"{f.name} does not show the wait command"
        assert "--timeout" in text and "answered" in text and re.search(r"(`3`|\b3) still open|\| 3 \|", text), \
            f"{f.name} does not explain the timeout and the exit status"
    assert "/api/asks/<id>/wait" in (docs / "events.md").read_text()
