# CUMCM Adaptive Research Harness v5.1 审计收口报告

> 快照日期：2026-09-04（Asia/Shanghai）  
> 审计状态：**本轮有限收口完成；当前源码快照的离线 release checkpoint 通过**  
> 结论边界：仅覆盖当前源码与合成离线回归，不代表真实模型、实网 provider、论文数学正确性或竞赛结果。

## 1. 结论

用户最初指出的 P0/P1/P2 问题均已关闭。继续审计后固定的四个收口任务——破坏性路径、外部调用事务、G1/G2/G3 防重放、release checkpoint——也已完成。随后按赛时最小可用性范围，仅补充统一 Gate 人工摘要与 G7 提交检查，没有拆分 `harness.py` 或引入新提交系统。

最终验证结果：15 项 focused checks、base compatibility regression、完整 v5.1 生命周期回归、Python/JSON 静态解析均通过；完整回归覆盖摘要生成到 assemble/finalize、科学冻结、G0–G7、v5.1 审批、新鲜度、真正的两步人工审批、G7 提交检查、AI 声明、官方支撑 ZIP、内部归档分流和 ZIP 成员精确比对。全部测试均为离线合成测试，没有网络、模型或 API 调用。

## 2. 原始问题状态

| 原问题 | 状态 | 收口结果 |
|---|---:|---|
| P0-1 摘要写入错误文件名 | 已关闭 | 摘要路径由 `PAPER_PLAN.json` 唯一解析，生成 `paper/sections/010_abstract.md`；回归贯通 build-abstract → assemble → finalize。 |
| P0-2 空 `STRONG_CLAIMS.csv` 冒充已扫描 | 已关闭 | strong-claim stage 必须读取当前 run、当前语料、当前规则和 registry 绑定的 canonical scan receipt；未运行、partial、stale、tampered 均失败。 |
| P1 CSV 模板旧 schema | 已关闭 | `INTERPRETATION_FORKS.csv`、`STRUCTURAL_SENSITIVITY.csv` 的模板、producer、schema 与 registry 已对齐。 |
| P1 v5.1 stage 未接入 G0–G7 | 已关闭 | base gate 会执行对应 v5.1 validator，并核验审批状态、输入哈希、存储清单与 freshness。 |
| P1 `evidence_ref` 只查非空 | 已关闭 | 引用必须解析到当前 run 内 canonical 实体文件或唯一、已核验的 ledger 记录。 |
| P2 claim guard 范围与误报 | 已关闭 | math/all scope 分离；默认语料不再包含 reviews/evidence/results；普通“显著优于/由此证明/唯一性”推导语境已收窄处理。 |
| P2 镜像配置无人读取 | 已关闭 | claim provenance、consistency dimensions、v5.1 stage registry 和 schemas 均被 runtime/static validation 消费；陈旧 mode registry 已移除。 |
| P2 `AGENTS.md` 仍写 v4 | 已关闭 | 项目入口已改为 v5.1 规则。 |

## 3. 四个有限收口任务

### 3.1 破坏性路径边界

已关闭 `blind_review.py`、`verifier_lite.py` 和 `math_engine.py` 的路径/标识符问题：

- destructive/replace 目标必须位于真实 run 的受控直接子 namespace；
- 拒绝 `.`、`..`、绝对路径、跨 run 目标、非 canonical 拼写；
- 拒绝 symlink/junction 重定向；
- 在递归删除前重新解析并二次确认 containment；
- Module ID 必须与其目录身份一致。

回归：`tools/self_test_path_safety.py`。

### 3.2 外部调用事务与预算

外部调用现在使用 phase-aware、fail-closed 的事务协议：

- competition/未知 phase 禁止未授权外部调用；
- 付费路径要求显式开关与确认；
- 调用前持锁预留预算，失败也保留 terminal/error 和 AI disclosure 记录；
- attempt、AI event、budget reservation、terminal state 由 G7 双向对账，拒绝 orphan、重复或字段不一致；
- G0 只绑定稳定预算策略投影，正常 `used_*`/计数变化不使 G0 失效；G7 绑定最终完整预算和调用记录。

