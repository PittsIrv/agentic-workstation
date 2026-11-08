# Should the dashboard move to event-driven updates?

- **Date:** 2026-11-07
- **Status:** approved
- **Decided by:** Owner
- **Project:** General

## Question

Should the dashboard move to event-driven updates?

## Context

Polling every 5 seconds is simple but wasteful.

## Positions

- **Claude:** Server-sent events: one-way push is enough here.
- **GPT:** Server-sent events, but keep polling as a fallback.

## Options considered

- **Server-sent events** — Proposed by Claude and GPT _(chosen)_
- **Keep polling** — Proposed by GPT

## Evidence

- [Load test](<https://example.com/load-test>)

## Decision

**Approved:** Server-sent events.

Reason:

Push is simpler for a read-only dashboard.

## Linked tasks

- [done] Implement: Server-sent events (owner Claude)

## Revisit when

If we need two-way messaging.
