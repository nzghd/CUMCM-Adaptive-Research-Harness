# 72-hour playbook

This is a flexible allocation, not a rigid schedule.

## 0–6 h

- Parse all questions and data.
- Build problem graph and validation map.
- Produce a minimal baseline.
- Start a small blind exploration batch.

## 6–18 h

- Run diversity/coverage review.
- Expand only missing directions.
- Deepen the most viable modules.
- Establish experiment protocol and data split before tuning.

## 18–40 h

- Implement competing modules.
- Run baselines, multi-seed experiments, constraints and sensitivity.
- Resolve high-severity issues with targeted specialists.

## 40–56 h

- Integrate modules end to end.
- Freeze core numbers provisionally.
- Draft paper around evidence, not chronology.

## 56–68 h

- Red Team, reproduce figures, audit claims, citations and AI log.
- Simplify models the team cannot explain.

## 68–72 h

- Final formatting, support files, disclosure and freeze verification.
- Do not introduce a new complex model unless it fixes a critical failure.

## v5.1 paper-writing checkpoint

Do not postpone all writing to the final hour. Once the scientific core is stable enough to freeze, initialize `tools/paper.py`, map claims, and draft evidence-backed method/result sections. Write the abstract last. Any late scientific correction requires unfreeze → repair → re-audit → re-freeze before finalization.