回归：`tools/self_test_egress_policy.py`。

### 3.3 G1/G2/G3 防重放合同

- **G1**：新增 `DIVERSITY_SCAN.json`，绑定输入/输出哈希、算法版本、参数与语义预算容量；能区分 not-run、stale、tampered、clean。
- **G2**：Idea/Module payload ID 必须等于父目录名且全局唯一；磁盘卡片、两份 registry 和 `FOCUS_SET.selected_*` 精确对账；未知、缺失、重复、错路径均失败。
- **G3**：实验 receipt 强制重演 executed argv、resolved executable、run-confined cwd、最小环境策略、继承/覆盖键、runner 环境键和 isolation truth fields；同步篡改 request/run 也不能绕过。

回归：`tools/self_test_diversity_contract.py`、`tools/self_test_selection_contract.py`、`tools/self_test_experiment_receipt_contract.py`。

### 3.4 发布检查点

- `setup_check.ps1` 已委托 `scripts/check_release.ps1`；
- 统一脚本包含 runtime、schema、focused、base、full 和 manifest 检查；
- 两份根 release manifest 由同一 canonical 文件集合确定性生成；
- 所有报告定稿后重新生成 manifest，再执行最终 integrity check。

最终源码已按统一脚本的同一命令集合分别执行并核对退出码，其中包含一次完整生命周期回归；未再把 wrapper 从头重复运行一遍。

## 4. 赛时人工审核与 G7 提交检查

### 4.1 统一 Gate 人工摘要

- 新增 `python tools/harness.py review --gate G0..G7`，生成
  `reviews/gates/<GATE>_REVIEW.md`；固定呈现待判断事项、自动通过项、关键结果、相对上次变化和原始证据链接。
- 摘要是派生决策面，不进入自身 Gate 输入，避免自引用；审批同时绑定 canonical 路径、摘要 SHA-256 和底层 Gate 输入 SHA-256。
- 摘要正文可由内嵌比较基线确定性重建。正文篡改、过期 metadata、越界/失效链接或摘要校验器变化都会使审批失败或过期。
- `approve` 只读取并验证已有摘要，不再生成或修复摘要；缺失、过期或被篡改时退出且不写 `STATUS.json`/`DECISION_LOG.jsonl`。该顺序同样覆盖“前置 Gate 已漂移”的组合，不会在报告检查前偷偷回退状态。
- 未变化的 G7 重复审批为幂等 no-op，避免重复 receipt 使刚批准的快照自失效。

### 4.2 G7 最终提交检查

- 必须存在 canonical `deliverables/FINAL_MANUSCRIPT.pdf`，并与 paper compile receipt 绑定；大小上限为含边界的 `20,000,000` bytes，配置只能收紧、不能放宽。
- 所有队伍都必须在参考文献前放置 `AI工具使用声明`。未使用时要求官方逐字句；使用时要求官方前后缀、实质用途、日志 used/no-use 一致，并由具名队员确认用途与日志完整性。默认论文 plan 已包含 `085_ai_usage_declaration.md`。
- 有需披露 AI 事件时，`AI工具使用详情.pdf` 必须来自 canonical Markdown 导出，并由具名队员完成可读性/内容对应核验；公开详情不再泄露 run id、事件 id 或复核人姓名。
- 新增 `build-support`：只把显式 allowlist 文件放进 `deliverables/official/支撑材料.zip`，将具名 receipt 留在 `deliverables/internal/`；AI 详情 PDF 按需自动加入。旧 `package` 是含内部审批材料的审计归档，不能当官方支撑材料提交。
- 官方支撑 ZIP 同样使用不可放宽的 `20,000,000` bytes 上限，校验成员、CRC、源文件/成员/整包哈希与 rollback。路径穿越、symlink/junction、hardlink、NTFS ADS、内部日志、常见密钥和具名内容会被拒绝。
- 匿名检查扫描明确身份字段、文件名、可读文本、PDF 可检索正文及 Author/XMP 元数据。构建时身份词表保存在内部 receipt；后续以“构建时 ∪ 当前发现”身份重扫，新增 reviewer 不会无故使包过期，也不会降低检测强度。图片、页眉页脚、OCR 和无法解析的容器仍由强制人工 checklist 兜底。

