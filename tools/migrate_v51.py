#!/usr/bin/env python3
"""Explicit, auditable migration for early v5.1 artifact contracts.

The command is a dry run unless ``--apply`` is supplied.  It recognizes only
the known pre-contract headers/shapes below.  Unknown columns and ambiguous
legacy probe rows stop the whole migration before any artifact is changed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from common import (
    ROOT,
    atomic_write_text,
    csv_header,
    die,
    now_iso,
    read_csv,
    read_json,
    resolve_run_evidence_ref,
    sha256_file,
    write_csv,
    write_json,
)
from consistency_matrix import FIELDS as CONSISTENCY_FIELDS
from interpretation_engine import FORK_FIELDS, PROBE_FIELDS
from sensitivity_engine import FIELDS as SENSITIVITY_FIELDS
from sensitivity_engine import REVIEWED_WAIVER_STATUS, VALID_STATUS
from v51_common import resolve_run_flexible, split_values


LEGACY_FORK_FIELDS = [
    "fork_id", "question_ids", "dimension", "ambiguity", "alternatives_json",
    "impact", "uncertainty", "irreversibility", "structural", "risk_score",
    "nondelegable", "status", "selected_alternative", "decision_by",
    "decision_at", "reason", "evidence_refs", "notes",
]
LEGACY_PROBE_FIELDS = [
    "probe_id", "fork_id", "question_ids", "baseline", "alternative", "metric",
    "protocol", "status", "result_ref", "effect_summary", "reviewed_by",
    "reviewed_at", "notes",
]
LEGACY_SENSITIVITY_FIELDS = [
    "case_id", "kind", "fork_id", "target_id", "question_ids", "dimension",
    "baseline", "alternative", "metric", "protocol", "status", "result_ref",
    "effect_size", "decision_impact", "reviewed_by", "reviewed_at",
    "waiver_reason", "notes",
]
PRE_WAIVER_SENSITIVITY_FIELDS = [
    "sensitivity_id", "kind", "target", "alternatives_or_range", "metric",
    "experiment_id", "evidence_path", "observed_effect", "effect_pct", "status",
    "verified_by", "verified_at", "notes",
]
LEGACY_CONSISTENCY_FIELDS = [
    field for field in CONSISTENCY_FIELDS if field != "waiver_reason"
]

STATUS_STAGE_FIELDS = {
    "status", "validated_at", "approved_at", "approved_by",
    "approved_input_sha256", "note", "last_result",
}
VALID_STAGE_STATUS = {"pending", "validated", "blocked", "approved"}
REAPPROVAL_MARKER = "MIGRATION_REAPPROVAL_REQUIRED"


def _bool_text(value: str, *, field: str, row_id: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"", "0", "false", "no", "n"}:
        return "false"
    if normalized in {"1", "true", "yes", "y"}:
        return "true"
    die(f"{row_id}: {field} 无法迁移为布尔值：{value!r}")


def _notes(original: str, preserved: dict[str, str]) -> str:
    nonempty = {key: value for key, value in preserved.items() if str(value).strip()}
    if not nonempty:
        return original or ""
    marker = "MIGRATED_V51=" + json.dumps(nonempty, ensure_ascii=False, sort_keys=True)
    return "; ".join(part for part in (original or "", marker) if part)


def _options(raw: str, *, row_id: str) -> tuple[str, set[str]]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        die(f"{row_id}: alternatives_json 不是有效 JSON：{exc}")
    if not isinstance(payload, list) or len(payload) < 2:
        die(f"{row_id}: alternatives_json 必须至少包含两个对象，无法安全迁移")
    normalized: list[dict[str, str]] = []
    ids: set[str] = set()
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            die(f"{row_id}: alternative #{index} 不是对象，无法推导稳定 option id")
        unknown = set(item) - {"id", "label", "description"}
        if unknown:
            die(f"{row_id}: alternative #{index} 含无法映射字段：{sorted(unknown)}")
        option_id = str(item.get("id", "")).strip()
        label = str(item.get("label") or item.get("description") or "").strip()
        if not option_id or not label:
            die(f"{row_id}: alternative #{index} 必须包含 id 和 label/description")
        if option_id in ids:
            die(f"{row_id}: alternative id 重复：{option_id}")
        ids.add(option_id)
        normalized.append({"id": option_id, "label": label})
    return json.dumps(normalized, ensure_ascii=False), ids


def _migrate_forks(
    rows: list[dict[str, str]], decisions: dict[str, Any], *, run: Path
) -> list[dict[str, Any]]:
    migrated: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, row in enumerate(rows, start=1):
        row_id = str(row.get("fork_id") or f"row-{index}")
        if row_id in seen_ids:
            die(f"重复 fork_id：{row_id}")
        seen_ids.add(row_id)
        question = str(row.get("ambiguity", "")).strip()
        if not question:
            die(f"{row_id}: ambiguity 为空，无法迁移到必填 question")
        options_json, option_ids = _options(str(row.get("alternatives_json", "")), row_id=row_id)
        selected = str(row.get("selected_alternative", "")).strip()
        if selected and selected not in option_ids:
            die(f"{row_id}: selected_alternative={selected!r} 不属于 option ids {sorted(option_ids)}")
        for field in ("dimension", "impact", "uncertainty", "irreversibility", "status"):
            if not str(row.get(field, "")).strip():
                die(f"{row_id}: 必填字段 {field} 为空")
        risk_score = str(row.get("risk_score", "")).strip()
        if not risk_score.isdigit():
            die(f"{row_id}: risk_score 必须是非负整数：{risk_score!r}")
        decision = decisions.get(row_id)
        if not isinstance(decision, dict):
            die(
                f"{row_id}: 旧 schema 没有 changes_objective、changes_feasible_set 和 "
                "expected_result_change_pct；请用 --fork-decisions 提供逐 fork 的显式迁移决定"
            )
        unknown_decisions = set(decision) - {
            "changes_objective", "changes_feasible_set", "expected_result_change_pct"
        }
        if unknown_decisions:
            die(f"{row_id}: fork decision 含未知字段：{sorted(unknown_decisions)}")
        for boolean_field in ("changes_objective", "changes_feasible_set"):
            if not isinstance(decision.get(boolean_field), bool):
                die(f"{row_id}: fork decision 的 {boolean_field} 必须是 JSON boolean")
        expected_change = decision.get("expected_result_change_pct")
        if expected_change is not None and (
            isinstance(expected_change, bool) or not isinstance(expected_change, (int, float))
        ):
            die(f"{row_id}: expected_result_change_pct 必须是 number 或 null")
        status = str(row.get("status", "")).strip()
        evidence_refs = split_values(row.get("evidence_refs", ""))
        if status == "decided" and not evidence_refs:
            die(f"{row_id}: decided 旧记录缺少可迁移 evidence")
        for evidence_ref in evidence_refs:
            _, evidence_errors = resolve_run_evidence_ref(run, evidence_ref)
            if evidence_errors:
                die(
                    f"{row_id}: evidence 无法解析：{evidence_ref}: "
                    + "; ".join(evidence_errors)
                )
        migrated.append({
            "fork_id": row_id,
            "dimension": row.get("dimension", ""),
            "question": question,
            "options_json": options_json,
            "impact": row.get("impact", ""),
            "uncertainty": row.get("uncertainty", ""),
            "irreversibility": row.get("irreversibility", ""),
            "changes_objective": str(decision["changes_objective"]).lower(),
            "changes_feasible_set": str(decision["changes_feasible_set"]).lower(),
            "expected_result_change_pct": "" if expected_change is None else expected_change,
            "structural_fork": _bool_text(row.get("structural", ""), field="structural", row_id=row_id),
            "nondelegable": _bool_text(row.get("nondelegable", ""), field="nondelegable", row_id=row_id),
            "risk_score": risk_score,
            "status": status,
            "selected_option": selected,
            "decision_by": row.get("decision_by", ""),
            "decision_at": row.get("decision_at", ""),
            "evidence": ";".join(evidence_refs),
            "reason": row.get("reason", ""),
            "notes": _notes(row.get("notes", ""), {
                "legacy_question_ids": row.get("question_ids", ""),
            }),
        })
    unused_decisions = sorted(set(decisions) - seen_ids)
    if unused_decisions:
        die(f"--fork-decisions 含当前旧 ledger 中不存在的 fork_id：{unused_decisions}")
    return migrated


def _migrate_sensitivity(
    rows: list[dict[str, str]], *, run: Path, expected_kind: str, pre_waiver: bool
) -> list[dict[str, Any]]:
    migrated: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        if pre_waiver:
            item = {field: row.get(field, "") for field in SENSITIVITY_FIELDS}
            item["waiver_reason"] = ""
            row_id = str(item.get("sensitivity_id") or f"row-{index}")
        else:
            row_id = str(row.get("case_id") or f"row-{index}")
            kind = str(row.get("kind") or expected_kind).strip()
            if kind != expected_kind:
                die(f"{row_id}: kind={kind!r} 与目标文件 {expected_kind!r} 不一致")
            target_parts = [
                f"fork={row['fork_id']}" if row.get("fork_id") else "",
                f"target={row['target_id']}" if row.get("target_id") else "",
            ]
            target = "; ".join(part for part in target_parts if part)
            alternatives = "; ".join(
                part for part in (
                    f"baseline={row['baseline']}" if row.get("baseline") else "",
                    f"alternative={row['alternative']}" if row.get("alternative") else "",
                ) if part
            )
            item = {
                "sensitivity_id": row_id,
                "kind": kind,
                "target": target,
                "alternatives_or_range": alternatives,
                "metric": row.get("metric", ""),
                "experiment_id": "",
                "evidence_path": row.get("result_ref", ""),
                "observed_effect": row.get("effect_size", ""),
                "effect_pct": "",
                "status": row.get("status", "") or "planned",
                "verified_by": row.get("reviewed_by", ""),
                "verified_at": row.get("reviewed_at", ""),
                "waiver_reason": row.get("waiver_reason", ""),
                "notes": _notes(row.get("notes", ""), {
                    "legacy_question_ids": row.get("question_ids", ""),
                    "legacy_dimension": row.get("dimension", ""),
                    "legacy_protocol": row.get("protocol", ""),
                    "legacy_decision_impact": row.get("decision_impact", ""),
                }),
            }
        status = str(item.get("status", ""))
        if str(item.get("kind", "")) != expected_kind:
            die(f"{row_id}: kind={item.get('kind')!r} 与目标文件 {expected_kind!r} 不一致")
        if status not in VALID_STATUS:
            die(f"{row_id}: status 无效：{status!r}")
        if not str(item.get("target", "")).strip():
            die(f"{row_id}: 无法从旧字段得到 target")
        if not str(item.get("alternatives_or_range", "")).strip():
            die(f"{row_id}: 无法从旧字段得到 alternatives_or_range")
        if not str(item.get("metric", "")).strip():
            die(f"{row_id}: metric 为空")
        if status == "verified" and (
            not str(item.get("evidence_path", "")).strip()
            or not str(item.get("verified_by", "")).strip()
        ):
            die(f"{row_id}: verified 行缺少 evidence_path 或 reviewer，拒绝迁移")
        if status == "verified":
            _, evidence_errors = resolve_run_evidence_ref(
                run, str(item.get("evidence_path", ""))
            )
            if evidence_errors:
                die(f"{row_id}: verified evidence 无法解析：" + "; ".join(evidence_errors))
        if status in REVIEWED_WAIVER_STATUS and (
            not str(item.get("verified_by", "")).strip()
            or not str(item.get("waiver_reason", "")).strip()
        ):
            die(f"{row_id}: {status} 行缺少 reviewer 或显式 waiver_reason，拒绝迁移")
        migrated.append(item)
    return migrated


def _migrate_consistency(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Insert the new waiver column without reinterpreting legacy notes."""
    return [
        {
            field: "" if field == "waiver_reason" else row.get(field, "")
            for field in CONSISTENCY_FIELDS
        }
        for row in rows
    ]


