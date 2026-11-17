"""Hostile agent and thread text, rendered by a real headless browser, must stay inert.

Every page that shows agent or thread text is loaded with fixtures that try script tags, event-handler
attributes, javascript: links, SVG onload and attribute break-outs. The dumped DOM must hold no extra
script, no media or embed elements, no on* attributes, no javascript: URLs, and no side effect of a payload.
Needs Chrome or Chromium (set WS_REQUIRE_BROWSER=1 to fail instead of skip when it is missing).
Fixtures only: no real Claude or Codex call.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from html.parser import HTMLParser
from pathlib import Path

import pytest
import uvicorn

from workstation.app import create_app
from workstation.runners import Fake, Unconnected

MARK = "PWNED"
PAYLOADS = [
    f"<script>document.title='{MARK}1'</script>",
    f"<img src=x onerror=\"document.title='{MARK}2'\">",
    f"<svg onload=\"document.title='{MARK}3'\"></svg>",
    f"\" onmouseover=\"document.title='{MARK}4'\" x=\"",
    f"' onfocus='document.title=\"{MARK}5\"' autofocus='",
    f"[click](javascript:document.title='{MARK}6')",
    f"<a href=\"javascript:document.title='{MARK}7'\">link</a>",
    f"<iframe srcdoc=\"<script>parent.document.title='{MARK}8'</script>\"></iframe>",
    f"</script><script>document.title='{MARK}9'</script>",
    f"<details open ontoggle=\"document.title='{MARK}10'\">",
    "<img src=x onerror=\"document.body.setAttribute('data-pwned','1')\">",
]
HOSTILE = " ".join(PAYLOADS)
JS_URL = f"javascript:document.title='{MARK}11'"
BAD_URL = f"https://example.test/\" onmouseover=\"document.title='{MARK}12'\" x=\""  # a valid web URL that tries to leave its attribute
FORBIDDEN_TAGS = {"img", "svg", "iframe", "object", "embed", "video", "audio", "math"}
URL_ATTRS = {"href", "src", "action", "formaction", "xlink:href"}


def chrome():
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        if path := shutil.which(name):
            return path
    for base in ("/Applications", os.path.expanduser("~/Applications")):
        mac = f"{base}/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.exists(mac):
            return mac
    return None


CHROME = chrome()
if CHROME is None and os.environ.get("WS_REQUIRE_BROWSER"):
    raise RuntimeError("WS_REQUIRE_BROWSER is set but no Chrome or Chromium was found")
pytestmark = pytest.mark.skipif(CHROME is None, reason="no Chrome or Chromium installed")


class Dom(HTMLParser):
    """Collects what a page's DOM contains that hostile text could have added."""

    def __init__(self):
        super().__init__()
        self.tags, self.attrs, self.urls, self.text = [], [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for k, v in attrs:
            self.attrs.append(k)
            if k in URL_ATTRS and v is not None:
                self.urls.append("".join(v.split()).lower())

    def handle_data(self, data):
        self.text.append(data)


@pytest.fixture(scope="module")
def fake_daybook():
    """A stand-in for Daybook's loopback API with made-up numbers and a hostile scope name."""
    from datetime import date, timedelta
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    day = (date.today() - timedelta(days=1)).isoformat()
    body = {"/api/health": {"application": "daybook", "demo": False},
            "/api/data": {"demo": False, "days": [{"date": day, "device_scope": HOSTILE[:80], "complete": True,
                                                   "total_seconds": 7200, "purpose_seconds": {
                                                       "Focus": 3600, "Entertainment": 1800, "Other": 1800, "Untagged": 0}}]}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            out = json.dumps(body.get(self.path, {})).encode()
            self.send_response(200 if self.path in body else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(scope="module")
def site(tmp_path_factory, fake_daybook):
    data = tmp_path_factory.mktemp("data")
    tickets = data / "tickets.md"
    tickets.write_text(f"## T-0001 · {HOSTILE}\n- status: open\n- priority: P1\n- repo: {HOSTILE}\n")
    esc = lambda s: json.dumps(s)  # TOML basic strings share JSON's escaping
    (data / "config.toml").write_text(
        f'[agents.claude]\nmodel = "claude-fable-5-2"\n'
        f"[owner]\nname = {esc(HOSTILE)}\n[tickets]\npath = {esc(str(tickets))}\ntitle = {esc(HOSTILE)}\n"
        f"[[coverage]]\nsource = {esc(HOSTILE)}\nstate = {esc(HOSTILE)}\n"
        f'[reminders]\nenabled = true\nwait_minutes_high = 0\nquiet_start = "00:00"\nquiet_end = "00:00"\n'
        f"[time]\ndaybook_url = {esc(fake_daybook)}\ndaybook_scope = {esc(HOSTILE)}\n")
    reply = (f"{HOSTILE}\n```json\n" + json.dumps({
        "position": HOSTILE, "options": [HOSTILE, "Safe option"], "verdict": "comment",
        "proposed_tasks": [{"title": HOSTILE, "owner": "claude"}]}) + "\n```")
    runners = {"claude": Fake(lambda p: reply), "gpt": Fake(lambda p: reply), "gemini": Unconnected("not connected")}
    app = create_app(data, runners)
    sock = socket.socket()  # bound here and handed to uvicorn, so no other process can take the port
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    base = f"http://127.0.0.1:{port}"
    import httpx
    token = app.state.ws.owner_file.read_text()
    http = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}", "X-Workstation": "1",
                                                "Origin": base})
    http.post("/api/projects", json={"id": "general", "name": HOSTILE, "goal": HOSTILE, "stage": HOSTILE,
                                     "milestone": HOSTILE}).raise_for_status()
    th = http.post("/api/threads", json={"title": HOSTILE, "project_id": "general"}).json()
    http.post(f"/api/threads/{th['id']}/messages", json={"text": f"@claude @gpt {HOSTILE}"}).raise_for_status()
    deadline = time.time() + 20
    while time.time() < deadline:
        msgs = http.get(f"/api/threads/{th['id']}").json()["messages"]
        if sum(m["author"] in ("claude", "gpt") for m in msgs) == 2:
            break
        time.sleep(0.2)
    draft = http.get(f"/api/threads/{th['id']}/decision-draft").json()
    dec = http.post("/api/decisions", json={
        "question": HOSTILE, "context": HOSTILE, "project_id": "general", "thread_id": th["id"],
        "options": [{"label": HOSTILE, "detail": HOSTILE}, {"label": "Safe", "detail": "ok"}],
        "recommendations": draft["recommendations"], "evidence": [{"title": HOSTILE, "url": BAD_URL}]}).json()
    done = http.post("/api/decisions", json={
        "question": HOSTILE, "context": HOSTILE, "project_id": "general", "thread_id": th["id"],
        "options": [{"label": HOSTILE, "detail": HOSTILE}, {"label": "Safe"}], "recommendations": draft["recommendations"],
        "evidence": [{"title": HOSTILE, "url": BAD_URL}]}).json()
    http.post(f"/api/decisions/{done['id']}/resolve", json={
        "status": "approved", "option_id": done["options"][0]["id"], "note": HOSTILE, "revisit": HOSTILE}).raise_for_status()
    record = http.get(f"/api/decisions/{done['id']}/record").json()["markdown"]
    task = http.post("/api/tasks", json={
        "title": HOSTILE, "project_id": "general", "owner": "owner", "status": "ready", "next_action": HOSTILE,
        "why": HOSTILE, "evidence_url": BAD_URL, "thread_id": th["id"]}).json()["task"]
    http.post(f"/api/tasks/{task['id']}/artifacts", json={"kind": "doc", "title": HOSTILE, "url": BAD_URL}).raise_for_status()
    http.post(f"/api/tasks/{task['id']}/artifacts", json={"kind": "other", "title": HOSTILE, "ref": HOSTILE}).raise_for_status()
    for status in ("in_progress", "needs_review", "done"):  # an accepted task, so the review has one to list
        http.patch(f"/api/tasks/{task['id']}", json={"expected_version": http.get(f"/api/tasks/{task['id']}").json()["version"],
                                                     "changes": {"status": status}}).raise_for_status()
    review_md = http.get("/api/review/record").json()["markdown"]
    # agent text on Home's new sections: an agent's task in progress, and one waiting for the owner's review
    mine = http.post("/api/tasks", json={"title": HOSTILE, "project_id": "general", "owner": "claude", "status": "in_progress",
                                          "next_action": HOSTILE}).json()["task"]
    rev = http.post("/api/tasks", json={"title": HOSTILE, "project_id": "general", "owner": "claude", "status": "in_progress",
                                         "next_action": HOSTILE, "why": HOSTILE}).json()["task"]
    http.post(f"/api/tasks/{rev['id']}/artifacts", json={"kind": "doc", "title": HOSTILE, "url": BAD_URL}).raise_for_status()
    http.patch(f"/api/tasks/{rev['id']}", json={"changes": {"status": "needs_review"}}).raise_for_status()
    agent_tokens = {a: app.state.ws.tokens.issue(a) for a in ("claude", "gpt")}
    for agent, kind in (("claude", "approval"), ("gpt", "question")):  # items from two agents, every text field hostile
        http.post("/api/asks", headers={"Authorization": f"Bearer {agent_tokens[agent]}"}, json={
            "title": HOSTILE[:200], "kind": kind, "text": HOSTILE, "blocked": HOSTILE[:300],
            "cost_of_waiting": HOSTILE[:300], "urgency": "high", "project_id": "general",
            "url": BAD_URL}).raise_for_status()
    # structured items: hostile text in every option field, the recommendation and the label; one open, one answered
    opts = [{"label": HOSTILE[:80], "cost": HOSTILE[:200], "risk": HOSTILE[:200], "after": HOSTILE[:200]},
            {"label": "Safe", "cost": HOSTILE[:200]}, {"label": HOSTILE[80:160] or "Other"}]
    for n in range(2):
        pick = http.post("/api/asks", headers={"Authorization": f"Bearer {agent_tokens['gpt']}"}, json={
            "title": HOSTILE[:200], "kind": "decision", "text": HOSTILE, "options": opts, "recommended": HOSTILE[:80],
            "label": HOSTILE[:60], "urgency": "high", "project_id": "general", "url": BAD_URL, "task_id": task["id"]})
        pick.raise_for_status()
    http.post(f"/api/asks/{pick.json()['ask']['id']}/answer", json={"resolution": "answered", "option_id": "o1", "note": HOSTILE[:300]}).raise_for_status()
    # reminders: every due item is reminded (hostile titles go into the log), then one is snoozed
    http.post("/api/reminders/run", json={"channel": "print"}).raise_for_status()
    snoozed = http.get("/api/needs-now").json()["items"][-1]["key"]
    http.post("/api/snooze", json={"key": snoozed, "minutes": 60}).raise_for_status()
    answered = http.post("/api/asks", headers={"Authorization": f"Bearer {agent_tokens['claude']}"},
                         json={"title": HOSTILE[:200], "kind": "question"}).json()["ask"]
    http.post(f"/api/asks/{answered['id']}/answer", json={"resolution": "answered", "note": HOSTILE}).raise_for_status()
    # an agent kind the owner added, with markup in its name and role: shown as plain text everywhere
    plain = "".join(c for c in HOSTILE if c.isprintable())
    http.post("/api/agents", json={"id": "muse", "display": plain[:40], "role": plain[:200]}).raise_for_status()
    muse = {"Authorization": "Bearer " + app.state.ws.tokens.issue("muse")}
    http.post("/api/asks", headers=muse, json={"title": HOSTILE[:200], "kind": "question", "text": HOSTILE}).raise_for_status()
    http.post("/api/tasks", headers=muse, json={"title": HOSTILE, "project_id": "general", "owner": "muse",
                                                "status": "in_progress", "next_action": HOSTILE}).raise_for_status()
    # lower-level work: a manager (claude is level 3 here) hands a task to the added, lower-level muse
    http.post("/api/tasks", headers={"Authorization": f"Bearer {agent_tokens['claude']}"}, json={
        "title": HOSTILE, "project_id": "general", "owner": "muse", "status": "ready", "next_action": HOSTILE}).raise_for_status()
    # a discussion between two agents, every text field hostile, one outcome recorded
    disc = http.post("/api/discussions", headers=muse, json={"title": HOSTILE[:200], "text": HOSTILE, "stance": "agree"}).json()["discussion"]
    http.post(f"/api/discussions/{disc['id']}/posts", headers={"Authorization": f"Bearer {agent_tokens['claude']}"},
              json={"text": HOSTILE, "stance": "disagree"}).raise_for_status()
    http.post(f"/api/discussions/{disc['id']}/close", headers=muse,
              json={"outcome": "sent_to_user", "note": HOSTILE[:500]}).raise_for_status()
    # an item with no project but a label, so Home has a group that is named by an agent's own (hostile) label
    http.post("/api/asks", headers=muse, json={"title": HOSTILE[:200], "kind": "question", "label": HOSTILE[:60]}).raise_for_status()
    profile = tmp_path_factory.mktemp("profile")
    yield {"base": base, "http": http, "profile": profile,
           "pages": {"home": "#/", "managers": "#/managers", "overview": "#/overview", "threads": "#/threads", f"thread": f"#/threads/{th['id']}", "tasks": "#/tasks",
                     "task": f"#/tasks/{task['id']}", "decision": f"#/decisions/{dec['id']}", "resolved": f"#/decisions/{done['id']}",
                     "activity": "#/activity", "review": "#/review", "inbox": "#/inbox", "time": "#/time", "decisions": "#/decisions", "setup": "#/setup", "discussions": "#/discussions",
                     "discussion": f"#/discussions/{disc['id']}"}, "record": record, "review_md": review_md}
    server.should_exit = True
    thread.join(timeout=10)
    app.state.ws.close()


