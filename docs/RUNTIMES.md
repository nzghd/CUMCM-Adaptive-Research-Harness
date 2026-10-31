# Runtime Adapters

v5.1 继承并强化了 v4 的运行时分离：CUMCM Core 的 Problem Graph、
G0–G7、v5.1 stage、Evidence、Experiments、Freeze、强主张扫描和论文产物
契约都不应随模型或供应商改变。

## Runtime-neutral boundary

`harness_config.json` 定义公开 Gate 生命周期，
`config/v51_stage_registry.json` 定义有序的 v5.1 校验 stage。基础 Gate 会
重跑相应 stage，并校验其具名人工批准、带时区时间和当前输入哈希。
因此切换 Codex、Claude Code、DeepSeek 或受限 Direct API 只会改变执行
Adapter，不会降级 Gate/stage 契约。

## Canonical roles

单一事实源：

```text
config/agent_roles.json
skills/
```

生成：

```bash
python tools/runtime.py render --runtime all
python tools/runtime.py check
```

请不要手工把 `.codex/agents/` 或 `.claude/agents/` 改成新的事实源；
它们是由 canonical role/Skill 渲染的 Adapter 产物。

## Codex

- Rules: `AGENTS.md`
- Agents: `.codex/agents/*.toml`
- Skills: `.agents/skills/*/SKILL.md`
- Native workspace actions: yes

## Claude Code

- Rules: `CLAUDE.md`
- Subagents: `.claude/agents/*.md`
- Skills: `.claude/skills/*/SKILL.md`
- Native workspace actions: yes

Claude Code project subagents are Markdown files with YAML frontmatter and run in separate context windows. The generated files inherit the main session model unless the runtime/provider overrides it.

Official docs: https://code.claude.com/docs/en/sub-agents

Skills docs: https://code.claude.com/docs/en/skills

## Claude Code + DeepSeek

This mode uses Claude Code as the agent/coding runtime while pointing its Anthropic-compatible endpoint to DeepSeek.

Windows:

```powershell
$env:DEEPSEEK_API_KEY="..."
.\scripts\start_claude_deepseek.ps1 --run YOUR_RUN --reserve-usd 5 --ack-separate-billing
```

macOS/Linux:

```bash
export DEEPSEEK_API_KEY='...'
./scripts/start_claude_deepseek.sh --run YOUR_RUN --reserve-usd 5 --ack-separate-billing
```

The launcher defaults to the model names currently documented by DeepSeek and
allows environment overrides. It enforces the run phase, reserves budget, and
writes attempt/outcome provenance before and after the interactive child. Exact
session cost still requires provider-dashboard reconciliation. API billing
remains separate from any Claude/ChatGPT subscription.

Official DeepSeek guide: https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/

## Direct API

`tools/provider_call.py` provides a deliberately constrained packet interface:

```text
canonical role + task + explicit text files → one provider request → RESPONSE.md
```

Supported providers:

- OpenAI Responses API;
- Anthropic Messages API;
- DeepSeek OpenAI-compatible Chat Completions API.

It does not expose arbitrary local shell execution. Use it when an independent reviewer or bounded researcher is useful; use a native coding runtime for iterative implementation.

## Runtime choice

Use `codex` or `claude-code` when the task needs code edits, tests, experiments or repeated file access. Use `claude-code-deepseek` when you want DeepSeek reasoning inside a mature coding-agent runtime. Use `direct-api` for a narrow independent call with explicit context.

## Independence caveat

Separate subagent contexts reduce context contamination but do not make workers statistically independent. Different provider/model families can add useful diversity, but agreement among them is still not proof. The Harness therefore routes disagreements toward executable tests, mathematics, data checks or source verification.
