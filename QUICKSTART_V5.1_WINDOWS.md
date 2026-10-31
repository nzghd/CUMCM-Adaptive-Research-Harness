# Windows 快速上手 — CUMCM Adaptive Research Harness v5.1

这份说明以 PowerShell 为例。命令默认针对当前 active run；需要操作指定
run 时，在命令后增加 `--run <RUN_ID>`。

## 1. 解压和自检

把 ZIP **直接解压到 Codex 项目根目录**，确保根目录能看到：

```text
AGENTS.md
.codex/
.agents/
tools/
inputs/
```

运行：

```powershell
python .\tools\runtime.py check
python .\tools\schema_validate.py --static
python .\tools\self_test_human_identity_contract.py
```

这三个快速检查不联网、不调用模型，也不会产生 API 费用。

修改 Gate、stage、schema 或准备发布时，再运行两套完整回归：

```powershell
python .\tools\self_test_v51.py
python .\tools\self_test.py
```

这两套覆盖面较大、执行时间更长，不是每次启动都必须运行。

## 2. 初始化一次演练

把题面放入 `inputs/problem/`，数据和附件放入 `inputs/data/`，然后执行：

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

`harness.py init` 已同时建立完整的 `V51_STATUS.json` 和 v5.1 工作区；
新 run 不需要再运行 `v51_orchestrator.py init`。

## 3. 先做题意解释分叉

先由队员和 Agent 完成：

```text
runs/<RUN_ID>/interpretation/PROBLEM_SEMANTICS.md
```

登记一个可能改变目标函数或可行域的结构性分叉：

```powershell
python .\tools\interpretation_engine.py fork-add `
  --id IF-001 `
  --dimension target_geometry `
  --question "Q1-Q3：遮蔽对象按质心点还是完整几何体判定？" `
  --option "P=质心点" `
  --option "S=表面采样点集" `
  --option "V=完整目标集合" `
  --impact critical `
  --uncertainty high `
  --irreversibility high `
  --changes-objective `
  --changes-feasible-set `
  --expected-result-change-pct 20 `
  --structural
```

登记各候选方案的假设指纹。注意参数名是 `--set key=value`：

```powershell
python .\tools\interpretation_engine.py fingerprint-set `
  --candidate IDEA_001 `
  --set target_geometry=point_centroid `
  --set aggregation_rule=interval_union `
  --set model_family=geometric_optimization `
  --set algorithm=genetic_algorithm `
  --source research/ideas/IDEA_001/IDEA_CARD.json

python .\tools\interpretation_engine.py fingerprint-set `
  --candidate IDEA_002 `
  --set target_geometry=point_centroid `
  --set aggregation_rule=interval_union `
  --set model_family=geometric_optimization `
  --set algorithm=mixed_integer_programming `
  --source research/ideas/IDEA_002/IDEA_CARD.json

python .\tools\interpretation_engine.py analyze
```

报告会分别给出：

- 解释层多样性；
- 模型结构层多样性；
- 算法层多样性。

因此“GA 和 MILP 不同”不会再被误报成“目标定义已经充分分叉”。

## 4. 为结构分叉运行廉价探针

结构分叉默认要求至少两个 option 有 verified probe。先真正运行两个分支，
把结果保存到 run 内，再登记：

```powershell
python .\tools\interpretation_engine.py probe-add `
  --id PROBE-P `
  --fork-id IF-001 `
  --option-id P `
  --probe-type branch-recompute `
  --method "相同输入和精度下重新建模并优化质心点分支" `
  --artifact results/target_point_probe.json `
  --metric "目标值、可行率、排序" `
  --result "见结果文件" `
  --status verified `
  --by <队员姓名>

python .\tools\interpretation_engine.py probe-add `
  --id PROBE-V `
  --fork-id IF-001 `
  --option-id V `
  --probe-type branch-recompute `
  --method "相同输入和精度下重新建模并优化完整目标分支" `
  --artifact results/target_full_probe.json `
  --metric "目标值、可行率、排序" `
  --result "见结果文件" `
  --status verified `
  --by <队员姓名>
```

允许的结构探针类型由 `config/interpretation_policy.json` 控制，默认包括：

```text
structural
branch-recompute
cheap-prototype
small-case-exact
geometry-replay
```

## 5. 单独登记结构敏感性

结构敏感性不能被速度 ±10%、高度 ±5 m 之类的参数扰动替代：

```powershell
python .\tools\sensitivity_engine.py add `
  --kind structural `
  --id SS-001 `
  --target "IF-001 / target_geometry" `
  --alternatives "point_centroid | full_target_set" `
  --metric "目标值、可行率和方案排序" `
  --experiment EXP-SS001 `
  --evidence results/target_full_probe.json `
  --effect "完整目标分支改变主要结论" `
  --effect-pct 49 `
  --status verified `
  --by <队员姓名> `
  --note "两个结构分支均在相同输入下独立重建并重新优化"
