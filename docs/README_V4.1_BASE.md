# CUMCM Adaptive Research Harness v4.1

> **Historical v4.1 baseline.** This file preserves the behavior and examples
> of the v4.1 release; it is not the executable quickstart for the current
> v5.1 harness. In particular, commands using `--by TEAM` do not satisfy current
> named-human review contracts. Use [`../QUICKSTART_V5.1_WINDOWS.md`](../QUICKSTART_V5.1_WINDOWS.md)
> for a new run.

面向全国大学生数学建模竞赛的 **运行时无关（runtime-neutral）动态研究编排系统**。

v4.1 在 v4 的 runtime-neutral 架构上补齐了 **真正的论文写作流水线**。核心仍然是把“研究协议”和“具体 AI 运行时”拆开：

```text
                     CUMCM Core
 Problem Graph / G0-G7 / Diversity / Evidence / Experiments / Freeze / Paper
                           │
                    Canonical Roles
             config/agent_roles.json + skills/
                           │
          ┌────────────────┼────────────────┐
          │                │                │
        Codex          Claude Code      Direct API
          │                │          (bounded packet)
       OpenAI       Anthropic / DeepSeek   OpenAI / Anthropic / DeepSeek
```

它仍然坚持 v3 的核心原则：**不固定 5→3→2→1、不多数投票、按问题结构动态加 Agent、真实代码与证据优先。**

---

## 1. v4 能用哪些 AI

| Runtime | 后端模型 | 工作区工具 | 原生 Subagent | 推荐用途 |
|---|---|---:|---:|---|
| `codex` | OpenAI | 是 | 是 | 默认兼容 v3；完整研究与代码实验 |
| `claude-code` | Anthropic Claude | 是 | 是 | 完整研究、代码、审计；迁移成本低 |
| `claude-code-deepseek` | DeepSeek | 是 | 是 | **Claude Code 当 Agent 外壳，DeepSeek 当模型后端** |
| `direct-api` | OpenAI / Anthropic / DeepSeek | 否 | 否 | 受限的独立研究、审稿、验证调用 |

`direct-api` 故意不是“自动执行 Bash 的全权限 Agent”。它只把明确选择的文本文件与 canonical role 打成一个审计包，然后调用一次模型；适合独立思路、数学审查、证据审查等。

---

## 2. 单一角色源，不维护三套 Prompt

逻辑角色只维护在：

```text
config/agent_roles.json
```

当前包括：

```text
problem_cartographer
meta_scientist
research_specialist
diversity_auditor
evidence_architect
experimenter
math_auditor
data_auditor
code_auditor
red_team
paper_architect
section_writer
manuscript_editor
paper_auditor
compliance_auditor
relay_worker
verifier_lite_judge
```

运行：

```powershell
python .\tools\runtime.py render --runtime all
python .\tools\runtime.py check
```

会自动生成：

```text
.codex/agents/*.toml
.claude/agents/*.md
.agents/skills/*/SKILL.md
.claude/skills/*/SKILL.md
```

以后修改角色职责，**改 `config/agent_roles.json` / `skills/`，不要分别手改两套 runtime 文件。**

---

# 快速开始

## 3. 自检

Python 3.10+：

```powershell
python --version
python .\tools\self_test.py
python .\tools\runtime.py list
```

预期包含：

```text
OK: CUMCM Adaptive Research Harness v4.1 static + isolated smoke tests passed;
no network/model/API calls made.
```

自检不会联网，也不会调用任何模型或产生 API 费用。

---

## 4. 放入赛题

```text
inputs/problem/   题面、补充说明
inputs/data/      Excel、CSV、图片、压缩包等原始附件
```

`inputs/` 按只读处理。`init` 会记录 SHA-256。

---

## 5. 初始化一个 run

### Codex

```powershell
python .\tools\harness.py init `
  --contest CUMCM2025-REHEARSAL `
  --problem A `
  --phase rehearsal `
  --runtime codex `
  --time-budget-hours 72 `
  --human-review-hours 18 `
  --max-exploration-batches 3 `
  --max-agent-threads 4
```

v3 的 `--max-codex-threads` 仍可作为兼容别名。

### Claude Code + Claude

