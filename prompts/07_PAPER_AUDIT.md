# G6–G7 — Paper audit, revision and finalization

Use `$cumcm-paper-audit` after the draft has been assembled by `$cumcm-paper-writing`.

Run independent `paper_auditor`, `red_team` and `compliance_auditor` passes. Check every formula, symbol, unit, table, figure, citation and conclusion against frozen code/results and `evidence/CLAIM_EVIDENCE_LEDGER.csv`.

Record reviewed findings in `paper/CONSISTENCY_AUDIT.md`, maintain `paper/REVISION_LOG.md`, and set `paper/PAPER_AUDIT.json` to `status: passed` only when blockers are zero. Revise section files, never `PAPER_DRAFT.md` directly.

Then run:

```bash
python tools/paper.py finalize
python tools/harness.py verify-freeze
python tools/harness.py validate --gate G6 --strict
python tools/harness.py export-ai --by <队员姓名>
```

Treat missing human verification for core claims, unresolved critical/high issues, source gaps, manuscript/frozen-result drift and incomplete AI disclosure as blockers. Stop for team sign-off before approving G6/G7.
