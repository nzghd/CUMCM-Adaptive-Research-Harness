#!/usr/bin/env python3
"""Runtime adapter utilities for CUMCM Adaptive Research Harness v5.1.

This tool keeps logical agent roles runtime-neutral.  Canonical role definitions
live in config/agent_roles.json and are rendered into Codex and Claude Code
project formats.  It performs no model or network call.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import shutil as _shutil
from pathlib import Path
from typing import Any

from common import ROOT, die, read_json

ROLES_PATH = ROOT / "config" / "agent_roles.json"
RUNTIME_PATH = ROOT / "config" / "runtime_registry.json"
CANONICAL_SKILLS = ROOT / "skills"


def _roles() -> list[dict[str, Any]]:
    payload = read_json(ROLES_PATH, expect=dict)
    roles = payload.get("roles", [])
    if not isinstance(roles, list) or not roles:
        die("config/agent_roles.json 未包含 roles")
    return roles


def _runtime_registry() -> dict[str, Any]:
    return read_json(RUNTIME_PATH, expect=dict)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_codex_role(role: dict[str, Any]) -> str:
    instructions = str(role["instructions"]).replace('"""', '\"\"\"')
    return (
        f"# GENERATED from config/agent_roles.json by tools/runtime.py; do not edit by hand.\n"
        f"name = {_toml_string(str(role['name']))}\n"
        f"description = {_toml_string(str(role['description']))}\n"
        f"sandbox_mode = {_toml_string(str(role.get('access', 'read-only')))}\n"
        f"developer_instructions = \"\"\"\n{instructions}\n\"\"\"\n"
    )


def _yaml_scalar(value: str) -> str:
    # JSON strings are valid YAML scalars and avoid hand-written escaping rules.
    return json.dumps(value, ensure_ascii=False)


def render_claude_role(role: dict[str, Any]) -> str:
    name = str(role["name"]).replace("_", "-")
    tools = role.get("claude_tools", ["Read", "Glob", "Grep"])
    tool_line = ", ".join(str(item) for item in tools)
    access_note = (
        "This logical role is read-only by policy. Do not modify project files. "
        if role.get("access") == "read-only"
        else "Write only assigned artifacts; do not modify inputs/ or unrelated shared files. "
    )
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {_yaml_scalar(str(role['description']))}\n"
        f"tools: {tool_line}\n"
        "---\n\n"
        "<!-- GENERATED from config/agent_roles.json by tools/runtime.py; do not edit by hand. -->\n\n"
        f"{access_note}{str(role['instructions']).strip()}\n"
    )


