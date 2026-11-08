"""Decision records: exact format, API and CLI access, and hostile text in the exported Markdown."""
from pathlib import Path

import pytest
from markdown_it import MarkdownIt

from conftest import H
from workstation.export import render_record, slug
from workstation.principals import AGENTS
from test_browser_security import BAD_URL, HOSTILE, PAYLOADS, Dom

GOLDEN = Path(__file__).parent / "fixtures" / "decision-record.md"
NAMES = {"owner": "Owner", "claude": "Claude", "gpt": "GPT"}

DECISION = {
    "id": "dec_1", "question": "Should the dashboard move to event-driven updates?",
    "context": "Polling every 5 seconds is simple but wasteful.", "status": "approved", "option_id": "o1",
    "options": [{"id": "o1", "label": "Server-sent events", "detail": "Proposed by Claude and GPT"},
                {"id": "o2", "label": "Keep polling", "detail": "Proposed by GPT"}],
    "recommendations": [{"agent": "claude", "summary": "Server-sent events: one-way push is enough here."},
                        {"agent": "gpt", "summary": "Server-sent events, but keep polling as a fallback."}],
    "evidence": [{"label": "Load test", "url": "https://example.com/load-test"}],
    "note": "Push is simpler for a read-only dashboard.", "revisit": "If we need two-way messaging.",
    "resolved_by": "owner", "resolved_at": "2026-11-07T17:22:00+00:00",
}
TASKS = [{"status": "done", "title": "Implement: Server-sent events", "owner": "claude"}]


def record(d=DECISION, tasks=TASKS):
    return render_record(d, tasks, "General", "America/New_York", lambda who: NAMES.get(who, who))


def test_record_matches_the_documented_format():
    r = record()
    assert r["filename"] == "2026-11-07-should-the-dashboard-move-to-event-driven-updates.md"
    assert r["markdown"] == GOLDEN.read_text()


def test_sections_are_left_out_when_empty():
    d = {**DECISION, "context": "", "evidence": [], "note": "", "revisit": "", "recommendations": []}
    md = record(d, [])["markdown"]
    for gone in ("## Context", "## Evidence", "Reason:", "## Linked tasks", "## Revisit when"):
        assert gone not in md
    assert "_None recorded._" in md


def test_rejected_and_deferred_records():
    assert "**Rejected:** none of the options." in record({**DECISION, "status": "rejected", "option_id": None})["markdown"]
    assert "**Deferred.**" in record({**DECISION, "status": "deferred", "option_id": None})["markdown"]


def test_slug():
    assert slug("Use SQLite, not Postgres!") == "use-sqlite-not-postgres"
    assert slug("日本語") == "decision"
    assert len(slug("x" * 200)) <= 60


# ---- API and CLI ----------------------------------------------------------------------------------

def resolved(c, tokens, **extra):
    q = {"question": "Cache the home view?", "options": [{"label": "Yes"}, {"label": "No"}], "project_id": "workstation"}
    d = c.post("/api/decisions", headers=H(tokens), json=q).json()
    c.post(f"/api/decisions/{d['id']}/resolve", headers=H(tokens),
           json={"status": "approved", "option_id": d["options"][0]["id"], **extra})
    return d["id"]


def test_record_is_read_only_for_every_identity_and_refused_while_pending(ws):
    app, c, tokens = ws
    q = {"question": "Later?", "options": [{"label": "a"}, {"label": "b"}], "project_id": "workstation"}
    pending = c.post("/api/decisions", headers=H(tokens), json=q).json()["id"]
    assert c.get(f"/api/decisions/{pending}/record", headers=H(tokens)).status_code == 409
    assert c.get("/api/decisions/nope/record", headers=H(tokens)).status_code == 404
    did = resolved(c, tokens, note="Cheap to try", revisit="After the next release")
    for who in ("owner", "claude", "gpt"):
        r = c.get(f"/api/decisions/{did}/record", headers=H(tokens, who))
        assert r.status_code == 200 and "Cheap to try" in r.json()["markdown"]
        assert "After the next release" in r.json()["markdown"] and "## Linked tasks" in r.json()["markdown"]
    assert c.get(f"/api/decisions/{did}/record").status_code == 401
    # an agent token still cannot resolve or change anything through this route
    assert c.post(f"/api/decisions/{did}/record", headers=H(tokens, "claude")).status_code == 405


