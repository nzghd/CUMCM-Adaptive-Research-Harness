# CUMCM Harness Domain Context

This file defines the lifecycle terms used by the harness, validators, tests,
and runtime instructions.  These meanings are normative for v5.1.

## Core terms

### Run

An isolated competition workspace under `runs/<run_id>/`.  Scientific evidence
referenced by a Run must either live inside that Run or be imported into it with
an immutable digest recorded by the harness.

### Artifact

A file or structured record used as an input or output of the research
workflow.  An Artifact's existence alone never establishes that work was
performed.

### Scaffold

An initialized placeholder or header-only Artifact.  A Scaffold helps a human
or Agent start work, but must never satisfy a Gate that requires completed work.

### Gate

An authoritative lifecycle checkpoint in `STATUS.json`.  A Gate validates its
current inputs and invokes every mandatory Stage mapped to it before approval.
Prior approval is stale when any bound input changes.

### Stage

A named family of validation rules, such as interpretation, structural
validation, strong-claim audit, or consistency.  A Stage is an implementation
detail of one or more Gates; it is not an independent terminal workflow.

### Audit Receipt

A machine-readable result that binds a validator and its rule version to the
ordered input manifest, input hashes, output hashes, timestamp, and human
approval where required.  A clean zero-finding receipt is valid; a missing or
stale receipt is not.

### Canonical Corpus

The authoritative source files scanned by a text audit.  Generated copies,
review commentary, registries, and prior audit reports are excluded unless a
specific Stage declares them authoritative inputs.

### Science Freeze

The immutable manifest of problem interpretation, data, models, code, results,
experiments, and scientific evidence used to write the paper.  Paper drafts and
paper-derived audit outputs are not part of the Science Freeze.

### Delivery Manifest

The final binding of the Science Freeze, current paper assembly, final
manuscript, source/AI disclosures, and all required Audit Receipts.  It is
created only after the final validation path succeeds.

### Current and stale

An Audit Receipt or approval is **current** when every bound input and rule hash
still matches.  It is **stale** after any mismatch, regardless of its recorded
status string.

## Lifecycle invariants

1. Validators inspect state; initialization and migration are explicit actions.
2. Missing, header-only, placeholder, malformed, or stale Artifacts fail closed
   whenever the Gate requires completed work.
3. A successful numerical command means an explicitly accepted solver status,
   not merely a process that returned without raising an exception.
4. Human approvals never override failed current validation or undetected input
   drift. A force flag may skip prerequisite ordering only; blocked approval
   attempts are recorded and remain unapproved.
5. CSV and JSON contracts have one executable source of truth.  Incompatible
   historical data requires an explicit, auditable migration.
6. Final paper validation binds the working title and the ordered included
   section projection, not only individual section contents.
7. The G5 science review binds an independent mathematical strong-claim scan;
   the later full-corpus scan binds stable manuscript wording without mutating
   the G5 receipt or science freeze.
