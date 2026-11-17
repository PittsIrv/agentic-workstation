"""A seeded demo: a throwaway workstation with made-up items, so the Home can be tried end to end without real agents.

`ws demo` starts a separate server on 127.0.0.1 with its own temporary data folder, loads the projects and items below
through the same HTTP API and the same kind of token a real agent uses, and then waits on one item the way `ws wait`
does, so the owner sees an agent get its answer. Your own data folder is never read or written.

Everything here is invented: no real project, person, ticket, bill, address or path. The demo server runs no agent
(every agent is board-only) and calls nothing outside the machine.
"""
from __future__ import annotations

import shutil
import socket
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

# (id, name, goal): three invented projects, so the items spread over more than one.
PROJECTS = [
    ("lantern", "Lantern", "A reading list that syncs between devices"),
    ("tide-table", "Tide Table", "Tide charts for small harbours"),
    ("kiln", "Kiln", "Booking for a shared pottery studio"),
]

# The items, in the order they are submitted. `agent` is the built-in identity that submits it; `key` makes loading
# twice add nothing. The first item is the one the demo waits on.
ASKS = [
    {"agent": "claude", "key": "demo-lantern-store", "kind": "decision", "urgency": "high", "project_id": "lantern",
     "label": "lantern-sync", "title": "Which store for the reading-list sync?",
     "text": "Sync has to keep one list per device. There are two ways to store it; both pass the current tests.",
     "blocked": "the sync feature", "cost_of_waiting": "each day of waiting moves the sync release by a day",
     "options": [
         {"label": "Keep the single SQLite file", "cost": "$0, no new service", "risk": "one writer at a time",
          "after": "sync ships this week"},
         {"label": "Move to Postgres", "cost": "about two days of work", "risk": "one more service to run and back up",
          "after": "sync ships next week, with concurrent writers"},
         {"label": "Postpone sync", "cost": "nothing now", "risk": "the feature slips a release",
          "after": "I pick up the import bug instead"}],
     "recommended": "Keep the single SQLite file"},
    {"agent": "gpt", "key": "demo-tide-release", "kind": "decision", "urgency": "normal", "project_id": "tide-table",
     "label": "tide-charts", "title": "Release the chart redesign this week?",
     "text": "The new chart passes on desktop. Two small-screen layouts have not been reviewed yet.",
     "blocked": "the release notes", "cost_of_waiting": "the old chart stays up; nothing breaks",
     "options": [
         {"label": "Release on Thursday", "cost": "$0", "risk": "small-screen layouts are untested",
          "after": "the redesign is live this week"},
         {"label": "Hold for one more review", "cost": "three more days", "risk": "low",
          "after": "I review the small-screen layouts first"}],
     "recommended": "Hold for one more review"},
    {"agent": "claude", "key": "demo-kiln-fixtures", "kind": "approval", "urgency": "normal", "project_id": "kiln",
     "label": "kiln-cleanup", "title": "Approve deleting 14 unused image fixtures?",
     "text": "No test refers to them. The repository gets about 6 MB smaller.",
     "blocked": "the cleanup branch", "cost_of_waiting": "nothing urgent; the branch waits"},
    {"agent": "gemini", "key": "demo-kiln-summary", "kind": "question", "urgency": "low", "project_id": "kiln",
     "label": "kiln-summary", "title": "Which day should the weekly booking summary go out?",
     "text": "Members get one summary of the coming week's bookings.",
     "options": [
         {"label": "Monday morning", "cost": "$0", "risk": "weekend changes arrive late",
          "after": "the first one goes out next Monday"},
         {"label": "Friday afternoon", "cost": "$0", "risk": "it may be read after the weekend",
          "after": "the first one goes out this Friday"}]},
    {"agent": "gpt", "key": "demo-lantern-wording", "kind": "review", "urgency": "normal", "project_id": "lantern",
     "label": "lantern-sync", "title": "Review the wording of three new error messages",
     "text": '1. "This list is open on another device."\n2. "Sync is paused until you are online."\n'
             '3. "That link is already in your list."',
     "blocked": "merging the wording change", "cost_of_waiting": "the old messages stay; no user is blocked"},
    {"agent": "claude", "key": "demo-tide-empty", "kind": "question", "urgency": "low", "project_id": "tide-table",
     "label": "tide-charts", "title": "What should the chart show when a harbour has no data?",
     "text": "I see two ways: an empty chart with a short note, or no chart at all. Is there wording you prefer?"},
]
AGENTS_USED = tuple(dict.fromkeys(a["agent"] for a in ASKS))


def seed(base: str, owner_token: str, agent_tokens: dict[str, str]) -> list[dict]:
    """Load the invented projects (as the owner) and items (each as its agent) into the server at `base`, through its
    API. Returns the items in ASKS order. Loading twice adds nothing: every item carries an idempotency key."""
    auth = lambda token: {"Authorization": f"Bearer {token}"}
    out = []
    with httpx.Client(base_url=base, timeout=30) as http:
        for pid, name, goal in PROJECTS:
            http.post("/api/projects", headers=auth(owner_token), json={"id": pid, "name": name, "goal": goal}).raise_for_status()
        for a in ASKS:
            body = {k: v for k, v in a.items() if k not in ("agent", "key")}
            r = http.post("/api/asks", headers=auth(agent_tokens[a["agent"]]), json={**body, "idempotency_key": a["key"]})
            r.raise_for_status()
            out.append(r.json()["ask"])
    return out


