#!/usr/bin/env python3
"""Deterministic, human-facing Gate review summaries.

The Gate validator remains the machine source of truth.  This module renders a
small decision surface over that result and selected run artifacts.  Review
files are deliberately excluded from the validator's input manifest: approval
code binds the review hash and the underlying validator hash separately.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote

from common import atomic_write_text, sha256_file


SCHEMA_VERSION = 2
VALID_GATES = tuple(f"G{index}" for index in range(8))
MAX_DECISIONS = 10
MAX_RESULTS = 12
MAX_REFERENCES = 16
MAX_TEXT = 240
_METADATA_START = "<!-- CUMCM_GATE_REVIEW_METADATA\n"
_METADATA_END = "\n-->"


def _clean(value: Any, *, limit: int = MAX_TEXT) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")
    if len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return text


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "是"}


def _json_copy(value: Any) -> Any:
    """Return a JSON-only copy or a stable textual fallback."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError):
        return _clean(value, limit=1000)


def _normal_manifest(validation: dict[str, Any]) -> list[dict[str, Any]]:
    raw = validation.get("input_manifest", [])
    if not isinstance(raw, list):
        return []
    records = [_json_copy(item) for item in raw if isinstance(item, dict)]
    records.sort(
        key=lambda item: json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    )
    return records


