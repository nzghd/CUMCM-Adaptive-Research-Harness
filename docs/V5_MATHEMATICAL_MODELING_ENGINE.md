# v5 Mathematical Modeling Engine

The v4.1 Idea Card remains the width-search artifact. A retained idea must not
jump directly to code. It becomes a mathematical module with explicit symbols,
equations, derivation, parameter provenance, theory checks, numerical method,
validation protocol and model-to-code mapping.

## Required module artifacts

See `templates/math_module/` and `tools/math_engine.py`.

The strict linter checks structure, traceability and open obligations. It does
not certify theorem correctness. High/critical obligations must be discharged,
downgraded, rejected or marked not applicable with recorded review.

## Optional executable checks

`tools/math_workbench.py` exposes optional SymPy, Pint, CVXPY and Pyomo checks.
A symbolic simplification, dimension check or solver status applies only to the
encoded expression/instance and assumptions; it is not automatically a general
proof or global certificate.
