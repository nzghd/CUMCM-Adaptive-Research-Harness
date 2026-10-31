# Stage 07A — Strong Claims and Consistency

1. Scan model and manuscript text with `tools/claim_guard.py scan`.
2. For every rigorous bound, optimum, uniqueness, convergence, stability, significance,
   robustness, proof or optimality-gap claim, attach valid proof/certificate provenance or
   downgrade the wording to tested empirical scope.
3. Run strict claim lint.
4. Populate `tools/consistency_matrix.py` by dimension: replay, coordinates, objectives,
   constraints, units, equation-code, results, figures and paper-evidence.
5. Preserve actual tolerances using PASS_WITH_TOLERANCE; do not collapse all checks into one PASS.