def _underlying_input_sha256(validation: dict[str, Any]) -> str:
    value = str(validation.get("input_sha256", "")).strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    encoded = json.dumps(
        _normal_manifest(validation),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _gate(validation: dict[str, Any]) -> str:
    gate = str(validation.get("gate", "")).strip().upper()
    if gate not in VALID_GATES:
        raise ValueError(f"gate 必须是 G0..G7，实际为 {gate!r}")
    return gate


def review_path(run: Path, gate: str) -> Path:
    """Return the canonical review path below ``run``."""
    normalized = str(gate).strip().upper()
    if normalized not in VALID_GATES:
        raise ValueError(f"gate 必须是 G0..G7，实际为 {normalized!r}")
    root = run.resolve()
    relative = f"reviews/gates/{normalized}_REVIEW.md"
    try:
        path = (root / relative).resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError("Gate 人工审核摘要路径无法安全解析") from exc
    try:
        actual = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("Gate 人工审核摘要路径离开当前 run") from exc
    if actual != relative:
        raise ValueError(
            "Gate 人工审核摘要路径包含符号链接或非 canonical 组件："
            f"{relative} -> {actual}"
        )
    return path


def _metadata_core(run: Path, validation: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run.resolve().name,
        "gate": _gate(validation),
        "input_sha256": _underlying_input_sha256(validation),
        "input_manifest": _normal_manifest(validation),
    }


def _metadata(
    run: Path,
    validation: dict[str, Any],
    *,
    previous_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = _metadata_core(run, validation)
    comparison_manifest = None
    if previous_metadata and isinstance(previous_metadata.get("input_manifest"), list):
        comparison_manifest = _normal_manifest(previous_metadata)
    metadata["comparison_input_manifest"] = comparison_manifest
    return metadata


def _metadata_matches_current(
    metadata: dict[str, Any] | None,
    current: dict[str, Any],
) -> bool:
    return bool(
        isinstance(metadata, dict)
        and all(metadata.get(key) == value for key, value in current.items())
    )


def _comparison_metadata(metadata: dict[str, Any]) -> dict[str, Any] | None:
    manifest = metadata.get("comparison_input_manifest")
    return {"input_manifest": manifest} if isinstance(manifest, list) else None


def parse_gate_review(text: str) -> dict[str, Any] | None:
    """Parse the final metadata block from review Markdown text."""
    marker = text.rfind(_METADATA_START)
    if marker < 0 or text[marker:].strip().endswith("-->") is False:
        return None
    payload = text[marker + len(_METADATA_START) :].strip()
    if not payload.endswith("-->"):
        return None
    payload = payload[: -len("-->")].rstrip()
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def parse_gate_review_metadata(path: Path) -> dict[str, Any] | None:
    try:
        return parse_gate_review(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError):
        return None


def _read_csv(run: Path, relative: str) -> list[dict[str, str]]:
    path = _existing_run_file(run, relative)
    if path is None:
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error):
        return []


def _read_json(run: Path, relative: str) -> dict[str, Any]:
    path = _existing_run_file(run, relative)
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _path_part(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    # Evidence selectors use fragments, but the link should target the file.
    return text.split("#", 1)[0]


def _existing_run_file(run: Path, value: Any) -> Path | None:
    relative = _path_part(value)
    if (
        not relative
        or relative.startswith(("/", "//"))
        or ":" in relative
        or "\x00" in relative
        or re.match(r"^[A-Za-z]:", relative)
    ):
        return None
    root = run.resolve()
    try:
        path = (root / relative).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    try:
        actual = path.relative_to(root).as_posix()
    except ValueError:
        return None
    if actual != Path(relative).as_posix():
        return None
    return path if path.is_file() else None


def _canonical_relative(run: Path, value: Any) -> str | None:
    path = _existing_run_file(run, value)
    if path is None:
        return None
    return path.relative_to(run.resolve()).as_posix()


def _md_link(run: Path, value: Any, *, label: str | None = None) -> str | None:
    relative = _canonical_relative(run, value)
    if relative is None:
        return None
    safe_label = _clean(label or relative, limit=140).replace("[", "\\[").replace("]", "\\]")
    href = "../../" + quote(relative, safe="/._~-")
    return f"[{safe_label}]({href})"


def _add_ref(references: list[str], run: Path, value: Any) -> None:
    relative = _canonical_relative(run, value)
    if relative and relative not in references:
        references.append(relative)


def _manifest_map(manifest: Any) -> dict[tuple[str, str], str]:
    if not isinstance(manifest, list):
        return {}
    result: dict[tuple[str, str], str] = {}
    for item in manifest:
        if not isinstance(item, dict):
            continue
        kind = _clean(item.get("kind"), limit=80)
        path = _clean(item.get("path"), limit=500)
        if path:
            result[(kind, path)] = _clean(item.get("sha256"), limit=128)
    return result


def _manifest_diff(
    previous: dict[str, Any] | None, current: dict[str, Any]
) -> tuple[list[str], list[str], list[str]] | None:
    if not previous or not isinstance(previous.get("input_manifest"), list):
        return None
    before = _manifest_map(previous.get("input_manifest"))
    after = _manifest_map(current.get("input_manifest"))
    added = sorted(key[1] for key in after if key not in before)
    removed = sorted(key[1] for key in before if key not in after)
    changed = sorted(
        key[1] for key in after if key in before and before[key] != after[key]
    )
    return added, removed, changed


def _format_issue(level: str, text: Any) -> str:
    return f"[{level}] {_clean(text)}"


def _generic_validation_items(validation: dict[str, Any]) -> tuple[list[str], int]:
    decisions: list[str] = []
    for item in validation.get("errors", []) if isinstance(validation.get("errors"), list) else []:
        decisions.append(_format_issue("红", item))
    for item in validation.get("warnings", []) if isinstance(validation.get("warnings"), list) else []:
        decisions.append(_format_issue("黄", item))
    return decisions, len(_normal_manifest(validation))


def _g0(run: Path) -> tuple[list[str], list[str], list[str], int]:
    decisions: list[str] = []
    results: list[str] = []
    refs: list[str] = []
    forks = _read_csv(run, "interpretation/INTERPRETATION_FORKS.csv")
    assumptions = _read_csv(run, "problem/ASSUMPTION_REGISTER.csv")
    closed = 0
    for row in forks:
        status = str(row.get("status", "")).strip().lower()
        fork_id = row.get("fork_id") or "未编号分叉"
        if status in {"resolved", "selected", "closed", "accepted-risk", "not-applicable"}:
            closed += 1
            if row.get("selected_option"):
                results.append(f"{fork_id} 已选择：{row.get('selected_option')}")
        elif _truthy(row.get("nondelegable")) or _truthy(row.get("structural_fork")):
            decisions.append(
                _format_issue("黄", f"{fork_id} 尚待队员选择：{row.get('question') or row.get('dimension')}")
            )
    for row in assumptions:
        status = str(row.get("status", "")).strip().lower()
        if status in {
            "validated", "accepted", "approved", "verified", "closed",
            "not-applicable",
        }:
            closed += 1
        elif str(row.get("impact", "")).strip().lower() in {"high", "critical", "高", "关键"}:
            decisions.append(
                _format_issue("黄", f"{row.get('assumption_id') or '关键假设'} 未关闭：{row.get('statement')}")
            )
    for relative in (
        "interpretation/INTERPRETATION_FORKS.csv",
        "problem/ASSUMPTION_REGISTER.csv",
        "data/DATA_AUDIT.md",
    ):
        _add_ref(refs, run, relative)
    results.insert(0, f"题意分叉 {len(forks)} 项；关键假设 {len(assumptions)} 项")
    return decisions, results, refs, closed


def _g1(run: Path) -> tuple[list[str], list[str], list[str], int]:
    registry = _read_json(run, "ideas/IDEA_REGISTRY.json")
    ideas = registry.get("ideas", []) if isinstance(registry.get("ideas"), list) else []
    report = _read_json(run, "research/diversity/DIVERSITY_REPORT.json")
    gaps = report.get("gaps", report.get("uncovered", []))
    if not isinstance(gaps, list):
        gaps = []
    decisions = [_format_diversity_gap(item) for item in gaps]
    results = [f"已登记 {len(ideas)} 条独立思路；多样性报告记录 {len(gaps)} 个缺口"]
    refs: list[str] = []
    for relative in (
        "ideas/IDEA_REGISTRY.json",
        "research/diversity/DIVERSITY_REPORT.json",
        "research/diversity/DIVERSITY_REPORT.md",
        "research/diversity/GAP_REGISTER.csv",
    ):
        _add_ref(refs, run, relative)
    return decisions, results, refs, max(0, len(ideas) - len(gaps))


def _format_diversity_gap(item: Any) -> str:
    """Render one current or legacy diversity gap without leaking dict repr."""
    if isinstance(item, dict):
        gap_id = _clean(item.get("gap_id"), limit=32) or "未编号"
        gap_type = _clean(item.get("gap_type"), limit=48) or "未分类"
        reason = _clean(item.get("reason"), limit=92) or "未说明原因"
        specialist = _clean(item.get("suggested_specialist"), limit=48) or "未指定"
        level = "红" if str(item.get("priority", "")).strip().lower() == "critical" else "黄"
    else:
        gap_id = "未编号"
        gap_type = "未分类"
        reason = _clean(item, limit=92) or "未说明原因"
        specialist = "未指定"
        level = "黄"
    return _format_issue(
        level,
        f"{gap_id} — {gap_type} — {reason} — 建议专家：{specialist}",
    )


def _g2(run: Path) -> tuple[list[str], list[str], list[str], int]:
    focus = _read_json(run, "selection/FOCUS_SET.json")
    selected = focus.get("selected_modules", [])
    if not isinstance(selected, list):
        selected = []
    rejected = focus.get("rejected_or_deferred", [])
    if not isinstance(rejected, list):
        rejected = []
    structural = _read_csv(run, "sensitivity/STRUCTURAL_SENSITIVITY.csv")
    decisions = []
    closed = 0
    for row in structural:
        status = str(row.get("status", "")).strip().lower()
        if status in {"verified", "accepted-risk", "not-applicable"}:
            closed += 1
        else:
            decisions.append(
                _format_issue("黄", f"{row.get('sensitivity_id') or '结构敏感性'} 未关闭：{row.get('target')}")
            )
    results = [
        "保留模块：" + (", ".join(_clean(item, limit=80) for item in selected) or "无"),
        f"淘汰或暂缓 {len(rejected)} 项；结构敏感性 {len(structural)} 项",
    ]
    refs: list[str] = []
    for relative in (
        "selection/FOCUS_SET.json",
        "reviews/FEASIBILITY_REVIEW.md",
        "sensitivity/STRUCTURAL_SENSITIVITY.csv",
    ):
        _add_ref(refs, run, relative)
    return decisions, results, refs, closed


def _g3(run: Path, validation: dict[str, Any]) -> tuple[list[str], list[str], list[str], int]:
    claims = _read_csv(run, "evidence/CLAIM_EVIDENCE_LEDGER.csv")
    experiments = _read_csv(run, "experiments/EXPERIMENT_LOG.csv")
    metrics = _read_csv(run, "experiments/METRICS.csv")
    sensitivities = _read_csv(run, "sensitivity/PARAMETER_SENSITIVITY.csv")
    baselines = _read_csv(run, "experiments/BASELINE_REGISTRY.csv")
    decisions: list[str] = []
    results: list[str] = []
    refs: list[str] = []

    by_claim: dict[str, list[dict[str, str]]] = {}
    for row in claims:
        claim_id = str(row.get("claim_id", "")).strip()
        if claim_id:
            by_claim.setdefault(claim_id, []).append(row)
        _add_ref(refs, run, row.get("evidence_path"))
    core_ids = sorted(
        claim_id
        for claim_id, rows in by_claim.items()
        if any(_truthy(row.get("is_core")) for row in rows)
    )
    verified_core = 0
    for claim_id in core_ids:
        rows = by_claim[claim_id]
        verified = any(
            str(row.get("verification_status", "")).strip().lower() == "verified"
            and str(row.get("evidence_path", "")).strip()
            for row in rows
        )
        if verified:
            verified_core += 1
        else:
            text = next((row.get("claim_text", "") for row in rows if row.get("claim_text")), "")
            decisions.append(_format_issue("红", f"{claim_id} 尚无已核验证据：{text}"))

    passed = 0
    seeds: set[str] = set()
    for row in experiments:
        status = str(row.get("status", "")).strip().lower()
        if status == "passed":
            passed += 1
            if str(row.get("seed", "")).strip():
                seeds.add(str(row.get("seed", "")).strip())
        else:
            decisions.append(
                _format_issue(
                    "黄" if status in {"planned", "running", "pending"} else "红",
                    f"实验 {row.get('experiment_id') or '未编号'} 状态为 {status or '空'}",
                )
            )
        _add_ref(refs, run, row.get("output_path"))

    for row in metrics[:MAX_RESULTS]:
        value = _clean(row.get("value"), limit=60)
        if not value:
            continue
        prefix = " / ".join(
            part
            for part in (
                _clean(row.get("experiment_id"), limit=60),
                _clean(row.get("module_id"), limit=60),
                _clean(row.get("split"), limit=40),
            )
            if part
        )
        metric = _clean(row.get("metric"), limit=80) or "metric"
        unit = _clean(row.get("unit"), limit=40)
        results.append(f"{prefix or '结果'}：{metric} = {value}{(' ' + unit) if unit else ''}")
        _add_ref(refs, run, row.get("evidence_path"))

    verified_sensitivity = 0
    for row in sensitivities:
        status = str(row.get("status", "")).strip().lower()
        sid = row.get("sensitivity_id") or "参数敏感性"
        effect = row.get("observed_effect") or row.get("effect_pct") or "尚无结果"
        if status in {"verified", "accepted-risk", "not-applicable"}:
            verified_sensitivity += 1
            results.append(f"{sid} · {row.get('target') or '参数'}：{effect}")
        else:
            decisions.append(
                _format_issue("黄", f"{sid} 尚未关闭（{status or '状态为空'}）：{row.get('target')}")
            )
        _add_ref(refs, run, row.get("evidence_path"))

    audit = validation.get("experiment_audit")
    receipt_count = audit.get("valid_receipt_count", 0) if isinstance(audit, dict) else 0
    results.insert(
        0,
        f"baseline 登记 {len(baselines)} 项；passed 实验 {passed} 项；"
        f"有效实验收据 {receipt_count} 项；随机种子 {len(seeds)} 个",
    )
    results.insert(1, f"核心主张 {len(core_ids)} 项，其中 {verified_core} 项已有已核验证据")
    for relative in (
        "experiments/BASELINE_REGISTRY.csv",
        "experiments/EXPERIMENT_LOG.csv",
        "experiments/METRICS.csv",
        "sensitivity/PARAMETER_SENSITIVITY.csv",
        "evidence/CLAIM_EVIDENCE_LEDGER.csv",
    ):
        _add_ref(refs, run, relative)
    green = passed + verified_core + verified_sensitivity + int(receipt_count or 0)
    return decisions, results, refs, green


def _g4(run: Path) -> tuple[list[str], list[str], list[str], int]:
    synthesis = _read_json(run, "synthesis/SYNTHESIS_MAP.json")
    modules = synthesis.get("modules", []) if isinstance(synthesis.get("modules"), list) else []
    register = _read_json(run, "disputes/DISPUTE_REGISTER.json")
    disputes = register.get("disputes", []) if isinstance(register.get("disputes"), list) else []
    decisions: list[str] = []
    closed = 0
    for item in disputes:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "open")).lower()
        if status in {"resolved", "closed", "accepted-risk"}:
            closed += 1
        else:
            level = "红" if str(item.get("severity", "")).lower() == "critical" else "黄"
            decisions.append(_format_issue(level, f"争议 {item.get('id') or '未编号'} 未关闭：{item.get('title') or item.get('question')}"))
    results = [f"综合模型包含 {len(modules)} 个模块；争议 {len(disputes)} 项，已关闭 {closed} 项"]
    refs: list[str] = []
    for relative in (
        "synthesis/SYNTHESIS_MAP.json",
        "synthesis/FINAL_MODEL_SPEC.md",
        "disputes/DISPUTE_REGISTER.json",
    ):
        _add_ref(refs, run, relative)
    return decisions, results, refs, closed


