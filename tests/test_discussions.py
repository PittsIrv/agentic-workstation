"""Discussions between agents: open, post, read, close with an outcome; CLI, API and page."""
import json

import pytest

from conftest import H, make
from workstation import cli
from workstation.principals import AGENTS


def agent_headers(c, tokens, name):
    return {"Authorization": "Bearer " + (c.app.state.ws.data / "secrets" / f"{name}.token").read_text()}


@pytest.fixture
def two(ws):
    """The shared workspace plus a third, added agent."""
    app, c, tokens = ws
    c.post("/api/agents", headers=H(tokens), json={"id": "muse", "display": "Muse"}).raise_for_status()
    tokens["muse"] = agent_headers(c, tokens, "muse")["Authorization"][7:]
    return app, c, tokens


def open_(c, tokens, who="claude", **kw):
    return c.post("/api/discussions", headers=H(tokens, who), json={"title": "Which store for the queue?", **kw})


def say(c, tokens, did, who, text="ok", **kw):
    return c.post(f"/api/discussions/{did}/posts", headers=H(tokens, who), json={"text": text, **kw})


def test_two_agents_exchange_messages_through_the_api_and_everyone_sees_them(two):
    app, c, tokens = two
    r = open_(c, tokens, "claude", text="I lean SQLite.", stance="agree")
    assert r.status_code == 200 and r.json()["outcome"] == "created"
    did = r.json()["discussion"]["id"]
    assert say(c, tokens, did, "gpt", "A file queue is simpler.", stance="disagree").status_code == 200
    assert say(c, tokens, did, "muse", "Either works for me.").status_code == 200
    for reader in ("claude", "gpt", "muse", "owner"):  # any registered agent, and the owner, reads it
        d = c.get(f"/api/discussions/{did}", headers=H(tokens, reader)).json()
        assert [(p["author"], p["text"]) for p in d["posts"]] == [
            ("claude", "I lean SQLite."), ("gpt", "A file queue is simpler."), ("muse", "Either works for me.")]
        assert d["participants"] == ["claude", "gpt", "muse"] and d["status"] == "open"
        assert d["stances"] == {"claude": "agree", "gpt": "disagree", "muse": "none"}
    assert [x["id"] for x in c.get("/api/discussions", headers=H(tokens, "gemini")).json()] == [did]


def test_author_comes_from_the_token(two):
    app, c, tokens = two
    did = open_(c, tokens, "gpt", text="t").json()["discussion"]["id"]
    r = c.post(f"/api/discussions/{did}/posts", headers=H(tokens, "claude"), json={"text": "x", "author": "owner"})
    assert r.json()["discussion"]["posts"][-1]["author"] == "claude"


def test_two_agents_exchange_messages_through_the_cli(two, monkeypatch, capsys, tmp_path):
    app, c, tokens = two

    def client(args, who):
        c.headers.update(H(tokens, who))
        return c
    monkeypatch.setattr(cli, "client", client)
    base = ["--data", str(tmp_path)]

    def run(*argv):
        cli.main(base + list(argv))
        return json.loads(capsys.readouterr().out)

    r = run("discuss", "Naming the CLI", "--as", "claude", "--text", "ws is fine", "--stance", "agree", "--key", "d1")
    did = r["discussion"]["id"]
    assert run("discuss", "Naming the CLI", "--as", "claude", "--text", "ws is fine", "--stance", "agree",
               "--key", "d1")["outcome"] == "duplicate"
    run("say", did, "Fine by me", "--as", "gpt", "--stance", "agree")
    shown = run("discussions", did, "--as", "muse")
    assert [(p["author"], p["text"]) for p in shown["posts"]] == [("claude", "ws is fine"), ("gpt", "Fine by me")]
    assert [d["id"] for d in run("discussions", "--as", "muse")] == [did]
    closed = run("close", did, "--as", "gpt", "--outcome", "agreed", "--note", "settled")
    assert closed["status"] == "closed" and closed["outcome"] == "agreed" and closed["closed_by"] == "gpt"
    assert run("discussions", "--as", "muse") == [] and len(run("discussions", "--as", "muse", "--status", "closed")) == 1


def test_a_discussion_can_be_attached_to_a_task(ws):
    app, c, tokens = ws
    t = c.post("/api/tasks", headers=H(tokens), json={"title": "Draft", "project_id": "workstation", "owner": "claude",
                                                       "status": "ready"}).json()["task"]
    d = open_(c, tokens, "claude", task_id=t["id"], text="Scope?").json()["discussion"]
    assert d["task_id"] == t["id"] and d["project_id"] == "workstation"
    assert open_(c, tokens, "claude", task_id="task_nope").status_code == 422


def test_idempotent_posts_and_limits(two):
    app, c, tokens = two
    did = open_(c, tokens).json()["discussion"]["id"]
    a = say(c, tokens, did, "gpt", "once", idempotency_key="k1")
    b = say(c, tokens, did, "gpt", "once", idempotency_key="k1")
    assert (a.json()["outcome"], b.json()["outcome"]) == ("posted", "duplicate")
    assert len(c.get(f"/api/discussions/{did}", headers=H(tokens)).json()["posts"]) == 1
    assert say(c, tokens, did, "gpt", "other", idempotency_key="k1").status_code == 409
    assert say(c, tokens, did, "gpt", "").status_code == 422
    assert say(c, tokens, did, "gpt", "x" * 2001).status_code == 422
    assert say(c, tokens, did, "gpt", "x", stance="loud").status_code == 422
    assert open_(c, tokens, title="").status_code == 422
    assert say(c, tokens, "disc_missing", "gpt").status_code == 404
    from workstation import discussions
    discussions.MAX_POSTS, old = 3, discussions.MAX_POSTS
    try:
        say(c, tokens, did, "gpt"); say(c, tokens, did, "gpt")
        assert say(c, tokens, did, "gpt").status_code == 409
    finally:
        discussions.MAX_POSTS = old


