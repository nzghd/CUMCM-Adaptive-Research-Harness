#!/usr/bin/env python3
"""Export blind/manual-Pro review packets and import responses without UI automation."""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from common import (
    INPUTS_ROOT,
    ROOT,
    append_ai_usage_event,
    atomic_write_text,
    die,
    is_named_human_identity,
    load_budget,
    load_idea_cards,
    now_iso,
    read_json,
    relative_to_root,
    relative_to_run,
    require_external_egress_allowed,
    require_safe_component,
    resolve_project_path,
    resolve_run,
    save_budget,
    sha256_file,
    write_json,
)

TEXT_EXTENSIONS = {".md", ".txt", ".csv", ".json", ".yaml", ".yml"}


def _safe_text(path: Path, remaining: int) -> tuple[str, int]:
    if path.suffix.lower() not in TEXT_EXTENSIONS or remaining <= 0:
        return "", remaining
    if path.stat().st_size > remaining:
        text = path.read_text(encoding="utf-8-sig", errors="replace")[:remaining]
        return text + "\n\n[TRUNCATED]\n", 0
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return text, remaining - len(text.encode("utf-8"))


def _input_sections(max_bytes: int) -> tuple[list[str], list[dict[str, Any]]]:
    sections: list[str] = []
    attachments: list[dict[str, Any]] = []
    remaining = max_bytes
    for base_name in ("problem", "data"):
        base = INPUTS_ROOT / base_name
        if not base.exists():
            continue
        for path in sorted(p for p in base.rglob("*") if p.is_file() and p.name != ".gitkeep"):
            rel = path.relative_to(INPUTS_ROOT).as_posix()
            attachments.append({"path": f"inputs/{rel}", "size": path.stat().st_size, "sha256": sha256_file(path)})
            text, remaining = _safe_text(path, remaining)
            if text:
                sections.extend([f"### File: inputs/{rel}", "", text, ""])
    return sections, attachments


def _read_if_exists(path: Path, max_chars: int = 150000) -> str:
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8-sig", errors="replace")[:max_chars]


def _kind_instructions(kind: str) -> str:
    mapping = {
        "blind-idea": (
            "从零提出若干实质不同、可证伪的建模方向。不要猜测主项目当前采用了什么方法。"
            "每条方向应包含目标问题节点、核心假设、数学结构、数据需求、baseline、验证和失败条件。"
        ),
        "fresh-solution": (
            "独立理解整题并给出模块化路线图。优先说明问题结构和验证需求，不要直接堆砌常见算法名。"
        ),
        "adversarial": (
            "扮演独立红队，努力推翻当前方案。寻找数学漏洞、数据泄漏、不可辨识参数、边界反例、"
            "缺失 baseline、夸大结论和论文数字不一致。给出可重复攻击步骤。"
        ),
        "paper-review": (
            "独立审阅论文。逐项核对公式、符号、单位、数字、图表、引用、证据和结论边界。"
            "输出阻塞问题、重要问题和可选改进，不要只做语言润色。"
        ),
    }
    return mapping[kind]