def _g5(run: Path) -> tuple[list[str], list[str], list[str], int]:
    consistency = _read_csv(run, "consistency/CONSISTENCY_MATRIX.csv")
    decisions: list[str] = []
    closed = 0
    for row in consistency:
        status = str(row.get("status", "")).strip().upper()
        if status in {"PASS", "NOT_APPLICABLE"}:
            closed += 1
            continue
        if status == "PASS_WITH_TOLERANCE":
            level = "黄"
            detail = f"带容差通过（{row.get('tolerance') or '未记录容差'}）"
        else:
            level = "红" if _truthy(row.get("critical")) or status in {"FAIL", "NOT_TESTED", ""} else "黄"
            detail = status or "状态为空"
        decisions.append(
            _format_issue(
                level,
                f"一致性 {row.get('check_id') or '未编号'}："
                f"{row.get('dimension') or '未注明维度'} · {detail}",
            )
        )
    blind = _read_json(run, "reviews/blind/BLIND_REVIEW_INDEX.json")
    reviews = blind.get("reviews", []) if isinstance(blind.get("reviews"), list) else []
    results = [f"一致性检查 {len(consistency)} 项，无条件通过/不适用 {closed} 项；盲审记录 {len(reviews)} 份"]
    refs: list[str] = []
    for relative in (
        "reviews/RED_TEAM_REPORT.md",
        "reviews/ROBUSTNESS_REPORT.md",
        "reviews/CODE_AUDIT.md",
        "reviews/EVIDENCE_AUDIT.md",
        "reviews/blind/BLIND_REVIEW_INDEX.json",
        "consistency/CONSISTENCY_MATRIX.csv",
        "claims/STRONG_CLAIMS_MATH.csv",
    ):
        _add_ref(refs, run, relative)
    return decisions, results, refs, closed + len(reviews)


