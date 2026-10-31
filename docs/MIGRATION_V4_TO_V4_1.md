# Migration: v4.0 → v4.1

> **Historical migration note.** This document stops at v4.1. Its `--by TEAM`
> example does not satisfy current v5.1 named-human approval contracts. For a
> current run, continue with
> [`../MIGRATION_V5_TO_V5.1.md`](../MIGRATION_V5_TO_V5.1.md) and replace review
> placeholders with a concrete team member.

v4.1 keeps the v4 runtime-neutral research core and adds the missing manuscript production layer.

## What stays compatible

- existing `runs/` research artifacts;
- G0–G5 state and approvals;
- Problem Graph, Idea/Module Cards, experiments and evidence ledger;
- Codex / Claude Code / Claude Code + DeepSeek runtime selection;
- Direct API roles, Verifier, Relay and Red Team modes;
- v3-compatible CLI aliases.

## New paper stage

After G5, freeze the existing scientific artifacts and initialize the manuscript workspace:

```bash
python tools/harness.py freeze --by TEAM
python tools/paper.py init
```

For an old v4.0 run whose `paper/` directory only contains TODO placeholders, `paper.py init` recognizes the untouched template and replaces it with the v4.1 writing workspace.

If the old run already contains meaningful paper files, back them up and use `paper.py init --replace` only intentionally.

## New canonical roles

- `paper_architect`
- `section_writer`
- `manuscript_editor`

`paper_auditor` remains an independent downstream reviewer.

After updating `config/agent_roles.json` / `skills/`, runtime files should be regenerated with:

```bash
python tools/runtime.py render --runtime all
python tools/runtime.py check
```

## New G6 semantics

G6 no longer passes because three paper placeholder files merely exist. It now requires a finalized manuscript and validates frozen science, claim/evidence mapping, trace tags, assembly hashes, audit hashes and final-build hashes.
