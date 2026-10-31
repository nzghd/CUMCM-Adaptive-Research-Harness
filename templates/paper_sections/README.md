# Paper section templates

Section workspaces are generated from `config/paper_profile_cumcm.json` by:

```bash
python tools/paper.py init
```

Do not copy numbered files from this directory into a run. Within a run,
`paper/PAPER_PLAN.json` is the only source of truth for section IDs, ordering and
output paths. The facts-first abstract generator resolves the planned
`abstract` section instead of using a hard-coded filename.
