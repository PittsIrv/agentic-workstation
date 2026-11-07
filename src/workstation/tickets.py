"""Read-only view of a Markdown ticket file.

Expected shape: one block per ticket, `## <id> · <title>` followed by `- key: value` lines.
Fenced code blocks are skipped (templates often live there). The file is only ever opened for reading.
"""
from __future__ import annotations

import re
from pathlib import Path

HEADING = re.compile(r"^##\s+([A-Za-z]+-\d+)\s*[·\-:–—]\s*(.+?)\s*$")
FIELD = re.compile(r"^-\s+([A-Za-z][\w-]*):\s*(.*?)\s*$")
FIELDS = ("status", "repo", "size", "priority", "opened", "worker")
DEFAULT_CLOSED = ("done", "closed", "canceled", "cancelled")
MAX_BYTES = 2_000_000
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def parse(text: str) -> list[dict]:
    tickets: list[dict] = []
    cur: dict | None = None
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        if m := HEADING.match(line):
            cur = {"id": m[1], "title": m[2], **{k: "" for k in FIELDS}}
            tickets.append(cur)
        elif line.startswith("## "):
            cur = None
        elif cur is not None and (f := FIELD.match(line)) and f[1].lower() in FIELDS and not cur[f[1].lower()]:
            cur[f[1].lower()] = re.sub(r"\s+#\s.*$", "", f[2])  # trailing "# comment"
    for t in tickets:
        t["state"] = (t["status"].split() or ["unknown"])[0].lower()
        t["priority"] = (t["priority"].split() or [""])[0]
    return tickets


class TicketFile:
    """The configured ticket file, re-read when its modification time changes."""

    def __init__(self, path: str | None, closed: tuple[str, ...] = DEFAULT_CLOSED, title: str = "Tickets"):
        self.path = Path(path).expanduser() if path else None
        self.closed, self.title = tuple(c.lower() for c in closed), title
        self._key: tuple | None = None
        self._tickets: list[dict] = []

    def read(self) -> dict:
        if not self.path:
            return {"state": "not_configured", "title": self.title, "open": [], "total": 0}
        try:
            st = self.path.stat()
            if st.st_size > MAX_BYTES:
                raise ValueError("file is larger than 2 MB")
            if self._key != (st.st_mtime_ns, st.st_size):
                with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                    self._tickets = parse(f.read())
                self._key = (st.st_mtime_ns, st.st_size)
        except (OSError, ValueError) as e:
            return {"state": "unavailable", "title": self.title, "reason": e.__class__.__name__ if isinstance(e, OSError) else str(e),
                    "open": [], "total": 0}
        live = [t for t in self._tickets if t["state"] not in self.closed]
        live.sort(key=lambda t: (PRIORITY_ORDER.get(t["priority"], 4), t["id"]))
        return {"state": "ok", "title": self.title, "total": len(self._tickets), "open": live}
