# CUMCM Adaptive Research Harness v5.1

面向全国大学生数学建模竞赛的运行时无关研究编排系统。v5.1 以真实
v4.1 工程为底座，保留其多运行时、实验、证据、冻结、Verifier、Relay
和完整 `tools/paper.py` 论文管线，新增两层此前缺失的核心能力：

1. **先质疑题意世界观，再构造数学**：解释分叉、结构假设指纹、结构敏感性和不可委托决策；
2. **把数学建模变成结构化产物**：变量、公式、推导、参数来源、理论义务、数值方法和公式—代码—证据映射。

```text
Problem statement
      ↓
Literal semantics / interpretation forks
      ↓
Assumption fingerprints and structural sensitivity
      ↓
Human decision or explicit multi-branch retention
      ↓
Mathematical construction and derivation
      ↓
Math-scope strong-claim provenance guard
      ↓
Numerical implementation, experiments and evidence
      ↓
Blind independent red team
      ↓
Fine-grained consistency
      ↓
Stable manuscript sections + full-corpus strong-claim guard
      ↓
Evidence-first paper pipeline + v5.1 paper extensions
```


## Release documents

- [Windows quickstart](QUICKSTART_V5.1_WINDOWS.md)
- [Migration from v5.0/v4.1](MIGRATION_V5_TO_V5.1.md)
- [v5.1 release test report](TEST_REPORT_V5.1.md)
- [v5.1 build report](BUILD_REPORT_V5.1.md)

## 关键原则

- 不固定 3、5、7 个 Agent；根据问题结构和信息增益动态扩展。
- 多数投票不证明正确；能用代码、求解器、证书和实验验证的，不靠主观评分。
- **算法异质性不等于世界观异质性**。GA/PSO/MILP 可以共享同一错误目标定义。
- 参数敏感性不能代替结构敏感性。
- 数值搜索最大值不是严格上界；可行最好值不是自动的全局最优。
- 不可委托的高影响解释分叉必须由队员显式确认。
- 同一会话自审可以保留，但不能冒充新上下文盲审。
- Harness 提高可审计性和发现错误的概率，不保证模型正确或获奖。

## 新增工具

| 工具 | 作用 |
|---|---|
| `tools/interpretation_engine.py` | 题意语义、解释分叉、假设指纹、非委托决策 |
| `tools/sensitivity_engine.py` | 分开管理参数敏感性与结构敏感性 |
| `tools/math_engine.py` | 数学模块、公式、参数、义务、公式—代码映射 |
| `tools/math_workbench.py` | 可选 SymPy/Pint/CVXPY/Pyomo 检查 |
| `tools/claim_guard.py` | 强数学措辞与证明/证书来源审计 |
| `tools/blind_review.py` | 清洗并导出新上下文盲审包、导入审阅结果 |
| `tools/consistency_matrix.py` | 分维度一致性与容差报告 |
| `tools/paper_v5.py` | 在 v4.1 `paper.py` 上增加摘要事实、公式/引用登记与可选编译 |
| `tools/v51_orchestrator.py` | 执行、审批 v5.1 stage，并与基础 Gate 绑定 |
| `tools/gate_review.py` | 生成面向队员的 Gate 决策摘要并校验其时效与证据链接 |
| `tools/support_package.py` | 构建并核验匿名的官方支撑材料 ZIP；与内部审计归档分离 |
| `tools/schema_validate.py` | 执行已登记的 JSON/CSV schema，并核对 CSV 生产者字段契约 |
| `tools/migrate_v51.py` | 对早期 v5.1 run 做先预览、后备份、再显式应用的契约迁移 |
| `tools/self_test_v51.py` | v5.1 生命周期与交付离线回归 |

## 自检

Python 3.10+：

```powershell
.\scripts\check_release.ps1
```

该入口优先使用 `E:\Anaconda\envs\common_env\python.exe`，执行静态、定向、
v5.1 生命周期、基础兼容和 release manifest 检查。全部检查不联网、不调用
模型、不产生 API 费用；日常只修改局部文档时可按影响范围缩减。

## 快速开始

把题面和附件放入：

```text
inputs/problem/
inputs/data/
```

初始化：

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

`harness.py init` 已会为新 run 创建完整 v5.1 stage 状态，无需再运行
`v51_orchestrator.py init`。早期 run 请按迁移文档处理，不要用二次初始化
代替契约迁移。

每个 Gate 都必须先生成一份面向人的统一摘要，例如：

```powershell
python .\tools\harness.py review --gate G3
```

