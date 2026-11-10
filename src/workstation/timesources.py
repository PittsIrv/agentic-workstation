"""Where the owner's time went, from sources that already exist on their machine.

A source implements `TimeSource.read(start, end)` and returns a `TimeResult`: a state, a message that says how to
connect when it is not connected, and per-day totals. Sources are read at request time and nothing is stored: no
time data enters the event log or the repository. Missing days are unknown, never zero.

The first source is Daybook, read through its own loopback dashboard (`/api/health`, `/api/data`). That needs no
macOS permission from this program and gives the same labels and overlap handling as Daybook's own screens. Only
day totals and the Focus/Entertainment/Other/Untagged split are read; app names and calendar titles are discarded.
"""
from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Protocol
from urllib.parse import urlsplit

import httpx

PURPOSES = ("Focus", "Entertainment", "Other", "Untagged")
MAX_BODY = 8_000_000
MAX_DAY_SECONDS = 86_400 * 2  # a day cannot hold more than a day of use per scope; a little slack for overlap
DEFAULT_DAYBOOK_URL = "http://127.0.0.1:8765"


@dataclass
class DayTotal:
    day: str
    seconds: int
    by_purpose: dict[str, int]
    complete: bool | None = None


@dataclass
class TimeResult:
    source: str
    state: str  # connected | not_connected | unreadable | off
    message: str = ""
    scope: str | None = None
    scopes: list[str] = field(default_factory=list)
    demo: bool = False
    days: list[DayTotal] = field(default_factory=list)


class TimeSource(Protocol):
    id: str
    label: str

    def read(self, start: date, end: date) -> TimeResult: ...


class Off:
    id, label = "off", "Time source"

    def read(self, start: date, end: date) -> TimeResult:
        return TimeResult("off", "off", "Time tracking is switched off. Set [time] source = \"daybook\" in data/config.toml to read Daybook.")


def check_loopback(url: str) -> str:
    """Only a loopback http address is accepted, so a config typo cannot send requests to another machine."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if parts.scheme != "http" or not local or parts.username or parts.password or parts.path not in ("", "/"):
        raise ValueError(f"time.daybook_url must be http://127.0.0.1:<port> (a loopback address), got {url!r}")
    return f"http://{parts.netloc}"


def _num(v) -> int | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v < 0 or v > MAX_DAY_SECONDS:
        return None
    return int(v)


class Daybook:
    id, label = "daybook", "Daybook"
    HOW = ("Daybook is not connected. Install it from https://github.com/PittsIrv/daybook, then in its folder run "
           "`python3 dashboard/launch.py` and keep it running. If you changed its port, set [time] daybook_url in data/config.toml.")

    def __init__(self, url: str = DEFAULT_DAYBOOK_URL, scope: str = "", timeout: float = 2.0,
                 fetch: Callable[[str], object] | None = None):
        self.url, self.scope, self.timeout = check_loopback(url), scope, timeout
        self._fetch = fetch or self._http

    def _http(self, path: str):
        with httpx.Client(timeout=self.timeout, follow_redirects=False) as c, c.stream("GET", self.url + path) as r:
            r.raise_for_status()
            body = b""
            for chunk in r.iter_bytes():  # stop reading at the cap: a wrong port must not fill memory
                body += chunk
                if len(body) > MAX_BODY:
                    raise ValueError("response too large")
            return json.loads(body)

    def read(self, start: date, end: date) -> TimeResult:
        try:
            health = self._fetch("/api/health")
        except (httpx.HTTPError, OSError):
            return TimeResult(self.id, "not_connected", self.HOW)
        except ValueError:
            return TimeResult(self.id, "unreadable", "Something answered on the Daybook address but it is not Daybook.")
        if not isinstance(health, dict) or health.get("application") != "daybook":
            return TimeResult(self.id, "unreadable", "Something answered on the Daybook address but it is not Daybook.")
        try:
            data = self._fetch("/api/data")
            rows = data["days"]
            if not isinstance(rows, list):
                raise TypeError("days")
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError):
            return TimeResult(self.id, "unreadable", "Daybook is running but its data could not be read. Update Daybook or check its page.")
        demo = health.get("demo") is True or (isinstance(data, dict) and data.get("demo") is True)
        by_scope: dict[str, dict[str, DayTotal]] = {}
        for d in rows:
            if not isinstance(d, dict):
                continue
            scope, day = d.get("device_scope"), d.get("date")
            try:
                day = date.fromisoformat(day).isoformat()  # canonical YYYY-MM-DD, so string order is date order
            except (TypeError, ValueError):
                continue
            total = _num(d.get("total_seconds"))
            purposes = d.get("purpose_seconds") if isinstance(d.get("purpose_seconds"), dict) else {}
            split = {p: (_num(purposes.get(p)) or 0) for p in PURPOSES}
            if not isinstance(scope, str) or total is None:
                continue
            by_scope.setdefault(scope[:80], {})[day] = DayTotal(
                day, total, split, d.get("complete") if isinstance(d.get("complete"), bool) else None)
        scopes = sorted(by_scope)
        if not scopes:
            return TimeResult(self.id, "connected", "Daybook has no recorded days yet. It records finished days, starting the morning after its first collection.", demo=demo)
        # One scope only: different devices or sources can measure the same use, so they are never added together.
        chosen = self.scope if self.scope in by_scope else max(scopes, key=lambda s: (max(by_scope[s]), s))
        note = "" if not self.scope or self.scope in by_scope else \
            f"Scope {self.scope!r} not found in Daybook; showing {chosen!r}."
        days = [t for k, t in sorted(by_scope[chosen].items()) if start <= date.fromisoformat(k) <= end]
        return TimeResult(self.id, "connected", note, scope=chosen, scopes=scopes, demo=demo, days=days)


def window(source: TimeSource, end: date, days: int = 7) -> dict:
    """The `days` days ending on `end`: per-day totals with explicit gaps, and totals over recorded days only."""
    start = end - timedelta(days=days - 1)
    res = source.read(start, end)
    have = {d.day: d for d in res.days}
    rows = []
    for i in range(days):
        k = (start + timedelta(days=i)).isoformat()
        d = have.get(k)
        rows.append({"date": k, "recorded": d is not None, "seconds": d.seconds if d else None,
                     "by_purpose": d.by_purpose if d else None, "complete": d.complete if d else None})
    got = [r for r in rows if r["recorded"]]
    return {"source": source.id, "state": res.state, "message": res.message, "scope": res.scope, "scopes": res.scopes,
            "demo": res.demo, "start": start.isoformat(), "end": end.isoformat(), "days": rows,
            "recorded_days": len(got), "missing_days": days - len(got),
            "total_seconds": sum(r["seconds"] for r in got),
            "by_purpose": {p: sum(r["by_purpose"][p] for r in got) for p in PURPOSES},
            "today": next((r for r in rows if r["date"] == end.isoformat()), None),
            "latest_recorded": max((r["date"] for r in got), default=None),
            "notes": ["Daybook records finished days: today is normally missing until tomorrow morning.",
                      "Missing days are unknown, not zero; totals cover recorded days only.",
                      "Focus is a label you gave an app, not measured productivity."]}