```powershell
python .\tools\harness.py init `
  --contest CUMCM2025-REHEARSAL `
  --problem A `
  --phase rehearsal `
  --runtime claude-code `
  --max-agent-threads 4
```

然后在项目根目录：

```powershell
claude
```

Claude Code 会读取 `CLAUDE.md`、`.claude/agents/` 和 `.claude/skills/`。

### Claude Code + DeepSeek

如果要让 DeepSeek 作为 Claude Code 后端：

```powershell
python .\tools\harness.py init `
  --contest CUMCM2025-REHEARSAL `
  --problem A `
  --phase rehearsal `
  --runtime claude-code-deepseek `
  --max-agent-threads 4 `
  --deepseek-api-budget-usd 10
```

在当前 PowerShell 会话设置 Key：

```powershell
$env:DEEPSEEK_API_KEY="你的 key"
```

然后：

```powershell
.\scripts\start_claude_deepseek.ps1
```

脚本只给**当前进程**设置 DeepSeek 官方 Anthropic-compatible 环境变量，不会把 Key 写入项目文件。

macOS/Linux：

```bash
export DEEPSEEK_API_KEY='...'
./scripts/start_claude_deepseek.sh
```

可选覆盖：

```text
CUMCM_DEEPSEEK_MODEL
CUMCM_DEEPSEEK_SUBAGENT_MODEL
CUMCM_CLAUDE_EFFORT
```

---

## 6. 第一条任务：只做 Intake

无论 Codex 还是 Claude Code，都使用项目 Skill `cumcm-adaptive-research`。可以直接给主控：

```text
使用 cumcm-adaptive-research。
读取 active run。

现在只执行 Intake：
1. 建立 Problem Brief、Question Map、Problem Graph；
2. 建立 Assumption Register 与 Validation Map；
3. 审计 inputs/data 并完成 Data Audit；
4. 严格区分题面事实、解释、建模假设和未知项；
5. 不直接选择最终模型，不写完整论文。

完成后运行：
python tools/harness.py validate --gate G0
然后停止，等待人工审核。
```

人工确认后：

```powershell
python .\tools\harness.py validate --gate G0
python .\tools\harness.py approve --gate G0 --by TEAM --note "已核对题意、单位、数据字段和验证要求"
```

---

# 自适应研究流程

## 7. G0 → G7

| Gate | 目的 |
|---|---|
| G0 | 题意、数据、假设、验证地图 |
| G1 | 盲探索与真正的方法多样性 |
| G2 | Focus Set 与模块深化 |
| G3 | Baseline、真实实验、证据 |
| G4 | 模块整合与争议解决 |
| G5 | Red Team、代码/证据/鲁棒性审计 |
| G6 | 冻结科学结果 → 论文架构 → 分章写作 → 组装 → 独立审计 → 最终正文 |
| G7 | 来源、AI 使用说明与交付检查 |

Harness 不要求一口气自动跑完。关键 Gate 默认需要人工批准。

---

## 8. 独立探索不是“多模型投票”

第一轮 `research_specialist` 应满足隔离规则：

```text
允许读取：
- 原始题面 / 数据；
- 已批准的 problem 文件；
- 自己的 research brief。

禁止读取：
- 其他 specialist 的 Idea Card；
- 其他盲探索目录。
```

第一批结束：

```powershell
python .\tools\diversity.py analyze
```

然后由 `diversity_auditor` 判断：

- 是真正不同的数学结构，还是只换了算法名字；
- Problem Graph 哪些节点未覆盖；
- 是否共享同一脆弱假设；
- 新增 Agent 是否能增加信息，而不是凑数量。

如果三条路线已覆盖关键空间，就停；如果四条全是类似的黑箱拟合，就继续补机理、优化、统计或鲁棒性等缺口。

---

## 9. 最终方案按模块组合

最终论文不要求“某一个 Agent 整套方案获胜”。允许：

```text
Idea A 的数据清洗
+ Idea C 的主模型
+ Idea B 的参数估计
+ Idea D 的鲁棒性分析
```

Focus Set：

```powershell
python .\tools\harness.py focus-set `
  --ideas IDEA_001 IDEA_003 `
  --modules MOD_001 MOD_002 `
  --by TEAM `
  --rationale "覆盖关键小问且模块互补" `
  --evidence reviews/FEASIBILITY_REVIEW.md research/diversity/DIVERSITY_REPORT.md
```

