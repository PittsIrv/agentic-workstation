"""HTTP API and dashboard. Binds to localhost.

Auth: the owner uses a session cookie obtained from a one-time login code (`ws open`);
agents and adapters send `Authorization: Bearer <token>` (`ws token <id>`). Cookie-authenticated
writes must carry `X-Workstation: 1` and a same-origin `Origin`, so other sites cannot post.
"""
from __future__ import annotations

import fcntl
import secrets
import shlex
import threading
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

from . import inbox as inbox_view
from . import reminders
from . import timesources
from . import views
from . import config as configuration
from . import principals as agent_registry
from .principals import AGENTS, InvalidAgent, Tokens, can, configure_owner, is_human, write_token_file
from . import review as weekly
from .export import decision_record
from .principals import owner as owner_id
from .runners import Dispatcher, PullOnly, Runner, build_prompt, default_runners, parse_reply
from .service import Conflict, Forbidden, Invalid, NotFound, Service
from .store import Store
from .tickets import TicketFile

STATIC = Path(__file__).parent / "static"
COOKIE = "ws_session"
CLAIM_LEASE_S = 15 * 60  # a session claim that has not replied by then can be claimed again


class ThreadIn(BaseModel):
    title: str
    project_id: str
    task_id: str | None = None
    idempotency_key: str | None = None


class MessageIn(BaseModel):
    text: str
    idempotency_key: str | None = None


class DecisionIn(BaseModel):
    question: str
    options: list[dict]
    project_id: str
    recommendations: list[dict] = []
    evidence: list[dict] = []
    context: str = ""
    urgency: str = "normal"
    due_at: str | None = None
    due_tz: str | None = None
    thread_id: str | None = None
    follow_up_owner: str = "claude"
    idempotency_key: str | None = None


class AskIn(BaseModel):
    title: str
    kind: str = "question"
    text: str = ""
    blocked: str = ""
    cost_of_waiting: str = ""
    urgency: str = "normal"
    project_id: str | None = None
    task_id: str | None = None
    due_at: str | None = None
    due_tz: str | None = None
    url: str | None = None
    idempotency_key: str | None = None


class AnswerIn(BaseModel):
    resolution: str
    note: str = ""


class SnoozeIn(BaseModel):
    key: str
    minutes: int


class KeyIn(BaseModel):
    key: str


class PauseIn(BaseModel):
    minutes: int | None = None


class RunIn(BaseModel):
    channel: str = "print"
    dry_run: bool = False


def agent_instructions(data_dir: Path, agent_id: str) -> str | None:
    """The copy-ready instructions for an added agent, with this install's folders and the agent's name filled in."""
    root = Path(__file__).resolve().parents[2]
    f = root / "docs" / "agent-instructions" / "AGENT.snippet.md"
    if not f.exists():
        return None
    text = f.read_text().replace("WORKSTATION_DIR/data", shlex.quote(str(Path(data_dir).resolve())))
    return text.replace("WORKSTATION_DIR", shlex.quote(str(root))).replace("AGENT_ID", agent_id)


class AgentIn(BaseModel):
    id: str
    display: str | None = None
    role: str | None = None


class ResolveIn(BaseModel):
    status: str
    option_id: str | None = None
    note: str = ""
    revisit: str = ""


class TaskIn(BaseModel):
    title: str
    project_id: str
    owner: str | None = None
    status: str = "inbox"
    priority: str = "P2"
    next_action: str = ""
    due_at: str | None = None
    due_tz: str | None = None
    evidence_url: str | None = None
    thread_id: str | None = None
    decision_id: str | None = None
    why: str = ""
    entity_key: str | None = None
    idempotency_key: str | None = None


class TaskPatch(BaseModel):
    expected_version: int | None = None
    idempotency_key: str | None = None
    changes: dict[str, Any] = Field(default_factory=dict)


class ArtifactIn(BaseModel):
    kind: str
    title: str
    url: str | None = None
    ref: str | None = None
    repo_path: str | None = None
    idempotency_key: str | None = None


