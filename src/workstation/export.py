"""A resolved decision as a Markdown decision record.

The text comes from agents and threads, so every value is escaped before it is placed in Markdown:
inline markup, raw HTML, entities, line-start constructs (headings, lists, quotes, tables, fences) and
link syntax are neutralized, and only http(s) links are written. A bare http(s) address in text may still
be linked by a renderer; it is shown as the address it is, not hidden behind a label.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .service import Conflict, NotFound

_INLINE = re.compile(r"[\\`*_\[\]&<>~]")
_LINE_START = re.compile(r"^(#|[-+=|:]|\d+[.)])")
_NUMBERED = re.compile(r"^(\d+)([.)])")


def _inline(text: str) -> str:
    return _INLINE.sub(lambda m: {"&": "&amp;", "<": "&lt;", ">": "&gt;"}.get(m.group(), "\\" + m.group()), text)


def _start(line: str) -> str:
    """Stop a line from opening a heading, list, quote or table. Only ASCII punctuation can be backslash-escaped,
    so a numbered start escapes its dot or bracket, not the digit."""
    if _NUMBERED.match(line):
        return _NUMBERED.sub(lambda m: m.group(1) + "\\" + m.group(2), line, count=1)
    return "\\" + line if _LINE_START.match(line) else line


def md(text: object) -> str:
    """One line of untrusted text, safe to place in Markdown."""
    return _start(_inline(" ".join(str(text or "").split())))


def block(text: object) -> str:
    """Untrusted multi-line text as paragraphs: no indented code, no line can start a Markdown construct."""
    lines = []
    for raw in str(text or "").replace("\r\n", "\n").split("\n"):
        lines.append(_start(_inline(raw.strip())))
    return "\n".join(lines).strip()


def link(label: object, url: object) -> str:
    """A web link, or just the label when the address is not an http(s) URL."""
    url = str(url or "")
    if not re.match(r"^https?://", url):
        return md(label or url)
    safe = quote(url, safe="/:?#@!$&'*+,;=%-._~[]")
    return f"[{md(label or url)}](<{safe}>)"


def slug(title: str) -> str:
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")[:60].strip("-") or "decision"


def record_filename(d: dict, tz: str) -> str:
    when = datetime.fromisoformat(d["resolved_at"]).astimezone(ZoneInfo(tz)).date().isoformat()
    return f"{when}-{slug(d['question'])}.md"


def render_record(d: dict, tasks: list[dict], project: str, tz: str, name) -> dict:
    """`name` maps an identity to its display name. Returns {"filename", "markdown"}."""
    when = datetime.fromisoformat(d["resolved_at"]).astimezone(ZoneInfo(tz)).date().isoformat()
    chosen = next((o for o in d["options"] if o["id"] == d.get("option_id")), None)
    out = [f"# {md(d['question'])}", "",
           f"- **Date:** {when}", f"- **Status:** {md(d['status'])}",
           f"- **Decided by:** {md(name(d['resolved_by']))}", f"- **Project:** {md(project)}", "",
           "## Question", "", block(d["question"]), ""]
    if d.get("context"):
        out += ["## Context", "", block(d["context"]), ""]
    out += ["## Positions", ""]
    out += [f"- **{md(name(r['agent']))}:** {md(r['summary'])}" for r in d["recommendations"]] or ["_None recorded._"]
    out += ["", "## Options considered", ""]
    for o in d["options"]:
        detail = f" — {md(o['detail'])}" if o.get("detail") else ""
        out.append(f"- **{md(o['label'])}**{detail}{' _(chosen)_' if o is chosen else ''}")
    if d.get("evidence"):
        out += ["", "## Evidence", ""]
        out += [f"- {link(e.get('label') or e.get('title'), e.get('url'))}" for e in d["evidence"]]
    verdict = {"approved": f"**Approved:** {md(chosen['label']) if chosen else ''}.",
               "rejected": "**Rejected:** none of the options.", "deferred": "**Deferred.**"}.get(
        d["status"], f"**{md(d['status'])}.**")
    out += ["", "## Decision", "", verdict]
    if d.get("note"):
        out += ["", "Reason:", "", block(d["note"])]
    if tasks:
        out += ["", "## Linked tasks", ""]
        out += [f"- [{md(t['status'])}] {md(t['title'])} (owner {md(name(t['owner']))})" for t in tasks]
    if d.get("revisit"):
        out += ["", "## Revisit when", "", block(d["revisit"])]
    return {"filename": f"{when}-{slug(d['question'])}.md", "markdown": "\n".join(out) + "\n"}


def decision_record(svc, decision_id: str, tz: str, name) -> dict:
    d = svc.s.decisions.get(decision_id)
    if not d:
        raise NotFound(f"decision {decision_id}")
    if d["status"] == "pending":
        raise Conflict("decision is still pending; resolve it first")
    tasks = [t for t in svc.s.tasks.values() if t.get("decision_id") == decision_id]
    project = svc.s.projects.get(d["project_id"], {}).get("name") or d["project_id"]
    return render_record(d, tasks, project, tz, name)
