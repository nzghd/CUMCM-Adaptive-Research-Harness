---
name: cumcm-paper-audit
description: Independently red-team and audit a drafted CUMCM manuscript against frozen code, experiments, claims, figures, references and AI/source disclosure before finalization.
---

# Paper Audit

This skill audits a manuscript already produced through `cumcm-paper-writing`; it is not the writing stage.

Run separate `paper_auditor`, `red_team` and `compliance_auditor` passes. Treat unresolved critical/high issues, unverified core claims, broken claim trace tags, frozen-result drift and fabricated/unregistered references as blockers.

Record the reviewed audit in:

- `paper/CONSISTENCY_AUDIT.md`;
- `paper/PAPER_AUDIT.json` (`status: passed` only when blockers are empty);
- `paper/REVISION_LOG.md`.

Revise `paper/sections/*.md`, then re-run `python tools/paper.py assemble`. Do not silently edit experimental numbers in prose; repair the source artifact or record the discrepancy. After all blockers are resolved, run `python tools/paper.py finalize` and `python tools/harness.py validate --gate G6 --strict`.