def _sync_skills(destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for skill_dir in sorted(CANONICAL_SKILLS.iterdir()):
        if skill_dir.is_dir():
            shutil.copytree(skill_dir, destination / skill_dir.name)


def render_runtime(name: str) -> list[Path]:
    written: list[Path] = []
    roles = _roles()
    if name == "codex":
        out_dir = ROOT / ".codex" / "agents"
        out_dir.mkdir(parents=True, exist_ok=True)
        for old in out_dir.glob("*.toml"):
            old.unlink()
        for role in roles:
            path = out_dir / (str(role["name"]).replace("_", "-") + ".toml")
            path.write_text(render_codex_role(role), encoding="utf-8")
            written.append(path)
        _sync_skills(ROOT / ".agents" / "skills")
        written.extend(sorted((ROOT / ".agents" / "skills").rglob("SKILL.md")))
    elif name == "claude-code":
        out_dir = ROOT / ".claude" / "agents"
        out_dir.mkdir(parents=True, exist_ok=True)
        for old in out_dir.glob("*.md"):
            old.unlink()
        for role in roles:
            path = out_dir / (str(role["name"]).replace("_", "-") + ".md")
            path.write_text(render_claude_role(role), encoding="utf-8")
            written.append(path)
        _sync_skills(ROOT / ".claude" / "skills")
        written.extend(sorted((ROOT / ".claude" / "skills").rglob("SKILL.md")))
    else:
        die(f"render 不支持 runtime={name}；可选 codex, claude-code, all")
    return written


def cmd_render(args: argparse.Namespace) -> int:
    names = ["codex", "claude-code"] if args.runtime == "all" else [args.runtime]
    count = 0
    for name in names:
        count += len(render_runtime(name))
    print(f"Rendered {count} runtime files from canonical roles/skills.")
    return 0


def _expected_files(runtime: str) -> dict[Path, str]:
    expected: dict[Path, str] = {}
    for role in _roles():
        stem = str(role["name"]).replace("_", "-")
        if runtime == "codex":
            expected[ROOT / ".codex" / "agents" / f"{stem}.toml"] = render_codex_role(role)
        elif runtime == "claude-code":
            expected[ROOT / ".claude" / "agents" / f"{stem}.md"] = render_claude_role(role)
    return expected


def cmd_check(args: argparse.Namespace) -> int:
    errors: list[str] = []
    for runtime in ("codex", "claude-code"):
        for path, expected in _expected_files(runtime).items():
            if not path.exists():
                errors.append(f"missing {path.relative_to(ROOT)}")
            elif path.read_text(encoding="utf-8") != expected:
                errors.append(f"out-of-sync {path.relative_to(ROOT)}")
    for dest in (ROOT / ".agents" / "skills", ROOT / ".claude" / "skills"):
        for source in sorted(CANONICAL_SKILLS.rglob("SKILL.md")):
            relative = source.relative_to(CANONICAL_SKILLS)
            target = dest / relative
            if not target.exists() or target.read_text(encoding="utf-8") != source.read_text(encoding="utf-8"):
                errors.append(f"out-of-sync {target.relative_to(ROOT)}")
    if errors:
        print("Runtime adapter check failed:")
        for item in errors:
            print(f"- {item}")
        return 3
    print("OK: canonical roles and skills match Codex + Claude Code runtime files.")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    registry = _runtime_registry()
    print("Runtime                  Provider     Workspace  Subagents  Launch")
    print("-----------------------  -----------  ---------  ---------  -------------------------------")
    for name, cfg in registry.get("runtimes", {}).items():
        print(
            f"{name:<23}  {str(cfg.get('provider','')):<11}  "
            f"{str(bool(cfg.get('supports_workspace_actions'))):<9}  "
            f"{str(bool(cfg.get('supports_subagents'))):<9}  {cfg.get('launch_command','')}"
        )
    return 0


def cmd_guide(args: argparse.Namespace) -> int:
    registry = _runtime_registry()
    runtimes = registry.get("runtimes", {})
    if args.runtime not in runtimes:
        die(f"未知 runtime：{args.runtime}")
    cfg = runtimes[args.runtime]
    print(f"Runtime: {args.runtime}")
    print(f"Provider: {cfg.get('provider')}")
    print(f"Project rules: {cfg.get('project_rules')}")
    print(f"Agent dir: {cfg.get('agent_dir', '(packet API)')}")
    print(f"Skill dir: {cfg.get('skill_dir', '(packet API)')}")
    print(f"Launch: {cfg.get('launch_command', 'python tools/provider_call.py ...')}")
    print(f"Notes: {cfg.get('notes','')}")
    if args.runtime == "claude-code-deepseek":
        print("Required env: DEEPSEEK_API_KEY (the launcher maps it to Claude Code's Anthropic-compatible variables only for that process).")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    runtime = args.runtime
    registry = _runtime_registry().get("runtimes", {})
    if runtime not in registry:
        die(f"未知 runtime：{runtime}")
    problems: list[str] = []
    if runtime == "codex" and _shutil.which("codex") is None:
        problems.append("未在 PATH 找到 codex")
    if runtime in {"claude-code", "claude-code-deepseek"} and _shutil.which("claude") is None:
        problems.append("未在 PATH 找到 claude")
    if runtime == "claude-code-deepseek" and not os.getenv("DEEPSEEK_API_KEY"):
        problems.append("未设置 DEEPSEEK_API_KEY")
    check_code = cmd_check(argparse.Namespace())
    if check_code:
        problems.append("runtime generated files 与 canonical roles/skills 不同步")
    if problems:
        print("Doctor found issues:")
        for problem in problems:
            print(f"- {problem}")
        return 3
    print(f"OK: runtime={runtime} local checks passed (no network/model call).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="列出可用 runtime")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("render", help="从 canonical roles/skills 生成 runtime 文件")
    p.add_argument("--runtime", choices=["codex", "claude-code", "all"], default="all")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("check", help="检查 runtime 文件是否与 canonical 定义一致")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("guide", help="显示某 runtime 的启动说明")
    p.add_argument("--runtime", required=True, choices=["codex", "claude-code", "claude-code-deepseek", "direct-api"])
    p.set_defaults(func=cmd_guide)

    p = sub.add_parser("doctor", help="本地检查 runtime CLI/env/生成文件；不联网")
    p.add_argument("--runtime", required=True, choices=["codex", "claude-code", "claude-code-deepseek", "direct-api"])
    p.set_defaults(func=cmd_doctor)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
