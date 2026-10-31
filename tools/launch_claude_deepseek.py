#!/usr/bin/env python3
"""Launch Claude Code on DeepSeek with run policy, budget, and audit binding."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from common import (
    ROOT,
    append_ai_usage_event,
    complete_api_budget_reservation,
    die,
    now_iso,
    relative_to_run,
    require_external_egress_allowed,
    require_safe_component,
    reserve_api_budget,
    resolve_run,
    write_json,
)


def _environment() -> tuple[dict[str, str], str, str]:
    key = str(os.getenv("DEEPSEEK_API_KEY", "")).strip()
    if not key:
        die("DEEPSEEK_API_KEY is not set in the current process")
    model = os.getenv("CUMCM_DEEPSEEK_MODEL", "deepseek-v4-pro[1m]")
    subagent_model = os.getenv(
        "CUMCM_DEEPSEEK_SUBAGENT_MODEL", "deepseek-v4-flash"
    )
    environment = os.environ.copy()
    # Prevent the child from silently falling back to another separately billed
    # provider domain when unrelated credentials are present in the shell.
    for name in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "VERTEX_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
            "ANTHROPIC_AUTH_TOKEN": key,
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": subagent_model,
            "CLAUDE_CODE_SUBAGENT_MODEL": subagent_model,
            "CLAUDE_CODE_EFFORT_LEVEL": os.getenv("CUMCM_CLAUDE_EFFORT", "max"),
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": os.getenv(
                "CUMCM_AUTO_COMPACT_WINDOW", "786432"
            ),
        }
    )
    return environment, model, subagent_model


def _safe_claude_args(values: list[str]) -> list[str]:
    result = list(values)
    if result and result[0] == "--":
        result = result[1:]
    return result


def cmd_launch(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    require_external_egress_allowed(
        run,
        operation="Claude Code with external DeepSeek backend",
        dry_run=bool(args.dry_run),
    )
    claude_args = _safe_claude_args(list(args.claude_args or []))
    preview = {
        "run_id": run.name,
        "provider": "deepseek",
        "backend": "https://api.deepseek.com/anthropic",
        "reserve_usd": float(args.reserve_usd),
        "actual_usd_supplied": args.actual_usd,
        "claude_args": claude_args,
        "dry_run": bool(args.dry_run),
    }
    if args.dry_run:
        print(__import__("json").dumps(preview, ensure_ascii=False, indent=2))
        print("Dry run: Claude Code was not started and no budget was consumed.")
        return 0
    if not args.ack_separate_billing:
        die(
            "必须传入 --ack-separate-billing，确认 DeepSeek API 与 Claude/"
            "ChatGPT/Codex 订阅分开计费。"
        )
    if float(args.reserve_usd) <= 0:
        die("--reserve-usd 必须 > 0")
    claude = shutil.which("claude")
    if not claude:
        die("Claude Code CLI 'claude' was not found in PATH")
    environment, model, subagent_model = _environment()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    session_id = require_safe_component(
        args.id or f"CLAUDE_DEEPSEEK_{stamp}",
        label="Claude Code DeepSeek --id",
    )
    output_dir = run / "modes" / "claude-code-deepseek" / session_id
    if output_dir.exists():
        die(f"Claude Code DeepSeek session id 已存在：{session_id}")
    output_dir.mkdir(parents=True)

    accounted, basis = reserve_api_budget(
        run,
        provider="deepseek",
        request_id=session_id,
        reserve_usd=float(args.reserve_usd),
        actual_usd=args.actual_usd,
        metadata={
            "model": model,
            "subagent_model": subagent_model,
            "mode": "claude-code-deepseek",
        },
    )
    command = [claude, *claude_args]
    execution_path = output_dir / "EXECUTION.json"
    execution: dict[str, Any] = {
        "session_id": session_id,
        "run_id": run.name,
        "created_at": now_iso(),
        "provider": "deepseek",
        "model": model,
        "subagent_model": subagent_model,
        "command": command,
        "status": "attempted",
        "network_request_attempted": True,
        # An interactive launcher cannot know whether the child reached the
        # provider; null is deliberately more honest than a guessed boolean.
        "network_request_sent": None,
        "budget_reserved_usd": accounted,
        "accounting_basis": basis,
        "completed_at": None,
    }
    write_json(execution_path, execution)
    append_ai_usage_event(
        run,
        event_id=session_id,
        tool="Claude Code with DeepSeek Anthropic-compatible backend",
        model=model,
        stage="claude-code-deepseek",
        purpose=args.purpose,
        prompt_or_task_summary=(
            "Interactive Claude Code session; prompts and tool turns remain in "
            "the runtime session, with this launch attempt bound locally."
        ),
        input_artifacts=[],
        output_artifacts=[
            relative_to_run(execution_path, run),
            relative_to_run(output_dir / "ERROR.json", run),
        ],
        adopted=False,
        human_modified=False,
        human_verified=False,
        notes=(
            "Interactive session cost cannot be measured locally; the conservative "
            f"reservation is retained until provider-dashboard reconciliation; {basis}"
        ),
    )

    print(
        f"Starting Claude Code with DeepSeek backend: {model} "
        f"(subagents: {subagent_model})"
    )
    try:
        completed = subprocess.run(command, cwd=ROOT, env=environment, check=False)
    except BaseException as exc:
        error = {
            "created_at": now_iso(),
            "status": "failed-or-unknown",
            "error_type": type(exc).__name__,
            "error": str(exc)[:4000],
        }
        write_json(output_dir / "ERROR.json", error)
        execution.update(
            {
                "status": "failed-or-unknown",
                "completed_at": now_iso(),
                "error_type": type(exc).__name__,
            }
        )
        write_json(execution_path, execution)
        complete_api_budget_reservation(
            run,
            provider="deepseek",
            request_id=session_id,
            status="failed-or-unknown",
            error_type=type(exc).__name__,
        )
        raise

    succeeded = completed.returncode == 0
    status = "succeeded" if succeeded else "failed-or-unknown"
    execution.update(
        {
            "status": status,
            "returncode": int(completed.returncode),
            "completed_at": now_iso(),
        }
    )
    write_json(execution_path, execution)
    if not succeeded:
        write_json(
            output_dir / "ERROR.json",
            {
                "created_at": now_iso(),
                "status": status,
                "error_type": "ClaudeCodeExit",
                "error": f"claude exited with code {completed.returncode}",
            },
        )
    complete_api_budget_reservation(
        run,
        provider="deepseek",
        request_id=session_id,
        status=status,
        error_type="" if succeeded else "ClaudeCodeExit",
    )
    return int(completed.returncode)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--id")
    parser.add_argument("--purpose", default="Interactive CUMCM research session")
    parser.add_argument("--reserve-usd", type=float, required=True)
    parser.add_argument("--actual-usd", type=float)
    parser.add_argument("--ack-separate-billing", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "claude_args",
        nargs=argparse.REMAINDER,
        help="Arguments after -- are forwarded to the Claude Code CLI",
    )
    parser.set_defaults(func=cmd_launch)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
