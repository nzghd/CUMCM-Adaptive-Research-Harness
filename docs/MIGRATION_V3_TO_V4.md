# Migration v3 → v4

## Preserved

- G0–G7;
- Problem Graph and Validation Map;
- dynamic exploration and diversity analysis;
- Focus Set / module integration;
- experiment runner;
- Claim–Evidence Ledger;
- Faithful/Lite Verifier;
- Relay, external review, Freeze, packaging;
- OpenAI Multi-agent Burst.

## Changed

### Runtime selection

`init` now accepts:

```text
--runtime codex
--runtime claude-code
--runtime claude-code-deepseek
--runtime direct-api
```

Default remains `codex` for compatibility.

### Generic concurrency

Use:

```text
--max-agent-threads 4
```

Old `--max-codex-threads` remains an alias.

### Provider budgets

New:

```text
--openai-api-budget-usd
--anthropic-api-budget-usd
--deepseek-api-budget-usd
```

Old `--api-budget-usd` remains an OpenAI alias.

### Canonical roles

Do not edit `.codex/agents/*.toml` directly. Edit `config/agent_roles.json`, then run:

```bash
python tools/runtime.py render --runtime all
python tools/runtime.py check
```

### Claude Code

v4 adds `CLAUDE.md`, `.claude/agents/` and `.claude/skills/`.

### DeepSeek

Use `scripts/start_claude_deepseek.*` for DeepSeek as Claude Code backend, or `tools/provider_call.py --provider deepseek` for a bounded direct role call.

## Existing v3 runs

Existing run folders remain readable because the scientific artifact layout is unchanged. Older `BUDGET.json` files may lack `agent_runtime` and `api_providers`; v4 status code uses compatibility fallbacks. For new API-provider features, create a new v4 run or manually add the new budget sections from `config/default_budget.json`.
