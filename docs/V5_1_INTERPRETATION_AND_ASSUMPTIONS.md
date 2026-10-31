# v5.1 Interpretation and Assumption Layer

A mathematically correct solution to the wrong operational problem is still a
bad modeling solution. v5.1 therefore places an explicit interpretation layer
before model construction.

## Three kinds of diversity

1. **Interpretation diversity:** what does the statement mean operationally?
2. **Model diversity:** what mathematical structure represents that meaning?
3. **Algorithm diversity:** how is the chosen mathematical problem solved?

Different optimizers do not compensate for a single shared structural
assumption. Record candidate fingerprints with
`tools/interpretation_engine.py fingerprint-set` and inspect the generated
assumption-diversity report.

## Nondelegable decisions

A fork becomes nondelegable when it is critical, has high combined risk, or
changes geometry, objective, feasible set, time/aggregation semantics,
uncertainty, observation, reference frame or boundary conditions. It cannot be
closed by AUTO/DEFAULT/AGENT/SYSTEM/LLM identities and requires an explicit
human-confirmed decision with reason and evidence/source.

This is a process guard, not proof that the human choice is correct.
