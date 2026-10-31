# Reference verifier — faithful mode

Use `$cumcm-reference-verifier` only for comparable candidate artifacts and only with a backend exposing token-level logprobs. First perform objective prechecks. Copy the faithful config template into the active run, preserve candidate hashes, criteria, model, repeats, pivots, seed and cache, then run:

```text
python tools/verifier_faithful.py dry-run --config <CONFIG>
python tools/verifier_faithful.py run --config <CONFIG>
```

Reject flat or near-0.5 score collapses as invalid diagnostics. Treat output as verifier preference evidence, never as a correctness probability or mathematical proof.
