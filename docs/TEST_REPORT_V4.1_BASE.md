# Test Report — CUMCM Adaptive Research Harness v4.1

> Historical baseline result only. The current v5.1 release result is recorded
> in [`../TEST_REPORT_V5.1.md`](../TEST_REPORT_V5.1.md); this file must not be
> read as a test result for the present worktree.

**Test date:** 2026-09-01  
**Test environment:** Linux, Python 3.13.5  
**Scope:** offline static validation and isolated smoke tests of the runtime-neutral research + manuscript package.

## Result

**PASS** — the current worktree passed `python tools/self_test.py` with no network/model/API call.

Expected terminal result:

```text
OK: CUMCM Adaptive Research Harness v4.1 static + isolated smoke tests passed; no network/model/API calls made.
```

## Additional release checks

The release worktree also passes:

- Python bytecode compilation for public tools;
- JSON parsing for configuration, schemas, templates and upstream locks;
- TOML parsing for distributed TOML files;
- `python tools/runtime.py check`;
- runtime-generated Codex/Claude Code role and Skill synchronization;
- `tools/paper.py --help` and manuscript subcommand parser checks;
- Bash syntax checks for shipped shell scripts;
- local Markdown link resolution checks;
- ZIP extraction and a second self-test from the extracted release tree.

## Covered

1. Required v4.1 files, JSON registries and pinned Verifier metadata.
2. Canonical role registry including `paper_architect`, `section_writer`, `manuscript_editor` and `paper_auditor`.
3. Generated Codex/Claude Code agent counts and runtime synchronization.
4. Run initialization with `runtime=claude-code-deepseek`.
5. Separate OpenAI and DeepSeek API budget initialization.
6. Run path-containment protection.
7. Problem Graph and synthetic independent Idea Cards.
8. Diversity analysis.
9. Reproducible local experiment runner, seeds, metrics and artifacts.
10. Gate prerequisite protection.
11. AI-use logging and disclosure export.
12. Manual external-review packet export/import.
13. Fresh-Agent Relay.
14. Faithful Verifier dry-run/diagnosis only; no backend call.
15. Lite Verifier deterministic pairwise/pivot flow.
16. DeepSeek direct-provider dry-run with no request or budget consumption.
17. OpenAI Multi-agent burst dry-run with no request or budget consumption.
18. Scientific freeze and verification.
19. **Paper workspace initialization after freeze, with an unwritten manuscript explicitly blocked from G6.**
20. **Core claim → section mapping and human-verified evidence trace.**
21. **Bounded section packet generation.**
22. **Section assembly into `PAPER_DRAFT.md`.**
23. **Structured paper audit state bound to the exact draft and freeze hashes.**
24. **Strict pre-audit and pre-final paper lint.**
25. **Paper audit hash binding: an unreviewed post-audit section edit invalidates the audit until re-reviewed.**
26. **Final manuscript generation with internal claim/section trace tags stripped.**
27. **Strict G6 validation against the completed manuscript.**
28. Run packaging including the manuscript layer.
29. Deliberate post-freeze scientific tamper detection.

## Not claimed as tested

The offline suite cannot verify user-account or network-dependent behavior, including:

- actual Codex subagent spawning in the product;
- actual Claude Code subagent spawning on the user's machine;
- live DeepSeek-through-Claude-Code authentication, quotas or billing;
- live OpenAI/Anthropic/DeepSeek direct API calls;
- a real Faithful Verifier logprob backend;
- manuscript quality on a real CUMCM problem;
- official Word/PDF typesetting or visual compliance with the current submission template;
- any competition award outcome.

Before live competition use, run one complete past-paper rehearsal on the same Windows machine, runtime, provider accounts, solver environment and final typesetting workflow intended for the contest.
