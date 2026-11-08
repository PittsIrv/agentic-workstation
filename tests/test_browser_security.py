"""Hostile agent and thread text, rendered by a real headless browser, must stay inert.

Every page that shows agent or thread text is loaded with fixtures that try script tags, event-handler
attributes, javascript: links, SVG onload and attribute break-outs. The dumped DOM must hold no extra
script, no media or embed elements, no on* attributes, no javascript: URLs, and no side effect of a payload.
Needs Chrome or Chromium (set WS_REQUIRE_BROWSER=1 to fail instead of skip when it is missing).
Fixtures only: no real Claude or Codex call.
"""
import json
import os
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
def site(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    tickets = data / "tickets.md"
    tickets.write_text(f"## T-0001 · {HOSTILE}\n- status: open\n- priority: P1\n- repo: {HOSTILE}\n")
    esc = lambda s: json.dumps(s)  # TOML basic strings share JSON's escaping
    (data / "config.toml").write_text(
        f"[owner]\nname = {esc(HOSTILE)}\n[tickets]\npath = {esc(str(tickets))}\ntitle = {esc(HOSTILE)}\n"
        f"[[coverage]]\nsource = {esc(HOSTILE)}\nstate = {esc(HOSTILE)}\n")
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
    profile = tmp_path_factory.mktemp("profile")
    yield {"base": base, "http": http, "profile": profile,
           "pages": {"home": "#/", "threads": "#/threads", f"thread": f"#/threads/{th['id']}", "tasks": "#/tasks",
                     "task": f"#/tasks/{task['id']}", "decision": f"#/decisions/{dec['id']}", "resolved": f"#/decisions/{done['id']}",
                     "activity": "#/activity", "review": "#/review"}, "record": record, "review_md": review_md}
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


@pytest.mark.parametrize("name", ["home", "threads", "thread", "tasks", "task", "decision", "resolved", "activity", "review"])
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
