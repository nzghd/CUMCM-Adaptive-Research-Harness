# CUMCM Strong Claim Guard

Scan model and paper text for claims such as rigorous bounds, global optimum, uniqueness, convergence guarantee, stability, statistical significance, robustness guarantee and proof.

For each claim either:

- attach an allowed proof/certificate provenance and exact evidence; or
- downgrade the wording to the empirical scope actually tested; or
- reject/remove it.

Numerical search, heuristic search, finite sampling, a single run, visual inspection or LLM judgment cannot certify a rigorous bound or global optimum.

Use the two-phase receipts rather than treating an empty table as a completed
audit:

1. Before G5, run the canonical `math` scope over `research/modules/` and
   `synthesis/`; review every hit and retain the generated math registry and
   scan receipt.
2. After all manuscript sections are stable, run the canonical `all` scope;
   it adds `paper/sections/` and must be reviewed before paper approval.

Do not scan review reports, evidence tables, result dumps or management JSON as
claim-bearing prose. A clean zero-hit result is valid only with a current scan
receipt bound to the complete canonical corpus and scanner policy.
