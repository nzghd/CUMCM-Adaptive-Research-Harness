# Contributing

欢迎改进 Harness 的实现、文档、回归测试和可解释示例。讨论与提交可使用中文或英文。

## 提交问题

请提供版本、操作系统、Python 版本、最小命令、预期行为和实际行为。优先使用合成数据复现，移除密钥、个人信息与参赛队身份；不要上传整个 `runs/` 或当前比赛的题面与解答。安全漏洞按 [SECURITY.md](SECURITY.md) 私下报告。

## 开发环境

```powershell
git clone https://github.com/nzghd/CUMCM-Adaptive-Research-Harness.git
cd CUMCM-Adaptive-Research-Harness
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-core.txt
```

Python 3.10+；Windows / PowerShell 是当前主要验证环境。数学工作台、模型 API 和 Faithful Verifier 的依赖单独放在相应的 `requirements-*.txt` 中，不是贡献文档或核心离线检查的前提。

## 修改约定

- 从 [AGENTS.md](AGENTS.md)、[CONTEXT.md](CONTEXT.md) 与 [架构](docs/ARCHITECTURE.md) 了解核心不变量。
- 角色修改以 `config/agent_roles.json` 为源；技能修改以 `skills/` 为源。运行 `python tools/runtime.py render --runtime all` 更新镜像，再运行 `python tools/runtime.py check`。
- 修改 Gate、schema、回执或冻结逻辑时，同步更新相关契约与有针对性的回归测试。不能用空模板、过期回执或机器身份绕过人工核验。
- `paper/PAPER_DRAFT.md` 是 run 内的组装产物，应修改章节源文件后重新 assemble。修改冻结科学产物前执行 unfreeze 并记录原因。
- 保持 PR 范围清晰。说明具体触发条件、修改后的行为与实际执行的验证；未运行的检查写明“未运行”。
- 不提交 `.env`、密钥、私人数据、真实比赛 runs、生成 ZIP 或个人审核记录。

## 验证与校验清单

发布清单覆盖公开源文件。完成修改后，先更新两个同源清单，再执行发布检查：

```powershell
.\.venv\Scripts\python.exe -B tools/release_manifest.py write
.\scripts\check_release.ps1 -PythonExe "$PWD\.venv\Scripts\python.exe"
```

完整检查不调用网络、模型或收费 API，测试在 `runs/` 中使用隔离工作区。GitHub Actions 在 Windows 上运行相同入口。纯文档修改可在本地只做链接核对和 manifest 检查；PR CI 仍执行完整回归。

`.gitattributes` 保留文件原始字节，避免不同系统换行转换导致 SHA-256 校验失败。请避免无关的全文件换行重写。只提交发布需要的文件，清单生成器不依赖 Git 的忽略规则，因此不要在项目根目录遗留临时文件或本地资料。

## Pull Request

1. Fork 仓库，在独立分支修改。
2. 添加与问题对应的复现或验证；记录执行结果。
3. 更新受影响文档、`CHANGELOG.md`（如适用）和两个 manifest。
4. 提交 PR，说明问题、最终行为和验证范围。

贡献以仓库现有 [MIT 许可证](LICENSE) 提供；加入第三方内容时保留来源和必要声明。使用 AI 辅助贡献时，在 PR 中如实说明用途，作者仍负责核对实现和证据。
