# Security policy

## Reporting a vulnerability

Please report vulnerabilities through GitHub's **Security → Report a vulnerability** feature for this repository:

https://github.com/nzghd/CUMCM-Adaptive-Research-Harness/security/advisories/new

Do not publish exploit details, credentials, private research data or team identities in a public issue. Include the affected revision, platform, impact and a minimal synthetic reproduction. No response-time guarantee is currently offered.

## Scope

Security-sensitive areas include run path isolation, external API egress, credential handling, experiment command execution, artifact receipts and packaging. Experiment commands and optional plugins execute code: review their source and run untrusted work in an appropriately isolated environment.

Keep keys in environment variables. `.env.example` contains empty placeholders; `.env`, local inputs and generated runs are ignored by Git. Git ignore rules do not remove secrets already committed. If a key is exposed, revoke or rotate it promptly.

This project currently maintains the latest source on `main`; there is no separate long-term support policy for earlier versions. Scientific correctness and competition-rule compliance still require team review.