def dump(site, route):
    """The DOM of one page after its scripts ran, from a browser that is signed in as the owner."""
    base = site["base"]
    flags = [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
             f"--user-data-dir={site['profile']}", "--virtual-time-budget=8000"]
    if not site.get("signed_in"):
        code = site["http"].post("/api/login-codes").json()["path"]
        subprocess.run(flags + ["--dump-dom", base + code], capture_output=True, timeout=90)
        site["signed_in"] = True
    out = subprocess.run(flags + ["--dump-dom", f"{base}/{route}"], capture_output=True, text=True, timeout=90)
    assert out.returncode == 0, out.stderr[-500:]
    if keep := os.environ.get("WS_DUMP_DIR"):  # inspect the DOM a failing page produced
        (Path(keep) / (route.strip("#/").replace("/", "_") or "home")).with_suffix(".html").write_text(out.stdout)
    return out.stdout


def inspect(html):
    dom = Dom()
    dom.feed(html)
    return dom


@pytest.mark.parametrize("name", ["home", "managers", "overview", "threads", "thread", "tasks", "task", "decision", "resolved", "activity", "review", "inbox", "time", "decisions", "setup", "discussions", "discussion"])
def test_hostile_text_stays_inert(site, name):
    html = dump(site, site["pages"][name])
    dom = inspect(html)
    shown = " ".join(dom.text)
    assert "<script>" in shown, f"{name}: the hostile fixture never rendered, so this page proves nothing"
    assert dom.tags.count("script") == 1, f"{name}: an extra <script> element reached the DOM"
    assert not FORBIDDEN_TAGS & set(dom.tags), f"{name}: {FORBIDDEN_TAGS & set(dom.tags)} reached the DOM"
    assert not [a for a in dom.attrs if a.startswith("on")], f"{name}: an event-handler attribute reached the DOM"
    assert not [a for a in dom.attrs if "pwned" in a], f"{name}: a payload set an attribute on the page"
    assert not [u for u in dom.urls if u.startswith(("javascript:", "data:", "vbscript:"))], \
        f"{name}: a script URL reached an href or src"
    assert f"<title>{MARK}" not in html and MARK not in "".join(dom.text[:1]), f"{name}: a payload changed the title"
    assert "<title>Workstation</title>" in html, f"{name}: the page title changed, so a payload ran"


