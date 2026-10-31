# G1 — Blind exploration

Use `$cumcm-adaptive-research` on the active run.

1. Require team approval of G0 before continuing.
2. Ask `meta_scientist` for the smallest useful batch of bounded briefs, based on the approved problem graph, open nodes, budgets and stopping rules. Do not impose a fixed Agent count.
3. Spawn `research_specialist` agents in waves no larger than the configured concurrency. First-round specialists may read only public inputs, approved G0 artifacts and their own brief; they must not read sibling idea directories.
4. Each specialist writes one structured `research/ideas/<IDEA_ID>/IDEA_CARD.json` and concise notes without inventing results. Register or inspect cards with `tools/harness.py idea-new` / `idea-list` as appropriate.
5. Run `python tools/diversity.py analyze`. Stop before selecting or merging ideas.
