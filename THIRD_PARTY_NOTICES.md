# Third-party notices

## LLM-as-a-Verifier

本项目不直接内置上游仓库，而是提供适配器与锁定安装脚本。

- Repository: https://github.com/llm-as-a-verifier/llm-as-a-verifier
- Locked commit: `8db8a114355a9d7fdf9a8d1d5c87f6aeebd18770`
- Package version at that commit: `0.2.0`
- License: MIT
- Copyright: 2026 llm-as-a-verifier

`tools/verifier_lite.py` 的 ring pass / pivot selection 结构受到上游 Probabilistic Pivot Tournament 的启发，但不使用上游的 logprob 连续奖励，因此不是该论文方法的忠实实现。

上游 MIT 许可证全文可在以下位置查看：
https://github.com/llm-as-a-verifier/llm-as-a-verifier/blob/8db8a114355a9d7fdf9a8d1d5c87f6aeebd18770/LICENSE
