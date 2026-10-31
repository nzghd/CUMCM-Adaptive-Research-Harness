# Evidence-first paper writing in v5.1

v5.1 includes a controlled manuscript production stage. `paper-audit` alone is not considered “writing the paper”.

## Why this stage exists

The scientific workflow produces models, code, experiments, figures and claim/evidence records. A competition paper needs a separate controlled transformation from those artifacts into prose. The transformation must not silently create new scientific facts.

The paper pipeline therefore uses four layers:

```text
Frozen scientific artifacts
        ↓
paper_architect
        ↓
PAPER_PLAN + CLAIM_MAP + SYMBOLS + FIGURE/TABLE REGISTER
        ↓
section_writer × bounded sections
        ↓
manuscript_editor
        ↓
PAPER_DRAFT.md
        ↓
paper_auditor + red_team + compliance_auditor
        ↓
FINAL_MANUSCRIPT.md
```

## 1. Freeze first

```bash
python tools/harness.py freeze --by <队员姓名>
python tools/harness.py verify-freeze
```

The paper directory itself is not inside the scientific freeze scope. This lets wording change while the underlying scientific truth stays hash-locked.

## 2. Initialize the manuscript workspace

```bash
python tools/paper.py init
python tools/paper_v5.py init
```

This creates a CUMCM-style default section plan, claim map, symbol table, figure/table register, section workspaces, audit state and revision log.
`PAPER_PLAN.json` is the only source of truth for section output paths. Do not create a parallel `00_abstract.md` or infer a filename from directory ordering.

## 3. Architecture before prose

`paper_architect` fills the paper brief and rewrites the default plan around the actual problem. Core claims should be assigned through:

```bash
python tools/paper.py map-claim --claim C001 --section model_and_solution
```

The section ids are visible in `paper/PAPER_PLAN.json` and `python tools/paper.py status`.

Generate writer packets only after the mapping is sensible:

```bash
python tools/paper.py packet --all
```

## 4. Bounded section writers

Each `section_writer` gets one packet and one output section. It may read the packet and explicitly referenced frozen files, but it must not alter the frozen scientific artifacts.

Every mapped core claim uses an internal trace comment, for example:

```text
<!-- CLAIM:C003 -->
```

The trace marker sits next to the prose that states the claim. It is an internal audit device, not a delivery-format citation.

Write the abstract last by first registering only frozen facts verified by a
named human (not `TEAM`, `AUTO`, `CODEX`, `GPT` or another placeholder).
Each fact records both `claim_ids` and `evidence_ids`; the evidence rows must
belong to exactly those claims, resolve to current run-local files and match the
scientific freeze. Then run:

```bash
python tools/paper_v5.py build-abstract
python tools/paper_v5.py section-quality-set --section abstract --status final --editor <队员姓名>
python tools/claim_guard.py scan --scope all
python tools/claim_guard.py lint --strict
python tools/v51_orchestrator.py approve --stage strong_claim_audit --by <队员姓名>
python tools/paper_v5.py lint --strict
python tools/v51_orchestrator.py approve --stage paper_extension --by <队员姓名>
```

`build-abstract` resolves section `abstract` through `PAPER_PLAN.json` and writes that planned file. The generated text must stay synchronized with `ABSTRACT_FACTS.json`; update the facts and rebuild instead of editing a differently named abstract file.
The `all` scope covers narrative files under `research/modules/`, `synthesis/`
and `paper/sections/`; the earlier G5 `math` scope covers only the first two.
Generated result tables, evidence ledgers and review registries are not scanned as
manuscript prose.
Record a final, named section-quality review for every included `PAPER_PLAN`
section before the paper-extension gate is approved. The register checks exact
claim, equation and active figure/table coverage, and its status/editor must
match the corresponding `PAPER_PLAN` status/writer. Any later section revision
invalidates the full-corpus strong-claim scan and paper-extension approval, so
rescan and re-approve both before G6.

## 5. Assemble, do not hand-edit the draft

```bash
python tools/paper.py assemble
python tools/paper.py lint --strict --pre-audit
```

`PAPER_DRAFT.md` is a generated assembly artifact. If it is wrong, edit the relevant `paper/sections/*.md` file and assemble again.

After `finalize`, an optional PDF build is provenance-bound and constrained to
the delivery directory:

```bash
python tools/paper_v5.py compile
# If the target already exists:
python tools/paper_v5.py compile --replace
```

The compiler reads only `paper/FINAL_MANUSCRIPT.md`, writes a non-empty PDF
under `deliverables/`, and records source/output hashes plus the pandoc version
in `deliverables/PAPER_COMPILE_RECEIPT.json`.

Downstream Gate validation also requires the receipt-bound file to have a
complete PDF header/xref/EOF envelope, be parseable and contain at least one
page. This structural check does not replace opening the rendered document and
checking visual layout against the current official template.

The lint blocks on:

- changed/missing frozen artifacts;
- missing required section files;
- draft placeholders;
- unmapped core claims;
- missing core-claim trace tags;
- missing core evidence;
- used figure/table paths that do not exist;
- section changes after assembly;
- changes to the title or ordered included-section plan after assembly/finalization;
- audit status not passed;
- any change to the complete paper-audit input manifest after `audit-set`.

Files under `paper/sections/` that are not referenced by `PAPER_PLAN.json` are preserved and reported as warnings, but they are never assembled into the manuscript.
Runs finalized by an older harness must be assembled, reviewed and finalized once more so the new ordered plan-projection hashes are recorded; the migration never deletes existing section files.

## 6. Independent manuscript audit

Run `paper_auditor`, `red_team` and `compliance_auditor` independently. Record findings in `CONSISTENCY_AUDIT.md` and the revision log.

When the reviewed blockers are zero:

```bash
python tools/paper.py audit-set --status passed --by <队员姓名> --note "逐项复核完成"
python tools/paper.py lint --strict --pre-final
```

`--pre-final` differs from `--pre-audit`: it requires the audit to be passed and bound to the exact current draft/freeze hashes.

Do not mark the audit passed simply because an AI reviewer says so; this field is intended to capture team-reviewed state.

## 7. Finalize

```bash
python tools/paper.py finalize
python tools/harness.py validate --gate G6 --strict
```

Finalization reassembles all sections, performs strict checks, strips internal claim trace comments, and writes:

- `paper/FINAL_MANUSCRIPT.md`;
- `paper/PAPER_BUILD.json`;
- `paper/PAPER_LINT.json`.

The output is manuscript content, not an assertion that the official typesetting/submission PDF is automatically correct. The team still needs to apply the current official template and perform final visual inspection.
