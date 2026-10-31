#!/usr/bin/env python3
"""Fine-grained consistency matrix for model, code, results, figures and paper."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import (
    ROOT,
    die,
    is_named_human_identity,
    is_timezone_aware_iso,
    now_iso,
    read_csv,
    read_json,
    resolve_run_evidence_ref,
    write_csv,
    write_json,
)
from v51_common import resolve_run_flexible

FIELDS = [
    "check_id", "dimension", "scope", "expected", "observed", "status",
    "tolerance", "critical", "evidence_ref", "command", "reviewed_by",
    "reviewed_at", "waiver_reason", "notes",
]

WAIVER_REASON_PLACEHOLDERS = {
    "n/a", "na", "none", "not applicable", "tbd", "todo", "unknown",
    "unspecified",
}
DIRECT_EVIDENCE_ROOTS = {"evidence", "figures", "results"}
MANAGEMENT_EVIDENCE_PATHS = {
    "BUDGET.json",
    "FROZEN_MANIFEST.json",
    "INPUT_MANIFEST.json",
    "RUN_META.json",
    "STATUS.json",
    "V51_STATUS.json",
    "evidence/CLAIM_EVIDENCE_LEDGER.csv",
    "experiments/BASELINE_REGISTRY.csv",
    "experiments/EXPERIMENT_LOG.csv",
    "experiments/EXPERIMENT_PLAN.md",
    "experiments/METRICS.csv",
    "consistency/CONSISTENCY_MATRIX.csv",
    "consistency/CONSISTENCY_REPORT.md",
    "consistency/CONSISTENCY_SUMMARY.json",
}
MANAGEMENT_EVIDENCE_PREFIXES = (
    "consistency/", "decisions/", "deliverables/", "paper/", "planning/",
    "reviews/", "selection/",
)


def _dimension_policy() -> tuple[list[str], list[str], set[str], dict[str, bool]]:
    payload = read_json(ROOT / "config" / "consistency_dimensions.json", expect=dict)
    dimensions = payload.get("dimensions", [])
    if not isinstance(dimensions, list):
        die("config/consistency_dimensions.json dimensions 必须是数组")
    configured: list[str] = []
    required: list[str] = []
    allow_not_applicable: dict[str, bool] = {}
    for index, item in enumerate(dimensions, start=1):
        if not isinstance(item, dict):
            die(f"consistency dimension #{index} 必须是对象")
        name = str(item.get("name", "")).strip()
        if not name:
            die(f"consistency dimension #{index} 缺少 name")
        if not isinstance(item.get("required"), bool):
            die(f"consistency dimension {name} required 必须是布尔值")
        if not isinstance(item.get("allow_not_applicable"), bool):
            die(
                f"consistency dimension {name} allow_not_applicable 必须是布尔值；"
                "缺失时不得静默允许豁免"
            )
        configured.append(name)
        allow_not_applicable[name] = item["allow_not_applicable"]
        if item["required"]:
            required.append(name)
    if not configured:
        die("config/consistency_dimensions.json 未定义 dimensions")
    if len(configured) != len(set(configured)):
        die("config/consistency_dimensions.json 含重复 dimension name")
    raw_statuses = payload.get("allowed_status", [])
    if not isinstance(raw_statuses, list) or any(not isinstance(item, str) or not item.strip() for item in raw_statuses):
        die("config/consistency_dimensions.json allowed_status 必须是非空字符串数组")
    statuses = {item.strip() for item in raw_statuses}
    if not statuses:
        die("config/consistency_dimensions.json 未定义 allowed_status")
    if len(statuses) != len(raw_statuses):
        die("config/consistency_dimensions.json 含重复 allowed_status")
    return configured, required, statuses, allow_not_applicable


def _split_refs(value: str) -> list[str]:
    normalized = str(value or "").replace("|", ";").replace(",", ";")
    return [item.strip() for item in normalized.split(";") if item.strip()]


def _run_file(run: Path, value: str) -> Path | None:
    token = str(value or "").strip().split("#", 1)[0]
    if not token or token.startswith(("http://", "https://")):
        return None
    raw = Path(token).expanduser()
    candidates = [raw] if raw.is_absolute() else [run / raw, ROOT / raw]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(run.resolve())
        except (OSError, ValueError):
            continue
        if resolved.is_file() and resolved.stat().st_size > 0:
            return resolved
    return None


def _is_management_evidence_path(relative: str) -> bool:
    return relative in MANAGEMENT_EVIDENCE_PATHS or relative.startswith(
        MANAGEMENT_EVIDENCE_PREFIXES
    )


def _evidence_universe(run: Path) -> tuple[set[str], set[str]]:
    """Return reviewed symbolic refs and runner-authenticated artifact paths."""
    refs: set[str] = set()
    runner_paths: set[str] = set()
    evidence_ids = {
        str(row.get("evidence_id", "")).strip()
        for row in read_csv(run / "evidence" / "CLAIM_EVIDENCE_LEDGER.csv")
        if str(row.get("evidence_id", "")).strip()
    }
    for evidence_id in evidence_ids:
        resolved, evidence_errors = resolve_run_evidence_ref(run, evidence_id)
        if (
            resolved
            and not evidence_errors
            and all(
                not _is_management_evidence_path(str(item.get("path", "")))
                for item in resolved
            )
        ):
            refs.add(evidence_id)
    source_rows = read_csv(run / "sources" / "SOURCE_LEDGER.csv")
    source_counts: dict[str, int] = {}
    for row in source_rows:
        source_id = str(row.get("source_id", "")).strip()
        if source_id:
            source_counts[source_id] = source_counts.get(source_id, 0) + 1
    for row in source_rows:
        source_id = str(row.get("source_id", "")).strip()
        url = str(row.get("url", "")).strip()
        if (
            source_id
            and source_counts.get(source_id) == 1
            and url.startswith(("http://", "https://"))
            and str(row.get("verified_by", "")).strip()
            and is_timezone_aware_iso(str(row.get("accessed_at", "")))
        ):
            refs.update((source_id, url))
    from harness import _experiment_gate_audit

    experiment_audit = _experiment_gate_audit(run)
    if not experiment_audit.get("errors"):
        for relative in experiment_audit.get("receipt_paths", []):
            result_path = (run / str(relative)).resolve()
            try:
                result = json.loads(result_path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            experiment_id = str(result.get("experiment_id", "")).strip() if isinstance(result, dict) else ""
            if experiment_id:
                refs.add(experiment_id)
                runner_paths.add(str(relative).replace("\\", "/"))
        for relative in experiment_audit.get("artifact_paths", []):
            normalized = str(relative).replace("\\", "/")
            parts = Path(normalized).parts
            if (
                len(parts) >= 4
                and parts[:2] == ("experiments", "runs")
                and not (
                    "_attempts" in parts
                    and Path(normalized).name in {
                        "request.json", "stderr.txt", "stdout.txt",
                    }
                )
            ):
                runner_paths.add(normalized)
    return refs, runner_paths


def _evidence_ref_error(
    run: Path,
    value: str,
    reviewed_refs: set[str],
    runner_paths: set[str],
) -> str | None:
    ref = str(value or "").strip()
    if not ref:
        return "reference is empty"
    if ref in reviewed_refs:
        return None
    if ref.startswith(("http://", "https://")):
        return "external URL is not a verified SOURCE_LEDGER reference"
    path = _run_file(run, ref)
    if path is None:
        return "reference does not resolve to a non-empty run-local file"
    relative = path.relative_to(run.resolve()).as_posix()
    if _is_management_evidence_path(relative):
        return f"management/self-referential artifact is not evidence: {relative}"
    if relative in runner_paths:
        return None
    if Path(relative).parts and Path(relative).parts[0] in DIRECT_EVIDENCE_ROOTS:
        return None
    return (
        "direct evidence must be a reviewed ledger/source reference, a validated "
        "runner receipt/artifact, or a file below evidence/, figures/, or results/: "
        + relative
    )


def _valid_waiver_reason(value: str) -> bool:
    reason = str(value or "").strip()
    normalized = reason.casefold()
    placeholder_markers = ("[draft_required]", "tbd", "todo", "unknown", "unspecified")
    return (
        len(reason) >= 10
        and normalized not in WAIVER_REASON_PLACEHOLDERS
        and not any(marker in normalized for marker in placeholder_markers)
    )


def _paths(run: Path, *, create: bool = True) -> dict[str, Path]:
    root = run / "consistency"
    matrix = root / "CONSISTENCY_MATRIX.csv"
    if create:
        root.mkdir(parents=True, exist_ok=True)
        if not matrix.exists():
            write_csv(matrix, FIELDS, [])
    return {"root": root, "matrix": matrix, "summary": root / "CONSISTENCY_SUMMARY.json", "report": root / "CONSISTENCY_REPORT.md"}


def cmd_init(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    p = _paths(run)
    print(f"Initialized {p['matrix'].relative_to(run)}")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    p = _paths(run)
    rows = read_csv(p["matrix"], expected_fields=FIELDS)
    row = next((r for r in rows if r.get("check_id") == args.check_id), None)
    configured_dims, _, _, allow_not_applicable = _dimension_policy()
    if args.dimension not in configured_dims:
        die(f"未配置的 consistency dimension：{args.dimension!r}")
    waiver_reason = str(getattr(args, "waiver_reason", None) or "").strip()
    if not is_named_human_identity(args.reviewed_by):
        die(
            "consistency check 必须提供具名 --reviewed-by，"
            "不能使用 TEAM/UNKNOWN/TODO 等占位值"
        )
    if args.status == "NOT_APPLICABLE":
        if not allow_not_applicable.get(args.dimension, False):
            die(
                f"dimension {args.dimension!r} 未显式允许 NOT_APPLICABLE；"
                "请执行该检查或修改经审查的全局 dimension policy"
            )
        if not is_named_human_identity(args.reviewed_by):
            die("NOT_APPLICABLE 必须提供具名 --reviewed-by，不能使用 TEAM/UNKNOWN 等占位值")
        if not _valid_waiver_reason(waiver_reason):
            die("NOT_APPLICABLE 必须通过 --waiver-reason 提供至少 10 个字符的具体理由")
    elif waiver_reason:
        die("--waiver-reason 仅可用于 status=NOT_APPLICABLE")
    payload = {
        "check_id": args.check_id, "dimension": args.dimension, "scope": args.scope,
        "expected": args.expected, "observed": args.observed, "status": args.status,
        "tolerance": args.tolerance or "", "critical": str(bool(args.critical)).lower(),
        "evidence_ref": args.evidence_ref or "", "command": args.command or "",
        "reviewed_by": args.reviewed_by or "", "reviewed_at": now_iso() if args.reviewed_by else "",
        "waiver_reason": waiver_reason,
        "notes": args.notes or "",
    }
    if args.status == "PASS_WITH_TOLERANCE" and not args.tolerance:
        die("PASS_WITH_TOLERANCE 必须提供 --tolerance。")
    if args.status in {"PASS", "PASS_WITH_TOLERANCE", "FAIL"} and not args.evidence_ref:
        die(f"{args.status} 必须提供 --evidence-ref。")
    if row is None:
        rows.append(payload)
    else:
        row.update(payload)
    write_csv(p["matrix"], FIELDS, rows)
    print(f"Recorded consistency check {args.check_id}: {args.status}")
    return 0


def derive(run: Path, *, strict: bool, persist: bool = True) -> tuple[dict, int]:
    p = _paths(run, create=False)
    rows = read_csv(p["matrix"], expected_fields=FIELDS) if p["matrix"].is_file() else []
    errors: list[str] = []
    warnings: list[str] = []
    if not p["matrix"].is_file():
        errors.append("Missing consistency/CONSISTENCY_MATRIX.csv; run consistency_matrix.py init first")
    (
        configured_dims,
        required_dims,
        allowed_statuses,
        allow_not_applicable,
    ) = _dimension_policy()
    reviewed_refs, runner_paths = _evidence_universe(run)
    critical = [r for r in rows if str(r.get("critical", "")).lower() == "true"]
    failed = [r for r in rows if r.get("status") == "FAIL"]
    critical_untested = [r for r in critical if r.get("status") == "NOT_TESTED"]
    invalid_tol = [r for r in rows if r.get("status") == "PASS_WITH_TOLERANCE" and not str(r.get("tolerance", "")).strip()]
    missing_evidence = [
        r for r in rows
        if r.get("status") in {"PASS", "PASS_WITH_TOLERANCE", "FAIL"}
        and not _split_refs(r.get("evidence_ref", ""))
    ]
    seen_ids: set[str] = set()
    duplicate_ids: set[str] = set()
    seen_scopes: set[tuple[str, str]] = set()
    duplicate_scopes: set[tuple[str, str]] = set()
    for index, row in enumerate(rows, start=1):
        check_id = str(row.get("check_id", "")).strip()
        status = str(row.get("status", "")).strip()
        dimension = str(row.get("dimension", "")).strip()
        scope = str(row.get("scope", "")).strip()
        if not check_id:
            errors.append(f"Consistency row {index} lacks check_id")
        elif check_id in seen_ids:
            duplicate_ids.add(check_id)
        else:
            seen_ids.add(check_id)
        if dimension and scope:
            scope_key = (dimension, scope)
            if scope_key in seen_scopes:
                duplicate_scopes.add(scope_key)
            seen_scopes.add(scope_key)
        if status not in allowed_statuses:
            errors.append(f"{check_id or f'row-{index}'}: invalid status {status!r}")
        if dimension and dimension not in configured_dims:
            (errors if strict else warnings).append(
                f"{check_id or f'row-{index}'}: unconfigured dimension {dimension!r}"
            )
        critical_value = str(row.get("critical", "")).strip().lower()
        if critical_value not in {"true", "false"}:
            errors.append(f"{check_id or f'row-{index}'}: critical must be true or false")
        waiver_reason = str(row.get("waiver_reason", "")).strip()
        if status == "NOT_APPLICABLE":
            if not allow_not_applicable.get(dimension, False):
                errors.append(
                    f"{check_id or f'row-{index}'}: dimension {dimension!r} does not "
                    "explicitly allow NOT_APPLICABLE"
                )
            if not is_named_human_identity(row.get("reviewed_by", "")):
                errors.append(
                    f"{check_id or f'row-{index}'}: NOT_APPLICABLE requires a named "
                    "reviewer, not TEAM/UNKNOWN or another placeholder"
                )
            if not is_timezone_aware_iso(str(row.get("reviewed_at", ""))):
                errors.append(
                    f"{check_id or f'row-{index}'}: NOT_APPLICABLE reviewed_at must be "
                    "timezone-aware ISO 8601"
                )
            if not _valid_waiver_reason(waiver_reason):
                errors.append(
                    f"{check_id or f'row-{index}'}: NOT_APPLICABLE requires a specific "
                    "waiver_reason of at least 10 characters"
                )
        elif waiver_reason:
            errors.append(
                f"{check_id or f'row-{index}'}: waiver_reason is only valid for NOT_APPLICABLE"
            )
        if strict:
            for field in ("dimension", "scope", "expected", "observed"):
                if not str(row.get(field, "")).strip():
                    errors.append(f"{check_id or f'row-{index}'}: missing {field}")
            reviewed_by = str(row.get("reviewed_by", "")).strip()
            reviewed_at = str(row.get("reviewed_at", "")).strip()
            if not is_named_human_identity(reviewed_by):
                errors.append(
                    f"{check_id or f'row-{index}'}: reviewed_by must name a human, "
                    "not TEAM/UNKNOWN/TODO or another placeholder"
                )
            if not is_timezone_aware_iso(reviewed_at):
                errors.append(f"{check_id or f'row-{index}'}: reviewed_at must be timezone-aware ISO 8601")
        evidence_refs = _split_refs(row.get("evidence_ref", ""))
        if status in {"PASS", "PASS_WITH_TOLERANCE", "FAIL"} and evidence_refs:
            invalid_refs = [
                (item, _evidence_ref_error(run, item, reviewed_refs, runner_paths))
                for item in evidence_refs
            ]
            invalid_refs = [(item, reason) for item, reason in invalid_refs if reason]
            if invalid_refs:
                errors.append(
                    f"{check_id or f'row-{index}'}: unresolved or disallowed evidence_ref: "
                    + "; ".join(f"{item} ({reason})" for item, reason in invalid_refs)
                )
    if duplicate_ids:
        errors.append("Duplicate check_id values: " + ", ".join(sorted(duplicate_ids)))
    if duplicate_scopes:
        errors.append(
            "Duplicate dimension/scope checks: "
            + ", ".join(f"{dimension}/{scope}" for dimension, scope in sorted(duplicate_scopes))
        )
    if failed:
        errors.append("Failed checks: " + ", ".join(r.get("check_id", "") for r in failed))
    if strict and critical_untested:
        errors.append("Critical checks not tested: " + ", ".join(r.get("check_id", "") for r in critical_untested))
    if invalid_tol:
        errors.append("Tolerance status without tolerance: " + ", ".join(r.get("check_id", "") for r in invalid_tol))
    if missing_evidence:
        errors.append("Checks without evidence: " + ", ".join(r.get("check_id", "") for r in missing_evidence))
    present_dims = {str(r.get("dimension", "")).strip() for r in rows if r.get("dimension")}
    missing_required = [dimension for dimension in required_dims if dimension not in present_dims]
    unresolved_required = [
        dimension for dimension in required_dims
        if any(r.get("dimension") == dimension and r.get("status") == "NOT_TESTED" for r in rows)
    ]
    if strict and missing_required:
        errors.append("Required dimensions not represented: " + ", ".join(missing_required))
    if strict and unresolved_required:
        errors.append("Required dimensions not tested: " + ", ".join(unresolved_required))
    missing_dims = [d for d in configured_dims if d not in present_dims]
    if missing_dims:
        warnings.append("Configured dimensions not represented: " + ", ".join(missing_dims))
    optional_untested = [
        str(r.get("check_id", "")) for r in rows
        if r.get("status") == "NOT_TESTED" and r.get("dimension") not in required_dims
    ]
    if optional_untested:
        warnings.append("Optional checks not tested: " + ", ".join(optional_untested))
    if not rows and strict:
        errors.append("No consistency checks recorded")
    if errors:
        overall = "FAIL"
    elif any(r.get("status") == "PASS_WITH_TOLERANCE" for r in rows):
        overall = "PASS_WITH_TOLERANCE"
    else:
        overall = "PASS"
    result = {
        "generated_at": now_iso(), "strict": strict, "overall": overall,
        "check_count": len(rows), "critical_count": len(critical),
        "required_dimensions": required_dims,
        "missing_required_dimensions": missing_required,
        "not_applicable_waivers": [
            {
                "check_id": str(row.get("check_id", "")),
                "dimension": str(row.get("dimension", "")),
                "waiver_reason": str(row.get("waiver_reason", "")),
                "reviewed_by": str(row.get("reviewed_by", "")),
                "reviewed_at": str(row.get("reviewed_at", "")),
            }
            for row in rows
            if row.get("status") == "NOT_APPLICABLE"
        ],
        "status_counts": {status: sum(1 for r in rows if r.get("status") == status) for status in sorted(allowed_statuses)},
        "errors": errors, "warnings": warnings, "passed": not errors,
        "note": "Overall status is derived from dimensions; it must not erase individual tolerances or untested checks.",
    }
    if persist:
        p["root"].mkdir(parents=True, exist_ok=True)
        write_json(p["summary"], result)
        lines = ["# Consistency Report", "", f"Generated: {result['generated_at']}", "", f"Overall: **{overall}**", "", "| ID | Dimension | Scope | Status | Tolerance | Evidence | Waiver reason |", "|---|---|---|---|---|---|---|"]
        for r in rows:
            lines.append(f"| {r.get('check_id','')} | {r.get('dimension','')} | {r.get('scope','')} | {r.get('status','')} | {r.get('tolerance','')} | {r.get('evidence_ref','')} | {r.get('waiver_reason','')} |")
        lines += ["", "## Errors", ""] + ([f"- {x}" for x in errors] if errors else ["- None."])
        lines += ["", "## Warnings", ""] + ([f"- {x}" for x in warnings] if warnings else ["- None."])
        p["report"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result, (0 if not errors else 3)


def cmd_derive(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    result, code = derive(run, strict=args.strict)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


def cmd_list(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    p = _paths(run)
    for r in read_csv(p["matrix"], expected_fields=FIELDS):
        print(f"{r.get('check_id')}\t{r.get('dimension')}\t{r.get('status')}\t{r.get('tolerance')}\t{r.get('scope')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init"); p.add_argument("--run"); p.set_defaults(func=cmd_init)
    p = sub.add_parser("set")
    p.add_argument("--run"); p.add_argument("--check-id", required=True); p.add_argument("--dimension", required=True)
    p.add_argument("--scope", required=True); p.add_argument("--expected", required=True); p.add_argument("--observed", required=True)
    p.add_argument("--status", choices=sorted(_dimension_policy()[2]), required=True); p.add_argument("--tolerance"); p.add_argument("--critical", action="store_true")
    p.add_argument("--evidence-ref"); p.add_argument("--command"); p.add_argument("--reviewed-by"); p.add_argument("--waiver-reason"); p.add_argument("--notes")
    p.set_defaults(func=cmd_set)
    p = sub.add_parser("derive"); p.add_argument("--run"); p.add_argument("--strict", action="store_true"); p.set_defaults(func=cmd_derive)
    p = sub.add_parser("lint"); p.add_argument("--run"); p.add_argument("--strict", action="store_true"); p.set_defaults(func=cmd_derive)
    p = sub.add_parser("list"); p.add_argument("--run"); p.set_defaults(func=cmd_list)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
