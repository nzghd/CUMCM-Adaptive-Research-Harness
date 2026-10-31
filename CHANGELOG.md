# Changelog

## Public repository preparation — 2026-10-31

- Added Chinese and English repository landing pages, a contribution guide,
  security policy, issue/PR templates and Windows offline-check CI.
  CI explicitly uses UTF-8 so Chinese CLI output works on English Windows runners.
- Preserved the detailed v5.1 overview in `GUIDE_V5.1.md`.
- Excluded local problem/data files from Git while retaining public scaffolds;
  preserved exact file bytes across checkouts for release-manifest integrity.
- Repaired lifecycle test fixtures to create the required `interpretation/`
  scan root and place the language probe in the canonical scan corpus.
  The bound-downgrade test now selects its live source claim by path and
  separately reviews the resolved obligation's historical wording.
  It refreshes the math-scan receipt after final science setup, before review.
  Validator requirements are unchanged.
- Resolve the base smoke-test temporary root before passing it to path guards,
  including Windows runners whose TEMP uses an 8.3 short-name alias.

## 5.1.0 — 2026-09-03

- Built from the verified v4.1 archive; preserved the complete paper pipeline and runtime adapters.
- Added problem-semantics and interpretation-fork layer before mathematical construction.
- Added assumption fingerprints and three-level diversity: interpretation, model structure and algorithm.
- Added nondelegable critical decisions that cannot be closed by AUTO/DEFAULT/AGENT identities.
- Split structural sensitivity from parameter sensitivity.
- Added structured mathematical modules, equation/parameter registries, mathematical obligations and model-to-code mapping.
- Added optional SymPy, Pint, CVXPY and Pyomo workbench adapters.
- Added strong-claim provenance guard for bounds, optimality, proof, convergence, stability, significance and robustness.
- Added sanitized fresh-context blind-review packets with hash-checked import metadata.
- Added fine-grained consistency matrix with PASS_WITH_TOLERANCE.
- Added incremental paper extensions and new canonical specialist roles.
- Added regression tests for shared-worldview diversity and numerical-search-as-bound failures.
- Unified facts-first abstract generation with the section path declared by `PAPER_PLAN.json` and added strict evidence/freshness checks.
- Bound assembly and final-build records to the current title and ordered included-section projection; legacy unplanned section files are preserved and reported.
- Connected v5.1 validators to G0–G7 and bound Gate/stage approvals to current artifact and validator hashes.
- Added a canonical strong-claim scan receipt so a pre-created empty table cannot masquerade as a completed scan.
- Activated the schema registry for JSON/CSV artifacts and producer contracts; added dry-run-first migration for legacy v5.1 tables.
- Required run-local evidence references, fresh runner-owned experiment receipts, structured finite metrics, strict solver success states, and complete consistency dimensions.
- Made blind-review verdicts and packet freshness fail closed; only `PASS` with `Blockers: NONE` is accepted.
- Kept mutable review/paper artifacts outside the science freeze to remove the freeze/audit dependency cycle.
- Made Gate and v5.1 status records fail closed: canonical keys, named/timezoned approvals, current validator/config hashes, exact input manifests and downstream invalidation are enforced.
- Added runner-owned experiment receipts, canonical science-freeze verification, manifest-bound packaging, and semantic G7 checks for AI disclosure, sources, checklist sign-off and optional compiled PDF receipts.
- Narrowed strong-claim scanning to canonical narrative files, reduced false positives in headings/code/ordinary derivation language, and ordered the full-corpus audit after consistency and stable manuscript sections.
- Made consistency evidence resolvable and non-self-referential; required dimensions cannot use `NOT_APPLICABLE`, while optional waivers require explicit policy, reason, reviewer and timestamp.
- Added exact paper claim/equation/figure coverage, plan/register ownership synchronization, frozen abstract traceability, and atomic provenance-bound PDF compilation.
- Hardened legacy migration so stale approvals and invalid lifecycle states are downgraded for explicit re-validation instead of being silently trusted.
- Split the mandatory pre-G5 math strong-claim audit into independent registry/receipt files, with fail-closed provenance inheritance into the later all-corpus scan.
- Added typed named-human AI disclosure PDF receipts, canonical checklist coverage, and a provenance-only atomic delivery allowlist.
- Bound strong-claim receipts to their run and repaired heading, repeated-match, negation and statistical-method handling.
- Schema-validated compile receipts and bound canonical paper/AI PDFs to successful typed audits before packaging.
- Extended base and v5.1 regressions through real assemble, finalize, G7 and package paths.