def _migrate_fingerprints(payload: dict[str, Any]) -> dict[str, Any] | None:
    candidates = payload.get("candidates")
    if isinstance(candidates, list) and isinstance(payload.get("dimensions"), list):
        for index, item in enumerate(candidates, start=1):
            if not isinstance(item, dict) or not str(item.get("id", "")).strip():
                die(f"canonical fingerprint candidate #{index} 无效")
            values = item.get("values")
            if not isinstance(values, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in values.items()
            ):
                die(f"canonical fingerprint candidate #{index} values 无效")
        return None
    if not isinstance(candidates, dict):
        die("ASSUMPTION_FINGERPRINTS.json candidates 既不是旧对象也不是当前数组")
    unknown_top = set(payload) - {"schema_version", "updated_at", "candidates"}
    if unknown_top:
        die(f"ASSUMPTION_FINGERPRINTS.json 含无法映射字段：{sorted(unknown_top)}")
    migrated: list[dict[str, Any]] = []
    timestamp = now_iso()
    for registry_key, item in candidates.items():
        if not isinstance(item, dict):
            die(f"fingerprint {registry_key!r} 不是对象")
        unknown = set(item) - {"candidate_id", "assumptions", "stage", "source_ref"}
        if unknown:
            die(f"fingerprint {registry_key!r} 含无法映射字段：{sorted(unknown)}")
        candidate_id = str(item.get("candidate_id") or registry_key).strip()
        if candidate_id != str(registry_key):
            die(f"fingerprint key={registry_key!r} 与 candidate_id={candidate_id!r} 不一致")
        assumptions = item.get("assumptions")
        if not isinstance(assumptions, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in assumptions.items()
        ):
            die(f"fingerprint {registry_key!r} assumptions 必须是字符串映射")
        candidate = {
            "id": candidate_id,
            "values": assumptions,
            "source": str(item.get("source_ref", "")),
            "updated_at": timestamp,
        }
        if item.get("stage"):
            candidate["stage"] = str(item["stage"])
        migrated.append(candidate)
    dimensions = sorted({key for item in migrated for key in item["values"]})
    return {
        "schema_version": 1,
        "dimensions": dimensions,
        "candidates": migrated,
        "updated_at": payload.get("updated_at") or timestamp,
    }


