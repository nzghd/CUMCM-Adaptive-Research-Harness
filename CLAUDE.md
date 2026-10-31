# Claude Code entry rules — CUMCM Adaptive Research Harness v5.1

本文件是 Claude Code 的项目入口。以下规则与其他运行时共享；逻辑 Agent 定义来自 `config/agent_roles.json`，不要手工维护 `.claude/agents/*.md`。

在 Claude Code 中，canonical 逻辑角色名中的下划线会映射成项目 subagent 文件名中的连字符，例如 `research_specialist` → `research-specialist`。

# CUMCM Adaptive Research Harness v5.1 — Runtime-Neutral Project Rules

## Mission

本仓库用于数学建模竞赛的问题拆解、动态研究编排、代码实验、证据管理、证据驱动论文写作和论文审计。运行时可以是 Codex、Claude Code、Claude Code + DeepSeek，或受限的直接 API 角色调用。核心研究协议不依赖某一家模型。

目标不是让多个 Agent 尽快达成共识，而是扩大合理假设空间、减少重复搜索，并尽早暴露错误。

## Non-negotiable rules

1. 不把往年题演练描述为训练或更新底层模型权重。
2. 不因候选数量是奇数而多数投票；多数意见不是数学证据。
3. 不把固定 `N→…→1` 漏斗当成默认流程。Agent 数量由问题结构、思路重复度、争议、时间和人工审核能力决定。
4. 第一轮独立探索时，`research_specialist` 只读取公共题面、数据、已批准的问题文件和自己的 brief，不读取其他 idea 目录。
5. 能用代码、求解器、统计检验、边界分析或原始来源验证的内容，不只依赖 LLM 主观评分。
6. 所有核心论文主张都进入 `evidence/CLAIM_EVIDENCE_LEDGER.csv`，并记录人工核验。
7. 不伪造实验、引用、数据来源、运行日志、模型版本或工具调用。无法验证就标记“未验证”。
8. `reference-verifier-lite` 必须明确标注不是官方 logprob 方法，也不得把分数解释为正确率。
9. 外部订阅/网页对话只能由队员人工开启和导入；不得用 UI 自动化把订阅套餐当 API。
10. 任何 API 都是单独计费的可选资源。调用前必须通过对应预算开关、显式确认，并写入 AI 使用日志。
11. 不要让所有 Agent 并发修改同一文件；共享文件由主控或指定 Owner 合并。
12. 最终模型、代码和论文必须处于队员能够解释、复算和维护的复杂度范围内。
13. 不把“不同模型给出相同答案”当成正确性证明。跨模型异质性用于扩大搜索空间与交叉审查，不替代实验和数学证据。
14. 运行时切换不得改变 Gate、Evidence Ledger、Problem Graph、实验记录和冻结规则；这些属于 Core。

## Current-contest source policy

竞赛期间不得搜索、浏览、发布或讨论当届赛题的现成解答、题号讨论或独特题面句子。可以查一般理论、官方标准、公开原始数据、算法文档和原始论文，但必须写入 `sources/SOURCE_LEDGER.csv` 并规范引用。

## Working protocol

- 首先建立 `problem/PROBLEM_GRAPH.json` 与 `problem/VALIDATION_MAP.md`。
- 先建立可运行 baseline，再用 Idea Card 扩展假设空间。
- 运行 `python tools/diversity.py analyze` 后再决定是否追加 Explorer。
- 深化的是有价值的模块，不一定是一整套方案。
- 对每个核心结论先问“怎样证伪”，再设计实验。
- 出现争议时创建专项任务：数学审计、统计显著性、参数可辨识性、复杂度收益、鲁棒性、代码或来源核验。
- G5 后先冻结科学产物；G6 使用 `cumcm-paper-writing` 执行 paper_architect → section_writer → manuscript_editor → assemble，再运行独立 Red Team、Paper Audit 和 Compliance Audit。
- `paper/PAPER_DRAFT.md` 是由 `paper/sections/*.md` 组装出的生成物，不得直接手改；最终使用 `python tools/paper.py finalize` 生成 `FINAL_MANUSCRIPT.md`。
- 修改已冻结的科学产物前，必须执行 `unfreeze` 并记录原因。

## Runtime discipline

- 逻辑角色的单一事实源是 `config/agent_roles.json`。
- Codex Agent 文件由 `python tools/runtime.py render --runtime codex` 生成。
- Claude Code Subagent 文件由 `python tools/runtime.py render --runtime claude-code` 生成。
- Canonical Skills 位于 `skills/`，由同一工具同步到 `.agents/skills/` 和 `.claude/skills/`。
- 运行 `python tools/runtime.py check` 可检查两个运行时是否与 canonical 定义一致。
- `direct-api` 只适用于显式文件包的受限研究/审查调用；它不是完整的本地 coding-agent 沙箱。

## File discipline

- `inputs/` 只读。
- 每个 Agent 只写分配给它的目录或明确的共享文件。
- 自动实验必须记录命令、工作目录、随机种子、参数、运行时间、stdout/stderr 和产物哈希。
- 密钥只放环境变量，不写入仓库、日志、Prompt 包或截图。

## v5.1 additions

Use the canonical skills `cumcm-interpretation-audit`,
`cumcm-mathematical-modeling`, `cumcm-blind-review` and
`cumcm-claim-guard`. High-impact interpretation forks are human decisions.
Do not turn empirical envelopes, heuristic best values or finite samples into
rigorous bounds, proofs or global optimality claims.
