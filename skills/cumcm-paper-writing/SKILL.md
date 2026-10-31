---
name: cumcm-paper-writing
description: Turn frozen CUMCM scientific artifacts into an evidence-traceable manuscript through paper architecture, bounded section drafting, assembly, audit, revision and finalization.
---

# CUMCM Paper Writing

This skill is the actual manuscript-writing stage. It is separate from `cumcm-paper-audit`.

## Entry condition

1. G5 should be approved or explicitly accepted by the team.
2. Freeze the scientific artifacts:

```bash
python tools/harness.py freeze --by <队员姓名>
python tools/harness.py verify-freeze
```

3. Initialize the paper workspace:

```bash
python tools/paper.py init
python tools/paper_v5.py init
```

Do not write the paper by editing frozen results or code. If a scientific result is wrong, `unfreeze`, repair the source, rerun the needed checks, and freeze again.

## Phase A — architecture

Invoke `paper_architect` first. It must fill:

- `paper/PAPER_BRIEF.md`;
- `paper/PAPER_PLAN.json`;
- `paper/PAPER_CLAIM_MAP.csv`;
- `paper/SYMBOL_TABLE.csv`;
- `paper/FIGURE_TABLE_REGISTER.csv`;
- `paper/PAPER_OUTLINE.md`.

Every core claim must be assigned to a section before substantive drafting. Do not force the default section structure if the problem needs a better split; update `PAPER_PLAN.json` while preserving unique section ids and valid file paths.

Then generate bounded writer packets:

```bash
python tools/paper.py packet --all
```

## Phase B — section drafting

Use `section_writer` for one bounded section at a time. Independent sections may be drafted in parallel, but sections that depend on notation or earlier derivations should wait until those interfaces are stable.

Rules:

- write only the assigned `paper/sections/*.md`;
- use only the section packet and referenced frozen artifacts;
- keep `<!-- CLAIM:Cxxx -->` next to every mapped core claim;
- never invent numbers, citations, solver settings or experiments;
- leave a blocker when evidence is missing.

Write the abstract last. Populate `paper/ABSTRACT_FACTS.json` only with frozen, evidence-linked and human-verified facts, then run:

```bash
python tools/paper_v5.py build-abstract
python tools/claim_guard.py scan --scope all
python tools/claim_guard.py lint --strict
python tools/v51_orchestrator.py approve --stage strong_claim_audit --by <队员姓名>
python tools/paper_v5.py lint --strict
python tools/v51_orchestrator.py approve --stage paper_extension --by <队员姓名>
```

The generator resolves section `abstract` through `PAPER_PLAN.json`; do not invent or copy to a second abstract filename. The section-quality register must exactly cover each included section's planned claims, registered equations, and active figures/tables, and its status/editor must match the plan. If facts or any section change, rebuild as needed, rescan the full corpus, and re-approve the two downstream stages before assembly.

## Phase C — manuscript editing

Invoke `manuscript_editor` after the substantive sections exist. It should align terminology, symbols, cross-references and narrative flow while preserving scientific values and trace tags.

Assemble and inspect:

```bash
python tools/paper.py assemble
python tools/paper.py lint --strict --pre-audit
```

Do not manually edit `PAPER_DRAFT.md`; edit section files and re-run `assemble`.

## Phase D — independent audit and revision

Run separate `paper_auditor`, `red_team`, and `compliance_auditor` passes. Record the human-reviewed result in:

- `paper/CONSISTENCY_AUDIT.md`;
- `paper/PAPER_AUDIT.json` with `status: passed` and no blockers;
- `paper/REVISION_LOG.md`.

If an audit finds a prose problem, revise the section. If it finds a scientific problem, do not alter the paper to hide it: unfreeze and repair the source science.

## Phase E — final manuscript

After audit-driven revisions:

```bash
python tools/paper.py finalize
python tools/harness.py validate --gate G6 --strict
```

`finalize` reassembles the manuscript, performs a strict evidence/trace/freeze check, strips internal claim trace comments from the delivery manuscript, and writes:

- `paper/FINAL_MANUSCRIPT.md`;
- `paper/PAPER_BUILD.json`;
- `paper/PAPER_LINT.json`.

If a PDF is required, run `python tools/paper_v5.py compile` after finalization.
It accepts only `paper/FINAL_MANUSCRIPT.md`, writes under `deliverables/`, and
records `deliverables/PAPER_COMPILE_RECEIPT.json`; pass `--replace` explicitly
when replacing an existing PDF.

Stop for team sign-off before G6 approval.
