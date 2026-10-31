# Optional APIs in v5.1

The core Harness does not require a paid API. API use is opt-in and separately budgeted.

## Provider budgets

At `init`:

```text
--openai-api-budget-usd
--anthropic-api-budget-usd
--deepseek-api-budget-usd
```

`BUDGET.json` stores per-provider enabled/max/used values. No key is written to disk.
All real attempts reserve `max(reserve_usd, caller_actual_usd)` before transport.
A failed or uncertain attempt is not refunded automatically because it may have
reached the provider and incurred charges. Reconcile it against the provider
dashboard.

G0 binds a semantic projection of budget policy (enablement, limits, schedule,
concurrency, and complexity), so normal consumption counters do not invalidate
the approved problem setup. G7 binds the complete `BUDGET.json` and rejects a
submission unless each managed external attempt has one terminal
`REQUEST.json`/`EXECUTION.json`, one AI usage event, and one matching provider
reservation in both directions. A terminal `failed-or-unknown` attempt is valid,
but its reservation remains charged until manual reconciliation.

`RUN_META.json.phase` is an active egress policy. Missing/unknown phases fail
closed. `competition` permits local dry-run previews but blocks these external
API paths and manual external-review packet export, with no force flag.

## Direct role calls

Use `tools/provider_call.py` for a bounded role against explicit text artifacts. Always start with `--dry-run`, inspect `PACKET.md`, then remove `--dry-run` only when the packet and budget are acceptable.

The script requires `--ack-separate-billing` and a positive provider budget.
`--actual-usd` can increase, but never reduce, the conservative reservation.
`REQUEST.json`, `ERROR.json` where applicable, and `AI_USAGE_LOG.jsonl` retain
failed/uncertain attempts as well as successful responses.

## OpenAI Multi-agent Burst

`tools/api_burst_openai.py` is retained as a separate optional mode for bounded multi-agent expansion. It is not the default research loop and should normally be used only for a high-value unresolved issue.

## Claude Code + DeepSeek

This route uses DeepSeek API billing through Claude Code. Start it through
`scripts/start_claude_deepseek.ps1` or `.sh` with explicit `--run`, positive
`--reserve-usd`, and `--ack-separate-billing`. The scripts delegate to
`tools/launch_claude_deepseek.py`, which applies phase policy and writes an AI
usage event plus `EXECUTION.json` before launching Claude. Because the child owns
the interactive request loop, the reservation remains conservative until manual
dashboard reconciliation.