报告写入 `runs/<RUN_ID>/reviews/gates/G3_REVIEW.md`，集中列出待判断事项、
关键假设、已验证结果、未解决风险、与上次审核相比的变化，以及原始证据链接。
常规通过项只显示数量，黄色/红色项目才展开。队员必须打开并阅读该文件，
然后另行运行 `approve --gate ... --by ...`。`approve` 只校验已有摘要并绑定其
正文哈希与底层输入哈希；摘要缺失、过期、被篡改或含失效链接时会直接失败，
且不会替人自动生成或修复摘要。

然后按 [QUICKSTART_V5.1_WINDOWS.md](QUICKSTART_V5.1_WINDOWS.md) 运行。

## 数学模块结构

每个被保留的模块位于：

```text
research/modules/<MODULE_ID>/
├─ MODEL_INTENT.md
├─ SYMBOL_TABLE.csv
├─ ASSUMPTIONS.md
├─ FORMULATION.md
├─ EQUATION_REGISTRY.csv
├─ DERIVATION.md
├─ PARAMETER_IDENTIFICATION.md
├─ PARAMETER_REGISTER.csv
├─ THEORY_CHECKS.md
├─ NUMERICAL_METHOD.md
├─ VALIDATION_PROTOCOL.md
├─ MODEL_TO_CODE_MAP.csv
├─ MATHEMATICAL_OBLIGATIONS.csv
├─ MODULE_CARD.json
└─ MODULE_SPEC.json
```

严格 lint 检查结构、可追踪性和未关闭义务；它不会自动证明数学正确。

所有已登记的 JSON/CSV 契约都由 `config/artifact_schema_registry.json`
统一路由。空 CSV 也会检查表头，所以模板与生产者字段漂移不会等到有数据时才暴露。

## 论文管线

v4.1 已有的 `tools/paper.py` 是论文主干，继续提供：

```text
init → map-claim → section-set → packet → assemble → audit-set → lint → finalize
```

v5.1 不重建这一管线，只增加：

- `ABSTRACT_FACTS.json`
- `EQUATION_REGISTER.csv`
- `CITATION_REGISTER.csv`
- `SECTION_QUALITY_REGISTER.csv`
- `TECHNICAL_STYLE_GUIDE.md`
- `abstract_writer`、`notation_editor`、`figure_table_editor`、`citation_manager`、`technical_editor`

摘要应从冻结、证据绑定且人工核验的事实生成，而不是凭语言模型自由发挥；
每条事实同时绑定 `claim_ids` 与属于这些主张的 `evidence_ids`。
先运行 `python tools/paper.py init` 建立唯一的 section 计划，再运行 `python tools/paper_v5.py build-abstract`；摘要输出路径由 `PAPER_PLAN.json` 的 `abstract` section 决定。全部 section（含摘要）稳定后，先做 canonical all-scope strong-claim scan 并批准 `strong_claim_audit`，再批准 `paper_extension`。`paper_v5.py lint --strict` 会核对 evidence 外键、claim map、精确 section 归属、方程量纲、图表覆盖和摘要同步状态。
所有纳入正文的 section 还必须在 `SECTION_QUALITY_REGISTER.csv` 中完成
`final` + 具名 editor 的复核。`paper.py audit-set` 会绑定完整审稿输入清单，
审稿后改动 plan、章节、登记表、来源或证据都必须重新审计。
最终 Markdown 可用 `python tools/paper_v5.py compile` 编译到
`deliverables/FINAL_MANUSCRIPT.pdf`；已有输出必须显式传入 `--replace`，
并生成绑定源文件、输出哈希和 pandoc 版本的编译回执。

