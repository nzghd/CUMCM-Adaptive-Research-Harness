# CUMCM Adaptive Research Harness v5.1 — Build Report

**Version:** 5.1.0  
**Build date:** 2026-09-04  
**Offline release checkpoint:** **PASS**

This tree retains the v4.1-facing workflow while hardening the v5.1 lifecycle.
It does not claim byte-for-byte identity with v4.1.

## Completed hardening

- unified abstract generation and assembly on `PAPER_PLAN.json`;
- mandatory current strong-claim scan receipts with separate math/full scopes;
- schema-aligned templates and config-driven validation;
- content-bound v5.1 stages integrated into G0–G7;
- real, run-confined evidence-reference resolution;
- run-confined destructive paths, safe identifiers and link/junction rejection;
- phase-aware external-egress transactions with pre-call reservations, failure
  disclosure and bidirectional G7 reconciliation;
- stable G0 budget-policy projection with full mutable budget binding at G7;
- replay-resistant G1 diversity receipts;
- strict G2 Idea/Module identity and registry reconciliation;
- replay-resistant G3 experiment receipts and child-environment boundaries;
- typed, hash-bound paper/AI PDF receipts and fail-closed delivery packaging;
- deterministic `review --gate G0..G7` decision summaries whose canonical body,
  underlying Gate snapshot and approval binding are checked independently;
- true two-step `review → human read → approve`: `approve` never creates or
  repairs a report, and a missing/stale/tampered report causes zero Gate/log
  writes even when an upstream approval has drifted;
- an `AI工具使用声明` section for every team, with exact official used/no-use
  forms, log-state reconciliation and named human purpose attestation;
- a separate official `build-support` path with an explicit allowlist,
  deterministic ZIP, external internal-only receipt, AI-detail-PDF binding,
  identity/secret scanning and rollback-safe replacement; legacy `package`
  remains a named internal audit archive and is explicitly non-submittable;
- G7 enforcement of separate paper/support artifacts, inclusive
  `20,000,000`-byte ceilings that configuration cannot relax, and conservative
  anonymity checks over names, readable text, searchable PDF text and metadata;
- support-path rejection for traversal, symlink/junction, hardlink and Windows
  alternate-data-stream syntax;
- deterministic twin release manifests plus an isolated manifest regression;
- one offline release entry point, `scripts/check_release.ps1`, preferring
  `E:\Anaconda\envs\common_env\python.exe`.

Fifteen focused checks, the base compatibility regression and the full v5.1
lifecycle regression passed. The full regression exercised both malicious and
valid delivery paths, then generated two valid ZIP packages and compared their
archive members exactly. It also exercised canonical review generation,
approval binding, zero-write review rejection, review-body tamper rejection,
identity-vocabulary growth, and idempotent repeat G7 approval. Python/JSON
parsing and final twin-manifest integrity also passed.

No network, model or API call was made. No independent release-ZIP CRC or
fresh-extraction result is claimed. See `TEST_REPORT_V5.1.md` and
`HARNESS_AUDIT_REPORT_2026-09-04.md`.