```

由队员显式选择或明确接受风险：

```powershell
python .\tools\interpretation_engine.py decide `
  --fork-id IF-001 `
  --select V `
  --by <队员姓名> `
  --human-confirm `
  --evidence results/target_point_probe.json `
  --evidence results/target_full_probe.json `
  --reason "完整目标集合更符合题面操作语义，并且结构重算显示会实质改变结果。"
```

`AUTO`、`DEFAULT`、`AGENT`、`CODEX`、`GPT` 等身份不能关闭 nondelegable
分叉。

检查：

```powershell
python .\tools\interpretation_engine.py lint --strict
python .\tools\sensitivity_engine.py lint --strict
```

## 6. 构造正式数学模块

```powershell
python .\tools\math_engine.py init-module `
  --module MOD_001 `
  --title "主模型" `
  --question-ids Q1 Q2 `
  --method-family geometric-spatial
```

随后填写：

```text
research/modules/MOD_001/
├─ MODEL_INTENT.md
├─ ASSUMPTIONS.md
├─ FORMULATION.md
├─ DERIVATION.md
├─ PARAMETER_IDENTIFICATION.md
├─ THEORY_CHECKS.md
├─ NUMERICAL_METHOD.md
├─ VALIDATION_PROTOCOL.md
├─ SYMBOL_TABLE.csv
├─ EQUATION_REGISTRY.csv
├─ PARAMETER_REGISTER.csv
├─ MODEL_TO_CODE_MAP.csv
└─ MATHEMATICAL_OBLIGATIONS.csv
```

常用登记命令可查看：

```powershell
python .\tools\math_engine.py symbol-add --help
python .\tools\math_engine.py equation-add --help
python .\tools\math_engine.py parameter-add --help
python .\tools\math_engine.py map-code --help
python .\tools\math_engine.py obligation-add --help
```

严格检查：

```powershell
python .\tools\math_engine.py lint --module MOD_001 --strict
```

尚未关闭的 high/critical 数学义务会阻止通过。

## 7. 审查“上界、最优、证明、收敛”等强措辞

```powershell
python .\tools\claim_guard.py scan --scope math
python .\tools\claim_guard.py list --scope math
python .\tools\claim_guard.py lint --scope math --strict
```

这一步是 G5 前的必需科学语料审计。`scan --scope math` 会生成绑定当前扫描
语料、规则和文件哈希的独立
`STRONG_CLAIMS_MATH.csv` / `STRONG_CLAIM_SCAN_MATH.json`。只有表头且无
current receipt 不能通过严格检查；正文或规则变化后必须重新扫描。
`math` 默认语料只包含 `research/modules/` 与 `synthesis/` 中的叙述文本；
后续 `all` 语料在此基础上加入 `paper/sections/`。两者都不会把结果表、
证据表或审计登记表自身当作待审正文。

给某条主张登记证据来源：

```powershell
python .\tools\claim_guard.py provenance-set `
  --scope math `
  --claim-id <CLAIM_ID> `
  --evidence-type numerical_search `
  --evidence-ref results/search.json `
  --status downgraded `
  --recommended-wording "当前搜索区域内观察到的最佳参考值为……" `
  --by <队员姓名>
```

`downgraded` 只是记录处理决定，不会替正文改写。接着必须把原文中的强措辞
改成 `recommended-wording` 所表达的受限结论，并重新运行 `scan`；只改登记表、
但让“理论上界/全局最优”等原句继续留在 canonical corpus 中，严格检查仍会失败。

`numerical_search`、`finite_sampling`、`single_run` 和 `llm_judgment` 不能
单独证明严格上界或全局最优。

## 8. 做新上下文盲审

```powershell
python .\tools\blind_review.py export --review-id BR-001 --overwrite
```

把生成的 `reviews/blind/BR-001/BR-001_packet.zip` 交给一个**新对话、
新 Codex Session、不同模型或独立人工审阅者**。导出目录还会
生成 `RESPONSE_CONTRACT.json` 和预填充的 `RESPONSE_TEMPLATE.md`。审阅者
必须从该模板填写响应，保留其精确的 `Packet-SHA256`、`Run-ID` 和
`Review-ID` 行；不能拿另一 run、另一 review 或旧 packet 的响应复用。
将填好的响应保存为 `blind_response.md`，再导入：

```powershell
python .\tools\blind_review.py import `
  --review-id BR-001 `
  --response .\blind_response.md `
  --reviewer-kind external-pro `
  --session-id NEW-PRO-SESSION-001 `
  --fresh-context `
  --attested-by <队员姓名> `
  --note "未向审阅者提供候选排名、旧审稿意见或选择历史"

python .\tools\blind_review.py validate --review-id BR-001 --strict
```

