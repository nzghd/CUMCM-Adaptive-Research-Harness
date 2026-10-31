# Limitations

1. **No award guarantee.** The Harness structures research and evidence; it does not guarantee mathematical correctness or competition placement.
2. **Multi-agent is not independence.** Separate contexts reduce contamination, but same-model agents share training biases. Cross-provider diversity helps but still does not constitute proof.
3. **Runtime products change.** Codex, Claude Code and provider APIs can change behavior, flags, model names or quotas. Run a full rehearsal on the actual competition machine.
4. **Direct API is bounded.** `provider_call.py` does not expose arbitrary shell/file tools and should not be mistaken for a full coding agent.
5. **Binary inputs.** The direct packet runner intentionally skips unsupported/binary artifacts. Native coding runtimes or preprocessing are needed for PDFs, images, spreadsheets and large binary data.
6. **Budget accounting is conservative.** Direct API calls can reserve a user-specified amount; actual provider billing must be reconciled. Claude Code + DeepSeek usage cannot be automatically metered by the local Harness.
7. **Verifier limitations.** Lite is not the official logprob verifier. Faithful mode depends on an external backend that really provides the required token-level signals.
8. **Heuristic diversity.** Local text/method-family similarity is a diagnostic aid, not a proof that two mathematical models are equivalent or distinct.
9. **Human gates matter.** Incorrect human approval can propagate bad assumptions. Gate files are safeguards, not substitutes for careful review.
10. **Competition policy.** Rules and AI-disclosure requirements can change by year; verify current official CUMCM requirements before live use.
11. **Experiment runner is not a sandbox.** It filters inherited environment secrets, reserves process-control variables, confines `cwd` and relative executable paths to the run, and records the resolved executable. Experiment code still runs with the user's filesystem permissions and network access; use a container/VM or OS policy when those capabilities must be denied.
