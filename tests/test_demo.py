"""The seeded demo: `ws demo` loads made-up items into a throwaway server so the Home can be tried end to end.

The last test is the end-to-end one: a real headless Chrome answers a seeded item in two steps (select, then confirm)
while a real `ws wait` process blocks on it as the agent, and the wait returns with the chosen option.
Fixtures only: no real agent runs, and every text is invented.
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from cdp import Browser
from test_browser_security import CHROME
from workstation import demo as demo_mod
from workstation.runners import PullOnly, Unconnected

needs_browser = pytest.mark.skipif(CHROME is None, reason="no Chrome or Chromium installed")
ENV = {k: v for k, v in os.environ.items() if k not in ("WS_URL", "WS_DATA", "PORT")}


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    d = demo_mod.start(tmp_path_factory.mktemp("demo") / "data")
    yield d
    d.stop()


def owner(d):
    return httpx.Client(base_url=d.base, headers={"Authorization": f"Bearer {d.owner_token}"}, timeout=30)


def as_agent(d, who):
    return httpx.Client(base_url=d.base, headers={"Authorization": f"Bearer {d.agent_tokens[who]}"}, timeout=30)


def test_seed_fills_the_queue_with_open_decisions_that_have_options_cost_and_risk(demo):
    items = owner(demo).get("/api/inbox").json()["items"]
    assert len(items) == len(demo_mod.ASKS) == 6 and all(i["source"] == "ask" and i["status"] == "open" for i in items)
    assert items[0]["title"] == demo_mod.ASKS[0]["title"], "the high-urgency item leads the queue"
    with_options = [i for i in items if i["options"]]
    assert len(with_options) >= 3
    for i in with_options:
        assert all(o["label"] and o["cost"] and o["risk"] for o in i["options"]), i["title"]
    assert sum(bool(i["recommended"]) for i in with_options) >= 2
    assert {i["kind"] for i in items} == {"decision", "approval", "question", "review"}
    assert {i["project"] for i in items} == {"Lantern", "Tide Table", "Kiln"}  # more than one project, by name
    assert {i["asked_by"] for i in items} == {"claude", "gpt", "gemini"} and all(i["label"] for i in items)
    assert owner(demo).get("/api/home").json()["first_item"] is True  # Home shows the queue, not the first-run strip


def test_seeding_twice_adds_nothing(demo):
    before = len(demo.app.state.ws.store.state.events)
    again = demo_mod.seed(demo.base, demo.owner_token, demo.agent_tokens)
    assert [a["id"] for a in again] == [a["id"] for a in demo.asks]
    assert len(owner(demo).get("/api/asks", params={"status": "all"}).json()) == len(demo_mod.ASKS)
    assert len(demo.app.state.ws.store.state.events) == before + len(demo_mod.PROJECTS)  # projects are upserts; no new item


def test_the_demo_server_is_local_runs_no_agent_and_keeps_the_usual_rules(demo, tmp_path):
    assert demo.base.startswith("http://127.0.0.1:")
    assert all(s.getsockname()[0] == "127.0.0.1" for srv in demo.server.servers for s in srv.sockets)
    runners = demo.app.state.ws.dispatcher.runners
    assert all(isinstance(r, (PullOnly, Unconnected)) for r in runners.values()), "the demo can never run an agent"
    mine, theirs = demo.asks[0], demo.asks[1]  # claude's and gpt's
    claude = as_agent(demo, "claude")
    assert claude.get(f"/api/asks/{mine['id']}").status_code == 200
    assert claude.get(f"/api/asks/{theirs['id']}").status_code == 404
    assert claude.post(f"/api/asks/{mine['id']}/answer", json={"resolution": "answered", "option_id": "o1"}).status_code == 403
    assert httpx.get(f"{demo.base}/api/inbox").status_code == 401  # nothing is open without a token
    week = owner(demo).get("/api/time").json()  # the demo reads no real time data and shows the default owner name
    assert week["source"] == "off" and not any(d["recorded"] for d in week["days"])
    assert owner(demo).get("/api/principals").json()["principals"]["owner"]["display"] == "Owner"
    (tmp_path / "used").mkdir()
    (tmp_path / "used" / "workstation.db").write_text("")
    with pytest.raises(RuntimeError, match="not empty"):  # it never starts on top of an existing instance
        demo_mod.start(tmp_path / "used")


def test_seed_text_is_invented_and_carries_nothing_personal():
    text = json.dumps([demo_mod.PROJECTS, demo_mod.ASKS])
    # the pattern is built from pieces so that a scan of this repository for local paths does not match the test itself
    assert not re.search("|".join(["/" + "Users/", "/" + "home/", r"[A-Za-z]:\\\\", "~/"]), text), "no local path"
    assert "@" not in text and "http" not in text, "no address and no link"
    assert not re.search(r"\b[A-Z]{1,5}-\d+\b", text), "no ticket id"
    assert not re.search(r"\b(invoice|bill|salary|rent|bank|card number|password|token)\b", text, re.I)
    for a in demo_mod.ASKS:  # the items satisfy the limits an agent's item has to satisfy
        assert a["agent"] in ("claude", "gpt", "gemini") and len(a["title"]) <= 200 and len(a["label"]) <= 60
        assert a["project_id"] in {p[0] for p in demo_mod.PROJECTS}


def read_until(proc, marker, timeout=60):
    """Lines of the process's output up to and including the first one that contains `marker`."""
    lines, deadline = [], time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        lines.append(line.rstrip("\n"))
        if marker in line:
            return lines
    raise AssertionError(f"`ws demo` never printed {marker!r}; got {lines}")


