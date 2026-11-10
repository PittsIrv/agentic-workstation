"""Who can act, and what each identity may do.

Identities are stable ids, never display names. Each identity authenticates with its own
bearer token; tokens are stored only as SHA-256 hashes in `<data>/tokens.json`.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
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
