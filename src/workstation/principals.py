"""Who can act, and what each identity may do.

Identities are stable ids, never display names. Each identity authenticates with its own
bearer token; tokens are stored only as SHA-256 hashes in `<data>/tokens.json`.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
from pathlib import Path

# Role cards. `capabilities` gate events; `projects` limits scope ("*" = all).
AGENTS: dict[str, dict] = {
    "owner": {
        "kind": "human", "display": "Owner", "handle": "owner", "role": "Owner and final decision-maker",
        "capabilities": {"*"}, "projects": {"*"}, "runtime": "human",
    },
    "claude": {
        "kind": "agent", "display": "Claude", "handle": "claude", "role": "Engineering lead: implements, tests, ships",
        "capabilities": {"reply", "task_create", "task_update_own", "artifact_link", "decision_propose", "agent_blocked", "ask_owner"},
        "projects": {"*"}, "runtime": "claude-cli",
    },
    "gpt": {
        "kind": "agent", "display": "GPT", "handle": "gpt",
        "role": "Independent research and review peer; does not edit Claude-owned code",
        "capabilities": {"reply", "review_record", "decision_propose", "task_suggest", "agent_blocked", "ask_owner"},
        "projects": {"*"}, "runtime": "codex-cli",
    },
    "gemini": {
        "kind": "agent", "display": "Gemini", "handle": "gemini",
        "role": "Secretary: reminders, schedule, organisation; makes no technical or strategic decisions",
        "capabilities": {"task_create_for_human", "task_update_own", "agent_blocked", "ask_owner"},
        "projects": {"*"}, "runtime": "not-connected",
    },
    # Internal author of notices (dispatch failures, budget limits). Never gets a token.
    "system": {
        "kind": "system", "display": "Workstation", "handle": "system", "role": "Workstation notices",
        "capabilities": set(), "projects": {"*"}, "runtime": "internal",
    },
}

# event_type -> capability an agent needs. Anything not listed is human-only.
EVENT_CAPABILITY = {
    "task_created": {"task_create", "task_create_for_human", "task_suggest"},
    "task_updated": {"task_update_own", "artifact_link", "review_record"},
    "task_completed": {"task_update_own"},
    "decision_required": {"decision_propose"},
    "agent_blocked": {"agent_blocked"},
}
HUMAN_ONLY = {"decision_resolved", "project_upserted", "thread_created"}


# ---- agent kinds the owner adds (Codex, Claude Code, Muse, anything that can run a command) ---------------
# Stored in `<data>/agents.json` as {id: {"display": ..., "role": ...}}. An added agent gets the same peer capabilities as
# `claude` plus recording reviews; nothing that belongs to the owner (see HUMAN_ONLY and the owner-only checks).
BUILTIN = ("owner", "claude", "gpt", "gemini", "system")
CUSTOM_CAPS = {"reply", "task_create", "task_update_own", "artifact_link", "review_record", "decision_propose",
               "agent_blocked", "ask_owner"}
AGENT_ID_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
DEFAULT_ROLE = "Agent added by the owner; works through the board"


class InvalidAgent(ValueError): ...


def _plain(text: object, what: str, limit: int) -> str:
    if not isinstance(text, str) or not text.strip() or len(text.strip()) > limit \
            or any(not c.isprintable() for c in text):
        raise InvalidAgent(f"{what} must be 1 to {limit} printable characters")
    return text.strip()


def check_new_agent(agent_id: str, display: str | None, role: str | None) -> tuple[str, str, str]:
    if not isinstance(agent_id, str) or not AGENT_ID_RE.fullmatch(agent_id):
        raise InvalidAgent("name must be lowercase letters, digits or -, start with a letter, and be at most 32 long")
    if agent_id in BUILTIN or agent_id == owner() or agent_id in AGENTS:
        raise InvalidAgent(f"{agent_id} is already taken")
    return agent_id, _plain(display or agent_id.capitalize(), "display name", 40), _plain(role or DEFAULT_ROLE, "role", 200)


def _agents_file(data_dir: Path) -> Path:
    return Path(data_dir) / "agents.json"


def load_custom(data_dir: Path) -> None:
    """Install the owner-added agents from `<data>/agents.json`, replacing any installed by an earlier call."""
    for pid in [p for p, a in AGENTS.items() if a.get("custom")]:
        del AGENTS[pid]
    f = _agents_file(data_dir)
    if not f.exists():
        return
    for pid, card in json.loads(f.read_text()).items():
        if not isinstance(card, dict):
            continue
        try:
            check_new_agent(pid, card.get("display"), card.get("role"))
        except InvalidAgent:
            continue  # a hand-edited file never installs a reserved or malformed id
        AGENTS[pid] = {"kind": "agent", "display": card["display"], "handle": pid, "role": card.get("role") or DEFAULT_ROLE,
                       "capabilities": set(CUSTOM_CAPS), "projects": {"*"}, "runtime": "board", "custom": True}


def custom_mtime(data_dir: Path) -> float:
    f = _agents_file(data_dir)
    return f.stat().st_mtime if f.exists() else 0.0


def add_custom(data_dir: Path, agent_id: str, display: str | None = None, role: str | None = None) -> dict:
    """Register a new agent kind. Serialised across processes; the file is replaced whole."""
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "agents.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        load_custom(d)  # pick up what another process added meanwhile
        agent_id, display, role = check_new_agent(agent_id, display, role)
        f = _agents_file(d)
        cards = json.loads(f.read_text()) if f.exists() else {}
        cards[agent_id] = {"display": display, "role": role}
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".agents-")
        with os.fdopen(fd, "w") as out:
            json.dump(cards, out, indent=1)
        os.replace(tmp, f)
        load_custom(d)
    return AGENTS[agent_id]


def configure_owner(owner_id: str, name: str) -> None:
    """Set the owner's id and display name (from config). The id is what the event log records."""
    current = owner()
    if current == owner_id and AGENTS[current]["display"] == name:
        return
    card = {**AGENTS.pop(current), "display": name, "handle": owner_id}
    rest = dict(AGENTS)
    AGENTS.clear()
    AGENTS.update({owner_id: card, **rest})