默认论文计划包含位于参考文献前的 `085_ai_usage_declaration.md`。按
[官方 2026 AI 使用规定](https://www.mcm.edu.cn/html_cn/node/fef94648f2836ab6cc81586f4c38512b.html)，
所有队伍都必须填写“AI工具使用声明”：未使用时逐字写
“本参赛队在竞赛过程中未使用任何AI工具。”；使用时采用官方“本参赛队在竞赛
过程中使用了AI工具，主要用于……，详细使用情况见支撑材料。”格式，并与
`AI_USAGE_LOG.jsonl` 保持一致。

G5 前必须先运行 `claim_guard.py scan --scope math`。该步骤使用独立的
`claims/STRONG_CLAIMS_MATH.csv` 与
`claims/STRONG_CLAIM_SCAN_MATH.json`，所以 G6 前的全文扫描不会覆盖已由
G5/冻结绑定的数学审计。正文稳定后再运行 `scan --scope all`；只有通过当前
math receipt 校验的具名核验记录才会继承到全文 registry。

G7 的 AI 导出分两层绑定：`export-ai --by <队员姓名>` 绑定源日志与 Markdown；
若日志包含需披露事件，还必须把人工视觉核对后的文件保存为
`deliverables/AI工具使用详情.pdf`，再运行
`attest-ai-disclosure-pdf --by <队员姓名>`。该 receipt 证明具名人工核对与
字节哈希；Gate 还会拒绝结构不完整、无法解析或零页的伪 PDF，但不声称
自动验证视觉版式或当年官方模板。

官方交付使用独立命令：

```powershell
python .\tools\harness.py build-support --run <RUN_ID> `
  --include <run内文件> --by <队员姓名>
```

它输出 `deliverables/official/支撑材料.zip`（最多 `20,000,000` bytes）
及仅供内部追踪的 receipt；有需披露 AI 事件时，ZIP 强制包含已核验的
`AI工具使用详情.pdf`。支撑材料中的文件内容、文件名和元数据都必须匿名。
现有 `package --by ...` 生成的是含具名审批材料的**内部审计归档**，绝不能当作
官方匿名支撑材料提交。按
[官方论文格式规范](https://www.mcm.edu.cn/html_cn/node/4cd596519c9eb9fbd866398f6df0caa3.html)，
论文 PDF 与支撑 ZIP 分开提交，二者均不得超过 20 MB。

G7 会检查上述大小、声明与日志的一致性，以及可检索文本和 PDF 元数据中的
身份信息。机器扫描无法证明图片、页眉页脚等渲染区域匿名；队员仍须逐页检查
论文与支撑材料，并完成最终清单中的人工复核项。

基础 G0–G7 会直接重跑 `harness_config.json` 中声明的 v5.1 stage，并要求
对应 stage 已由具名队员批准：G0 → `interpretation`，G2 →
`structural_validation`，G4 → `mathematical_construction`，G5 →
`independent_review` + `consistency`，G6 → `consistency` +
`strong_claim_audit` + `paper_extension`，G7 → `submission`。G1/G3
没有额外 stage，但仍执行自身的 Gate 契约。

Gate 和 stage 的人工批准都绑定当前产物与校验器哈希；任何相关
输入改变后必须重新验证、重新批准。`--by`、`--reviewed-by`、
`--verified-by`、`--editor` 和 `--attested-by` 等人工责任字段必须是具体
队员姓名；`TEAM`、`AUTO`、`CODEX`、`GPT`、`UNKNOWN` 等占位/机器
身份不能关闭审计义务。审批和复核时间必须带 ISO-8601 时区。设计理由见
[`docs/adr/0001-fail-closed-artifact-gates.md`](docs/adr/0001-fail-closed-artifact-gates.md)
与
[`docs/adr/0002-human-review-and-official-delivery.md`](docs/adr/0002-human-review-and-official-delivery.md)。

## 现成 Verifier 模式仍保留

官方 LLM-as-a-Verifier Faithful 模式、Pro-only Lite 模式、Fresh-Agent
Relay、独立 Pro Review 和可选 API Burst 均来自 v4.1，未被移除。Faithful
模式要求能够返回 token-level logprobs 的 API 或本地兼容服务；Pro/Codex
界面本身只能运行 Lite 近似流程。

## 文档入口

- `QUICKSTART_V5.1_WINDOWS.md`
- `docs/V5_1_INTERPRETATION_AND_ASSUMPTIONS.md`
- `docs/V5_MATHEMATICAL_MODELING_ENGINE.md`
- `docs/V5_1_BLIND_REVIEW_AND_CLAIMS.md`
- `docs/V5_1_CONSISTENCY.md`
- `docs/PAPER_WRITING.md`
- `docs/REFERENCE_VERIFIER.md`
- `docs/LIMITATIONS.md`
- `MIGRATION_V5_TO_V5.1.md`

## 明确限制

v5.1 没有声称完成以下事情：

- 自动判断唯一正确的题意；
- 自动证明所有数学推导；
- 自动证明人工确认是认真完成的；
- 自动验证外部 reviewer 从未看过其他材料；
- 自动把多 Agent 变成独立数学家；
- 保证竞赛获奖。

它的目标更具体：让结构性歧义更难被悄悄锁死，让数学义务更难被跳过，
让经验结果更难被写成严格证明，并让最终模型能够从题意、公式、代码、
实验、证据一直追踪到论文。
