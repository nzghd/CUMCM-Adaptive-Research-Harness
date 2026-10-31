# G6 — Evidence-first paper writing

Use `$cumcm-paper-writing` (Claude Code: `cumcm-paper-writing`). Do not use the research skill as a one-shot paper generator.

1. Verify the G5 audit state and freeze the scientific artifacts.
2. Run `python tools/paper.py init`.
3. Invoke `paper_architect` to build the paper brief, plan, claim map, symbol table, figure/table register and customized outline.
4. Run `python tools/paper.py packet --all`.
5. Dispatch bounded `section_writer` tasks. Each writer may edit only its assigned `paper/sections/*.md` and must preserve claim trace tags. Write the abstract last.
6. Invoke `manuscript_editor` for terminology, notation, transitions and whole-paper coherence without altering frozen scientific truth.
7. Run `python tools/paper.py assemble` and `python tools/paper.py lint --strict --pre-audit`.
8. Stop and report any scientific/evidence blockers instead of fabricating prose to fill them.

Do not finalize until independent paper, red-team and compliance audits have been reviewed.