回归：`tools/self_test_gate_review_unit.py`、`tools/self_test_gate_review.py`、`tools/self_test_submission_compliance.py`、`tools/self_test_support_package.py`，并贯通 `tools/self_test_v51.py`。

## 5. 最终验证记录

| 检查 | 结果 | 说明 |
|---|---:|---|
| 15 项 focused checks | PASS | runtime、schema、path、selection、diversity、experiment env/receipt、egress、consistency、human identity、Gate review unit/integration、G7 submission、official support package、manifest self-test。 |
| `tools/self_test.py` | PASS | base compatibility；无网络/模型/API。 |
| `tools/self_test_v51.py` | PASS | 完整 lifecycle、负例、G0–G7、PDF 与 package；无网络/模型/API。 |
| Python AST | PASS | 47 个非 run `.py`（其中 `tools/` 43 个）。 |
| 非 run JSON | PASS | 87 个 JSON。 |
| merge marker 扫描 | PASS | 未发现冲突标记。 |
| release manifest integrity | PASS | 报告定稿后双 manifest 重建并校验。 |
| `runs/` 清洁度 | PASS | 最终仅保留 `.gitkeep` 与 `README.md`。 |

完整回归第一次运行在一个测试合同断言处失败：测试把“当前 G7 结构合法”和“既有 G7 审批仍新鲜”混为一谈。生产代码本来会在历史决策日志变化后改变 G7 输入哈希，并由 `_approved_gate_freshness` 标记 stale、阻止正式打包。测试已改为分别断言：`validate_gate` 结构成功、approval stale、正式 `package` 退出码为 2；随后完整回归通过。该修正没有放宽生产门禁。

本轮新增官方支撑包后的第一次完整回归还暴露了一个集成缺口：测试修改历史 freeze reviewer 后，动态身份集合增长，使未变的安全 ZIP 因 receipt 身份摘要不同而被误判过期。现改为 receipt 绑定构建时身份词表，审计用构建时与当前身份的并集重新扫描；G7 仍由 decision-history 输入哈希正确变 stale。修复后重新从头执行完整生命周期并通过。

## 6. 仍需诚实保留的边界

- 未调用真实网络、模型、付费 API 或 provider；launcher/provider 边界主要由 mock/离线 fixture 验证。
- 未证明真实外部 reviewer 的独立性；工具只能验证导入包、session 声明、哈希和人工 attestation。
- Gate、schema 与 receipt 通过不自动证明数学推导、数据质量或论文结论正确。
- 实验 receipt 明确记录 `filesystem_isolated=false`、`network_isolated=false`；当前保证的是环境最小化、路径约束和收据完整性，不是 OS 级沙箱。
- 本轮没有在另一台机器上对独立发行 ZIP 做全新解压验收，也不声称 ZIP CRC/fresh extraction 结果。
- 自动匿名扫描无法检查不可检索的图片文字、复杂页眉页脚或全部渲染差异，最终仍需队员目视复核。
- 尚未用一套真实往年题材料让队员计时阅读八份摘要；当前“1–2 分钟可完成人工判断”的目标仍需真实演练验证。
- 不保证竞赛获奖，也不会替队员关闭 nondelegable 题意分支。

## 7. 下一版本 backlog（不阻塞本次收口）

1. 为 append-only 决策/AI 日志增加统一序号、hash chain 与跨进程 journal。
2. 用 Typed Artifact Graph 统一 gate、stage、schema 与 snapshot 声明，减少重复配置。
3. 优化完整回归中大体积 `STATUS.json`/`DECISION_LOG.jsonl` 的重复重验；保留 focused tests 作为日常秒级入口。
4. 在独立机器/全新目录执行发行 ZIP 解压、CRC、manifest 和最小 smoke 验收。
5. 用一套真实往年题完成 G0–G7 摘要可读性与人工审核耗时演练。

本轮未安排、未触发自动关机。
