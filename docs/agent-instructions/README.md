# Teach your agents to ask you

The Set up screen of the workstation shows these with your folders already filled in. To copy them from here instead, replace `WORKSTATION_DIR` with the folder you cloned, and run
`uv run ws token <id>` there once (`gpt` for Codex, `claude` for Claude Code). Keep `uv run ws serve` running.

| Agent | Copy | Into |
|---|---|---|
| Codex | [AGENTS.snippet.md](AGENTS.snippet.md) | the `AGENTS.md` of a project, or `~/.codex/AGENTS.md` |
| Claude Code | [claude-code-skill/](claude-code-skill/) | `~/.claude/skills/ask-the-person/` |
| Claude Code (without skills) | [CLAUDE.snippet.md](CLAUDE.snippet.md) | the `CLAUDE.md` of a project |

Each snippet teaches the same loop: submit an item (`ws ask`), carry on with other work, and read the answer later
(`ws asks`) or block on it when nothing else can proceed (`ws wait <ask-id> --as <id> --timeout <seconds>`: exit
status 0 answered, 3 still open, 4 withdrawn, 1 error).

An agent's token is a file the agent can read. A sandboxed agent that cannot read it, or cannot reach
`127.0.0.1:8770`, cannot submit; the snippets tell it to say so. Nothing here runs an agent for you.
