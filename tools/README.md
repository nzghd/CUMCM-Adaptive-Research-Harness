# Tools command index — v5.1

Core tools use the Python standard library. Direct provider calls and optional OpenAI API burst need packages from `requirements-api.txt`. Faithful Verifier has its own optional dependency path.

## `harness.py`

State, Gate, budget, evidence and packaging CLI.

```powershell
python tools/harness.py init --contest ... --problem ... --runtime codex
python tools/harness.py active
python tools/harness.py status
python tools/harness.py budget-status
python tools/harness.py budget-log ...
python tools/harness.py validate --gate G0 --strict
python tools/harness.py review --gate G3
python tools/harness.py approve --gate G0 --by <队员姓名>
python tools/harness.py revoke --gate G0 --by <队员姓名>
python tools/harness.py idea-new ...
python tools/harness.py idea-list
python tools/harness.py module-new ...
python tools/harness.py module-list
python tools/harness.py focus-set ...
python tools/harness.py dispute-add ...
python tools/harness.py dispute-resolve ...
python tools/harness.py claim-add ...
python tools/harness.py evidence-add ...
python tools/harness.py ai-log ...
python tools/harness.py source-add ...
python tools/harness.py source-attest-none --by <队员姓名> --reason "..."
python tools/harness.py freeze
python tools/harness.py verify-freeze
python tools/harness.py unfreeze ...
python tools/harness.py export-ai --by <队员姓名>
python tools/harness.py attest-ai-disclosure-pdf --by <队员姓名>
python tools/harness.py build-support --run <RUN_ID> --include <run内文件> --by <队员姓名>
python tools/harness.py regenerate-input-manifest
python tools/harness.py package --by <队员姓名>  # 仅内部审计归档，不得提交
python tools/harness.py next
```

`review --gate <G0-G7>` writes a compact human decision summary to
`runs/<RUN_ID>/reviews/gates/<GATE>_REVIEW.md`. It expands unresolved warnings and
decisions, counts routine passing checks, summarizes key results and links to the
machine-readable evidence. It also shows input changes since the previous review.
The required flow is `review` → a team member opens and reads the Markdown →
`approve`. Approval is read-only with respect to the report: it validates and
binds the existing report and underlying input hashes. A missing, stale, tampered,
or link-broken report fails closed; `approve` never generates or repairs one.

`init` runtime choices:

```text
codex
claude-code
claude-code-deepseek
direct-api
```

## `runtime.py`

Canonical role/skill adapter manager. No network calls.

```powershell
python tools/runtime.py list
python tools/runtime.py render --runtime all
python tools/runtime.py check
python tools/runtime.py guide --runtime claude-code-deepseek
python tools/runtime.py doctor --runtime claude-code
```

`render` regenerates Codex and Claude Code project agents from `config/agent_roles.json` and synchronizes canonical `skills/`.

## `provider_call.py`

Bounded single-role direct API call through OpenAI, Anthropic or DeepSeek.

```powershell
python tools/provider_call.py `
  --run YOUR_RUN `
  --provider deepseek `
  --role math-auditor `
  --task "audit the derivation" `
  --include-run synthesis\FINAL_MODEL_SPEC.md `
  --reserve-usd 1 `
  --ack-separate-billing `
  --dry-run
```

Always inspect `PACKET.md` in dry-run first. `.env` and common secret files are blocked; binary/unsupported inputs are skipped. This tool does not provide autonomous Bash/file tools.
Real external calls are disabled for `competition` runs. Before transport, a
rehearsal/benchmark/calibration call reserves the larger of `--reserve-usd` and
caller-supplied `--actual-usd`, writes an attempted request, and appends its AI
usage event. A failed or uncertain call keeps that reservation for later billing
reconciliation.

## `diversity.py`

```powershell
python tools/diversity.py analyze
python tools/diversity.py list-families
```

Outputs text similarity, method-family overlap, problem-node coverage, common fragile assumptions and gap suggestions. It is heuristic, not mathematical equivalence proof.

## `run_experiments.py`

