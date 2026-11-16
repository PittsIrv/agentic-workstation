# Handoff: next worker

State: structured asks and the decision-queue Home are merged to main. Next work, one PR each, in this order:

1. Blocking `wait` for an agent's answer (CLI `ws wait <ask-id> --timeout N` and a long-poll endpoint), own items only, with a timeout; tests for timeout, answer arrival, another agent's id (same "not found" as unknown).
2. Seeded demo with synthetic asks, one command, so Home can be tried end to end. Measure: time from an agent's ask to the owner's answer reaching the agent, in the demo.
3. `ws mcp` (stdio) exposing submit, wait, list-my-items-and-answers, withdraw on the same API and tokens. First verify from each tool's current official docs (Claude Code, Codex, Gemini CLI, Muse Code) that local stdio MCP is supported; add copy-ready setup text for Gemini and Muse beside the existing ones; state per tool "verified against docs" vs "actually run". Stop and report if Claude Code and Codex share no method.
4. Home groups open items by project or label; then the design page (source in `docs/`, rendered): reading a board and billing data from local files named in config (read-only, file contract), how an off-machine agent could reach the workspace (options, exposure, cost, a recommendation), and whether the server should start at login.

Rules: public repo, no personal data or ticket numbers in tracked files, $0, localhost only, no real agent calls, temp data dirs only (never the live server or its data), feature branch, PR, CI green on Linux and macOS, ship request to the Router, merge after its go, delete the branch after merge. Add a `docs/whats-new.md` entry and a `docs/decisions/` record per PR.

Facts learned: stored times have one-second resolution (`since` is inclusive); the agent table is process-wide, so a second app in one test process must be built after tests of the first; deleting a stacked PR's base branch closes the PR; the `ship` skill's Gemini review was skipped for #23/#25 because the Router reviewed.
