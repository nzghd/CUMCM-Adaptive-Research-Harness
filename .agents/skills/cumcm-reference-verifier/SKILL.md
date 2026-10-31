---
name: cumcm-reference-verifier
description: Select among comparable candidate trajectories using either the official LLM-as-a-Verifier adapter or the explicitly non-faithful Pro-only Lite tournament.
---

# Reference Verifier

Determine the backend first.

## Faithful

Use only when a compatible token-logprob backend is available. Validate the configuration with `tools/verifier_faithful.py dry-run`, then run. Preserve candidate hashes, cache, model, criteria, repeats, pivots and seed.

## Lite

Use when a token-logprob backend is unavailable but an independent runtime/manual reviewer is available. Run `tools/verifier_lite.py init`; fill each generated packet with an independent `verifier_lite_judge` through the active runtime or a manual isolated reviewer; run `advance` after ring reviews and `aggregate` after pivot reviews. Label every report `faithful: false`.

Before either mode, perform objective prechecks. Do not rank candidates that fail required executable constraints unless the purpose is explicitly diagnostic.
