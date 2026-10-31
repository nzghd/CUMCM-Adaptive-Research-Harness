# G0 — Intake / Problem Graph

Use `$cumcm-adaptive-research` on the active run.

1. Read the active runtime rules (`AGENTS.md` for Codex or `CLAUDE.md` for Claude Code), `RUN_META.json`, `BUDGET.json`, `inputs/problem/` and `inputs/data/`.
2. Delegate bounded, mostly read-only work to `problem_cartographer` and `data_auditor`.
3. Produce only the G0 artifacts:
   - `problem/PROBLEM_BRIEF.md`
   - `problem/QUESTION_MAP.md`
   - `problem/PROBLEM_GRAPH.json`
   - `problem/ASSUMPTION_REGISTER.csv`
   - `problem/VALIDATION_MAP.md`
   - `data/DATA_AUDIT.md`
4. Separate facts, interpretations, assumptions and unknowns. Do not choose a final model or draft the paper.
5. Run `python tools/harness.py validate --gate G0`. Stop for team review.
