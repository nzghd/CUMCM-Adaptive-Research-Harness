# LP / MILP / NLP

**Use when:** explicit decisions, objective and constraints can be written
mathematically.

**Formulation:** list variables/domains, objective terms, all constraints and
physical meaning. Record whether the model is linear, convex, nonconvex, mixed
integer or combinatorial.

**Parameters:** source and scaling for every coefficient. Audit Big-M values.

**Baseline:** relaxed problem, greedy feasible solution and small exhaustive case.

**Required diagnostics:** primal feasibility, constraint margins, solver status,
primal/dual bound where available, integrality gap, multiple starts for nonconvex
NLP, time limit and tolerance.

**Common traps:** calling a feasible solution globally optimal; oversized Big-M;
nonlinear expressions hidden in an LP; ignoring solver status; comparing a
heuristic against no lower/upper reference.
