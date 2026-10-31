#!/usr/bin/env python3
"""Optional OpenAI Responses Multi-agent beta burst with explicit budget consent.

ChatGPT/Codex subscription usage and API billing are separate. This tool is
disabled unless the run budget enables API use and the caller explicitly
acknowledges separate billing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from common import (
    append_ai_usage_event,
    atomic_write_text,
    complete_api_budget_reservation,
    die,
    load_budget,
    now_iso,
    read_json,
    relative_to_run,
    require_external_egress_allowed,
    require_safe_component,
    reserve_api_budget,
    resolve_run,
    sha256_file,
    write_json,
)

DEFAULT_DEVELOPER_INSTRUCTIONS = """
Use subagents only for concrete, independent workstreams that add information.
Do not spawn agents merely to reach a fixed count. Preserve disagreements,
state assumptions, and distinguish generated hypotheses from verified facts.
For mathematical modeling, propose falsification tests, baselines, and evidence
requirements. The root agent must synthesize the work without majority voting.
""".strip()


def _prompt_text(args: argparse.Namespace) -> tuple[str, str]:
    if bool(args.prompt) == bool(args.prompt_file):
        die("必须且只能提供 --prompt 或 --prompt-file 之一")
    if args.prompt_file:
        path = Path(args.prompt_file).expanduser().resolve()
        if not path.exists():
            die(f"prompt file 不存在：{path}")
        return path.read_text(encoding="utf-8-sig"), str(path)
    return str(args.prompt), "inline"


def _budget_check(run: Path, reserve_usd: float, subagents: int, actual_usd: float | None = None) -> dict[str, Any]:
    budget = load_budget(run)
    api = budget.get("openai_api", {})
    if not api.get("enabled", False):
        die("BUDGET.json 中 openai_api.enabled=false。请人工设定正预算后再调用。")
    maximum = float(api.get("max_usd", 0.0))
    used = float(api.get("used_usd", 0.0))
    if reserve_usd <= 0:
        die("--reserve-usd 必须大于 0；这是本次调用的保守预算占用，不是价格估算。")
    planned = max(reserve_usd, float(actual_usd)) if actual_usd is not None else reserve_usd
    if planned < 0:
        die("--actual-usd 不能为负数")
    if used + planned > maximum + 1e-12:
        die(f"API 预算不足：已记账 ${used:.4f} + 本次计划 ${planned:.4f} > 上限 ${maximum:.4f}")
    allowed_subagents = int(api.get("max_concurrent_subagents", 3))
    if subagents > allowed_subagents:
        die(f"请求 subagents={subagents} 超过 BUDGET.json 限制 {allowed_subagents}")
    return budget


def _extract_root_text(response: Any) -> str:
    parts: list[str] = []
    for item in getattr(response, "output", []) or []:
        item_type = getattr(item, "type", None)
        agent = getattr(item, "agent", None)
        agent_name = getattr(agent, "agent_name", None) if agent is not None else None
        phase = getattr(item, "phase", None)
        if item_type == "message" and (agent_name in {None, "/root"}) and phase in {None, "final_answer"}:
            for part in getattr(item, "content", []) or []:
                if getattr(part, "type", None) == "output_text":
                    parts.append(str(getattr(part, "text", "")))
    if parts:
        return "".join(parts)
    fallback = getattr(response, "output_text", None)
    if fallback:
        return str(fallback)
    return ""


def _serializable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool, list, dict)):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dict__"):
        return {key: _serializable(item) for key, item in value.__dict__.items() if not key.startswith("_")}
    return str(value)


def cmd_run(args: argparse.Namespace) -> int:
    if not args.ack_separate_billing:
        die("必须传入 --ack-separate-billing，确认 API 与 ChatGPT/Codex 订阅分开计费。")
    run = resolve_run(args.run)
    require_external_egress_allowed(
        run,
        operation="OpenAI Responses API Multi-agent burst",
        dry_run=bool(args.dry_run),
    )
    subagents = int(args.subagents)
    if subagents < 1:
        die("--subagents 必须 >= 1")
    budget = _budget_check(run, float(args.reserve_usd), subagents, args.actual_usd)
    prompt, prompt_source = _prompt_text(args)
    model = args.model or budget["openai_api"].get("default_model", "gpt-5.6-sol")
    developer = args.developer_instructions or DEFAULT_DEVELOPER_INSTRUCTIONS
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    burst_id = require_safe_component(
        args.id or f"API_BURST_{timestamp}",
        label="api-burst --id",
    )
    output_dir = run / "modes" / "api-burst" / burst_id
    if output_dir.exists():
        die(f"burst id 已存在：{burst_id}")
    output_dir.mkdir(parents=True)

    request_preview = {
        "burst_id": burst_id,
        "run_id": run.name,
        "provider": "openai",
        "created_at": now_iso(),
        "model": model,
        "subagents": subagents,
        "prompt_source": prompt_source,
        "prompt_sha256": sha256_file(Path(prompt_source)) if prompt_source != "inline" else None,
        "developer_instructions": developer,
        "reserve_usd": float(args.reserve_usd),
        "actual_usd_supplied": args.actual_usd,
        "separate_billing_acknowledged": True,
        "beta": "responses_multi_agent=v1",
        "status": "prepared",
        "network_request_attempted": False,
        "network_request_sent": False,
        "attempted_at": None,
        "completed_at": None,
    }
    write_json(output_dir / "REQUEST.json", request_preview)
    atomic_write_text(output_dir / "PROMPT.md", prompt)

    if args.dry_run:
        print(json.dumps(request_preview, ensure_ascii=False, indent=2))
        print("Dry run: no API request sent and no budget consumed.")
        return 0

    if not os.getenv("OPENAI_API_KEY"):
        die("未设置 OPENAI_API_KEY")
    try:
        from openai import OpenAI
    except ImportError:
        die("未安装 openai Python SDK。执行 `python -m pip install --upgrade openai`。")

    client = OpenAI()
    request: dict[str, Any] = {
        "model": model,
        "instructions": developer,
        "input": prompt,
        "store": False,
        "multi_agent": {"enabled": True, "max_concurrent_subagents": subagents},
        "betas": ["responses_multi_agent=v1"],
    }
    if args.max_output_tokens is not None:
        request["max_output_tokens"] = int(args.max_output_tokens)
    if args.reasoning_effort:
        request["reasoning"] = {"effort": args.reasoning_effort}

    accounted_cost, accounting_basis = reserve_api_budget(
        run,
        provider="openai",
        request_id=burst_id,
        reserve_usd=float(args.reserve_usd),
        actual_usd=args.actual_usd,
        metadata={"model": model, "subagents": subagents, "mode": "api-burst"},
    )
    request_preview.update(
        {
            "status": "attempted",
            "network_request_attempted": True,
            "network_request_sent": None,
            "attempted_at": now_iso(),
            "budget_reserved_usd": accounted_cost,
            "accounting_basis": accounting_basis,
        }
    )
    write_json(output_dir / "REQUEST.json", request_preview)
    input_artifacts = [relative_to_run(Path(prompt_source), run)] if prompt_source != "inline" else []
    append_ai_usage_event(
        run,
        event_id=burst_id,
        tool="OpenAI Responses API Multi-agent beta",
        model=model,
        stage="api-burst",
        purpose=args.purpose,
        prompt_or_task_summary=prompt[:800],
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
            f"subagents={subagents}; separate API billing acknowledged; request "
            f"attempt logged before transport; terminal state is in REQUEST.json; "
            f"{accounting_basis}"
        ),
    )
    try:
        response = client.beta.responses.create(**request)
    except AttributeError as exc:
        write_json(
            output_dir / "ERROR.json",
            {
                "created_at": now_iso(),
                "status": "failed-or-unknown",
                "error_type": type(exc).__name__,
                "error": str(exc)[:4000],
            },
        )
        request_preview.update(
            {
                "status": "failed-or-unknown",
                "network_request_sent": None,
                "completed_at": now_iso(),
                "error_type": type(exc).__name__,
            }
        )
        write_json(output_dir / "REQUEST.json", request_preview)
        complete_api_budget_reservation(
            run,
            provider="openai",
            request_id=burst_id,
            status="failed-or-unknown",
            error_type=type(exc).__name__,
        )
        die(
            "当前 openai SDK 不包含 client.beta.responses。请升级 SDK；Multi-agent beta 接口可能发生变化。"
            f" 原始错误：{exc}"
        )
    except BaseException as exc:
        write_json(
            output_dir / "ERROR.json",
            {
                "created_at": now_iso(),
                "status": "failed-or-unknown",
                "error_type": type(exc).__name__,
                "error": str(exc)[:4000],
            },
        )
        request_preview.update(
            {
                "status": "failed-or-unknown",
                "network_request_sent": None,
                "completed_at": now_iso(),
                "error_type": type(exc).__name__,
            }
        )
        write_json(output_dir / "REQUEST.json", request_preview)
        complete_api_budget_reservation(
            run,
            provider="openai",
            request_id=burst_id,
            status="failed-or-unknown",
            error_type=type(exc).__name__,
        )
        raise

    request_preview.update(
        {
            "status": "response-received",
            "network_request_sent": True,
            "response_received_at": now_iso(),
        }
    )
    write_json(output_dir / "REQUEST.json", request_preview)
    usage = _serializable(getattr(response, "usage", None))
    try:
        text = _extract_root_text(response)
        if not text.strip():
            warn_message = "API 返回中未提取到 /root final_answer；请检查 RAW_RESPONSE.json 和 beta schema。"
            print(f"警告：{warn_message}", file=sys.stderr)
        atomic_write_text(output_dir / "RESPONSE.md", text)
        raw = _serializable(response)
        write_json(output_dir / "RAW_RESPONSE.json", raw)
        result = {
            "burst_id": burst_id,
            "created_at": now_iso(),
            "response_id": getattr(response, "id", None),
            "model": model,
            "subagents": subagents,
            "output_path": relative_to_run(output_dir / "RESPONSE.md", run),
            "usage": usage,
            "accounted_usd": accounted_cost,
            "accounting_basis": accounting_basis,
            "human_verification_required": True,
        }
        write_json(output_dir / "RESULT.json", result)
        complete_api_budget_reservation(
            run,
            provider="openai",
            request_id=burst_id,
            status="succeeded",
            usage=usage,
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
        request_preview.update(
            {
                "status": "post-response-error",
                "completed_at": now_iso(),
                "error_type": type(exc).__name__,
            }
        )
        write_json(output_dir / "REQUEST.json", request_preview)
        complete_api_budget_reservation(
            run,
            provider="openai",
            request_id=burst_id,
            status="post-response-error",
            usage=usage,
            error_type=type(exc).__name__,
        )
        raise
    request_preview.update({"status": "succeeded", "completed_at": now_iso()})
    write_json(output_dir / "REQUEST.json", request_preview)
    print(output_dir / "RESPONSE.md")
    print(f"Budget accounted: ${accounted_cost:.4f} ({accounting_basis})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run")
    parser.add_argument("--id")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prompt")
    group.add_argument("--prompt-file")
    parser.add_argument("--purpose", default="Targeted dispute resolution or hypothesis expansion")
    parser.add_argument("--developer-instructions")
    parser.add_argument("--model")
    parser.add_argument("--subagents", type=int, default=3)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--reasoning-effort", choices=["none", "low", "medium", "high", "xhigh", "max"])
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