def cmd_export(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    # Although this command does not itself open a network connection, its
    # declared product is a packet for manual upload to an external service.
    # Block that preparation at the same policy boundary as an API request.
    require_external_egress_allowed(
        run,
        operation="External review packet export",
        dry_run=False,
    )
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    packet_id = require_safe_component(
        args.packet_id or f"EXT_{args.kind.upper().replace('-', '_')}_{timestamp}",
        label="external packet_id",
    )
    if args.out:
        raw_output = Path(args.out).expanduser()
        output_dir = (
            raw_output.resolve()
            if raw_output.is_absolute()
            else (run / raw_output).resolve()
        )
    else:
        output_dir = (run / "external" / packet_id).resolve()
    run_root = run.resolve()
    try:
        output_relative = output_dir.relative_to(run_root)
    except ValueError:
        die(f"external review --out 必须位于当前 run 内：{output_dir}")
    if not output_relative.parts:
        die("external review --out 不能等于 run 根目录")
    if output_dir.exists() and any(output_dir.iterdir()) and not args.replace:
        die(f"输出目录非空：{output_dir}。使用 --replace。")
    if output_dir.exists() and args.replace:
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    input_sections, attachments = _input_sections(int(args.max_embedded_bytes))
    meta = read_json(run / "RUN_META.json", expect=dict)
    sections = [
        "# Independent External Review Packet",
        "",
        "> 请在一个全新的 ChatGPT Pro 对话中人工使用本包。不要通过脚本自动驱动 Pro 界面。",
        "",
        f"- Packet ID: `{packet_id}`",
        f"- Kind: `{args.kind}`",
        f"- Contest/Problem: `{meta.get('contest')}` / `{meta.get('problem')}`",
        "",
        "## Independence rule",
        "",
        "你看不到主项目的其他候选方案。请基于下方材料独立工作，并明确区分事实、假设和待验证内容。",
        "",
        "## Task",
        "",
        _kind_instructions(args.kind),
        "",
        "## Attachments to upload separately",
        "",
    ]
    for item in attachments:
        sections.append(f"- `{item['path']}` — {item['size']} bytes — SHA-256 `{item['sha256']}`")
    sections.extend(["", "## Embedded public material", ""])
    sections.extend(input_sections or ["No text-readable input was embedded. Upload the listed files manually.", ""])

    if args.kind == "adversarial":
        sections.extend(
            [
                "## Current integrated model",
                "",
                _read_if_exists(run / "synthesis" / "FINAL_MODEL_SPEC.md") or "[missing]",
                "",
                "## Claim–evidence ledger",
                "",
                _read_if_exists(run / "evidence" / "CLAIM_EVIDENCE_LEDGER.csv") or "[missing]",
                "",
            ]
        )
    elif args.kind == "paper-review":
        sections.extend(
            [
                "## Paper draft",
                "",
                _read_if_exists(run / "paper" / "PAPER_DRAFT.md") or "[missing]",
                "",
                "## Claim–evidence ledger",
                "",
                _read_if_exists(run / "evidence" / "CLAIM_EVIDENCE_LEDGER.csv") or "[missing]",
                "",
            ]
        )
    elif args.include_problem_graph:
        sections.extend(
            [
                "## Team-approved problem graph",
                "",
                _read_if_exists(run / "problem" / "PROBLEM_GRAPH.json") or "[missing]",
                "",
            ]
        )

    sections.extend(
        [
            "## Output contract",
            "",
            "Return a structured Markdown response. Include concrete assumptions, equations or algorithms where relevant, "
            "a falsification/verification plan, and the strongest reason your own recommendation may fail.",
            "",
        ]
    )
    packet_path = output_dir / "PACKET.md"
    atomic_write_text(packet_path, "\n".join(sections))
    metadata = {
        "packet_id": packet_id,
        "kind": args.kind,
        "created_at": now_iso(),
        "run_id": run.name,
        "independent_context": True,
        "contains_current_problem": True,
        "contains_current_ideas": bool(
            args.include_problem_graph
            or args.kind in {"adversarial", "paper-review"}
        ),
        "include_problem_graph": bool(args.include_problem_graph),
        "attachments": attachments,
        "packet_path": relative_to_root(packet_path),
        "packet_sha256": sha256_file(packet_path),
        "intended_external_transfer": True,
        "status": "exported",
    }
    write_json(output_dir / "META.json", metadata)
    print(packet_path)
    return 0


def _register_idea_from_json(run: Path, response_path: Path, tool: str, model: str) -> str:
    payload = read_json(response_path, expect=dict)
    required = ["id", "title", "target_nodes", "summary", "method_families", "validation_plan", "main_risks"]
    missing = [key for key in required if key not in payload]
    if missing:
        die(f"无法注册为 Idea Card，响应 JSON 缺少：{missing}")
    existing = {card.get("id") for _, card in load_idea_cards(run)}
    idea_id = require_safe_component(payload["id"], label="external Idea ID")
    if idea_id in existing:
        die(f"Idea ID 已存在：{idea_id}")
    payload["id"] = idea_id
    payload.setdefault("schema_version", 1)
    payload["author_resource"] = "manual-pro"
    payload["imported_from"] = relative_to_root(response_path)
    payload["external_tool"] = tool
    payload["external_model"] = model
    payload.setdefault("status", "proposed")
    payload.setdefault("created_at", now_iso())
    idea_dir = run / "research" / "ideas" / idea_id
    idea_dir.mkdir(parents=True, exist_ok=False)
    write_json(idea_dir / "IDEA_CARD.json", payload)
    atomic_write_text(
        idea_dir / "IDEA_CARD.md",
        f"# {idea_id} — {payload['title']}\n\n{payload['summary']}\n\n"
        f"Method families: {', '.join(payload.get('method_families', []))}\n",
    )
    registry_path = run / "ideas" / "IDEA_REGISTRY.json"
    registry = read_json(registry_path, expect=dict)
    registry.setdefault("ideas", []).append(
        {
            "id": idea_id,
            "title": payload["title"],
            "path": relative_to_run(idea_dir / "IDEA_CARD.json", run),
            "source": "manual-pro",
            "status": payload.get("status", "proposed"),
            "created_at": payload["created_at"],
        }
    )
    write_json(registry_path, registry)
    return idea_id


def cmd_import(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    verifier = str(args.by or "").strip()
    if args.adopted and not args.verified:
        die("--adopted 必须同时指定 --verified，并通过 --by 记录具名核验人")
    if args.verified and not is_named_human_identity(verifier):
        die("--verified 必须通过 --by 记录具名队员，不能使用 TEAM/UNKNOWN/TODO 等占位值")
    response = Path(args.response).expanduser().resolve()
    if not response.exists() or not response.is_file():
        die(f"响应文件不存在：{response}")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    import_id = require_safe_component(
        args.import_id or f"IMP_{args.kind.upper().replace('-', '_')}_{timestamp}",
        label="external import_id",
    )
    if args.packet_id:
        require_safe_component(args.packet_id, label="external packet_id")
    output_dir = run / "external" / import_id
    if output_dir.exists():
        die(f"import_id 已存在：{import_id}")
    output_dir.mkdir(parents=True)
    target = output_dir / f"RESPONSE{response.suffix.lower() or '.md'}"
    shutil.copy2(response, target)
    idea_id = None
    if args.register_idea:
        if response.suffix.lower() != ".json":
            die("--register-idea 需要结构化 JSON 响应")
        idea_id = _register_idea_from_json(run, target, args.tool, args.model)

    metadata = {
        "import_id": import_id,
        "kind": args.kind,
        "imported_at": now_iso(),
        "tool": args.tool,
        "model": args.model,
        "source_file": str(response),
        "stored_file": relative_to_run(target, run),
        "sha256": sha256_file(target),
        "packet_id": args.packet_id,
        "registered_idea": idea_id,
        "human_verified": bool(args.verified),
        "verified_by": verifier if args.verified else "",
    }
    write_json(output_dir / "META.json", metadata)
    packet_artifacts = [f"external/{args.packet_id}/PACKET.md"] if args.packet_id else []
    append_ai_usage_event(
        run,
        event_id=import_id,
        tool=args.tool,
        model=args.model,
        stage="external-pro-review",
        purpose=args.kind,
        prompt_or_task_summary=f"Independent manual dialogue; packet={args.packet_id or 'not recorded'}",
        input_artifacts=packet_artifacts,
        output_artifacts=[relative_to_run(target, run)] + ([f"research/ideas/{idea_id}/IDEA_CARD.json"] if idea_id else []),
        adopted=bool(args.adopted),
        human_modified=bool(args.modified),
        human_verified=bool(args.verified),
        verified_by=verifier,
        notes=args.note or "",
    )
    budget = load_budget(run)
    budget["manual_pro"]["used_dialogues"] = int(budget["manual_pro"].get("used_dialogues", 0)) + 1
    save_budget(run, budget)
    print(target)
    if idea_id:
        print(f"Registered Idea: {idea_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("export")
    p.add_argument("--run")
    p.add_argument("--kind", choices=["blind-idea", "fresh-solution", "adversarial", "paper-review"], default="blind-idea")
    p.add_argument("--packet-id")
    p.add_argument("--out")
    p.add_argument("--max-embedded-bytes", type=int, default=180000)
    p.add_argument("--include-problem-graph", action="store_true")
    p.add_argument("--replace", action="store_true")
    p.set_defaults(func=cmd_export)
    p = sub.add_parser("import")
    p.add_argument("--run")
    p.add_argument("--kind", choices=["blind-idea", "fresh-solution", "adversarial", "paper-review"], required=True)
    p.add_argument("--response", required=True)
    p.add_argument("--tool", default="ChatGPT")
    p.add_argument("--model", required=True)
    p.add_argument("--packet-id")
    p.add_argument("--import-id")
    p.add_argument("--register-idea", action="store_true")
    p.add_argument("--adopted", action="store_true")
    p.add_argument("--modified", action="store_true")
    p.add_argument("--verified", action="store_true")
    p.add_argument("--by", default="TEAM")
    p.add_argument("--note")
    p.set_defaults(func=cmd_import)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