def test_one_command_serves_seeds_waits_reports_the_answer_and_cleans_up(tmp_path):
    """`ws demo --no-open` as a real process, started in an unrelated folder: it prints where it serves and how to sign
    in, an agent's wait is running, the owner's answer makes it report, and Ctrl-C removes the temporary folder."""
    proc = subprocess.Popen([sys.executable, "-m", "workstation.cli", "demo", "--no-open"], cwd=tmp_path, env=ENV,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        out = "\n".join(read_until(proc, "Confirm choice"))
        base = re.search(r"Demo workstation: (http://127\.0\.0\.1:\d+)$", out, re.M).group(1)
        data = Path(re.search(r"^Data folder .*: (.+)$", out, re.M).group(1))
        ask_id = re.search(r" wait (ask_\w+) --as claude", out).group(1)
        login = re.search(r"Sign in .*: (http://127\.0\.0\.1:\d+/login\?code=\S+)", out).group(1)
        assert "6 made-up items" in out and data.is_dir() and not (tmp_path / "data").exists()
        r = httpx.get(login, follow_redirects=False)
        assert r.status_code == 303 and "ws_session" in r.headers.get("set-cookie", "")  # the link signs the owner in
        me = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {(data / 'secrets' / 'owner.token').read_text()}"})
        assert len(me.get("/api/inbox").json()["items"]) == 6
        me.post(f"/api/asks/{ask_id}/answer", json={"resolution": "answered", "option_id": "o2"}).raise_for_status()
        out = "\n".join(read_until(proc, "Press Ctrl-C"))
        assert 'wait returned: answered, option o2 "Move to Postgres"' in out
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=20) == 0
        assert not data.exists(), "the temporary data folder is removed"
    finally:
        if proc.poll() is None:
            proc.kill()


@needs_browser
def test_answering_a_seeded_item_in_the_browser_in_two_steps_returns_the_waiting_agents_wait(demo, tmp_path):
    """End to end. An agent process blocks in `ws wait` on a seeded decision. In a real browser the owner signs in, sees
    the seeded cards with cost and risk, clicks "Choose this" (nothing is answered yet), then "Confirm choice".
    The wait returns with exit status 0 and the chosen option."""
    ask = demo.asks[1]  # the release decision, submitted by gpt, with two options
    assert ask["asked_by"] == "gpt" and [o["id"] for o in ask["options"]] == ["o1", "o2"]
    card, pick = f'.ib[data-ask="{ask["id"]}"]', f'.ib[data-ask="{ask["id"]}"] button[data-opt="o1"]'
    agent = subprocess.Popen([sys.executable, "-m", "workstation.cli", "--data", str(demo.data), "wait", ask["id"],
                              "--as", "gpt", "--timeout", "120"], env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    page = Browser(CHROME, tmp_path / "profile")
    try:
        page.goto(demo.login_url())  # the one-time link signs in and lands on Home
        page.until(f"!!document.querySelector({json.dumps(card)})", what="the seeded card on Home")
        home = page.text("#view")
        assert "Waiting for your decision" in home and all(a["title"] in home for a in demo_mod.ASKS)
        shown = page.text(card)
        for o in ask["options"]:  # the options side by side, each with its cost and risk
            assert o["label"] in shown and f"Cost: {o['cost']}" in shown and f"Risk: {o['risk']}" in shown
        assert "GPT recommends" in shown
        assert page.text(pick).strip() == "Choose this"

        page.click(pick)  # step 1: select
        page.until(f"document.querySelector({json.dumps(pick)}).textContent.trim() === 'Confirm choice'",
                   what="the button asking to confirm")
        time.sleep(1.0)
        assert agent.poll() is None, "selecting alone answers nothing: the agent is still waiting"
        assert owner(demo).get(f"/api/asks/{ask['id']}").json()["status"] == "open"

        page.click(pick)  # step 2: confirm
        out, err = agent.communicate(timeout=30)
        assert agent.returncode == 0, err
        res = json.loads(out)
        assert res["outcome"] == "answered" and res["ask"]["option_id"] == "o1" and res["ask"]["resolution"] == "answered"
        page.until(f"!document.querySelector({json.dumps(card)})", what="the answered card leaving the queue")
        page.until("document.querySelector('#view').textContent.includes('Recently answered')")
        assert f"answered: {ask['options'][0]['label']}" in page.text("#view")
    finally:
        page.close()
        if agent.poll() is None:
            agent.kill()