def test_agreed_needs_every_participants_latest_stance_to_be_agree(two):
    app, c, tokens = two
    close = lambda did, who="claude", outcome="agreed": c.post(f"/api/discussions/{did}/close", headers=H(tokens, who),
                                                               json={"outcome": outcome, "note": "n"})
    did = open_(c, tokens, "claude", text="A", stance="agree").json()["discussion"]["id"]
    assert close(did).status_code == 409                    # nobody else took part
    say(c, tokens, did, "gpt", "B", stance="disagree")
    assert close(did).status_code == 409                    # a dissent
    say(c, tokens, did, "gpt", "B, after thought", stance="agree")  # latest stance counts
    say(c, tokens, did, "muse", "no stance")
    assert close(did).status_code == 409                    # a participant who never agreed
    say(c, tokens, did, "muse", "agree", stance="agree")
    assert close(did, "muse").status_code == 200
    assert say(c, tokens, did, "gpt").status_code == 409    # closed: no more posts
    assert close(did).status_code == 409                    # and no second outcome
    d = c.get(f"/api/discussions/{did}", headers=H(tokens)).json()
    assert d["outcome"] == "agreed" and d["closed_by"] == "muse"


def test_only_a_participant_closes_and_the_owner_is_not_a_participant(two):
    app, c, tokens = two
    did = open_(c, tokens, "claude", text="A", stance="agree").json()["discussion"]["id"]
    say(c, tokens, did, "gpt", "B", stance="agree")
    r = c.post(f"/api/discussions/{did}/close", headers=H(tokens, "muse"), json={"outcome": "agreed"})
    assert r.status_code == 403
    say(c, tokens, did, "owner", "owner chimes in")
    assert "owner" not in c.get(f"/api/discussions/{did}", headers=H(tokens)).json()["participants"]


def test_unsettled_discussion_sent_to_the_user_creates_an_inbox_item(two):
    app, c, tokens = two
    did = open_(c, tokens, "claude", text="SQLite", stance="agree").json()["discussion"]["id"]
    say(c, tokens, did, "gpt", "Files", stance="disagree")
    r = c.post(f"/api/discussions/{did}/close", headers=H(tokens, "gpt"), json={"outcome": "sent_to_user", "note": "we split"})
    assert r.status_code == 200 and r.json()["outcome"] == "sent_to_user" and r.json()["ask_id"]
    items = c.get("/api/inbox", headers=H(tokens)).json()["items"]
    item = next(i for i in items if i["id"] == r.json()["ask_id"])
    assert item["title"].startswith("Unsettled:") and item["asked_by"] == "gpt"
    assert item["discussion_id"] == did and item["link"] == f"#/discussions/{did}"
    assert "Claude: agree" in item["text"] and "GPT: disagree" in item["text"]
    assert c.post("/api/discussions", headers=H(tokens), json={"title": "t"}).status_code == 200  # owner may open too
    odid = c.post("/api/discussions", headers=H(tokens), json={"title": "t2"}).json()["discussion"]["id"]
    assert c.post(f"/api/discussions/{odid}/close", headers=H(tokens), json={"outcome": "sent_to_user"}).status_code == 422


def test_overruled_is_not_available_to_agents_until_levels_exist(two):
    app, c, tokens = two
    did = open_(c, tokens, "claude", text="A").json()["discussion"]["id"]
    say(c, tokens, did, "gpt", "B")
    assert c.post(f"/api/discussions/{did}/close", headers=H(tokens, "claude"), json={"outcome": "overruled"}).status_code == 403
    assert c.post(f"/api/discussions/{did}/close", headers=H(tokens), json={"outcome": "overruled"}).status_code == 200


def test_discussions_open_no_owner_only_action_and_survive_a_restart(tmp_path, agents):
    app, c, tokens = make(tmp_path, agents)
    did = open_(c, tokens, "claude", text="hello").json()["discussion"]["id"]
    assert c.post("/api/discussions", json={"title": "x"}).status_code == 401
    assert c.get(f"/api/discussions/{did}").status_code == 401
    app.state.ws.close()
    app2, c2, tokens2 = make(tmp_path, agents)
    d = c2.get(f"/api/discussions/{did}", headers=H(tokens2)).json()
    assert d["posts"][0]["text"] == "hello"
    app2.state.ws.close()


def test_gemini_and_custom_agents_may_discuss_but_a_removed_capability_is_refused(two):
    app, c, tokens = two
    assert open_(c, tokens, "gemini").status_code == 200
    AGENTS["gpt"]["capabilities"].discard("discuss")
    try:
        assert open_(c, tokens, "gpt").status_code == 403
    finally:
        AGENTS["gpt"]["capabilities"].add("discuss")


def test_no_agent_is_called(two):
    app, c, tokens = two
    did = open_(c, tokens, "claude", text="@gpt please answer").json()["discussion"]["id"]
    assert say(c, tokens, did, "muse", "@claude hi").status_code == 200
    assert not app.state.ws.store.state.dispatches  # a mention in a discussion dispatches nothing