class ReviewIn(BaseModel):
    verdict: str
    summary: str
    message_id: str | None = None
    idempotency_key: str | None = None


class EventIn(BaseModel):
    event_type: str
    idempotency_key: str
    project_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    entity_key: str | None = None
    source_system: str = "api"
    occurred_at: str | None = None
    provenance: dict | None = None


class ProjectIn(BaseModel):
    id: str
    name: str
    goal: str = ""
    stage: str = ""
    milestone: str = ""


class ReplyIn(BaseModel):
    text: str
    attempt: int | None = None
    runtime: dict = Field(default_factory=dict)


class Workstation:
    def __init__(self, data_dir: Path, runners: dict[str, Runner] | None = None):
        self.data = Path(data_dir)
        self.data.mkdir(parents=True, exist_ok=True)
        self.config = configuration.load(self.data)
        configure_owner(self.config.owner_id, self.config.owner_name)
        views.DISPLAY_TZ, views.COVERAGE = self.config.timezone, self.config.coverage
        views.TICKETS = TicketFile(self.config.tickets_path or None, tuple(self.config.tickets_closed),
                                   self.config.tickets_title)
        self._lock_file = open(self.data / "server.lock", "a")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_file.close()
            raise RuntimeError(f"another workstation server is using {self.data}") from None
        self.store = Store(self.data / "workstation.db")
        self.tokens = Tokens(self.data / "tokens.json")
        self._agents_mtime = -1.0
        self.sync_agents()
        self.dispatcher: Dispatcher | None = None
        self.svc = Service(self.store, on_dispatch=lambda d: self.dispatcher and self.dispatcher.submit(d))
        self.time_source = (timesources.Daybook(self.config.daybook_url, self.config.daybook_scope)
                            if self.config.time_source == "daybook" else timesources.Off())
        self.svc.max_open_asks = self.config.inbox_max_open_per_agent
        if runners is None:
            runners = default_runners(self.config.claude_model, self.config.gpt_model, self.config.gpt_effort)
        self.dispatcher = Dispatcher(self.svc, runners)
        self._agents_mtime = -1.0
        self.sync_agents()
        self.login_codes: dict[str, float] = {}
        self.owner_file = self.data / "secrets" / f"{owner_id()}.token"
        if not self.owner_file.exists():
            self.owner_file.parent.mkdir(parents=True, exist_ok=True)
            self.owner_file.write_text(self.tokens.issue(owner_id()))
            self.owner_file.chmod(0o600)


    def sync_agents(self) -> None:
        """Install owner-added agents from `<data>/agents.json` (also when the CLI added one while the server runs);
        each gets a runner that never runs it, so its mentions wait for the agent to pick them up."""
        m = agent_registry.custom_mtime(self.data)
        if m == self._agents_mtime:
            return
        agent_registry.load_custom(self.data)
        self._agents_mtime = m
        runners = getattr(getattr(self, "dispatcher", None), "runners", None)
        if runners is not None:
            for pid, a in AGENTS.items():
                if a.get("custom"):
                    runners.setdefault(pid, PullOnly())

    def close(self) -> None:
        """Release the data directory (tests restart a server on the same data)."""
        self.dispatcher.pool.shutdown(wait=True)
        self.store.db.close()
        self._lock_file.close()


def rotate_backup(ws: Workstation, keep: int = 14) -> Path:
    d = ws.data / "backups"
    out = ws.store.backup(d / f"workstation-{time.strftime('%Y%m%d-%H%M%S')}.db")
    for old in sorted(d.glob("workstation-*.db"))[:-keep]:
        old.unlink()
    return out


def start_backups(ws: Workstation, every: float = 24 * 3600) -> None:
    """A backup at start and then daily, while the server runs."""
    def loop():
        while True:
            try:
                rotate_backup(ws)
            except Exception as e:
                print(f"backup failed: {e}", flush=True)
            time.sleep(every)
    threading.Thread(target=loop, name="backup", daemon=True).start()


