# LLM-as-a-Verifier Faithful Mode

此目录把官方方案保留为独立模式；核心算法不复制、不魔改。

## 固定上游

- Repository: `llm-as-a-verifier/llm-as-a-verifier`
- Pinned commit: `8db8a114355a9d7fdf9a8d1d5c87f6aeebd18770`
- Upstream package version: `0.2.0`
- License: MIT

## 为什么需要独立后端

论文方法读取评分 token 的完整 logprob 分布并计算期望。普通 ChatGPT Pro/Codex 产品界面不会把这些 logprobs 暴露给本地 Python，因此 Faithful 模式需要：

- 上游支持的 API；或
- 能返回 token-level logprobs 的 OpenAI-compatible 本地服务。

## 安装

使用项目根目录的唯一安装脚本。它会创建隔离环境 `.venv-verifier`：

Windows：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_reference_verifier.ps1
```

Linux/macOS：

```bash
bash scripts/install_reference_verifier.sh
```

安装脚本从固定 commit 安装，不把上游源码复制进本 Harness。

## 推荐运行入口

先复制并修改 `templates/verifier/faithful_config.example.json`，然后：

```powershell
python .\tools\verifier_faithful.py dry-run --config .\runs\<RUN_ID>\modes\reference-verifier-faithful\config.json
python .\tools\verifier_faithful.py diagnose
python .\tools\verifier_faithful.py run --config .\runs\<RUN_ID>\modes\reference-verifier-faithful\config.json
```

`tools/verifier_faithful.py` 会准备输入、调用本目录中的薄适配器、检查上游版本，并记录结果。底层适配器也可直接执行，但通常没有必要。

## 保护措施

适配器会拒绝或警告：

- 少于两个候选；
- 候选文件为空；
- 安装版本与锁定版本不一致；
- 所有分数几乎完全相同；
- 所有分数塌缩到约 0.5。

这类异常可能表示后端 logprob 不可用、解析失败或候选不可区分，并不证明候选真的等价。
