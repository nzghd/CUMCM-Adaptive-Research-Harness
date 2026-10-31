---
name: cumcm-adaptive-research
description: Dynamically explore, test and integrate mathematical-modeling approaches for a CUMCM run. Use for problem graphing, blind idea generation, diversity checks, targeted specialists, experiments and evidence integration. Do not use as a one-shot paper generator.
---

# CUMCM Adaptive Research

1. Locate the active run with `python tools/harness.py active`.
2. Read the active runtime project rules (`AGENTS.md` for Codex or `CLAUDE.md` for Claude Code), plus `RUN_META.json`, `BUDGET.json` and current status.
3. Route the request to one of these stages.

## Intake

Use `problem_cartographer`. Produce and validate:

- `problem/PROBLEM_GRAPH.json`
- `data/DATA_AUDIT.md`
- `problem/ASSUMPTION_REGISTER.csv`
- `problem/VALIDATION_MAP.md`

Stop for human review before deep modeling.

## Explore

Use `meta_scientist` to create a small batch of bounded briefs. Spawn `research_specialist` agents in waves no larger than the configured concurrent thread limit. During the first batch, specialists may read only public inputs, approved problem files and their own brief. Each writes one `IDEA_CARD.json`.

Run:

```bash
python tools/diversity.py analyze
```

Use `diversity_auditor` to interpret the report. Expand only for missing information, not to reach a fixed count.

## Deepen

Convert selected ideas into module cards. Assign separate targeted work for equations, implementation and evidence. Require a baseline, falsification test and failure modes.

## Verify

Use `evidence_architect`, then `experimenter`, `math_auditor`, `data_auditor` and `code_auditor` only where needed. Register core claims and evidence with `tools/harness.py`.

## Resolve disputes

If evidence is inconclusive, create a narrow issue and choose one resource:

- another independent specialist through the active runtime;
- independent Pro review;
- faithful or Lite verifier;
- optional API burst;
- additional experiment.

## Integrate

Integrate compatible modules by graph interface. Do not force one Agent's whole solution to win. Run Red Team before freezing.