def _current_stage_contract() -> tuple[str, list[str], str]:
    registry_path = ROOT / "config" / "v51_stage_registry.json"
    registry = read_json(registry_path, expect=dict)
    version = str(registry.get("version", "")).strip()
    raw_stages = registry.get("stages")
    if not version or not isinstance(raw_stages, list):
        die(f"v5.1 stage registry 缺少有效 version/stages：{registry_path}")
    stage_ids: list[str] = []
    for index, item in enumerate(raw_stages, start=1):
        if not isinstance(item, dict):
            die(f"v5.1 stage registry stage #{index} 必须是对象")
        stage_id = str(item.get("id", "")).strip()
        if not stage_id:
            die(f"v5.1 stage registry stage #{index} 缺少非空 id")
        if stage_id in stage_ids:
            die(f"v5.1 stage registry stage id 重复：{stage_id}")
        stage_ids.append(stage_id)
    if not stage_ids:
        die("v5.1 stage registry 不得为空")
    return version, stage_ids, sha256_file(registry_path)


def _valid_iso_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _pending_stage(*, note: str = "") -> dict[str, Any]:
    return {
        "status": "pending",
        "validated_at": None,
        "approved_at": None,
        "approved_by": None,
        "approved_input_sha256": None,
        "note": note,
        "last_result": None,
    }


