# CUMCM Adaptive Research Harness

**A research workflow for mathematical modeling: from interpretation forks to evidence-backed manuscripts.**

[中文](README.md) · [Windows quickstart (Chinese)](QUICKSTART_V5.1_WINDOWS.md) · [Full guide (Chinese)](GUIDE_V5.1.md) · [Contributing](CONTRIBUTING.md)

This independent community project helps teams and coding agents structure a modeling problem, explore alternative assumptions, run experiments, track evidence, and write a manuscript from frozen results. It targets CUMCM practice and can be adapted to other mathematical-modeling projects. It is not affiliated with the competition organizers.

Current version: **5.1.0**. Python **3.10+**. Windows / PowerShell is the primary documented and validated environment. Most task instructions and user guides are in Chinese.

## What it provides

- Interpretation forks and assumption fingerprints before deep modeling.
- Adaptive idea exploration and module selection, with no fixed agent funnel.
- Structured equations, derivations, mathematical obligations and code mappings.
- Experiment receipts, source records and a claim–evidence ledger.
- Blind-review packets, strong-claim provenance checks and consistency audits.
- G0–G7 checkpoints with named human approvals bound to current artifact hashes.
- A science freeze followed by evidence-driven drafting, auditing and packaging.

Agreement between agents is not mathematical evidence. Numerical search does not establish a rigorous bound or global optimality. The harness supports auditability; it does not guarantee correct science or competition results.

## Quick start

Run in PowerShell with Git and Python installed:

```powershell
git clone https://github.com/nzghd/CUMCM-Adaptive-Research-Harness.git
cd CUMCM-Adaptive-Research-Harness
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-core.txt
.\.venv\Scripts\python.exe -B tools/runtime.py check
.\.venv\Scripts\python.exe -B tools/schema_validate.py --static
```

The core workflow primarily uses the standard library. PDF delivery checks additionally require `pypdf`. Installing packages requires network access; the checks above do not call model services.

Place authorized problem statements in `inputs/problem/` and attachments in `inputs/data/`, then initialize a practice run:

```powershell
.\.venv\Scripts\python.exe tools/harness.py init `
  --contest CUMCM-PRACTICE --problem A --phase rehearsal --runtime codex `
  --time-budget-hours 72 --human-review-hours 18 `
  --max-exploration-batches 3 --max-agent-threads 4
.\.venv\Scripts\python.exe tools/harness.py status
.\.venv\Scripts\python.exe tools/harness.py next
```

This creates a scaffold under `runs/<RUN_ID>/`, not completed research. Keep original inputs read-only after initialization. Local input data, generated runs and secrets are ignored by Git.

Open the project in your configured coding-agent runtime and follow the [quickstart](QUICKSTART_V5.1_WINDOWS.md). Codex uses `AGENTS.md`; Claude Code uses `CLAUDE.md`. Each checkpoint requires a review summary, a team member reading it, and approval only after validation succeeds. Empty templates cannot pass a research checkpoint.

Run the complete offline software regression suite with:

```powershell
.\scripts\check_release.ps1 -PythonExe "$PWD\.venv\Scripts\python.exe"
```

## Runtimes and optional integrations

The scientific protocol is shared across Codex, Claude Code, an optional Claude Code + DeepSeek backend, and bounded direct API calls. Install and configure the desired runtime separately. Model identifiers in configuration files must be checked against your provider account.

API access is disabled by default. Enabling it requires a budget, explicit confirmation and AI-usage records; provider charges are separate. The optional Faithful Verifier requires token-level logprobs. The Lite workflow is not a faithful implementation of the upstream method, and its scores are not probabilities of correctness.

See [runtime adapters](docs/RUNTIMES.md), [optional APIs](docs/API_OPTIONAL.md), [Verifier](docs/REFERENCE_VERIFIER.md), and [mathematical workbench](docs/V5_MATHEMATICAL_MODELING_ENGINE.md).

## Repository map

| Directory | Purpose |
| --- | --- |
| `config/`, `skills/` | Canonical roles, policies and workflows |
| `tools/`, `schemas/` | CLIs, validators, tests and artifact contracts |
| `templates/`, `prompts/` | Research, modeling, review and paper scaffolds |
| `method_packs/`, `examples/` | Method references and small workbench examples |
| `docs/` | Architecture, limitations and design decisions |
| `inputs/`, `runs/` | Local inputs and generated research workspaces |

Runtime role files and skill mirrors are generated from canonical definitions. See [architecture](docs/ARCHITECTURE.md), [domain terms](CONTEXT.md), [command index](tools/README.md), [limitations](docs/LIMITATIONS.md), and [migration](MIGRATION_V5_TO_V5.1.md).

## Contributing and license

Read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a pull request and [SECURITY.md](SECURITY.md) for private vulnerability reporting. Use synthetic, anonymized reproductions instead of private competition workspaces.

Teams must verify current official competition rules and disclose AI use as required. Do not search for, publish or discuss ready-made solutions to an ongoing contest. Year-specific repository policies are not a substitute for current official notices.

[MIT License](LICENSE). See [third-party notices](THIRD_PARTY_NOTICES.md). User-supplied data and problem statements are not automatically covered by this license.