def test_the_fixture_is_not_vacuous(site):
    """Hostile text shows as text, and the break-out URL is a real link whose attribute did not split."""
    dom = inspect(dump(site, site["pages"]["thread"]))
    assert any("onerror" in t for t in dom.text)
    task = inspect(dump(site, site["pages"]["task"]))
    assert any(u.startswith("https://example.test/") for u in task.urls)
    assert not [a for a in task.attrs if a.startswith("on")]


def test_script_urls_are_refused_at_intake(site):
    """The renderer is the second layer: the API already refuses anything but web links and in-app anchors."""
    http, th = site["http"], site["pages"]["task"].split("/")[-1]
    assert http.post("/api/tasks", json={"title": "t", "project_id": "general", "evidence_url": JS_URL}).status_code == 422
    assert http.post(f"/api/tasks/{th}/artifacts", json={"kind": "doc", "title": "t", "url": JS_URL}).status_code == 422


@pytest.mark.parametrize("export", ["record", "review_md"])
def test_exported_markdown_is_inert_when_rendered_in_a_browser(site, tmp_path, export):
    """The decision record and the weekly review, rendered as HTML with raw HTML allowed (the worst case for a viewer)."""
    from markdown_it import MarkdownIt
    assert "onerror" in site[export], "the export must carry the hostile text, or this proves nothing"
    body = MarkdownIt("commonmark", {"html": True}).render(site[export])
    page = tmp_path / "record.html"
    page.write_text(f"<!doctype html><title>Record</title><body>{body}</body>")
    out = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=4000",
                          f"--user-data-dir={site['profile']}", "--dump-dom", page.as_uri()],
                         capture_output=True, text=True, timeout=90)
    assert out.returncode == 0, out.stderr[-500:]
    dom = inspect(out.stdout)
    assert "script" not in dom.tags and not FORBIDDEN_TAGS & set(dom.tags)
    assert not [a for a in dom.attrs if a.startswith("on") or "pwned" in a]
    assert not [u for u in dom.urls if u.startswith(("javascript:", "data:", "vbscript:"))]
    assert "<title>Record</title>" in out.stdout and any("onerror" in t for t in dom.text)