def _approval_errors(
    stage_id: str,
    item: dict[str, Any],
    *,
    registry_sha256: str,
    registry_changed: bool,
) -> list[str]:
    errors: list[str] = []
    if registry_changed:
        errors.append("stage registry changed")
    approved_hash = item.get("approved_input_sha256")
    if not _valid_sha256(approved_hash):
        errors.append("approved_input_sha256 missing or invalid")
    if not isinstance(item.get("approved_by"), str) or not item["approved_by"].strip():
        errors.append("approved_by missing")
    if not _valid_iso_timestamp(item.get("approved_at")):
        errors.append("approved_at missing or invalid")
    if not _valid_iso_timestamp(item.get("validated_at")):
        errors.append("validated_at missing or invalid")
    result = item.get("last_result")
    if not isinstance(result, dict):
        errors.append("last_result missing")
        return errors
    if result.get("stage") != stage_id:
        errors.append("last_result stage mismatch")
    if result.get("passed") is not True or result.get("errors") != []:
        errors.append("last_result is not a clean pass")
    if not isinstance(result.get("input_manifest"), list):
        errors.append("last_result input_manifest missing or invalid")
    if result.get("input_sha256") != approved_hash:
        errors.append("last_result input hash does not match approval")
    if result.get("stage_registry_sha256") != registry_sha256:
        errors.append("last_result stage registry mismatch")
    return errors


def _nonapproved_lifecycle_errors(
    stage_id: str,
    item: dict[str, Any],
    *,
    registry_sha256: str,
    registry_changed: bool,
) -> list[str]:
    """Return reasons a validated/blocked legacy record cannot be preserved."""
    status = item.get("status")
    errors: list[str] = []
    if registry_changed:
        errors.append("stage registry changed")
    if not _valid_iso_timestamp(item.get("validated_at")):
        errors.append("validated_at missing or invalid")
    result = item.get("last_result")
    if not isinstance(result, dict):
        return errors + ["last_result missing"]
    if result.get("stage") != stage_id:
        errors.append("last_result stage mismatch")
    if not isinstance(result.get("passed"), bool):
        errors.append("last_result passed is not boolean")
    if not isinstance(result.get("errors"), list):
        errors.append("last_result errors is not an array")
    if not isinstance(result.get("input_manifest"), list):
        errors.append("last_result input_manifest is not an array")
    if not _valid_sha256(result.get("input_sha256")):
        errors.append("last_result input_sha256 is invalid")
    if result.get("stage_registry_sha256") != registry_sha256:
        errors.append("last_result stage registry mismatch")
    if status == "validated" and (
        result.get("passed") is not True or result.get("errors") != []
    ):
        errors.append("validated status lacks a clean passing result")
    if status == "blocked" and (
        result.get("passed") is not False
        or not isinstance(result.get("errors"), list)
        or not result.get("errors")
    ):
        errors.append("blocked status lacks a failing result with errors")
    return errors


