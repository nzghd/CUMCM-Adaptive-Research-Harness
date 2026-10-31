# CUMCM Interpretation Audit

Use this skill before selecting a mathematical model.

## Required behavior

1. Separate literal facts, operational definitions, structural assumptions, parametric assumptions and unknowns.
2. Convert every high-impact ambiguity into an `INTERPRETATION_FORKS.csv` entry.
3. Generate at least two materially different alternatives for a structural fork.
4. Give each retained idea/module an assumption fingerprint.
5. Assess diversity at three levels: interpretation, model structure and algorithm.
6. Run cheap structural probes when branches can change the objective, feasible set, geometry, time semantics or conclusions.
7. Do not close nondelegable forks on behalf of the team.

## Commands

```text
python tools/interpretation_engine.py init
python tools/interpretation_engine.py fork-add ...
python tools/interpretation_engine.py fingerprint-set ...
python tools/interpretation_engine.py analyze
python tools/sensitivity_engine.py add --kind structural ...
python tools/interpretation_engine.py lint --strict
```

Passing these checks does not establish that the chosen interpretation is correct.
