# Workflow modes (descriptive guide)

This document explains supported workflows. It is not an executable registry;
runtime definitions live in `config/runtime_registry.json`, while mandatory
scientific stages live in `config/v51_stage_registry.json`.

## baseline

Create a transparent end-to-end baseline early. It is a reference point, not a disposable toy. Every complex model should show what it improves over the baseline.

## adaptive-research

Default modeling workflow. It uses blind idea cards, diversity analysis, targeted expansion, module deepening and evidence-driven integration.

## reference-verifier-faithful

Calls the official `llm-verifier` package. Requires a verifier backend that returns token-level logprobs. Candidate trajectories may come from Codex, Claude Code, direct API providers, manual external reviewers or human-written solutions.

## reference-verifier-lite

No API required. Creates pairwise review packets and locally aggregates structured reviews. It is intentionally labeled non-faithful because it lacks logprob expectations.

## fresh-agent-relay

Starts a new Agent context each round. Durable state lives in files; each handoff states completed work, evidence, unresolved risks and the next bounded task. Insert independent audits to prevent inherited errors from compounding.

## external-pro-review

A human opens a separate ChatGPT Pro conversation. Blind-idea packets omit current candidates to reduce anchoring; adversarial packets include the chosen solution and ask for attacks.

## direct-api-role

Bounded canonical-role call through OpenAI, Anthropic or DeepSeek using an explicit text-file packet. This mode has no arbitrary local shell/file tools.

## api-burst

Optional OpenAI Responses Multi-agent call for one bounded uncertainty. Use only when the expected value exceeds cost and the API budget is explicitly enabled.

## paper-writing

Transforms frozen scientific artifacts into a manuscript through `paper_architect`, bounded `section_writer` tasks, `manuscript_editor`, deterministic assembly and trace/evidence lint. It may change prose and manuscript structure, but not frozen scientific truth.

## red-team and paper-audit

These modes do not invent a new model. They attack assumptions, code, edge cases, claims, figures, citations and consistency. Paper audit is downstream of paper-writing and must be tied to the exact assembled draft and freeze hashes.
