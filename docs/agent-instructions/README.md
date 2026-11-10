# Teach your agents to ask you

Copy one of these into the agent's instructions, replace `WORKSTATION_DIR` with the folder you cloned, and run
`uv run ws token <id>` there once (`gpt` for Codex, `claude` for Claude Code). Keep `uv run ws serve` running.

| Agent | Copy | Into |
|---|---|---|
| Codex | [AGENTS.snippet.md](AGENTS.snippet.md) | the `AGENTS.md` of a project, or `~/.codex/AGENTS.md` |
| Claude Code | [claude-code-skill/](claude-code-skill/) | `~/.claude/skills/ask-the-person/` |
| Claude Code (without skills) | [CLAUDE.snippet.md](CLAUDE.snippet.md) | the `CLAUDE.md` of a project |

An agent's token is a file the agent can read. A sandboxed agent that cannot read it, or cannot reach
`127.0.0.1:8770`, cannot submit; the snippets tell it to say so. Nothing here runs an agent for you.