def _reapproval_note(existing: str, reasons: list[str]) -> str:
    marker = f"{REAPPROVAL_MARKER}: " + "; ".join(reasons)
    if REAPPROVAL_MARKER in existing:
        return existing
    return "; ".join(part for part in (existing, marker) if part)


def _migrate_status(payload: dict[str, Any], run: Path) -> dict[str, Any] | None:
    unknown = set(payload) - {
        "version", "run_id", "created_at", "initialized_at", "updated_at",
        "stage_registry_sha256", "stages",
    }
    if unknown:
        die(f"V51_STATUS.json 含无法映射字段：{sorted(unknown)}")
    stages = payload.get("stages")
    if not isinstance(stages, dict):
        die("V51_STATUS.json stages 必须是对象")
    version, stage_ids, registry_hash = _current_stage_contract()
    current_stage_ids = set(stage_ids)
    unknown_stages = sorted(set(stages) - current_stage_ids)
    if unknown_stages:
        die(f"V51_STATUS.json 含注册表中不存在的 stage：{unknown_stages}")

    raw_run_id = payload.get("run_id")
    if raw_run_id is not None and str(raw_run_id).strip() and str(raw_run_id).strip() != run.name:
        die(
            f"V51_STATUS.json run_id={raw_run_id!r} 与当前 run={run.name!r} 不一致，"
            "拒绝把其他 run 的审批状态迁入"
        )

    original_registry_hash = payload.get("stage_registry_sha256")
    registry_changed = original_registry_hash != registry_hash
    normalized_stages: dict[str, Any] = {}
    for stage_id in stage_ids:
        if stage_id not in stages:
            normalized_stages[stage_id] = _pending_stage()
            continue
        item = stages[stage_id]
        if not isinstance(item, dict):
            die(f"V51_STATUS.json stage {stage_id!r} 必须是对象")
        unknown_stage = set(item) - STATUS_STAGE_FIELDS
        if unknown_stage:
            die(f"V51_STATUS.json stage {stage_id!r} 含未知字段：{sorted(unknown_stage)}")
        status = item.get("status")
        if status not in VALID_STAGE_STATUS:
            die(f"V51_STATUS.json stage {stage_id!r} status 无效：{status!r}")
        note = item.get("note", "")
        if note is None:
            note = ""
        if not isinstance(note, str):
            die(f"V51_STATUS.json stage {stage_id!r} note 必须是字符串")
        normalized = {
            "status": status,
            "validated_at": item.get("validated_at"),
            "approved_at": item.get("approved_at"),
            "approved_by": item.get("approved_by"),
            "approved_input_sha256": item.get("approved_input_sha256"),
            "note": note,
            "last_result": item.get("last_result"),
        }
        if status == "approved":
            approval_errors = _approval_errors(
                stage_id,
                normalized,
                registry_sha256=registry_hash,
                registry_changed=registry_changed,
            )
            if approval_errors:
                normalized = _pending_stage(
                    note=_reapproval_note(note, approval_errors)
                )
        elif status == "pending":
            residue = [
                field
                for field in (
                    "validated_at", "approved_at", "approved_by",
                    "approved_input_sha256", "last_result",
                )
                if normalized.get(field) is not None
            ]
            normalized = _pending_stage(
                note=(
                    _reapproval_note(note, ["pending record retained lifecycle fields: " + ", ".join(residue)])
                    if residue else note
                )
            )
        else:
            normalized.update({
                "approved_at": None,
                "approved_by": None,
                "approved_input_sha256": None,
            })
            lifecycle_errors = _nonapproved_lifecycle_errors(
                stage_id,
                normalized,
                registry_sha256=registry_hash,
                registry_changed=registry_changed,
            )
            if lifecycle_errors:
                normalized = _pending_stage(
                    note=_reapproval_note(note, lifecycle_errors)
                )
        normalized_stages[stage_id] = normalized

    created_at = payload.get("created_at") or payload.get("initialized_at") or now_iso()
    updated_at = payload.get("updated_at") or now_iso()
    if not _valid_iso_timestamp(created_at):
        die("V51_STATUS.json created_at/initialized_at 必须是带时区的 ISO-8601 时间")
    if not _valid_iso_timestamp(updated_at):
        die("V51_STATUS.json updated_at 必须是带时区的 ISO-8601 时间")
    migrated = {
        "version": version,
        "run_id": run.name,
        "created_at": created_at,
        "updated_at": updated_at,
        "stage_registry_sha256": registry_hash,
        "stages": normalized_stages,
    }
    if payload == migrated:
        return None
    migrated["updated_at"] = now_iso()
    return migrated


