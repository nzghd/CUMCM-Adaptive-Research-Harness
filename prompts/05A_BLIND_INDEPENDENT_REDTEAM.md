# Stage 05A — Blind Independent Red Team

1. Export a sanitized packet with `tools/blind_review.py export`.
2. Review it in a fresh Codex/ChatGPT session, a different model, or by an independent human.
3. Do not provide prior rankings, reviewer conclusions, candidate identities or selection history.
4. Ask the reviewer to falsify interpretation, formulation, proof provenance, code mapping,
   baselines, boundary cases, uncertainty and conclusion scope.
5. Import the response with fresh-context attestation and validate it.

An internal same-session self-review does not satisfy this stage.