def test_a_discussion_between_agents_is_visible_in_the_front_end(site):
    """Two agents' messages, their stances and the recorded outcome show on the discussion page and in the list."""
    page = " ".join(inspect(dump(site, site["pages"]["discussion"])).text)
    assert "agree" in page and "disagree" in page and "Sent to you" in page
    assert page.count("onerror") >= 3  # the opening post, the reply and the closing note, as plain text
    assert "Muse" in page or "muse" in page.lower()
    listing = " ".join(inspect(dump(site, site["pages"]["discussions"])).text)
    assert "2 message(s)" in listing and "Sent to you" in listing


def test_home_is_the_decision_queue_with_option_cards(site):
    """Home leads with what waits for the owner: option cards side by side, one recommended, one-click choose buttons,
    a link to the task, the chosen option in the answered list. Hostile option text is plain text."""
    html = dump(site, site["pages"]["home"]).split("<script")[0]  # the rendered page, not the script source
    page = " ".join(inspect(html).text)
    assert "Waiting for your decision" in page and "Your managers" not in page
    assert html.count('class="optc rec"') >= 1 and html.count('class="optgrid"') >= 1
    assert html.count('data-opt="o1"') >= 1 and html.count('data-opt="o2"') >= 1 and html.count('data-opt="o3"') >= 1
    assert re.search(r'<button class="primary" data-ans="[^"]+" data-res="answered" data-label="choice" data-opt="o1"', html), \
        "choosing an option is two-step (select, then confirm), like approvals"
    assert "recommends" in page and "Cost:" in page and "Risk:" in page and "Then:" in page
    assert "Recently answered" in page and "answered: " in page  # the answered item shows the chosen option
    assert re.search(r'href="#/tasks/task_[^"]+">task</a>', html)
    assert page.count("onerror") >= 8


