# Adaptive Research protocol

## 1. Intake

Produce:

- `problem/PROBLEM_GRAPH.json`
- `data/DATA_AUDIT.md`
- `problem/ASSUMPTION_REGISTER.csv`
- `problem/VALIDATION_MAP.md`

Do not start with a list of fashionable algorithms. Start from decisions, observables, constraints, uncertainty and what can be checked.

## 2. Blind idea generation

The orchestrator writes bounded briefs in `research/briefs/`. Each Explorer receives only:

- original inputs;
- approved problem graph;
- its own brief;
- shared output schema.

It must not read other idea directories in round one.

## 3. Diversity check

Run `tools/diversity.py analyze`. Review both the automated report and the substance manually. Expand when:

- important graph nodes have no viable candidate;
- all routes share a fragile assumption;
- method families are heavily duplicated;
- every route lacks a credible validation plan;
- a materially different specialty is missing.

Stop expanding when two consecutive batches add no new viable family, the critical nodes are covered, or human-review capacity is saturated.

## 4. Module deepening

Convert promising idea cards into module cards. Require:

- equations and variable definitions;
- parameter-estimation path;
- implementation plan;
- baseline;
- falsification test;
- expected failure modes;
- paper value.

## 5. Dispute-triggered review

Create targeted work only for live uncertainty. Examples:

- mathematical validity;
- data leakage;
- identifiability;
- statistical significance;
- optimization feasibility;
- complexity vs. gain;
- source verification.

## 6. Integration

Choose modules by compatibility and evidence, not by Agent identity. Record interfaces between modules and run an end-to-end consistency test.