@dataclass
class Demo:
    base: str
    data: Path
    asks: list[dict]
    app: object = field(repr=False)
    server: object = field(repr=False)
    thread: threading.Thread = field(repr=False)
    owner_token: str = field(repr=False)
    agent_tokens: dict[str, str] = field(repr=False)

    def login_url(self) -> str:
        """A one-time sign-in link for the owner (valid two minutes), the same one `ws open` makes."""
        r = httpx.post(f"{self.base}/api/login-codes", headers={"Authorization": f"Bearer {self.owner_token}"}, timeout=30)
        r.raise_for_status()
        return self.base + r.json()["path"]

    def wait(self, ask: dict, chunk: float = 25.0) -> dict:
        """Block, as the agent that submitted `ask`, until it is answered or withdrawn: the long-poll `ws wait` uses."""
        headers = {"Authorization": f"Bearer {self.agent_tokens[ask['asked_by']]}"}
        while True:
            r = httpx.get(f"{self.base}/api/asks/{ask['id']}/wait", headers=headers, params={"timeout": chunk}, timeout=chunk + 15)
            r.raise_for_status()
            if r.json()["outcome"] != "timeout":
                return r.json()

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.app.state.ws.close()


def start(data: Path, port: int = 0) -> Demo:
    """Start a demo server on 127.0.0.1 (a free port unless one is given) with `data` as its folder, and seed it.
    The folder must be empty or new: the demo never runs on an existing instance."""
    import uvicorn
    from .app import create_app
    from .principals import write_token_file
    from .runners import board_runners
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    if any(data.iterdir()):
        raise RuntimeError(f"{data} is not empty; the demo only runs in a folder of its own")
    sock = socket.socket()  # bound here to the loopback address and handed to uvicorn: never reachable from outside
    sock.bind(("127.0.0.1", port))
    port = sock.getsockname()[1]
    # The port, so `ws --data <folder> wait ...` finds this server; and no time source, so the demo's Time page never
    # shows the owner's real screen time next to made-up items.
    (data / "config.toml").write_text(f'[server]\nport = {port}\n[time]\nsource = "off"\n')
    app = create_app(data, board_runners())  # board-only runners: the demo server can never run an agent
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), name="ws-demo", daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("the demo server did not start")
        time.sleep(0.05)
    ws = app.state.ws
    for agent in AGENTS_USED:
        write_token_file(ws.tokens, data, agent)
    agent_tokens = {a: (data / "secrets" / f"{a}.token").read_text() for a in AGENTS_USED}
    base = f"http://127.0.0.1:{port}"
    owner_token = ws.owner_file.read_text()
    return Demo(base, data, seed(base, owner_token, agent_tokens), app, server, thread, owner_token, agent_tokens)


def _interrupt(*_):
    raise KeyboardInterrupt


def run(port: int = 0, open_browser: bool = True) -> None:
    """`ws demo`: serve a seeded throwaway instance, show how to sign in, wait on the first item like an agent would,
    report its answer, and keep serving until Ctrl-C. The temporary folder is removed on the way out."""
    import functools
    import signal
    import webbrowser
    say = functools.partial(print, flush=True)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _interrupt)  # a plain `kill` also cleans up
    data = Path(tempfile.mkdtemp(prefix="ws-demo-"))
    demo = None
    try:
        demo = start(data, port)
        first = demo.asks[0]
        who = first["asked_by_display"]
        url = demo.login_url()
        say(f"Demo workstation: {demo.base}")
        say(f"Data folder (temporary, removed when you stop the demo): {data}")
        say(f"Loaded {len(demo.asks)} made-up items from {len(AGENTS_USED)} agents in {len(PROJECTS)} invented projects. "
            "No agent runs here and nothing leaves this machine.")
        say(f"Sign in (one-time link, valid 2 minutes): {url}")
        if open_browser and webbrowser.open(url):
            say("Opened it in your browser.")
        say("")
        say(f'{who} is now waiting for your answer to: "{first["title"]}"')
        say(f"  (the same wait an agent runs: uv run ws --data {data} wait {first['id']} --as {first['asked_by']})")
        say('On Home, click "Choose this" on an option, then "Confirm choice".')
        res = demo.wait(first)
        ask = res["ask"]
        chosen = next((o["label"] for o in ask["options"] if o["id"] == ask.get("option_id")), None)
        detail = f'option {ask["option_id"]} "{chosen}"' if chosen else (ask.get("note") or ask.get("resolution") or "")
        say("")
        say(f"{who}'s wait returned: {res['outcome']}" + (f", {detail}" if detail else "") + ".")
        say("The demo keeps running so you can answer the rest. Press Ctrl-C to stop.")
        while demo.thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        say("")
        say("Stopping the demo.")
    finally:
        if demo:
            demo.stop()
        shutil.rmtree(data, ignore_errors=True)