def test_home_groups_the_queue_by_project_and_by_label(site):
    """Home puts the open cards under one heading per project; an item with no project goes under its label, and one
    with neither under "No project". Every card is in exactly one group and each heading counts the cards under it."""
    html = dump(site, site["pages"]["home"]).split("<script")[0]
    keys = re.findall(r'<div class="grp" data-group="([^"]*)"', html)
    assert len(keys) == len(set(keys)) == 3 and "project:general" in keys and "none" in keys
    assert sum(k.startswith("label:") for k in keys) == 1, "the item with only a label has a group of its own"
    total = int(re.search(r'Waiting for your decision <span class="cnt">(\d+)</span>', html).group(1))
    counts = []
    for part in html.split('<div class="grp" ')[1:]:
        n = int(re.search(r'<h3>.*?<span class="cnt">(\d+)</span></h3>', part, re.S).group(1))
        assert part.count('<div class="ibw">') == n, "a heading's count is the number of cards under it"
        counts.append(n)
    assert sum(counts) == total and total == html.count('<div class="ibw">')
    page = " ".join(inspect(html).text)
    assert "by project" in page and "No project" in page
    flat = dump(site, site["pages"]["inbox"]).split("<script")[0]  # the Inbox page keeps its single ordered list
    assert 'class="grp"' not in flat and flat.count('<div class="ibw">') == total


