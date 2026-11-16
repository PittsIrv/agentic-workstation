"""Structured asks: options, recommendation, label; the owner's choice; what an agent may read back."""
from datetime import datetime, timedelta, timezone

from conftest import H, make

OPTS = [{"label": "Ship now", "cost": "$0", "risk": "untested on Linux", "after": "release goes out today"},
        {"label": "Wait a day", "cost": "one day", "risk": "low", "after": "CI runs first"},
        {"label": "Drop it", "after": "feature removed"}]


def submit(c, tokens, who="claude", **kw):
    body = {"title": "Release the billing change?", "kind": "decision", "text": "CI is green on macOS only.",
            "options": OPTS, "recommended": "Wait a day", "label": "PROJ-12", **kw}
    return c.post("/api/asks", headers=H(tokens, who), json=body)


def test_agent_submits_three_options_and_owner_picks_one(ws):
    app, c, tokens = ws
    ask = submit(c, tokens).json()["ask"]
    assert [o["id"] for o in ask["options"]] == ["o1", "o2", "o3"]
    assert ask["options"][0]["risk"] == "untested on Linux" and ask["recommended"] == "o2" and ask["label"] == "PROJ-12"
    item = [i for i in c.get("/api/inbox", headers=H(tokens)).json()["items"] if i["id"] == ask["id"]][0]
    assert item["options"][1]["label"] == "Wait a day" and item["recommended"] == "o2"
    r = c.post(f"/api/asks/{ask['id']}/answer", headers=H(tokens), json={"resolution": "answered", "option_id": "o2"})
    assert r.status_code == 200
    back = c.get(f"/api/asks/{ask['id']}", headers=H(tokens, "claude")).json()
    assert back["status"] == "closed" and back["option_id"] == "o2" and back["resolution"] == "answered"


def test_free_text_answer_still_works_and_bad_option_is_refused(ws):
    app, c, tokens = ws
    ask = submit(c, tokens).json()["ask"]
    own = lambda body: c.post(f"/api/asks/{ask['id']}/answer", headers=H(tokens), json=body)
    assert own({"resolution": "answered", "option_id": "o9"}).status_code == 422
    assert own({"resolution": "dismissed", "option_id": "o1"}).status_code == 422
    assert own({"resolution": "answered"}).status_code == 422  # neither text nor option
    assert own({"resolution": "answered", "note": "Do it Friday"}).status_code == 200


def test_validation_of_options(ws):
    app, c, tokens = ws
    bad = lambda **kw: submit(c, tokens, **kw).status_code
    assert bad(options=OPTS[:1]) == 422
    assert bad(options=OPTS * 3) == 422  # more than six, and duplicate labels
    assert bad(options=[{"label": "a"}, {"label": "A"}]) == 422
    assert bad(options=[{"label": "a"}, {"label": ""}]) == 422
    assert bad(options=[{"label": "a"}, {"label": "b", "cost": "x" * 201}]) == 422
    assert bad(recommended="Nothing") == 422
    assert bad(kind="approval") == 422  # approvals have fixed buttons
    assert bad(label="x" * 61) == 422
    assert c.post("/api/asks", headers=H(tokens, "claude"), json={"title": "t", "recommended": "x"}).status_code == 422


def test_plain_ask_has_empty_options(ws):
    app, c, tokens = ws
    a = c.post("/api/asks", headers=H(tokens, "gpt"), json={"title": "Plain"}).json()["ask"]
    assert a["options"] == [] and a["recommended"] is None and a["label"] == ""


def test_agent_token_cannot_answer_even_with_an_option(ws):
    app, c, tokens = ws
    ask = submit(c, tokens).json()["ask"]
    for who in ("claude", "gpt"):  # the asker and another agent
        r = c.post(f"/api/asks/{ask['id']}/answer", headers=H(tokens, who), json={"resolution": "answered", "option_id": "o1"})
        assert r.status_code == 403
    assert c.get(f"/api/asks/{ask['id']}", headers=H(tokens, "claude")).json()["status"] == "open"


def test_agent_cannot_read_another_agents_items(ws):
    app, c, tokens = ws
    mine = submit(c, tokens, "claude").json()["ask"]
    theirs = submit(c, tokens, "gpt", title="GPT's question").json()["ask"]
    assert c.get(f"/api/asks/{theirs['id']}", headers=H(tokens, "claude")).status_code == 404
    unknown = c.get("/api/asks/ask_nope", headers=H(tokens, "claude"))
    assert unknown.status_code == 404
    other = c.get(f"/api/asks/{theirs['id']}", headers=H(tokens, "claude"))
    # same status and same shape; the only difference is the id the caller itself sent
    assert other.json()["error"].replace(theirs["id"], "X") == unknown.json()["error"].replace("ask_nope", "X")
    assert set(other.json()) == set(unknown.json())
    assert mine["id"] not in other.text
    listed = c.get("/api/asks", headers=H(tokens, "claude"), params={"status": "all"}).json()
    assert [a["id"] for a in listed] == [mine["id"]]
    assert c.get(f"/api/asks/{theirs['id']}", headers=H(tokens)).status_code == 200  # the owner sees all
    assert c.get(f"/api/asks/{mine['id']}", headers=H(tokens, "claude")).status_code == 200
    assert c.get(f"/api/asks/{mine['id']}").status_code == 401  # no token


def test_list_since_returns_new_and_changed_items(ws):
    app, c, tokens = ws
    svc = app.state.ws.svc
    first = submit(c, tokens).json()["ask"]
    second = submit(c, tokens, title="Second").json()["ask"]
    svc.s.asks[first["id"]]["updated_at"] = "2026-11-01T10:00:00+00:00"  # older than the mark
    get = lambda **p: {a["id"] for a in c.get("/api/asks", headers=H(tokens, "claude"), params={"status": "all", **p}).json()}
    mark = "2026-11-02T00:00:00+00:00"
    assert get(since=mark) == {second["id"]}
    c.post(f"/api/asks/{first['id']}/answer", headers=H(tokens), json={"resolution": "answered", "option_id": "o1"})
    assert get(since=mark) == {first["id"], second["id"]}  # answering changes it
    assert get() == {first["id"], second["id"]}
    assert c.get("/api/asks", headers=H(tokens, "claude"), params={"since": "yesterday"}).status_code == 422


def test_event_intake_accepts_options(ws):
    app, c, tokens = ws
    r = c.post("/api/events", headers=H(tokens, "claude"), json={
        "event_type": "ask_created", "idempotency_key": "k1", "project_id": "workstation",
        "payload": {"title": "Pick", "kind": "decision", "options": OPTS[:2], "recommended": "Ship now"}})
    assert r.status_code == 200 and r.json()["ask"]["recommended"] == "o1"


def test_since_without_utc_offset_is_refused_not_a_500(ws):
    app, c, tokens = ws
    submit(c, tokens, "claude")
    for bad in ("2026-11-12T09:00:00", "2026-11-12", "yesterday"):
        r = c.get("/api/asks", params={"since": bad}, headers=H(tokens, "claude"))
        assert r.status_code == 422, bad
    ok = c.get("/api/asks", params={"since": "2026-01-01T00:00:00+00:00"}, headers=H(tokens, "claude"))
    assert ok.status_code == 200 and len(ok.json()) == 1