def _migrate_abstract_facts(payload: dict[str, Any], run: Path) -> dict[str, Any] | None:
    allowed_top_fields = {"schema_version", "keywords", "facts"}
    unknown_top_fields = sorted(set(payload) - allowed_top_fields)
    if unknown_top_fields:
        die(
            "paper/ABSTRACT_FACTS.json 含未知顶层字段，拒绝有损迁移："
            f"{unknown_top_fields}"
        )
    schema_version = payload.get("schema_version", 1)
    if schema_version != 1:
        die(
            "paper/ABSTRACT_FACTS.json schema_version 必须为 1；"
            f"收到 {schema_version!r}"
        )
    facts = payload.get("facts")
    if not isinstance(facts, list):
        die("paper/ABSTRACT_FACTS.json facts 必须是数组")
    keywords = payload.get("keywords", [])
    if not isinstance(keywords, list) or any(
        not isinstance(keyword, str) or not keyword.strip() for keyword in keywords
    ):
        die("paper/ABSTRACT_FACTS.json keywords 必须是非空字符串数组")
    normalized_keywords = [keyword.strip() for keyword in keywords]
    if len(normalized_keywords) != len(set(normalized_keywords)):
        die("paper/ABSTRACT_FACTS.json keywords 不得重复")
    if facts and not normalized_keywords:
        die(
            "paper/ABSTRACT_FACTS.json 已有事实但没有关键词；"
            "关键词无法可靠推断，请人工补充后再迁移"
        )
    ledger_by_evidence: dict[str, list[str]] = {}
    for row in read_csv(run / "evidence" / "CLAIM_EVIDENCE_LEDGER.csv"):
        evidence_id = str(row.get("evidence_id", "")).strip()
        claim_id = str(row.get("claim_id", "")).strip()
        if evidence_id:
            ledger_by_evidence.setdefault(evidence_id, []).append(claim_id)
    changed = (
        "schema_version" not in payload
        or "keywords" not in payload
        or keywords != normalized_keywords
    )
    migration_timestamp = now_iso()
    allowed_fact_fields = {
        "fact_id", "question", "purpose", "method", "result", "scope",
        "claim_ids", "evidence_ids", "limitations", "frozen",
        "verified_by", "created_at",
    }
    required_legacy_fields = allowed_fact_fields - {"claim_ids", "created_at"}
    migrated_facts: list[dict[str, Any]] = []
    for index, fact in enumerate(facts, start=1):
        if not isinstance(fact, dict):
            die(f"paper/ABSTRACT_FACTS.json fact #{index} 必须是对象")
        item = dict(fact)
        unknown_fact_fields = sorted(set(item) - allowed_fact_fields)
        if unknown_fact_fields:
            die(
                f"paper abstract fact #{index} 含未知字段，拒绝有损迁移："
                f"{unknown_fact_fields}"
            )
        missing_legacy_fields = sorted(required_legacy_fields - set(item))
        if missing_legacy_fields:
            die(
                f"paper abstract fact #{index} 缺少无法可靠推断的字段："
                f"{missing_legacy_fields}"
            )
        raw_claim_ids = item.get("claim_ids")
        if isinstance(raw_claim_ids, list) and raw_claim_ids:
            normalized_claim_ids = [str(value).strip() for value in raw_claim_ids]
            if any(not value for value in normalized_claim_ids):
                die(f"paper abstract fact #{index} claim_ids 含空值")
            if len(normalized_claim_ids) != len(set(normalized_claim_ids)):
                die(f"paper abstract fact #{index} claim_ids 不得重复")
            if raw_claim_ids != normalized_claim_ids:
                item["claim_ids"] = normalized_claim_ids
                changed = True
        else:
            evidence_ids = item.get("evidence_ids")
            if not isinstance(evidence_ids, list) or not evidence_ids:
                die(f"paper abstract fact #{index} 缺少 evidence_ids，无法推导 claim_ids")
            derived: set[str] = set()
            for raw_evidence_id in evidence_ids:
                evidence_id = str(raw_evidence_id).strip()
                matches = ledger_by_evidence.get(evidence_id, [])
                if len(matches) != 1 or not matches[0]:
                    die(
                        f"paper abstract fact #{index} evidence_id={evidence_id!r} "
                        "未唯一绑定非空 claim_id，拒绝猜测迁移"
                    )
                derived.add(matches[0])
            item["claim_ids"] = sorted(derived)
            changed = True
        created_at = item.get("created_at")
        if created_at is None:
            item["created_at"] = migration_timestamp
            changed = True
        elif not isinstance(created_at, str) or not _valid_iso_timestamp(created_at):
            die(
                f"paper abstract fact #{index} created_at 必须是带时区的 ISO-8601 时间"
            )
        migrated_facts.append(item)
    if not changed:
        return None
    migrated = dict(payload)
    migrated["schema_version"] = 1
    migrated["keywords"] = normalized_keywords
    migrated["facts"] = migrated_facts
    return migrated