def test_cli_prints_and_writes_the_record(ws, tmp_path, monkeypatch, capsys):
    app, c, tokens = ws
    did = resolved(c, tokens)
    from workstation import cli
    import httpx
    monkeypatch.setattr(cli, "client", lambda args, who: type("C", (), {
        "get": staticmethod(lambda path, **kw: c.get(path, headers=H(tokens, who), **kw))})())
    cli.main(["--data", str(app.state.ws.data), "record", did])
    printed = capsys.readouterr().out
    assert printed.startswith("# Cache the home view?")
    cli.main(["--data", str(app.state.ws.data), "record", did, "--out", str(tmp_path)])
    assert (tmp_path / next(p.name for p in tmp_path.glob("*cache-the-home-view.md"))).read_text() == printed


# ---- hostile text ---------------------------------------------------------------------------------

LINE_STARTS = ["# heading", "## sub", "- bullet", "+ plus", "1. numbered", "> quote", "```\ncode\n```", "~~~\nx\n~~~",
               "| a | b |\n|---|---|", "[ref]: javascript:alert(1)", "    indented code", "![img](https://evil.test/x.png)",
               "<https://evil.test>", "[trusted](https://evil.test)", "===", "---", "&#x3C;script&#x3E;alert(1)&#x3C;/script&#x3E;"]
HOSTILE_ALL = " ".join(PAYLOADS) + "\n" + "\n".join(LINE_STARTS)
ALLOWED_TAGS = {"h1", "h2", "p", "ul", "li", "strong", "em", "a"}


def hostile_record():
    d = {**DECISION, "question": HOSTILE_ALL, "context": HOSTILE_ALL, "note": HOSTILE_ALL, "revisit": HOSTILE_ALL,
         "options": [{"id": "o1", "label": HOSTILE_ALL, "detail": HOSTILE_ALL}, {"id": "o2", "label": "Safe"}],
         "recommendations": [{"agent": "claude", "summary": HOSTILE_ALL}, {"agent": "gpt", "summary": HOSTILE_ALL}],
         "evidence": [{"label": HOSTILE_ALL, "url": BAD_URL}, {"label": "plain", "url": "javascript:alert(1)"}]}
    t = [{"status": "ready", "title": HOSTILE_ALL, "owner": "claude"}]
    return render_record(d, t, HOSTILE_ALL, "UTC", lambda who: HOSTILE_ALL if who == "owner" else NAMES.get(who, who))


def rendered_html():
    return MarkdownIt("commonmark", {"html": True}).render(hostile_record()["markdown"])  # worst case: raw HTML allowed


def test_hostile_text_cannot_inject_markup_into_a_rendered_record():
    dom = Dom()
    dom.feed(rendered_html())
    assert set(dom.tags) <= ALLOWED_TAGS, f"unexpected elements: {set(dom.tags) - ALLOWED_TAGS}"
    assert not [a for a in dom.attrs if a.startswith("on")]
    assert not [u for u in dom.urls if u.startswith(("javascript:", "data:", "vbscript:"))]


def test_hostile_text_cannot_add_sections_or_links_to_a_rendered_record():
    dom = Dom()
    dom.feed(rendered_html())
    assert dom.tags.count("h1") == 1
    sections = 8  # Question, Context, Positions, Options considered, Evidence, Decision, Linked tasks, Revisit when
    assert hostile_record()["markdown"].count("\n## ") == dom.tags.count("h2") == sections
    assert dom.tags.count("a") == 1, "only the one real evidence link may be a link"
    assert dom.urls[0].startswith("https://example.test/")
    assert "evil.test" not in "".join(dom.urls)


def test_hostile_text_is_still_readable_in_the_record():
    md = hostile_record()["markdown"]
    assert "onerror" in md and "javascript:" in md  # shown as text, not dropped


def test_escaped_numbered_line_shows_no_stray_backslash():
    from workstation.export import md
    assert md("1. numbered") == "1\\. numbered"
    html = MarkdownIt("commonmark", {"html": True}).render(md("1. numbered") + "\n\n" + md("2) two"))
    assert "<ol" not in html and "\\" not in html and "1. numbered" in html and "2) two" in html


def test_decided_by_shows_the_owner_display_name(ws):
    app, c, tokens = ws
    did = resolved(c, tokens)
    assert "**Decided by:** Owner" in c.get(f"/api/decisions/{did}/record", headers=H(tokens)).json()["markdown"]
