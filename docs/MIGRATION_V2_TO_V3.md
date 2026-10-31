# Migration from v2

v2's fixed candidate tournament is not automatically migrated into the new state model.

Recommended migration:

1. Keep v2 as an archive.
2. Initialize a fresh v3 run.
3. Convert each v2 candidate's strongest parts into `IDEA_CARD.json` or `MODULE_CARD.json`.
4. Import experiments as evidence records.
5. Do not copy old selection results as ground truth.
6. Rebuild the problem graph and claim–evidence ledger.

The v2 `5→3→2→1` workflow can still be emulated as one manually configured research policy, but it is no longer the core architecture.
