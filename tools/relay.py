#!/usr/bin/env python3
"""Fresh-context relay state manager using workspace artifacts and bounded handoffs."""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from common import (
    atomic_write_text,
    die,
    load_config,
    now_iso,
    read_json,
    relative_to_run,
    require_safe_component,
    resolve_run,
    write_json,
)


def _require_session_id(value: Any) -> str:
    raw = str(value or "")
    session_id = require_safe_component(raw, label="relay --id")
    if raw != session_id:
        die("relay --id 必须是无首尾空白的 canonical 单一路径组件")
    return session_id


def _round_dir(session: Path, round_no: int) -> Path:
    return session / f"round_{round_no:03d}"


def _handoff_skeleton(round_no: int) -> dict[str, Any]:
    return {
        "round": round_no,
        "status": "continue",
        "summary": "TODO",
        "evidence": [],
        "artifacts": [],
        "commands_and_results": [],
        "assumptions_added": [],
        "next_steps": [],
        "blockers": [],
        "do_not_repeat": [],
        "workspace_truth": [],
        "next_bounded_task": "TODO",
        "stop_condition": "TODO",
        "completed": False,
    }


def _render_prompt(state: dict[str, Any], round_no: int, previous: dict[str, Any] | None) -> str:
    prior = "No previous round. Verify the initial workspace before acting."
    if previous:
        prior = json.dumps(previous, ensure_ascii=False, indent=2)
    return f"""# Fresh-Agent Relay — {state['session_id']} — Round {round_no}

## Goal

{state['goal']}

## Current bounded task

{state.get('next_task') or state['initial_task']}

## Rules

1. Start as a fresh Agent. Do not assume the previous summary is true.
2. Verify referenced artifacts and commands before continuing.
3. Work only on this bounded task; do not silently expand scope.
4. Do not modify protected files without explicit human approval.
5. Record commands, outcomes, failures and evidence paths.
6. Fill `HANDOFF.json`, set `completed=true`, and choose status `continue`, `complete`, or `blocked`.
7. The workspace is durable memory; conversation history is not.

## Protected files

{chr(10).join('- ' + item for item in state.get('protected_files', [])) or '- none'}

## Previous handoff (untrusted until verified)

```json
{prior}
```
"""


def _validate_handoff(path: Path) -> dict[str, Any]:
    payload = read_json(path, expect=dict)
    required = ["status", "summary", "evidence", "next_steps", "blockers", "do_not_repeat", "workspace_truth", "completed"]
    missing = [key for key in required if key not in payload]
    if missing:
        die(f"handoff 缺少字段：{missing}")
    if not payload.get("completed"):
        die(f"handoff 尚未标记 completed=true：{path}")
    if payload.get("status") not in {"continue", "complete", "blocked"}:
        die("handoff.status 必须是 continue/complete/blocked")
    return payload


def cmd_init(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    session_id = _require_session_id(
        args.id if args.id is not None else f"RELAY_{timestamp}"
    )
    session = run / "relay" / session_id
    if session.exists():
        die(f"relay session 已存在：{session_id}")
    session.mkdir(parents=True)
    state = {
        "version": load_config().get("version", "5.1.0"),
        "session_id": session_id,
        "run_id": run.name,
        "goal": args.goal,
        "initial_task": args.task,
        "next_task": args.task,
        "max_rounds": int(args.max_rounds),
        "current_round": 1,
        "status": "active",
        "protected_files": args.protected_files or [],
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "history": [],
    }
    write_json(session / "STATE.json", state)
    first = _round_dir(session, 1)
    first.mkdir()
    write_json(first / "HANDOFF.json", _handoff_skeleton(1))
    atomic_write_text(first / "PROMPT.md", _render_prompt(state, 1, None))
    print(first / "PROMPT.md")
    return 0


def cmd_advance(args: argparse.Namespace) -> int:
    session = Path(args.session).expanduser().resolve()
    state = read_json(session / "STATE.json", expect=dict)
    if state.get("status") != "active":
        die(f"session 当前状态：{state.get('status')}")
    current = int(state["current_round"])
    handoff_path = _round_dir(session, current) / "HANDOFF.json"
    handoff = _validate_handoff(handoff_path)
    state.setdefault("history", []).append(
        {
            "round": current,
            "status": handoff["status"],
            "summary": handoff["summary"],
            "handoff": str(handoff_path),
            "completed_at": now_iso(),
        }
    )
    if handoff["status"] in {"complete", "blocked"}:
        state["status"] = handoff["status"]
        state["updated_at"] = now_iso()
        write_json(session / "STATE.json", state)
        print(f"Relay ended with status: {handoff['status']}")
        return 0
    if current >= int(state["max_rounds"]):
        state["status"] = "max-rounds-reached"
        state["updated_at"] = now_iso()
        write_json(session / "STATE.json", state)
        die("达到 max_rounds；需要人工决定是否新建 session。", code=4)
    next_round = current + 1
    state["current_round"] = next_round
    state["next_task"] = handoff.get("next_bounded_task") or (handoff.get("next_steps") or ["Human must define next task"])[0]
    state["updated_at"] = now_iso()
    write_json(session / "STATE.json", state)
    target = _round_dir(session, next_round)
    target.mkdir()
    write_json(target / "HANDOFF.json", _handoff_skeleton(next_round))
    atomic_write_text(target / "PROMPT.md", _render_prompt(state, next_round, handoff))
    print(target / "PROMPT.md")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    session = Path(args.session).expanduser().resolve()
    state = read_json(session / "STATE.json", expect=dict)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init")
    p.add_argument("--run")
    p.add_argument("--id")
    p.add_argument("--goal", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--max-rounds", type=int, default=6)
    p.add_argument("--protected-files", nargs="*")
    p.set_defaults(func=cmd_init)
    p = sub.add_parser("advance")
    p.add_argument("--session", required=True)
    p.set_defaults(func=cmd_advance)
    p = sub.add_parser("status")
    p.add_argument("--session", required=True)
    p.set_defaults(func=cmd_status)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
