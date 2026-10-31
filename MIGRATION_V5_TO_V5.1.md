# Migration from v5.0 or v4.1 to v5.1

v5.1 keeps the v4.1 paper pipeline and adds the v5 math layer, interpretation
forks, structural sensitivity, blind review, strong-claim guard and
fine-grained consistency. These stages are compatibility-preserving extensions,
but they are not an optional or manually synchronized overlay: current G0–G7
validation re-runs the stages declared in `harness_config.json` and requires
their current, named-human approvals.

For an existing run:

```powershell
python .\tools\migrate_v51.py --run .\runs\<RUN_ID>
```

The migration command is dry-run by default. Review every proposed mapping.
If a legacy interpretation fork needs fields that cannot be inferred safely,
provide `--fork-decisions <JSON>`; then rerun with `--apply`. Before the first
write, the tool creates a backup and `MIGRATION_MANIFEST.json` below the run's
`migrations/` directory. Unknown columns and lossy legacy probe rows are rejected.

After the contract migration of an existing run, initialize its current stage
state and validate its schemas:

```powershell
python .\tools\v51_orchestrator.py init --run .\runs\<RUN_ID>
python .\tools\schema_validate.py --run .\runs\<RUN_ID>
```

Do not add this second initialization to the new-run workflow:
`tools/harness.py init` already creates canonical v5.1 state for a new run.

Then:

1. Reconstruct important interpretation forks already decided implicitly.
2. Record assumption fingerprints for retained ideas/modules.
3. Separate parameter sweeps from structural comparisons.
4. Convert retained modules to the structured math module format.
5. Export and import a fresh-context blind review packet. Fill the generated
   `RESPONSE_TEMPLATE.md` without changing its exact `Packet-SHA256`, `Run-ID`
   or `Review-ID` declarations, then replace old aggregate PASS statements with
   consistency dimensions.
6. Run the independent math-scope strong-claim scan before G5; this creates `claims/STRONG_CLAIMS_MATH.csv` and `claims/STRONG_CLAIM_SCAN_MATH.json` rather than overwriting the later manuscript audit.
7. Ensure `paper/PAPER_PLAN.json` is initialized, move any still-needed prose from legacy unplanned section files into the planned files, then rebuild the facts-first abstract. Add the required `ai_usage_declaration` section at `paper/sections/085_ai_usage_declaration.md`, before references, when migrating a plan created before that section existed.
8. After every manuscript section is stable, run the canonical all-scope scan and approve the full `strong_claim_audit` before paper approval/finalization.

Legacy section files are never deleted automatically. `paper.py lint` reports files that are not referenced by the current plan. After migration, run `paper.py assemble`, repeat the manuscript audit, and run `paper.py finalize` so `ASSEMBLY_MANIFEST.json` and `PAPER_BUILD.json` bind the current title and ordered included-section projection.

Legacy abstract facts gain `claim_ids` only when every cited evidence ID maps to
exactly one ledger claim. Missing `created_at` values are recorded as migration
time. If a non-empty facts file has no reviewed keywords, or contains unknown
fields, migration stops for manual repair instead of inventing content.

Existing evidence is reusable only after correct classification. A numerical
maximum is not automatically a rigorous upper bound.

Old `approved` flags, an empty strong-claim table without a current scan receipt,
scalar metrics JSON, and experiment rows without a runner-owned `run.json` receipt
are intentionally not grandfathered. Revalidate and re-approve them against the
current artifacts and validators.

Legacy `TEAM`, `AUTO`, `CODEX`, `GPT`, `UNKNOWN` or timezone-free human-review
records are not grandfathered either. Replace them through the corresponding
CLI with a concrete team member and a timezone-aware timestamp; do not edit an
approval hash in place.

Gate approval now has a mandatory human separation: run `review --gate Gx`, open
and read `reviews/gates/Gx_REVIEW.md`, then run `approve --gate Gx --by <name>`.
Approval never creates or repairs the report. Existing approvals must therefore be
re-established after migration with a current, unmodified review summary.

All migrated papers need an `AI工具使用声明`, including no-AI runs. Use the exact
no-AI sentence `本参赛队在竞赛过程中未使用任何AI工具。`, or the official used-AI
wording consistent with `AI_USAGE_LOG.jsonl`. For final delivery, rebuild the
official anonymous archive with `harness.py build-support`; do not reuse a legacy
`package` archive. `package` contains named internal audit material and is not an
official support submission. The manuscript PDF and support ZIP are separate,
anonymous deliverables, each limited to `20,000,000` bytes.