def _g6(run: Path) -> tuple[list[str], list[str], list[str], int]:
    claims = _read_csv(run, "paper/PAPER_CLAIM_MAP.csv")
    figures = _read_csv(run, "paper/FIGURE_TABLE_REGISTER.csv")
    audit = _read_json(run, "paper/PAPER_AUDIT.json")
    decisions = []
    blockers = audit.get("blockers", []) if isinstance(audit.get("blockers"), list) else []
    warnings = audit.get("warnings", []) if isinstance(audit.get("warnings"), list) else []
    decisions.extend(_format_issue("红", item) for item in blockers)
    decisions.extend(_format_issue("黄", item) for item in warnings)
    for row in claims:
        if not _truthy(row.get("is_core")):
            continue
        if (
            str(row.get("evidence_status", "")).lower() != "verified"
            or str(row.get("trace_tag_status", "")).lower() != "verified"
        ):
            decisions.append(
                _format_issue(
                    "红",
                    f"核心论文主张 {row.get('claim_id') or '未编号'} 尚未完成证据/trace 核验",
                )
            )
    for row in figures:
        status = str(row.get("status", "")).lower()
        if status not in {"approved", "final", "excluded"}:
            decisions.append(
                _format_issue(
                    "黄",
                    f"图表 {row.get('item_id') or '未编号'} 状态为 {status or '空'}",
                )
            )
    closed_claims = sum(
        str(row.get("evidence_status", "")).lower() in {"verified", "complete", "passed"}
        and str(row.get("trace_tag_status", "")).lower() in {"verified", "complete", "present", "passed"}
        for row in claims
    )
    verified_figures = sum(
        str(row.get("status", "")).lower() in {"approved", "final", "excluded"}
        for row in figures
    )
    results = [
        f"论文主张映射 {len(claims)} 项，证据与 trace 均关闭 {closed_claims} 项",
        f"图表登记 {len(figures)} 项，已核验 {verified_figures} 项；论文审计状态：{audit.get('status') or '未知'}",
    ]
    refs: list[str] = []
    for relative in (
        "paper/FINAL_MANUSCRIPT.md",
        "paper/PAPER_AUDIT.json",
        "paper/PAPER_CLAIM_MAP.csv",
        "paper/FIGURE_TABLE_REGISTER.csv",
        "claims/STRONG_CLAIMS.csv",
    ):
        _add_ref(refs, run, relative)
    return decisions, results, refs, closed_claims + verified_figures


