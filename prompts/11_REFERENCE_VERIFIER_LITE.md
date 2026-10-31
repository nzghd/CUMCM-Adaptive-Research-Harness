# Reference verifier — Lite mode

Use `$cumcm-reference-verifier` when a token-logprob backend is unavailable but an isolated runtime/manual reviewer is available. This mode is explicitly non-faithful because it has no score-token logprob distribution.

1. Run `python tools/verifier_lite.py init --config <CONFIG>`.
2. Assign every `stage1/*/PROMPT.md` to an isolated `verifier_lite_judge`; each fills its adjacent `REVIEW.json`.
3. Run `advance`, complete all generated `stage2/*/REVIEW.json`, then run `aggregate`.
4. Confirm the result with executable tests, mathematics and team review.

Do not call aggregate scores probabilities and do not describe this as a reproduction of the paper.