工具只能核验文件和声明；它无法从技术上证明审阅者真的没看过旧材料。
`--attested-by` 必须是对该独立性声明负责的具名队员。

## 9. 分维度记录一致性

```powershell
python .\tools\consistency_matrix.py set `
  --check-id CK-001 `
  --dimension duration_replay `
  --scope "result2/result3" `
  --expected "所有时长可由代码复现" `
  --observed "全部时长复现" `
  --status PASS `
  --critical `
  --evidence-ref evidence/forward_replay.json `
  --reviewed-by <队员姓名>

python .\tools\consistency_matrix.py set `
  --check-id CK-002 `
  --dimension vertical_roundtrip `
  --scope "导出坐标" `
  --expected "在输出精度内复现" `
  --observed "最大误差 5.4e-4 m" `
  --status PASS_WITH_TOLERANCE `
  --tolerance "1e-3 m" `
  --critical `
  --evidence-ref evidence/forward_replay.json `
  --reviewed-by <队员姓名>

python .\tools\consistency_matrix.py derive --strict
```

必检维度不能写成 `NOT_APPLICABLE`。只有配置中显式允许豁免的可选维度才可
使用该状态，并且命令必须同时提供具体 `--waiver-reason` 与非占位的
`--reviewed-by`；工具会记录带时区的复核时间。状态文件、ledger 或 consistency 自身
也不能作为 `evidence-ref`；引用必须解析到当前 run 内真实存在的
证据文件。

## 10. 查看和批准 V5.1 阶段

```powershell
python .\tools\v51_orchestrator.py status

python .\tools\v51_orchestrator.py validate --stage interpretation
python .\tools\v51_orchestrator.py validate --stage structural_validation
python .\tools\v51_orchestrator.py validate --stage mathematical_construction
python .\tools\v51_orchestrator.py validate --stage independent_review --review-id BR-001
python .\tools\v51_orchestrator.py validate --stage consistency
```

验证通过后仍需由队员批准：

```powershell
python .\tools\v51_orchestrator.py approve `
  --stage interpretation `
  --by <队员姓名> `
  --note "题意分叉、结构探针和选择依据已由队员核验"
```

通过这些检查表示记录和防线完整，**不表示数学模型自动正确，也不保证获奖**。

这些 stage 已接入基础 G0–G7；它们不是可选 overlay：

| Gate | 必须当前有效的 v5.1 stage 批准 |
|---|---|
| G0 | `interpretation` |
| G1 | 无额外 stage（仍执行 G1 自身校验） |
| G2 | `structural_validation` |
| G3 | 无额外 stage（仍执行实验/证据校验） |
| G4 | `mathematical_construction` |
| G5 | `independent_review`、`consistency`，另需当前 math-scope 扫描回执 |
| G6 | `consistency`、`strong_claim_audit`、`paper_extension` |
| G7 | `submission` |

空模板只表示工作区已初始化，不表示 stage 已执行。每个 stage 都要
先通过当前严格校验，再以 `--by <队员姓名>` 批准；修改产物、schema
或校验器会使输入哈希变化，必须重新验证和批准。`TEAM`、`AUTO`、
`CODEX`、`GPT`、`UNKNOWN` 等不能代替具名队员；由 CLI 写入的复核/
审批时间会带时区。最终交付前再批准 `submission` 并运行 G7。

基础 Gate 采用强制的两步人工审核：

```powershell
python .\tools\harness.py review --gate G3
# 打开 runs/<RUN_ID>/reviews/gates/G3_REVIEW.md，完成判断后：
python .\tools\harness.py approve --gate G3 --by <队员姓名>
```

`approve` 只读取并校验已有摘要；缺失、过期、被篡改或含失效证据链接时直接
失败，不会自动生成摘要。G0–G7 都必须遵循 `review → 人工阅读 → approve`。

其中 `independent_review` 和 `consistency` 在 G5 前批准并由科学冻结绑定。
完成 G5、冻结并写稳全部论文 section（含生成的摘要）后，再按依赖顺序执行：