def _g7(run: Path, validation: dict[str, Any]) -> tuple[list[str], list[str], list[str], int]:
    compliance = validation.get("compliance_audit")
    decisions: list[str] = []
    results: list[str] = []
    refs: list[str] = []
    passed = 0
    if isinstance(compliance, dict):
        for name, audit in sorted(compliance.items()):
            if not isinstance(audit, dict):
                continue
            errors = audit.get("errors", []) if isinstance(audit.get("errors"), list) else []
            warnings = audit.get("warnings", []) if isinstance(audit.get("warnings"), list) else []
            if not errors and not warnings:
                passed += 1
            decisions.extend(_format_issue("红", f"{name}: {item}") for item in errors)
            decisions.extend(_format_issue("黄", f"{name}: {item}") for item in warnings)
            for field in ("input_paths", "checked_files"):
                values = audit.get(field, [])
                if isinstance(values, list):
                    for value in values:
                        _add_ref(refs, run, value.get("path") if isinstance(value, dict) else value)
            for field in ("path", "output_path", "archive_path", "receipt_path"):
                _add_ref(refs, run, audit.get(field))
        submission = compliance.get("submission_delivery")
        if isinstance(submission, dict):
            anonymity = submission.get("anonymity", {})
            ai_statement = submission.get("ai_statement", {})
            final_pdf = submission.get("final_pdf", {})
            anonymous = (
                anonymity.get("count") == 0
                if isinstance(anonymity, dict) and isinstance(anonymity.get("count"), int)
                else None
            )
            location = (
                ai_statement.get("before_references")
                if isinstance(ai_statement, dict) and ai_statement.get("required") is True
                else True
                if isinstance(ai_statement, dict) and ai_statement.get("required") is False
                else None
            )
            usage_consistent = (
                ai_statement.get("usage_consistent")
                if isinstance(ai_statement, dict)
                else None
            )
            purpose_attested = (
                ai_statement.get("purpose_consistency_attested")
                if isinstance(ai_statement, dict)
                and ai_statement.get("declared_usage") == "used"
                else True
                if isinstance(ai_statement, dict)
                and ai_statement.get("declared_usage") == "none"
                else None
            )
            declared_usage = (
                ai_statement.get("declared_usage")
                if isinstance(ai_statement, dict)
                else None
            )
            log_usage = (
                ai_statement.get("log_usage")
                if isinstance(ai_statement, dict)
                else None
            )
            size = (
                final_pdf.get("within_limit")
                if isinstance(final_pdf, dict)
                else None
            )
            size_detail = ""
            if isinstance(final_pdf, dict) and isinstance(final_pdf.get("size"), int):
                size_detail = (
                    f"（{final_pdf['size']:,}/{final_pdf.get('max_bytes', 0):,} bytes）"
                )
            results.append(
                f"匿名性机器扫描：{_status_word(anonymous)}；"
                f"AI 声明位置：{_status_word(location)}；"
                f"AI 使用状态一致：{_status_word(usage_consistent)}"
                f"（声明={declared_usage or '未报告'}，日志={log_usage or '未报告'}）；"
                f"用途人工一致性复核：{_status_word(purpose_attested)}；"
                f"论文 20 MB 限制：{_status_word(size)}{size_detail}"
            )
        support = compliance.get("official_support_archive")
        if isinstance(support, dict):
            support_size = support.get("archive_size")
            support_detail = (
                f"（{support_size:,}/{support.get('max_bytes', 0):,} bytes）"
                if isinstance(support_size, int)
                else ""
            )
            results.append(
                "官方支撑 ZIP："
                f"{_status_word(support.get('passed'))}；"
                f"20 MB 限制：{_status_word(support.get('within_limit'))}"
                f"{support_detail}；成员 {len(support.get('members', [])) if isinstance(support.get('members'), list) else 0} 个"
            )
    for relative in (
        "deliverables/FINAL_CHECKLIST.md",
        "deliverables/FINAL_MANUSCRIPT.pdf",
        "deliverables/AI工具使用详情.pdf",
        "deliverables/official/支撑材料.zip",
        "deliverables/internal/SUPPORT_PACKAGE_RECEIPT.json",
        "paper/FINAL_MANUSCRIPT.md",
        "ai/AI_TOOL_USAGE_DETAILS.md",
    ):
        _add_ref(refs, run, relative)
    if not results:
        results.append("提交合规结果以 Gate 校验器为准；当前未提供结构化 submission_policy 摘要。")
    return decisions, results, refs, passed


