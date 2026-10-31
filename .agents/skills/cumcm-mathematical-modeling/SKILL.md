# CUMCM Mathematical Modeling

Turn an approved interpretation into auditable mathematics. Do not jump from an Idea Card directly to code.

For every retained module create and complete:

- `MODEL_INTENT.md`
- `SYMBOL_TABLE.csv`
- `ASSUMPTIONS.md`
- `FORMULATION.md`
- `EQUATION_REGISTRY.csv`
- `DERIVATION.md`
- `PARAMETER_IDENTIFICATION.md`
- `PARAMETER_REGISTER.csv`
- `THEORY_CHECKS.md`
- `NUMERICAL_METHOD.md`
- `VALIDATION_PROTOCOL.md`
- `MODEL_TO_CODE_MAP.csv`
- `MATHEMATICAL_OBLIGATIONS.csv`
- `MODULE_SPEC.json`

Rules:

1. Define variables, domains and units before using them.
2. Map every objective/constraint/governing equation to the real statement.
3. State theorem conditions and approximation error.
4. Distinguish exact solution, certificate, local optimum, heuristic best value and empirical reference envelope.
5. Register unresolved claims as mathematical obligations.
6. Build a transparent baseline and a hand-checkable/small exact case.
7. Map equations to code, tests, experiments, evidence and paper locations.
8. Use SymPy/Pint/CVXPY/Pyomo only as checks on encoded objects, not as automatic proof.
