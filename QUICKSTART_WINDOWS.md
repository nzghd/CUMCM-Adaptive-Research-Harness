# Windows Quickstart — CUMCM Adaptive Research Harness v4.1

> **历史文档，不是 v5.1 可执行流程。** v5.1 用户请阅读
> [`QUICKSTART_V5.1_WINDOWS.md`](QUICKSTART_V5.1_WINDOWS.md)。下文保留用于
> 理解 v4.1 基线，它缺少 v5.1 必需的 stage 审批、全文强主张扫描和
> 具名人工责任契约，不应直接用于新 run。

## 1. 解压

项目根目录必须直接看到：

```text
README.md
AGENTS.md
CLAUDE.md
RUNTIME_RULES.md
.codex/
.claude/
.agents/
config/
tools/
inputs/
```

Windows 资源管理器可能隐藏以 `.` 开头的目录。

## 2. 自检

在 PowerShell：

```powershell
cd D:\CUMCM_Harness
python --version
python .\tools\self_test.py
python .\tools\runtime.py list
```

## 3. 放题目

```text
inputs/problem/  ← 题面
inputs/data/     ← 附件
```

## 4. 选一种 Runtime

### A. Codex

```powershell
python .\tools\harness.py init `
  --contest CUMCM2025-REHEARSAL `
  --problem A `
  --phase rehearsal `
  --runtime codex `
  --max-agent-threads 4
```

重新打开 Codex 项目，然后使用 `$cumcm-adaptive-research`。

### B. Claude Code + Claude

先确认：

```powershell
claude --version
```

初始化：

```powershell
python .\tools\harness.py init `
  --contest CUMCM2025-REHEARSAL `
  --problem A `
  --phase rehearsal `
  --runtime claude-code `
  --max-agent-threads 4
```

启动：

```powershell
claude
```

Claude Code 会读取 `CLAUDE.md`、`.claude/agents/`、`.claude/skills/`。

### C. Claude Code + DeepSeek

需要 Claude Code CLI 和 DeepSeek API Key。

初始化：

```powershell
python .\tools\harness.py init `
  --contest CUMCM2025-REHEARSAL `
  --problem A `
  --phase rehearsal `
  --runtime claude-code-deepseek `
  --max-agent-threads 4 `
  --deepseek-api-budget-usd 10
```

只在当前 PowerShell 会话设置 Key：

```powershell
$env:DEEPSEEK_API_KEY="你的 key"
```

启动：

```powershell
.\scripts\start_claude_deepseek.ps1 `
  --run YOUR_RUN `
  --reserve-usd 5 `
  --ack-separate-billing
```

先用 `--dry-run` 可检查本地启动预览且不占用预算。`competition` run 不允许
通过该外部后端启动；失败或无法确认是否送达的会话仍保留预留金额，需按
DeepSeek 控制台人工核销。

不要把真实 Key 写进 `.env.example`、README、Prompt 或截图。

## 5. Intake Prompt

给当前主控：

```text
使用 cumcm-adaptive-research。
读取 active run，只执行 Intake。

完成 Problem Brief、Question Map、Problem Graph、Assumption Register、
Validation Map、Data Manifest/Data Audit。
严格区分题面事实、解释、假设和未知项。
不要选最终模型，不要写完整论文。

完成后运行 python tools/harness.py validate --gate G0，然后停止等待人工审核。
```

## 6. 人工批准 G0

```powershell
python .\tools\harness.py validate --gate G0
python .\tools\harness.py approve --gate G0 --by <队员姓名> --note "已核对题意、单位、数据字段和验证要求"
```

## 7. Explore

告诉主控：

```text
进入 Explore。
让 meta_scientist 根据 Problem Graph 和预算创建最小有用的第一批独立研究 brief。
调用 research_specialist 做盲探索；首轮 specialist 不得读取其他 Idea Card。
每个方向只写结构化 Idea Card，不写完整论文。
第一批完成后运行 python tools/diversity.py analyze，
再由 diversity_auditor 判断是否需要补新的数学结构或验证路线。
不要为了凑 Agent 数量继续扩展。
```

## 8. Runtime 检查

角色改动后：

```powershell
python .\tools\runtime.py render --runtime all
python .\tools\runtime.py check
```

本机 CLI 检查：

```powershell
python .\tools\runtime.py doctor --runtime claude-code
```

或：

```powershell
python .\tools\runtime.py doctor --runtime claude-code-deepseek
```

`doctor` 不联网。

## 9. Direct DeepSeek API（可选，不是完整 coding Agent）

先 dry-run：

```powershell
python .\tools\provider_call.py `
  --provider deepseek `
  --role math-auditor `
  --task "审计当前模型推导" `
  --include-run synthesis\FINAL_MODEL_SPEC.md `
  --reserve-usd 1 `
  --ack-separate-billing `
  --dry-run
```

检查生成的 `PACKET.md` 后，确定要付费再删除 `--dry-run`。

## 10. 常用状态命令

```powershell
python .\tools\harness.py active
python .\tools\harness.py status
python .\tools\harness.py budget-status
python .\tools\harness.py next
```

详细说明见 `README.md` 和 `docs/RUNTIMES.md`。


## 11. G6：真正写论文

G5 的 Red Team / code / evidence / robustness 审计完成后：

```powershell
python .\tools\harness.py freeze --by <队员姓名>
python .\tools\harness.py verify-freeze
python .\tools\paper.py init
```

然后给主控：

```text
使用 cumcm-paper-writing。
先调用 paper_architect 完成 PAPER_BRIEF、PAPER_PLAN、PAPER_CLAIM_MAP、
SYMBOL_TABLE、FIGURE_TABLE_REGISTER 和 PAPER_OUTLINE。
核心 claim 必须映射到 section，不得创造新结果或引用。
然后运行 python tools/paper.py packet --all，
把各 section 交给 section_writer；最后由 manuscript_editor 统一全文，摘要最后写。
完成后 assemble + lint --strict --pre-audit，并停止等待独立审计。
```

独立 `paper_auditor`、`red_team`、`compliance_auditor` 审完并人工复核后：

```powershell
python .\tools\paper.py audit-set --status passed --by <队员姓名> --note "论文逐项核对完成"
python .\tools\paper.py lint --strict --pre-final
python .\tools\paper.py finalize
python .\tools\harness.py validate --gate G6 --strict
```

最终正文位于：

```text
runs/<RUN>/paper/FINAL_MANUSCRIPT.md
```

详细见 `docs/PAPER_WRITING.md`。