def create_app(data_dir: Path | str = "data", runners: dict[str, Runner] | None = None,
               recover: bool = True, allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")) -> FastAPI:
    ws = Workstation(Path(data_dir), runners)
    app = FastAPI(title="Workstation", version="0.1.0", docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))  # refuses DNS-rebinding hosts
    app.state.ws = ws
    if recover:
        ws.dispatcher.recover()
        start_backups(ws)
        if not ws.svc.s.projects:  # a thread needs a project; give a fresh install one to start in
            ws.svc.upsert_project(owner_id(), "general", "General", idempotency_key="seed-general-project")

    def refused(request: Request, reason: str) -> None:
        """Log an agent's refused attempt (owner-only action) for the weekly review."""
        try:
            who = principal(request)
            route = getattr(request.scope.get("route"), "path", request.url.path)
            ws.svc.record_refusal(who, f"{request.method} {route}", reason)
        except Exception:  # logging a refusal must never turn the 403 into a 500
            pass

    @app.exception_handler(Forbidden)
    async def _f(request, e):
        refused(request, str(e))
        return JSONResponse({"error": str(e)}, 403)

    @app.exception_handler(Invalid)
    async def _i(_, e): return JSONResponse({"error": str(e)}, 422)

    @app.exception_handler(Conflict)
    async def _c(_, e): return JSONResponse({"error": str(e)}, 409)

    @app.exception_handler(NotFound)
    async def _n(_, e): return JSONResponse({"error": str(e)}, 404)

    def principal(request: Request) -> str:
        ws.sync_agents()
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            who = ws.tokens.who(auth[7:].strip())
            if not who:
                raise HTTPException(401, "unknown token")
            return who
        who = ws.tokens.who(request.cookies.get(COOKIE))
        if not who:
            raise HTTPException(401, "sign in: run `ws open`")
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if request.headers.get("x-workstation") != "1" or (origin and origin != f"{request.url.scheme}://{request.url.netloc}"):
                raise HTTPException(403, "cross-site write refused")
        return who

    def owner(request: Request, who: str = Depends(principal)) -> str:
        if not is_human(who):
            refused(request, "owner only")
            raise HTTPException(403, "owner only")
        return who

    # ---- pages ---------------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/login")
    def login(code: str = ""):
        exp = ws.login_codes.pop(code, 0)
        if exp < time.time():
            return HTMLResponse("<p style='font:16px system-ui;margin:40px'>This sign-in link expired or was used. "
                                "Run <code>ws open</code> again.</p>", 401)
        r = RedirectResponse("/", 303)
        r.set_cookie(COOKIE, ws.owner_file.read_text(), httponly=True, samesite="strict",
                     max_age=60 * 60 * 24 * 30)
        return r

    @app.post("/api/login-codes")
    def login_code(_: str = Depends(owner)):
        code = secrets.token_urlsafe(18)
        ws.login_codes[code] = time.time() + 120
        return {"path": f"/login?code={code}", "expires_in": 120}

    # ---- reads ---------------------------------------------------------------------------
    @app.get("/api/version")
    def version(_: str = Depends(principal)):
        return {"seq": len(ws.store.state.events)}

    @app.get("/api/principals")
    def principals(_: str = Depends(principal)):
        return {"owner": owner_id(), "timezone": views.DISPLAY_TZ,
                "principals": {pid: {k: a[k] for k in ("display", "kind", "role")} for pid, a in AGENTS.items()}}

    @app.get("/api/me")
    def me(who: str = Depends(principal)):
        a = AGENTS[who]
        return {"id": who, "display": a["display"], "kind": a["kind"], "role": a["role"]}

    @app.get("/api/home")
    def home(_: str = Depends(owner)):
        agents = ws.dispatcher.status()
        with ws.store.lock:
            return {**views.home(ws.svc, agents), "agent_work": inbox_view.agent_work(ws.svc),
                    "first_item": bool(ws.store.state.asks)}

    @app.get("/api/threads/{thread_id}")
    def thread(thread_id: str, who: str = Depends(principal)):
        if thread_id not in ws.store.state.threads:
            raise NotFound(thread_id)
        if not is_human(who) and who not in ws.store.state.threads[thread_id]["participants"]:
            raise Forbidden("not a participant")
        with ws.store.lock:
            return views.thread_view(ws.svc, thread_id)

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str, _: str = Depends(principal)):
        with ws.store.lock:
            return views.task_card(ws.svc, ws.svc._task(task_id))

    @app.get("/api/tasks")
    def tasks(_: str = Depends(owner), status: str | None = None, owner_id: str | None = None,
              project_id: str | None = None, q: str | None = None):
        with ws.store.lock:
            ts = [views.task_card(ws.svc, t) for t in ws.store.state.tasks.values()
                  if (not status or t["status"] == status) and (not owner_id or t["owner"] == owner_id)
                  and (not project_id or t["project_id"] == project_id)
                  and (not q or q.lower() in (t["title"] + " " + (t.get("next_action") or "")).lower())]
        return sorted(ts, key=lambda c: c["updated_at"], reverse=True)

    @app.get("/api/decisions")
    def decisions(_: str = Depends(owner)):
        with ws.store.lock:
            return sorted((views.decision_card(d) for d in ws.store.state.decisions.values()),
                          key=lambda d: d["created_at"], reverse=True)

    @app.get("/api/activity")
    def activity(_: str = Depends(owner), limit: int = 200, project_id: str | None = None, actor: str | None = None):
        with ws.store.lock:
            return views.activity(ws.svc, limit, project_id, actor)

    @app.post("/api/agents/{agent}/probe")
    def probe(agent: str, _: str = Depends(owner)):
        ok, why = ws.dispatcher.probe(agent, fresh=True)
        return {"agent": agent, "available": ok, "reason": why}

    # ---- writes: owner ---------------------------------------------------------------------

    @app.post("/api/projects")
    def project(body: ProjectIn, who: str = Depends(owner)):
        return ws.svc.upsert_project(who, **body.model_dump())

    @app.post("/api/threads")
    def new_thread(body: ThreadIn, who: str = Depends(owner)):
        return ws.svc.create_thread(who, **body.model_dump())

    @app.post("/api/threads/{thread_id}/messages")
    def post(thread_id: str, body: MessageIn, who: str = Depends(principal)):
        key = f"{who}:{body.idempotency_key}" if body.idempotency_key else None
        return ws.svc.post_message(who, thread_id, body.text, idempotency_key=key)

    @app.post("/api/threads/{thread_id}/critique")
    def critique(thread_id: str, who: str = Depends(owner)):
        return {"dispatches": ws.svc.cross_review(who, thread_id)}

    @app.get("/api/threads/{thread_id}/decision-draft")
    def draft(thread_id: str, _: str = Depends(owner)):
        with ws.store.lock:
            return ws.svc.draft_decision(thread_id)

    @app.post("/api/decisions")
    def new_decision(body: DecisionIn, who: str = Depends(principal)):
        d = body.model_dump()
        if d["idempotency_key"]:
            d["idempotency_key"] = f"{who}:{d['idempotency_key']}"
        return ws.svc.create_decision(who, **d)

    @app.get("/api/decisions/{decision_id}/record")
    def record(decision_id: str, _: str = Depends(principal)):
        """The resolved decision as Markdown. Read-only: any identity may fetch it, nothing is written."""
        return decision_record(ws.svc, decision_id, views.DISPLAY_TZ, lambda who: AGENTS.get(who, {}).get("display", who))

    def review_for(end: str | None):
        try:
            day = date.fromisoformat(end) if end else None
        except ValueError:
            raise Invalid("end must be a date like 2026-11-08") from None
        return weekly.build(ws.svc, views.DISPLAY_TZ, day)

    @app.get("/api/review")
    def review(end: str | None = None, _: str = Depends(principal)):
        """The seven days ending on `end` (default today) in the display time zone. Read-only for every identity."""
        with ws.store.lock:
            return review_for(end)

    @app.get("/api/review/record")
    def review_record(end: str | None = None, _: str = Depends(principal)):
        with ws.store.lock:
            return weekly.render_review(review_for(end))

    @app.post("/api/decisions/{decision_id}/resolve")
    def resolve(decision_id: str, body: ResolveIn, who: str = Depends(principal)):
        return views.decision_card(ws.svc.resolve_decision(who, decision_id, **body.model_dump()))

    @app.post("/api/tasks")
    def new_task(body: TaskIn, who: str = Depends(principal)):
        d = body.model_dump()
        if d["idempotency_key"]:
            d["idempotency_key"] = f"{who}:{d['idempotency_key']}"
        t, outcome = ws.svc.create_task(who, **d)
        return {"task": views.task_card(ws.svc, t), "outcome": outcome}

    @app.patch("/api/tasks/{task_id}")
    def patch_task(task_id: str, body: TaskPatch, who: str = Depends(principal)):
        key = f"{who}:{body.idempotency_key}" if body.idempotency_key else None
        t = ws.svc.update_task(who, task_id, expected_version=body.expected_version, idempotency_key=key, **body.changes)
        return views.task_card(ws.svc, t)

    @app.post("/api/tasks/{task_id}/artifacts")
    def artifact(task_id: str, body: ArtifactIn, who: str = Depends(principal)):
        d = body.model_dump()
        if d["idempotency_key"]:
            d["idempotency_key"] = f"{who}:{d['idempotency_key']}"
        return views.task_card(ws.svc, ws.svc.link_artifact(who, task_id, **d))

    @app.post("/api/tasks/{task_id}/reviews")
    def review(task_id: str, body: ReviewIn, who: str = Depends(principal)):
        d = body.model_dump()
        if d["idempotency_key"]:
            d["idempotency_key"] = f"{who}:{d['idempotency_key']}"
        return views.task_card(ws.svc, ws.svc.record_review(who, task_id, **d))

    # ---- time: where the owner's time went (read at request time from a local source, never stored) ----

    @app.get("/api/time")
    def time_panel(end: str | None = None, _: str = Depends(owner)):
        """The seven days ending on `end` (default today in the display time zone). Owner only: it is the owner's own data."""
        try:
            day = date.fromisoformat(end) if end else datetime.now(ZoneInfo(views.DISPLAY_TZ)).date()
        except ValueError:
            raise Invalid("end must be a date like 2026-11-08") from None
        return timesources.window(ws.time_source, day)

    # ---- inbox: items agents submit for the owner ------------------------------------------

    # ---- first run: tokens for agents and the instructions to give them ----------------------------

    ROOT = Path(__file__).resolve().parents[2]
    SNIPPETS = {"codex": ("AGENTS.snippet.md", "AGENTS.md of a project, or ~/.codex/AGENTS.md"),
                "claude_md": ("CLAUDE.snippet.md", "CLAUDE.md of a project"),
                "claude_skill": ("claude-code-skill/SKILL.md", "~/.claude/skills/ask-the-person/SKILL.md")}
    SETUP_AGENTS = ("claude", "gpt")

    def setup_agent_ids() -> list[str]:
        return [*SETUP_AGENTS, *[p for p, a in AGENTS.items() if a.get("custom")]]

    def instructions_for(agent_id: str) -> str | None:
        return agent_instructions(ws.data, agent_id)

    @app.get("/api/setup")
    def setup(_: str = Depends(owner)):
        """What the first-run screen needs: which agents have a token, whether any item has arrived, and the copy-ready
        instructions with this install's folders filled in. Nothing secret: only whether a token file exists."""
        docs = ROOT / "docs" / "agent-instructions"
        data = str(ws.data.resolve())
        q = shlex.quote  # folders with spaces must survive being pasted into a shell
        fill = lambda text: text.replace("WORKSTATION_DIR/data", q(data)).replace("WORKSTATION_DIR", q(str(ROOT)))
        snippets = {}
        for key, (name, into) in SNIPPETS.items():
            f = docs / name
            snippets[key] = {"into": into, "text": fill(f.read_text()) if f.exists() else None}
        with ws.store.lock:
            asks = list(ws.store.state.asks.values())
        base = f"uv --directory {q(str(ROOT))} run ws --data {q(data)}"
        return {
            "agents": [{"id": a, "display": AGENTS[a]["display"], "has_token": (ws.data / "secrets" / f"{a}.token").exists(),
                        "submitted": sum(x["asked_by"] == a for x in asks), "custom": bool(AGENTS[a].get("custom")),
                        **({"instructions": instructions_for(a)} if AGENTS[a].get("custom") else {})}
                       for a in setup_agent_ids()],
            "first_item": bool(asks), "snippets": snippets,
            "test_command": f'{base} ask "My first item" --as claude --kind question --text "Does this reach my inbox?"',
            "workstation_dir": str(ROOT), "data_dir": data, "is_checkout": (ROOT / "pyproject.toml").exists(),
            "coverage": [{"source": c["source"], "state": c["state"]} for c in views.COVERAGE]}

    @app.post("/api/setup/token/{agent}")
    def setup_token(agent: str, who: str = Depends(owner)):
        """Create (or replace) an agent's token file in this install's data folder. The token is not returned."""
        if agent not in setup_agent_ids():
            raise NotFound(f"agent {agent}")
        write_token_file(ws.tokens, ws.data, agent)
        return {"agent": agent, "has_token": True}

    @app.post("/api/agents")
    def add_agent(body: AgentIn, who: str = Depends(owner)):
        """Add an agent kind by name (for example codex, claude-code, muse). Creates its token file and returns the
        copy-ready instructions. Owner only; the token itself is never returned."""
        try:
            agent_registry.add_custom(ws.data, body.id, body.display, body.role)
        except InvalidAgent as e:
            raise Invalid(str(e)) from None
        ws.sync_agents()
        write_token_file(ws.tokens, ws.data, body.id)
        return {"id": body.id, "display": AGENTS[body.id]["display"], "has_token": True,
                "instructions": instructions_for(body.id)}

    @app.post("/api/asks")
    def new_ask(body: AskIn, who: str = Depends(principal)):
        d = body.model_dump()
        if d["idempotency_key"]:
            d["idempotency_key"] = f"{who}:{d['idempotency_key']}"
        a, outcome = ws.svc.create_ask(who, **d)
        return {"outcome": outcome, "ask": inbox_view.ask_card(ws.svc, a)}

    @app.get("/api/asks")
    def my_asks(status: str | None = None, who: str = Depends(principal)):
        """The owner sees every ask; an agent sees only its own, with the owner's answer once there is one."""
        if status not in (None, "open", "closed", "all"):
            raise Invalid("status must be open, closed or all")
        with ws.store.lock:
            return inbox_view.asks_for(ws.svc, who, status)

    @app.get("/api/inbox")
    def inbox(_: str = Depends(owner)):
        with ws.store.lock:
            return inbox_view.inbox(ws.svc, datetime.now(timezone.utc))

    @app.post("/api/asks/{ask_id}/answer")
    def answer_ask(ask_id: str, body: AnswerIn, who: str = Depends(owner)):
        return inbox_view.ask_card(ws.svc, ws.svc.answer_ask(who, ask_id, **body.model_dump()))

    @app.post("/api/asks/{ask_id}/withdraw")
    def withdraw_ask(ask_id: str, who: str = Depends(principal)):
        return inbox_view.ask_card(ws.svc, ws.svc.withdraw_ask(who, ask_id))

    # ---- reminders: what needs the owner now, the rule, snooze and pause, the log ----------------

    def rcfg() -> dict:
        return {**ws.config.reminders, "enabled": ws.config.reminders_enabled}

    @app.get("/api/needs-now")
    def needs_now(full: bool = False, who: str = Depends(principal)):
        """The owner gets the items to act on now, each with why. An agent gets only the state of its own items and
        whether the owner already has items due (no other agent's titles, no time data)."""
        with ws.store.lock:
            if is_human(who):
                return reminders.needs_now(ws.svc, rcfg(), views.DISPLAY_TZ, full=full)
            return reminders.agent_view(ws.svc, who, rcfg(), views.DISPLAY_TZ)

    @app.get("/api/reminders/settings")
    def reminder_settings(_: str = Depends(owner)):
        with ws.store.lock:
            return reminders.settings(rcfg(), ws.config.reminders_set, views.DISPLAY_TZ, ws.svc)

    @app.get("/api/reminders/log")
    def reminder_log(_: str = Depends(owner)):
        with ws.store.lock:
            return reminders.log(ws.svc, rcfg())

    @app.post("/api/snooze")
    def snooze(body: SnoozeIn, who: str = Depends(owner)):
        with ws.store.lock:
            return {"key": body.key, "until": reminders.snooze(ws.svc, who, body.key, body.minutes)}

    @app.post("/api/unsnooze")
    def unsnooze(body: KeyIn, who: str = Depends(owner)):
        with ws.store.lock:
            reminders.unsnooze(ws.svc, who, body.key)
        return {"key": body.key}

    @app.post("/api/reminders/pause")
    def pause(body: PauseIn, who: str = Depends(owner)):
        with ws.store.lock:
            return {"paused": reminders.pause(ws.svc, who, body.minutes)}

    @app.post("/api/reminders/resume")
    def resume(who: str = Depends(owner)):
        with ws.store.lock:
            reminders.resume(ws.svc, who)
        return {"paused": None}

    @app.post("/api/reminders/run")
    def reminders_run(body: RunIn, who: str = Depends(owner)):
        """Which due items to remind about now; records them unless dry_run. The caller prints or notifies."""
        with ws.store.lock:
            return reminders.run(ws.svc, who, rcfg(), views.DISPLAY_TZ, body.channel, body.dry_run)

    # ---- normalised event intake (assistants and bridges) ----------------------------------

    @app.post("/api/events")
    def intake(body: EventIn, who: str = Depends(principal)):
        key, p, svc = f"{who}:{body.idempotency_key}", body.payload, ws.svc
        if body.event_type == "task_created":
            fields = {k: p[k] for k in ("title", "owner", "status", "priority", "next_action", "due_at", "due_tz",
                                        "evidence_url", "thread_id", "why") if k in p}
            t, outcome = svc.create_task(who, project_id=body.project_id, entity_key=body.entity_key,
                                         idempotency_key=key, source_system=body.source_system,
                                         provenance=body.provenance, occurred_at=body.occurred_at, **fields)
            return {"outcome": outcome, "task": views.task_card(svc, t)}
        if body.event_type in ("task_updated", "task_completed"):
            tid = p.get("id") or (svc.store.task_for_entity(body.project_id, body.entity_key or "") or {}).get("id")
            if not tid:
                raise NotFound("task (by id or entity_key)")
            changes = {k: p[k] for k in ("status", "title", "next_action", "due_at", "due_tz", "priority",
                                         "blocked_reason", "note") if k in p}
            if body.event_type == "task_completed":
                changes["status"] = "done"
            t = svc.update_task(who, tid, idempotency_key=key, expected_version=p.get("expected_version"), **changes)
            return {"outcome": "updated", "task": views.task_card(svc, t)}
        if body.event_type == "decision_required":
            d = svc.create_decision(who, project_id=body.project_id, idempotency_key=key,
                                    **{k: p[k] for k in ("question", "options", "recommendations", "evidence",
                                                         "context", "urgency", "due_at", "due_tz", "thread_id")
                                       if k in p})
            return {"outcome": "created", "decision": views.decision_card(d)}
        if body.event_type == "ask_created":
            a, outcome = svc.create_ask(who, project_id=body.project_id or None, idempotency_key=key,
                                        **{k: p[k] for k in ("title", "kind", "text", "blocked", "cost_of_waiting",
                                                             "urgency", "task_id", "due_at", "due_tz", "url") if k in p})
            return {"outcome": outcome, "ask": inbox_view.ask_card(svc, a)}
        if body.event_type == "decision_resolved":
            return {"decision": views.decision_card(svc.resolve_decision(who, p.get("id", ""), status=p.get("status", ""),
                                                                        option_id=p.get("option_id"),
                                                                        note=p.get("note", "")))}
        if body.event_type == "agent_blocked":
            if not (is_human(who) or can(who, "agent_blocked", body.project_id)):
                raise Forbidden(f"{who} may not report blocks")
            if not isinstance(p.get("reason", ""), str):
                raise Invalid("reason must be text")
            with svc.store.batch():
                _, dup = svc.store.append("agent_blocked", who, {"reason": p.get("reason", ""),
                                                                  "task_id": p.get("task_id")},
                                          idempotency_key=key, project_id=body.project_id,
                                          source_system=body.source_system)
                if not dup and p.get("task_id"):
                    svc.update_task(who, p["task_id"], status="blocked", blocked_reason=p.get("reason", ""))
            return {"outcome": "duplicate" if dup else "recorded"}
        if body.event_type == "thread_mention":
            return {"message": svc.post_message(who, p.get("thread_id", ""), p.get("text", ""), idempotency_key=key)}
        raise Invalid(f"unsupported event_type {body.event_type}")

    # ---- pull pickup for agent sessions (e.g. a Claude Code session) ------------------------
    def claimable(d: dict) -> bool:
        if d["status"] == "queued":
            return True
        lapsed = time.time() - d.get("claimed_at", float("inf")) >= CLAIM_LEASE_S
        return d["status"] == "running" and (d.get("runtime") or {}).get("runtime") == "session-pickup" and lapsed

    @app.get("/api/dispatches")
    def my_dispatches(who: str = Depends(principal)):
        with ws.store.lock:
            return [{k: d.get(k) for k in ("id", "thread_id", "mode", "status", "created_at", "error")}
                    for d in ws.store.state.dispatches.values() if d.get("agent") == who and claimable(d)]

    @app.post("/api/dispatches/{dispatch_id}/claim")
    def claim(dispatch_id: str, who: str = Depends(principal)):
        with ws.store.batch():
            d = ws.store.state.dispatches.get(dispatch_id)
            if not d:
                raise NotFound(dispatch_id)
            if d["agent"] != who:
                raise Forbidden("this mention is addressed to another agent")
            if not claimable(d):
                raise Conflict(f"dispatch is {d['status']}")
            prompt, context = build_prompt(ws.svc, d)
            attempt = ws.svc.dispatch_started(dispatch_id, {"runtime": "session-pickup"}, claimed_at=time.time())
        return {"dispatch_id": dispatch_id, "attempt": attempt, "prompt": prompt, "context": context}

    @app.post("/api/dispatches/{dispatch_id}/reply")
    def reply(dispatch_id: str, body: ReplyIn, who: str = Depends(principal)):
        with ws.store.batch():
            d = ws.store.state.dispatches.get(dispatch_id)
            if not d:
                raise NotFound(dispatch_id)
            if d["agent"] != who:
                raise Forbidden("this mention is addressed to another agent")
            if body.attempt is None:
                raise Invalid("pass the attempt number your claim returned")
            if (d.get("runtime") or {}).get("runtime") != "session-pickup":
                raise Conflict(f"dispatch is {d['status']} and not held by a session; claim it first")
            text, structured = parse_reply(body.text)
            _, context = build_prompt(ws.svc, d)
            msg = ws.svc.dispatch_replied(dispatch_id, text, structured,
                                          {"runtime": "session-pickup", "reported": body.runtime}, context,
                                          body.attempt)
        return {"message": msg}

    return app
