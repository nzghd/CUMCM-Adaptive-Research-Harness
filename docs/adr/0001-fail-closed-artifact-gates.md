# ADR-0001：Gate 必须验证当前产物，而不是文件存在性

- 状态：已采纳
- 日期：2026-09-02
- 适用版本：5.1

## 背景

旧流程同时存在两套事实来源：基础 G0–G7 Gate 和 v5.1 stage overlay。模板又会预置空表，因此“文件存在”不能说明相应审计已经执行。另有一些审批只保存状态、不绑定输入；输入或校验器改变后，旧的 `approved` 仍可能被误用。

## 决策

1. `harness_config.json` 显式声明每个 Gate 依赖的 v5.1 stage；基础 Gate 会直接重跑这些 stage 的严格校验。
2. Gate 与 stage 的校验结果都记录规范化输入清单及 SHA-256。审批只对该输入哈希有效；上游重新审批或输入漂移会撤销下游审批。
3. JSON/CSV 契约由 `config/artifact_schema_registry.json` 统一登记。`tools/schema_validate.py` 同时检查 schema、空 CSV 的表头，以及生产者字段常量，避免“镜像 schema 无人读取”。
4. 强主张 registry 不再由 run 模板预置；强主张审计必须持有扫描命令为当前 canonical corpus 同步生成的 registry 与收据，空 registry 也不例外。
5. 证据引用必须解析为当前 run 内真实存在的文件；外部来源进入 `SOURCE_LEDGER.csv`，不得冒充可冻结的本地产物。
6. `FROZEN_MANIFEST.json` 只冻结科学产物，不把可变论文和审计文件混入文件范围。G5 的批准输入另外绑定盲审、一致性和独立的 math-scope 强主张扫描；正文稳定后使用另一份 all-scope 扫描收据。两份收据互不覆盖，从而既保证 G5 前科学审查，又避免论文写作使科学冻结自失效。
7. G3 必须至少有一个核心主张、一个由 runner 产生且状态为 `passed` 的 `run.json` 收据，以及至少一条绑定该收据的有限数值指标。
8. 论文 assembly/build 绑定 `PAPER_PLAN.json` 的 section id、顺序、路径和内容哈希；摘要写入计划中 `abstract` section 的唯一文件。
9. `--force` 只能跳过 Gate 前置顺序，不能把当前严格校验失败的 Gate 写成 `approved`。

## 兼容性

旧 run 不会被静默改写。`tools/migrate_v51.py` 默认仅生成迁移预览；遇到旧 CSV 里无法无损推断的语义字段时，必须提供显式决策文件。迁移会保留备份和清单。

旧审批、旧扫描结果和旧实验日志在缺少当前输入哈希或 runner 收据时会失败关闭，需要重新执行相应校验并审批。这是有意的行为变化。

## 后果

- 空模板仍可用于初始化，但不能再充当审计证据。
- 修改输入、证据、schema、stage registry 或相关校验器后，需要重新审批。
- 校验成本略增，但“未运行”和“运行后干净”现在可区分，且最终交付可追溯到具体输入与控制代码。
