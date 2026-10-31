#!/usr/bin/env python3
"""Run one canonical CUMCM role through a selectable API provider.

This is intentionally a bounded packet runner, not an autonomous shell/file
agent.  The caller explicitly selects text artifacts to include.  Use Codex or
Claude Code for tasks that need native workspace tools and iterative execution.

Supported providers: OpenAI Responses API, Anthropic Messages API, and
DeepSeek's OpenAI-compatible Chat Completions API.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from common import (
    ROOT,
    append_ai_usage_event,
    atomic_write_text,
    complete_api_budget_reservation,
    die,
    load_budget,
    now_iso,
    read_json,
    relative_to_root,
    relative_to_run,
    require_external_egress_allowed,
    require_safe_component,
    reserve_api_budget,
    resolve_project_path,
    resolve_run,
    write_json,
)

TEXT_EXTENSIONS = {
    ".md", ".txt", ".csv", ".json", ".jsonl", ".py", ".toml", ".yaml", ".yml",
    ".tex", ".r", ".jl", ".m", ".ini", ".cfg", ".tsv",
}
BLOCKED_NAMES = {".env", ".env.local", ".env.production", "credentials.json", "secrets.json"}


def _registry() -> dict[str, Any]:
    return read_json(ROOT / "config" / "runtime_registry.json", expect=dict)


def _role(name: str) -> dict[str, Any]:
    payload = read_json(ROOT / "config" / "agent_roles.json", expect=dict)
    normalized = name.replace("-", "_")
    for role in payload.get("roles", []):
        if role.get("name") == normalized:
            return role
    choices = ", ".join(sorted(str(item.get("name")) for item in payload.get("roles", [])))
    die(f"未知 role：{name}。可选：{choices}")


def _safe_text_files(base: Path) -> list[Path]:
    if base.is_file():
        return [base]
    return [p for p in sorted(base.rglob("*")) if p.is_file()]


def _collect(paths: Iterable[Path], max_bytes: int) -> tuple[list[tuple[Path, str]], list[str]]:
    records: list[tuple[Path, str]] = []
    skipped: list[str] = []
    total = 0
    seen: set[Path] = set()
    for base in paths:
        for path in _safe_text_files(base):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if path.name.lower() in BLOCKED_NAMES or any(part.lower() in {".git", "__pycache__", ".venv", "venv"} for part in path.parts):
                skipped.append(f"blocked:{relative_to_root(path)}")
                continue
            if path.suffix.lower() not in TEXT_EXTENSIONS:
                skipped.append(f"binary-or-unsupported:{relative_to_root(path)}")
                continue
            size = path.stat().st_size
            if total + size > max_bytes:
                skipped.append(f"packet-limit:{relative_to_root(path)}")
                continue
            try:
                text = path.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError:
                skipped.append(f"decode-failed:{relative_to_root(path)}")
                continue
            total += size
            records.append((path, text))
    return records, skipped


def _task_text(args: argparse.Namespace, run: Path) -> tuple[str, list[Path]]:
    if bool(args.task) == bool(args.task_file):
        die("必须且只能提供 --task 或 --task-file 之一")
    task_sources: list[Path] = []
    if args.task_file:
        raw = Path(args.task_file)
        path = raw if raw.is_absolute() else (run / raw)
        path = resolve_project_path(path, must_exist=True)
        task_sources.append(path)
        return path.read_text(encoding="utf-8-sig"), task_sources
    return str(args.task), task_sources


def _include_paths(args: argparse.Namespace, run: Path) -> list[Path]:
    result: list[Path] = []
    for value in args.include_run or []:
        result.append(resolve_project_path(run / value, must_exist=True))
    for value in args.include_project or []:
        result.append(resolve_project_path(value, must_exist=True))
    return result


def _build_packet(role: dict[str, Any], task: str, files: list[tuple[Path, str]], skipped: list[str]) -> str:
    chunks = [
        f"# Canonical role\n\n`{role['name']}` — {role['description']}\n",
        "# Assigned task\n\n" + task.strip() + "\n",
        "# Evidence packet\n",
    ]
    if not files:
        chunks.append("\n(No text artifacts were included.)\n")
    for path, text in files:
        chunks.append(
            f"\n## FILE: {relative_to_root(path)}\n\n"
            "```text\n" + text.rstrip() + "\n```\n"
        )
    if skipped:
        chunks.append("\n# Skipped artifacts\n\n" + "\n".join(f"- {item}" for item in skipped) + "\n")
    chunks.append(
        "\n# Output discipline\n\n"
        "Return only the assigned role's work product. Distinguish verified facts, interpretations, assumptions, and unknowns. "
        "Do not invent experiment results, citations, files, commands, or measurements. If the packet is insufficient, state exactly what is missing.\n"
    )
    return "".join(chunks)


def _system_prompt(role: dict[str, Any]) -> str:
    core = (ROOT / "RUNTIME_RULES.md").read_text(encoding="utf-8")
    return (
        "You are executing one bounded role inside CUMCM Adaptive Research Harness v5.1.\n\n"
        f"ROLE: {role['name']}\nDESCRIPTION: {role['description']}\n\n"
        f"ROLE INSTRUCTIONS:\n{role['instructions']}\n\n"
        "PROJECT RULES:\n" + core
    )


def _budget_check(run: Path, provider: str, reserve_usd: float, actual_usd: float | None) -> dict[str, Any]:
    budget = load_budget(run)
    providers = budget.get("api_providers", {})
    item = providers.get(provider, {})
    # v3 OpenAI-only runs can still use the old openai_api section.
    if provider == "openai" and not item:
        item = budget.get("openai_api", {})
    if not item.get("enabled", False):
        die(f"BUDGET.json 中 api_providers.{provider}.enabled=false；init 时给该 provider 设置正预算后再调用。")
    if reserve_usd <= 0:
        die("--reserve-usd 必须 > 0；它是保守记账额度，不是价格估算。")
    maximum = float(item.get("max_usd", 0.0))
    used = float(item.get("used_usd", 0.0))
    planned = max(float(reserve_usd), float(actual_usd)) if actual_usd is not None else float(reserve_usd)
    if actual_usd is not None and actual_usd < 0:
        die("--actual-usd 不能为负数")
    if used + planned > maximum + 1e-12:
        die(f"{provider} API 预算不足：${used:.4f} + ${planned:.4f} > ${maximum:.4f}")
    return budget


def _serializable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool, list, dict)):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return str(value)


def _call_openai(model: str, system: str, packet: str, max_output_tokens: int) -> tuple[str, Any, Any]:
    try:
        from openai import OpenAI
    except ImportError:
        die("未安装 openai SDK。执行 `python -m pip install -r requirements-api.txt`。")
    if not os.getenv("OPENAI_API_KEY"):
        die("未设置 OPENAI_API_KEY")
    client = OpenAI()
    response = client.responses.create(
        model=model,
        instructions=system,
        input=packet,
        max_output_tokens=max_output_tokens,
        store=False,
    )
    return str(getattr(response, "output_text", "") or ""), response, getattr(response, "usage", None)


def _call_deepseek(model: str, system: str, packet: str, max_output_tokens: int) -> tuple[str, Any, Any]:
    try:
        from openai import OpenAI
    except ImportError:
        die("未安装 openai SDK（DeepSeek OpenAI-compatible 客户端需要它）。执行 `python -m pip install -r requirements-api.txt`。")
    key = os.getenv("DEEPSEEK_API_KEY")
    if not key:
        die("未设置 DEEPSEEK_API_KEY")
    client = OpenAI(api_key=key, base_url="https://api.deepseek.com")
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": packet},
        ],
        max_tokens=max_output_tokens,
    )
    text = ""
    if getattr(response, "choices", None):
        text = str(response.choices[0].message.content or "")
    return text, response, getattr(response, "usage", None)


def _call_anthropic(model: str, system: str, packet: str, max_output_tokens: int) -> tuple[str, Any, Any]:
    try:
        import anthropic
    except ImportError:
        die("未安装 anthropic SDK。执行 `python -m pip install -r requirements-api.txt`。")
    if not os.getenv("ANTHROPIC_API_KEY"):
        die("未设置 ANTHROPIC_API_KEY")
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        system=system,
        max_tokens=max_output_tokens,
        messages=[{"role": "user", "content": packet}],
    )
    parts = [str(block.text) for block in getattr(response, "content", []) if getattr(block, "type", None) == "text"]
    return "".join(parts), response, getattr(response, "usage", None)


def cmd_run(args: argparse.Namespace) -> int:
    if not args.ack_separate_billing:
        die("必须传入 --ack-separate-billing，确认直接 API 与订阅/本地运行时分开计费。")
    run = resolve_run(args.run)
    require_external_egress_allowed(
        run,
        operation=f"Direct API ({args.provider})",
        dry_run=bool(args.dry_run),
    )
    role = _role(args.role)
    task, task_sources = _task_text(args, run)
    include_paths = _include_paths(args, run)
    files, skipped = _collect(include_paths, int(args.max_input_bytes))
    packet = _build_packet(role, task, files, skipped)
    system = _system_prompt(role)

    registry = _registry()
    provider_cfg = registry.get("providers", {}).get(args.provider, {})
    budget = _budget_check(run, args.provider, float(args.reserve_usd), args.actual_usd)
    budget_provider = budget.get("api_providers", {}).get(args.provider, {})
    model = args.model or budget_provider.get("default_model") or provider_cfg.get("default_model")
    if not model:
        die(f"没有为 provider={args.provider} 配置 model；请传 --model")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    call_id = require_safe_component(
        args.id or f"DIRECT_{args.provider.upper()}_{stamp}",
        label="direct-api --id",
    )
    output_dir = run / "modes" / "direct-api" / call_id
    if output_dir.exists():
        die(f"direct-api id 已存在：{call_id}")
    output_dir.mkdir(parents=True)
    atomic_write_text(output_dir / "PACKET.md", packet)
    request = {
        "id": call_id,
        "run_id": run.name,
        "created_at": now_iso(),
        "provider": args.provider,
        "model": model,
        "role": role["name"],
        "task_source": args.task_file or "inline",
        "included_files": [relative_to_root(path) for path, _ in files],
        "skipped": skipped,
        "max_input_bytes": int(args.max_input_bytes),
        "max_output_tokens": int(args.max_output_tokens),
        "reserve_usd": float(args.reserve_usd),
        "actual_usd_supplied": args.actual_usd,
        "separate_billing_acknowledged": True,
        "status": "prepared",
        "network_request_attempted": False,
        "network_request_sent": False,
        "attempted_at": None,
        "completed_at": None,
    }
    write_json(output_dir / "REQUEST.json", request)

    if args.dry_run:
        print(output_dir / "PACKET.md")
        print(json.dumps(request, ensure_ascii=False, indent=2))
        print("Dry run: no API request sent and no budget consumed.")
        return 0

    callers = {
        "openai": _call_openai,
        "anthropic": _call_anthropic,
        "deepseek": _call_deepseek,
    }
    input_artifacts = [relative_to_run(path, run) if run in path.parents else relative_to_root(path) for path, _ in files]
    input_artifacts += [relative_to_run(path, run) if run in path.parents else relative_to_root(path) for path in task_sources]
    accounted, basis = reserve_api_budget(
        run,
        provider=args.provider,
        request_id=call_id,
        reserve_usd=float(args.reserve_usd),
        actual_usd=args.actual_usd,
        metadata={"model": model, "role": role["name"], "mode": "direct-api"},
    )
    request.update(
        {
            "status": "attempted",
            "network_request_attempted": True,
            # Until a response arrives, a transport exception cannot prove
            # whether the remote provider received the payload.
            "network_request_sent": None,
            "attempted_at": now_iso(),
            "budget_reserved_usd": accounted,
            "accounting_basis": basis,
        }
    )
    write_json(output_dir / "REQUEST.json", request)
    append_ai_usage_event(
        run,
        event_id=call_id,
        tool=f"Direct API role runner ({args.provider})",
        model=model,
        stage=args.stage,
        purpose=args.purpose,
        prompt_or_task_summary=task[:800],
        input_artifacts=input_artifacts,
        output_artifacts=[
            relative_to_run(output_dir / "REQUEST.json", run),
            relative_to_run(output_dir / "RESPONSE.md", run),
            relative_to_run(output_dir / "ERROR.json", run),
        ],
        adopted=False,
        human_modified=False,
        human_verified=False,
        notes=(
            f"role={role['name']}; bounded packet runner; request attempt logged "
            f"before transport; terminal state is in REQUEST.json; {basis}"
        ),
    )

    try:
        text, raw, usage = callers[args.provider](
            model, system, packet, int(args.max_output_tokens)
        )
    except BaseException as exc:
        error = {
            "created_at": now_iso(),
            "status": "failed-or-unknown",
            "error_type": type(exc).__name__,
            "error": str(exc)[:4000],
        }
        write_json(output_dir / "ERROR.json", error)
        request.update(
            {
                "status": "failed-or-unknown",
                "network_request_sent": None,
                "completed_at": now_iso(),
                "error_type": type(exc).__name__,
            }
        )
        write_json(output_dir / "REQUEST.json", request)
        complete_api_budget_reservation(
            run,
            provider=args.provider,
            request_id=call_id,
            status="failed-or-unknown",
            error_type=type(exc).__name__,
        )
        raise

    request.update(
        {
            "status": "response-received",
            "network_request_sent": True,
            "response_received_at": now_iso(),
        }
    )
    write_json(output_dir / "REQUEST.json", request)
    serial_usage = _serializable(usage)
    try:
        atomic_write_text(output_dir / "RESPONSE.md", text)
        write_json(output_dir / "RAW_RESPONSE.json", _serializable(raw))
        write_json(output_dir / "RESULT.json", {
            "id": call_id,
            "provider": args.provider,
            "model": model,
            "role": role["name"],
            "response_path": relative_to_run(output_dir / "RESPONSE.md", run),
            "usage": serial_usage,
            "accounted_usd": accounted,
            "accounting_basis": basis,
            "human_verification_required": True,
        })
        complete_api_budget_reservation(
            run,
            provider=args.provider,
            request_id=call_id,
            status="succeeded",
            usage=serial_usage,
        )
    except BaseException as exc:
        write_json(
            output_dir / "ERROR.json",
            {
                "created_at": now_iso(),
                "status": "post-response-error",
                "error_type": type(exc).__name__,
                "error": str(exc)[:4000],
            },
        )
        request.update(
            {
                "status": "post-response-error",
                "completed_at": now_iso(),
                "error_type": type(exc).__name__,
            }
        )
        write_json(output_dir / "REQUEST.json", request)
        complete_api_budget_reservation(
            run,
            provider=args.provider,
            request_id=call_id,
            status="post-response-error",
            usage=serial_usage,
            error_type=type(exc).__name__,
        )
        raise
    request.update({"status": "succeeded", "completed_at": now_iso()})
    write_json(output_dir / "REQUEST.json", request)
    print(output_dir / "RESPONSE.md")
    print(f"Budget accounted: ${accounted:.4f} ({basis})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run")
    parser.add_argument("--provider", choices=["openai", "anthropic", "deepseek"], required=True)
    parser.add_argument("--role", required=True)
    task = parser.add_mutually_exclusive_group(required=True)
    task.add_argument("--task")
    task.add_argument("--task-file", help="Path relative to the run unless absolute")
    parser.add_argument("--include-run", action="append", default=[], help="File/dir relative to active run; repeatable")
    parser.add_argument("--include-project", action="append", default=[], help="File/dir relative to project root; repeatable")
    parser.add_argument("--model")
    parser.add_argument("--id")
    parser.add_argument("--stage", default="direct-api")
    parser.add_argument("--purpose", default="Bounded independent research or audit")
    parser.add_argument("--max-input-bytes", type=int, default=750000)
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--reserve-usd", type=float, required=True)
    parser.add_argument("--actual-usd", type=float)
    parser.add_argument("--ack-separate-billing", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.set_defaults(func=cmd_run)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
