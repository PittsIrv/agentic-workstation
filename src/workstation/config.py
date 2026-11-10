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


# The first reminder rule. Every number is this project's own placeholder, not a researched value.
REMINDER_DEFAULTS = {
    "wait_minutes_high": 30,       # minutes an item may wait before it is due, by urgency
    "wait_minutes_normal": 240,
    "wait_minutes_low": 1440,
    "deadline_window_minutes": 1440,  # a deadline closer than this makes an item due
    "quiet_start": "21:00",        # no reminder from this local time ...
    "quiet_end": "08:00",          # ... until this one (may cross midnight)
    "daily_cap": 3,                # reminders recorded per local day
    "repeat_minutes": 1440,        # the same item is not reminded again inside this
    "acted_window_minutes": 120,   # the reminder log counts an item as acted on if answered within this
}


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
    inbox_max_open_per_agent: int = 20  # open items one agent may have waiting on the owner
    time_source: str = "daybook"  # daybook | off
    daybook_url: str = "http://127.0.0.1:8765"
    daybook_scope: str = ""
    reminders: dict = field(default_factory=lambda: dict(REMINDER_DEFAULTS))
    reminders_set: list[str] = field(default_factory=list)  # keys the config file changed from the defaults
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
    cfg.inbox_max_open_per_agent = raw.get("inbox", {}).get("max_open_per_agent", cfg.inbox_max_open_per_agent)
    tm = raw.get("time", {})
    cfg.time_source = str(tm.get("source", cfg.time_source))
    cfg.daybook_url = str(tm.get("daybook_url", cfg.daybook_url))
    cfg.daybook_scope = str(tm.get("daybook_scope", cfg.daybook_scope))
    rm = raw.get("reminders", {})
    unknown = sorted(set(rm) - set(REMINDER_DEFAULTS))
    if unknown:
        raise ValueError(f"{path}: unknown [reminders] keys {unknown}; known: {sorted(REMINDER_DEFAULTS)}")
    cfg.reminders = {**REMINDER_DEFAULTS, **rm}
    cfg.reminders_set = sorted(k for k in rm if rm[k] != REMINDER_DEFAULTS[k])
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
    if not (isinstance(cfg.inbox_max_open_per_agent, int) and not isinstance(cfg.inbox_max_open_per_agent, bool)
            and 1 <= cfg.inbox_max_open_per_agent <= 1000):
        raise ValueError(f"{path}: inbox.max_open_per_agent must be a whole number from 1 to 1000")
    r = cfg.reminders
    for k, v in r.items():
        if k in ("quiet_start", "quiet_end"):
            if not (isinstance(v, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v)):
                raise ValueError(f"{path}: reminders.{k} must be a time like 21:00")
        elif not (isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100000):
            raise ValueError(f"{path}: reminders.{k} must be a whole number from 0 to 100000")
    if r["repeat_minutes"] < 1 or r["wait_minutes_high"] < 0:
        raise ValueError(f"{path}: reminders.repeat_minutes must be at least 1")
    if cfg.time_source not in ("daybook", "off"):
        raise ValueError(f"{path}: time.source must be daybook or off")
    from .timesources import check_loopback
    check_loopback(cfg.daybook_url)
    if cfg.gpt_effort not in ("low", "medium", "high"):
        raise ValueError(f"{path}: agents.gpt.effort must be low, medium or high")
