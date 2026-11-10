"""`ws` command line: run the server, sign in, issue agent tokens, and act as an agent.

Agents (e.g. a Claude Code session) use `ws inbox / claim / reply / task --as <id>`; the
token for `<id>` is read from `<data>/secrets/<id>.token`, created by `ws token <id>`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from pathlib import Path

import httpx

from . import config as configuration
from .principals import configure_owner, owner

URL = os.environ.get("WS_URL")


def data_dir(args) -> Path:
    return Path(args.data or os.environ.get("WS_DATA", "data"))


def client(args, who: str) -> httpx.Client:
    f = data_dir(args) / "secrets" / f"{who}.token"
    if not f.exists():
        sys.exit(f"no token for {who}: run `ws token {who}`")
    return httpx.Client(base_url=URL or f"http://127.0.0.1:{args.cfg.port}", headers={"Authorization": f"Bearer {f.read_text().strip()}"}, timeout=30)


def out(r: httpx.Response):
    if r.status_code >= 400:
        sys.exit(f"{r.status_code}: {r.text}")
    print(json.dumps(r.json(), indent=1, ensure_ascii=False))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="ws")
    ap.add_argument("--data", help="data directory (default ./data or $WS_DATA)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=None); s.add_argument("--fake", action="store_true")
    sub.add_parser("open")
    s = sub.add_parser("token"); s.add_argument("principal")
    s = sub.add_parser("export"); s.add_argument("path")
    s = sub.add_parser("record"); s.add_argument("decision_id"); s.add_argument("--as", dest="who", default=None)
    s.add_argument("--out", default=None, help="write to this file or directory instead of printing")
    s = sub.add_parser("review"); s.add_argument("--end", default=None, help="last day of the 7-day window, YYYY-MM-DD (default today)")
    s.add_argument("--as", dest="who", default=None); s.add_argument("--out", default=None, help="write to this file or directory instead of printing")
    s = sub.add_parser("backup"); s.add_argument("path", nargs="?")
    s = sub.add_parser("project"); s.add_argument("id"); s.add_argument("name"); s.add_argument("--goal", default="")
    s.add_argument("--stage", default=""); s.add_argument("--milestone", default="")
    s = sub.add_parser("ask", help="submit an item that needs the owner (an agent command)")
    s.add_argument("title"); s.add_argument("--as", dest="who", default=None)
    s.add_argument("--kind", default="question", choices=["decision", "review", "approval", "question"])
    s.add_argument("--text", default="", help="details; '-' reads stdin")
    s.add_argument("--blocked", default="", help="what is blocked until the owner answers")
    s.add_argument("--cost", default="", help="what it costs if this waits")
    s.add_argument("--urgency", default="normal", choices=["low", "normal", "high"])
    s.add_argument("--project", default=None); s.add_argument("--task", default=None); s.add_argument("--url", default=None)
    s.add_argument("--due", default=None, help="ISO 8601 deadline with a UTC offset (2026-11-12T17:00:00-05:00), or without one plus --due-tz"); s.add_argument("--due-tz", default=None)
    s.add_argument("--key", default=None, help="idempotency key: resubmitting with it returns the same item")
    s = sub.add_parser("asks", help="items submitted for the owner and their answers")
    s.add_argument("--as", dest="who", default=None); s.add_argument("--status", default="open", choices=["open", "closed", "all"])
    s.add_argument("--inbox", action="store_true", help="owner only: the whole inbox, most urgent first")
    s = sub.add_parser("answer", help="owner: answer an item")
    s.add_argument("ask_id"); s.add_argument("--as", dest="who", default=None)
    s.add_argument("--resolution", default="answered", choices=["answered", "approved", "declined", "dismissed"])
    s.add_argument("--note", default="")
    s = sub.add_parser("withdraw", help="agent: take back an item it submitted")
    s.add_argument("ask_id"); s.add_argument("--as", dest="who", default=None)
    for name in ("inbox", "claim", "reply", "task", "post", "event"):
        s = sub.add_parser(name); s.add_argument("--as", dest="who", default=None)
        if name in ("claim", "reply"):
            s.add_argument("dispatch")
        if name == "task":
            s.add_argument("task_id"); s.add_argument("--status"); s.add_argument("--note")
            s.add_argument("--commit", help="SHA to link as a commit artifact"); s.add_argument("--repo", help="repo path for --commit")
            s.add_argument("--url", help="URL to link (PR, doc, test run)"); s.add_argument("--kind", default="pr")
            s.add_argument("--title", default="")
        if name == "post":
            s.add_argument("thread_id"); s.add_argument("text")
        if name == "event":
            s.add_argument("json_file", help="normalised event JSON (see docs/events.md)")
    args = ap.parse_args(argv)
    args.cfg = configuration.load(data_dir(args))
    configure_owner(args.cfg.owner_id, args.cfg.owner_name)
    if getattr(args, "who", "") is None:
        args.who = owner()

    if args.cmd == "serve":
        import uvicorn
        from .app import create_app
        runners = None
        if args.fake:
            from .runners import Fake, Unconnected
            fake = lambda name: Fake(lambda p: f"[fake {name}] I read {p.count('[#')} messages.\n```json\n"
                                               '{"position": "Fake position", "options": ["Option A", "Option B"]}\n```')
            runners = {"claude": fake("claude"), "gpt": fake("gpt"), "gemini": Unconnected("not connected")}
        uvicorn.run(create_app(data_dir(args), runners), host="127.0.0.1",
                    port=args.port or int(os.environ.get("PORT", 0)) or args.cfg.port, log_level="warning")
    elif args.cmd == "token":
        from .principals import Tokens
        d = data_dir(args)
        tok = Tokens(d / "tokens.json").issue(args.principal)
        f = d / "secrets" / f"{args.principal}.token"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(tok); f.chmod(0o600)
        print(f"token for {args.principal} written to {f} (previous one revoked)")
    elif args.cmd == "open":
        r = client(args, owner()).post("/api/login-codes")
        if r.status_code >= 400:
            sys.exit(r.text)
        url = (URL or f"http://127.0.0.1:{args.cfg.port}") + r.json()["path"]
        if not webbrowser.open(url):
            print(url)
        print("opened a one-time sign-in link (valid 2 minutes)")
    elif args.cmd == "export":
        from .store import Store
        n = Store(data_dir(args) / "workstation.db").export_jsonl(Path(args.path))
        print(f"{n} events → {args.path}")
    elif args.cmd in ("record", "review"):
        c = client(args, args.who or owner())
        r = c.get(f"/api/decisions/{args.decision_id}/record") if args.cmd == "record" else \
            c.get("/api/review/record", params={"end": args.end} if args.end else None)
        if r.status_code >= 400:
            sys.exit(f"{r.status_code}: {r.text}")
        rec = r.json()
        if args.out:
            dest = Path(args.out)
            if args.out.endswith(("/", os.sep)):  # a trailing slash means a folder, created if missing
                dest.mkdir(parents=True, exist_ok=True)
            dest = dest / rec["filename"] if dest.is_dir() else dest
            dest.write_text(rec["markdown"])
            print(f"written: {dest}")
        else:
            print(rec["markdown"], end="")
    elif args.cmd == "backup":
        import time as _t
        from .store import Store
        d = data_dir(args)
        dest = Path(args.path) if args.path else d / "backups" / f"workstation-{_t.strftime('%Y%m%d-%H%M%S')}.db"
        print(f"backup written and replay-checked: {Store(d / 'workstation.db').backup(dest)}")
    elif args.cmd == "project":
        out(client(args, owner()).post("/api/projects", json={"id": args.id, "name": args.name, "goal": args.goal,
                                                               "stage": args.stage, "milestone": args.milestone}))
    elif args.cmd == "inbox":
        out(client(args, args.who).get("/api/dispatches"))
    elif args.cmd == "claim":
        r = client(args, args.who).post(f"/api/dispatches/{args.dispatch}/claim")
        if r.status_code >= 400:
            sys.exit(f"{r.status_code}: {r.text}")
        claims = data_dir(args) / "claims"
        claims.mkdir(parents=True, exist_ok=True)
        (claims / f"{args.dispatch}.attempt").write_text(str(r.json()["attempt"]))
        print(r.json()["prompt"])
    elif args.cmd == "reply":
        text = sys.stdin.read()
        held = data_dir(args) / "claims" / f"{args.dispatch}.attempt"
        if not held.exists():
            sys.exit(f"no claim recorded for {args.dispatch}: run `ws claim {args.dispatch}` first")
        out(client(args, args.who).post(f"/api/dispatches/{args.dispatch}/reply",
                                        json={"text": text, "attempt": int(held.read_text()),
                                              "runtime": {"runtime": "claude-code-session"}}))
        held.unlink()
    elif args.cmd == "task":
        c = client(args, args.who)
        if args.commit or args.url:
            body = {"kind": "commit" if args.commit else args.kind, "title": args.title or (args.commit or args.url),
                    "ref": args.commit, "url": args.url, "repo_path": str(Path(args.repo).resolve()) if args.repo else None}
            out(c.post(f"/api/tasks/{args.task_id}/artifacts", json=body))
        if args.status or args.note:
            changes = {k: v for k, v in {"status": args.status, "note": args.note}.items() if v}
            out(c.patch(f"/api/tasks/{args.task_id}", json={"changes": changes}))
    elif args.cmd == "ask":
        body = {"title": args.title, "kind": args.kind, "text": sys.stdin.read() if args.text == "-" else args.text,
                "blocked": args.blocked, "cost_of_waiting": args.cost, "urgency": args.urgency,
                "project_id": args.project, "task_id": args.task, "url": args.url, "due_at": args.due,
                "due_tz": args.due_tz, "idempotency_key": args.key}
        out(client(args, args.who).post("/api/asks", json=body))
    elif args.cmd == "asks":
        c = client(args, args.who)
        out(c.get("/api/inbox") if args.inbox else c.get("/api/asks", params={"status": args.status}))
    elif args.cmd == "answer":
        out(client(args, args.who).post(f"/api/asks/{args.ask_id}/answer",
                                        json={"resolution": args.resolution, "note": args.note}))
    elif args.cmd == "withdraw":
        out(client(args, args.who).post(f"/api/asks/{args.ask_id}/withdraw"))
    elif args.cmd == "post":
        out(client(args, args.who).post(f"/api/threads/{args.thread_id}/messages", json={"text": args.text}))
    elif args.cmd == "event":
        out(client(args, args.who).post("/api/events", json=json.loads(Path(args.json_file).read_text())))


if __name__ == "__main__":
    main()
