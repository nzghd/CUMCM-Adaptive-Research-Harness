# Blind Review and Strong-Claim Provenance

## Blind independent review

`tools/blind_review.py export` creates a sanitized packet excluding prior
reviews, rankings, decisions and model/provider history. Importing a response
with `--fresh-context` records an attestation; the tool cannot independently
verify what the reviewer saw outside the packet.

Each export also writes `RESPONSE_CONTRACT.json` and a prefilled
`RESPONSE_TEMPLATE.md` next to the packet ZIP. Reviewers must preserve the
template's exact `Packet-SHA256`, `Run-ID`, and `Review-ID` lines in their
`RESPONSE.md`. Import and later validation both check those declarations
against the current packet, so a response from another run, review, or older
packet cannot be reused. `--attested-by` must identify a named human; team or
automation placeholders are not accepted.

## Strong claims

`tools/claim_guard.py scan` detects claims about rigorous bounds, global
optimality, uniqueness, convergence, stability, significance, robustness,
proof and optimality gaps.

A numerical search or finite sample may be valuable evidence, but cannot by
itself certify a rigorous upper bound or global optimum. Attach an allowed
proof/certificate provenance or downgrade the wording to the tested empirical
scope. A `downgraded` registry status records the decision only: edit the
canonical source sentence and run a fresh full-corpus scan. Strict lint remains
blocked while the original strong wording is still present.

Run a `--scope math` scan before G5 as a mandatory scientific audit. It writes
the dedicated `STRONG_CLAIMS_MATH.csv` and `STRONG_CLAIM_SCAN_MATH.json`, which
G5 validates and hashes without being overwritten by the later full scan. The canonical
`strong_claim_audit` approval is intentionally ordered after `consistency`:
finish every manuscript section (including the generated abstract), run the
default all-scope scan, resolve its registry, then approve `strong_claim_audit`
before `paper_extension` and G6. This ordering keeps the G5 scientific freeze
stable while still binding the wording that is actually submitted. Any later
edit to a scanned section invalidates the receipt and both downstream approvals.
The all-scope scan reuses reviewed math provenance only after the dedicated math
receipt is proven complete and current.