```powershell
python tools/run_experiments.py --manifest PATH --dry-run
python tools/run_experiments.py --manifest PATH
```

Uses argument arrays with `shell=False`. Supports `{seed}`, `{output_dir}`,
`{record_dir}`, `{attempt_id}`, `{run_dir}`, `{project_root}` and `{experiment_id}`
placeholders. `expected_artifacts` must be non-empty and contain a fresh non-empty
artifact from this attempt. Metrics JSON is a list (or `{"metrics": [...]}`) whose
rows explicitly contain `metric`, finite numeric `value`, `split`, and `unit`.

Experiment children do **not** inherit the caller's full environment. The runner
uses a small OS/Python allowlist, removes relative/empty `PATH` entries, rejects
manifest variables that look like secrets or can redirect executable/interpreter
loading, and reserves every `CUMCM_*` name for runner metadata. A command's
executable is resolved once and its absolute path is recorded before launch;
relative executable paths must remain inside the run. See `docs/ISOLATION.md` for
the exact policy and its limits. This is environment hygiene, not a filesystem or
network sandbox.

## `verifier_faithful.py`

```powershell
python tools/verifier_faithful.py dry-run --run YOUR_RUN --provider deepseek --config PATH
python tools/verifier_faithful.py run --run YOUR_RUN --provider deepseek --config PATH --reserve-usd 2 --ack-separate-billing
python tools/verifier_faithful.py diagnose
```

Calls the pinned upstream `llm_verifier.select`; requires a true token-logprob
backend. `output_dir` must be inside the explicitly named run. Use `--provider
local` only with `OPENAI_BASE_URL` set to a literal loopback IP; local mode uses
zero API budget, while external providers require acknowledgement and a positive
pre-call reservation.

## `verifier_lite.py`

```powershell
python tools/verifier_lite.py init --config PATH
python tools/verifier_lite.py status --session PATH
python tools/verifier_lite.py advance --session PATH
python tools/verifier_lite.py aggregate --session PATH
```

Discrete reviewer approximation. Output remains `faithful: false`.

## `external_review.py`

```powershell
python tools/external_review.py export --run YOUR_RUN --kind blind-idea
python tools/external_review.py export --run YOUR_RUN --kind adversarial
python tools/external_review.py import --run YOUR_RUN --kind blind-idea --response PATH --model MODEL
```

Exports/imports files only; it does not automate a consumer UI. Packet export is
disabled for `competition` runs because its declared purpose is manual external
upload. `--out` is confined to a non-root directory inside the current run.

## `relay.py`

```powershell
python tools/relay.py init --goal "..." --task "..."
python tools/relay.py status --session PATH
python tools/relay.py advance --session PATH
```

Fresh-context relay through `HANDOFF.json` and shared workspace artifacts.

## `api_burst_openai.py`

Optional OpenAI Responses Multi-agent burst retained from v3:

```powershell
python tools/api_burst_openai.py `
  --run YOUR_RUN `
  --prompt-file PATH `
  --model gpt-5.6-sol `
  --subagents 3 `
  --reserve-usd 2.00 `
  --ack-separate-billing `
  --dry-run
```

It follows the same competition-phase block, pre-call reservation, attempted
request, failure receipt, and AI usage rules as `provider_call.py`.

At G7, the harness reconciles all managed `REQUEST.json`/`EXECUTION.json`
records against `AI_USAGE_LOG.jsonl` and per-provider reservations in the full
`BUDGET.json`. Orphan, duplicate, non-terminal, provider/status/amount mismatch,
or a refunded failed reservation blocks approval; the reconciled files are part
of the G7 input snapshot and delivery allowlist. G0 hashes only the immutable
budget-policy projection, so ordinary usage does not make G0 stale.

G7 requires `deliverables/FINAL_MANUSCRIPT.pdf` and a manuscript section headed
`AI工具使用声明` before the references. Every team needs that section. With no
logged AI use its text must be exactly `本参赛队在竞赛过程中未使用任何AI工具。`;
with logged use it must use the official used-AI wording and agree with
`AI_USAGE_LOG.jsonl`. The default paper plan places it at
`paper/sections/085_ai_usage_declaration.md`.

`build-support` creates the separate official artifact
`deliverables/official/支撑材料.zip` plus the non-submittable internal receipt at
`deliverables/internal/SUPPORT_PACKAGE_RECEIPT.json`. Both the paper
PDF and support ZIP are limited to `20,000,000` bytes. If AI use needs disclosure,
the ZIP must contain the attested `AI工具使用详情.pdf`. All support filenames,
readable content, and metadata must be anonymous. The older `package` command is
an internal audit archive containing named approvals; never upload it as official
supporting material. A named reviewer must still inspect rendered pages, headers,
footers, figures/images, and every support member because machine scans cannot
prove visual anonymity.

## `launch_claude_deepseek.py`

The platform scripts delegate to this run-aware launcher:

```powershell
.\scripts\start_claude_deepseek.ps1 `
  --run YOUR_RUN `
  --reserve-usd 5 `
  --ack-separate-billing `
  -- --continue