```powershell
python .\tools\claim_guard.py scan --scope all
python .\tools\claim_guard.py lint --strict
python .\tools\v51_orchestrator.py approve --stage strong_claim_audit --by <队员姓名>
python .\tools\paper_v5.py lint --strict
python .\tools\v51_orchestrator.py approve --stage paper_extension --by <队员姓名>
```

全文强主张阶段排在 consistency 之后，避免论文写作让 G5 所绑定的一致性审批
失效；all-scope scan 只会从通过 current math receipt 校验的专用 registry
继承具名核验记录，且不会重写 G5 的 math 工件。任何后续 section 改动仍会使
full-corpus scan 和 paper extension 失效。

## 11. G7 合规与 typed PDF

默认 `paper/sections/085_ai_usage_declaration.md` 位于参考文献前，所有队伍都要
填写。未使用 AI 时逐字写：

```text
本参赛队在竞赛过程中未使用任何AI工具。
```

使用 AI 时采用官方“本参赛队在竞赛过程中使用了AI工具，主要用于……，详细
使用情况见支撑材料。”格式，且用途和使用状态必须与 `AI_USAGE_LOG.jsonl`
一致。规定原文见[官方 2026 AI 使用规定](https://www.mcm.edu.cn/html_cn/node/fef94648f2836ab6cc81586f4c38512b.html)。

```powershell
python .\tools\harness.py export-ai --by <队员姓名>
```

存在需披露 AI 事件时，人工核对导出的 Markdown，将其按官方要求转换并保存为唯一 canonical 路径
`deliverables/AI工具使用详情.pdf`，打开 PDF 完成视觉与内容对应核对后再写
hash receipt：

```powershell
python .\tools\harness.py attest-ai-disclosure-pdf --by <队员姓名>
```

没有需披露 AI 事件时不要求生成该 PDF 或 receipt。适用时，该 receipt 记录具名
人工核对和当前字节哈希。Gate 会另外拒绝缺少
PDF header/xref/EOF、无法解析或零页的伪 PDF；这仍不等于自动验证视觉版式
或当年官方模板，所以人工打开检查不能省略。
`FINAL_CHECKLIST.md` 必须保留模板定义的全部 `[item-id]`、逐项勾选并填写具名
Reviewer。若没有使用任何外部来源且 SOURCE_LEDGER 为空，还需运行：

```powershell
python .\tools\harness.py source-attest-none `
  --by <队员姓名> `
  --reason "本 run 未使用外部网页、论文或数据来源"
```

选择需要正式提交的 run 内支撑文件，构建匿名官方 ZIP：

```powershell
python .\tools\harness.py build-support --run <RUN_ID> `
  --include code/model.py `
  --include results/final_metrics.csv `
  --by <队员姓名>
```

输出为 `deliverables/official/支撑材料.zip` 和不可提交的内部 receipt
`deliverables/internal/SUPPORT_PACKAGE_RECEIPT.json`。有需披露 AI
事件时，工具强制加入已核验的 `AI工具使用详情.pdf`。所有成员的文件名、可检索
正文与元数据都必须匿名；机器无法读出的图片身份信息仍要人工逐项检查。

完成后批准 submission，再按“生成摘要—打开阅读—批准”完成 G7：

```powershell
python .\tools\v51_orchestrator.py approve --stage submission --by <队员姓名>
python .\tools\harness.py review --gate G7
python .\tools\harness.py approve --gate G7 --by <队员姓名>
```

按[官方论文格式规范](https://www.mcm.edu.cn/html_cn/node/4cd596519c9eb9fbd866398f6df0caa3.html)，
论文 PDF 与支撑 ZIP 分开提交，二者均不超过 `20,000,000` bytes。现有
`package --by <队员姓名>` 是含具名审批与审计回执的**内部归档命令**，绝不能
把其输出当作官方匿名支撑材料上传。

## 12. 旧 run 契约迁移

先只看迁移计划：

```powershell
python .\tools\migrate_v51.py --run .\runs\<RUN_ID>
```

若旧解释分叉缺少不能安全推断的结构语义，准备一个按 `fork_id` 索引的 JSON
决定文件，再显式应用：

```powershell
python .\tools\migrate_v51.py `
  --run .\runs\<RUN_ID> `
  --fork-decisions .\fork_migration_decisions.json `
  --apply
```

工具会先在该 run 的 `migrations/` 下保存原文件和迁移清单。未知表头、未知
字段或有损映射会终止整个迁移，不会静默丢列。
