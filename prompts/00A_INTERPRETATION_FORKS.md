# Stage 00A — Interpretation Forks

Use `cumcm-interpretation-audit`.

Read only the original statement/attachments and approved G0 problem/data files.
Do not read candidate solutions yet.

1. Complete `interpretation/PROBLEM_SEMANTICS.md` by separating literal facts,
   operational definitions, structural assumptions, parametric assumptions and unknowns.
2. Register every ambiguity that can change geometry, objective, feasible set,
   time/aggregation, uncertainty, observation, reference frame or boundary conditions.
3. For each structural fork, provide at least two materially different alternatives.
4. Propose cheap probes that compare branches using the same inputs; re-optimize when
   the branch changes the model.
5. Do not choose a nondelegable branch for the team.

Run:

```text
python tools/interpretation_engine.py lint --strict
```

Stop for human review.