```

An interactive session's exact cost is not locally observable. The full
reservation therefore remains charged after either a successful or failed
session until a human reconciles it with the DeepSeek dashboard.

## `self_test.py`

```powershell
python tools/self_test.py
python tools/self_test_egress_policy.py
```

Runs offline static + isolated smoke tests, including runtime adapter consistency and direct DeepSeek provider dry-run. No model/API request is made.

## Paper writing (v5.1)

```bash
python tools/paper.py init
python tools/paper_v5.py init
python tools/paper.py status
python tools/paper.py map-claim --claim C001 --section model_and_solution
python tools/paper.py packet --all
python tools/paper.py assemble
python tools/paper.py lint --strict --pre-audit
python tools/paper.py audit-set --status passed --by <队员姓名>
python tools/paper.py lint --strict --pre-final
python tools/paper.py finalize
```

`paper.py` never calls a model. It is the deterministic shared state/validation layer used by `paper_architect`, `section_writer`, `manuscript_editor`, and the independent paper auditors.
After the substantive sections are stable, register frozen facts and run `paper_v5.py build-abstract`; it resolves the planned `abstract` section rather than writing a hard-coded filename. Then run the canonical all-scope strong-claim scan, approve `strong_claim_audit`, and run `paper_v5.py lint --strict` before approving `paper_extension` and assembling. The strict check requires exact claim/equation/figure coverage and synchronized section status/ownership. After finalization, `paper_v5.py compile` writes only to `deliverables/`, refuses an implicit overwrite, and records `deliverables/PAPER_COMPILE_RECEIPT.json`.

## v5/v5.1 tools

- `interpretation_engine.py`: problem semantics, structural forks, fingerprints and human decisions.
- `sensitivity_engine.py`: separate structural comparisons from parameter sweeps.
- `math_engine.py`: structured mathematical module and obligation ledger.
- `math_workbench.py`: optional SymPy/Pint/CVXPY/Pyomo executable checks.
- `claim_guard.py`: strong-claim proof/certificate provenance. G5 uses the
  independent `STRONG_CLAIMS_MATH.csv` / `STRONG_CLAIM_SCAN_MATH.json`
  receipt; the later all-scope scan uses `STRONG_CLAIMS.csv` /
  `STRONG_CLAIM_SCAN.json` and safely inherits current reviewed math rows.
- `blind_review.py`: sanitized fresh-context packet plus a response template/JSON
  contract that binds imports to the exact packet hash, run and review.
- `consistency_matrix.py`: fine-grained consistency and tolerance matrix.
- `paper_v5.py`: incremental paper facts/equation/citation/compile utilities.
- `v51_orchestrator.py`: non-destructive stage overlay.
- `schema_validate.py`: registry-driven JSON/CSV/schema/producer-contract validation.
- `migrate_v51.py`: dry-run-first migration for early v5.1 artifact contracts.
- `self_test_v51.py`: offline regression tests for all new controls.

Every base Gate directly invokes the v5.1 stages listed in `harness_config.json`.
Both Gate approvals and stage approvals are bound to an input manifest and digest;
changes to relevant artifacts or validator controls require re-approval.
