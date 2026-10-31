# Architecture — v5.1 Runtime-Neutral Harness

## Layer 1 — Scientific core

The core is model-independent:

```text
Problem semantics / interpretation forks
→ Problem Graph
→ adaptive search and diversity analysis
→ module selection and structured mathematics
→ experiments with runner receipts
→ claim/evidence ledger
→ dispute resolution and structural sensitivity
→ independent review and consistency audit
→ freeze(science)
→ evidence-bound paper pipeline
```

These artifacts live under `runs/<RUN_ID>/` and remain stable when the AI runtime changes.

## Layer 2 — Canonical research roles

`config/agent_roles.json` defines role intent, access class, Claude tool profile and instructions. `skills/` defines reusable workflows. These are the only sources that should be edited when changing research behavior.

## Layer 3 — Runtime adapters

`tools/runtime.py` renders canonical roles to:

- Codex TOML project agents;
- Claude Code Markdown/YAML project subagents;
- synchronized project Skills.

DeepSeek can be used through Claude Code's Anthropic-compatible backend without changing the scientific core.

## Layer 4 — Provider calls

`tools/provider_call.py` supports bounded direct calls through OpenAI, Anthropic and DeepSeek. This is an audit-friendly packet path, not a full autonomous coding sandbox.

## Selection philosophy

A final solution may combine the best data-cleaning module from one route, prediction module from another, and robustness module from a third. The system therefore selects and integrates **modules**, not merely one “winning Agent”.

## Verification philosophy

Model outputs are hypotheses until supported by mathematics, executable checks, data, solver status, source evidence or a clearly scoped human review. Cross-model agreement can trigger confidence or priority decisions, but never substitutes for the Claim–Evidence Ledger.

## Gate and artifact contracts

G0–G7 are the public lifecycle. The v5.1 stages are validators used by those Gates,
not a second manually synchronized lifecycle. Each validation records a normalized
input manifest and hash; approval is valid only while that hash remains current.
The current mapping is G0/`interpretation`, G2/`structural_validation`,
G4/`mathematical_construction`, G5/`independent_review` + `consistency`,
G6/`consistency` + `strong_claim_audit` + `paper_extension`, and
G7/`submission`; G1 and G3 retain their native Gate contracts without an extra
v5.1 stage. A stage or Gate approval must name a concrete human and carry a
timezone-aware timestamp. Placeholder or machine identities cannot close a
human-review obligation.

`config/artifact_schema_registry.json` is the executable registry for JSON/CSV
contracts. `tools/schema_validate.py` validates run artifacts, configuration files,
empty-table headers and producer field declarations. Audit receipts distinguish an
initialized scaffold from a completed clean audit.

Evidence used by scientific Gates must resolve to a real file inside the current
run. Web and subscription sources are registered separately in
`sources/SOURCE_LEDGER.csv`.

## Manuscript layer

The manuscript is a downstream transformation of frozen science, not another research Agent free-for-all:

```text
freeze(science)
  -> paper_architect
  -> section_writer packets
  -> manuscript_editor
  -> deterministic assemble/lint
  -> independent paper/red-team/compliance audit
  -> finalize
```

`paper/PAPER_DRAFT.md` is generated from section files. `paper/FINAL_MANUSCRIPT.md` is generated only after strict paper lint passes and internal claim trace tags are stripped.

The science freeze deliberately excludes the mutable review, claim-audit,
consistency and paper layers. Their approvals bind to the frozen inputs by hash.
Assembly also binds every included section to the title, order, path and digest in
`PAPER_PLAN.json`; `paper_v5.py build-abstract` resolves the abstract path from that
same plan.

The fail-closed rationale and migration consequences are recorded in
[`adr/0001-fail-closed-artifact-gates.md`](adr/0001-fail-closed-artifact-gates.md).