def _plan_csv(
    run: Path,
    relative: str,
    current: list[str],
    legacy: list[str],
    converter,
    *,
    migration: str,
) -> dict[str, Any] | None:
    path = run / relative
    if not path.exists():
        return None
    actual = csv_header(path)
    if actual == current:
        return None
    if actual != legacy:
        die(f"{relative}: 未知 CSV 表头，拒绝迁移：{actual}")
    rows = read_csv(path)
    return {
        "path": path,
        "relative": relative,
        "kind": "csv",
        "fields": current,
        "payload": converter(rows),
        "migration": migration,
        "row_count": len(rows),
    }


def build_plan(run: Path, *, fork_decisions: dict[str, Any]) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    item = _plan_csv(
        run, "interpretation/INTERPRETATION_FORKS.csv", FORK_FIELDS,
        LEGACY_FORK_FIELDS, lambda rows: _migrate_forks(rows, fork_decisions, run=run),
        migration="interpretation-forks-v0-to-v1",
    )
    if item:
        plan.append(item)

    probe_path = run / "interpretation/FORK_PROBES.csv"
    if probe_path.exists() and csv_header(probe_path) != PROBE_FIELDS:
        actual = csv_header(probe_path)
        if actual != LEGACY_PROBE_FIELDS:
            die(f"interpretation/FORK_PROBES.csv: 未知 CSV 表头，拒绝迁移：{actual}")
        probe_rows = read_csv(probe_path)
        if probe_rows:
            die(
                "旧 FORK_PROBES.csv 的一行同时表达 baseline/alternative 比较，"
                "无法无损变成每 option 一行的新 probe。请人工拆分旧行后再迁移。"
            )
        plan.append({
            "path": probe_path,
            "relative": "interpretation/FORK_PROBES.csv",
            "kind": "csv",
            "fields": PROBE_FIELDS,
            "payload": [],
            "migration": "empty-probe-header-v0-to-v1",
            "row_count": 0,
        })

    for kind, name in (("parameter", "PARAMETER_SENSITIVITY.csv"), ("structural", "STRUCTURAL_SENSITIVITY.csv")):
        relative = f"sensitivity/{name}"
        path = run / relative
        if not path.exists():
            continue
        actual = csv_header(path)
        if actual == SENSITIVITY_FIELDS:
            continue
        if actual == LEGACY_SENSITIVITY_FIELDS:
            rows = _migrate_sensitivity(read_csv(path), run=run, expected_kind=kind, pre_waiver=False)
            migration = "sensitivity-v0-to-v2"
        elif actual == PRE_WAIVER_SENSITIVITY_FIELDS:
            rows = _migrate_sensitivity(read_csv(path), run=run, expected_kind=kind, pre_waiver=True)
            migration = "sensitivity-v1-add-waiver-reason"
        else:
            die(f"{relative}: 未知 CSV 表头，拒绝迁移：{actual}")
        plan.append({
            "path": path,
            "relative": relative,
            "kind": "csv",
            "fields": SENSITIVITY_FIELDS,
            "payload": rows,
            "migration": migration,
            "row_count": len(rows),
        })

    item = _plan_csv(
        run,
        "consistency/CONSISTENCY_MATRIX.csv",
        CONSISTENCY_FIELDS,
        LEGACY_CONSISTENCY_FIELDS,
        _migrate_consistency,
        migration="consistency-matrix-add-waiver-reason",
    )
    if item:
        plan.append(item)

    fingerprint_path = run / "interpretation/ASSUMPTION_FINGERPRINTS.json"
    if fingerprint_path.exists():
        migrated = _migrate_fingerprints(read_json(fingerprint_path, expect=dict))
        if migrated is not None:
            plan.append({
                "path": fingerprint_path,
                "relative": "interpretation/ASSUMPTION_FINGERPRINTS.json",
                "kind": "json",
                "payload": migrated,
                "migration": "assumption-fingerprints-object-to-array",
                "row_count": len(migrated["candidates"]),
            })

    status_path = run / "V51_STATUS.json"
    if status_path.exists():
        migrated = _migrate_status(read_json(status_path, expect=dict), run)
        if migrated is not None:
            plan.append({
                "path": status_path,
                "relative": "V51_STATUS.json",
                "kind": "json",
                "payload": migrated,
                "migration": "v51-status-metadata-v0-to-v1",
                "row_count": len(migrated["stages"]),
            })

    abstract_facts_path = run / "paper" / "ABSTRACT_FACTS.json"
    if abstract_facts_path.exists():
        migrated = _migrate_abstract_facts(
            read_json(abstract_facts_path, expect=dict), run
        )
        if migrated is not None:
            plan.append({
                "path": abstract_facts_path,
                "relative": "paper/ABSTRACT_FACTS.json",
                "kind": "json",
                "payload": migrated,
                "migration": "abstract-facts-v1-contract",
                "row_count": len(migrated["facts"]),
            })
    return plan


