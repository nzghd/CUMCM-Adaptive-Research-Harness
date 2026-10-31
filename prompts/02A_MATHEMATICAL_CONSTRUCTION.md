# Stage 02A — Mathematical Construction

Use `cumcm-mathematical-modeling` after critical interpretation forks are resolved
or explicitly retained as multiple branches.

For each focused module:

1. Initialize the structured module with `tools/math_engine.py init-module`.
2. Define sets, variables, domains, units, parameters, objective/governing equations,
   constraints and boundary/initial conditions.
3. Register equations and give a step-by-step derivation with conditions.
4. Separate parameter sources and assess identifiability.
5. Design numerical method, solver diagnostics, exact/synthetic small cases and baseline.
6. Register every unproved strong statement as a mathematical obligation.
7. Map mathematical items to code, tests, experiments, evidence and paper sections.
8. Run `math_engine.py lint --strict` and stop on blockers.

Do not call a numerical best value a global optimum or a searched envelope a rigorous bound.