---

## 10. 真实实验优先于 LLM 评分

实验应尽量落到：

- baseline；
- 留出集 / 交叉验证；
- 多随机种子；
- 敏感性分析；
- Bootstrap / 统计检验；
- 消融；
- 极端场景；
- 约束可行率；
- 求解器状态、收敛与运行时间。

执行：

```powershell
python .\tools\run_experiments.py --manifest PATH --dry-run
python .\tools\run_experiments.py --manifest PATH
```

核心论文主张必须登记：

```text
evidence/CLAIM_EVIDENCE_LEDGER.csv
```

---

# 论文写作：v4.1 新增完整阶段

## 11. G6 不再只是 Paper Audit

v4.0 的 `paper/PAPER_DRAFT.md` 只是占位文件。v4.1 增加了真正的写作状态机和三个写作角色：

```text
paper_architect
section_writer
manuscript_editor
```

流程：

```text
G5 审计通过
   ↓
freeze 科学产物
   ↓
paper.py init
   ↓
paper_architect：论文主线 / claim map / 符号 / 图表计划
   ↓
section_writer：按 section packet 分章写作
   ↓
manuscript_editor：统一叙事、术语、符号，最后写摘要
   ↓
paper.py assemble
   ↓
paper_auditor + red_team + compliance_auditor
   ↓
人工复核 audit 状态
   ↓
paper.py finalize
   ↓
paper/FINAL_MANUSCRIPT.md
```

先冻结：

```powershell
python .\tools\harness.py freeze --by TEAM
python .\tools\harness.py verify-freeze
python .\tools\paper.py init
```

让 `paper_architect` 填好 `PAPER_BRIEF.md`、`PAPER_PLAN.json`、`PAPER_CLAIM_MAP.csv`、`SYMBOL_TABLE.csv` 和图表计划。核心 claim 可以用命令映射：

```powershell
python .\tools\paper.py map-claim --claim C001 --section model_and_solution
```

然后：

```powershell
python .\tools\paper.py packet --all
```

把各 packet 分配给独立 `section_writer`。每个核心结论在 section 文件里必须保留类似：

```text
<!-- CLAIM:C001 -->
```

的内部追踪标记。它不会进入最终交付稿。

组装与预审：

```powershell
python .\tools\paper.py assemble
python .\tools\paper.py lint --strict --pre-audit
```

独立审稿、Red Team、合规检查完成并由队员复核后：

```powershell
python .\tools\paper.py audit-set --status passed --by TEAM --note "逐项复核完成"
python .\tools\paper.py lint --strict --pre-final
python .\tools\paper.py finalize
python .\tools\harness.py validate --gate G6 --strict
```

`finalize` 会重新组装、检查冻结状态/核心 claim/evidence/trace tag，并生成：

```text
paper/FINAL_MANUSCRIPT.md
paper/PAPER_BUILD.json
paper/PAPER_LINT.json
```

详细协议见 `docs/PAPER_WRITING.md`。

---

# Direct API：OpenAI / Anthropic / DeepSeek

## 12. 受限单角色调用

如果只需要一个独立数学审稿 Agent，不需要它在工作区执行命令，可以使用：

```powershell
python .\tools\provider_call.py `
  --provider deepseek `
  --role math-auditor `
  --task "检查当前推导的量纲、边界条件和隐含假设" `
  --include-run problem\PROBLEM_GRAPH.json `
  --include-run synthesis\FINAL_MODEL_SPEC.md `
  --reserve-usd 1 `
  --ack-separate-billing `
  --dry-run
```

`--dry-run` 只生成：

```text
runs/<RUN>/modes/direct-api/<ID>/PACKET.md
runs/<RUN>/modes/direct-api/<ID>/REQUEST.json
```

确认包里没有不该发送的文件后，再删除 `--dry-run`。

支持：

```text
--provider openai
--provider anthropic
--provider deepseek
```

对应环境变量：

```text
OPENAI_API_KEY
ANTHROPIC_API_KEY
DEEPSEEK_API_KEY
```

Direct API Runner 只收集明确指定的文本文件；`.env`、常见 secret 文件、二进制附件不会自动塞进请求。

---

## 13. API 预算

初始化时可分开设限：