def _write_replacement(item: dict[str, Any]) -> None:
    target: Path = item["path"]
    temporary = target.with_name(f".{target.name}.migrate-{os.getpid()}.tmp")
    try:
        if item["kind"] == "csv":
            write_csv(temporary, item["fields"], item["payload"])
        else:
            atomic_write_text(
                temporary,
                json.dumps(item["payload"], ensure_ascii=False, indent=2) + "\n",
            )
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def apply_plan(
    run: Path,
    plan: list[dict[str, Any]],
    *,
    decision_source: dict[str, str] | None,
) -> Path | None:
    if not plan:
        return None
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    backup_root = run / "migrations" / f"v51-contract-{stamp}"
    backup_root.mkdir(parents=True, exist_ok=False)
    records: list[dict[str, Any]] = []
    for item in plan:
        target: Path = item["path"]
        backup = backup_root / item["relative"]
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup)
        records.append({
            "path": item["relative"],
            "migration": item["migration"],
            "row_count": item["row_count"],
            "before_sha256": sha256_file(target),
            "backup": backup.relative_to(run).as_posix(),
            "after_sha256": None,
        })
    manifest_path = backup_root / "MIGRATION_MANIFEST.json"
    manifest = {
        "schema_version": 1,
        "migration": "v5.1-artifact-contract-convergence",
        "run_id": run.name,
        "started_at": now_iso(),
        "completed_at": None,
        "status": "backed-up",
        "fork_decisions": decision_source,
        "records": records,
    }
    write_json(manifest_path, manifest)
    for item, record in zip(plan, records):
        _write_replacement(item)
        record["after_sha256"] = sha256_file(item["path"])
    manifest.update({"completed_at": now_iso(), "status": "complete"})
    write_json(manifest_path, manifest)
    return manifest_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", help="run id or path below runs/")
    parser.add_argument("--apply", action="store_true", help="write after backing up every changed artifact")
    parser.add_argument(
        "--fork-decisions",
        help=(
            "JSON object keyed by fork_id with explicit changes_objective, "
            "changes_feasible_set and expected_result_change_pct values"
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    run = resolve_run_flexible(args.run)
    fork_decisions: dict[str, Any] = {}
    decision_source: dict[str, str] | None = None
    if args.fork_decisions:
        decision_path = Path(args.fork_decisions).expanduser().resolve()
        if not decision_path.is_file():
            die(f"fork decisions 文件不存在：{decision_path}")
        fork_decisions = read_json(decision_path, expect=dict)
        decision_source = {"path": str(decision_path), "sha256": sha256_file(decision_path)}
    plan = build_plan(run, fork_decisions=fork_decisions)
    summary = {
        "run": run.name,
        "apply": bool(args.apply),
        "change_count": len(plan),
        "changes": [
            {
                "path": item["relative"],
                "migration": item["migration"],
                "row_count": item["row_count"],
            }
            for item in plan
        ],
    }
    if decision_source:
        summary["fork_decisions"] = decision_source
    if not args.apply:
        summary["note"] = "Dry run only; rerun with --apply after reviewing every mapping."
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    manifest = apply_plan(run, plan, decision_source=decision_source)
    summary["manifest"] = manifest.relative_to(run).as_posix() if manifest else None
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
