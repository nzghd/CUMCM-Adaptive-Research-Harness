# Evidence engine

## Evidence hierarchy

Prefer, in order:

1. executable tests, constraint checks and reproducible solver outputs;
2. held-out evaluation, statistical tests, sensitivity and ablation;
3. verified derivations with clearly stated conditions;
4. authoritative source records;
5. structured expert judgment;
6. unverified LLM opinion.

## Claim–evidence ledger

Every core paper claim has:

- claim text and paper section;
- evidence type;
- artifact path and hash;
- generation command and random seed;
- result summary;
- human verifier and date;
- limitations.

A claim may cite multiple evidence records. A visually attractive figure is not evidence unless its data source is traceable.
