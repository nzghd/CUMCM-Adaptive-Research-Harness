#!/usr/bin/env python3
"""Separate structural sensitivity from ordinary parameter sensitivity."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from common import (
    die,
    is_named_human_identity,
    is_timezone_aware_iso,
    next_numeric_id,
    now_iso,
    read_csv,
    require_csv_header,
    resolve_run,
    resolve_run_evidence_ref,
    sha256_file,
    write_csv,
)

FIELDS=["sensitivity_id","kind","target","alternatives_or_range","metric","experiment_id","evidence_path","observed_effect","effect_pct","status","verified_by","verified_at","waiver_reason","notes"]
VALID_STATUS={"planned","running","verified","failed","accepted-risk","not-applicable"}
REVIEWED_WAIVER_STATUS={"accepted-risk","not-applicable"}


def _path(run:Path,kind:str, *, create:bool=True)->Path:
    name="STRUCTURAL_SENSITIVITY.csv" if kind=="structural" else "PARAMETER_SENSITIVITY.csv"
    path=run/"sensitivity"/name
    if not path.exists():
        if create: write_csv(path,FIELDS,[])
        return path
    require_csv_header(path,FIELDS)
    return path


def _require_closure_fields(row:dict[str,str], *, label:str)->None:
    status=row.get("status","")
    if status=="verified":
        if not str(row.get("evidence_path","")).strip():
            die(f"{label} 标记 verified 时必须提供 evidence_path")
        if not str(row.get("observed_effect","")).strip():
            die(f"{label} 标记 verified 时必须提供 observed_effect")
        if not is_named_human_identity(row.get("verified_by")):
            die(f"{label} 标记 verified 时必须提供具名 verified_by")
        if not is_timezone_aware_iso(row.get("verified_at")):
            die(f"{label} 标记 verified 时 verified_at 必须是含时区 ISO 8601")
    if status in REVIEWED_WAIVER_STATUS:
        if not is_named_human_identity(row.get("verified_by")):
            die(f"{label} 标记 {status} 时必须提供具名 reviewer (--by)")
        if not is_timezone_aware_iso(row.get("verified_at")):
            die(f"{label} 标记 {status} 时 verified_at 必须是含时区 ISO 8601")
        if not str(row.get("waiver_reason","")).strip():
            die(f"{label} 标记 {status} 时必须提供显式 --waiver-reason")


def _require_verified_evidence(run:Path,row:dict[str,str], *, label:str)->None:
    if row.get("status")!="verified": return
    _, evidence_errors=resolve_run_evidence_ref(run,str(row.get("evidence_path", "")))
    if evidence_errors:
        die(f"{label} verified evidence 无法解析："+"; ".join(evidence_errors))


def _experiment_receipt_state(
    run: Path,
    experiment_ids: set[str],
) -> tuple[
    set[str],
    list[str],
    list[dict[str, str]],
    list[dict[str, Any]],
]:
    """Reuse the base G3 receipt verifier for experiment-linked sensitivity."""
    from harness import _experiment_gate_audit

    audit = _experiment_gate_audit(run, only_experiment_ids=experiment_ids)
    valid_ids = {str(item) for item in audit.get("valid_experiment_ids", [])}
    resolved: list[dict[str, str]] = []
    for relative in [*audit.get("receipt_paths", []), *audit.get("artifact_paths", [])]:
        path = (run / str(relative)).resolve()
        try:
            relative_path = path.relative_to(run.resolve()).as_posix()
        except ValueError:
            continue
        if not path.is_file() or path.stat().st_size <= 0:
            continue
        resolved.append({"reference": "experiment", "path": relative_path, "sha256": sha256_file(path)})
    resolved = sorted(
        {item["path"]: item for item in resolved}.values(), key=lambda item:item["path"]
    )
    input_records = [
        dict(item) for item in audit.get("input_records", []) if isinstance(item, dict)
    ]
    return (
        valid_ids,
        [str(item) for item in audit.get("errors", [])],
        resolved,
        input_records,
    )


def _require_verified_experiment(run: Path, row: dict[str, str], *, label: str) -> None:
    experiment_id = str(row.get("experiment_id", "")).strip()
    if row.get("status") != "verified" or not experiment_id:
        return
    valid_ids, receipt_errors, _, _ = _experiment_receipt_state(run, {experiment_id})
    if receipt_errors:
        die(f"{label} experiment receipt 未通过 G3 校验：" + "; ".join(receipt_errors))
    if experiment_id not in valid_ids:
        die(f"{label} experiment_id 没有当前有效的 passed receipt：{experiment_id}")


def cmd_add(args:argparse.Namespace)->int:
    run=resolve_run(args.run); path=_path(run,args.kind); rows=read_csv(path,expected_fields=FIELDS)
    prefix="SS" if args.kind=="structural" else "PS"
    sid=args.id or next_numeric_id(prefix,[r.get("sensitivity_id","") for r in rows])
    if any(r.get("sensitivity_id")==sid for r in rows): die(f"Sensitivity 已存在：{sid}")
    reviewed=args.status=="verified" or args.status in REVIEWED_WAIVER_STATUS
    row={"sensitivity_id":sid,"kind":args.kind,"target":args.target,"alternatives_or_range":args.alternatives,"metric":args.metric,"experiment_id":args.experiment or "","evidence_path":args.evidence or "","observed_effect":args.effect or "","effect_pct":"" if args.effect_pct is None else args.effect_pct,"status":args.status,"verified_by":args.by if reviewed else "","verified_at":now_iso() if reviewed and args.by else "","waiver_reason":getattr(args,"waiver_reason",None) or "","notes":args.note or ""}
    _require_closure_fields(row,label=sid)
    _require_verified_evidence(run,row,label=sid)
    _require_verified_experiment(run,row,label=sid)
    rows.append(row)
    write_csv(path,FIELDS,rows); print(sid); return 0


def cmd_set(args:argparse.Namespace)->int:
    run=resolve_run(args.run); path=_path(run,args.kind); rows=read_csv(path,expected_fields=FIELDS)
    row=next((r for r in rows if r.get("sensitivity_id")==args.id),None)
    if row is None: die(f"找不到 sensitivity：{args.id}")
    if args.status not in VALID_STATUS: die(f"无效状态：{args.status}")
    if args.status=="verified" and (not args.evidence or not args.by):
        die("Sensitivity 标记 verified 时必须显式提供 --evidence 和 --by")
    if args.status in REVIEWED_WAIVER_STATUS and (
        not args.by or not (getattr(args,"waiver_reason",None) or "").strip()
    ):
        die(f"Sensitivity 标记 {args.status} 时必须显式提供 --by 和 --waiver-reason")
    row["status"]=args.status
    if args.experiment is not None: row["experiment_id"]=args.experiment
    if args.evidence is not None: row["evidence_path"]=args.evidence
    if args.effect is not None: row["observed_effect"]=args.effect
    if args.effect_pct is not None: row["effect_pct"]=str(args.effect_pct)
    if args.by: row["verified_by"]=args.by; row["verified_at"]=now_iso()
    waiver_reason=getattr(args,"waiver_reason",None)
    if waiver_reason is not None: row["waiver_reason"]=waiver_reason
    if args.note: row["notes"]=args.note
    _require_closure_fields(row,label=args.id)
    _require_verified_evidence(run,row,label=args.id)
    _require_verified_experiment(run,row,label=args.id)
    write_csv(path,FIELDS,rows); print(f"{args.id} -> {args.status}"); return 0


def validate_sensitivity(
    run:Path,
    *,
    strict:bool=False,
    kinds:tuple[str, ...]=("parameter", "structural"),
)->dict[str,Any]:
    errors=[]; warnings=[]; counts={}; resolved_evidence=[]; input_records=[]
    requested=tuple(dict.fromkeys(kinds))
    invalid_kinds=sorted(set(requested)-{"parameter","structural"})
    if not requested or invalid_kinds:
        return {
            "ok":False,
            "errors":[f"sensitivity kinds 无效：{invalid_kinds or list(requested)}"],
            "warnings":[],
            "counts":{},
            "resolved_evidence":[],
            "input_records":[],
        }
    rows_by_kind: dict[str, list[dict[str, str]]] = {}
    for kind in requested:
        path = _path(run, kind, create=False)
        if not path.is_file():
            errors.append(f"缺少 sensitivity ledger：{path.relative_to(run).as_posix()}")
            rows_by_kind[kind] = []
        else:
            rows_by_kind[kind] = read_csv(path, expected_fields=FIELDS)
        counts[kind] = len(rows_by_kind[kind])
    experiment_ids = {
        str(row.get("experiment_id", "")).strip()
        for rows in rows_by_kind.values()
        for row in rows
        if str(row.get("experiment_id", "")).strip()
    }
    experiment_state: tuple[
        set[str], list[str], list[dict[str, str]], list[dict[str, Any]]
    ] | None = None
    if experiment_ids:
        experiment_state = _experiment_receipt_state(run, experiment_ids)
        resolved_evidence.extend(experiment_state[2])
        input_records.extend(experiment_state[3])
    experiment_errors_reported = False
    for kind in requested:
        rows=rows_by_kind[kind]
        seen_ids:set[str]=set()
        for row in rows:
            sid=row.get("sensitivity_id","unknown"); status=row.get("status","planned")
            if not sid or sid=="unknown": errors.append(f"{kind} sensitivity 缺少 sensitivity_id")
            elif sid in seen_ids: errors.append(f"重复 sensitivity_id：{sid}")
            else: seen_ids.add(sid)
            if row.get("kind")!=kind: errors.append(f"{sid} kind={row.get('kind')!r} 与文件类型 {kind!r} 不一致")
            if status not in VALID_STATUS: errors.append(f"{sid} status 无效：{status!r}")
            for field in ("target","alternatives_or_range","metric"):
                if not str(row.get(field,"")).strip(): errors.append(f"{sid} 缺少 {field}")
            if status=="verified":
                if not row.get("metric"): errors.append(f"{sid} verified 但缺少 metric")
                if not row.get("evidence_path"): errors.append(f"{sid} verified 但缺少 evidence_path")
                if not str(row.get("observed_effect", "")).strip(): errors.append(f"{sid} verified 但缺少 observed_effect")
                if not is_named_human_identity(row.get("verified_by")): errors.append(f"{sid} verified 但缺少具名 verified_by")
                if not is_timezone_aware_iso(row.get("verified_at")): errors.append(f"{sid} verified_at 必须是含时区 ISO 8601")
            effect_pct = str(row.get("effect_pct", "")).strip()
            if effect_pct:
                try:
                    effect_pct_is_finite = math.isfinite(float(effect_pct))
                except (TypeError, ValueError, OverflowError):
                    effect_pct_is_finite = False
                if not effect_pct_is_finite:
                    errors.append(f"{sid} effect_pct 必须是有限数值")
            if status in REVIEWED_WAIVER_STATUS:
                if not is_named_human_identity(row.get("verified_by")): errors.append(f"{sid} {status} 但缺少具名 reviewer")
                if not is_timezone_aware_iso(row.get("verified_at")): errors.append(f"{sid} {status} 的 verified_at 必须是含时区 ISO 8601")
                if not row.get("waiver_reason"): errors.append(f"{sid} {status} 但缺少 waiver_reason")
            evidence = str(row.get("evidence_path", "")).strip()
            if evidence:
                resolved, evidence_errors = resolve_run_evidence_ref(run, evidence)
                resolved_evidence.extend(resolved)
                target = errors if status == "verified" or strict else warnings
                target.extend(f"{sid} evidence_path 无法解析：{item}" for item in evidence_errors)
            experiment_id = str(row.get("experiment_id", "")).strip()
            if experiment_id:
                assert experiment_state is not None
                valid_experiment_ids, receipt_errors, _, _ = experiment_state
                if receipt_errors and not experiment_errors_reported:
                    errors.extend(f"experiment receipt: {item}" for item in receipt_errors)
                    experiment_errors_reported = True
                if experiment_id not in valid_experiment_ids:
                    errors.append(f"{sid} experiment_id 没有当前有效的 passed receipt：{experiment_id}")
            if kind=="structural" and status not in {"verified","accepted-risk","not-applicable"}:
                (errors if strict else warnings).append(f"结构敏感性未关闭：{sid}")
            if kind=="structural" and "parameter" in row.get("notes","").lower() and status=="verified":
                errors.append(f"{sid} 使用参数敏感性说明冒充结构敏感性")
    if strict and "structural" in requested and counts.get("structural",0)==0:
        errors.append("没有登记结构敏感性；若确实不适用，应登记 not-applicable 项并说明")
    resolved_evidence=sorted(
        {
            (
                item["path"],
                item.get("evidence_id", ""),
                item.get("reference", ""),
            ): item
            for item in resolved_evidence
        }.values(),
        key=lambda item:(
            item["path"], item.get("evidence_id", ""), item.get("reference", "")
        ),
    )
    return {
        "ok":not errors,
        "errors":errors,
        "warnings":warnings,
        "counts":counts,
        "resolved_evidence":resolved_evidence,
        "input_records":input_records,
    }


def cmd_lint(args:argparse.Namespace)->int:
    run=resolve_run(args.run); report=validate_sensitivity(run,strict=args.strict)
    print(json.dumps(report,ensure_ascii=False,indent=2)); return 0 if report["ok"] else 3


def cmd_status(args:argparse.Namespace)->int:
    run=resolve_run(args.run)
    for kind in ("structural","parameter"):
        rows=read_csv(_path(run,kind),expected_fields=FIELDS); print(f"{kind}: {len(rows)}")
        for r in rows: print(f"- {r.get('sensitivity_id')}: {r.get('target')} / {r.get('status')}")
    return 0


def ensure(run: Path) -> None:
    """Create both sensitivity ledgers if absent."""
    _path(run, "parameter")
    _path(run, "structural")


def audit(
    run: Path,
    *,
    strict: bool,
    kinds: tuple[str, ...] = ("parameter", "structural"),
) -> tuple[dict[str, Any], int]:
    result = validate_sensitivity(run, strict=strict, kinds=kinds)
    result = {**result, "passed": result.get("ok", False), "generated_at": now_iso()}
    return result, (0 if result["passed"] else 3)


def build_parser()->argparse.ArgumentParser:
    p=argparse.ArgumentParser(description=__doc__); sub=p.add_subparsers(dest="command",required=True)
    x=sub.add_parser("add"); x.add_argument("--run"); x.add_argument("--kind",choices=["parameter","structural"],required=True); x.add_argument("--id"); x.add_argument("--target",required=True); x.add_argument("--alternatives",required=True); x.add_argument("--metric",required=True); x.add_argument("--experiment"); x.add_argument("--evidence"); x.add_argument("--effect"); x.add_argument("--effect-pct",type=float); x.add_argument("--status",choices=sorted(VALID_STATUS),default="planned"); x.add_argument("--by",default=""); x.add_argument("--waiver-reason"); x.add_argument("--note"); x.set_defaults(func=cmd_add)
    x=sub.add_parser("set"); x.add_argument("--run"); x.add_argument("--kind",choices=["parameter","structural"],required=True); x.add_argument("--id",required=True); x.add_argument("--status",choices=sorted(VALID_STATUS),required=True); x.add_argument("--experiment"); x.add_argument("--evidence"); x.add_argument("--effect"); x.add_argument("--effect-pct",type=float); x.add_argument("--by",default=""); x.add_argument("--waiver-reason"); x.add_argument("--note"); x.set_defaults(func=cmd_set)
    x=sub.add_parser("lint"); x.add_argument("--run"); x.add_argument("--strict",action="store_true"); x.set_defaults(func=cmd_lint)
    x=sub.add_parser("status"); x.add_argument("--run"); x.set_defaults(func=cmd_status)
    return p

def main()->int:
    args=build_parser().parse_args(); return int(args.func(args))
if __name__=="__main__": raise SystemExit(main())
