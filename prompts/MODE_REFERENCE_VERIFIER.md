# Mode: reference verifier

Use `$cumcm-reference-verifier`. Decide faithful versus Lite before running anything. Faithful requires token-level logprobs and calls the pinned official package. Lite uses structured discrete reviews and must remain labeled `faithful: false`. In both modes, do objective prechecks, preserve hashes/configuration, and treat selection as evidence rather than truth.
