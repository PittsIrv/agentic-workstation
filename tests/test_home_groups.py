"""Home groups the open items by project, or by label for an item with no project.

Items of equal urgency submitted in the same second have no defined order between them, so these tests give the items
they compare a different urgency or deadline."""
from conftest import H

DUE = "2099-01-0{}T12:00:00+00:00"  # far-off deadlines: among equal urgency the earliest deadline comes first


def ask(c, tokens, who, title, **kw):
    r = c.post("/api/asks", headers=H(tokens, who), json={"title": title, **kw})
    r.raise_for_status()
    return r.json()["ask"]


def inbox(c, tokens):
    return c.get("/api/inbox", headers=H(tokens)).json()


def test_open_items_are_grouped_by_project_then_label_and_the_most_urgent_group_leads(ws):
    app, c, tokens = ws
    svc = app.state.ws.svc
    svc.upsert_project("owner", "lantern", "Lantern")
    svc.upsert_project("owner", "kiln", "Kiln")
    a1 = ask(c, tokens, "claude", "Store for the sync?", project_id="lantern", due_at=DUE.format(1))
    a2 = ask(c, tokens, "gpt", "Release the booking page?", project_id="kiln", urgency="high")
    a3 = ask(c, tokens, "claude", "Order the seeds?", label="garden-plan", due_at=DUE.format(2))
    a4 = ask(c, tokens, "gpt", "Which beds first?", label="Garden-Plan", urgency="low")
    a5 = ask(c, tokens, "gemini", "Anything else this week?", due_at=DUE.format(3))
    a6 = ask(c, tokens, "claude", "Second sync question", project_id="lantern", label="ignored-when-there-is-a-project")
    dec = c.post("/api/decisions", headers=H(tokens, "claude"), json={
        "question": "Cache the list?", "options": [{"label": "a"}, {"label": "b"}], "project_id": "lantern"}).json()
    ib = inbox(c, tokens)
    groups = ib["groups"]
    assert [(g["key"], g["name"], g["by"], g["count"]) for g in groups] == [
        ("project:kiln", "Kiln", "project", 1),            # holds the only high-urgency item, so it comes first
        ("project:lantern", "Lantern", "project", 3),      # two asks and the pending decision
        ("label:garden-plan", "garden-plan", "label", 2),  # no project: grouped by label, whatever its case
        ("none", "No project", "none", 1)]
    by_key = {g["key"]: g["items"] for g in groups}
    assert by_key["project:kiln"] == [f"ask:{a2['id']}"]
    assert set(by_key["project:lantern"]) == {f"ask:{a1['id']}", f"ask:{a6['id']}", f"decision:{dec['id']}"}
    assert by_key["label:garden-plan"] == [f"ask:{a3['id']}", f"ask:{a4['id']}"]  # normal before low
    assert by_key["none"] == [f"ask:{a5['id']}"]
    # every open item is in exactly one group, and inside a group the inbox order (most urgent first) is kept
    order = [e["key"] for e in ib["items"]]
    flat = [k for g in groups for k in g["items"]]
    assert sorted(flat) == sorted(order) and len(set(flat)) == len(flat)
    for g in groups:
        assert g["items"] == [k for k in order if k in g["items"]]
    assert groups[0]["items"][0] == order[0], "the most urgent item is still the first thing on the page"


def test_a_group_empties_and_disappears_as_its_items_are_answered(ws):
    app, c, tokens = ws
    one = ask(c, tokens, "claude", "In the build project", project_id="workstation", urgency="high")
    two = ask(c, tokens, "gpt", "Tagged only", label="side-quest")
    assert [g["key"] for g in inbox(c, tokens)["groups"]] == ["project:workstation", "label:side-quest"]
    c.post(f"/api/asks/{one['id']}/answer", headers=H(tokens), json={"resolution": "answered", "note": "ok"}).raise_for_status()
    groups = inbox(c, tokens)["groups"]
    assert [(g["key"], g["count"]) for g in groups] == [("label:side-quest", 1)]  # one group left: Home shows no headings
    c.post(f"/api/asks/{two['id']}/withdraw", headers=H(tokens, "gpt")).raise_for_status()
    assert inbox(c, tokens)["groups"] == []


def test_tasks_waiting_for_the_owner_are_grouped_with_their_project(ws):
    app, c, tokens = ws
    t = c.post("/api/tasks", headers=H(tokens, "claude"), json={"title": "Ship it", "project_id": "workstation",
                                                                 "owner": "claude", "status": "in_progress"}).json()["task"]
    c.post(f"/api/tasks/{t['id']}/artifacts", headers=H(tokens, "claude"),
           json={"kind": "doc", "title": "d", "url": "https://example.com/d"}).raise_for_status()
    c.patch(f"/api/tasks/{t['id']}", headers=H(tokens, "claude"), json={"changes": {"status": "needs_review"}}).raise_for_status()
    ask(c, tokens, "gpt", "No project here", urgency="low")
    groups = inbox(c, tokens)["groups"]
    assert {g["key"]: g["items"] for g in groups}["project:workstation"] == [f"task:{t['id']}"]
    assert groups[0]["name"] == "Workstation build" and {g["key"] for g in groups} == {"project:workstation", "none"}
