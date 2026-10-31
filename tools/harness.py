#!/usr/bin/env python3
"""State, evidence, gate and audit CLI for CUMCM Adaptive Research Harness."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from common import (
    ACTIVE_FILE,
    CLAIM_EVIDENCE_FIELDS,
    HARNESS_STATE,
    INPUTS_ROOT,
    PLACEHOLDER_HUMAN_IDENTITIES,
    ROOT,
    RUNS_ROOT,
    active_run_id,
    append_csv,
    append_ai_usage_event,
    append_jsonl,
    atomic_write_text,
    contains_placeholder,
    die,
    is_named_human_identity,
    is_timezone_aware_iso,
    is_truthy,
    iter_files,
    load_budget,
    load_config,
    load_idea_cards,
    load_module_cards,
    load_run_meta,
    load_status,
    next_numeric_id,
    nonempty_file,
    now_iso,
    pdf_envelope_errors,
    read_csv,
    read_json,
    read_jsonl,
    relative_to_root,
    relative_to_run,
    require_safe_component,
    replace_placeholders,
    resolve_project_path,
    resolve_run,
    save_budget,
    save_status,
    sha256_file,
    slugify,
    warn,
    write_active,
    write_csv,
    write_json,
)

CLAIM_FIELDS = list(CLAIM_EVIDENCE_FIELDS)

SOURCE_FIELDS = [
    "source_id",
    "title",
    "author_or_org",
    "year",
    "url",
    "source_type",
    "claim_supported",
    "accessed_at",
    "verified_by",
    "notes",
]


def _require_canonical_path_component(value: Any, *, label: str) -> str:
    """Reject traversal and non-canonical spelling before path construction."""
    raw = str(value or "")
    component = require_safe_component(raw, label=label)
    if raw != component:
        die(f"{label} 必须是无首尾空白的 canonical 单一路径组件")
    return component

PHASES = ("rehearsal", "competition", "benchmark", "calibration")
RUNTIMES = ("codex", "claude-code", "claude-code-deepseek", "direct-api")
UNSPECIFIED_METRIC_VALUES = {"", "unknown", "unspecified", "n/a", "na", "none", "null"}

# DECISION_LOG.jsonl is append-only and also carries operational audit events.
# Gate approvals must not invalidate themselves merely by appending their audit
# record.  G2 therefore binds only the events that define its model-selection
# history; the selected focus itself is independently bound by FOCUS_SET.json.
MODEL_SELECTION_DECISION_EVENT_TYPES = frozenset({
    "idea-created",
    "module-created",
    "focus-set",
})
NON_SEMANTIC_DECISION_EVENT_TYPES = frozenset({
    "budget-log",
    "gate-approval",
    "gate-revoke",
    "gate-stale-detected",
    "gate-approval-blocked",
    "freeze",
    "unfreeze",
    "package-validation-bypass",
})
EMPIRICAL_CLAIM_TYPES = frozenset({
    "result", "empirical", "experiment", "experimental", "quantitative",
    "performance", "metric", "statistical", "numerical", "simulation",
    "实验", "实证", "定量", "性能", "统计", "数值", "仿真",
})
EMPIRICAL_EVIDENCE_TYPES = frozenset({
    "experiment", "experimental", "benchmark", "metric", "simulation",
    "statistical_test", "numerical_test", "ablation",
})
MANAGEMENT_EVIDENCE_PREFIXES = (
    "evidence/", "claims/", "reviews/", "consistency/", "decisions/", "ai/",
)
MANAGEMENT_EVIDENCE_NAMES = frozenset({
    "STATUS.JSON", "V51_STATUS.JSON", "RUN_META.JSON", "BUDGET.JSON",
    "VALIDATION_REPORT.JSON", "FREEZE_VERIFICATION.JSON", "DECISION_LOG.JSONL",
    "EXPERIMENT_LOG.CSV", "METRICS.CSV", "SOURCE_LEDGER.CSV",
})
AI_EXPORT_RECEIPT_RELATIVE = "ai/AI_EXPORT_RECEIPT.json"
AI_DISCLOSURE_PDF_RECEIPT_RELATIVE = "ai/AI_DISCLOSURE_PDF_RECEIPT.json"
AI_DISCLOSURE_PDF_RELATIVE = "deliverables/AI工具使用详情.pdf"
NO_SOURCE_ATTESTATION_RELATIVE = "sources/NO_SOURCE_ATTESTATION.json"
PAPER_COMPILE_RECEIPT_RELATIVE = "deliverables/PAPER_COMPILE_RECEIPT.json"
DEFAULT_PAPER_PDF_RELATIVE = "deliverables/FINAL_MANUSCRIPT.pdf"
PLACEHOLDER_REVIEWERS = PLACEHOLDER_HUMAN_IDENTITIES
AI_EVENT_FIELDS = frozenset({
    "event_id", "timestamp", "tool", "model", "stage", "purpose",
    "prompt_or_task_summary", "input_artifacts", "output_artifacts",
    "adopted", "human_modified", "human_verified", "verified_by", "notes",
})

_TRUSTED_AI_INIT_SENTINEL = {
    "event_id": "AI_000",
    "tool": "CUMCM Adaptive Research Harness",
    "model": "none",
    "stage": "initialization",
    "purpose": "Create deterministic project state and audit files",
    "prompt_or_task_summary": "Local initialization only; no model call",
    "input_artifacts": [],
    "output_artifacts": ["RUN_META.json", "BUDGET.json", "STATUS.json", "INPUT_MANIFEST.json"],
    "adopted": False,
    "human_modified": False,
    "human_verified": False,
    "verified_by": "",
    "notes": "This event is tooling provenance, not an LLM invocation.",
}


def _parse_datetime(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        die(f"时间格式无效：{value}。请使用 ISO 8601，例如 2026-09-13T20:00:00+08:00。")
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _input_manifest(base: Path) -> dict[str, Any]:
    records = []
    if base.exists():
        for path in iter_files(base, exclude_names={".gitkeep"}):
            stat = path.stat()
            records.append(
                {
                    "path": path.relative_to(base).as_posix(),
                    "size": stat.st_size,
                    "sha256": sha256_file(path),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                    .astimezone()
                    .isoformat(timespec="seconds"),
                }
            )
    return {"generated_at": now_iso(), "source_root": relative_to_root(base), "files": records}


def _initial_status(config: dict[str, Any], run_id: str) -> dict[str, Any]:
    gates = {}
    for gate_id, gate in config.get("gates", {}).items():
        gates[gate_id] = {
            "name": gate.get("name", gate_id),
            "status": "pending",
            "approved_at": None,
            "approved_by": None,
            "note": "",
            "validation": None,
        }
    return {
        "version": config.get("version", "5.1.0"),
        "run_id": run_id,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "current_gate": next(iter(gates), "G0"),
        "gates": gates,
        "frozen": False,
        "frozen_at": None,
        "freeze_manifest": None,
    }


def cmd_init(args: argparse.Namespace) -> int:
    config = load_config()
    template_root = ROOT / "templates" / "run"
    if not template_root.exists():
        die(f"缺少运行模板：{template_root}")
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    default_id = f"{slugify(args.contest)}-{slugify(args.problem)}-{stamp}"
    run_id = slugify(args.run_id or default_id)
    run = RUNS_ROOT / run_id
    if run.exists():
        die(f"run_id 已存在：{run_id}")

    shutil.copytree(template_root, run)
    for relative in [
        "research/briefs",
        "research/ideas",
        "research/modules",
        "research/diversity",
        "research/issues",
        "modes/baseline",
        "modes/reference-verifier-faithful",
        "modes/reference-verifier-faithful/candidates",
        "modes/reference-verifier-lite",
        "modes/reference-verifier-lite/candidates",
        "modes/fresh-agent-relay",
        "modes/external-pro-review",
        "modes/direct-api",
        "modes/api-burst",
        "modes/red-team",
        "modes/paper-writing",
        "modes/paper-audit",
        "experiments/runs",
        "deliverables/packages",
        "interpretation",
        "sensitivity",
        "claims",
        "consistency",
        "reviews/blind",
    ]:
        (run / relative).mkdir(parents=True, exist_ok=True)

    started = datetime.now().astimezone()
    if args.deadline:
        deadline = _parse_datetime(args.deadline)
        total_hours = max(0.0, (deadline - started).total_seconds() / 3600)
    else:
        total_hours = float(args.time_budget_hours)
        deadline = started + timedelta(hours=total_hours)

    budget = read_json(ROOT / "config" / "default_budget.json", expect=dict)
    budget = copy.deepcopy(budget)
    budget["version"] = config.get("version", "5.1.0")
    budget["created_at"] = now_iso()
    budget["updated_at"] = now_iso()
    budget["time"]["total_hours"] = round(total_hours, 3)
    budget["time"]["started_at"] = started.isoformat(timespec="seconds")
    budget["time"]["deadline"] = deadline.isoformat(timespec="seconds")
    budget["human_review"]["max_hours"] = float(args.human_review_hours)
    max_agents = int(args.max_agent_threads)
    budget.setdefault("agent_runtime", {})["primary"] = args.runtime
    budget["agent_runtime"]["max_concurrent_agents"] = max_agents
    budget.setdefault("codex", {})["max_concurrent_threads"] = max_agents
    budget.setdefault("claude_code", {})["max_concurrent_agents"] = max_agents
    budget["exploration"]["max_batches"] = int(args.max_exploration_batches)

    # v3 compatibility: --api-budget-usd remains an alias for OpenAI API budget.
    openai_budget = float(args.openai_api_budget_usd if args.openai_api_budget_usd is not None else args.api_budget_usd)
    provider_limits = {
        "openai": openai_budget,
        "anthropic": float(args.anthropic_api_budget_usd),
        "deepseek": float(args.deepseek_api_budget_usd),
    }
    providers = budget.setdefault("api_providers", {})
    for provider_name, maximum in provider_limits.items():
        item = providers.setdefault(provider_name, {})
        item["enabled"] = bool(maximum > 0)
        item["max_usd"] = maximum
        item.setdefault("used_usd", 0.0)
        item.setdefault("call_count", 0)

    # Legacy OpenAI burst adapter still reads openai_api; keep it synchronized.
    budget["openai_api"]["enabled"] = bool(openai_budget > 0)
    budget["openai_api"]["max_usd"] = openai_budget

    meta = {
        "version": config.get("version", "5.1.0"),
        "run_id": run_id,
        "contest": args.contest,
        "problem": args.problem,
        "phase": args.phase,
        "runtime": args.runtime,
        "created_at": now_iso(),
        "project_root": str(ROOT),
        "run_path": relative_to_root(run),
        "input_policy": "inputs/ is treated as read-only; hashes are captured in INPUT_MANIFEST.json",
        "notes": args.note or "",
    }
    write_json(run / "RUN_META.json", meta)
    write_json(run / "BUDGET.json", budget)
    write_json(run / "STATUS.json", _initial_status(config, run_id))

    full_input_manifest = _input_manifest(INPUTS_ROOT)
    data_manifest = _input_manifest(INPUTS_ROOT / "data")
    write_json(run / "INPUT_MANIFEST.json", full_input_manifest)
    write_json(run / "data" / "DATA_MANIFEST.json", data_manifest)

    # Keep two small registries deterministic and easy for tools/agents to edit.
    write_json(run / "ideas" / "IDEA_REGISTRY.json", {"next_id": 1, "ideas": []})
    write_json(run / "research" / "MODULE_REGISTRY.json", {"next_id": 1, "modules": []})

    replace_placeholders(
        run,
        {
            "RUN_ID": run_id,
            "CONTEST": args.contest,
            "PROBLEM": args.problem,
            "PHASE": args.phase,
            "CREATED_AT": now_iso(),
        },
    )
    # V51_STATUS contains a live stage-registry hash and therefore cannot be a
    # static template.  Create the complete canonical state only after the run
    # exists; existing/legacy state is never silently migrated by this call.
    from v51_orchestrator import ensure_state as ensure_v51_state

    ensure_v51_state(run)
    write_active(run_id)

    # Register the harness itself as an AI-enabled process without fabricating model use.
    append_jsonl(
        run / "ai" / "AI_USAGE_LOG.jsonl",
        {
            "event_id": "AI_000",
            "timestamp": now_iso(),
            "tool": "CUMCM Adaptive Research Harness",
            "model": "none",
            "stage": "initialization",
            "purpose": "Create deterministic project state and audit files",
            "prompt_or_task_summary": "Local initialization only; no model call",
            "input_artifacts": [],
            "output_artifacts": ["RUN_META.json", "BUDGET.json", "STATUS.json", "INPUT_MANIFEST.json"],
            "adopted": False,
            "human_modified": False,
            "human_verified": False,
            "verified_by": "",
            "notes": "This event is tooling provenance, not an LLM invocation.",
        },
    )

    print(f"已创建 run：{run_id}")
    print(f"路径：{run}")
    print(f"阶段：{args.phase}")
    print(f"Runtime：{args.runtime}")
    print(f"截止时间：{deadline.isoformat(timespec='seconds')}")
    if args.runtime == "codex":
        print("下一步：重新打开 Codex 项目，使用 $cumcm-adaptive-research，从 G0 Intake 开始。")
    elif args.runtime == "claude-code":
        print("下一步：在项目根目录运行 `claude`，让 Claude Code 使用项目 Skill cumcm-adaptive-research，从 G0 Intake 开始。")
    elif args.runtime == "claude-code-deepseek":
        print("下一步：先设置 DEEPSEEK_API_KEY，再运行 scripts/start_claude_deepseek.ps1（Windows）或 .sh（macOS/Linux）。")
    else:
        print("下一步：用 tools/provider_call.py 运行受限角色调用，或切换到 Codex/Claude Code 完成需要工作区工具的阶段。")
    return 0


def _gate_order(config: dict[str, Any]) -> list[str]:
    return list(config.get("gates", {}).keys())


def _gate_index(config: dict[str, Any], gate_id: str) -> int:
    order = _gate_order(config)
    try:
        return order.index(gate_id)
    except ValueError:
        die(f"未知 Gate：{gate_id}。可选：{', '.join(order)}")


def _file_check(run: Path, relative: str, strict: bool, tokens: list[str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    raw_text = str(relative)
    raw_path = Path(raw_text)
    normalized = raw_path.as_posix()
    portable_text = raw_text.replace("\\", "/")
    if (
        raw_text != raw_text.strip()
        or raw_path.is_absolute()
        or bool(raw_path.drive)
        or normalized in {"", ".", "/"}
        or ".." in raw_path.parts
        or portable_text != normalized
    ):
        errors.append(f"required path 不是 canonical run-relative 文件路径：{relative}")
        return errors, warnings
    path = run / raw_path
    if path.is_symlink():
        errors.append(f"required path 不得是符号链接：{relative}")
        return errors, warnings
    if not path.exists():
        errors.append(f"缺少：{relative}")
        return errors, warnings
    if not path.is_file():
        errors.append(f"required path 不是 regular file：{relative}")
        return errors, warnings
    resolved = path.resolve()
    try:
        resolved_relative = resolved.relative_to(run.resolve()).as_posix()
    except ValueError:
        errors.append(f"required path 离开当前 run：{relative}")
        return errors, warnings
    if resolved_relative != normalized:
        errors.append(f"required path 使用非 canonical 路径或目录链接：{relative}")
        return errors, warnings
    if path.suffix.lower() not in {".jsonl"} and path.stat().st_size == 0:
        errors.append(f"空文件：{relative}")
    placeholders = contains_placeholder(path, tokens)
    if placeholders:
        message = f"仍含占位符 {sorted(set(placeholders))}：{relative}"
        (errors if strict else warnings).append(message)
    if path.suffix.lower() == ".json":
        try:
            json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            errors.append(f"JSON 无效：{relative}: {exc}")
    return errors, warnings


def _resolve_run_artifact(run: Path, value: str) -> tuple[Path | None, str | None]:
    """Resolve legacy project-relative or run-relative evidence, but never leave this run."""
    text = str(value or "").strip()
    if not text:
        return None, "empty evidence path"
    if text.startswith(("http://", "https://")):
        return None, "external URLs belong in SOURCE_LEDGER.csv, not evidence_path"
    raw = Path(text).expanduser()
    candidates = [raw] if raw.is_absolute() else [run / raw, ROOT / raw]
    run_root = run.resolve()
    outside_existing: list[str] = []
    for candidate_value in candidates:
        candidate = candidate_value.resolve()
        try:
            candidate.relative_to(run_root)
        except ValueError:
            if candidate.exists():
                outside_existing.append(str(candidate))
            continue
        if candidate.exists() and candidate.is_file():
            return candidate, None
    if outside_existing or raw.is_absolute() or ".." in raw.parts:
        target = outside_existing[0] if outside_existing else str(raw)
        return None, f"evidence path leaves the current run: {target}"
    return None, f"evidence artifact does not exist in the current run: {text}"


def _validate_v51_for_gate(run: Path, gate: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    stage_ids = gate.get("v51_requires", [])
    if not stage_ids:
        return [], [], []
    if not isinstance(stage_ids, list):
        return [], ["gate v51_requires must be a list"], []
    details: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []
    try:
        from v51_orchestrator import (
            approval_projection_record,
            approved_stage_record_errors,
            ensure_state,
            validate_stage,
        )
    except (Exception, SystemExit) as exc:
        return [], [f"cannot load v5.1 validators: {exc}"], []
    state: dict[str, Any] | None = None
    if not (run / "V51_STATUS.json").is_file():
        errors.append("V51_STATUS.json missing; required v5.1 stages must be explicitly approved")
    else:
        try:
            state = ensure_state(run, persist=False)
        except (Exception, SystemExit) as exc:
            errors.append(f"V51_STATUS.json invalid or stale: {exc}")
    for stage_id in stage_ids:
        stage = str(stage_id)
        stage_item = (
            state.get("stages", {}).get(stage, {})
            if isinstance(state, dict) and isinstance(state.get("stages"), dict)
            else {}
        )
        review_id = None
        if stage == "independent_review" and isinstance(stage_item, dict):
            stored_result = stage_item.get("last_result") or {}
            if isinstance(stored_result, dict):
                review_id = stored_result.get("review_id")
        try:
            result, _ = validate_stage(run, stage, review_id=review_id)
        except (Exception, SystemExit) as exc:
            errors.append(f"v5.1 {stage}: validator exception: {exc}")
            continue
        detail: dict[str, Any] = {"stage": stage, "result": result}
        if state is not None and isinstance(stage_item, dict):
            detail["approval_projection"] = approval_projection_record(
                state,
                stage_ids=[stage],
                label=f"V51_STATUS.json#{stage}-approval-projection",
            )
        details.append(detail)
        errors.extend(f"v5.1 {stage}: {item}" for item in result.get("errors", []))
        warnings.extend(f"v5.1 {stage}: {item}" for item in result.get("warnings", []))
        if state is None or not isinstance(stage_item, dict):
            errors.append(f"v5.1 {stage}: approval state is missing")
        else:
            errors.extend(
                f"v5.1 approval: {item}"
                for item in approved_stage_record_errors(
                    stage,
                    stage_item,
                    current_result=result,
                )
            )
    return details, errors, warnings


def _is_valid_review_time(value: Any) -> bool:
    return is_timezone_aware_iso(value)


def _require_named_human(value: Any, *, action: str) -> str:
    actor = str(value or "").strip()
    if not is_named_human_identity(actor):
        die(
            f"{action} 必须记录具名队员，不能使用 "
            "TEAM/UNKNOWN/TODO/AUTO/CODEX 等占位或机器身份"
        )
    return actor


def _known_support_identities(
    run: Path, *, extra: Iterable[Any] = ()
) -> list[str]:
    """Collect explicit participant/reviewer identities for support scanning.

    Only identity-bearing contract fields are read.  Bibliographic authors and
    ordinary prose are intentionally excluded so third-party attribution does
    not become a false participant token.
    """
    identities: dict[str, str] = {}

    def add(value: Any) -> None:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        if is_named_human_identity(text):
            identities.setdefault(text.casefold(), text)

    for value in extra:
        add(value)

    for state_name, collection_name in (
        ("STATUS.json", "gates"),
        ("V51_STATUS.json", "stages"),
    ):
        path = run / state_name
        if not path.is_file():
            continue
        try:
            state = read_json(path, expect=dict)
        except (Exception, SystemExit):
            continue
        collection = state.get(collection_name, {})
        if isinstance(collection, dict):
            for item in collection.values():
                if isinstance(item, dict):
                    add(item.get("approved_by"))

    json_identity_fields = {
        "selection/FOCUS_SET.json": ("selected_by",),
        AI_EXPORT_RECEIPT_RELATIVE: ("reviewed_by",),
        AI_DISCLOSURE_PDF_RECEIPT_RELATIVE: ("attested_by",),
        NO_SOURCE_ATTESTATION_RELATIVE: ("attested_by",),
    }
    configured_support_receipt = load_config().get("compliance", {}).get(
        "support_archive_receipt_path"
    )
    if (
        isinstance(configured_support_receipt, str)
        and configured_support_receipt
        and configured_support_receipt == configured_support_receipt.strip()
        and "\\" not in configured_support_receipt
        and not Path(configured_support_receipt).is_absolute()
        and all(part not in {"", ".", ".."} for part in configured_support_receipt.split("/"))
    ):
        json_identity_fields[configured_support_receipt] = ("generated_by",)
    for relative, fields in json_identity_fields.items():
        path = run / relative
        if not path.is_file():
            continue
        try:
            payload = read_json(path, expect=dict)
        except (Exception, SystemExit):
            continue
        for field in fields:
            add(payload.get(field))

    for relative, field in (
        ("evidence/CLAIM_EVIDENCE_LEDGER.csv", "verified_by"),
        ("sources/SOURCE_LEDGER.csv", "verified_by"),
    ):
        path = run / relative
        if not path.is_file():
            continue
        try:
            rows = read_csv(path)
        except (Exception, SystemExit):
            continue
        for row in rows:
            add(row.get(field))

    for relative, field in (
        ("decisions/DECISION_LOG.jsonl", "by"),
        ("ai/AI_USAGE_LOG.jsonl", "verified_by"),
    ):
        path = run / relative
        if not path.is_file():
            continue
        try:
            rows = read_jsonl(path)
        except (Exception, SystemExit):
            continue
        for row in rows:
            if isinstance(row, dict):
                add(row.get(field))

    checklist = _final_checklist_audit(run)
    for reviewer in checklist.get("reviewers", []):
        add(reviewer)

    return sorted(identities.values(), key=str.casefold)


def _ai_usage_event_errors(events: list[Any]) -> list[str]:
    errors: list[str] = []
    seen_ids: dict[str, int] = {}
    required_text = (
        "event_id", "timestamp", "tool", "model", "stage", "purpose",
        "prompt_or_task_summary",
    )
    for line_number, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            errors.append(f"AI_USAGE_LOG.jsonl:{line_number} event must be a JSON object")
            continue
        unknown = sorted(set(event) - AI_EVENT_FIELDS)
        missing = sorted(AI_EVENT_FIELDS - set(event))
        if missing or unknown:
            errors.append(
                f"AI_USAGE_LOG.jsonl:{line_number} non-canonical fields "
                f"(missing={missing}, extra={unknown})"
            )
        for field in required_text:
            if not isinstance(event.get(field), str) or not event.get(field, "").strip():
                errors.append(f"AI_USAGE_LOG.jsonl:{line_number} {field} must be non-empty text")
        if event.get("timestamp") and not _is_valid_review_time(event.get("timestamp")):
            errors.append(f"AI_USAGE_LOG.jsonl:{line_number} timestamp must include a timezone")
        event_id = str(event.get("event_id", "")).strip()
        if event_id:
            if event_id in seen_ids:
                errors.append(
                    f"AI_USAGE_LOG.jsonl duplicate event_id {event_id!r} "
                    f"at lines {seen_ids[event_id]} and {line_number}"
                )
            else:
                seen_ids[event_id] = line_number
        for field in ("input_artifacts", "output_artifacts"):
            value = event.get(field)
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item.strip() for item in (value or [])
            ):
                errors.append(
                    f"AI_USAGE_LOG.jsonl:{line_number} {field} must be a string array"
                )
        for field in ("adopted", "human_modified", "human_verified"):
            if not isinstance(event.get(field), bool):
                errors.append(f"AI_USAGE_LOG.jsonl:{line_number} {field} must be boolean")
        verified_by = event.get("verified_by")
        if not isinstance(verified_by, str):
            errors.append(f"AI_USAGE_LOG.jsonl:{line_number} verified_by must be text")
        elif event.get("human_verified") is True and not is_named_human_identity(verified_by):
            errors.append(
                f"AI_USAGE_LOG.jsonl:{line_number} human_verified event lacks a named verifier"
            )
        if event.get("adopted") is True and event.get("human_verified") is not True:
            errors.append(
                f"AI_USAGE_LOG.jsonl:{line_number} adopted event must be human_verified"
            )
        if not isinstance(event.get("notes"), str):
            errors.append(f"AI_USAGE_LOG.jsonl:{line_number} notes must be text")
    return errors


def _disclosure_ai_event_count(events: list[Any]) -> int:
    def is_trusted_init_sentinel(event: Any) -> bool:
        return (
            isinstance(event, dict)
            and set(event) == AI_EVENT_FIELDS
            and all(event.get(field) == value for field, value in _TRUSTED_AI_INIT_SENTINEL.items())
            and is_timezone_aware_iso(event.get("timestamp"))
        )

    return sum(1 for event in events if not is_trusted_init_sentinel(event))


EGRESS_TERMINAL_STATUSES = frozenset({
    "succeeded", "failed-or-unknown", "post-response-error",
})
EGRESS_BUDGET_MODES = frozenset({
    "direct-api", "api-burst", "reference-verifier-faithful",
    "claude-code-deepseek",
})


def _egress_attempt_audit(run: Path) -> dict[str, Any]:
    """Reconcile every harness-managed AI attempt with logs and reservations.

    The audit is deliberately bidirectional: an attempt needs exactly one AI
    event (and, for an external backend, exactly one budget reservation), while
    every reservation/event emitted by these adapters must point back to one
    canonical attempt record.  Failed or uncertain calls are valid terminal
    records and their conservative reservation is never refunded.
    """
    errors: list[str] = []
    warnings: list[str] = []
    input_paths: set[str] = set()
    run_root = run.resolve()
    log_path = run / "ai" / "AI_USAGE_LOG.jsonl"
    budget_path = run / "BUDGET.json"
    if log_path.is_file():
        input_paths.add("ai/AI_USAGE_LOG.jsonl")
    if budget_path.is_file():
        input_paths.add("BUDGET.json")

    try:
        events = read_jsonl(log_path)
    except (Exception, SystemExit) as exc:
        events = []
        errors.append(f"AI_USAGE_LOG.jsonl cannot be parsed for egress audit: {exc}")
    events_by_id: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("event_id", "")).strip()
        if event_id:
            events_by_id.setdefault(event_id, []).append(event)

    try:
        budget = read_json(budget_path, expect=dict)
    except (Exception, SystemExit) as exc:
        budget = {}
        errors.append(f"BUDGET.json cannot be parsed for egress audit: {exc}")

    def canonical_file(path: Path, *, label: str) -> str | None:
        try:
            lexical = path.relative_to(run).as_posix()
            resolved = path.resolve(strict=True)
            canonical = resolved.relative_to(run_root).as_posix()
        except (OSError, ValueError) as exc:
            errors.append(f"{label} leaves the current run or cannot be resolved: {exc}")
            return None
        if lexical != canonical or path.is_symlink():
            errors.append(f"{label} is a symlink/non-canonical path: {lexical}")
            return None
        if not resolved.is_file():
            errors.append(f"{label} is not a regular file: {lexical}")
            return None
        input_paths.add(canonical)
        return canonical

    def bind_attempt_tree(directory: Path, *, label: str) -> None:
        try:
            lexical_dir = directory.relative_to(run).as_posix()
            canonical_dir = directory.resolve(strict=True).relative_to(run_root).as_posix()
        except (OSError, ValueError) as exc:
            errors.append(f"{label} directory leaves the current run: {exc}")
            return
        if directory.is_symlink() or lexical_dir != canonical_dir:
            errors.append(f"{label} directory is a symlink/non-canonical path: {lexical_dir}")
            return
        for item in sorted(directory.rglob("*")):
            if item.is_symlink():
                errors.append(
                    f"{label} output contains a symlink: {item.relative_to(run).as_posix()}"
                )
                continue
            if item.is_file():
                canonical_file(item, label=f"{label} output")

    specs = (
        ("direct-api", "REQUEST.json", "id", False),
        ("api-burst", "REQUEST.json", "burst_id", False),
        ("reference-verifier-faithful", "EXECUTION.json", "request_id", True),
        ("claude-code-deepseek", "EXECUTION.json", "session_id", False),
    )
    attempts: list[dict[str, Any]] = []
    attempts_by_id: dict[str, list[dict[str, Any]]] = {}
    for mode, filename, id_field, recursive in specs:
        base = run / "modes" / mode
        if not base.exists():
            continue
        if base.is_symlink() or not base.is_dir():
            errors.append(f"modes/{mode} must be a canonical directory, not a symlink")
            continue
        candidates = base.rglob(filename) if recursive else base.glob(f"*/{filename}")
        for path in sorted(candidates):
            relative = canonical_file(path, label=f"{mode} attempt record")
            if relative is None:
                continue
            if not recursive and len(path.relative_to(base).parts) != 2:
                errors.append(f"{mode} attempt record has a non-canonical depth: {relative}")
                continue
            bind_attempt_tree(path.parent, label=f"{mode} attempt")
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                errors.append(f"{relative} is invalid JSON: {exc}")
                continue
            if not isinstance(payload, dict):
                errors.append(f"{relative} must contain a JSON object")
                continue
            request_id = str(payload.get(id_field, "")).strip()
            provider = str(payload.get("provider", "")).strip().lower()
            status = str(payload.get("status", "")).strip()
            if not request_id:
                errors.append(f"{relative} lacks {id_field}")
                continue
            if payload.get("run_id") != run.name:
                errors.append(f"{relative} run_id does not match the current run")
            if not provider:
                errors.append(f"{relative} lacks provider")
            if mode in {"direct-api", "api-burst", "claude-code-deepseek"}:
                if path.parent.name != request_id:
                    errors.append(
                        f"{relative} id does not match its canonical directory name"
                    )
                external = True
            else:
                backend_scope = str(payload.get("backend_scope", "")).strip()
                if backend_scope not in {"external", "loopback-local"}:
                    errors.append(f"{relative} has invalid backend_scope")
                external = backend_scope == "external"
            attempt = {
                "id": request_id,
                "mode": mode,
                "path": relative,
                "directory": path.parent,
                "provider": provider,
                "status": status,
                "external": external,
                "payload": payload,
            }
            attempts.append(attempt)
            attempts_by_id.setdefault(request_id, []).append(attempt)

    for request_id, matches in sorted(attempts_by_id.items()):
        if len(matches) != 1:
            errors.append(
                f"egress request id {request_id!r} is not unique across attempt records"
            )

    reservations_by_id: dict[str, list[dict[str, Any]]] = {}
    providers = budget.get("api_providers", {}) if isinstance(budget, dict) else {}
    if not isinstance(providers, dict):
        errors.append("BUDGET.json api_providers must be an object")
        providers = {}
    for provider_name, item in providers.items():
        label = f"BUDGET.json api_providers.{provider_name}"
        if not isinstance(item, dict):
            errors.append(f"{label} must be an object")
            continue
        try:
            used = float(item.get("used_usd", 0.0))
            maximum = float(item.get("max_usd", 0.0))
        except (TypeError, ValueError):
            errors.append(f"{label} used_usd/max_usd must be numeric")
            continue
        if not math.isfinite(used) or used < 0 or not math.isfinite(maximum) or maximum < 0:
            errors.append(f"{label} used_usd/max_usd must be finite and non-negative")
        if used > maximum + 1e-9:
            errors.append(f"{label} used_usd exceeds max_usd")
        records = item.get("usage_records", [])
        if not isinstance(records, list):
            errors.append(f"{label}.usage_records must be an array")
            continue
        call_count = item.get("call_count", 0)
        if isinstance(call_count, bool) or not isinstance(call_count, int) or call_count < 0:
            errors.append(f"{label}.call_count must be a non-negative integer")
        elif call_count != len(records):
            errors.append(f"{label}.call_count does not equal usage_records length")
        reserved_total = 0.0
        seen_record_ids: set[str] = set()
        for index, record in enumerate(records, start=1):
            record_label = f"{label}.usage_records[{index}]"
            if not isinstance(record, dict):
                errors.append(f"{record_label} must be an object")
                continue
            record_id = str(record.get("id", "")).strip()
            if not record_id:
                errors.append(f"{record_label} lacks id")
            elif record_id in seen_record_ids:
                errors.append(f"{label} has duplicate reservation id {record_id!r}")
            else:
                seen_record_ids.add(record_id)
            if "reserved_usd" in record:
                try:
                    reserved = float(record["reserved_usd"])
                except (TypeError, ValueError):
                    reserved = float("nan")
                if not math.isfinite(reserved) or reserved <= 0:
                    errors.append(f"{record_label}.reserved_usd must be finite and > 0")
                else:
                    reserved_total += reserved
            mode = str(record.get("mode", "")).strip()
            central_reservation = "reserved_usd" in record
            if central_reservation and mode not in EGRESS_BUDGET_MODES:
                errors.append(f"{record_label}.mode is missing or not an audited egress mode")
            if (mode in EGRESS_BUDGET_MODES or central_reservation) and record_id:
                reservations_by_id.setdefault(record_id, []).append({
                    "provider_bucket": str(provider_name),
                    "record": record,
                    "label": record_label,
                })
                if record.get("provider") != provider_name:
                    errors.append(f"{record_label}.provider does not match its budget bucket")
        if used + 1e-9 < reserved_total:
            errors.append(
                f"{label}.used_usd is lower than the sum of conservative reservations"
            )

    canonical_openai = providers.get("openai")
    legacy_openai = budget.get("openai_api") if isinstance(budget, dict) else None
    if isinstance(canonical_openai, dict):
        if not isinstance(legacy_openai, dict):
            errors.append("BUDGET.json openai_api mirror is missing or invalid")
        else:
            defaults = {"call_count": 0, "usage_records": []}
            for field in (
                "enabled", "max_usd", "used_usd", "default_model",
                "call_count", "usage_records",
            ):
                canonical_value = canonical_openai.get(field, defaults.get(field))
                legacy_value = legacy_openai.get(field, defaults.get(field))
                if canonical_value != legacy_value:
                    errors.append(f"BUDGET.json openai_api mirror differs at {field}")

    for attempt in attempts:
        request_id = attempt["id"]
        status = attempt["status"]
        payload = attempt["payload"]
        path = attempt["path"]
        event_matches = events_by_id.get(request_id, [])
        reservation_matches = reservations_by_id.get(request_id, [])
        if status == "prepared":
            if payload.get("network_request_attempted") is not False:
                errors.append(f"{path} prepared preview claims an attempted request")
            if payload.get("network_request_sent") is not False:
                errors.append(f"{path} prepared preview has invalid network_request_sent")
            if event_matches:
                errors.append(f"{path} prepared preview unexpectedly has an AI usage event")
            if reservation_matches:
                errors.append(f"{path} prepared preview unexpectedly has a reservation")
            continue
        if status not in EGRESS_TERMINAL_STATUSES:
            errors.append(f"{path} has non-terminal egress status {status!r}")
        if not _is_valid_review_time(payload.get("completed_at")):
            errors.append(f"{path} terminal attempt lacks a timezone-aware completed_at")
        if len(event_matches) != 1:
            errors.append(
                f"{path} must have exactly one AI usage event; found {len(event_matches)}"
            )
        else:
            outputs = event_matches[0].get("output_artifacts", [])
            if not isinstance(outputs, list) or path not in outputs:
                errors.append(f"AI usage event {request_id!r} does not bind {path}")
        if attempt["external"]:
            if payload.get("network_request_attempted") is not True:
                errors.append(f"{path} external attempt lacks network_request_attempted=true")
            if payload.get("network_request_sent") not in {True, None}:
                errors.append(f"{path} external attempt has impossible network_request_sent")
            try:
                request_reserved = float(payload.get("budget_reserved_usd"))
            except (TypeError, ValueError):
                request_reserved = float("nan")
            if not math.isfinite(request_reserved) or request_reserved <= 0:
                errors.append(f"{path} lacks a finite positive budget_reserved_usd")
            if len(reservation_matches) != 1:
                errors.append(
                    f"{path} must have exactly one budget reservation; found "
                    f"{len(reservation_matches)}"
                )
            else:
                reservation = reservation_matches[0]
                record = reservation["record"]
                try:
                    record_reserved = float(record.get("reserved_usd"))
                except (TypeError, ValueError):
                    record_reserved = float("nan")
                if reservation["provider_bucket"] != attempt["provider"]:
                    errors.append(f"{path} provider does not match its reservation bucket")
                if record.get("provider") != attempt["provider"]:
                    errors.append(f"{path} provider does not match reservation.provider")
                if record.get("status") != status:
                    errors.append(f"{path} status does not match reservation.status")
                if not (
                    math.isfinite(request_reserved)
                    and math.isfinite(record_reserved)
                    and abs(request_reserved - record_reserved) <= 1e-9
                ):
                    errors.append(f"{path} reserved amount does not match BUDGET.json")
        else:
            if payload.get("network_request_attempted") is not False:
                errors.append(f"{path} loopback-local attempt claims external network use")
            try:
                local_reserved = float(payload.get("budget_reserved_usd", 0.0))
            except (TypeError, ValueError):
                local_reserved = float("nan")
            if not math.isfinite(local_reserved) or local_reserved != 0.0:
                errors.append(f"{path} loopback-local attempt must reserve exactly 0 USD")
            if reservation_matches:
                errors.append(f"{path} loopback-local attempt unexpectedly has a reservation")
        if status in {"failed-or-unknown", "post-response-error"}:
            error_path = attempt["directory"] / "ERROR.json"
            if not error_path.is_file():
                errors.append(f"{path} terminal failure lacks ERROR.json")
            else:
                canonical_file(error_path, label=f"{path} error record")
                try:
                    error_payload = json.loads(error_path.read_text(encoding="utf-8-sig"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    errors.append(f"{path} ERROR.json is invalid: {exc}")
                else:
                    if not isinstance(error_payload, dict) or error_payload.get("status") != status:
                        errors.append(f"{path} ERROR.json status does not match the attempt")
        elif status == "succeeded" and attempt["mode"] != "claude-code-deepseek":
            result_path = attempt["directory"] / "RESULT.json"
            if not result_path.is_file():
                errors.append(f"{path} successful attempt lacks RESULT.json")
            else:
                canonical_file(result_path, label=f"{path} result")

    for request_id, reservations in sorted(reservations_by_id.items()):
        matching = [
            item for item in attempts_by_id.get(request_id, []) if item["external"]
        ]
        if len(reservations) != 1 or len(matching) != 1:
            errors.append(
                f"budget reservation {request_id!r} is orphaned or ambiguous "
                f"(reservations={len(reservations)}, attempts={len(matching)})"
            )

    known_attempt_paths = {item["path"]: item for item in attempts}
    for event_id, event_matches in events_by_id.items():
        for event in event_matches:
            outputs = event.get("output_artifacts", [])
            if not isinstance(outputs, list):
                continue
            referenced_attempts = [
                value for value in outputs
                if isinstance(value, str)
                and (
                    value.startswith("modes/direct-api/")
                    or value.startswith("modes/api-burst/")
                    or value.startswith("modes/reference-verifier-faithful/")
                    or value.startswith("modes/claude-code-deepseek/")
                )
                and value.endswith(("/REQUEST.json", "/EXECUTION.json"))
            ]
            tool = str(event.get("tool", "")).strip()
            stage = str(event.get("stage", "")).strip()
            declared_egress = bool(referenced_attempts) or (
                tool.startswith("Direct API role runner (")
                or tool == "OpenAI Responses API Multi-agent beta"
                or tool == "LLM-as-a-Verifier (pinned official package)"
                or tool == "Claude Code with DeepSeek Anthropic-compatible backend"
                or stage in {
                    "api-burst", "reference-verifier-faithful",
                    "claude-code-deepseek",
                }
            )
            matching_attempts = attempts_by_id.get(event_id, [])
            binds_matching_path = any(
                value in known_attempt_paths and known_attempt_paths[value]["id"] == event_id
                for value in referenced_attempts
            )
            if declared_egress and (
                len(matching_attempts) != 1 or not binds_matching_path
            ):
                errors.append(
                    f"AI usage event {event_id!r} references no matching egress attempt"
                )

    return {
        "errors": errors,
        "warnings": warnings,
        "attempt_count": len(attempts),
        "external_attempt_count": sum(
            bool(item["external"]) and item["status"] != "prepared"
            for item in attempts
        ),
        "reservation_count": sum(len(items) for items in reservations_by_id.values()),
        "input_paths": sorted(input_paths),
    }


def _ai_export_audit(run: Path) -> dict[str, Any]:
    errors: list[str] = []
    log_path = run / "ai" / "AI_USAGE_LOG.jsonl"
    export_path = run / "ai" / "AI_TOOL_USAGE_DETAILS.md"
    receipt_path = run / AI_EXPORT_RECEIPT_RELATIVE
    try:
        events = read_jsonl(log_path)
    except (Exception, SystemExit) as exc:
        events = []
        errors.append(f"AI_USAGE_LOG.jsonl 无法解析：{exc}")
    errors.extend(_ai_usage_event_errors(events))
    if not receipt_path.is_file():
        errors.append("缺少 AI_EXPORT_RECEIPT.json；请重新运行 export-ai 并记录人工复核人")
        return {
            "errors": errors,
            "event_count": len(events),
            "disclosure_event_count": _disclosure_ai_event_count(events),
            "input_paths": [],
        }
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        receipt = {}
        errors.append(f"AI_EXPORT_RECEIPT.json 无效：{exc}")
    if not isinstance(receipt, dict):
        receipt = {}
        errors.append("AI_EXPORT_RECEIPT.json 顶层必须是对象")
    checks = {
        "schema_version": (
            not isinstance(receipt.get("schema_version"), bool)
            and receipt.get("schema_version") == 1
        ),
        "run_id": receipt.get("run_id") == run.name,
        "source_path": receipt.get("source_path") == "ai/AI_USAGE_LOG.jsonl",
        "export_path": receipt.get("export_path") == "ai/AI_TOOL_USAGE_DETAILS.md",
        "human_reviewed": receipt.get("human_reviewed") is True,
        "reviewed_by": (
            bool(str(receipt.get("reviewed_by", "")).strip())
            and str(receipt.get("reviewed_by", "")).strip().upper()
            not in PLACEHOLDER_REVIEWERS
        ),
        "reviewed_at": _is_valid_review_time(receipt.get("reviewed_at")),
        "generated_at": _is_valid_review_time(receipt.get("generated_at")),
    }
    for field, passed in checks.items():
        if not passed:
            errors.append(f"AI_EXPORT_RECEIPT.json {field} 无效")
    event_count = receipt.get("event_count")
    if isinstance(event_count, bool) or not isinstance(event_count, int) or event_count != len(events):
        errors.append("AI_EXPORT_RECEIPT.json event_count 与当前 AI_USAGE_LOG 不一致")
    if not log_path.is_file() or receipt.get("source_sha256") != sha256_file(log_path):
        errors.append("AI_EXPORT_RECEIPT.json source_sha256 与当前 AI_USAGE_LOG 不一致")
    if not export_path.is_file() or receipt.get("export_sha256") != sha256_file(export_path):
        errors.append("AI_EXPORT_RECEIPT.json export_sha256 与当前 AI_TOOL_USAGE_DETAILS 不一致")
    return {
        "errors": errors,
        "event_count": len(events),
        "disclosure_event_count": _disclosure_ai_event_count(events),
        "input_paths": [
            "ai/AI_USAGE_LOG.jsonl",
            "ai/AI_TOOL_USAGE_DETAILS.md",
            AI_EXPORT_RECEIPT_RELATIVE,
        ],
    }


def _ai_disclosure_pdf_audit(run: Path) -> dict[str, Any]:
    """Validate the typed PDF disclosure, required when AI events are logged."""
    errors: list[str] = []
    source_path = run / "ai" / "AI_TOOL_USAGE_DETAILS.md"
    pdf_path = run / AI_DISCLOSURE_PDF_RELATIVE
    receipt_path = run / AI_DISCLOSURE_PDF_RECEIPT_RELATIVE
    input_paths: list[str] = []
    export_audit = _ai_export_audit(run)
    pdf_required = int(export_audit.get("disclosure_event_count", 0) or 0) > 0
    if not pdf_path.is_file() and not receipt_path.is_file():
        return {
            "errors": (
                [
                    "AI usage events exist, so the reviewed disclosure PDF and its "
                    "typed receipt are required"
                ]
                if pdf_required
                else []
            ),
            "input_paths": [],
            "pdf_path": None,
            "required": pdf_required,
        }
    if not pdf_path.is_file():
        return {
            "errors": [
                f"{AI_DISCLOSURE_PDF_RECEIPT_RELATIVE} exists without "
                f"{AI_DISCLOSURE_PDF_RELATIVE}"
            ],
            "input_paths": [AI_DISCLOSURE_PDF_RECEIPT_RELATIVE],
            "pdf_path": None,
            "required": pdf_required,
        }
    input_paths.append(AI_DISCLOSURE_PDF_RELATIVE)
    if not receipt_path.is_file():
        return {
            "errors": [
                f"{AI_DISCLOSURE_PDF_RELATIVE} exists without "
                f"{AI_DISCLOSURE_PDF_RECEIPT_RELATIVE}; run attest-ai-disclosure-pdf"
            ],
            "input_paths": input_paths,
            "pdf_path": AI_DISCLOSURE_PDF_RELATIVE,
            "required": pdf_required,
        }
    input_paths.extend([
        AI_DISCLOSURE_PDF_RECEIPT_RELATIVE,
        "ai/AI_TOOL_USAGE_DETAILS.md",
        AI_EXPORT_RECEIPT_RELATIVE,
        "ai/AI_USAGE_LOG.jsonl",
    ])
    run_root = run.resolve()
    for candidate, relative in (
        (source_path, "ai/AI_TOOL_USAGE_DETAILS.md"),
        (pdf_path, AI_DISCLOSURE_PDF_RELATIVE),
        (receipt_path, AI_DISCLOSURE_PDF_RECEIPT_RELATIVE),
    ):
        resolved = candidate.resolve()
        try:
            canonical = resolved.relative_to(run_root).as_posix()
        except ValueError:
            errors.append(f"AI disclosure artifact leaves the current run: {relative}")
            continue
        if canonical != relative:
            errors.append(f"AI disclosure artifact is a symlink/non-canonical path: {relative}")
    errors.extend(
        "AI disclosure PDF envelope: " + item
        for item in pdf_envelope_errors(pdf_path)
    )
    errors.extend(
        f"AI export prerequisite: {item}" for item in export_audit.get("errors", [])
    )
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        receipt = {}
        errors.append(f"AI_DISCLOSURE_PDF_RECEIPT.json is invalid: {exc}")
    if not isinstance(receipt, dict):
        receipt = {}
        errors.append("AI_DISCLOSURE_PDF_RECEIPT.json must contain an object")
    try:
        from schema_validate import validate_json_file

        errors.extend(
            "AI disclosure PDF receipt schema: " + item
            for item in validate_json_file(
                receipt_path,
                ROOT / "schemas" / "ai_disclosure_pdf_receipt.schema.json",
            )
        )
    except (Exception, SystemExit) as exc:
        errors.append(f"AI disclosure PDF receipt schema validator exception: {exc}")
    checks = {
        "schema_version": (
            not isinstance(receipt.get("schema_version"), bool)
            and receipt.get("schema_version") == 1
        ),
        "run_id": receipt.get("run_id") == run.name,
        "attested_at": _is_valid_review_time(receipt.get("attested_at")),
        "attested_by": (
            bool(str(receipt.get("attested_by", "")).strip())
            and str(receipt.get("attested_by", "")).strip().upper()
            not in PLACEHOLDER_REVIEWERS
        ),
        "human_visual_reviewed": receipt.get("human_visual_reviewed") is True,
        "source_path": receipt.get("source_path") == "ai/AI_TOOL_USAGE_DETAILS.md",
        "pdf_path": receipt.get("pdf_path") == AI_DISCLOSURE_PDF_RELATIVE,
    }
    for field, passed in checks.items():
        if not passed:
            errors.append(f"AI_DISCLOSURE_PDF_RECEIPT.json {field} is invalid")
    if (
        not source_path.is_file()
        or source_path.stat().st_size <= 0
        or receipt.get("source_sha256") != sha256_file(source_path)
    ):
        errors.append("AI disclosure PDF receipt does not bind the current Markdown export")
    if (
        pdf_path.stat().st_size <= 0
        or receipt.get("pdf_size") != pdf_path.stat().st_size
        or receipt.get("pdf_sha256") != sha256_file(pdf_path)
    ):
        errors.append("AI disclosure PDF receipt does not bind the current PDF bytes")
    return {
        "errors": errors,
        "input_paths": list(dict.fromkeys(input_paths)),
        "pdf_path": AI_DISCLOSURE_PDF_RELATIVE,
        "required": pdf_required,
    }


def _source_compliance_audit(run: Path) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    ledger_path = run / "sources" / "SOURCE_LEDGER.csv"
    try:
        rows = read_csv(ledger_path, expected_fields=SOURCE_FIELDS)
    except (Exception, SystemExit) as exc:
        rows = []
        errors.append(f"SOURCE_LEDGER.csv 无法按当前 schema 读取：{exc}")
    input_paths = ["sources/SOURCE_LEDGER.csv"]
    attestation_path = run / NO_SOURCE_ATTESTATION_RELATIVE
    if rows:
        seen_ids: dict[str, int] = {}
        required_fields = (
            "source_id", "title", "author_or_org", "year", "url", "source_type",
            "claim_supported", "accessed_at", "verified_by",
        )
        for row_number, row in enumerate(rows, start=2):
            missing = [field for field in required_fields if not str(row.get(field, "")).strip()]
            if missing:
                errors.append(
                    f"SOURCE_LEDGER.csv:{row_number} 缺少字段：" + ", ".join(missing)
                )
            source_id = str(row.get("source_id", "")).strip()
            if source_id:
                if source_id in seen_ids:
                    errors.append(
                        f"SOURCE_LEDGER.csv source_id 重复：{source_id} "
                        f"(rows {seen_ids[source_id]} and {row_number})"
                    )
                else:
                    seen_ids[source_id] = row_number
            url = str(row.get("url", "")).strip().lower()
            if url and not url.startswith(("https://", "http://")):
                errors.append(f"SOURCE_LEDGER.csv:{row_number} url 必须为 http(s) URL")
            if row.get("accessed_at") and not _is_valid_review_time(row.get("accessed_at")):
                errors.append(f"SOURCE_LEDGER.csv:{row_number} accessed_at 必须为含时区 ISO 8601")
            verified_by = str(row.get("verified_by", "")).strip()
            if verified_by.upper() in PLACEHOLDER_REVIEWERS:
                errors.append(
                    f"SOURCE_LEDGER.csv:{row_number} verified_by 使用了占位身份"
                )
        if attestation_path.is_file():
            warnings.append("SOURCE_LEDGER 非空；现有 no-source attestation 仅作为历史记录，不再生效")
    else:
        if not attestation_path.is_file():
            errors.append(
                "SOURCE_LEDGER.csv 为空时必须提供 sources/NO_SOURCE_ATTESTATION.json"
            )
        else:
            input_paths.append(NO_SOURCE_ATTESTATION_RELATIVE)
            try:
                attestation = json.loads(attestation_path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                attestation = {}
                errors.append(f"NO_SOURCE_ATTESTATION.json 无效：{exc}")
            if not isinstance(attestation, dict):
                attestation = {}
                errors.append("NO_SOURCE_ATTESTATION.json 顶层必须是对象")
            if (
                isinstance(attestation.get("schema_version"), bool)
                or attestation.get("schema_version") != 1
            ):
                errors.append("NO_SOURCE_ATTESTATION.json schema_version 必须为 1")
            if attestation.get("run_id") != run.name:
                errors.append("NO_SOURCE_ATTESTATION.json run_id 与当前 run 不一致")
            if (
                isinstance(attestation.get("source_count"), bool)
                or attestation.get("source_count") != 0
            ):
                errors.append("NO_SOURCE_ATTESTATION.json source_count 必须为 0")
            if attestation.get("source_ledger_path") != "sources/SOURCE_LEDGER.csv":
                errors.append("NO_SOURCE_ATTESTATION.json source_ledger_path 无效")
            attested_by = str(attestation.get("attested_by", "")).strip()
            if not attested_by:
                errors.append("NO_SOURCE_ATTESTATION.json 缺少 attested_by")
            elif attested_by.upper() in PLACEHOLDER_REVIEWERS:
                errors.append("NO_SOURCE_ATTESTATION.json attested_by 使用了占位身份")
            if not _is_valid_review_time(attestation.get("attested_at")):
                errors.append("NO_SOURCE_ATTESTATION.json attested_at 必须为含时区 ISO 8601")
            if len(str(attestation.get("reason", "")).strip()) < 10:
                errors.append("NO_SOURCE_ATTESTATION.json reason 过短，必须具体说明为何无外部来源")
            if not ledger_path.is_file() or attestation.get("source_ledger_sha256") != sha256_file(ledger_path):
                errors.append("NO_SOURCE_ATTESTATION.json 未绑定当前 SOURCE_LEDGER.csv")
    return {
        "errors": errors,
        "warnings": warnings,
        "source_count": len(rows),
        "input_paths": input_paths,
    }


def _final_checklist_audit(run: Path) -> dict[str, Any]:
    path = run / "deliverables" / "FINAL_CHECKLIST.md"
    errors: list[str] = []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        return {"errors": [f"FINAL_CHECKLIST.md 无法读取：{exc}"], "checkbox_count": 0}
    checkboxes = list(re.finditer(
        r"(?im)^\s*(?:[-*+]|\d+[.)])\s+\[([ xX])\]\s+(.+?)\s*$",
        text,
    ))
    if not checkboxes:
        errors.append("FINAL_CHECKLIST.md 至少必须包含一个 Markdown checkbox")
    unchecked = [match.group(2).strip() for match in checkboxes if match.group(1).lower() != "x"]
    if unchecked:
        errors.append("FINAL_CHECKLIST.md 仍有未勾选项目：" + "; ".join(unchecked))
    required_ids = [
        str(item).strip()
        for item in load_config().get("compliance", {}).get(
            "final_checklist_item_ids", []
        )
        if str(item).strip()
    ]
    found_ids: list[str] = []
    for match in checkboxes:
        item_match = re.match(r"^\[([a-z0-9][a-z0-9-]*)\]\s+", match.group(2).strip())
        if not item_match:
            errors.append(
                "FINAL_CHECKLIST.md checkbox 缺少 canonical [item-id]："
                + match.group(2).strip()
            )
            continue
        found_ids.append(item_match.group(1))
    duplicate_ids = sorted({item for item in found_ids if found_ids.count(item) > 1})
    if duplicate_ids:
        errors.append("FINAL_CHECKLIST.md item-id 重复：" + ", ".join(duplicate_ids))
    if not required_ids:
        errors.append("harness_config.json 未定义 canonical final checklist item IDs")
    elif set(found_ids) != set(required_ids) or len(found_ids) != len(required_ids):
        errors.append(
            "FINAL_CHECKLIST.md canonical item-id 集合不完整："
            f"required={required_ids}, found={found_ids}"
        )
    reviewer_pattern = re.compile(
        r"(?im)^\s*(?:[-*]\s*)?(?:reviewed\s+by|reviewer|approved\s+by|"
        r"复核人|审核人|核验人|负责人|by)\s*[:：]\s*(\S.*?)\s*$"
    )
    reviewers = [match.group(1).strip() for match in reviewer_pattern.finditer(text)]
    if not reviewers:
        errors.append("FINAL_CHECKLIST.md 缺少 Reviewer/Reviewed by/审核人 行")
    elif any(not is_named_human_identity(reviewer) for reviewer in reviewers):
        errors.append("FINAL_CHECKLIST.md Reviewer 必须是具名队员，不能使用占位或机器身份")
    return {
        "errors": errors,
        "checkbox_count": len(checkboxes),
        "item_ids": found_ids,
        "reviewers": reviewers,
    }


def _official_support_policy() -> tuple[dict[str, Any], list[str]]:
    from support_package import (
        DEFAULT_MAX_BYTES,
        DEFAULT_OUTPUT_RELATIVE,
        DEFAULT_RECEIPT_RELATIVE,
    )

    configured = load_config().get("compliance", {})
    errors: list[str] = []
    required = configured.get("support_archive_required", True)
    if not isinstance(required, bool):
        errors.append("support_archive_required 必须是布尔值")
        required = True
    output = configured.get("support_archive_path", DEFAULT_OUTPUT_RELATIVE)
    if not isinstance(output, str) or not output.strip():
        errors.append("support_archive_path 必须是非空字符串")
        output = DEFAULT_OUTPUT_RELATIVE
    receipt = configured.get(
        "support_archive_receipt_path", DEFAULT_RECEIPT_RELATIVE
    )
    if not isinstance(receipt, str) or not receipt.strip():
        errors.append("support_archive_receipt_path 必须是非空字符串")
        receipt = DEFAULT_RECEIPT_RELATIVE
    max_bytes = configured.get("support_archive_max_bytes", DEFAULT_MAX_BYTES)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        errors.append("support_archive_max_bytes 必须是正整数")
        max_bytes = DEFAULT_MAX_BYTES
    elif max_bytes > DEFAULT_MAX_BYTES:
        errors.append(
            "support_archive_max_bytes 不得超过官方上限 "
            f"{DEFAULT_MAX_BYTES} bytes"
        )
        max_bytes = DEFAULT_MAX_BYTES
    return {
        "required": required,
        "output": output,
        "receipt": receipt,
        "max_bytes": max_bytes,
    }, errors


def _official_support_archive_audit(
    run: Path, *, ai_required: bool
) -> dict[str, Any]:
    """Audit the official anonymous support ZIP, separately from package."""
    from support_package import audit_support_archive

    policy, policy_errors = _official_support_policy()
    output = str(policy["output"])
    receipt = str(policy["receipt"])

    def configured_file_exists(relative: str) -> bool:
        return bool(
            relative
            and relative == relative.strip()
            and "\\" not in relative
            and not Path(relative).is_absolute()
            and all(part not in {"", ".", ".."} for part in relative.split("/"))
            and (run / relative).is_file()
        )

    output_exists = configured_file_exists(output)
    receipt_exists = configured_file_exists(receipt)
    effective_required = bool(policy["required"] or ai_required)
    if not effective_required and not output_exists and not receipt_exists:
        return {
            "passed": True,
            "errors": policy_errors,
            "warnings": [],
            "required": False,
            "ai_required": ai_required,
            "archive_path": output,
            "receipt_path": receipt,
            "archive_size": None,
            "archive_sha256": "",
            "max_bytes": policy["max_bytes"],
            "within_limit": None,
            "members": [],
            "files": [],
            "known_identity_count": len(_known_support_identities(run)),
            "input_paths": [],
        }
    identities = _known_support_identities(run)
    try:
        result = audit_support_archive(
            run,
            output,
            receipt,
            ai_required=ai_required,
            known_identities=identities,
            max_bytes=int(policy["max_bytes"]),
        )
    except (Exception, SystemExit) as exc:
        result = {
            "passed": False,
            "errors": [f"validator exception: {type(exc).__name__}: {exc}"],
            "warnings": [],
            "archive_path": output,
            "receipt_path": receipt,
            "archive_size": None,
            "archive_sha256": "",
            "members": [],
            "files": [],
        }
    result = dict(result)
    result["errors"] = [*policy_errors, *list(result.get("errors", []))]
    result["warnings"] = list(result.get("warnings", []))
    result["passed"] = not result["errors"]
    result["required"] = effective_required
    result["ai_required"] = ai_required
    result["max_bytes"] = policy["max_bytes"]
    size = result.get("archive_size")
    result["within_limit"] = (
        isinstance(size, int)
        and not isinstance(size, bool)
        and 0 < size <= int(policy["max_bytes"])
    ) if size is not None else None
    result["known_identity_count"] = len(identities)
    input_paths: list[str] = []
    for relative in (output, receipt):
        if configured_file_exists(relative):
            input_paths.append(relative)
    for record in result.get("files", []):
        if isinstance(record, dict) and isinstance(record.get("source_path"), str):
            input_paths.append(record["source_path"])
    result["input_paths"] = list(dict.fromkeys(input_paths))
    return result


def _paper_compile_audit(run: Path) -> dict[str, Any]:
    """Bind an optional final PDF to the canonical manuscript and compile receipt."""
    errors: list[str] = []
    receipt_path = run / PAPER_COMPILE_RECEIPT_RELATIVE
    default_output = run / DEFAULT_PAPER_PDF_RELATIVE
    source_path = run / "paper" / "FINAL_MANUSCRIPT.md"
    input_paths: list[str] = []
    # A broad submission snapshot (``deliverables/**``) is not provenance for
    # an arbitrary PDF.  Enumerate every candidate up front so an unreceipted
    # attachment cannot become package-eligible merely by existing before the
    # submission approval.  The compile receipt may bind exactly one PDF.
    discovered_pdfs: list[str] = []
    run_root = run.resolve()
    for relative_root in ("paper", "deliverables"):
        base = run / relative_root
        if not base.is_dir():
            continue
        for candidate in base.rglob("*"):
            if not candidate.is_file() or candidate.suffix.lower() != ".pdf":
                continue
            lexical = candidate.relative_to(run).as_posix()
            if lexical.startswith("deliverables/packages/"):
                continue
            if lexical == AI_DISCLOSURE_PDF_RELATIVE:
                # This typed PDF has its own source/hash/human-review receipt.
                # It is audited separately and cannot serve as the manuscript.
                continue
            resolved = candidate.resolve()
            try:
                canonical = resolved.relative_to(run_root).as_posix()
            except ValueError:
                errors.append(f"PDF candidate leaves the current run: {lexical}")
                continue
            if canonical != lexical:
                errors.append(f"PDF candidate is a symlink/non-canonical path: {lexical}")
                continue
            discovered_pdfs.append(canonical)
    discovered_pdfs = sorted(set(discovered_pdfs))
    input_paths.extend(discovered_pdfs)
    if not receipt_path.is_file() and not discovered_pdfs:
        return {"errors": errors, "input_paths": input_paths, "output_path": None}
    if not receipt_path.is_file():
        return {
            "errors": [
                *errors,
                "PDF artifacts exist without a canonical compile receipt: "
                + ", ".join(discovered_pdfs),
            ],
            "input_paths": list(dict.fromkeys(input_paths)),
            "output_path": None,
        }
    input_paths.append(PAPER_COMPILE_RECEIPT_RELATIVE)
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            "errors": [f"PAPER_COMPILE_RECEIPT.json is invalid: {exc}"],
            "input_paths": input_paths,
            "output_path": None,
        }
    if not isinstance(receipt, dict):
        return {
            "errors": ["PAPER_COMPILE_RECEIPT.json must contain an object"],
            "input_paths": input_paths,
            "output_path": None,
        }
    try:
        from schema_validate import validate_json_file

        errors.extend(
            "paper compile receipt schema: " + item
            for item in validate_json_file(
                receipt_path,
                ROOT / "schemas" / "paper_compile_receipt.schema.json",
            )
        )
    except (Exception, SystemExit) as exc:
        errors.append(f"paper compile receipt schema validator exception: {exc}")
    if receipt.get("run_id") != run.name:
        errors.append("PAPER_COMPILE_RECEIPT.json run_id differs from the current run")
    if not _is_valid_review_time(receipt.get("generated_at")):
        errors.append("PAPER_COMPILE_RECEIPT.json generated_at is not timezone-aware ISO-8601")
    if receipt.get("source_path") != "paper/FINAL_MANUSCRIPT.md":
        errors.append("PAPER_COMPILE_RECEIPT.json source_path is not canonical")
    input_paths.append("paper/FINAL_MANUSCRIPT.md")
    if (
        not source_path.is_file()
        or source_path.stat().st_size <= 0
        or receipt.get("source_sha256") != sha256_file(source_path)
    ):
        errors.append("PAPER_COMPILE_RECEIPT.json does not bind the current FINAL_MANUSCRIPT.md")

    output_text = str(receipt.get("output_path", "")).strip().replace("\\", "/")
    output_path, output_error = _resolve_run_artifact(run, output_text)
    output_relative: str | None = None
    if output_error or output_path is None:
        errors.append(
            "PAPER_COMPILE_RECEIPT.json output_path does not resolve to a current run artifact: "
            + (output_error or output_text or "<empty>")
        )
    else:
        output_relative = output_path.relative_to(run.resolve()).as_posix()
        input_paths.append(output_relative)
        if output_relative != output_text:
            errors.append("PAPER_COMPILE_RECEIPT.json output_path is non-canonical")
        if not output_relative.startswith("deliverables/") or output_path.suffix.lower() != ".pdf":
            errors.append("PAPER_COMPILE_RECEIPT.json output_path must be a deliverables/*.pdf file")
        if output_relative.startswith("deliverables/packages/"):
            errors.append("PAPER_COMPILE_RECEIPT.json output_path cannot use deliverables/packages/")
        if output_relative == AI_DISCLOSURE_PDF_RELATIVE:
            errors.append("PAPER_COMPILE_RECEIPT.json cannot reuse the reserved AI disclosure PDF")
        errors.extend(
            "compiled PDF envelope: " + item
            for item in pdf_envelope_errors(output_path)
        )
        if output_path.stat().st_size <= 0 or receipt.get("output_sha256") != sha256_file(output_path):
            errors.append("PAPER_COMPILE_RECEIPT.json does not bind the current PDF bytes")
    if default_output.is_file() and output_relative != DEFAULT_PAPER_PDF_RELATIVE:
        input_paths.append(DEFAULT_PAPER_PDF_RELATIVE)
        errors.append(
            f"{DEFAULT_PAPER_PDF_RELATIVE} exists but is not the output bound by "
            "PAPER_COMPILE_RECEIPT.json"
        )
    extra_pdfs = sorted(set(discovered_pdfs) - ({output_relative} if output_relative else set()))
    if extra_pdfs:
        errors.append(
            "PDF artifacts are not the unique output bound by PAPER_COMPILE_RECEIPT.json: "
            + ", ".join(extra_pdfs)
        )
    return {
        "errors": errors,
        "input_paths": list(dict.fromkeys(input_paths)),
        "output_path": output_relative,
    }


def _evidence_artifact_errors(run: Path) -> list[str]:
    ledger_path = run / "evidence" / "CLAIM_EVIDENCE_LEDGER.csv"
    if not ledger_path.exists():
        return []
    errors: list[str] = []
    for row in read_csv(ledger_path):
        evidence_path = str(row.get("evidence_path", "")).strip()
        if not evidence_path:
            continue
        artifact, artifact_error = _resolve_run_artifact(run, evidence_path)
        if artifact_error:
            errors.append(
                f"证据 {row.get('evidence_id') or row.get('claim_id') or 'unknown'} 无法冻结：{artifact_error}"
            )
            continue
        relative = artifact.relative_to(run.resolve()).as_posix() if artifact else ""
        relative_lower = relative.lower()
        name = Path(relative).name.upper()
        if (
            any(relative_lower.startswith(prefix) for prefix in MANAGEMENT_EVIDENCE_PREFIXES)
            or name in MANAGEMENT_EVIDENCE_NAMES
            or "MANIFEST" in name
            or name.endswith("_LEDGER.CSV")
            or name.endswith("_REGISTER.CSV")
            or name.endswith("_REGISTER.JSON")
        ):
            errors.append(
                f"证据 {row.get('evidence_id') or row.get('claim_id') or 'unknown'} "
                f"指向管理/登记文件，不能作为论文事实证据：{relative}"
            )
    return errors


def _claim_ledger_integrity_errors(rows: list[dict[str, str]]) -> list[str]:
    errors: list[str] = []
    evidence_rows: dict[str, int] = {}
    claim_metadata: dict[str, tuple[str, str, bool, str]] = {}
    for row_number, row in enumerate(rows, start=2):
        evidence_id = str(row.get("evidence_id", "")).strip()
        if evidence_id:
            if evidence_id in evidence_rows:
                errors.append(
                    f"CLAIM_EVIDENCE_LEDGER.csv evidence_id 重复：{evidence_id} "
                    f"(rows {evidence_rows[evidence_id]} and {row_number})"
                )
            else:
                evidence_rows[evidence_id] = row_number
        claim_id = str(row.get("claim_id", "")).strip()
        if not claim_id:
            continue
        metadata = (
            str(row.get("claim_text", "")).strip(),
            str(row.get("claim_type", "")).strip(),
            is_truthy(row.get("is_core")),
            str(row.get("paper_location", "")).strip(),
        )
        previous = claim_metadata.setdefault(claim_id, metadata)
        if previous != metadata:
            errors.append(
                f"CLAIM_EVIDENCE_LEDGER.csv:{row_number} {claim_id} 的 "
                "claim_text/claim_type/is_core/paper_location 与同 claim_id 其他行不一致"
            )
    return errors


def _is_empirical_claim(rows: list[dict[str, str]]) -> bool:
    claim_types = {str(row.get("claim_type", "")).strip().lower() for row in rows}
    evidence_types = {str(row.get("evidence_type", "")).strip().lower() for row in rows}
    return bool(claim_types & EMPIRICAL_CLAIM_TYPES or evidence_types & EMPIRICAL_EVIDENCE_TYPES)


def _experiment_gate_audit(
    run: Path,
    *,
    only_experiment_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Validate G3 against runner receipts, not merely pre-created CSV files."""
    from run_experiments import (
        ENVIRONMENT_POLICY,
        INHERITED_ENV_NAMES,
        LOG_FIELDS,
        METRIC_FIELDS,
        RUNNER_RECORD_NAMES,
        SAFE_COMPONENT_RE,
        _command_list,
        _is_sensitive_env_name,
        _manifest_environment,
        _resolve_run_file,
        _run_relative_pattern,
        _safe_cwd,
    )

    errors: list[str] = []
    valid_receipts: dict[Path, dict[str, Any]] = {}
    receipt_artifact_paths: set[Path] = set()
    manifest_source_paths: set[Path] = set()
    log_path = run / "experiments" / "EXPERIMENT_LOG.csv"
    metric_path = run / "experiments" / "METRICS.csv"
    selected_ids = (
        None
        if only_experiment_ids is None
        else {str(item).strip() for item in only_experiment_ids if str(item).strip()}
    )
    if only_experiment_ids is not None and not selected_ids:
        errors.append("targeted experiment audit requires at least one experiment_id")
    if selected_ids is not None:
        invalid_ids = sorted(
            item for item in selected_ids if SAFE_COMPONENT_RE.fullmatch(item) is None
        )
        if invalid_ids:
            errors.append("targeted experiment_id 不符合 component 契约：" + ", ".join(invalid_ids))
    try:
        log_rows = (
            read_csv(log_path, expected_fields=LOG_FIELDS) if log_path.is_file() else []
        )
    except SystemExit as exc:
        errors.append(f"EXPERIMENT_LOG.csv 表头无效：{exc}")
        log_rows = []
    selected_log_rows = [
        row for row in log_rows
        if selected_ids is None or str(row.get("experiment_id", "")).strip() in selected_ids
    ]
    passed_rows = [
        (index, row) for index, row in enumerate(log_rows, start=2)
        if (selected_ids is None or str(row.get("experiment_id", "")).strip() in selected_ids)
        if str(row.get("status", "")).strip().lower() == "passed"
    ]
    if not passed_rows:
        errors.append("EXPERIMENT_LOG.csv 没有 status=passed 的实验记录")
    if selected_ids is not None:
        passed_ids = {
            str(row.get("experiment_id", "")).strip() for _, row in passed_rows
        }
        for missing_id in sorted(selected_ids - passed_ids):
            errors.append(f"targeted experiment 没有 status=passed 的日志记录：{missing_id}")

    def normalize_seed(value: Any) -> str:
        return "none" if value is None else str(value).strip()

    seen_passed_outputs: dict[str, int] = {}
    for row_number, row in passed_rows:
        experiment_id = str(row.get("experiment_id", "")).strip()
        output_path = str(row.get("output_path", "")).strip()
        if not experiment_id:
            errors.append(f"EXPERIMENT_LOG.csv:{row_number} passed 记录缺少 experiment_id")
            continue
        artifact, artifact_error = _resolve_run_artifact(run, output_path)
        if artifact_error or artifact is None:
            errors.append(
                f"EXPERIMENT_LOG.csv:{row_number} {experiment_id} 的 output_path 无效："
                f"{artifact_error or 'unresolved path'}"
            )
            continue
        if output_path in seen_passed_outputs:
            errors.append(
                f"EXPERIMENT_LOG.csv:{row_number} 重复 passed output_path {output_path!r}；"
                f"首次出现在 row {seen_passed_outputs[output_path]}"
            )
            continue
        seen_passed_outputs[output_path] = row_number
        if artifact.name.lower() != "run.json":
            errors.append(
                f"EXPERIMENT_LOG.csv:{row_number} {experiment_id} 的 output_path 必须指向 runner run.json"
            )
            continue
        try:
            receipt = json.loads(artifact.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(f"实验收据无法读取 {artifact.relative_to(run)}：{exc}")
            continue
        if not isinstance(receipt, dict):
            errors.append(f"实验收据不是 JSON 对象：{artifact.relative_to(run)}")
            continue
        receipt_id = str(receipt.get("experiment_id", "")).strip()
        receipt_status = str(receipt.get("status", "")).strip().lower()
        if receipt_id != experiment_id:
            errors.append(
                f"实验收据 ID 不一致：日志={experiment_id!r}，收据={receipt_id!r} ({artifact.relative_to(run)})"
            )
            continue
        if receipt_status != "passed":
            errors.append(
                f"实验收据状态不是 passed：{experiment_id}={receipt_status or '<empty>'} ({artifact.relative_to(run)})"
            )
            continue
        log_seed = normalize_seed(row.get("seed"))
        receipt_seed = normalize_seed(receipt.get("seed"))
        if receipt_seed != log_seed:
            errors.append(
                f"实验收据 seed 不一致：{experiment_id} 日志={log_seed!r}，收据={receipt_seed!r}"
            )
            continue
        log_module = str(row.get("module_id", "")).strip()
        receipt_module = str(receipt.get("module_id", "")).strip()
        if receipt_module != log_module:
            errors.append(
                f"实验收据 module_id 不一致：{experiment_id} 日志={log_module!r}，收据={receipt_module!r}"
            )
            continue
        receipt_contract_errors: list[str] = []
        attempt_id = str(receipt.get("attempt_id", "")).strip()
        if (
            SAFE_COMPONENT_RE.fullmatch(experiment_id) is None
            or SAFE_COMPONENT_RE.fullmatch(log_seed) is None
            or re.fullmatch(r"attempt_[0-9]+_[0-9a-f]{8}", attempt_id) is None
        ):
            receipt_contract_errors.append("experiment/seed/attempt_id 不符合 runner component 契约")
            errors.extend(
                f"实验收据来源契约失败 {experiment_id} ({artifact.relative_to(run)})：{message}"
                for message in receipt_contract_errors
            )
            continue
        expected_record_dir = (
            run
            / "experiments"
            / "runs"
            / experiment_id
            / f"seed_{log_seed}"
            / "_attempts"
            / attempt_id
        )
        expected_receipt_path = expected_record_dir / "run.json"
        expected_receipt_relative = expected_receipt_path.relative_to(run).as_posix()
        if output_path.replace("\\", "/") != expected_receipt_relative:
            receipt_contract_errors.append("EXPERIMENT_LOG.output_path 不是 canonical runner attempt 路径")
        if artifact != expected_receipt_path.resolve():
            receipt_contract_errors.append("run.json 实际路径与 experiment/seed/attempt_id 不一致")

        request_path = expected_record_dir / "request.json"
        request: dict[str, Any] = {}
        if not request_path.is_file():
            receipt_contract_errors.append("同一 attempt 缺少 request.json")
        else:
            receipt_artifact_paths.add(request_path.resolve())
            try:
                request_payload = json.loads(request_path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                request_payload = {}
                receipt_contract_errors.append(f"request.json 无法读取：{exc}")
            if not isinstance(request_payload, dict):
                receipt_contract_errors.append("request.json 顶层不是对象")
            else:
                request = request_payload
                required_request_fields = {
                    "experiment_id", "attempt_id", "description", "module_id", "seed",
                    "command", "executed_command", "executable", "cwd", "timeout_sec",
                    "artifact_output_dir", "record_dir", "expected_artifacts",
                    "environment_override_keys", "inherited_environment_keys",
                    "runner_environment_keys", "environment_policy", "manifest_path",
                    "manifest_sha256", "pre_artifacts", "missing_before_run", "created_at",
                    "no_shell", "cwd_confined_to_run",
                    "relative_executable_confined_to_run", "filesystem_isolated",
                    "network_isolated",
                }
                missing_request_fields = sorted(required_request_fields - set(request))
                if missing_request_fields:
                    receipt_contract_errors.append(
                        "request.json 缺少 runner 字段：" + ", ".join(missing_request_fields)
                    )
                for key, value in request.items():
                    if receipt.get(key) != value:
                        receipt_contract_errors.append(f"run.json 未原样继承 request.json 字段：{key}")

        expected_record_relative = expected_record_dir.relative_to(run).as_posix()
        expected_output_relative = (
            run / "experiments" / "runs" / experiment_id / f"seed_{log_seed}"
        ).relative_to(run).as_posix()
        if receipt.get("record_dir") != expected_record_relative:
            receipt_contract_errors.append("record_dir 与 canonical attempt 目录不一致")
        if receipt.get("artifact_output_dir") != expected_output_relative:
            receipt_contract_errors.append("artifact_output_dir 与 canonical experiment/seed 目录不一致")
        if receipt.get("no_shell") is not True:
            receipt_contract_errors.append("no_shell 不是 true")
        command = receipt.get("command")
        command_valid = isinstance(command, list) and bool(command) and not any(
            not isinstance(item, str) or not item for item in command
        )
        if not command_valid:
            receipt_contract_errors.append("command 不是非空 argv 字符串数组")
        else:
            try:
                logged_command = json.loads(str(row.get("command", "")))
            except json.JSONDecodeError:
                logged_command = None
            if logged_command != command:
                receipt_contract_errors.append("EXPERIMENT_LOG.command 与 run.json argv 不一致")
        timeout_sec = receipt.get("timeout_sec")
        if isinstance(timeout_sec, bool) or not isinstance(timeout_sec, int) or timeout_sec <= 0:
            receipt_contract_errors.append("timeout_sec 不是正整数")
        environment_keys = receipt.get("environment_override_keys")
        if (
            not isinstance(environment_keys, list)
            or any(not isinstance(item, str) for item in environment_keys)
            or environment_keys != sorted(set(environment_keys))
        ):
            receipt_contract_errors.append("environment_override_keys 不是排序去重字符串数组")
            environment_keys = []
        inherited_keys = receipt.get("inherited_environment_keys")
        inherited_allowed = set(INHERITED_ENV_NAMES)
        if (
            not isinstance(inherited_keys, list)
            or any(not isinstance(item, str) for item in inherited_keys)
            or inherited_keys != sorted(set(inherited_keys))
            or any(item != item.upper() for item in inherited_keys)
        ):
            receipt_contract_errors.append(
                "inherited_environment_keys 不是排序去重的大写字符串数组"
            )
            inherited_keys = []
        else:
            invalid_inherited = [
                item
                for item in inherited_keys
                if (
                    item not in inherited_allowed
                    and not (
                        re.fullmatch(r"LC_[A-Z0-9_]+", item)
                        and not _is_sensitive_env_name(item)
                    )
                )
            ]
            if invalid_inherited:
                receipt_contract_errors.append(
                    "inherited_environment_keys 含非 allowlist 项："
                    + ", ".join(invalid_inherited)
                )
        if set(str(item).upper() for item in environment_keys) & set(inherited_keys):
            receipt_contract_errors.append(
                "environment_override_keys 与 inherited_environment_keys 冲突"
            )
        runner_keys = receipt.get("runner_environment_keys")
        expected_runner_keys = {
            "CUMCM_RUN_DIR",
            "CUMCM_EXPERIMENT_ID",
            "CUMCM_EXPERIMENT_OUTPUT_DIR",
            "CUMCM_EXPERIMENT_RECORD_DIR",
            "CUMCM_ATTEMPT_ID",
        }
        if receipt.get("seed") is not None:
            expected_runner_keys.add("CUMCM_SEED")
        if (
            not isinstance(runner_keys, list)
            or runner_keys != sorted(expected_runner_keys)
        ):
            receipt_contract_errors.append(
                "runner_environment_keys 与 canonical runner metadata 不一致"
            )
        if receipt.get("environment_policy") != ENVIRONMENT_POLICY:
            receipt_contract_errors.append(
                f"environment_policy 必须为 {ENVIRONMENT_POLICY}"
            )
        for field, expected_value in (
            ("cwd_confined_to_run", True),
            ("relative_executable_confined_to_run", True),
            ("filesystem_isolated", False),
            ("network_isolated", False),
        ):
            if receipt.get(field) is not expected_value:
                receipt_contract_errors.append(f"{field} 必须精确为 {expected_value}")
        cwd_text = str(receipt.get("cwd", "")).strip()
        cwd_path = Path(cwd_text).expanduser()
        cwd_resolved: Path | None = None
        if not cwd_text or not cwd_path.is_absolute():
            receipt_contract_errors.append("cwd 不是 runner 记录的绝对路径")
        else:
            cwd_resolved = cwd_path.resolve()
            try:
                cwd_resolved.relative_to(run.resolve())
            except ValueError:
                receipt_contract_errors.append("cwd 离开当前 run")
            if not cwd_resolved.is_dir() or cwd_path != cwd_resolved:
                receipt_contract_errors.append("cwd 不存在或使用了非 canonical/symlink 路径")

        executed_command = receipt.get("executed_command")
        executable = receipt.get("executable")
        executable_path: Path | None = None
        if (
            not isinstance(executed_command, list)
            or not executed_command
            or any(not isinstance(item, str) or not item for item in executed_command)
        ):
            receipt_contract_errors.append("executed_command 不是非空 argv 字符串数组")
        elif not command_valid or len(executed_command) != len(command):
            receipt_contract_errors.append("executed_command 长度与 command 不一致")
        else:
            if executed_command[1:] != command[1:]:
                receipt_contract_errors.append("executed_command 非首参数与 command 不一致")
            executable_path = Path(executed_command[0]).expanduser()
            if not executable_path.is_absolute():
                receipt_contract_errors.append("executed_command[0] 不是绝对路径")
                executable_path = None
            else:
                canonical_executable = executable_path.resolve()
                if (
                    executable_path != canonical_executable
                    or not canonical_executable.is_file()
                    or canonical_executable.is_symlink()
                ):
                    receipt_contract_errors.append(
                        "executed_command[0] 不存在或不是 canonical 普通文件"
                    )
                executable_path = canonical_executable
        if not isinstance(executable, dict):
            receipt_contract_errors.append("executable metadata 不是对象")
        elif command_valid and executable_path is not None:
            resolution = executable.get("resolution")
            if executable.get("argv0") != command[0]:
                receipt_contract_errors.append("executable.argv0 与 command[0] 不一致")
            if executable.get("resolved_path") != str(executable_path):
                receipt_contract_errors.append(
                    "executable.resolved_path 与 executed_command[0] 不一致"
                )
            if resolution not in {"absolute", "run-relative", "sanitized-PATH"}:
                receipt_contract_errors.append("executable.resolution 无效")
            expected_within_run = False
            expected_within_project = False
            try:
                executable_path.relative_to(run.resolve())
                expected_within_run = True
            except ValueError:
                pass
            try:
                executable_path.relative_to(ROOT.resolve())
                expected_within_project = True
            except ValueError:
                pass
            if executable.get("within_run") is not expected_within_run:
                receipt_contract_errors.append("executable.within_run 与真实路径不一致")
            if executable.get("within_project") is not expected_within_project:
                receipt_contract_errors.append("executable.within_project 与真实路径不一致")
            raw_argv0 = Path(
                command[0].replace("\\", os.sep).replace("/", os.sep)
            ).expanduser()
            has_separator = "/" in command[0] or "\\" in command[0]
            if resolution == "absolute":
                if not raw_argv0.is_absolute() or raw_argv0.resolve() != executable_path:
                    receipt_contract_errors.append(
                        "absolute executable resolution 不能由 command[0] 重放"
                    )
            elif resolution == "run-relative":
                if (
                    raw_argv0.is_absolute()
                    or not has_separator
                    or cwd_resolved is None
                    or (cwd_resolved / raw_argv0).resolve() != executable_path
                    or not expected_within_run
                ):
                    receipt_contract_errors.append(
                        "run-relative executable resolution 不能由 cwd/command 重放"
                    )
            elif resolution == "sanitized-PATH" and (
                raw_argv0.is_absolute() or has_separator
            ):
                receipt_contract_errors.append(
                    "sanitized-PATH executable 必须使用无路径分隔符的 argv0"
                )

        for stream_name in ("stdout", "stderr"):
            stream_path = expected_record_dir / f"{stream_name}.txt"
            expected_stream_relative = stream_path.relative_to(run).as_posix()
            if receipt.get(f"{stream_name}_path") != expected_stream_relative:
                receipt_contract_errors.append(f"{stream_name}_path 与 canonical attempt 路径不一致")
                continue
            if not stream_path.is_file():
                receipt_contract_errors.append(f"缺少 {stream_name}.txt")
                continue
            resolved_stream = stream_path.resolve()
            try:
                canonical_stream = resolved_stream.relative_to(run.resolve()).as_posix()
            except ValueError:
                receipt_contract_errors.append(f"{stream_name}.txt 离开当前 run")
                continue
            if canonical_stream != expected_stream_relative:
                receipt_contract_errors.append(f"{stream_name}.txt 使用了符号链接或非 canonical 路径")
                continue
            receipt_artifact_paths.add(resolved_stream)
            if receipt.get(f"{stream_name}_sha256") != sha256_file(resolved_stream):
                receipt_contract_errors.append(f"{stream_name}_sha256 与当前文件不一致")

        manifest_text = str(receipt.get("manifest_path", "")).strip()
        manifest_path = Path(manifest_text).expanduser()
        if not manifest_text or not manifest_path.is_absolute():
            receipt_contract_errors.append("manifest_path 不是 runner 记录的绝对路径")
        else:
            resolved_manifest = manifest_path.resolve()
            if manifest_path != resolved_manifest:
                receipt_contract_errors.append("manifest_path 使用了符号链接或非 canonical 路径")
            try:
                project_manifest_relative = resolved_manifest.relative_to(ROOT.resolve()).as_posix()
            except ValueError:
                project_manifest_relative = ""
                receipt_contract_errors.append("manifest_path 离开 harness project")
            if not resolved_manifest.is_file() or resolved_manifest.stat().st_size <= 0:
                receipt_contract_errors.append("manifest_path 不存在、不是文件或为空")
            elif (
                not _valid_sha256(receipt.get("manifest_sha256"))
                or receipt.get("manifest_sha256") != sha256_file(resolved_manifest)
            ):
                receipt_contract_errors.append("manifest_sha256 与当前实验 manifest 不一致")
            elif project_manifest_relative:
                try:
                    resolved_manifest.relative_to(run.resolve())
                except ValueError:
                    manifest_source_paths.add(resolved_manifest)
                else:
                    receipt_artifact_paths.add(resolved_manifest)
                try:
                    manifest_payload = json.loads(
                        resolved_manifest.read_text(encoding="utf-8-sig")
                    )
                    manifest_experiments = (
                        manifest_payload.get("experiments", [])
                        if isinstance(manifest_payload, dict)
                        else []
                    )
                    manifest_matches = [
                        item
                        for item in manifest_experiments
                        if isinstance(item, dict)
                        and str(item.get("id", "")).strip() == experiment_id
                    ]
                    if len(manifest_matches) != 1:
                        raise ValueError(
                            "manifest must contain exactly one experiment entry with this id"
                        )
                    manifest_experiment = manifest_matches[0]
                    manifest_seeds = manifest_experiment.get("seeds", [None])
                    if (
                        not isinstance(manifest_seeds, list)
                        or sum(normalize_seed(item) == log_seed for item in manifest_seeds) != 1
                    ):
                        raise ValueError("manifest seeds do not uniquely contain the receipt seed")
                    artifact_dir = (
                        run / "experiments" / "runs" / experiment_id / f"seed_{log_seed}"
                    )
                    mapping = {
                        "seed": log_seed,
                        "output_dir": str(artifact_dir.resolve()),
                        "record_dir": str(expected_record_dir.resolve()),
                        "run_dir": str(run.resolve()),
                        "project_root": str(ROOT.resolve()),
                        "experiment_id": experiment_id,
                        "attempt_id": attempt_id,
                    }
                    raw_patterns = manifest_experiment.get("expected_artifacts")
                    if not isinstance(raw_patterns, list) or not raw_patterns:
                        raise ValueError("manifest expected_artifacts must be a non-empty array")
                    derived_patterns: list[str] = []
                    for raw_pattern in raw_patterns:
                        if not isinstance(raw_pattern, str):
                            raise ValueError("manifest expected_artifacts must contain strings")
                        normalized_pattern = _run_relative_pattern(run, raw_pattern, mapping)
                        if normalized_pattern not in derived_patterns:
                            derived_patterns.append(normalized_pattern)
                    if manifest_experiment.get("metrics_file"):
                        derived_metrics = _resolve_run_file(
                            run,
                            str(manifest_experiment["metrics_file"]),
                            mapping,
                            label="metrics_file",
                        )
                        derived_metrics_relative = relative_to_run(derived_metrics, run)
                        if derived_metrics_relative not in derived_patterns:
                            derived_patterns.append(derived_metrics_relative)
                    raw_env = manifest_experiment.get("env", {})
                    if not isinstance(raw_env, dict):
                        raise ValueError("manifest env must be an object")
                    derived_environment = _manifest_environment(raw_env, mapping)
                    derived_timeout = int(manifest_experiment.get("timeout_sec", 1800))
                    derived_values = {
                        "description": manifest_experiment.get("description", ""),
                        "module_id": manifest_experiment.get("module_id", ""),
                        "command": _command_list(manifest_experiment.get("command"), mapping),
                        "cwd": str(
                            _safe_cwd(
                                run,
                                str(manifest_experiment.get("cwd", ".")),
                                mapping,
                            )
                        ),
                        "timeout_sec": derived_timeout,
                        "expected_artifacts": derived_patterns,
                        "environment_override_keys": sorted(derived_environment),
                    }
                    for field, derived_value in derived_values.items():
                        if receipt.get(field) != derived_value:
                            receipt_contract_errors.append(
                                f"run.json {field} 不能由绑定的 manifest entry 重放"
                            )
                except (Exception, SystemExit) as exc:
                    receipt_contract_errors.append(
                        f"绑定的 manifest 无法重放 runner request：{type(exc).__name__}: {exc}"
                    )

        created_at = receipt.get("created_at")
        finished_at = receipt.get("finished_at")
        if not _is_valid_review_time(created_at) or not _is_valid_review_time(finished_at):
            receipt_contract_errors.append("created_at/finished_at 必须是含时区 ISO 8601")
        else:
            created_time = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            finished_time = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
            if finished_time < created_time:
                receipt_contract_errors.append("finished_at 早于 created_at")
        if str(row.get("timestamp", "")).strip() != str(finished_at or "").strip():
            receipt_contract_errors.append("EXPERIMENT_LOG.timestamp 与 finished_at 不一致")
        duration = receipt.get("duration_sec")
        try:
            duration_value = float(duration)
            logged_duration = float(str(row.get("duration_sec", "")).strip())
        except (TypeError, ValueError, OverflowError):
            duration_value = math.nan
            logged_duration = math.nan
        if (
            isinstance(duration, bool)
            or not math.isfinite(duration_value)
            or duration_value < 0
            or logged_duration != duration_value
        ):
            receipt_contract_errors.append("duration_sec 无效或与 EXPERIMENT_LOG 不一致")

        if receipt_contract_errors:
            errors.extend(
                f"实验收据来源契约失败 {experiment_id} ({artifact.relative_to(run)})：{message}"
                for message in receipt_contract_errors
            )
            continue
        if (
            str(receipt.get("process_status", "")).strip().lower() != "passed"
            or isinstance(receipt.get("return_code"), bool)
            or not isinstance(receipt.get("return_code"), int)
            or receipt.get("return_code") != 0
            or receipt.get("timed_out") is not False
            or not str(receipt.get("attempt_id", "")).strip()
        ):
            errors.append(
                f"实验收据缺少成功执行证明：{experiment_id} ({artifact.relative_to(run)})"
            )
            continue
        metric_validation = receipt.get("metrics_validation")
        try:
            receipt_metric_count = int(
                metric_validation.get("valid_row_count", 0)
                if isinstance(metric_validation, dict) else 0
            )
        except (TypeError, ValueError, OverflowError):
            receipt_metric_count = 0
        if (
            not isinstance(metric_validation, dict)
            or metric_validation.get("configured") is not True
            or metric_validation.get("errors")
            or receipt_metric_count < 1
        ):
            errors.append(f"实验收据没有有效指标证明：{experiment_id} ({artifact.relative_to(run)})")
            continue
        expected_patterns = receipt.get("expected_artifacts")
        artifact_records = receipt.get("artifacts")
        receipt_artifact_errors: list[str] = []
        if not isinstance(expected_patterns, list) or not expected_patterns or any(
            not isinstance(item, str) or not item.strip() for item in expected_patterns
        ):
            receipt_artifact_errors.append("expected_artifacts 不是非空字符串数组")
            expected_patterns = []
        elif len(expected_patterns) != len(set(expected_patterns)):
            receipt_artifact_errors.append("expected_artifacts 含重复 pattern")
        if receipt.get("missing_expected_artifacts") != []:
            receipt_artifact_errors.append("missing_expected_artifacts 非空或缺失")
        if receipt.get("stale_expected_artifacts") != []:
            receipt_artifact_errors.append("stale_expected_artifacts 非空或缺失")
        if not isinstance(artifact_records, list) or not artifact_records:
            receipt_artifact_errors.append("artifacts 不是非空数组")
            artifact_records = []
        covered_patterns: set[str] = set()
        current_artifact_paths: set[Path] = set()
        recorded_by_pattern: dict[str, set[Path]] = {}
        for artifact_index, record in enumerate(artifact_records, start=1):
            if not isinstance(record, dict):
                receipt_artifact_errors.append(f"artifacts[{artifact_index}] 不是对象")
                continue
            expected_pattern = str(record.get("expected_pattern", "")).strip()
            if expected_pattern not in expected_patterns:
                receipt_artifact_errors.append(
                    f"artifacts[{artifact_index}].expected_pattern 未在 expected_artifacts 中"
                )
            else:
                covered_patterns.add(expected_pattern)
            current_artifact, current_error = _resolve_run_artifact(
                run, str(record.get("path", "")).strip()
            )
            if current_error or current_artifact is None:
                receipt_artifact_errors.append(
                    f"artifacts[{artifact_index}].path 无效：{current_error or 'unresolved path'}"
                )
                continue
            if record.get("fresh") is not True:
                receipt_artifact_errors.append(f"artifacts[{artifact_index}].fresh 不是 true")
            try:
                recorded_size = int(record.get("size"))
            except (TypeError, ValueError, OverflowError):
                recorded_size = -1
            current_stat = current_artifact.stat()
            if recorded_size != current_stat.st_size:
                receipt_artifact_errors.append(f"artifacts[{artifact_index}].size 与当前文件不一致")
            try:
                recorded_mtime_ns = int(record.get("mtime_ns"))
            except (TypeError, ValueError, OverflowError):
                recorded_mtime_ns = -1
            if recorded_mtime_ns != current_stat.st_mtime_ns:
                receipt_artifact_errors.append(
                    f"artifacts[{artifact_index}].mtime_ns 与当前文件不一致"
                )
            current_sha256 = sha256_file(current_artifact)
            if str(record.get("sha256", "")).strip() != current_sha256:
                receipt_artifact_errors.append(f"artifacts[{artifact_index}].sha256 与当前文件不一致")
            pre_sha256 = record.get("pre_sha256")
            pre_mtime_ns = record.get("pre_mtime_ns")
            if (pre_sha256 is None) != (pre_mtime_ns is None):
                receipt_artifact_errors.append(
                    f"artifacts[{artifact_index}] pre-state 字段不完整"
                )
            elif pre_sha256 is not None:
                normalized_pre_sha256 = str(pre_sha256).strip().lower()
                if len(normalized_pre_sha256) != 64 or any(
                    char not in "0123456789abcdef" for char in normalized_pre_sha256
                ):
                    receipt_artifact_errors.append(
                        f"artifacts[{artifact_index}].pre_sha256 格式无效"
                    )
                try:
                    normalized_pre_mtime_ns = int(pre_mtime_ns)
                except (TypeError, ValueError, OverflowError):
                    normalized_pre_mtime_ns = recorded_mtime_ns
                    receipt_artifact_errors.append(
                        f"artifacts[{artifact_index}].pre_mtime_ns 格式无效"
                    )
                if normalized_pre_sha256 == current_sha256:
                    receipt_artifact_errors.append(
                        f"artifacts[{artifact_index}] 内容摘要未变化，并非本次 fresh 产物"
                    )
            current_artifact_paths.add(current_artifact)
            recorded_by_pattern.setdefault(expected_pattern, set()).add(current_artifact)
        uncovered = sorted(set(expected_patterns) - covered_patterns)
        if uncovered:
            receipt_artifact_errors.append("expected_artifacts 未被覆盖：" + ", ".join(uncovered))
        for expected_pattern in expected_patterns:
            current_matches: set[Path] = set()
            try:
                candidates = run.glob(expected_pattern)
                for candidate in candidates:
                    if not candidate.is_file() or candidate.stat().st_size <= 0:
                        continue
                    resolved_candidate = candidate.resolve()
                    try:
                        relative_candidate = resolved_candidate.relative_to(run.resolve())
                    except ValueError:
                        receipt_artifact_errors.append(
                            f"expected pattern 解析到 run 外：{expected_pattern}"
                        )
                        continue
                    if (
                        "_attempts" in relative_candidate.parts
                        and relative_candidate.name in RUNNER_RECORD_NAMES
                    ):
                        continue
                    current_matches.add(resolved_candidate)
            except (OSError, ValueError) as exc:
                receipt_artifact_errors.append(f"expected pattern 无法重放 {expected_pattern!r}: {exc}")
                continue
            if current_matches != recorded_by_pattern.get(expected_pattern, set()):
                receipt_artifact_errors.append(
                    f"expected pattern 当前匹配集与收据不一致：{expected_pattern}"
                )
        try:
            recorded_fresh_count = int(receipt.get("fresh_artifact_count"))
        except (TypeError, ValueError, OverflowError):
            recorded_fresh_count = -1
        if recorded_fresh_count != len(current_artifact_paths):
            receipt_artifact_errors.append("fresh_artifact_count 与当前唯一产物数不一致")
        metrics_source_path, metrics_source_error = _resolve_run_artifact(
            run, str(metric_validation.get("path", "")).strip()
        )
        source_metrics: set[tuple[str, str, float, str]] = set()
        if metrics_source_error or metrics_source_path not in current_artifact_paths:
            receipt_artifact_errors.append("metrics_validation.path 未绑定到当前 artifacts 记录")
        elif metrics_source_path is not None:
            try:
                metrics_payload = json.loads(metrics_source_path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                receipt_artifact_errors.append(f"metrics_validation.path 当前不是有效 JSON：{exc}")
                metrics_payload = []
            if isinstance(metrics_payload, dict):
                source_rows = metrics_payload.get("metrics", [])
            else:
                source_rows = metrics_payload
            if not isinstance(source_rows, list):
                receipt_artifact_errors.append("metrics source 必须是数组或含 metrics 数组的对象")
                source_rows = []
            seen_metric_keys: set[tuple[str, str]] = set()
            for metric_index, source_row in enumerate(source_rows, start=1):
                if not isinstance(source_row, dict):
                    receipt_artifact_errors.append(f"metrics source[{metric_index}] 不是对象")
                    continue
                source_metric = str(source_row.get("metric", "")).strip()
                source_split = str(source_row.get("split", "")).strip()
                source_unit = str(source_row.get("unit", "")).strip()
                try:
                    source_value = float(source_row.get("value"))
                    source_finite = math.isfinite(source_value)
                except (TypeError, ValueError, OverflowError):
                    source_value = 0.0
                    source_finite = False
                source_key = (source_split, source_metric)
                if (
                    not source_metric
                    or source_split.lower() in UNSPECIFIED_METRIC_VALUES
                    or source_unit.lower() in UNSPECIFIED_METRIC_VALUES
                    or not source_finite
                    or source_key in seen_metric_keys
                ):
                    receipt_artifact_errors.append(f"metrics source[{metric_index}] 不满足 runner 指标契约")
                    continue
                if "seed" in source_row and normalize_seed(source_row.get("seed")) != log_seed:
                    receipt_artifact_errors.append(f"metrics source[{metric_index}].seed 与收据不一致")
                    continue
                seen_metric_keys.add(source_key)
                source_metrics.add((source_split, source_metric, source_value, source_unit))
            if len(source_metrics) != receipt_metric_count:
                receipt_artifact_errors.append("metrics source 有效行数与 receipt valid_row_count 不一致")
        if receipt_artifact_errors:
            errors.extend(
                f"实验收据产物校验失败 {experiment_id} ({artifact.relative_to(run)})：{message}"
                for message in receipt_artifact_errors
            )
            continue
        valid_receipts[artifact] = {
            "experiment_id": experiment_id,
            "seed": log_seed,
            "module_id": log_module,
            "artifact_paths": sorted(current_artifact_paths),
            "metrics_source_path": metrics_source_path,
            "source_metrics": source_metrics,
        }
        receipt_artifact_paths.update(current_artifact_paths)

    if passed_rows and not valid_receipts:
        errors.append("没有可验证的 passed 实验 run.json 收据")

    valid_metric_count = 0
    valid_metric_receipts: set[Path] = set()
    try:
        all_metric_rows = (
            read_csv(metric_path, expected_fields=METRIC_FIELDS) if metric_path.is_file() else []
        )
    except SystemExit as exc:
        errors.append(f"METRICS.csv 表头无效：{exc}")
        all_metric_rows = []
    metric_rows = [
        row for row in all_metric_rows
        if selected_ids is None or str(row.get("experiment_id", "")).strip() in selected_ids
    ]
    receipts_by_experiment: dict[str, set[Path]] = {}
    for receipt_path, metadata in valid_receipts.items():
        receipts_by_experiment.setdefault(metadata["experiment_id"], set()).add(receipt_path)
    seen_metric_rows: dict[tuple[str, str, str, str, str], int] = {}
    valid_metrics_by_receipt: dict[Path, set[tuple[str, str, float, str]]] = {}
    for row_number, row in enumerate(metric_rows, start=2):
        experiment_id = str(row.get("experiment_id", "")).strip()
        metric_key = (
            experiment_id,
            normalize_seed(row.get("seed")),
            str(row.get("module_id", "")).strip(),
            str(row.get("split", "")).strip(),
            str(row.get("metric", "")).strip(),
        )
        if metric_key in seen_metric_rows:
            errors.append(
                f"METRICS.csv:{row_number} 重复指标键 {metric_key!r}；"
                f"首次出现在 row {seen_metric_rows[metric_key]}"
            )
            continue
        seen_metric_rows[metric_key] = row_number
        if not experiment_id:
            errors.append(f"METRICS.csv:{row_number} experiment_id 为空")
            continue
        if experiment_id not in receipts_by_experiment:
            errors.append(
                f"METRICS.csv:{row_number} {experiment_id} 未绑定任何有效 passed runner receipt"
            )
            continue
        row_errors: list[str] = []
        metric = str(row.get("metric", "")).strip()
        split = str(row.get("split", "")).strip()
        unit = str(row.get("unit", "")).strip()
        value = str(row.get("value", "")).strip()
        if not metric:
            row_errors.append("metric 为空")
        if split.lower() in UNSPECIFIED_METRIC_VALUES:
            row_errors.append("split 未明确")
        if unit.lower() in UNSPECIFIED_METRIC_VALUES:
            row_errors.append("unit 未明确")
        try:
            finite_value = bool(value) and math.isfinite(float(value))
        except (TypeError, ValueError, OverflowError):
            finite_value = False
        if not finite_value:
            row_errors.append("value 不是有限数值")
        evidence_path, evidence_error = _resolve_run_artifact(
            run, str(row.get("evidence_path", "")).strip()
        )
        if evidence_error:
            row_errors.append(f"evidence_path 无效：{evidence_error}")
        elif evidence_path not in receipts_by_experiment[experiment_id]:
            row_errors.append("evidence_path 未指向该实验的 passed run.json")
        else:
            receipt_metadata = valid_receipts[evidence_path]
            if normalize_seed(row.get("seed")) != receipt_metadata["seed"]:
                row_errors.append("seed 与所引用的 passed run.json 不一致")
            if str(row.get("module_id", "")).strip() != receipt_metadata["module_id"]:
                row_errors.append("module_id 与所引用的 passed run.json 不一致")
            if finite_value and (
                split, metric, float(value), unit
            ) not in receipt_metadata["source_metrics"]:
                row_errors.append("指标内容与 run.json 绑定的 metrics source 不一致")
        if row_errors:
            errors.append(
                f"METRICS.csv:{row_number} {experiment_id} 指标无效：" + "; ".join(row_errors)
            )
        else:
            valid_metric_count += 1
            if evidence_path is not None:
                valid_metric_receipts.add(evidence_path)
                valid_metrics_by_receipt.setdefault(evidence_path, set()).add(
                    (split, metric, float(value), unit)
                )
    for receipt_path, metadata in valid_receipts.items():
        recorded_metrics = valid_metrics_by_receipt.get(receipt_path, set())
        source_metrics = metadata.get("source_metrics", set())
        if recorded_metrics != source_metrics:
            relative_receipt = receipt_path.relative_to(run.resolve()).as_posix()
            errors.append(
                "METRICS.csv 未完整、精确镜像 passed receipt 的 metrics source："
                f"{relative_receipt}"
            )
    if not valid_metric_count:
        errors.append("METRICS.csv 没有与有效 passed 实验收据关联的指标")

    empirical_receipt_paths = sorted(
        path.relative_to(run.resolve()).as_posix() for path in valid_metric_receipts
    )
    empirical_metric_paths = sorted({
        str(valid_receipts[path]["metrics_source_path"].relative_to(run.resolve()).as_posix())
        for path in valid_metric_receipts
        if path in valid_receipts and valid_receipts[path].get("metrics_source_path") is not None
    })
    input_records: list[dict[str, Any]] = []
    if selected_ids is not None:
        for experiment_id in sorted(selected_ids):
            for table_name, fields, rows in (
                ("EXPERIMENT_LOG.csv", LOG_FIELDS, selected_log_rows),
                ("METRICS.csv", METRIC_FIELDS, metric_rows),
            ):
                projected_rows = [
                    {field: str(row.get(field, "")) for field in fields}
                    for row in rows
                    if str(row.get("experiment_id", "")).strip() == experiment_id
                ]
                projected_rows.sort(
                    key=lambda item: json.dumps(
                        item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                )
                projection = {
                    "schema_version": 1,
                    "table": table_name,
                    "fields": fields,
                    "experiment_id": experiment_id,
                    "rows": projected_rows,
                }
                encoded = json.dumps(
                    projection,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                input_records.append({
                    "kind": "semantic-projection",
                    "path": f"experiments/{table_name}#experiment_id={experiment_id}",
                    "row_count": len(projected_rows),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                })
        for path in sorted(manifest_source_paths):
            input_records.append({
                "kind": "source",
                "path": path.relative_to(ROOT.resolve()).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            })
    return {
        "errors": errors,
        "passed_log_rows": len(passed_rows),
        "valid_receipt_count": len(valid_receipts),
        "valid_metric_count": valid_metric_count,
        "valid_experiment_ids": sorted({
            str(metadata["experiment_id"]) for metadata in valid_receipts.values()
        }),
        "receipt_paths": [path.relative_to(run.resolve()).as_posix() for path in sorted(valid_receipts)],
        "artifact_paths": [
            path.relative_to(run.resolve()).as_posix() for path in sorted(receipt_artifact_paths)
        ],
        "source_paths": [
            path.relative_to(ROOT.resolve()).as_posix() for path in sorted(manifest_source_paths)
        ],
        "empirical_receipt_paths": empirical_receipt_paths,
        "empirical_metric_paths": empirical_metric_paths,
        "input_records": input_records,
    }


def _schema_gate_inputs(run: Path, gate_id: str) -> tuple[set[Path], set[Path]]:
    """Return run artifacts and schemas participating in a gate validation."""
    artifacts: set[Path] = set()
    controls: set[Path] = set()
    registry_path = ROOT / "config" / "artifact_schema_registry.json"
    if not registry_path.is_file():
        return artifacts, controls
    registry = read_json(registry_path, expect=dict)
    for rule in registry.get("artifacts", []):
        if not isinstance(rule, dict) or gate_id not in [str(item) for item in rule.get("stages", [])]:
            continue
        base = ROOT if rule.get("base") == "project" else run
        artifacts.update(path.resolve() for path in base.glob(str(rule.get("glob", ""))) if path.is_file())
        schema_path = (ROOT / str(rule.get("schema", ""))).resolve()
        if schema_path.is_file():
            controls.add(schema_path)
    return artifacts, controls


def _model_selection_decision_digest(path: Path) -> tuple[str, int]:
    """Hash only stable G2 selection events from the mixed audit JSONL."""
    events = [
        event
        for event in read_jsonl(path)
        if str(event.get("type", "")).strip() in MODEL_SELECTION_DECISION_EVENT_TYPES
    ]
    payload = {
        "schema_version": 1,
        "included_event_types": sorted(MODEL_SELECTION_DECISION_EVENT_TYPES),
        "explicitly_excluded_audit_event_types": sorted(NON_SEMANTIC_DECISION_EVENT_TYPES),
        "events": events,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), len(events)


def _g7_decision_log_digest(
    path: Path,
    current_gate_state: dict[str, Any] | None = None,
    *,
    exclude_current: bool = True,
) -> tuple[str, int, int | None]:
    """Bind G7 audit history, excluding at most its current self receipt.

    The approval command validates G7 and then appends that exact validation to
    ``DECISION_LOG.jsonl``.  Excluding the last event that exactly matches the
    current ``STATUS.G7`` record prevents this write from invalidating itself.
    Older G7 approvals, duplicate receipts and unrelated audit events remain
    bound inputs.
    """
    events = read_jsonl(path)
    excluded_index: int | None = None
    state = current_gate_state if isinstance(current_gate_state, dict) else {}
    approved_validation = state.get("validation")
    approved_by = str(state.get("approved_by", "")).strip()
    if (
        exclude_current
        and state.get("status") == "approved"
        and approved_by
        and isinstance(approved_validation, dict)
        and str(approved_validation.get("input_sha256", "")).strip()
    ):
        matching_indices: list[int] = []
        for index, event in enumerate(events):
            event_validation = event.get("validation")
            if (
                str(event.get("type", "")).strip() == "gate-approval"
                and str(event.get("gate", "")).strip() == "G7"
                and str(event.get("by", "")).strip() == approved_by
                and event.get("strict") is True
                and isinstance(event_validation, dict)
                and event_validation == approved_validation
            ):
                matching_indices.append(index)
        if matching_indices:
            # Exclude exactly one record.  An appended duplicate therefore
            # remains visible and changes the approval-bound history digest.
            excluded_index = matching_indices[-1]
    projected_events = [
        event for index, event in enumerate(events) if index != excluded_index
    ]
    payload = {
        "schema_version": 2,
        "projection": "all-events-except-current-matching-g7-approval",
        "events": projected_events,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), len(projected_events), excluded_index


def _decision_log_contract_errors(run: Path) -> list[str]:
    """Validate audit events and reconcile current approvals with their records."""
    path = run / "decisions" / "DECISION_LOG.jsonl"
    if not path.is_file() or path.stat().st_size <= 0:
        return ["decisions/DECISION_LOG.jsonl 不存在或为空"]
    try:
        events = read_jsonl(path)
    except SystemExit as exc:
        return [f"DECISION_LOG.jsonl 无效：{exc}"]
    errors: list[str] = []
    human_event_types = {
        "gate-approval", "gate-approval-blocked", "gate-revoke",
        "freeze", "unfreeze", "dispute-resolved", "package-validation-bypass",
    }
    for index, event in enumerate(events, start=1):
        event_type = str(event.get("type", "")).strip()
        if not event_type:
            errors.append(f"DECISION_LOG.jsonl:{index} 缺少 type")
        if not is_timezone_aware_iso(event.get("timestamp")):
            errors.append(f"DECISION_LOG.jsonl:{index} timestamp 不是含时区 ISO 8601")
        if event_type in human_event_types and not is_named_human_identity(event.get("by")):
            errors.append(f"DECISION_LOG.jsonl:{index} {event_type} 缺少具名人工 by")

    try:
        status = load_status(run)
    except SystemExit as exc:
        return errors + [f"无法用 STATUS.json 对账 decision log：{exc}"]
    gates = status.get("gates", {}) if isinstance(status, dict) else {}
    if isinstance(gates, dict):
        for gate_id, state in gates.items():
            if not isinstance(state, dict) or state.get("status") != "approved":
                continue
            validation = state.get("validation") or {}
            expected_hash = (
                validation.get("input_sha256") if isinstance(validation, dict) else None
            )
            matches = []
            for event in events:
                event_validation = event.get("validation") or {}
                if (
                    event.get("type") == "gate-approval"
                    and event.get("gate") == gate_id
                    and event.get("by") == state.get("approved_by")
                    and event.get("strict") is True
                    and isinstance(event_validation, dict)
                    and event_validation.get("input_sha256") == expected_hash
                    and event_validation.get("review_report")
                    == validation.get("review_report")
                ):
                    matches.append(event)
            if not matches:
                errors.append(f"当前 {gate_id} approval 在 DECISION_LOG.jsonl 中无精确对应记录")
    if status.get("frozen") is True:
        manifest_path = run / "FROZEN_MANIFEST.json"
        if manifest_path.is_file():
            try:
                manifest = read_json(manifest_path, expect=dict)
            except SystemExit as exc:
                errors.append(f"无法用 FROZEN_MANIFEST.json 对账 decision log：{exc}")
            else:
                freeze_matches = [
                    event for event in events
                    if event.get("type") == "freeze"
                    and event.get("by") == manifest.get("created_by")
                    and event.get("file_count") == manifest.get("file_count")
                    and event.get("before_g5_exception") == manifest.get("before_g5_exception")
                ]
                if not freeze_matches:
                    errors.append("当前 science freeze 在 DECISION_LOG.jsonl 中无精确对应记录")
    return errors


def _valid_sha256(value: Any) -> bool:
    digest = str(value or "").strip()
    return (
        len(digest) == 64
        and digest.lower() == digest
        and all(char in "0123456789abcdef" for char in digest)
    )


def _gate_approval_record_errors(
    gate_id: str,
    item: dict[str, Any],
    *,
    run: Path | None = None,
    current_result: dict[str, Any] | None = None,
) -> list[str]:
    """Validate human approval metadata and its stored/current strict snapshot."""
    errors: list[str] = []
    if item.get("status") != "approved":
        return [f"{gate_id} 尚未批准"]
    if not is_named_human_identity(item.get("approved_by")):
        errors.append(f"{gate_id} approved_by 不是具名人工身份")
    if not _is_valid_review_time(item.get("approved_at")):
        errors.append(f"{gate_id} approved_at 缺失或不是含时区 ISO 8601")
    validation = item.get("validation")
    if not isinstance(validation, dict):
        return errors + [f"{gate_id} 缺少已存 gate validation"]
    if validation.get("gate") != gate_id:
        errors.append(f"{gate_id} 已存 validation 指向其他 gate")
    if validation.get("ok") is not True:
        errors.append(f"{gate_id} 已存 validation 不是成功结果")
    if validation.get("strict") is not True:
        errors.append(f"{gate_id} 批准不是基于 strict validation")
    approved_hash = validation.get("input_sha256")
    if not _valid_sha256(approved_hash):
        errors.append(f"{gate_id} 已存批准输入哈希缺失或无效")
    stored_manifest = validation.get("input_manifest")
    if not isinstance(stored_manifest, list):
        errors.append(f"{gate_id} 已存 validation.input_manifest 不是数组")
    review_report = validation.get("review_report")
    expected_review_path = f"reviews/gates/{gate_id}_REVIEW.md"
    if not isinstance(review_report, dict):
        # Keep legacy STATUS records structurally readable so their Gate can
        # be re-approved under the new contract.  Any freshness check supplies
        # current_result and then fails closed until a report is generated.
        if current_result is not None:
            errors.append(f"{gate_id} 缺少已绑定的人工审核摘要")
    else:
        if review_report.get("path") != expected_review_path:
            errors.append(f"{gate_id} 人工审核摘要路径不是 canonical 路径")
        if not _valid_sha256(review_report.get("sha256")):
            errors.append(f"{gate_id} 人工审核摘要 SHA-256 缺失或无效")
        if review_report.get("underlying_input_sha256") != approved_hash:
            errors.append(f"{gate_id} 人工审核摘要未绑定批准输入哈希")
    if current_result is not None:
        if current_result.get("ok") is not True or current_result.get("strict") is not True:
            errors.append(f"{gate_id} 当前 strict 重验未通过")
        if approved_hash != current_result.get("input_sha256"):
            errors.append(f"{gate_id} 批准输入哈希与当前重验不一致")
        if stored_manifest != current_result.get("input_manifest"):
            errors.append(f"{gate_id} 已存输入清单与当前重验不一致")
        if run is None:
            errors.append(f"{gate_id} 无法在缺少 run 路径时重验人工审核摘要")
        elif isinstance(review_report, dict):
            try:
                from gate_review import verify_gate_review

                errors.extend(
                    f"{gate_id} 人工审核摘要：{message}"
                    for message in verify_gate_review(
                        run,
                        current_result,
                        binding=review_report,
                    )
                )
            except (Exception, SystemExit) as exc:
                errors.append(
                    f"{gate_id} 人工审核摘要校验器异常："
                    f"{type(exc).__name__}: {exc}"
                )
    return errors


def _status_contract_errors(
    run: Path, status: dict[str, Any], config: dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    expected_top_fields = {
        "version", "run_id", "created_at", "updated_at", "current_gate",
        "gates", "frozen", "frozen_at", "freeze_manifest",
    }
    if set(status) != expected_top_fields:
        errors.append(
            "STATUS.json top-level keys are non-canonical "
            f"(missing={sorted(expected_top_fields - set(status))}, "
            f"extra={sorted(set(status) - expected_top_fields)})"
        )
    if status.get("version") != config.get("version"):
        errors.append("STATUS.json version does not match harness_config.json")
    if status.get("run_id") != run.name:
        errors.append("STATUS.json run_id does not match the current run")
    for field in ("created_at", "updated_at"):
        if not _is_valid_review_time(status.get(field)):
            errors.append(f"STATUS.json {field} is not a timezone-aware ISO-8601 timestamp")
    gate_order = _gate_order(config)
    gates = status.get("gates")
    if not isinstance(gates, dict):
        errors.append("STATUS.json gates must be an object")
        gates = {}
    if set(gates) != set(gate_order):
        errors.append(
            "STATUS.json gate keys do not exactly match harness_config.json "
            f"(missing={sorted(set(gate_order) - set(gates))}, "
            f"extra={sorted(set(gates) - set(gate_order))})"
        )
    expected_gate_fields = {
        "name", "status", "approved_at", "approved_by", "note", "validation",
    }
    for gate_id in gate_order:
        item = gates.get(gate_id)
        if not isinstance(item, dict):
            errors.append(f"STATUS.json {gate_id} state must be an object")
            continue
        if set(item) != expected_gate_fields:
            errors.append(
                f"STATUS.json {gate_id} keys are non-canonical "
                f"(missing={sorted(expected_gate_fields - set(item))}, "
                f"extra={sorted(set(item) - expected_gate_fields)})"
            )
        if item.get("name") != config.get("gates", {}).get(gate_id, {}).get("name", gate_id):
            errors.append(f"STATUS.json {gate_id} name differs from harness_config.json")
        gate_status = item.get("status")
        if gate_status not in {"pending", "stale", "approved"}:
            errors.append(f"STATUS.json {gate_id} has invalid status {gate_status!r}")
        if gate_status == "approved":
            errors.extend(_gate_approval_record_errors(gate_id, item))
        elif item.get("approved_at") is not None or item.get("approved_by") is not None:
            errors.append(f"STATUS.json non-approved {gate_id} retains approval metadata")
        if gate_status == "pending" and item.get("validation") is not None:
            errors.append(f"STATUS.json pending {gate_id} retains a validation snapshot")
    if status.get("current_gate") not in gate_order:
        errors.append("STATUS.json current_gate is not canonical")
    if not isinstance(status.get("frozen"), bool):
        errors.append("STATUS.json frozen must be boolean")
    elif status.get("frozen"):
        if not _is_valid_review_time(status.get("frozen_at")):
            errors.append("STATUS.json frozen_at is invalid while frozen=true")
        if status.get("freeze_manifest") != "FROZEN_MANIFEST.json":
            errors.append("STATUS.json freeze_manifest is invalid while frozen=true")
    elif status.get("frozen_at") is not None or status.get("freeze_manifest") is not None:
        errors.append("STATUS.json unfrozen state retains freeze metadata")
    return errors


def _gate_approval_projection_record(
    status: dict[str, Any], gate_ids: list[str], *, label: str
) -> dict[str, str]:
    gates = status.get("gates", {}) if isinstance(status, dict) else {}
    projection: list[dict[str, Any]] = []
    for gate_id in gate_ids:
        item = gates.get(gate_id, {}) if isinstance(gates, dict) else {}
        if not isinstance(item, dict):
            item = {}
        validation = item.get("validation") or {}
        if not isinstance(validation, dict):
            validation = {}
        manifest = validation.get("input_manifest", [])
        if not isinstance(manifest, list):
            manifest = []
        manifest_encoded = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        review_report = validation.get("review_report")
        if not isinstance(review_report, dict):
            review_report = {}
        projection.append({
            "gate": gate_id,
            "status": item.get("status"),
            "approved_by": item.get("approved_by"),
            "approved_at": item.get("approved_at"),
            "note": item.get("note", ""),
            "validation_ok": validation.get("ok"),
            "validation_strict": validation.get("strict"),
            "validation_input_sha256": validation.get("input_sha256"),
            "validation_manifest_sha256": hashlib.sha256(manifest_encoded).hexdigest(),
            "review_report_path": review_report.get("path"),
            "review_report_sha256": review_report.get("sha256"),
            "review_report_underlying_input_sha256": review_report.get(
                "underlying_input_sha256"
            ),
        })
    encoded = json.dumps(
        {"schema_version": 1, "gates": projection},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "kind": "approval-projection",
        "path": label,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


_BUDGET_RUNTIME_KEYS = frozenset({
    "updated_at", "call_count", "logged_tasks", "usage_records",
})


def _budget_policy_projection(value: Any) -> Any:
    """Remove mutable consumption counters while retaining budget policy.

    G0 approves limits, enablement, schedule, concurrency and complexity.  It
    must not become stale merely because an approved provider call consumes a
    reservation.  G7 separately binds and reconciles the complete BUDGET file.
    """
    if isinstance(value, dict):
        return {
            str(key): _budget_policy_projection(item)
            for key, item in value.items()
            if str(key) not in _BUDGET_RUNTIME_KEYS
            and not str(key).startswith("used_")
        }
    if isinstance(value, list):
        return [_budget_policy_projection(item) for item in value]
    return value


def _budget_policy_projection_record(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        # Keep snapshot creation total so validation can report the malformed
        # artifact; the invalid bytes remain bound and cannot silently pass as
        # an empty/default policy.
        projection: Any = {
            "schema_version": 1,
            "invalid_budget_sha256": sha256_file(path),
            "parse_error": type(exc).__name__,
        }
    else:
        projection = {
            "schema_version": 1,
            "policy": _budget_policy_projection(payload),
        }
    encoded = json.dumps(
        projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "kind": "semantic-policy",
        "path": "BUDGET.json#policy",
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _gate_input_snapshot(
    run: Path,
    gate_id: str,
    gate: dict[str, Any],
    v51_results: list[dict[str, Any]],
    *,
    extra_run_paths: Iterable[str] = (),
    extra_source_paths: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], str]:
    keyed_records: dict[tuple[str, str], dict[str, Any]] = {}
    run_root = run.resolve()
    g7_decision_context: tuple[Path, dict[str, Any], int | None] | None = None

    def add_record(kind: str, path: Path, display_path: str) -> None:
        if not path.is_file():
            return
        keyed_records[(kind, display_path)] = {
            "kind": kind, "path": display_path, "sha256": sha256_file(path)
        }

    for relative in gate.get("requires", []):
        display_path = str(relative).replace("\\", "/")
        path = (run / display_path).resolve()
        try:
            path.relative_to(run_root)
        except ValueError:
            continue
        if path.is_file():
            if gate_id == "G0" and display_path == "BUDGET.json":
                record = _budget_policy_projection_record(path)
                keyed_records[(record["kind"], record["path"])] = record
            elif gate_id == "G2" and display_path == "decisions/DECISION_LOG.jsonl":
                digest, event_count = _model_selection_decision_digest(path)
                keyed_records[("semantic-event-log", display_path)] = {
                    "kind": "semantic-event-log",
                    "path": display_path,
                    "sha256": digest,
                    "event_count": event_count,
                }
            else:
                add_record("artifact", path, display_path)

    if gate_id == "G7":
        decision_log = run / "decisions" / "DECISION_LOG.jsonl"
        if decision_log.is_file():
            current_g7_state: dict[str, Any] = {}
            try:
                status_payload = read_json(run / "STATUS.json", expect=dict)
                gate_states = status_payload.get("gates", {})
                candidate_state = (
                    gate_states.get("G7", {})
                    if isinstance(gate_states, dict)
                    else {}
                )
                if isinstance(candidate_state, dict):
                    current_g7_state = candidate_state
            except (Exception, SystemExit):
                # Validation reports malformed STATUS separately.  Keep the
                # snapshot total and conservatively bind the complete log.
                pass
            digest, event_count, excluded_index = _g7_decision_log_digest(
                decision_log, current_g7_state
            )
            keyed_records[("semantic-event-log", "decisions/DECISION_LOG.jsonl")] = {
                "kind": "semantic-event-log",
                "path": "decisions/DECISION_LOG.jsonl#g7-history-projection",
                "sha256": digest,
                "event_count": event_count,
            }
            g7_decision_context = (
                decision_log,
                current_g7_state,
                excluded_index,
            )

    if gate_id == "G0":
        stored_manifest = (run / "INPUT_MANIFEST.json").resolve()
        try:
            stored_manifest.relative_to(run_root)
        except ValueError:
            pass
        else:
            add_record("artifact", stored_manifest, "INPUT_MANIFEST.json")
        input_root = INPUTS_ROOT.resolve()
        for source_path in iter_files(INPUTS_ROOT, exclude_names={".gitkeep"}):
            resolved_source = source_path.resolve()
            try:
                resolved_source.relative_to(input_root)
                display_source = resolved_source.relative_to(ROOT.resolve()).as_posix()
            except ValueError:
                continue
            add_record("source", resolved_source, display_source)

    for value in extra_run_paths:
        artifact, artifact_error = _resolve_run_artifact(run, str(value))
        if artifact_error or artifact is None:
            continue
        add_record("referenced-artifact", artifact, artifact.relative_to(run_root).as_posix())

    project_root = ROOT.resolve()
    for value in extra_source_paths:
        source_path = (ROOT / str(value)).resolve()
        try:
            source_relative = source_path.relative_to(project_root).as_posix()
        except ValueError:
            continue
        add_record("source", source_path, source_relative)

    schema_artifacts, schema_controls = _schema_gate_inputs(run, gate_id)
    for path in schema_artifacts:
        try:
            relative = path.relative_to(run_root).as_posix()
            add_record("artifact", path, relative)
        except ValueError:
            try:
                relative = path.relative_to(ROOT.resolve()).as_posix()
            except ValueError:
                continue
            add_record("control", path, relative)

    if gate_id == "G3":
        reference_files = (
            (run / "experiments" / "EXPERIMENT_LOG.csv", "output_path"),
            (run / "experiments" / "METRICS.csv", "evidence_path"),
            (run / "evidence" / "CLAIM_EVIDENCE_LEDGER.csv", "evidence_path"),
        )
        for table, field in reference_files:
            if not table.is_file():
                continue
            for row in read_csv(table):
                value = str(row.get(field, "")).strip()
                if not value:
                    continue
                artifact, artifact_error = _resolve_run_artifact(run, value)
                if artifact_error or artifact is None:
                    continue
                add_record("referenced-artifact", artifact, artifact.relative_to(run_root).as_posix())

    if gate_id == "G5":
        # G5 approves the exact prospective scientific freeze, including
        # files that no individual validator happened to reference.
        for record in _freeze_records(run):
            relative = str(record["path"])
            add_record("artifact", (run / relative).resolve(), relative)

    for entry in v51_results:
        result = entry.get("result", {}) if isinstance(entry, dict) else {}
        if result.get("input_sha256"):
            keyed_records[("v51-stage", str(entry.get("stage", "")))] = {
                "kind": "v51-stage",
                "path": str(entry.get("stage", "")),
                "sha256": str(result["input_sha256"]),
            }
        approval_record = entry.get("approval_projection") if isinstance(entry, dict) else None
        if isinstance(approval_record, dict) and all(
            approval_record.get(field) for field in ("kind", "path", "sha256")
        ):
            keyed_records[(approval_record["kind"], approval_record["path"])] = approval_record
    if gate_id == "G7" and (run / "V51_STATUS.json").is_file():
        try:
            from v51_orchestrator import approval_projection_record
            v51_state = read_json(run / "V51_STATUS.json", expect=dict)
            record = approval_projection_record(
                v51_state,
                label="V51_STATUS.json#approval-projection",
            )
            keyed_records[(record["kind"], record["path"])] = record
        except (Exception, SystemExit):
            # The G7 validator reports the malformed/missing status. Keep input
            # snapshot construction total so that the validation result can be
            # rendered instead of crashing here.
            pass
    gate_order = _gate_order(load_config())
    gate_index = gate_order.index(gate_id)
    if gate_index:
        try:
            gate_state = read_json(run / "STATUS.json", expect=dict)
            record = _gate_approval_projection_record(
                gate_state,
                gate_order[:gate_index],
                label="STATUS.json#prior-approval-projection",
            )
            keyed_records[(record["kind"], record["path"])] = record
        except (Exception, SystemExit):
            # Approval commands and package/freeze freshness checks report
            # malformed state.  Standalone validation can still render all
            # artifact-level findings without mutating state here.
            pass
    controls = {
        ROOT / "harness_config.json",
        ROOT / "config" / "artifact_schema_registry.json",
        ROOT / "tools" / "harness.py",
        ROOT / "tools" / "common.py",
        ROOT / "tools" / "gate_review.py",
        ROOT / "tools" / "v51_orchestrator.py",
        ROOT / "tools" / "schema_validate.py",
        *schema_controls,
    }
    if gate_id == "G1":
        controls.add(ROOT / "tools" / "diversity.py")
    if gate_id == "G3":
        controls.update({
            ROOT / "tools" / "run_experiments.py",
            ROOT / "tools" / "sensitivity_engine.py",
        })
    if gate_id == "G5":
        controls.update({
            ROOT / "tools" / "claim_guard.py",
            ROOT / "tools" / "v51_common.py",
            ROOT / "config" / "claim_provenance.json",
            ROOT / "schemas" / "strong_claim.schema.json",
            ROOT / "schemas" / "strong_claim_scan.schema.json",
        })
    if gate_id == "G7":
        controls.update({
            ROOT / "tools" / "submission_compliance.py",
            ROOT / "tools" / "support_package.py",
        })
    for control in controls:
        if control.is_file():
            add_record("control", control, control.relative_to(ROOT).as_posix())

    def materialize_snapshot() -> tuple[list[dict[str, Any]], str]:
        records = list(keyed_records.values())
        records.sort(key=lambda item: (item["kind"], item["path"]))
        encoded = json.dumps(
            records,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return records, hashlib.sha256(encoded).hexdigest()

    records, input_sha256 = materialize_snapshot()
    if g7_decision_context is not None:
        decision_log, current_g7_state, excluded_index = g7_decision_context
        approved_validation = current_g7_state.get("validation")
        approved_input_sha256 = (
            str(approved_validation.get("input_sha256", "")).strip()
            if isinstance(approved_validation, dict)
            else ""
        )
        if (
            excluded_index is not None
            and approved_input_sha256
            and input_sha256 != approved_input_sha256
        ):
            # The current receipt is excluded only while doing so reproduces
            # its approved snapshot.  Once any bound input changes, that old
            # receipt becomes immutable history and the next approval binds it.
            digest, event_count, _ = _g7_decision_log_digest(
                decision_log,
                exclude_current=False,
            )
            keyed_records[("semantic-event-log", "decisions/DECISION_LOG.jsonl")] = {
                "kind": "semantic-event-log",
                "path": "decisions/DECISION_LOG.jsonl#g7-history-projection",
                "sha256": digest,
                "event_count": event_count,
            }
            records, input_sha256 = materialize_snapshot()
    return records, input_sha256


def _selection_identity_audit(
    run: Path,
    ideas: list[tuple[Path, dict[str, Any]]],
    modules: list[tuple[Path, dict[str, Any]]],
) -> dict[str, Any]:
    """Reconcile G2 card identities with their canonical registries."""
    errors: list[str] = []
    input_paths: list[str] = []

    def actual_pairs(
        cards: list[tuple[Path, dict[str, Any]]], *, label: str
    ) -> set[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        ids: list[str] = []
        for path, payload in cards:
            card_id = str(payload.get("id", ""))
            try:
                safe_id = require_safe_component(card_id, label=f"{label} id")
            except SystemExit as exc:
                errors.append(f"{label} identity invalid: {exc}")
                continue
            try:
                relative = path.resolve().relative_to(run.resolve()).as_posix()
            except ValueError:
                errors.append(f"{label} leaves current run: {path}")
                continue
            if safe_id != path.parent.name:
                errors.append(
                    f"{label} id {safe_id!r} does not match directory {path.parent.name!r}"
                )
            ids.append(safe_id)
            pairs.append((safe_id, relative))
            input_paths.append(relative)
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            errors.append(f"{label} ids are not unique: {duplicates}")
        return set(pairs)

    def registry_pairs(
        relative: str,
        collection: str,
        expected: set[tuple[str, str]],
        *,
        label: str,
    ) -> None:
        input_paths.append(relative)
        path = run / relative
        if path.is_symlink() or not path.is_file():
            errors.append(f"{label} registry missing or unsafe: {relative}")
            return
        try:
            payload = read_json(path, expect=dict)
        except SystemExit as exc:
            errors.append(f"{label} registry invalid: {exc}")
            return
        rows = payload.get(collection)
        if not isinstance(rows, list):
            errors.append(f"{label} registry {collection} must be an array")
            return
        found: list[tuple[str, str]] = []
        ids: list[str] = []
        paths: list[str] = []
        for index, item in enumerate(rows):
            if not isinstance(item, dict):
                errors.append(f"{label} registry {collection}[{index}] must be an object")
                continue
            raw_id = item.get("id")
            raw_path = item.get("path")
            if not isinstance(raw_id, str) or raw_id != raw_id.strip():
                errors.append(f"{label} registry {collection}[{index}].id is not canonical")
                continue
            try:
                entry_id = require_safe_component(raw_id, label=f"{label} registry id")
            except SystemExit as exc:
                errors.append(f"{label} registry {collection}[{index}].id invalid: {exc}")
                continue
            if (
                not isinstance(raw_path, str)
                or raw_path != raw_path.strip()
                or "\\" in raw_path
            ):
                errors.append(f"{label} registry {collection}[{index}].path is not canonical")
                continue
            file_errors, _ = _file_check(run, raw_path, True, [])
            if file_errors:
                errors.extend(
                    f"{label} registry {collection}[{index}].path: {message}"
                    for message in file_errors
                )
                continue
            if (run / raw_path).parent.name != entry_id:
                errors.append(
                    f"{label} registry id/path directory mismatch: {entry_id!r} -> {raw_path!r}"
                )
            ids.append(entry_id)
            paths.append(raw_path)
            found.append((entry_id, raw_path))
            input_paths.append(raw_path)
        duplicate_ids = sorted({item for item in ids if ids.count(item) > 1})
        duplicate_paths = sorted({item for item in paths if paths.count(item) > 1})
        if duplicate_ids:
            errors.append(f"{label} registry ids are not unique: {duplicate_ids}")
        if duplicate_paths:
            errors.append(f"{label} registry paths are not unique: {duplicate_paths}")
        found_set = set(found)
        if found_set != expected or len(found) != len(expected):
            missing = sorted(expected - found_set)
            extra = sorted(found_set - expected)
            errors.append(
                f"{label} registry/card mismatch: missing={missing}, extra={extra}"
            )

    idea_pairs = actual_pairs(ideas, label="Idea Card")
    module_pairs = actual_pairs(modules, label="Module Card")
    registry_pairs(
        "ideas/IDEA_REGISTRY.json", "ideas", idea_pairs, label="Idea"
    )
    registry_pairs(
        "research/MODULE_REGISTRY.json", "modules", module_pairs, label="Module"
    )
    return {
        "passed": not errors,
        "errors": errors,
        "input_paths": list(dict.fromkeys(input_paths)),
        "idea_count": len(idea_pairs),
        "module_count": len(module_pairs),
    }


def validate_gate(run: Path, gate_id: str, *, strict: bool = False) -> dict[str, Any]:
    config = load_config()
    gates = config.get("gates", {})
    if gate_id not in gates:
        die(f"未知 Gate：{gate_id}")
    gate = gates[gate_id]
    tokens = list(config.get("placeholder_tokens", ["TODO"]))
    errors: list[str] = []
    warnings: list[str] = []
    v51_results: list[dict[str, Any]] = []
    diversity_audit: dict[str, Any] | None = None
    selection_identity_audit: dict[str, Any] | None = None
    experiment_audit: dict[str, Any] | None = None
    parameter_sensitivity_audit: dict[str, Any] | None = None
    strong_claim_math_audit: dict[str, Any] | None = None
    compliance_audit: dict[str, Any] | None = None

    for relative in gate.get("requires", []):
        file_errors, file_warnings = _file_check(run, relative, strict, tokens)
        errors.extend(file_errors)
        warnings.extend(file_warnings)

    stage_results, stage_errors, stage_warnings = _validate_v51_for_gate(run, gate)
    v51_results.extend(stage_results)
    errors.extend(stage_errors)
    warnings.extend(stage_warnings)

    try:
        ideas = load_idea_cards(run)
    except (Exception, SystemExit) as exc:
        ideas = []
        errors.append(f"Idea Card 集合无效：{type(exc).__name__}: {exc}")
    try:
        modules = load_module_cards(run)
    except (Exception, SystemExit) as exc:
        modules = []
        errors.append(f"Module Card 集合无效：{type(exc).__name__}: {exc}")

    if gate_id == "G0":
        meta_path = run / "RUN_META.json"
        if meta_path.is_file():
            try:
                meta = read_json(meta_path, expect=dict)
            except SystemExit as exc:
                errors.append(f"RUN_META.json 无效：{exc}")
            else:
                expected_run_path = run.relative_to(ROOT.resolve()).as_posix()
                if meta.get("run_id") != run.name:
                    errors.append("RUN_META.json run_id 与当前 run 目录不一致")
                if meta.get("run_path") != expected_run_path:
                    errors.append("RUN_META.json run_path 与当前 run 目录不一致")
                if Path(str(meta.get("project_root", ""))).resolve() != ROOT.resolve():
                    errors.append("RUN_META.json project_root 与当前 harness 根目录不一致")
                if meta.get("version") != config.get("version"):
                    errors.append("RUN_META.json version 与当前 harness 配置不一致")
        manifest = run / "INPUT_MANIFEST.json"
        if not manifest.exists():
            errors.append("缺少 INPUT_MANIFEST.json")
        else:
            stored_input_manifest = read_json(manifest, expect=dict)
            current_input_manifest = _input_manifest(INPUTS_ROOT)
            # generated_at describes the inspection, not the inputs. Every
            # source-root/file record (size, SHA-256 and mtime) must otherwise
            # still match the manifest captured for this run.
            stored_snapshot = {
                "source_root": stored_input_manifest.get("source_root"),
                "files": stored_input_manifest.get("files"),
            }
            current_snapshot = {
                "source_root": current_input_manifest.get("source_root"),
                "files": current_input_manifest.get("files"),
            }
            if stored_snapshot != current_snapshot:
                message = (
                    "project inputs/ 已在本 run 的 INPUT_MANIFEST.json 生成后变化；"
                    "请确认题面/数据版本并显式重新生成 manifest"
                )
                (errors if strict else warnings).append(message)
            data_manifest = run / "data" / "DATA_MANIFEST.json"
            if not data_manifest.is_file():
                errors.append("缺少 data/DATA_MANIFEST.json")
            else:
                try:
                    stored_data_manifest = read_json(data_manifest, expect=dict)
                except SystemExit as exc:
                    errors.append(f"data/DATA_MANIFEST.json 无效：{exc}")
                else:
                    current_data_manifest = _input_manifest(INPUTS_ROOT / "data")
                    stored_data_snapshot = {
                        "source_root": stored_data_manifest.get("source_root"),
                        "files": stored_data_manifest.get("files"),
                    }
                    current_data_snapshot = {
                        "source_root": current_data_manifest.get("source_root"),
                        "files": current_data_manifest.get("files"),
                    }
                    if stored_data_snapshot != current_data_snapshot:
                        message = (
                            "inputs/data 与本 run 的 data/DATA_MANIFEST.json 不一致；"
                            "请确认数据版本并显式重新生成 manifest"
                        )
                        (errors if strict else warnings).append(message)
            input_root = INPUTS_ROOT.resolve()
            for source_path in iter_files(INPUTS_ROOT, exclude_names={".gitkeep"}):
                try:
                    source_path.resolve().relative_to(input_root)
                except ValueError:
                    errors.append(f"inputs/ 含指向目录外的文件或符号链接：{source_path}")
        graph_path = run / "problem" / "PROBLEM_GRAPH.json"
        if graph_path.exists():
            graph = read_json(graph_path, expect=dict)
            if not graph.get("nodes"):
                errors.append("PROBLEM_GRAPH.json 没有节点")

    if gate_id == "G1":
        minimum = int(gate.get("min_ideas", 1))
        if len(ideas) < minimum:
            errors.append(f"Idea Card 数量不足：{len(ideas)} < {minimum}")
        try:
            from diversity import validate_diversity

            diversity_audit, _ = validate_diversity(run, strict=True)
            errors.extend(
                f"多样性收据：{item}"
                for item in diversity_audit.get("errors", [])
            )
            warnings.extend(
                f"多样性收据：{item}"
                for item in diversity_audit.get("warnings", [])
            )
        except (Exception, SystemExit) as exc:
            diversity_audit = {
                "passed": False,
                "status": "validator-error",
                "errors": [f"{type(exc).__name__}: {exc}"],
                "warnings": [],
            }
            errors.append(f"多样性收据校验器异常：{type(exc).__name__}: {exc}")

    if gate_id == "G2":
        selection_identity_audit = _selection_identity_audit(run, ideas, modules)
        errors.extend(
            f"selection identity: {item}"
            for item in selection_identity_audit.get("errors", [])
        )
        focus_path = run / "selection" / "FOCUS_SET.json"
        if focus_path.exists():
            focus = read_json(focus_path, expect=dict)
            selected = focus.get("selected_ideas")
            selected_modules = focus.get("selected_modules")
            if (
                not isinstance(selected, list)
                or any(not isinstance(item, str) or not item for item in selected)
                or len(selected) != len(set(selected))
            ):
                errors.append("FOCUS_SET selected_ideas 必须是唯一的非空字符串数组")
                selected = []
            if not selected:
                errors.append("FOCUS_SET.json 未选择任何 idea")
            known = {payload["id"] for _, payload in ideas}
            unknown = [item for item in selected if item not in known]
            if unknown:
                errors.append(f"FOCUS_SET 含未知 Idea：{unknown}")
            if (
                not isinstance(selected_modules, list)
                or any(
                    not isinstance(item, str) or not item
                    for item in selected_modules
                )
                or len(selected_modules) != len(set(selected_modules))
            ):
                errors.append("FOCUS_SET selected_modules 必须是唯一的非空字符串数组")
                selected_modules = []
            known_modules = {payload["id"] for _, payload in modules}
            unknown_modules = [
                item for item in selected_modules if item not in known_modules
            ]
            if unknown_modules:
                errors.append(f"FOCUS_SET 含未知 Module：{unknown_modules}")
            minimum = int(gate.get("min_focused_modules", 1))
            if len(selected_modules) < minimum:
                errors.append(
                    f"FOCUS_SET 聚焦 Module 数量不足："
                    f"{len(selected_modules)} < {minimum}"
                )
            if not is_named_human_identity(focus.get("selected_by")):
                errors.append("FOCUS_SET selected_by 必须是具名人工身份")
            if not _is_valid_review_time(focus.get("selected_at")):
                errors.append("FOCUS_SET selected_at 必须是含时区 ISO 8601")
            if not str(focus.get("rationale", "")).strip():
                errors.append("FOCUS_SET rationale 不能为空")

    if gate_id == "G3":
        ledger_path = run / "evidence" / "CLAIM_EVIDENCE_LEDGER.csv"
        rows = read_csv(ledger_path)
        errors.extend(_claim_ledger_integrity_errors(rows))
        errors.extend(_evidence_artifact_errors(run))
        claims = {row.get("claim_id", "") for row in rows if row.get("claim_id")}
        if not claims:
            errors.append("尚未登记任何论文主张")
        core_claims = {row.get("claim_id") for row in rows if is_truthy(row.get("is_core"))}
        if not core_claims:
            errors.append("CLAIM_EVIDENCE_LEDGER.csv 至少需要一个核心主张")
        for claim_id in sorted(core_claims):
            evidence_rows = [
                row for row in rows
                if row.get("claim_id") == claim_id and row.get("evidence_id") and row.get("evidence_path")
            ]
            if not evidence_rows:
                errors.append(f"核心主张 {claim_id} 尚无证据记录")
            verified_rows = [
                row for row in evidence_rows
                if row.get("verification_status") == "verified"
            ]
            complete_verified_rows = [
                row for row in verified_rows
                if is_named_human_identity(row.get("verified_by"))
                and _is_valid_review_time(row.get("verified_at"))
            ]
            if verified_rows and not complete_verified_rows:
                message = f"核心主张 {claim_id} 的 verified 证据缺少 verified_by/verified_at"
                (errors if strict else warnings).append(message)
            if not complete_verified_rows:
                message = f"核心主张 {claim_id} 尚无人工 verified 证据"
                (errors if strict else warnings).append(message)
        experiment_audit = _experiment_gate_audit(run)
        errors.extend(experiment_audit["errors"])
        empirical_artifacts = {
            *experiment_audit.get("empirical_receipt_paths", []),
            *experiment_audit.get("empirical_metric_paths", []),
        }
        for claim_id in sorted(core_claims):
            claim_rows = [row for row in rows if row.get("claim_id") == claim_id]
            if not _is_empirical_claim(claim_rows):
                continue
            bound_empirical_evidence: list[str] = []
            for row in claim_rows:
                if (
                    not str(row.get("evidence_id", "")).strip()
                    or str(row.get("evidence_type", "")).strip().lower()
                    not in EMPIRICAL_EVIDENCE_TYPES
                    or row.get("verification_status") != "verified"
                    or not is_named_human_identity(row.get("verified_by"))
                    or not _is_valid_review_time(row.get("verified_at"))
                ):
                    continue
                artifact, artifact_error = _resolve_run_artifact(
                    run, str(row.get("evidence_path", "")).strip()
                )
                if artifact_error or artifact is None:
                    continue
                relative = artifact.relative_to(run.resolve()).as_posix()
                if relative in empirical_artifacts:
                    bound_empirical_evidence.append(relative)
            if not bound_empirical_evidence:
                errors.append(
                    f"核心实证主张 {claim_id} 必须由本 claim 的人工核验 evidence 行绑定"
                    "有效 passed experiment run.json 或其 metrics artifact"
                )
        try:
            from sensitivity_engine import validate_sensitivity

            parameter_sensitivity_audit = validate_sensitivity(
                run, strict=strict, kinds=("parameter",)
            )
            errors.extend(
                f"参数敏感性审计：{item}"
                for item in parameter_sensitivity_audit.get("errors", [])
            )
            warnings.extend(
                f"参数敏感性审计：{item}"
                for item in parameter_sensitivity_audit.get("warnings", [])
            )
        except (Exception, SystemExit) as exc:
            parameter_sensitivity_audit = {
                "ok": False,
                "errors": [f"validator exception: {type(exc).__name__}: {exc}"],
                "warnings": [],
                "resolved_evidence": [],
            }
            errors.append(f"参数敏感性审计器异常：{type(exc).__name__}: {exc}")

    if gate_id == "G4":
        dispute_path = run / "disputes" / "DISPUTE_REGISTER.json"
        if dispute_path.exists():
            register = read_json(dispute_path, expect=dict)
            for item in register.get("disputes", []):
                if not isinstance(item, dict) or str(item.get("status", "")).lower() not in {
                    "resolved", "closed", "accepted-risk",
                }:
                    continue
                dispute_id = item.get("id", "unknown")
                if not is_named_human_identity(item.get("resolved_by")):
                    errors.append(f"已关闭 dispute {dispute_id} 缺少具名 resolved_by")
                if not _is_valid_review_time(item.get("resolved_at")):
                    errors.append(f"已关闭 dispute {dispute_id} 的 resolved_at 无效")
                if not str(item.get("resolution", "")).strip():
                    errors.append(f"已关闭 dispute {dispute_id} 缺少 resolution")
            critical_open = [
                item.get("id", "unknown")
                for item in register.get("disputes", [])
                if str(item.get("severity", "")).lower() == "critical"
                and str(item.get("status", "open")).lower() not in {"resolved", "closed", "accepted-risk"}
            ]
            if critical_open and gate.get("block_on_open_critical_disputes", False):
                errors.append(f"仍有未解决的 critical dispute：{critical_open}")

    if gate_id == "G5":
        if not (run / "evidence" / "CLAIM_EVIDENCE_LEDGER.csv").exists():
            errors.append("缺少 claim-evidence ledger")
        try:
            from claim_guard import lint as lint_strong_claims

            strong_claim_math_audit, _ = lint_strong_claims(
                run,
                strict=True,
                scope="math",
                persist=False,
            )
            errors.extend(
                f"数学强主张审计：{item}"
                for item in strong_claim_math_audit.get("errors", [])
            )
            warnings.extend(
                f"数学强主张审计：{item}"
                for item in strong_claim_math_audit.get("warnings", [])
            )
        except (Exception, SystemExit) as exc:
            strong_claim_math_audit = {
                "passed": False,
                "errors": [f"validator exception: {type(exc).__name__}: {exc}"],
                "warnings": [],
                "resolved_evidence": [],
            }
            errors.append(f"数学强主张审计器异常：{type(exc).__name__}: {exc}")

    if gate_id == "G6":
        manifest = run / "FROZEN_MANIFEST.json"
        if manifest.exists():
            check = verify_freeze(run, quiet=True)
            if not check["ok"]:
                errors.extend(f"冻结校验失败：{item}" for item in check["issues"])
        # v4.1: G6 is a real manuscript gate, not merely a placeholder-file check.
        try:
            from paper import validate_paper_run
            paper_check = validate_paper_run(run, strict=strict, require_final=True)
            errors.extend(f"论文检查：{item}" for item in paper_check.get("errors", []))
            warnings.extend(f"论文检查：{item}" for item in paper_check.get("warnings", []))
        except (Exception, SystemExit) as exc:
            errors.append(f"论文检查器异常：{exc}")

    if gate_id == "G7":
        errors.extend(
            f"decision log: {item}" for item in _decision_log_contract_errors(run)
        )
        egress_audit = _egress_attempt_audit(run)
        ai_audit = _ai_export_audit(run)
        ai_pdf_audit = _ai_disclosure_pdf_audit(run)
        source_audit = _source_compliance_audit(run)
        checklist_audit = _final_checklist_audit(run)
        compile_audit = _paper_compile_audit(run)
        purpose_consistency_attested = bool(
            not checklist_audit.get("errors")
            and "ai-log-completeness-reviewed"
            in checklist_audit.get("item_ids", [])
            and checklist_audit.get("reviewers")
        )
        support_audit = _official_support_archive_audit(
            run,
            ai_required=int(
                ai_audit.get("disclosure_event_count", 0) or 0
            ) > 0,
        )
        try:
            from submission_compliance import audit_submission_compliance

            submission_audit = audit_submission_compliance(
                run,
                compile_audit,
                ai_event_count=int(ai_audit.get("event_count", 0) or 0),
                ai_disclosure_count=int(
                    ai_audit.get("disclosure_event_count", 0) or 0
                ),
                ai_purpose_consistency_attested=purpose_consistency_attested,
                policy=load_config().get("compliance", {}),
            )
        except (Exception, SystemExit) as exc:
            submission_audit = {
                "errors": [f"validator exception: {type(exc).__name__}: {exc}"],
                "warnings": [],
                "input_paths": [],
                "final_pdf": {},
                "ai_statement": {},
                "anonymity": {},
            }
        compliance_audit = {
            "egress_attempts": egress_audit,
            "ai_export": ai_audit,
            "ai_disclosure_pdf": ai_pdf_audit,
            "sources": source_audit,
            "final_checklist": checklist_audit,
            "paper_compile": compile_audit,
            "submission_delivery": submission_audit,
            "official_support_archive": support_audit,
        }
        errors.extend(
            f"egress attempts: {item}" for item in egress_audit.get("errors", [])
        )
        errors.extend(f"AI export: {item}" for item in ai_audit.get("errors", []))
        errors.extend(
            f"AI disclosure PDF: {item}" for item in ai_pdf_audit.get("errors", [])
        )
        errors.extend(f"sources: {item}" for item in source_audit.get("errors", []))
        errors.extend(f"final checklist: {item}" for item in checklist_audit.get("errors", []))
        errors.extend(f"paper compile: {item}" for item in compile_audit.get("errors", []))
        errors.extend(
            f"submission delivery: {item}"
            for item in submission_audit.get("errors", [])
        )
        errors.extend(
            f"official support archive: {item}"
            for item in support_audit.get("errors", [])
        )
        warnings.extend(f"sources: {item}" for item in source_audit.get("warnings", []))
        warnings.extend(
            f"egress attempts: {item}"
            for item in egress_audit.get("warnings", [])
        )
        warnings.extend(
            f"submission delivery: {item}"
            for item in submission_audit.get("warnings", [])
        )
        warnings.extend(
            f"official support archive: {item}"
            for item in support_audit.get("warnings", [])
        )
        if ai_audit.get("event_count") == 0:
            warnings.append("AI_USAGE_LOG.jsonl 为空；AI export receipt 已绑定该人工复核结论")
        v51_status_path = run / "V51_STATUS.json"
        submission_result = next(
            (
                entry.get("result", {}) for entry in v51_results
                if entry.get("stage") == "submission"
            ),
            {},
        )
        if not v51_status_path.is_file():
            errors.append("V51_STATUS.json 不存在；必须先批准 v5.1 submission stage")
        else:
            try:
                from v51_orchestrator import (
                    approved_stage_record_errors,
                    ensure_state as ensure_v51_state,
                )

                v51_state = ensure_v51_state(run, persist=False)
                submission_state = v51_state["stages"]["submission"]
                errors.extend(
                    "v5.1 submission: " + item
                    for item in approved_stage_record_errors(
                        "submission",
                        submission_state,
                        current_result=submission_result,
                    )
                )
            except (Exception, SystemExit) as exc:
                # _validate_v51_for_gate reports the same malformed status at
                # the overlay boundary.  Keep this explicit G7 check total as
                # well, so ``harness validate`` can still emit a failed report.
                errors.append(f"v5.1 submission 状态无法校验：{exc}")

    schema_result: dict[str, Any] | None = None
    try:
        from schema_validate import validate_run as validate_run_schemas
        schema_result = validate_run_schemas(run, stage=gate_id)
        errors.extend(f"schema: {item}" for item in schema_result.get("errors", []))
    except (Exception, SystemExit) as exc:
        errors.append(f"schema: validator exception: {exc}")

    dynamic_gate_inputs = [] if experiment_audit is None else [
        *experiment_audit.get("receipt_paths", []),
        *experiment_audit.get("artifact_paths", []),
    ]
    if selection_identity_audit is not None:
        dynamic_gate_inputs.extend(selection_identity_audit.get("input_paths", []))
    if parameter_sensitivity_audit is not None:
        dynamic_gate_inputs.extend(
            str(item.get("path", ""))
            for item in parameter_sensitivity_audit.get("resolved_evidence", [])
            if isinstance(item, dict) and item.get("path")
        )
    if compliance_audit is not None:
        dynamic_gate_inputs.extend(
            compliance_audit["egress_attempts"].get("input_paths", [])
        )
        dynamic_gate_inputs.extend(compliance_audit["ai_export"].get("input_paths", []))
        dynamic_gate_inputs.extend(
            compliance_audit["ai_disclosure_pdf"].get("input_paths", [])
        )
        dynamic_gate_inputs.extend(compliance_audit["sources"].get("input_paths", []))
        dynamic_gate_inputs.extend(compliance_audit["paper_compile"].get("input_paths", []))
        dynamic_gate_inputs.extend(
            compliance_audit["submission_delivery"].get("input_paths", [])
        )
        dynamic_gate_inputs.extend(
            compliance_audit["official_support_archive"].get("input_paths", [])
        )
        dynamic_gate_inputs.append("deliverables/FINAL_CHECKLIST.md")
    if strong_claim_math_audit is not None:
        dynamic_gate_inputs.extend(
            str(item.get("path", ""))
            for item in strong_claim_math_audit.get("resolved_evidence", [])
            if isinstance(item, dict) and item.get("path")
        )
    input_manifest, input_sha256 = _gate_input_snapshot(
        run,
        gate_id,
        gate,
        v51_results,
        extra_run_paths=dynamic_gate_inputs,
        extra_source_paths=(
            [] if experiment_audit is None else experiment_audit.get("source_paths", [])
        ),
    )

    return {
        "gate": gate_id,
        "name": gate.get("name", gate_id),
        "strict": strict,
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "checked_at": now_iso(),
        "counts": {"ideas": len(ideas), "modules": len(modules)},
        "diversity_audit": diversity_audit,
        "selection_identity_audit": selection_identity_audit,
        "experiment_audit": experiment_audit,
        "parameter_sensitivity_audit": parameter_sensitivity_audit,
        "strong_claim_math_audit": strong_claim_math_audit,
        "compliance_audit": compliance_audit,
        "v51": v51_results,
        "schema": schema_result,
        "input_manifest": input_manifest,
        "input_sha256": input_sha256,
    }


def cmd_validate(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    config = load_config()
    gates = _gate_order(config) if args.gate == "all" else [args.gate]
    overall_ok = True
    payloads = []
    for gate_id in gates:
        result = validate_gate(run, gate_id, strict=args.strict)
        payloads.append(result)
        overall_ok &= result["ok"]
        mark = "OK" if result["ok"] else "FAIL"
        print(f"[{mark}] {gate_id} — {result['name']}")
        for item in result["errors"]:
            print(f"  ERROR: {item}")
        for item in result["warnings"]:
            print(f"  WARN : {item}")
    write_json(run / "VALIDATION_REPORT.json", {"run_id": run.name, "results": payloads, "ok": overall_ok})
    return 0 if overall_ok else 3


def _write_current_gate_review(
    run: Path, result: dict[str, Any]
) -> dict[str, Any]:
    """Render and hash-bind the deterministic human review for one Gate."""
    from gate_review import verify_gate_review, write_gate_review

    binding = write_gate_review(run, result)
    review_errors = verify_gate_review(run, result, binding=binding)
    if review_errors:
        die(
            "无法生成 canonical Gate 人工审核摘要："
            + "; ".join(review_errors)
        )
    result["review_report"] = binding
    return binding


def _current_gate_review_binding(
    run: Path, result: dict[str, Any]
) -> tuple[dict[str, Any] | None, list[str]]:
    """Read and validate an existing Gate review without modifying it."""
    from gate_review import gate_review_binding, verify_gate_review

    binding: dict[str, Any] | None = None
    binding_error: str | None = None
    try:
        binding = gate_review_binding(run, result)
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        binding_error = f"无法读取 Gate 人工审核摘要 binding：{exc}"
    try:
        errors = verify_gate_review(run, result, binding=binding)
    except (Exception, SystemExit) as exc:
        errors = [
            "Gate 人工审核摘要校验器异常："
            f"{type(exc).__name__}: {exc}"
        ]
    if binding_error and not errors:
        errors.append(binding_error)
    if errors:
        return None, errors
    if binding is None:
        return None, [binding_error or "Gate 人工审核摘要 binding 缺失"]
    return binding, []


def cmd_review(args: argparse.Namespace) -> int:
    """Generate a concise human decision report from strict Gate facts."""
    run = resolve_run(args.run)
    result = validate_gate(run, args.gate, strict=True)
    binding = _write_current_gate_review(run, result)
    state = "可批准" if result["ok"] else "暂不可批准"
    print(f"已生成 {args.gate} 人工审核摘要（{state}）：{binding['path']}")
    print(f"底层输入 SHA-256：{binding['underlying_input_sha256']}")
    print(f"摘要 SHA-256：{binding['sha256']}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    meta = load_run_meta(run)
    status = load_status(run)
    config = load_config()
    print(f"Run       : {meta['run_id']}")
    print(f"Contest   : {meta.get('contest')}")
    print(f"Problem   : {meta.get('problem')}")
    print(f"Phase     : {meta.get('phase')}")
    print(f"Runtime   : {meta.get('runtime', load_budget(run).get('agent_runtime', {}).get('primary', 'codex'))}")
    print(f"Current   : {status.get('current_gate')}")
    print(f"Frozen    : {status.get('frozen')}")
    print(f"Ideas     : {len(load_idea_cards(run))}")
    print(f"Modules   : {len(load_module_cards(run))}")
    print("\nGates:")
    for gate_id in _gate_order(config):
        item = status.get("gates", {}).get(gate_id, {})
        print(
            f"  {gate_id} {item.get('status', 'pending'):>9}  "
            f"{config['gates'][gate_id].get('name', gate_id)}"
        )
    return 0


def cmd_active(args: argparse.Namespace) -> int:
    run_id = active_run_id()
    print(run_id)
    print(RUNS_ROOT / run_id)
    return 0


def _remaining_hours(budget: dict[str, Any]) -> float | None:
    deadline = budget.get("time", {}).get("deadline")
    if not deadline:
        return None
    try:
        end = _parse_datetime(deadline)
    except SystemExit:
        return None
    return (end - datetime.now().astimezone()).total_seconds() / 3600


def cmd_budget_status(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    budget = load_budget(run)
    remaining = _remaining_hours(budget)
    print(f"Deadline              : {budget['time'].get('deadline')}")
    print(f"Time remaining (hours): {remaining:.2f}" if remaining is not None else "Time remaining: unknown")
    print(
        f"Human review          : {budget['human_review'].get('used_hours', 0)} / "
        f"{budget['human_review'].get('max_hours', 0)} h"
    )
    print(
        f"Manual Pro dialogues  : {budget['manual_pro'].get('used_dialogues', 0)} / "
        f"{budget['manual_pro'].get('max_dialogues', 0)}"
    )
    print(
        f"Exploration batches   : {budget['exploration'].get('used_batches', 0)} / "
        f"{budget['exploration'].get('max_batches', 0)}"
    )
    runtime_cfg = budget.get("agent_runtime", {})
    print(f"Primary runtime       : {runtime_cfg.get('primary', 'codex')}")
    print(f"Agent concurrency     : {runtime_cfg.get('max_concurrent_agents', budget.get('codex', {}).get('max_concurrent_threads', 4))}")
    providers = budget.get("api_providers", {})
    for provider_name in ("openai", "anthropic", "deepseek"):
        item = providers.get(provider_name, {})
        if not item and provider_name == "openai":
            item = budget.get("openai_api", {})
        print(
            f"{provider_name.title():<22}: {item.get('enabled', False)} / "
            f"${float(item.get('used_usd', 0)):.2f} of ${float(item.get('max_usd', 0)):.2f}"
        )
    return 0


def cmd_budget_log(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    budget = load_budget(run)
    kind = args.kind
    amount = float(args.amount)
    if amount < 0:
        die("amount 不能为负数")
    if kind == "human-hours":
        budget["human_review"]["used_hours"] = round(float(budget["human_review"].get("used_hours", 0)) + amount, 3)
    elif kind == "manual-pro-dialogues":
        budget["manual_pro"]["used_dialogues"] = int(budget["manual_pro"].get("used_dialogues", 0)) + int(amount)
    elif kind == "exploration-batches":
        budget["exploration"]["used_batches"] = int(budget["exploration"].get("used_batches", 0)) + int(amount)
    elif kind in {"api-usd", "openai-api-usd", "anthropic-api-usd", "deepseek-api-usd"}:
        provider_name = {
            "api-usd": "openai",
            "openai-api-usd": "openai",
            "anthropic-api-usd": "anthropic",
            "deepseek-api-usd": "deepseek",
        }[kind]
        providers = budget.setdefault("api_providers", {})
        item = providers.setdefault(provider_name, {"enabled": False, "max_usd": 0.0, "used_usd": 0.0})
        item["used_usd"] = round(float(item.get("used_usd", 0)) + amount, 6)
        if provider_name == "openai":
            budget["openai_api"]["used_usd"] = item["used_usd"]
    elif kind in {"codex-tasks", "agent-tasks"}:
        budget.setdefault("agent_runtime", {})["logged_tasks"] = int(budget.get("agent_runtime", {}).get("logged_tasks", 0)) + int(amount)
        if kind == "codex-tasks":
            budget["codex"]["logged_tasks"] = int(budget["codex"].get("logged_tasks", 0)) + int(amount)
    else:
        die(f"未知预算类型：{kind}")
    save_budget(run, budget)
    append_jsonl(
        run / "decisions" / "DECISION_LOG.jsonl",
        {"timestamp": now_iso(), "type": "budget-log", "kind": kind, "amount": amount, "by": args.by, "note": args.note},
    )
    return cmd_budget_status(argparse.Namespace(run=args.run))


def cmd_approve(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    reviewer = _require_named_human(args.by, action="Gate approval --by")
    config = load_config()
    status = load_status(run)
    status_errors = _status_contract_errors(run, status, config)
    if status_errors:
        die("STATUS.json 非 canonical，拒绝批准：" + "; ".join(status_errors))
    gate_index = _gate_index(config, args.gate)
    order = _gate_order(config)
    missing_prerequisites = [
        gate_id
        for gate_id in order[:gate_index]
        if status.get("gates", {}).get(gate_id, {}).get("status") != "approved"
    ]
    if missing_prerequisites and not args.force:
        die(
            "不能跳过未批准的前置 Gate：" + ", ".join(missing_prerequisites)
            + "。先完成前置 Gate，或明确使用 --force 并记录理由。"
        )
    # Enforce the human decision boundary before *any* approval-triggered
    # status/log mutation, including automatic rollback of stale prerequisite
    # Gates.  ``approve`` may inspect the current Gate and its existing report,
    # but it must never create/repair that report or change state when the
    # report is missing, stale or tampered.
    result = validate_gate(run, args.gate, strict=True)
    review_binding, review_errors = _current_gate_review_binding(run, result)
    if review_binding is not None:
        result["review_report"] = review_binding
    if review_errors or review_binding is None:
        detail = "; ".join(review_errors) or "审核摘要 binding 缺失"
        die(
            f"{args.gate} 现有人工审核摘要不可绑定：{detail}。"
            "approve 不会创建、修复或更新摘要，也不会改写 Gate 状态；请先运行 "
            f"`python tools/harness.py review --gate {args.gate}`，审核后再批准。"
        )
    prerequisite_revalidation: list[dict[str, Any]] = []
    stale_prerequisites: list[str] = []
    for prerequisite in order[:gate_index]:
        prerequisite_item = status.get("gates", {}).get(prerequisite, {})
        if prerequisite_item.get("status") != "approved":
            continue
        fresh = validate_gate(run, prerequisite, strict=True)
        approved_validation = prerequisite_item.get("validation") or {}
        approved_input_sha256 = approved_validation.get("input_sha256")
        input_matches = bool(
            approved_input_sha256
            and approved_input_sha256 == fresh.get("input_sha256")
        )
        approval_errors = _gate_approval_record_errors(
            prerequisite,
            prerequisite_item,
            run=run,
            current_result=fresh,
        )
        prerequisite_revalidation.append({
            "gate": prerequisite,
            "approved_input_sha256": approved_input_sha256,
            "current_input_sha256": fresh.get("input_sha256"),
            "input_matches": input_matches,
            "approval_errors": approval_errors,
            "validation": fresh,
        })
        if not fresh["ok"] or not input_matches or approval_errors:
            stale_prerequisites.append(prerequisite)
    if stale_prerequisites and not args.force:
        first_stale_index = min(order.index(gate_id) for gate_id in stale_prerequisites)
        for affected_gate in order[first_stale_index:]:
            affected_item = status["gates"][affected_gate]
            if affected_gate in stale_prerequisites:
                fresh = next(
                    entry["validation"]
                    for entry in prerequisite_revalidation
                    if entry["gate"] == affected_gate
                )
                affected_item.update({
                    "status": "stale",
                    "approved_at": None,
                    "approved_by": None,
                    "note": "invalidated by prerequisite revalidation",
                    "validation": fresh,
                })
            elif (
                affected_item.get("status") != "pending"
                or affected_item.get("validation") is not None
            ):
                affected_item.update({
                    "status": "pending",
                    "approved_at": None,
                    "approved_by": None,
                    "note": "invalidated by stale upstream gate",
                    "validation": None,
                })
        status["current_gate"] = order[first_stale_index]
        save_status(run, status)
        append_jsonl(
            run / "decisions" / "DECISION_LOG.jsonl",
            {
                "timestamp": now_iso(),
                "type": "gate-stale-detected",
                "while_approving": args.gate,
                "stale_gates": stale_prerequisites,
                "by": args.by,
                "revalidation": prerequisite_revalidation,
            },
        )
        print(json.dumps({"stale_prerequisites": prerequisite_revalidation}, ensure_ascii=False, indent=2))
        die("已批准的前置 Gate 产物发生变化；状态已回退到最早失效 Gate。")
    result["prerequisite_revalidation"] = prerequisite_revalidation
    existing_item = status.get("gates", {}).get(args.gate, {})
    if (
        args.gate == "G7"
        and isinstance(existing_item, dict)
        and existing_item.get("status") == "approved"
        and not _gate_approval_record_errors(
            "G7",
            existing_item,
            run=run,
            current_result=result,
        )
    ):
        print(
            "G7 已是当前批准状态；未追加重复审批记录。"
            "若需更换复核人或备注，请先 revoke G7。"
        )
        return 0
    if not result["ok"]:
        item = status["gates"][args.gate]
        item.update({
            "status": "stale",
            "approved_at": None,
            "approved_by": None,
            "note": "approval blocked because current strict validation failed",
            "validation": result,
        })
        for downstream in order[gate_index + 1:]:
            downstream_item = status["gates"][downstream]
            downstream_item.update({
                "status": "pending",
                "approved_at": None,
                "approved_by": None,
                "note": f"invalidated because {args.gate} strict validation failed",
                "validation": None,
            })
        status["current_gate"] = args.gate
        save_status(run, status)
        append_jsonl(
            run / "decisions" / "DECISION_LOG.jsonl",
            {
                "timestamp": now_iso(),
                "type": "gate-approval-blocked",
                "gate": args.gate,
                "by": reviewer,
                "force_requested": bool(args.force),
                "note": args.note,
                "validation": result,
                "missing_prerequisites": missing_prerequisites,
                "stale_prerequisites": stale_prerequisites,
            },
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        warn(
            "Gate 当前 strict 校验未通过；已记录 stale/validation 并阻止批准。"
            "--force 只能跳过前置 Gate 顺序，不能绕过当前 Gate 校验。"
            + (
                f"请查看已验证摘要 {review_binding['path']}。"
                if review_binding is not None
                else "approve 未创建或修复审核摘要；请运行 "
                f"`python tools/harness.py review --gate {args.gate}` 后查看。"
            )
        )
        return 3
    item = status["gates"][args.gate]
    item.update(
        {
            "status": "approved",
            "approved_at": now_iso(),
            "approved_by": reviewer,
            "note": args.note or (
                "forced despite prerequisite gate order/state"
                if args.force and (missing_prerequisites or stale_prerequisites)
                else ""
            ),
            "validation": result,
        }
    )
    index = gate_index
    for downstream in order[index + 1:]:
        downstream_item = status["gates"][downstream]
        if downstream_item.get("status") != "pending" or downstream_item.get("validation") is not None:
            downstream_item.update({
                "status": "pending", "approved_at": None, "approved_by": None,
                "note": f"invalidated by {args.gate} approval", "validation": None,
            })
    status["current_gate"] = order[index + 1] if index + 1 < len(order) else args.gate
    save_status(run, status)
    append_jsonl(
        run / "decisions" / "DECISION_LOG.jsonl",
        {
            "timestamp": now_iso(),
            "type": "gate-approval",
            "gate": args.gate,
            "by": reviewer,
            "forced": bool(args.force),
            "strict": True,
            "note": args.note,
            "validation": result,
            "missing_prerequisites_at_approval": missing_prerequisites,
            "stale_prerequisites_at_approval": stale_prerequisites,
            "prerequisite_revalidation": prerequisite_revalidation,
        },
    )
    print(
        f"已批准 {args.gate}，并绑定 {review_binding['path']}。"
        f"当前 Gate：{status['current_gate']}"
    )
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    reviewer = _require_named_human(args.by, action="Gate revoke --by")
    config = load_config()
    status = load_status(run)
    index = _gate_index(config, args.gate)
    order = _gate_order(config)
    for gate_id in order[index:]:
        status["gates"][gate_id].update(
            {"status": "pending", "approved_at": None, "approved_by": None, "note": "revoked", "validation": None}
        )
    status["current_gate"] = args.gate
    archived_manifest = None
    if status.get("frozen"):
        status["frozen"] = False
        status["frozen_at"] = None
        status["freeze_manifest"] = None
        manifest = run / "FROZEN_MANIFEST.json"
        if manifest.exists():
            archive = run / "decisions" / f"FROZEN_MANIFEST.revoked.{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
            shutil.move(str(manifest), str(archive))
            archived_manifest = relative_to_run(archive, run)
    save_status(run, status)
    append_jsonl(
        run / "decisions" / "DECISION_LOG.jsonl",
        {"timestamp": now_iso(), "type": "gate-revoke", "gate": args.gate, "by": reviewer, "note": args.note, "archived_manifest": archived_manifest},
    )
    print(f"已撤销 {args.gate} 及其后续 Gate。")
    return 0


def _idea_registry(run: Path) -> tuple[Path, dict[str, Any]]:
    path = run / "ideas" / "IDEA_REGISTRY.json"
    if not path.exists():
        write_json(path, {"next_id": 1, "ideas": []})
    return path, read_json(path, expect=dict)


def _card_markdown(card: dict[str, Any]) -> str:
    def bullet(values: Iterable[Any]) -> str:
        values = list(values or [])
        return "\n".join(f"- {value}" for value in values) if values else "- TODO"

    return f"""# {card['id']} — {card['title']}

## 目标问题节点

{bullet(card.get('target_nodes', []))}

## 核心思想

{card.get('summary', '')}

## 方法族

{bullet(card.get('method_families', []))}

## 关键假设

{bullet(card.get('core_assumptions', []))}

## 数学对象

{bullet(card.get('mathematical_objects', []))}

## 数据需求

{bullet(card.get('data_requirements', []))}

## 验证计划

{bullet(card.get('validation_plan', []))}

## 可证伪测试

{card.get('falsification_test', '') or 'TODO'}

## 最大风险

{bullet(card.get('main_risks', []))}

## 当前状态

`{card.get('status', 'proposed')}`
"""


def cmd_idea_new(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    requested_id = (
        _require_canonical_path_component(args.id, label="idea-new --id")
        if args.id is not None
        else None
    )
    path, registry = _idea_registry(run)
    existing = [item.get("id", "") for item in registry.get("ideas", [])]
    idea_id = requested_id or next_numeric_id("IDEA", existing)
    if idea_id in existing or (run / "research" / "ideas" / idea_id).exists():
        die(f"Idea 已存在：{idea_id}")
    card = {
        "schema_version": 1,
        "id": idea_id,
        "title": args.title,
        "author_resource": args.source,
        "target_nodes": args.target_nodes or [],
        "summary": args.summary or "TODO: 至少用一句可证伪的语言描述核心思想。",
        "method_families": args.families or ["baseline"],
        "core_assumptions": args.assumptions or [],
        "mathematical_objects": [],
        "data_requirements": [],
        "validation_plan": args.validation or [],
        "falsification_test": args.falsification or "",
        "main_risks": args.risks or [],
        "estimated_effort_hours": float(args.effort_hours),
        "novelty_relative_to_pool": "unknown",
        "parent_ids": args.parent_ids or [],
        "created_at": now_iso(),
        "status": "proposed",
    }
    idea_dir = run / "research" / "ideas" / idea_id
    idea_dir.mkdir(parents=True)
    write_json(idea_dir / "IDEA_CARD.json", card)
    atomic_write_text(idea_dir / "IDEA_CARD.md", _card_markdown(card))
    registry.setdefault("ideas", []).append(
        {
            "id": idea_id,
            "title": args.title,
            "path": relative_to_run(idea_dir / "IDEA_CARD.json", run),
            "source": args.source,
            "status": "proposed",
            "created_at": card["created_at"],
        }
    )
    registry["next_id"] = max(int(registry.get("next_id", 1)), int(idea_id.split("_")[-1]) + 1 if idea_id.split("_")[-1].isdigit() else 1)
    write_json(path, registry)
    append_jsonl(
        run / "decisions" / "DECISION_LOG.jsonl",
        {"timestamp": now_iso(), "type": "idea-created", "idea_id": idea_id, "by": args.by, "source": args.source},
    )
    print(idea_id)
    print(idea_dir)
    return 0


def cmd_idea_list(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    cards = load_idea_cards(run)
    if not cards:
        print("尚无 Idea Card。")
        return 0
    for path, card in cards:
        print(
            f"{card.get('id'):>10}  {card.get('status', 'unknown'):<10}  "
            f"[{', '.join(card.get('method_families', []))}]  {card.get('title')}  ({relative_to_run(path, run)})"
        )
    return 0


def _module_markdown(card: dict[str, Any]) -> str:
    return f"""# {card['id']} — {card.get('purpose', '')}

## 来源 Idea

{', '.join(card.get('source_ideas', [])) or 'TODO'}

## 目标节点

{', '.join(card.get('target_nodes', [])) or 'TODO'}

## 变量、单位与接口

```json
{json.dumps(card.get('interfaces', {}), ensure_ascii=False, indent=2)}
```

## 方程或算法

{card.get('equations_or_algorithm', '') or 'TODO'}

## 参数估计

{card.get('parameter_estimation', '') or 'TODO'}

## Baseline

{card.get('baseline', '') or 'TODO'}

## 验证协议

{chr(10).join('- ' + str(x) for x in card.get('validation_protocol', [])) or '- TODO'}

## 失败模式

{chr(10).join('- ' + str(x) for x in card.get('failure_modes', [])) or '- TODO'}
"""


def cmd_module_new(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    requested_id = (
        _require_canonical_path_component(args.id, label="module-new --id")
        if args.id is not None
        else None
    )
    registry_path = run / "research" / "MODULE_REGISTRY.json"
    registry = read_json(registry_path, expect=dict) if registry_path.exists() else {"next_id": 1, "modules": []}
    existing = [item.get("id", "") for item in registry.get("modules", [])]
    module_id = requested_id or next_numeric_id("MOD", existing)
    if module_id in existing or (run / "research" / "modules" / module_id).exists():
        die(f"Module 已存在：{module_id}")
    idea_ids = {card["id"] for _, card in load_idea_cards(run)}
    missing = [item for item in args.source_ideas if item not in idea_ids]
    if missing:
        die(f"未知 source idea：{missing}")
    card = {
        "schema_version": 1,
        "id": module_id,
        "source_ideas": args.source_ideas,
        "target_nodes": args.target_nodes or [],
        "purpose": args.purpose,
        "variables_and_units": [],
        "equations_or_algorithm": args.algorithm or "",
        "parameter_estimation": "",
        "interfaces": {"inputs": [], "outputs": []},
        "baseline": args.baseline or "",
        "implementation_path": "",
        "validation_protocol": args.validation or [],
        "failure_modes": args.failure_modes or [],
        "evidence_ids": [],
        "human_owner": args.owner or "",
        "created_at": now_iso(),
        "status": "planned",
    }
    module_dir = run / "research" / "modules" / module_id
    module_dir.mkdir(parents=True)
    write_json(module_dir / "MODULE_CARD.json", card)
    atomic_write_text(module_dir / "MODULE_CARD.md", _module_markdown(card))
    registry.setdefault("modules", []).append(
        {
            "id": module_id,
            "purpose": args.purpose,
            "path": relative_to_run(module_dir / "MODULE_CARD.json", run),
            "status": "planned",
            "created_at": card["created_at"],
        }
    )
    registry["next_id"] = int(registry.get("next_id", 1)) + 1
    write_json(registry_path, registry)
    append_jsonl(
        run / "decisions" / "DECISION_LOG.jsonl",
        {"timestamp": now_iso(), "type": "module-created", "module_id": module_id, "source_ideas": args.source_ideas, "by": args.by},
    )
    print(module_id)
    print(module_dir)
    return 0


def cmd_module_list(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    cards = load_module_cards(run)
    if not cards:
        print("尚无 Module Card。")
        return 0
    for path, card in cards:
        print(
            f"{card.get('id'):>10}  {card.get('status', 'unknown'):<10}  "
            f"{card.get('purpose')}  ({relative_to_run(path, run)})"
        )
    return 0


def cmd_focus_set(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    reviewer = _require_named_human(args.by, action="focus-set --by")
    known_ideas = {card["id"] for _, card in load_idea_cards(run)}
    unknown = [item for item in args.ideas if item not in known_ideas]
    if unknown:
        die(f"未知 Idea：{unknown}")
    known_modules = {card["id"] for _, card in load_module_cards(run)}
    unknown_modules = [item for item in (args.modules or []) if item not in known_modules]
    if unknown_modules:
        die(f"未知 Module：{unknown_modules}")
    payload = {
        "selected_ideas": args.ideas,
        "selected_modules": args.modules or [],
        "selected_by": reviewer,
        "selected_at": now_iso(),
        "evidence": args.evidence or [],
        "rationale": args.rationale,
        "rejected_or_deferred": args.deferred or [],
    }
    write_json(run / "selection" / "FOCUS_SET.json", payload)
    append_jsonl(
        run / "decisions" / "DECISION_LOG.jsonl",
        {"timestamp": now_iso(), "type": "focus-set", **payload},
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _dispute_register(run: Path) -> tuple[Path, dict[str, Any]]:
    path = run / "disputes" / "DISPUTE_REGISTER.json"
    if not path.exists():
        write_json(path, {"next_id": 1, "disputes": []})
    return path, read_json(path, expect=dict)


def cmd_dispute_add(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    requested_id = (
        _require_canonical_path_component(args.id, label="dispute-add --id")
        if args.id is not None
        else None
    )
    path, register = _dispute_register(run)
    existing = [item.get("id", "") for item in register.get("disputes", [])]
    dispute_id = requested_id or next_numeric_id("DSP", existing)
    item = {
        "id": dispute_id,
        "title": args.title,
        "type": args.type,
        "severity": args.severity,
        "target": args.target,
        "question": args.question,
        "status": "open",
        "created_at": now_iso(),
        "created_by": args.by,
        "assigned_resource": args.resource or None,
        "resolution": None,
        "evidence": [],
        "resolved_at": None,
        "resolved_by": None,
    }
    register.setdefault("disputes", []).append(item)
    register["next_id"] = int(register.get("next_id", 1)) + 1
    write_json(path, register)
    issue_dir = run / "research" / "issues" / dispute_id
    issue_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        issue_dir / "ISSUE_PROMPT.md",
        f"# {dispute_id} — {args.title}\n\nTarget: `{args.target}`\n\n## Question\n\n{args.question}\n\n"
        "Provide a falsifiable resolution plan, evidence requirements and stop condition.\n",
    )
    print(dispute_id)
    return 0


def cmd_dispute_resolve(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    reviewer = _require_named_human(args.by, action="dispute-resolve --by")
    path, register = _dispute_register(run)
    match = None
    for item in register.get("disputes", []):
        if item.get("id") == args.id:
            match = item
            break
    if match is None:
        die(f"找不到 dispute：{args.id}")
    match.update(
        {
            "status": args.status,
            "resolution": args.resolution,
            "evidence": args.evidence or [],
            "resolved_at": now_iso(),
            "resolved_by": reviewer,
        }
    )
    write_json(path, register)
    append_jsonl(
        run / "decisions" / "DECISION_LOG.jsonl",
        {"timestamp": now_iso(), "type": "dispute-resolved", "id": args.id, "status": args.status, "by": reviewer, "evidence": args.evidence or []},
    )
    print(f"已更新 {args.id} -> {args.status}")
    return 0


def _claim_rows(run: Path) -> tuple[Path, list[dict[str, str]]]:
    path = run / "evidence" / "CLAIM_EVIDENCE_LEDGER.csv"
    if not path.exists():
        write_csv(path, CLAIM_FIELDS, [])
    return path, read_csv(path, expected_fields=CLAIM_FIELDS)


def cmd_claim_add(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    path, rows = _claim_rows(run)
    known = {row.get("claim_id") for row in rows}
    claim_id = args.id or next_numeric_id("CLAIM", [item or "" for item in known])
    if claim_id in known:
        die(f"Claim 已存在：{claim_id}")
    rows.append(
        {
            "claim_id": claim_id,
            "claim_text": args.claim,
            "claim_type": args.type,
            "is_core": "true" if args.core else "false",
            "evidence_id": "",
            "evidence_type": "",
            "evidence_path": "",
            "generation_command": "",
            "result_summary": "",
            "verification_status": "pending",
            "verified_by": "",
            "verified_at": "",
            "paper_location": args.paper_section or "",
            "notes": args.note or "",
        }
    )
    write_csv(path, CLAIM_FIELDS, rows)
    print(claim_id)
    return 0


def cmd_evidence_add(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    path, rows = _claim_rows(run)
    claims = [row for row in rows if row.get("claim_id") == args.claim]
    if not claims:
        die(f"Claim 不存在：{args.claim}")
    claim_header = claims[0]
    existing_ids = [row.get("evidence_id", "") for row in rows]
    evidence_id = args.id or next_numeric_id("EVID", existing_ids)
    if evidence_id in existing_ids:
        die(f"Evidence 已存在：{evidence_id}")
    artifact = None
    if args.artifact:
        artifact, artifact_error = _resolve_run_artifact(run, args.artifact)
        if artifact_error:
            die(artifact_error)
    if args.human_verified and (
        not str(args.by or "").strip()
        or str(args.by).strip().upper() in PLACEHOLDER_REVIEWERS
    ):
        die("--human-verified 必须通过 --by 记录具名核验人，不能使用 TEAM/UNKNOWN/TODO")
    rows.append(
        {
            "claim_id": args.claim,
            "claim_text": claim_header.get("claim_text", ""),
            "claim_type": claim_header.get("claim_type", ""),
            "is_core": claim_header.get("is_core", "false"),
            "evidence_id": evidence_id,
            "evidence_type": args.type,
            "evidence_path": relative_to_run(artifact, run) if artifact else "",
            "generation_command": args.command or "",
            "result_summary": args.result,
            "verification_status": "verified" if args.human_verified else "unverified",
            "verified_by": args.by if args.human_verified else "",
            "verified_at": now_iso() if args.human_verified else "",
            "paper_location": claim_header.get("paper_location", ""),
            "notes": args.note or "",
        }
    )
    write_csv(path, CLAIM_FIELDS, rows)
    print(evidence_id)
    return 0


def cmd_ai_log(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    if args.adopted and not args.verified:
        die("ai-log --adopted 必须同时指定 --verified，并通过 --by 记录具名核验人")
    if args.verified and not is_named_human_identity(args.by):
        die("ai-log --verified 必须通过 --by 记录具名核验人")
    event_id = append_ai_usage_event(
        run,
        event_id=args.id,
        tool=args.tool,
        model=args.model,
        stage=args.stage,
        purpose=args.purpose,
        prompt_or_task_summary=args.prompt_summary,
        input_artifacts=args.input or [],
        output_artifacts=args.output or [],
        adopted=bool(args.adopted),
        human_modified=bool(args.modified),
        human_verified=bool(args.verified),
        verified_by=args.by,
        notes=args.note or "",
    )
    print(event_id)
    return 0


def cmd_source_add(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    reviewer = str(args.by or "").strip()
    if not reviewer or reviewer.upper() in PLACEHOLDER_REVIEWERS:
        die("source-add --by 必须是具名核验人，不能使用 TEAM/UNKNOWN/TODO 等占位值")
    path = run / "sources" / "SOURCE_LEDGER.csv"
    rows = read_csv(path, expected_fields=SOURCE_FIELDS)
    source_id = args.id or next_numeric_id("SRC", [row.get("source_id", "") for row in rows])
    if any(row.get("source_id") == source_id for row in rows):
        die(f"Source 已存在：{source_id}")
    rows.append(
        {
            "source_id": source_id,
            "title": args.title,
            "author_or_org": args.author,
            "year": args.year,
            "url": args.url,
            "source_type": args.type,
            "claim_supported": args.claim,
            "accessed_at": now_iso(),
            "verified_by": reviewer,
            "notes": args.note or "",
        }
    )
    write_csv(path, SOURCE_FIELDS, rows)
    print(source_id)
    return 0


def cmd_source_attest_none(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    ledger_path = run / "sources" / "SOURCE_LEDGER.csv"
    if not ledger_path.is_file():
        die("缺少 sources/SOURCE_LEDGER.csv")
    rows = read_csv(ledger_path, expected_fields=SOURCE_FIELDS)
    if rows:
        die("SOURCE_LEDGER.csv 已有来源记录，不能声明 no-source")
    reviewer = str(args.by or "").strip()
    reason = str(args.reason or "").strip()
    if not reviewer:
        die("source-attest-none 必须记录 --by")
    if reviewer.upper() in PLACEHOLDER_REVIEWERS:
        die("source-attest-none --by 必须是具名核验人，不能使用 TEAM/UNKNOWN/TODO")
    if len(reason) < 10:
        die("source-attest-none --reason 至少 10 个字符，并具体说明为何无外部来源")
    payload = {
        "schema_version": 1,
        "run_id": run.name,
        "attested_at": now_iso(),
        "attested_by": reviewer,
        "reason": reason,
        "source_count": 0,
        "source_ledger_path": "sources/SOURCE_LEDGER.csv",
        "source_ledger_sha256": sha256_file(ledger_path),
    }
    output = run / NO_SOURCE_ATTESTATION_RELATIVE
    write_json(output, payload)
    print(output)
    return 0


def _freeze_records(run: Path) -> list[dict[str, Any]]:
    config = load_config()
    freeze = config.get("freeze", {})
    include = freeze.get("include", [])
    exclude_names = set(freeze.get("exclude_names", []))
    records: list[dict[str, Any]] = []
    run_root = run.resolve()
    seen_paths: set[str] = set()
    for relative in include:
        base = (run / relative).resolve()
        try:
            base_relative = base.relative_to(run_root).as_posix()
        except ValueError:
            die(f"冻结范围离开当前 run：{relative}")
        requested_relative = Path(str(relative).replace("\\", "/")).as_posix().rstrip("/")
        if base_relative != requested_relative:
            die(f"冻结范围使用了符号链接或非 canonical 路径：{relative}")
        if not base.exists():
            continue
        for path in iter_files(base, exclude_names=exclude_names):
            lexical_relative = path.relative_to(run).as_posix()
            resolved = path.resolve()
            try:
                canonical_relative = resolved.relative_to(run_root).as_posix()
            except ValueError:
                die(f"冻结范围含 run 外文件或符号链接：{path}")
            if canonical_relative != lexical_relative:
                die(f"冻结范围含符号链接或非 canonical 文件：{lexical_relative}")
            if canonical_relative in seen_paths:
                die(f"冻结范围含重复 canonical 文件：{canonical_relative}")
            seen_paths.add(canonical_relative)
            records.append(
                {
                    "path": canonical_relative,
                    "size": resolved.stat().st_size,
                    "sha256": sha256_file(resolved),
                }
            )
    return sorted(records, key=lambda item: item["path"])


def _approved_gate_freshness(
    run: Path, gate_ids: Iterable[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Strictly revalidate approved gates and compare their bound input hashes."""
    status = load_status(run)
    checks: list[dict[str, Any]] = []
    config = load_config()
    errors: list[str] = _status_contract_errors(run, status, config)
    if errors:
        return checks, errors
    for gate_id in gate_ids:
        item = status.get("gates", {}).get(gate_id, {})
        if item.get("status") != "approved":
            errors.append(f"{gate_id} 尚未批准")
            checks.append({"gate": gate_id, "status": item.get("status", "missing")})
            continue
        approved_hash = (item.get("validation") or {}).get("input_sha256")
        try:
            current = validate_gate(run, gate_id, strict=True)
        except (Exception, SystemExit) as exc:
            errors.append(f"{gate_id} strict 重验异常：{exc}")
            checks.append({
                "gate": gate_id, "status": "validation-exception",
                "approved_input_sha256": approved_hash,
            })
            continue
        current_hash = current.get("input_sha256")
        input_matches = bool(approved_hash and approved_hash == current_hash)
        approval_errors = _gate_approval_record_errors(
            gate_id,
            item,
            run=run,
            current_result=current,
        )
        approval_current = not approval_errors
        checks.append({
            "gate": gate_id,
            "status": (
                "current"
                if current.get("ok") and input_matches and approval_current
                else "stale"
            ),
            "approved_input_sha256": approved_hash,
            "current_input_sha256": current_hash,
            "validation_ok": bool(current.get("ok")),
            "input_matches": input_matches,
        })
        errors.extend(approval_errors)
        if not current.get("ok"):
            errors.append(f"{gate_id} strict 重验失败")
        if not input_matches:
            errors.append(f"{gate_id} 批准输入哈希缺失或已变化")
    return checks, errors


def cmd_freeze(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    reviewer = _require_named_human(args.by, action="freeze --by")
    config = load_config()
    status = load_status(run)
    gate_order = _gate_order(config)
    g5_index = gate_order.index("G5")
    prerequisite_freshness, prerequisite_errors = _approved_gate_freshness(
        run, gate_order[:g5_index]
    )
    if prerequisite_errors:
        die(
            "科学冻结要求 G0-G4 全部批准、strict 重验通过且批准输入哈希仍匹配；"
            "--allow-before-g5 不能绕过前置 Gate 漂移："
            + "; ".join(prerequisite_errors)
        )
    g5_freshness, g5_errors = _approved_gate_freshness(run, ["G5"])
    g5_state = status.get("gates", {}).get("G5", {})
    g5_status = g5_state.get("status")
    g5_is_approved = g5_status == "approved"
    if g5_is_approved and g5_errors:
        die(
            "G5 已批准但 strict 重验失败或批准输入已漂移；该错误不可通过 "
            "--allow-before-g5 绕过：" + "; ".join(g5_errors)
        )
    decision_path = run / "decisions" / "DECISION_LOG.jsonl"
    g5_was_approved = any(
        event.get("type") == "gate-approval" and event.get("gate") == "G5"
        for event in (read_jsonl(decision_path) if decision_path.is_file() else [])
    )
    before_g5_exception = (
        not g5_is_approved
        and g5_status == "pending"
        and not g5_was_approved
        and not g5_state.get("approved_at")
        and not g5_state.get("approved_by")
    )
    if not g5_is_approved and not before_g5_exception:
        die(
            "--allow-before-g5 只适用于从未批准且当前为 pending 的 G5；"
            f"当前状态={g5_status!r}，历史批准={g5_was_approved}"
        )
    if before_g5_exception and not args.allow_before_g5:
        die(
            "科学冻结要求先批准 v5.1 independent_review、consistency，再批准 G5。"
            "仅为旧流程在尚未批准 G5 时冻结，可显式使用 "
            "--allow-before-g5（该例外会写入决策日志）。"
        )
    freshness = [*prerequisite_freshness, *g5_freshness]
    freshness_errors = [*prerequisite_errors, *g5_errors]
    if (run / "FROZEN_MANIFEST.json").exists() and not args.replace:
        die("已经存在 FROZEN_MANIFEST.json。使用 --replace 明确重新冻结。")
    evidence_errors = _evidence_artifact_errors(run)
    if evidence_errors:
        die("拒绝冻结 run 外或缺失的证据：" + "; ".join(evidence_errors))
    records = _freeze_records(run)
    payload = {
        "schema_version": 1,
        "version": config.get("version", "5.1.0"),
        "run_id": run.name,
        "created_at": now_iso(),
        "created_by": reviewer,
        "note": args.note or "",
        "scope": config.get("freeze", {}).get("include", []),
        "before_g5_exception": before_g5_exception,
        "gate_freshness": freshness,
        "file_count": len(records),
        "files": records,
    }
    write_json(run / "FROZEN_MANIFEST.json", payload)
    status["frozen"] = True
    status["frozen_at"] = payload["created_at"]
    status["freeze_manifest"] = "FROZEN_MANIFEST.json"
    save_status(run, status)
    append_jsonl(
        run / "decisions" / "DECISION_LOG.jsonl",
        {
            "timestamp": now_iso(), "type": "freeze", "by": reviewer,
            "file_count": len(records), "note": args.note,
            "before_g5_exception": before_g5_exception,
            "freshness_errors": freshness_errors,
            "gate_freshness": freshness,
        },
    )
    print(f"已冻结 {len(records)} 个文件。")
    return 0


def verify_freeze(run: Path, *, quiet: bool = False) -> dict[str, Any]:
    path = run / "FROZEN_MANIFEST.json"
    if not path.exists():
        return {"ok": False, "issues": ["FROZEN_MANIFEST.json 不存在"], "checked_at": now_iso()}
    issues: list[str] = []
    try:
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        manifest = {}
        issues.append(f"FROZEN_MANIFEST.json 无法读取：{exc}")
    if not isinstance(manifest, dict):
        manifest = {}
        issues.append("FROZEN_MANIFEST.json 顶层必须是对象")
    try:
        from schema_validate import validate_json_file

        issues.extend(
            "schema: " + item
            for item in validate_json_file(
                path,
                ROOT / "schemas" / "frozen_manifest.schema.json",
            )
        )
    except (Exception, SystemExit) as exc:
        issues.append(f"schema: frozen manifest validator exception: {exc}")
    config = load_config()
    if isinstance(manifest.get("schema_version"), bool) or manifest.get("schema_version") != 1:
        issues.append("FROZEN_MANIFEST.json schema_version 必须为 1")
    if manifest.get("version") != config.get("version", "5.1.0"):
        issues.append("FROZEN_MANIFEST.json version 与当前 harness 不一致")
    if manifest.get("run_id") != run.name:
        issues.append("FROZEN_MANIFEST.json run_id 与当前 run 不一致")
    if not is_named_human_identity(manifest.get("created_by")):
        issues.append("FROZEN_MANIFEST.json created_by 必须是具名人工身份")
    expected_scope = config.get("freeze", {}).get("include", [])
    if manifest.get("scope") != expected_scope:
        issues.append("FROZEN_MANIFEST.json scope 与当前 canonical freeze scope 不一致")
    gate_order = _gate_order(config)
    freeze_gate_ids = gate_order[: gate_order.index("G5") + 1]
    try:
        current_gate_freshness, _ = _approved_gate_freshness(run, freeze_gate_ids)
    except (Exception, SystemExit) as exc:
        current_gate_freshness = []
        issues.append(f"冻结所依据的 Gate 无法重验：{exc}")
    stored_gate_freshness = manifest.get("gate_freshness")
    if stored_gate_freshness != current_gate_freshness:
        issues.append("FROZEN_MANIFEST.json gate_freshness 与当前 G0-G5 批准状态不一致")
    if isinstance(stored_gate_freshness, list):
        freshness_by_gate = {
            item.get("gate"): item
            for item in stored_gate_freshness
            if isinstance(item, dict) and item.get("gate")
        }
        if set(freshness_by_gate) != set(freeze_gate_ids):
            issues.append("FROZEN_MANIFEST.json gate_freshness 必须恰好覆盖 G0-G5")
        before_g5_exception = manifest.get("before_g5_exception") is True
        required_current = freeze_gate_ids[:-1] if before_g5_exception else freeze_gate_ids
        for gate_id in required_current:
            if freshness_by_gate.get(gate_id, {}).get("status") != "current":
                issues.append(f"FROZEN_MANIFEST.json 记录的 {gate_id} 不是 current")
        if before_g5_exception and freshness_by_gate.get("G5", {}).get("status") != "pending":
            issues.append("before_g5_exception 只允许 G5 处于 pending")
    raw_files = manifest.get("files", [])
    if not isinstance(raw_files, list):
        issues.append("FROZEN_MANIFEST.json files 必须是数组")
        raw_files = []
    declared_count = manifest.get("file_count")
    if isinstance(declared_count, bool) or not isinstance(declared_count, int):
        issues.append("FROZEN_MANIFEST.json file_count 必须是整数")
    elif declared_count != len(raw_files):
        issues.append("FROZEN_MANIFEST.json file_count 与 files 行数不一致")
    expected: dict[str, dict[str, Any]] = {}
    run_root = run.resolve()
    for index, item in enumerate(raw_files, start=1):
        if not isinstance(item, dict):
            issues.append(f"FROZEN_MANIFEST.json files[{index}] 必须是对象")
            continue
        raw_path_text = str(item.get("path", "")).strip()
        relative = raw_path_text.replace("\\", "/")
        raw_relative = Path(relative)
        if raw_path_text != relative:
            issues.append(f"FROZEN_MANIFEST.json files[{index}].path 必须使用 canonical '/' 分隔符")
            continue
        if not relative or raw_relative.is_absolute() or ".." in raw_relative.parts:
            issues.append(f"FROZEN_MANIFEST.json files[{index}].path 不是 run-relative 路径")
            continue
        resolved = (run / raw_relative).resolve()
        try:
            canonical = resolved.relative_to(run_root).as_posix()
        except ValueError:
            issues.append(f"FROZEN_MANIFEST.json files[{index}].path 离开当前 run")
            continue
        if canonical != relative:
            issues.append(f"FROZEN_MANIFEST.json files[{index}].path 不是 canonical：{relative}")
            continue
        if canonical in expected:
            issues.append(f"FROZEN_MANIFEST.json 含重复 path：{canonical}")
            continue
        size = item.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            issues.append(f"FROZEN_MANIFEST.json files[{index}].size 无效")
            continue
        digest = str(item.get("sha256", "")).strip()
        if len(digest) != 64 or digest.lower() != digest or any(
            char not in "0123456789abcdef" for char in digest
        ):
            issues.append(f"FROZEN_MANIFEST.json files[{index}].sha256 无效")
            continue
        expected[canonical] = {"path": canonical, "size": size, "sha256": digest}
    try:
        current = {item["path"]: item for item in _freeze_records(run)}
    except (Exception, SystemExit) as exc:
        current = {}
        issues.append(f"当前 freeze scope 无法读取：{exc}")
    for relative, item in expected.items():
        if relative not in current:
            issues.append(f"已删除：{relative}")
        elif (
            current[relative]["sha256"] != item["sha256"]
            or current[relative]["size"] != item["size"]
        ):
            issues.append(f"已修改：{relative}")
    for relative in sorted(set(current) - set(expected)):
        issues.append(f"冻结后新增：{relative}")
    result = {"ok": not issues, "issues": issues, "checked_at": now_iso(), "expected_count": len(expected), "current_count": len(current)}
    if not quiet:
        print("Freeze OK" if result["ok"] else "Freeze FAIL")
        for issue in issues:
            print(f"  - {issue}")
    return result


def cmd_verify_freeze(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    result = verify_freeze(run)
    write_json(run / "FREEZE_VERIFICATION.json", result)
    return 0 if result["ok"] else 4


def cmd_unfreeze(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    reviewer = _require_named_human(args.by, action="unfreeze --by")
    manifest = run / "FROZEN_MANIFEST.json"
    if not manifest.exists():
        print("当前未冻结。")
        return 0
    archive = run / "decisions" / f"FROZEN_MANIFEST.revoked.{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    shutil.move(str(manifest), str(archive))
    status = load_status(run)
    status["frozen"] = False
    status["frozen_at"] = None
    status["freeze_manifest"] = None
    save_status(run, status)
    append_jsonl(
        run / "decisions" / "DECISION_LOG.jsonl",
        {"timestamp": now_iso(), "type": "unfreeze", "by": reviewer, "reason": args.reason, "archived_manifest": relative_to_run(archive, run)},
    )
    print(f"已撤销冻结；旧清单保存在 {archive}")
    return 0


def _event_bool(event: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        if key in event:
            return is_truthy(event.get(key))
    return False


def cmd_export_ai(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    log_path = run / "ai" / "AI_USAGE_LOG.jsonl"
    if not log_path.is_file():
        die("缺少 ai/AI_USAGE_LOG.jsonl")
    reviewer = str(args.by or "").strip()
    if not reviewer:
        die("export-ai 必须通过 --by 记录人工复核人")
    if reviewer.upper() in PLACEHOLDER_REVIEWERS:
        die("export-ai --by 必须是具名复核人，不能使用 TEAM/UNKNOWN/TODO 等占位值")
    raw_events = read_jsonl(log_path)
    event_errors = _ai_usage_event_errors(raw_events)
    if event_errors:
        die("AI_USAGE_LOG.jsonl 不满足 disclosure contract：" + "; ".join(event_errors))
    generated_at = now_iso()
    events: list[dict[str, Any]] = []
    for index, event in enumerate(raw_events, 1):
        events.append(
            {
                "event_id": event.get("event_id") or event.get("id") or f"AI_LEGACY_{index:03d}",
                "timestamp": event.get("timestamp") or event.get("created_at") or "",
                "tool": event.get("tool") or event.get("provider") or "",
                "model": event.get("model") or "",
                "stage": event.get("stage") or event.get("mode") or "unspecified",
                "purpose": event.get("purpose") or event.get("task") or "",
                "prompt_or_task_summary": event.get("prompt_or_task_summary") or event.get("prompt_summary") or "",
                "input_artifacts": event.get("input_artifacts") or event.get("inputs") or [],
                "output_artifacts": event.get("output_artifacts") or event.get("outputs") or [],
                "adopted": _event_bool(event, "adopted", "accepted"),
                "human_modified": _event_bool(event, "human_modified", "modified"),
                "human_verified": _event_bool(event, "human_verified", "verified"),
                "verified_by": event.get("verified_by") or event.get("by") or "",
                "notes": event.get("notes") or event.get("note") or "",
            }
        )
    by_stage: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_stage.setdefault(str(event["stage"]), []).append(event)
    lines = [
        "# AI工具使用详情",
        "",
        "> 本文件由参赛队员对源日志与导出字段进行人工复核后生成；"
        "提交前仍须检查其中所有文字与文件路径是否匿名，并转换为 `AI工具使用详情.pdf`。",
        "",
        f"- 已记录事件数：`{len(events)}`",
        "",
    ]
    public_event_index = 0
    for stage, stage_events in sorted(by_stage.items()):
        lines.extend([f"## {stage}", ""])
        for event in stage_events:
            public_event_index += 1
            lines.extend(
                [
                    f"### 记录 {public_event_index} — {event['tool']} / {event['model']}",
                    "",
                    f"- 时间：{event['timestamp']}",
                    f"- 目的：{event['purpose']}",
                    f"- 提示或任务摘要：{event['prompt_or_task_summary']}",
                    f"- 输入：{', '.join(map(str, event['input_artifacts'])) or '未记录'}",
                    f"- 输出：{', '.join(map(str, event['output_artifacts'])) or '未记录'}",
                    f"- 是否采纳：{event['adopted']}",
                    f"- 是否人工修改：{event['human_modified']}",
                    f"- 是否人工核验：{event['human_verified']}",
                    "- 人工核验责任：已由参赛队内部留痕；公开详情不写复核人姓名",
                    f"- 备注：{event['notes']}",
                    "",
                ]
            )
    output = run / "ai" / "AI_TOOL_USAGE_DETAILS.md"
    atomic_write_text(output, "\n".join(lines).rstrip() + "\n")
    receipt = {
        "schema_version": 1,
        "run_id": run.name,
        "generated_at": generated_at,
        "source_path": "ai/AI_USAGE_LOG.jsonl",
        "source_sha256": sha256_file(log_path),
        "event_count": len(raw_events),
        "export_path": "ai/AI_TOOL_USAGE_DETAILS.md",
        "export_sha256": sha256_file(output),
        "human_reviewed": True,
        "reviewed_by": reviewer,
        "reviewed_at": now_iso(),
    }
    receipt_path = run / AI_EXPORT_RECEIPT_RELATIVE
    write_json(receipt_path, receipt)
    print(output)
    print(receipt_path)
    return 0


def cmd_attest_ai_disclosure_pdf(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    reviewer = str(args.by or "").strip()
    if not reviewer or reviewer.upper() in PLACEHOLDER_REVIEWERS:
        die(
            "attest-ai-disclosure-pdf --by 必须是完成人工视觉/内容复核的具名队员，"
            "不能使用 TEAM/UNKNOWN/TODO 等占位值"
        )
    export_audit = _ai_export_audit(run)
    if export_audit.get("errors"):
        die(
            "AI disclosure PDF 见证前必须有 current AI export receipt："
            + "; ".join(export_audit["errors"])
        )
    source_path = run / "ai" / "AI_TOOL_USAGE_DETAILS.md"
    pdf_path = run / AI_DISCLOSURE_PDF_RELATIVE
    run_root = run.resolve()
    for candidate, relative in (
        (source_path, "ai/AI_TOOL_USAGE_DETAILS.md"),
        (pdf_path, AI_DISCLOSURE_PDF_RELATIVE),
    ):
        if not candidate.is_file() or candidate.stat().st_size <= 0:
            die(f"缺少或为空：{relative}")
        resolved = candidate.resolve()
        try:
            canonical = resolved.relative_to(run_root).as_posix()
        except ValueError:
            die(f"AI disclosure artifact 离开当前 run：{relative}")
        if canonical != relative:
            die(f"AI disclosure artifact 使用符号链接或非 canonical 路径：{relative}")
    envelope_errors = pdf_envelope_errors(pdf_path)
    if envelope_errors:
        die("AI disclosure PDF 结构无效：" + "; ".join(envelope_errors))
    receipt = {
        "schema_version": 1,
        "run_id": run.name,
        "attested_at": now_iso(),
        "attested_by": reviewer,
        "human_visual_reviewed": True,
        "source_path": "ai/AI_TOOL_USAGE_DETAILS.md",
        "source_sha256": sha256_file(source_path),
        "pdf_path": AI_DISCLOSURE_PDF_RELATIVE,
        "pdf_size": pdf_path.stat().st_size,
        "pdf_sha256": sha256_file(pdf_path),
        "note": (
            "Named human attestation of visual readability and content correspondence; "
            "this hash receipt does not claim automatic layout or official-format validation."
        ),
    }
    receipt_path = run / AI_DISCLOSURE_PDF_RECEIPT_RELATIVE
    write_json(receipt_path, receipt)
    try:
        from schema_validate import validate_json_file

        schema_errors = validate_json_file(
            receipt_path,
            ROOT / "schemas" / "ai_disclosure_pdf_receipt.schema.json",
        )
    except (Exception, SystemExit) as exc:
        die(f"AI disclosure PDF receipt schema validator exception: {exc}")
    if schema_errors:
        die("AI disclosure PDF receipt schema validation failed: " + "; ".join(schema_errors))
    print(receipt_path)
    return 0


def cmd_regenerate_manifest(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    full = _input_manifest(INPUTS_ROOT)
    data = _input_manifest(INPUTS_ROOT / "data")
    write_json(run / "INPUT_MANIFEST.json", full)
    write_json(run / "data" / "DATA_MANIFEST.json", data)
    print(f"已登记 {len(full['files'])} 个输入文件。")
    return 0


DELIVERY_MANIFEST_RELATIVE = "deliverables/DELIVERY_MANIFEST.json"
FIXED_DELIVERY_FILES = (
    "RUN_META.json",
    "INPUT_MANIFEST.json",
    "STATUS.json",
    "V51_STATUS.json",
    "FROZEN_MANIFEST.json",
    "decisions/DECISION_LOG.jsonl",
)


def _collect_delivery_files(
    run: Path, *, allow_missing_fixed: bool
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str]]:
    """Build an integrity-checked allowlist from frozen and approval snapshots."""
    run_root = run.resolve()
    bindings: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    missing_fixed: list[str] = []
    compile_audit = _paper_compile_audit(run)
    errors.extend(
        f"paper compile allowlist: {item}"
        for item in compile_audit.get("errors", [])
    )
    compile_output = str(compile_audit.get("output_path") or "").strip()
    approved_document_pdfs = {
        compile_output
    } if compile_output and not compile_audit.get("errors") else set()
    disclosure_audit = _ai_disclosure_pdf_audit(run)
    errors.extend(
        f"AI disclosure PDF allowlist: {item}"
        for item in disclosure_audit.get("errors", [])
    )
    disclosure_output = str(disclosure_audit.get("pdf_path") or "").strip()
    if disclosure_output and not disclosure_audit.get("errors"):
        approved_document_pdfs.add(disclosure_output)

    def bind(
        value: Any,
        *,
        source: str,
        expected_sha256: Any = None,
        expected_current: bool = False,
        required: bool = True,
    ) -> None:
        text = str(value or "").strip().replace("\\", "/")
        if not text:
            errors.append(f"{source} contains an empty artifact path")
            return
        raw = Path(text)
        if raw.is_absolute() or ".." in raw.parts:
            errors.append(f"{source} artifact path is not run-relative: {text}")
            return
        candidate = (run / raw).resolve()
        try:
            relative = candidate.relative_to(run_root).as_posix()
        except ValueError:
            errors.append(f"{source} artifact leaves the run directory: {text}")
            return
        if relative != text:
            errors.append(f"{source} artifact path is non-canonical or a symlink: {text}")
            return
        if relative == DELIVERY_MANIFEST_RELATIVE or relative.startswith(
            "deliverables/packages/"
        ):
            errors.append(f"{source} binds a generated package artifact: {relative}")
            return
        if (
            Path(relative).suffix.lower() == ".pdf"
            and relative.startswith(("paper/", "deliverables/"))
            and relative not in approved_document_pdfs
        ):
            errors.append(
                f"{source} binds an untyped/unreceipted PDF artifact: {relative}"
            )
            return
        if not candidate.is_file():
            if required:
                errors.append(f"{source} artifact is missing: {relative}")
            else:
                missing_fixed.append(relative)
            return
        item = bindings.setdefault(relative, {
            "path": candidate,
            "sources": set(),
            "expected_sha256": set(),
        })
        item["sources"].add(source)
        if expected_current:
            # Audit-backed delivery artifacts must stay byte-identical between
            # their successful audit and archive construction.  Capturing the
            # digest here also makes pre-G7 diagnostic packages fail closed
            # without relying on a later Gate snapshot to bind the files.
            item["expected_sha256"].add(sha256_file(candidate))
        if expected_sha256 is not None:
            digest = str(expected_sha256).strip().lower()
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                errors.append(f"{source} has an invalid SHA-256 for {relative}")
            else:
                item["expected_sha256"].add(digest)

    def bind_audit_inputs(audit: dict[str, Any], *, source: str) -> None:
        """Bind every artifact covered by one successful typed audit."""
        if audit.get("errors"):
            return
        input_paths = audit.get("input_paths", [])
        if not isinstance(input_paths, list):
            errors.append(f"{source} input_paths is not an array")
            return
        for index, relative in enumerate(input_paths, start=1):
            bind(
                relative,
                source=f"{source}:{index}",
                expected_current=True,
            )

    bind_audit_inputs(compile_audit, source="paper-compile-audit")
    bind_audit_inputs(disclosure_audit, source="ai-disclosure-pdf-audit")

    frozen_path = run / "FROZEN_MANIFEST.json"
    if frozen_path.is_file():
        freeze_check = verify_freeze(run, quiet=True)
        if not freeze_check.get("ok"):
            errors.extend(
                f"FROZEN_MANIFEST.json is not a current canonical freeze: {issue}"
                for issue in freeze_check.get("issues", [])
            )
            frozen_files: list[Any] = []
        else:
            frozen = read_json(frozen_path, expect=dict)
            frozen_files = frozen.get("files", [])
        if not isinstance(frozen_files, list):
            errors.append("FROZEN_MANIFEST.json files must be an array")
        else:
            for index, record in enumerate(frozen_files, start=1):
                if not isinstance(record, dict):
                    errors.append(f"FROZEN_MANIFEST.json files[{index}] is not an object")
                    continue
                bind(
                    record.get("path"),
                    source=f"freeze:{index}",
                    expected_sha256=record.get("sha256"),
                )

    status_path = run / "STATUS.json"
    if status_path.is_file():
        status_payload = read_json(status_path, expect=dict)
        status_contract_errors = _status_contract_errors(
            run, status_payload, load_config()
        )
        errors.extend(f"STATUS.json contract: {item}" for item in status_contract_errors)
        gate_states = status_payload.get("gates", {})
        if not isinstance(gate_states, dict):
            errors.append("STATUS.json gates must be an object")
            gate_states = {}
        for gate_id in _gate_order(load_config()):
            item = gate_states.get(gate_id, {})
            if not isinstance(item, dict) or item.get("status") != "approved":
                continue
            validation = item.get("validation") or {}
            if not isinstance(validation, dict):
                continue
            try:
                current_gate_result = validate_gate(run, gate_id, strict=True)
            except (Exception, SystemExit) as exc:
                errors.append(f"STATUS.json {gate_id} current validation failed: {exc}")
                continue
            gate_approval_errors = _gate_approval_record_errors(
                gate_id,
                item,
                run=run,
                current_result=current_gate_result,
            )
            if gate_approval_errors:
                errors.extend(
                    f"STATUS.json {gate_id} approval: {message}"
                    for message in gate_approval_errors
                )
                continue
            input_manifest = validation.get("input_manifest", [])
            if not isinstance(input_manifest, list):
                errors.append(f"STATUS.json {gate_id} validation.input_manifest must be an array")
                continue
            for index, record in enumerate(input_manifest, start=1):
                if not isinstance(record, dict) or record.get("kind") not in {
                    "artifact", "referenced-artifact",
                }:
                    continue
                bind(
                    record.get("path"),
                    source=f"gate:{gate_id}:{index}",
                    expected_sha256=record.get("sha256"),
                )

    v51_status_path = run / "V51_STATUS.json"
    if v51_status_path.is_file():
        try:
            from v51_orchestrator import (
                STAGES as V51_STAGES,
                approved_stage_record_errors,
                ensure_state as ensure_v51_state,
                validate_stage as validate_v51_stage,
            )

            v51_payload = ensure_v51_state(run, persist=False)
        except (Exception, SystemExit) as exc:
            errors.append(f"V51_STATUS.json contract: {exc}")
            v51_payload = {"stages": {}}
            V51_STAGES = []
        stage_states = v51_payload.get("stages", {})
        if not isinstance(stage_states, dict):
            errors.append("V51_STATUS.json stages must be an object")
            stage_states = {}
        for stage_id in V51_STAGES:
            item = stage_states.get(stage_id, {})
            if not isinstance(item, dict) or item.get("status") != "approved":
                continue
            review_id = None
            stored_result = item.get("last_result") or {}
            if stage_id == "independent_review" and isinstance(stored_result, dict):
                review_id = stored_result.get("review_id")
            try:
                current_stage_result, _ = validate_v51_stage(
                    run, stage_id, review_id=review_id
                )
            except (Exception, SystemExit) as exc:
                errors.append(f"V51_STATUS.json {stage_id} current validation failed: {exc}")
                continue
            stage_approval_errors = approved_stage_record_errors(
                stage_id,
                item,
                current_result=current_stage_result,
            )
            if stage_approval_errors:
                errors.extend(
                    f"V51_STATUS.json {stage_id} approval: {message}"
                    for message in stage_approval_errors
                )
                continue
            result = item.get("last_result") or {}
            if not isinstance(result, dict):
                continue
            input_manifest = result.get("input_manifest", [])
            if not isinstance(input_manifest, list):
                errors.append(f"V51_STATUS.json {stage_id} last_result.input_manifest must be an array")
                continue
            for index, record in enumerate(input_manifest, start=1):
                if not isinstance(record, dict) or record.get("kind") not in {
                    "artifact", "referenced-artifact",
                }:
                    continue
                bind(
                    record.get("path"),
                    source=f"v51:{stage_id}:{index}",
                    expected_sha256=record.get("sha256"),
                )

    fixed_files = list(FIXED_DELIVERY_FILES)
    fixed_files.extend(
        str(value) for value in load_config().get("gates", {}).get("G7", {}).get("requires", [])
    )
    for relative in dict.fromkeys(fixed_files):
        bind(
            relative,
            source="fixed-delivery-contract",
            required=not allow_missing_fixed,
        )

    records: list[dict[str, Any]] = []
    for relative, item in sorted(bindings.items()):
        expected = sorted(item["expected_sha256"])
        if len(expected) > 1:
            errors.append(
                f"approval/freeze snapshots disagree for {relative}: " + ", ".join(expected)
            )
            continue
        current_sha256 = sha256_file(item["path"])
        if expected and current_sha256 != expected[0]:
            errors.append(
                f"snapshot-bound artifact changed: {relative} "
                f"expected={expected[0]} current={current_sha256}"
            )
            continue
        records.append({
            "path": relative,
            "size": item["path"].stat().st_size,
            "sha256": current_sha256,
            "bound_by": sorted(item["sources"]),
        })

    bound_paths = {record["path"] for record in records}
    omitted: list[dict[str, str]] = []
    for path in iter_files(run, exclude_names={".gitkeep"}):
        try:
            lexical_relative = path.relative_to(run).as_posix()
        except ValueError:
            errors.append(f"run iterator returned an outside path: {path}")
            continue
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(run_root).as_posix()
        except ValueError:
            errors.append(f"run contains a file or symlink outside the run directory: {path}")
            continue
        if relative != lexical_relative:
            errors.append(f"delivery packaging rejects non-canonical file links: {lexical_relative}")
            continue
        if relative.startswith("deliverables/packages/"):
            omitted.append({"path": relative, "reason": "generated-package"})
            continue
        if relative == DELIVERY_MANIFEST_RELATIVE:
            omitted.append({"path": relative, "reason": "regenerated-delivery-manifest"})
            continue
        if relative in bound_paths:
            continue
        sensitive_unbound = relative.startswith("deliverables/") or (
            relative.startswith("paper/") and Path(relative).suffix.lower() == ".pdf"
        )
        if sensitive_unbound:
            errors.append(f"unbound delivery artifact must not be packaged: {relative}")
        else:
            omitted.append({"path": relative, "reason": "not-bound-by-approved-snapshot"})

    if errors:
        die("交付 allowlist 校验失败：" + "; ".join(errors))
    return records, sorted(omitted, key=lambda item: item["path"]), sorted(set(missing_fixed))


def cmd_build_support(args: argparse.Namespace) -> int:
    """Build the official anonymous support ZIP from an explicit allowlist."""
    run = resolve_run(args.run)
    reviewer = _require_named_human(args.by, action="build-support --by")
    ai_audit = _ai_export_audit(run)
    if ai_audit.get("errors"):
        die(
            "build-support 前必须先完成 current AI 日志导出："
            + "; ".join(ai_audit["errors"])
        )
    ai_required = int(ai_audit.get("disclosure_event_count", 0) or 0) > 0
    if ai_required:
        ai_pdf_audit = _ai_disclosure_pdf_audit(run)
        if ai_pdf_audit.get("errors"):
            die(
                "存在需披露的 AI 使用时，官方支撑 ZIP 必须绑定已人工见证的 "
                "AI工具使用详情.pdf：" + "; ".join(ai_pdf_audit["errors"])
            )
    policy, policy_errors = _official_support_policy()
    if policy_errors:
        die("官方支撑材料策略无效：" + "; ".join(policy_errors))
    identities = _known_support_identities(
        run,
        extra=[reviewer, *list(args.identity or [])],
    )
    try:
        from support_package import SupportPackageError, build_support_archive
    except (ImportError, OSError) as exc:
        die(f"无法载入官方支撑材料构建器：{exc}")
    try:
        result = build_support_archive(
            run,
            list(args.include or []),
            str(policy["output"]),
            str(policy["receipt"]),
            generated_by=reviewer,
            ai_required=ai_required,
            known_identities=identities,
            max_bytes=int(policy["max_bytes"]),
            replace=bool(args.replace),
        )
    except SupportPackageError as exc:
        die(f"官方支撑材料构建失败：{exc}")
    except (Exception, SystemExit) as exc:
        die(f"官方支撑材料构建器异常：{type(exc).__name__}: {exc}")
    for item in result.get("warnings", []):
        warn(f"official support archive: {item}")
    print(f"官方匿名支撑材料：{result['archive_path']}")
    print(
        f"大小：{result['archive_size']:,}/{int(policy['max_bytes']):,} bytes；"
        f"SHA-256：{result['archive_sha256']}"
    )
    print(f"内部具名回执（不要提交）：{result['receipt_path']}")
    return 0


def cmd_package(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    reviewer = str(args.by or "").strip()
    bypass_note = str(args.note or "").strip()
    if not reviewer:
        die("package 必须通过 --by 记录打包责任人")
    if reviewer.upper() in PLACEHOLDER_REVIEWERS:
        die("package --by 必须是具名责任人，不能使用 TEAM/UNKNOWN/TODO 等占位值")
    if args.allow_unvalidated:
        if len(bypass_note) < 10:
            die("--allow-unvalidated 必须通过 --note 提供至少 10 个字符的具体绕过理由")
    gate_order = _gate_order(load_config())
    freshness, freshness_errors = _approved_gate_freshness(run, gate_order)
    if freshness_errors and not args.allow_unvalidated:
        die(
            "正式打包要求 G0-G7 全部批准、strict 重验通过且批准输入哈希仍匹配："
            + "; ".join(freshness_errors)
            + "。诊断性快照请显式使用 --allow-unvalidated。"
        )
    run_root = run.resolve()
    requested_output = (
        Path(args.out).expanduser()
        if args.out
        else run / "deliverables" / "packages" / f"{run.name}.zip"
    )
    # abspath preserves the lexical route through directories while resolve()
    # follows links.  Comparing them prevents an apparently in-run output from
    # being redirected outside the run by a symlink.
    lexical_output = Path(os.path.abspath(str(requested_output)))
    output = requested_output.resolve()
    if lexical_output != output:
        die("package 输出路径不得经过符号链接或其他非 canonical 路径")
    if output.suffix.lower() != ".zip":
        die("package 输出必须使用 .zip 扩展名")
    try:
        output_relative = output.relative_to(run_root).as_posix()
    except ValueError:
        output_relative = None
    if not args.out and output_relative != f"deliverables/packages/{run.name}.zip":
        die("默认 package 输出必须严格位于当前 run 的 deliverables/packages/。")
    if args.out and output_relative is not None and not output_relative.startswith("deliverables/packages/"):
        die("run 内的 package 输出必须位于 deliverables/packages/；也可选择 run 外路径。")
    if args.out and output_relative is None:
        try:
            output.relative_to(ROOT.resolve())
        except ValueError:
            pass
        else:
            die("显式 run 外 package 输出不得覆盖 harness 项目内的其他文件")
    output.parent.mkdir(parents=True, exist_ok=True)
    checksum_candidate = output.with_suffix(output.suffix + ".sha256")
    checksum_path = checksum_candidate.resolve()
    if Path(os.path.abspath(str(checksum_candidate))) != checksum_path:
        die("package checksum 路径不得经过符号链接或其他非 canonical 路径")
    existing_outputs = [path for path in (output, checksum_path) if path.exists()]
    if existing_outputs and not args.replace:
        die("输出已存在：" + ", ".join(str(path) for path in existing_outputs) + "。使用 --replace。")
    if args.allow_unvalidated:
        append_jsonl(
            run / "decisions" / "DECISION_LOG.jsonl",
            {
                "timestamp": now_iso(), "type": "package-validation-bypass",
                "by": reviewer, "note": bypass_note,
                "freshness_errors": freshness_errors, "gate_freshness": freshness,
            },
        )
    delivery_records, omitted, missing_fixed = _collect_delivery_files(
        run, allow_missing_fixed=bool(args.allow_unvalidated)
    )
    delivery_manifest = {
        "schema_version": 1,
        "run_id": run.name,
        "generated_at": now_iso(),
        "generated_by": reviewer,
        "package_name": output.name,
        "validation_bypass": bool(args.allow_unvalidated),
        "gate_freshness": freshness,
        "freshness_errors": freshness_errors,
        "missing_fixed_files": missing_fixed,
        "file_count": len(delivery_records),
        "archive_file_count": len(delivery_records) + 1,
        "files": delivery_records,
        "omitted": omitted,
        "note": (
            "files contains only frozen, approval-bound, or fixed delivery-contract artifacts; "
            "the generated delivery manifest is the one additional archive member"
        ),
    }
    delivery_manifest_path = run / DELIVERY_MANIFEST_RELATIVE
    write_json(delivery_manifest_path, delivery_manifest)
    try:
        from schema_validate import validate_json_file

        delivery_schema_errors = validate_json_file(
            delivery_manifest_path,
            ROOT / "schemas" / "delivery_manifest.schema.json",
        )
    except (Exception, SystemExit) as exc:
        die(f"交付 manifest schema 校验器异常：{exc}")
    if delivery_schema_errors:
        die("交付 manifest schema 校验失败：" + "; ".join(delivery_schema_errors))
    package_files: list[tuple[Path, Path]] = []
    for record in delivery_records:
        resolved = (run / record["path"]).resolve()
        if (
            not resolved.is_file()
            or resolved.stat().st_size != record["size"]
            or sha256_file(resolved) != record["sha256"]
        ):
            die(f"交付文件在 manifest 生成后发生变化：{record['path']}")
        package_files.append((resolved, Path(record["path"])))
    package_files.append((delivery_manifest_path.resolve(), Path(DELIVERY_MANIFEST_RELATIVE)))
    expected_members = [f"{run.name}/{relative.as_posix()}" for _, relative in package_files]
    expected_hashes = {
        f"{run.name}/{record['path']}": record["sha256"] for record in delivery_records
    }
    expected_hashes[f"{run.name}/{DELIVERY_MANIFEST_RELATIVE}"] = sha256_file(
        delivery_manifest_path
    )
    temp_output: Path | None = None
    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=str(output.parent)
        )
        os.close(descriptor)
        temp_output = Path(temp_name)
        with zipfile.ZipFile(
            temp_output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for resolved, relative in package_files:
                archive.write(resolved, arcname=f"{run.name}/{relative.as_posix()}")
        with zipfile.ZipFile(temp_output, "r") as archive:
            actual_members = archive.namelist()
            if actual_members != expected_members:
                die("临时交付包成员清单与 allowlist 不一致")
            corrupt_member = archive.testzip()
            if corrupt_member is not None:
                die(f"临时交付包 CRC 校验失败：{corrupt_member}")
            for member in actual_members:
                digest = hashlib.sha256()
                with archive.open(member, "r") as source:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                if digest.hexdigest() != expected_hashes[member]:
                    die(f"临时交付包内容哈希不一致：{member}")
        os.replace(temp_output, output)
        temp_output = None
    finally:
        if temp_output is not None:
            try:
                temp_output.unlink()
            except FileNotFoundError:
                pass
    checksum = sha256_file(output)
    atomic_write_text(checksum_path, f"{checksum}  {output.name}\n")
    warn(
        "package 生成的是包含具名审批/审计材料的内部归档，绝不能作为官方匿名支撑材料提交；"
        "官方 ZIP 请使用 build-support。"
    )
    print(output)
    print(checksum)
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    config = load_config()
    status = load_status(run)
    gate = status.get("current_gate", "G0")
    suggestions = {
        "G0": "通过当前 runtime 的 cumcm-adaptive-research Skill 执行 Intake，完成问题图、数据审计、假设与验证地图；然后人工 approve G0。",
        "G1": "建立动态 TEAM_PLAN，分批运行盲探索；创建 Idea Card 后执行 `python tools/diversity.py analyze`。",
        "G2": "人工选择值得深化的 Idea/模块，运行可行性审查并用 `module-new` 建立 Module Card。",
        "G3": "为聚焦模块设计 baseline、实验、敏感性和证据；用 claim-add/evidence-add 登记。",
        "G4": "按模块接口整合，登记争议；对 critical dispute 启动专项 Agent、独立 Pro、Verifier 或补实验。",
        "G5": "必须先运行 `claim_guard.py scan --scope math`，用 `provenance-set --scope math` 处置命中项；再依次批准 v5.1 independent_review、consistency、G5 并冻结。",
        "G6": "复用 current consistency；正文稳定后运行 `claim_guard.py scan --scope all`，批准 strong_claim_audit，再批准 paper_extension，完成 G6。",
        "G7": "先生成 canonical 最终 PDF，完成 AI 导出/正文声明/来源与匿名复核清单；用 build-support 生成官方匿名支撑 ZIP，再批准 v5.1 submission，运行 review --gate G7 并批准 G7。package 只用于内部审计归档。",
    }
    print(f"Current gate: {gate} — {config['gates'].get(gate, {}).get('name', '')}")
    print(suggestions.get(gate, "检查 STATUS.json。"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="创建新的 run")
    p.add_argument("--contest", required=True)
    p.add_argument("--problem", required=True)
    p.add_argument("--phase", choices=PHASES, default="rehearsal")
    p.add_argument("--run-id")
    p.add_argument("--time-budget-hours", type=float, default=72)
    p.add_argument("--deadline")
    p.add_argument("--human-review-hours", type=float, default=18)
    p.add_argument("--max-exploration-batches", type=int, default=3)
    p.add_argument("--runtime", choices=RUNTIMES, default="codex")
    p.add_argument("--max-agent-threads", "--max-codex-threads", dest="max_agent_threads", type=int, default=4, help="Runtime agent concurrency; --max-codex-threads kept as v3 alias")
    p.add_argument("--api-budget-usd", type=float, default=0.0, help="v3 alias for --openai-api-budget-usd")
    p.add_argument("--openai-api-budget-usd", type=float)
    p.add_argument("--anthropic-api-budget-usd", type=float, default=0.0)
    p.add_argument("--deepseek-api-budget-usd", type=float, default=0.0)
    p.add_argument("--by", default="TEAM")
    p.add_argument("--note")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("active", help="显示 active run")
    p.set_defaults(func=cmd_active)

    p = sub.add_parser("status", help="显示 run 状态")
    p.add_argument("--run")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("budget-status", help="显示预算状态")
    p.add_argument("--run")
    p.set_defaults(func=cmd_budget_status)

    p = sub.add_parser("budget-log", help="登记资源消耗")
    p.add_argument("--run")
    p.add_argument("--kind", required=True, choices=["human-hours", "manual-pro-dialogues", "exploration-batches", "api-usd", "openai-api-usd", "anthropic-api-usd", "deepseek-api-usd", "codex-tasks", "agent-tasks"])
    p.add_argument("--amount", required=True, type=float)
    p.add_argument("--by", default="TEAM")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_budget_log)

    p = sub.add_parser("validate", help="检查 Gate 文件和动态条件")
    p.add_argument("--run")
    p.add_argument("--gate", default="all")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("review", help="生成人工可读的 Gate 决策摘要")
    p.add_argument("--run")
    p.add_argument("--gate", required=True)
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("approve", help="人工批准 Gate")
    p.add_argument("--run")
    p.add_argument("--gate", required=True)
    p.add_argument("--by", required=True)
    p.add_argument("--note", default="")
    p.add_argument(
        "--strict",
        action="store_true",
        help="兼容参数；approve 无论是否传入都始终执行 strict validation",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="仅跳过前置 Gate 的顺序/状态；当前 Gate strict validation 仍必须通过",
    )
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("revoke", help="撤销 Gate 及后续批准")
    p.add_argument("--run")
    p.add_argument("--gate", required=True)
    p.add_argument("--by", required=True)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_revoke)

    p = sub.add_parser("idea-new", help="创建结构化 Idea Card")
    p.add_argument("--run")
    p.add_argument("--id")
    p.add_argument("--title", required=True)
    p.add_argument("--summary")
    p.add_argument("--families", nargs="+")
    p.add_argument("--target-nodes", nargs="*")
    p.add_argument("--assumptions", nargs="*")
    p.add_argument("--validation", nargs="*")
    p.add_argument("--falsification")
    p.add_argument("--risks", nargs="*")
    p.add_argument("--parent-ids", nargs="*")
    p.add_argument("--source", choices=["codex", "claude-code", "deepseek", "openai-api", "anthropic-api", "manual-pro", "api", "human", "imported"], default="codex")
    p.add_argument("--effort-hours", type=float, default=1.0)
    p.add_argument("--by", default="TEAM")
    p.set_defaults(func=cmd_idea_new)

    p = sub.add_parser("idea-list", help="列出 Idea Card")
    p.add_argument("--run")
    p.set_defaults(func=cmd_idea_list)

    p = sub.add_parser("module-new", help="创建 Module Card")
    p.add_argument("--run")
    p.add_argument("--id")
    p.add_argument("--source-ideas", nargs="+", required=True)
    p.add_argument("--target-nodes", nargs="*")
    p.add_argument("--purpose", required=True)
    p.add_argument("--algorithm")
    p.add_argument("--baseline")
    p.add_argument("--validation", nargs="*")
    p.add_argument("--failure-modes", nargs="*")
    p.add_argument("--owner")
    p.add_argument("--by", default="TEAM")
    p.set_defaults(func=cmd_module_new)

    p = sub.add_parser("module-list", help="列出 Module Card")
    p.add_argument("--run")
    p.set_defaults(func=cmd_module_list)

    p = sub.add_parser("focus-set", help="人工选择深化对象")
    p.add_argument("--run")
    p.add_argument("--ideas", nargs="+", required=True)
    p.add_argument("--modules", nargs="*")
    p.add_argument("--by", required=True)
    p.add_argument("--rationale", required=True)
    p.add_argument("--evidence", nargs="*")
    p.add_argument("--deferred", nargs="*")
    p.set_defaults(func=cmd_focus_set)

    p = sub.add_parser("dispute-add", help="登记专项争议")
    p.add_argument("--run")
    p.add_argument("--id")
    p.add_argument("--title", required=True)
    p.add_argument("--type", required=True, choices=["math", "data", "identifiability", "significance", "optimization", "complexity", "source", "integration", "other"])
    p.add_argument("--severity", choices=["low", "medium", "high", "critical"], default="high")
    p.add_argument("--target", required=True)
    p.add_argument("--question", required=True)
    p.add_argument("--resource")
    p.add_argument("--by", default="TEAM")
    p.set_defaults(func=cmd_dispute_add)

    p = sub.add_parser("dispute-resolve", help="解决或接受争议风险")
    p.add_argument("--run")
    p.add_argument("--id", required=True)
    p.add_argument("--status", choices=["resolved", "closed", "accepted-risk"], default="resolved")
    p.add_argument("--resolution", required=True)
    p.add_argument("--evidence", nargs="*")
    p.add_argument("--by", required=True)
    p.set_defaults(func=cmd_dispute_resolve)

    p = sub.add_parser("claim-add", help="登记论文主张")
    p.add_argument("--run")
    p.add_argument("--id")
    p.add_argument("--claim", required=True)
    p.add_argument("--type", default="result")
    p.add_argument("--paper-section")
    p.add_argument("--core", action="store_true")
    p.add_argument("--note")
    p.set_defaults(func=cmd_claim_add)

    p = sub.add_parser("evidence-add", help="为主张登记证据")
    p.add_argument("--run")
    p.add_argument("--id")
    p.add_argument("--claim", required=True)
    p.add_argument("--type", required=True)
    p.add_argument("--artifact")
    p.add_argument("--result", required=True)
    p.add_argument("--command")
    p.add_argument("--human-verified", action="store_true")
    p.add_argument("--by", default="TEAM")
    p.add_argument("--note")
    p.set_defaults(func=cmd_evidence_add)

    p = sub.add_parser("ai-log", help="登记一次 AI 工具使用")
    p.add_argument("--run")
    p.add_argument("--id")
    p.add_argument("--tool", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--stage", required=True)
    p.add_argument("--purpose", required=True)
    p.add_argument("--prompt-summary", required=True)
    p.add_argument("--input", nargs="*", help="输入文件或资源路径")
    p.add_argument("--output", nargs="*", help="输出文件或资源路径")
    p.add_argument("--adopted", action="store_true")
    p.add_argument("--modified", action="store_true")
    p.add_argument("--verified", action="store_true")
    p.add_argument("--by", default="TEAM")
    p.add_argument("--note")
    p.set_defaults(func=cmd_ai_log)

    p = sub.add_parser("source-add", help="登记外部来源")
    p.add_argument("--run")
    p.add_argument("--id")
    p.add_argument("--title", required=True)
    p.add_argument("--author", required=True)
    p.add_argument("--year", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--type", required=True)
    p.add_argument("--claim", required=True)
    p.add_argument("--by", required=True)
    p.add_argument("--note")
    p.set_defaults(func=cmd_source_add)

    p = sub.add_parser("source-attest-none", help="人工确认本 run 确实未使用外部来源")
    p.add_argument("--run")
    p.add_argument("--by", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_source_attest_none)

    p = sub.add_parser("freeze", help="冻结科学产物哈希")
    p.add_argument("--run")
    p.add_argument("--by", required=True)
    p.add_argument("--note")
    p.add_argument("--replace", action="store_true")
    p.add_argument(
        "--allow-before-g5", action="store_true",
        help="显式记录并仅允许绕过 G5 尚未批准；G0-G4 与已批准 G5 的漂移不可绕过",
    )
    p.set_defaults(func=cmd_freeze)

    p = sub.add_parser("verify-freeze", help="校验冻结后是否变动")
    p.add_argument("--run")
    p.set_defaults(func=cmd_verify_freeze)

    p = sub.add_parser("unfreeze", help="撤销冻结并保留旧清单")
    p.add_argument("--run")
    p.add_argument("--by", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_unfreeze)

    p = sub.add_parser("export-ai", help="导出 AI 使用详情并记录显式人工复核")
    p.add_argument("--run")
    p.add_argument(
        "--by",
        required=True,
        help="调用前已核对源日志/导出字段、并为本次导出具名背书的具体队员",
    )
    p.set_defaults(func=cmd_export_ai)

    p = sub.add_parser(
        "attest-ai-disclosure-pdf",
        help="为人工视觉核对后的 AI 工具使用详情 PDF 写入 hash receipt",
    )
    p.add_argument("--run")
    p.add_argument("--by", required=True, help="完成人工视觉与内容对应核对的具体队员")
    p.set_defaults(func=cmd_attest_ai_disclosure_pdf)

    p = sub.add_parser("regenerate-input-manifest", help="重新计算输入哈希")
    p.add_argument("--run")
    p.set_defaults(func=cmd_regenerate_manifest)

    p = sub.add_parser(
        "build-support",
        help="由显式 allowlist 构建官方匿名支撑材料 ZIP",
    )
    p.add_argument("--run")
    p.add_argument(
        "--include",
        action="append",
        default=[],
        help="要提交的 run 内 canonical 文件路径；每个文件重复一次参数",
    )
    p.add_argument(
        "--identity",
        action="append",
        default=[],
        help="额外队员姓名/身份 token，仅用于匿名扫描并以摘要写入内部回执",
    )
    p.add_argument("--replace", action="store_true")
    p.add_argument("--by", required=True, help="执行匿名复核与构建的具名队员")
    p.set_defaults(func=cmd_build_support)

    p = sub.add_parser("package", help="打包具名内部审计归档（不得作为官方支撑材料）")
    p.add_argument("--run")
    p.add_argument("--out")
    p.add_argument("--replace", action="store_true")
    p.add_argument(
        "--allow-unvalidated", action="store_true",
        help="允许生成未通过当前 G7 的诊断快照，并写入决策日志",
    )
    p.add_argument("--by", required=True)
    p.add_argument("--note")
    p.set_defaults(func=cmd_package)

    p = sub.add_parser("next", help="根据当前 Gate 给出下一步")
    p.add_argument("--run")
    p.set_defaults(func=cmd_next)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
