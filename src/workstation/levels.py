"""Levels: an agent's level comes from the model it runs on, through an editable table.

The shipped table is `levels.toml` next to this file; `[levels]` in the installation's config.toml overrides single
entries or the default for unknown models. A level is never set per agent. The owner is above every level.

Rules enforced elsewhere with these levels: an agent may assign work only to agents at its own level or lower; a
strictly higher level than every other participant may close a discussion as overruled; agents at the same level are
peers. Levels never open an owner-only action (accepting, cancelling or reassigning tasks, answering, settings).
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from .principals import AGENTS, is_human

OWNER_LEVEL = 10**6
SHIPPED = Path(__file__).with_name("levels.toml")
MAX_LEVEL = 99


def normalise(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(model).lower()).strip("-")


def check_table(models: dict, default: object, where: str) -> None:
    for k, v in {**models, "default": default}.items():
        if not (isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= MAX_LEVEL):
            raise ValueError(f"{where}: level for {k!r} must be a whole number from 1 to {MAX_LEVEL}")
    for k in models:
        if not normalise(k):
            raise ValueError(f"{where}: empty model name in the levels table")


class Levels:
    def __init__(self, models: dict[str, int] | None = None, default: int | None = None):
        shipped = tomllib.loads(SHIPPED.read_text())
        merged = {normalise(k): v for k, v in shipped["models"].items()}
        merged.update({normalise(k): v for k, v in (models or {}).items()})
        self.models = merged
        self.default = shipped["default"] if default is None else default
        check_table(self.models, self.default, "levels")

    def of_model(self, model: str | None) -> int:
        name = normalise(model or "")
        if not name:
            return self.default
        if name in self.models:
            return self.models[name]
        best = max((k for k in self.models if name.startswith(k + "-")), key=len, default=None)
        return self.models[best] if best else self.default

    def of(self, pid: str) -> int:
        """The level of an identity: the owner is above all, the system has none, an agent follows its model."""
        a = AGENTS.get(pid)
        if a is None or a["kind"] == "system":
            return 0
        if is_human(pid):
            return OWNER_LEVEL
        return self.of_model(a.get("model"))

    def table(self) -> dict:
        return {"default": self.default, "models": dict(sorted(self.models.items(), key=lambda kv: (-kv[1], kv[0])))}