```powershell
--openai-api-budget-usd 10
--anthropic-api-budget-usd 10
--deepseek-api-budget-usd 10
```

查看：

```powershell
python .\tools\harness.py budget-status
```

手工补记实际费用：

```powershell
python .\tools\harness.py budget-log --kind deepseek-api-usd --amount 0.82 --by TEAM
```

直接 API 与订阅套餐是不同资源；Harness 不会假设订阅额度等于 API 余额。

---

# Verifier / 独立审查

## 14. LLM-as-a-Verifier 仍是独立模式

v4 没有把 Verifier 变成默认投票器。

### Faithful

`reference-verifier-faithful` 只在有兼容 token-logprob 后端时使用，官方上游仍锁定在项目的 `upstream/LLM_AS_A_VERIFIER.lock.json`。

### Lite

`reference-verifier-lite` 是离散 pairwise 审查近似：

```text
客观预检 → A/B 双向审查 → Pivot 聚合
```

始终标记：

```text
faithful: false
```

它不能被解释为“正确率”。

---

# 常用命令

```powershell
python .\tools\harness.py active
python .\tools\harness.py status
python .\tools\harness.py budget-status
python .\tools\harness.py next
python .\tools\runtime.py list
python .\tools\runtime.py check
```

完整命令见 `tools/README.md`。

---

# 目录结构

```text
RUNTIME_RULES.md                  运行时无关的项目规则
AGENTS.md                         Codex 入口规则
CLAUDE.md                         Claude Code 入口规则
config/agent_roles.json           17 个 canonical 逻辑角色
config/runtime_registry.json      Runtime / Provider 注册表
skills/                           canonical Skills（含 cumcm-paper-writing）
.codex/agents/                    自动生成的 Codex Agents
.agents/skills/                   自动同步的 Codex Skills
.claude/agents/                   自动生成的 Claude Code Subagents
.claude/skills/                   自动同步的 Claude Code Skills

inputs/                           原始赛题与附件
runs/<RUN_ID>/                    每次比赛/演练的独立状态
schemas/                          Problem/Idea/Module/Handoff schema
prompts/                          阶段提示

tools/harness.py                  Gate、预算、证据、冻结、打包
tools/paper.py                    论文架构状态、section packet、组装、lint、finalize
tools/runtime.py                  Runtime 渲染、检查、doctor
tools/provider_call.py            OpenAI/Anthropic/DeepSeek 受限角色调用
tools/diversity.py                本地多样性与缺口分析
tools/run_experiments.py          可复现实验执行器
tools/verifier_*.py               Faithful/Lite Verifier
tools/api_burst_openai.py         OpenAI Responses Multi-agent 可选扩容
```

---

# v3/v4.0 兼容性

v4.1 保留了原有：

- `tools/harness.py` Gate / Evidence / Freeze 接口；
- `.codex/config.toml`；
- `--max-codex-threads`（作为别名）；
- `--api-budget-usd`（作为 OpenAI API 预算别名）；
- 原 Faithful/Lite Verifier；
- Fresh-Agent Relay；
- OpenAI Multi-agent Burst。

迁移说明见 `docs/MIGRATION_V3_TO_V4.md`；从 v4.0 升级论文阶段见 `docs/MIGRATION_V4_TO_V4_1.md`。

---

# 重要限制

- 不同模型的共识不是证据。
- Subagent 的“独立上下文”也不等于真正独立的训练分布；跨模型能增加异质性，但仍需实验验证。
- `direct-api` 不是通用 coding-agent 沙箱，不会自动执行代码。
- Claude Code / Codex / DeepSeek 的产品能力与接口会变化；赛前应运行一次实际环境演练。
- Harness 不保证奖项；它提供的是可审计的研究流程和减少失误的工程约束。

详见 `docs/LIMITATIONS.md`。

---

## 官方接口参考（截至 2026-09-01）

- Claude Code subagents: https://code.claude.com/docs/en/sub-agents
- Claude Code skills: https://code.claude.com/docs/en/skills
- DeepSeek + Claude Code: https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/
- DeepSeek API: https://api-docs.deepseek.com/
- DeepSeek tool calls: https://api-docs.deepseek.com/guides/tool_calls/
- OpenAI model/API docs: https://platform.openai.com/docs/
- Anthropic API docs: https://docs.anthropic.com/