## 4.1.0 — 2026-09-01

- Added an actual evidence-first manuscript-writing stage instead of placeholder paper files.
- Added canonical roles: `paper_architect`, `section_writer`, and `manuscript_editor`.
- Added canonical Skill `cumcm-paper-writing` and runtime-generated Codex/Claude Code copies.
- Added `tools/paper.py` with `init`, `map-claim`, `section-set`, `packet`, `assemble`, `audit-set`, `lint`, `finalize`, and `status`.
- Added `PAPER_PLAN`, claim map, symbol table, figure/table register, bounded section workspaces, assembly/build manifests, audit state, and revision log.
- G6 now hard-checks frozen scientific truth, core claim/evidence mapping, claim trace tags, current assembly hashes, current audit hashes, and final manuscript build hashes.
- Finalization strips internal trace tags from `FINAL_MANUSCRIPT.md`; writing agents edit section files rather than the generated draft.
- Added `docs/PAPER_WRITING.md`, stage prompts, updated quickstart, and offline smoke coverage for the complete paper pipeline.

## 4.0.0 — 2026-09-01

- Split the scientific Harness Core from model/runtime-specific agent configuration.
- Added canonical logical roles in `config/agent_roles.json` and canonical project Skills under `skills/`.
- Added `tools/runtime.py` to render/check Codex and Claude Code agent/skill adapters from one source.
- Added `CLAUDE.md`, `.claude/agents/*.md` and `.claude/skills/*/SKILL.md` for native Claude Code operation.
- Added `claude-code-deepseek` runtime plus Windows/macOS/Linux launcher scripts using DeepSeek's Anthropic-compatible Claude Code integration.
- Added runtime selection to `harness.py init`: `codex`, `claude-code`, `claude-code-deepseek`, `direct-api`.
- Added runtime-neutral concurrency (`--max-agent-threads`) while retaining the v3 `--max-codex-threads` alias.
- Added per-provider OpenAI/Anthropic/DeepSeek API budgets while retaining v3 OpenAI budget compatibility.
- Added `tools/provider_call.py` for bounded, auditable canonical-role calls through OpenAI, Anthropic or DeepSeek; it intentionally has no arbitrary local shell/file tool loop.
- Preserved the v3 Problem Graph, G0–G7, adaptive search, diversity checks, evidence ledger, experiment runner, Verifier modes, Relay, OpenAI API burst, freeze and packaging semantics.
- Extended offline self-tests to cover generated runtime consistency, runtime persistence, DeepSeek budget setup and a zero-network direct-provider dry-run.

## 3.0.0 — 2026-08-31

- Replaced the fixed candidate funnel with a problem-graph-centered adaptive research loop.
- Added dynamic research briefs, budget ledger, human Gates and dispute-triggered specialist work.
- Added deterministic local idea diversity, method-family coverage and gap analysis.
- Preserved the official LLM-as-a-Verifier package as a separate Faithful mode pinned to an upstream commit.
- Added an explicitly non-faithful Pro-only Lite verifier with prechecks, reversed A/B reviews and pivot-style aggregation.
- Added fresh-agent relay, manual independent Pro review, and optional separately billed OpenAI API multi-agent bursts.
- Added a no-shell experiment runner, claim–evidence ledger, source ledger, AI-use export, artifact hashing and freeze verification.
- Renamed “training” concepts to rehearsal, benchmark and calibration; no model-weight training is claimed.
- Added an offline isolated self-test covering initialization, diversity, external review, relay, experiments, both verifier wrappers, API dry-run, freeze and packaging.