def _status_word(value: Any) -> str:
    if value is True:
        return "通过"
    if value is False:
        return "未通过"
    return "未报告"


def _domain_summary(
    run: Path, validation: dict[str, Any]
) -> tuple[list[str], list[str], list[str], int]:
    gate = _gate(validation)
    if gate == "G0":
        return _g0(run)
    if gate == "G1":
        return _g1(run)
    if gate == "G2":
        return _g2(run)
    if gate == "G3":
        return _g3(run, validation)
    if gate == "G4":
        return _g4(run)
    if gate == "G5":
        return _g5(run)
    if gate == "G6":
        return _g6(run)
    return _g7(run, validation)


def _unique(items: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = _clean(item)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _bullets(items: list[str], *, empty: str) -> list[str]:
    return [f"- {item}" for item in items] if items else [f"- {empty}"]


def _bounded(items: list[str], limit: int) -> list[str]:
    if len(items) <= limit:
        return items
    return [*items[:limit], f"其余 {len(items) - limit} 项已折叠；请查看原始校验输出。"]


def render_gate_review(
    run: Path,
    validation: dict[str, Any],
    *,
    previous_metadata: dict[str, Any] | None = None,
) -> str:
    """Render review Markdown without modifying the filesystem."""
    if not isinstance(validation, dict):
        raise TypeError("validation 必须是 dict")
    root = run.resolve()
    gate = _gate(validation)
    metadata = _metadata(
        root,
        validation,
        previous_metadata=previous_metadata,
    )
    machine_decisions, input_count = _generic_validation_items(validation)
    domain_decisions, results, references, green_count = _domain_summary(root, validation)
    all_decisions = _unique([*machine_decisions, *domain_decisions])
    decision_count = len(all_decisions)
    decisions = _bounded(all_decisions, MAX_DECISIONS)
    results = _bounded(_unique(results), MAX_RESULTS)

    for item in metadata["input_manifest"]:
        _add_ref(references, root, item.get("path"))
    references = _unique(references)
    reference_lines: list[str] = []
    for relative in references[:MAX_REFERENCES]:
        link = _md_link(root, relative)
        if link:
            reference_lines.append(link)
    if len(references) > MAX_REFERENCES:
        reference_lines.append(f"其余 {len(references) - MAX_REFERENCES} 个底层文件已折叠。")

    diff = _manifest_diff(previous_metadata, metadata)
    if diff is None:
        diff_lines = ["首次生成审核摘要；无上一版输入快照。"]
    else:
        added, removed, changed = diff
        if not added and not removed and not changed:
            diff_lines = ["底层输入未变化。"]
        else:
            diff_lines = [f"新增 {len(added)} 项，删除 {len(removed)} 项，变更 {len(changed)} 项。"]
            for label, paths in (("新增", added), ("变更", changed), ("删除", removed)):
                for path in paths[:4]:
                    diff_lines.append(f"{label}：`{_clean(path, limit=160)}`")
            total = len(added) + len(removed) + len(changed)
            shown = sum(min(4, len(items)) for items in (added, changed, removed))
            if shown < total:
                diff_lines.append(f"其余 {total - shown} 项已折叠。")

    ok = validation.get("ok") is True
    automatic_lines = [
        f"已绑定底层输入 {input_count} 项。",
        f"结构化检查通过项合计 {green_count} 项。",
    ]
    if ok:
        automatic_lines.append("Gate 严格校验器当前报告 0 个阻断错误。")

    lines = [
        f"# {gate} 人工审核摘要",
        "",
        f"状态：{'可批准' if ok else '暂不可批准'}",
        "",
        f"## 需要队员判断的 {decision_count} 项",
        "",
        *_bullets(decisions, empty="无；当前没有展开的红色或黄色项目。"),
        "",
        "## 已自动检查通过",
        "",
        *_bullets(automatic_lines, empty="暂无自动检查计数。"),
        "",
        "## 关键结果",
        "",
        *_bullets(results, empty="暂无可聚合的结构化关键结果。"),
        "",
        "## 与上次审核相比",
        "",
        *_bullets(diff_lines, empty="没有可比较的底层输入。"),
        "",
        "## 原始证据",
        "",
        *_bullets(reference_lines, empty="当前 Gate 没有可链接的 run 内现存证据文件。"),
        "",
        _METADATA_START
        + json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + _METADATA_END,
        "",
    ]
    return "\n".join(lines)


def gate_review_binding(run: Path, validation: dict[str, Any]) -> dict[str, str]:
    gate = _gate(validation)
    path = review_path(run, gate)
    if not path.is_file():
        raise FileNotFoundError(path)
    return {
        "path": path.relative_to(run.resolve()).as_posix(),
        "sha256": sha256_file(path),
        "underlying_input_sha256": _underlying_input_sha256(validation),
    }


def write_gate_review(run: Path, validation: dict[str, Any]) -> dict[str, str]:
    """Write or retain the canonical review, then return its approval binding."""
    root = run.resolve()
    gate = _gate(validation)
    path = review_path(root, gate)
    previous = parse_gate_review_metadata(path) if path.is_file() else None
    current = _metadata_core(root, validation)
    if _metadata_matches_current(previous, current):
        # The comparison baseline is embedded so the generated body can be
        # reconstructed exactly.  This preserves byte stability while
        # repairing any manual/tampered change outside the metadata block.
        assert previous is not None
        rendered = render_gate_review(
            root,
            validation,
            previous_metadata=_comparison_metadata(previous),
        )
        try:
            existing = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            existing = ""
        if existing != rendered:
            atomic_write_text(path, rendered)
    else:
        atomic_write_text(
            path,
            render_gate_review(root, validation, previous_metadata=previous),
        )
    return gate_review_binding(root, validation)


def verify_gate_review(
    run: Path,
    validation: dict[str, Any],
    binding: dict[str, Any] | None = None,
) -> list[str]:
    """Return freshness, integrity and canonical-path errors for one review."""
    errors: list[str] = []
    root = run.resolve()
    try:
        gate = _gate(validation)
        expected_path = review_path(root, gate)
    except (TypeError, ValueError) as exc:
        return [str(exc)]
    relative = expected_path.relative_to(root).as_posix()
    if binding is not None:
        if not isinstance(binding, dict):
            errors.append("human review binding 不是对象")
        else:
            if binding.get("path") != relative:
                errors.append(f"human review binding.path 必须是 canonical 路径 {relative}")
            if binding.get("underlying_input_sha256") != _underlying_input_sha256(validation):
                errors.append("human review binding 的底层 input_sha256 已过期")
    if not expected_path.is_file():
        errors.append(f"缺少 Gate 人工审核摘要：{relative}")
        return errors
    metadata = parse_gate_review_metadata(expected_path)
    expected_metadata = _metadata_core(root, validation)
    if metadata is None:
        errors.append("Gate 人工审核摘要缺少有效的末尾 metadata")
    elif not _metadata_matches_current(metadata, expected_metadata):
        errors.append("Gate 人工审核摘要的底层输入 metadata 已过期或被篡改")
    digest = sha256_file(expected_path)
    if isinstance(binding, dict) and binding.get("sha256") != digest:
        errors.append("Gate 人工审核摘要内容与审批绑定哈希不一致")

    try:
        text = expected_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        errors.append(f"Gate 人工审核摘要无法读取：{exc}")
        return errors
    if metadata is not None and _metadata_matches_current(metadata, expected_metadata):
        canonical = render_gate_review(
            root,
            validation,
            previous_metadata=_comparison_metadata(metadata),
        )
        if text != canonical:
            errors.append("Gate 人工审核摘要的生成主体已被修改")
    required_sections = (
        f"# {gate} 人工审核摘要",
        "## 需要队员判断",
        "## 已自动检查通过",
        "## 关键结果",
        "## 与上次审核相比",
        "## 原始证据",
    )
    for section in required_sections:
        if section not in text:
            errors.append(f"Gate 人工审核摘要缺少固定段落：{section}")
    for href in re.findall(r"\]\(([^)]+)\)", text):
        if not href.startswith("../../"):
            errors.append(f"Gate 人工审核摘要含非 run-local 链接：{href}")
            continue
        decoded = unquote(href[len("../../") :])
        if _canonical_relative(root, decoded) is None:
            errors.append(f"Gate 人工审核摘要含非 canonical 或已失效链接：{href}")
    return errors


__all__ = [
    "SCHEMA_VERSION",
    "VALID_GATES",
    "gate_review_binding",
    "parse_gate_review",
    "parse_gate_review_metadata",
    "render_gate_review",
    "review_path",
    "verify_gate_review",
    "write_gate_review",
]