def test_the_managers_page_shows_manager_conversations_and_folds_lower_level_work(site):
    """DOM assertion: managers by name and level, their conversations, and lower-level work inside closed <details>."""
    html = dump(site, site["pages"]["managers"])
    dom = inspect(html)
    assert "Your managers" in " ".join(dom.text) and "level 3" in " ".join(dom.text)
    cards = re.findall(r'<div class="card mgr" data-manager="([a-z0-9-]+)"', html)
    assert cards == ["claude", "gpt"]                      # both top-model agents, and nobody else
    assert 'data-manager="muse"' not in html               # the lower-level agent has no card of its own
    tag = re.search(r'<details class="lower" data-lower="claude"[^>]*>', html)
    assert tag and " open" not in tag.group(0), "lower-level work must start collapsed"
    summary = re.search(r'<details class="lower" data-lower="claude"[^>]*><summary>(.*?)</summary>', html, re.S).group(1)
    assert "1 open task" in summary and "1 queued" in summary  # a count and a one-line status
    assert "Talk to an agent" in " ".join(dom.text)
    assert 'id="talk"' in html


@pytest.fixture(scope="module")
def bare_site(tmp_path_factory):
    """A server with nothing in it, for the first-run screen. Last in the file: the agent table is process-wide, so
    building a second app must not run before the tests of the first."""
    import httpx
    data = tmp_path_factory.mktemp("bare")
    app = create_app(data, {"claude": Fake(lambda p: "ok"), "gpt": Fake(lambda p: "ok"), "gemini": Unconnected("not connected")})
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    token = app.state.ws.owner_file.read_text()
    http = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}", "X-Workstation": "1", "Origin": base})
    yield {"base": base, "http": http, "profile": tmp_path_factory.mktemp("bareprofile")}
    server.should_exit = True
    thread.join(timeout=10)
    app.state.ws.close()


def test_empty_queue_says_so_and_shows_how_to_submit(bare_site):
    """With nothing ever submitted, Home says nothing waits and shows the command that submits a first item."""
    html = dump(bare_site, "#/").split("<script")[0]
    page = " ".join(inspect(html).text)
    assert "Nothing is waiting on you" in page and "Try it" in page
    assert re.search(r'<pre class="cmd">[^<]*ws[^<]*ask', html), "the submit command is shown"
    assert 'class="optc' not in html
    assert 'class="ib' not in html, "no item cards on an empty queue"

