# Prompt index

The numbered prompts are stage-level control packets for any supported runtime. They reference only tools and canonical agent roles shipped in this repository.

- `00`–`05`: intake, exploration, deepening, experiments, integration and red-team lifecycle.
- `06_PAPER_WRITE.md`: actual evidence-first manuscript production.
- `07_PAPER_AUDIT.md`: independent manuscript audit, revision and finalization.
- `06_PAPER_AUDIT.md`: compatibility pointer retained for v4.0 users.
- `10`–`14`: optional independent modes.
- `MODE_*`: short routing reminders.

Always read the active runtime rules (`AGENTS.md` or `CLAUDE.md`) and the applicable Skill first. Do not paste every prompt at once.

## v5.1 inserts

- `00A_INTERPRETATION_FORKS.md` runs before model search/deepening.
- `02A_MATHEMATICAL_CONSTRUCTION.md` runs after interpretation decisions.
- `05A_BLIND_INDEPENDENT_REDTEAM.md` runs before final scientific approval.
- `07A_STRONG_CLAIM_AND_CONSISTENCY.md` runs before manuscript finalization/submission.
