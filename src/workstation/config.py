"""Per-installation settings, read from `<data dir>/config.toml` (see config.example.toml).

Every key is optional; a missing file means the defaults below.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .tickets import DEFAULT_CLOSED

DEFAULT_COVERAGE = [
    {"source": "Claude Code (threads, engineering tasks)", "state": "connected", "agent": "claude"},
    {"source": "GPT via Codex CLI (threads, reviews)", "state": "connected", "agent": "gpt"},
    {"source": "Gemini (secretary role)", "state": "not connected", "agent": "gemini"},
    {"source": "Mail and calendar", "state": "not connected; nothing from mail is shown"},
]


@dataclass
class Config:
    owner_id: str = "owner"
    owner_name: str = "Owner"
    timezone: str = "UTC"
    port: int = 8770
    claude_model: str = "sonnet"
    gpt_model: str = "gpt-6-astra"
    gpt_effort: str = "medium"
    tickets_path: str = ""
    tickets_title: str = "Tickets"
    tickets_closed: list[str] = field(default_factory=lambda: list(DEFAULT_CLOSED))
    coverage: list[dict] = field(default_factory=lambda: [dict(c) for c in DEFAULT_COVERAGE])


def load(data_dir: Path | str) -> Config:
    path = Path(data_dir) / "config.toml"
    raw = tomllib.loads(path.read_text()) if path.exists() else {}
    owner, display = raw.get("owner", {}), raw.get("display", {})
    claude, gpt = raw.get("agents", {}).get("claude", {}), raw.get("agents", {}).get("gpt", {})
    cfg = Config()
    cfg.owner_id = owner.get("id", cfg.owner_id)
    cfg.owner_name = owner.get("name", cfg.owner_name)
    cfg.timezone = display.get("timezone", cfg.timezone)
    cfg.port = int(raw.get("server", {}).get("port", cfg.port))
    cfg.claude_model = claude.get("model", cfg.claude_model)
    cfg.gpt_model = gpt.get("model", cfg.gpt_model)
    cfg.gpt_effort = gpt.get("effort", cfg.gpt_effort)
    tk = raw.get("tickets", {})
    cfg.tickets_path = str(tk.get("path", cfg.tickets_path))
    cfg.tickets_title = str(tk.get("title", cfg.tickets_title))
    if "closed" in tk:
        cfg.tickets_closed = [str(x) for x in tk["closed"]]
    if "coverage" in raw:
        cfg.coverage = [{"source": str(c["source"]), "state": str(c["state"]),
                         **({"agent": str(c["agent"])} if "agent" in c else {})} for c in raw["coverage"]]
    validate(cfg, path)
    return cfg


def validate(cfg: Config, path: Path | str = "config") -> None:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", cfg.owner_id) or cfg.owner_id in ("claude", "gpt", "gemini", "system"):
        raise ValueError(f"{path}: owner.id must be lowercase letters, digits, - or _, and not an agent id")
    try:
        ZoneInfo(cfg.timezone)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"{path}: display.timezone {cfg.timezone!r} is not an IANA zone") from e
    if cfg.gpt_effort not in ("low", "medium", "high"):
        raise ValueError(f"{path}: agents.gpt.effort must be low, medium or high")
