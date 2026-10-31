# Fine-Grained Consistency

Do not collapse different checks into one unqualified PASS. Record duration
replay, coordinates, objectives, constraints, units, equation-code mapping,
result-table mapping, figure-data mapping and paper-evidence mapping separately.

Statuses:

- PASS
- PASS_WITH_TOLERANCE
- FAIL
- NOT_TESTED
- NOT_APPLICABLE

`PASS_WITH_TOLERANCE` must retain the actual tolerance and evidence reference.
The overall status is derived from the matrix and never erases per-dimension
qualification.

`required: true` dimensions cannot be closed as `NOT_APPLICABLE`. An optional
dimension may use that status only when its canonical config explicitly sets
`allow_not_applicable: true` and the row records a concrete `waiver_reason`, a
named reviewer (not a generic placeholder), and a timezone-aware review time.
Other statuses must leave `waiver_reason` empty.

Evidence references are resolved, not merely checked for non-empty text. Use a
human-verified evidence/source ledger ID, a passed experiment receipt or
artifact, or a non-empty result under `evidence/`, `figures/`, or `results/`.
Status files, manifests, ledgers and the consistency outputs themselves cannot
serve as evidence.
