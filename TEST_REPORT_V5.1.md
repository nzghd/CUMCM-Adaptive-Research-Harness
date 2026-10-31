# CUMCM Adaptive Research Harness v5.1 — Release Test Report

**Version:** 5.1.0  
**Test date:** 2026-09-04  
**Environment:** Windows, `E:\Anaconda\envs\common_env\python.exe` 3.12.13  
**Network/model/API calls:** None

## Result

**PASS** for the current source tree's offline release checkpoint.

The following all returned exit code 0:

- runtime mirror consistency and static schema validation;
- path-safety, selection-identity, diversity-receipt, experiment-environment,
  experiment-receipt, egress-transaction, consistency, human-identity,
  Gate-review unit/integration, G7 submission-compliance, official support
  packaging and release-manifest focused regressions;
- `tools/self_test.py` base-compatibility regression;
- `tools/self_test_v51.py` full lifecycle regression;
- final twin release-manifest integrity verification.

The unified offline entry point is:

```powershell
.\scripts\check_release.ps1
```

It prefers `E:\Anaconda\envs\common_env\python.exe`. The final source tree was
checked with the wrapper's same command set individually, including a fresh
complete lifecycle run, and every exit code was checked; the wrapper was not
redundantly rerun from start to finish.

A separate parse sweep accepted 47 non-run Python files (43 under `tools/`)
and 87 non-run JSON files, with no merge-conflict markers.

## Regression coverage

- Abstract generation follows `PAPER_PLAN.json`, writes canonical
  `paper/sections/010_abstract.md`, and is exercised through assemble and
  finalize.
- Strong-claim approval requires a current, run-bound canonical scan receipt;
  an empty pre-created CSV cannot pass as a completed scan.
- v5.1 stages are enforced through G0–G7, and evidence references must resolve
  to actual run-local artifacts or uniquely verified ledger entries.
- Destructive/replacement outputs are confined to validated direct-child run
  namespaces; traversal, cross-run targets, unsafe identifiers and
  link/junction, hardlink and NTFS alternate-data-stream redirection fail closed.
- External egress is phase-gated and transactionally recorded. Pre-call budget
  reservation, terminal failure records, AI disclosure, attempt directories
  and G7 reverse reconciliation prevent silent calls and orphan records.
- G0 binds stable budget policy rather than mutable consumption counters; G7
  separately binds and reconciles the full live budget state.
- G1 requires a current `DIVERSITY_SCAN.json` bound to inputs, parameters and
  algorithm version, distinguishing not-run, stale, tampered and clean states.
- G2 reconciles selected Idea/Module IDs, canonical card paths and registries;
  duplicate, mismatched, missing and unknown identities fail.
- G3 receipts bind executed command, resolved executable, environment policy,
  inherited/runner keys, isolation declarations and input/output hashes. Six
  synchronized replay/tamper cases are rejected.
- `review --gate G0..G7` generates a fixed, concise decision surface from the
  strict validator and structured artifacts. The report is excluded from its
  own input snapshot, but approval separately binds its canonical path, body
  hash and underlying input hash; body edits, stale metadata, unsafe links and
  verifier changes fail closed. `approve` cannot auto-generate or repair a
  report; missing/stale/tampered reports cause no Gate or decision-log writes,
  including when a prerequisite Gate is stale.
- Unchanged repeat G7 approval is an idempotent no-op, avoiding a new receipt
  that would invalidate the approval it describes.
- G7 requires `deliverables/FINAL_MANUSCRIPT.pdf`, accepts at most 20,000,000
  bytes, and requires every team to place `AI工具使用声明` before references.
  Exact official no-use/used forms are reconciled with the AI log; used-AI
  purpose consistency needs named human attestation.
- `build-support` creates a distinct anonymous official ZIP from an explicit
  allowlist and enforces its own non-relaxable 20,000,000-byte ceiling. It binds
  the canonical attested AI-detail PDF when needed, scans filenames, readable
  text, searchable PDF text and PDF metadata, and keeps named receipts outside
  the ZIP. Build-time and newly discovered identities are unioned on re-audit.
- Fenced code and HTML comments do not create false manuscript headings or
  identity hits; a human checklist covers images, headers, footers, OCR-only
  content and formats that cannot be fully machine-inspected.
- G7 decision-history changes make an existing approval stale; formal package
  creation is rejected until reapproval. Delivery ZIP members must exactly
  equal the approved allowlist plus its generated delivery manifest.
- Deterministic twin release manifests are generated from one canonical file
  set and checked for equality, freshness, missing files and unlisted files.

## Limits

These are synthetic offline regressions. They do not prove a real model,
theorem, external-review independence, live-provider behavior, visual PDF
quality, complete anonymity or competition outcome. The anonymity scan cannot
see all image/OCR/header/footer content and therefore requires human review.
No network, model or API call was made. No independent release-ZIP CRC or
fresh-extraction result is claimed. No real past-problem team rehearsal was run;
that remains the final usability exercise rather than a release-code blocker.
