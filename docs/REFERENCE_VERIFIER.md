# Reference Verifier modes

## What the official method does

The official package evaluates candidate trajectories with fine-grained rewards. Instead of using only one sampled discrete score, it computes an expectation over the probability distribution of score tokens, repeats evaluations, decomposes criteria, and ranks candidates with a cost-efficient Probabilistic Pivot Tournament.

The locked upstream package exposes:

- `llm_verifier.select(...)`
- `llm_verifier.compare(...)`
- `llm_verifier.track(...)`

Its Best-of-N selector uses a ring pass, chooses pivots, then compares candidates against pivots rather than running a full round-robin.

## Faithful backend requirements

A compatible backend must expose token-level logprobs. The upstream README lists DeepSeek API, Vertex, and OpenAI-compatible local servers such as vLLM as examples. A normal ChatGPT Pro or Codex UI session does not supply these logprobs to local Python.

## Lite backend

Lite uses the same broad tournament idea but different evidence:

- deterministic prechecks;
- independent structured reviewer scores;
- A/B and B/A orientations;
- local aggregation.

It must never be reported as a reproduction of the paper's continuous reward.

## Appropriate use in CUMCM

Use verifier selection for bounded artifacts with comparable outputs, such as:

- alternative implementations of one module;
- several derivations of the same formula;
- multiple code repair trajectories;
- competing explanations of a numerical result;
- paper section revisions.

Do not ask it to replace the whole adaptive research process or objective experiments.