def owner() -> str:
    return next(pid for pid, a in AGENTS.items() if a["kind"] == "human")


def by_handle(handle: str) -> str | None:
    h = handle.lower()
    return next((pid for pid, a in AGENTS.items() if a["handle"] == h), None)


def can(principal: str, capability: str, project_id: str | None = None) -> bool:
    a = AGENTS.get(principal)
    if not a:
        return False
    if project_id and "*" not in a["projects"] and project_id not in a["projects"]:
        return False
    return "*" in a["capabilities"] or capability in a["capabilities"]


def is_human(principal: str) -> bool:
    return AGENTS.get(principal, {}).get("kind") == "human"


class Tokens:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.mtime = 0.0
        self.hashes: dict[str, str] = {}
        self.lock = threading.Lock()
        self._reload()

    def _reload(self) -> None:
        # Tokens issued by the CLI while the server runs are picked up on the next request.
        with self.lock:
            if self.path.exists() and (m := self.path.stat().st_mtime) != self.mtime:
                self.hashes = json.loads(self.path.read_text())
                self.mtime = m

    @staticmethod
    def _h(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def issue(self, principal: str) -> str:
        """Create a new token for `principal`, revoking its previous one. Serialised across processes;
        the file is replaced whole, so readers never see a partial one."""
        if principal not in AGENTS or AGENTS[principal]["kind"] == "system":
            raise KeyError(principal)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(".lock"), "a") as lock, self.lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = json.loads(self.path.read_text()) if self.path.exists() else {}
            hashes = {h: p for h, p in current.items() if p != principal}
            token = f"ws_{principal}_{secrets.token_urlsafe(24)}"
            hashes[self._h(token)] = principal
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tokens-")
            with os.fdopen(fd, "w") as f:
                json.dump(hashes, f, indent=1)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            self.hashes, self.mtime = hashes, self.path.stat().st_mtime
        return token

    def who(self, token: str | None) -> str | None:
        self._reload()
        return self.hashes.get(self._h(token)) if token else None


def write_token_file(tokens: "Tokens", data_dir: Path, principal: str) -> Path:
    """Issue a new token for `principal` (revoking the old one) and store it in `<data>/secrets/<principal>.token`.
    The token value is never returned or printed."""
    token = tokens.issue(principal)
    f = Path(data_dir) / "secrets" / f"{principal}.token"
    f.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=f.parent, prefix=".token-")  # created 0600; replaced whole, never half written
    with os.fdopen(fd, "w") as out:
        out.write(token)
    os.replace(tmp, f)
    return f
