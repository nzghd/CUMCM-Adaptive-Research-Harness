#!/usr/bin/env python3
"""Manage semantic forks, assumption fingerprints and nondelegable decisions."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import (
    append_jsonl,
    contains_placeholder,
    die,
    is_named_human_identity,
    is_timezone_aware_iso,
    next_numeric_id,
    now_iso,
    read_csv,
    read_json,
    require_csv_header,
    resolve_run,
    resolve_run_evidence_ref,
    write_csv,
    write_json,
)
from v51_common import split_values

FORK_FIELDS = [
    "fork_id", "dimension", "question", "options_json", "impact",
    "uncertainty", "irreversibility", "changes_objective",
    "changes_feasible_set", "expected_result_change_pct", "structural_fork",
    "nondelegable", "risk_score", "status", "selected_option", "decision_by",
    "decision_at", "evidence", "reason", "notes",
]
PROBE_FIELDS = [
    "probe_id", "fork_id", "option_id", "probe_type", "command_or_method",
    "artifact_path", "metric", "result", "status", "verified_by",
    "verified_at", "notes",
]
FORBIDDEN_DEFAULT = {"AUTO", "DEFAULT", "AGENT", "SYSTEM", "ORCHESTRATOR", "CODEX", "GPT", "CLAUDE", "DEEPSEEK"}


def _policy() -> dict[str, Any]:
    from common import ROOT
    return read_json(ROOT / "config" / "interpretation_policy.json", expect=dict)


def _paths(run: Path) -> tuple[Path, Path, Path]:
    base = run / "interpretation"
    return base / "INTERPRETATION_FORKS.csv", base / "FORK_PROBES.csv", base / "ASSUMPTION_FINGERPRINTS.json"


def _bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是"}


def _score(impact: str, uncertainty: str, irreversibility: str) -> int:
    policy = _policy()
    return (
        int(policy.get("impact_weights", {}).get(impact, 2))
        * int(policy.get("uncertainty_weights", {}).get(uncertainty, 2))
        * int(policy.get("irreversibility_weights", {}).get(irreversibility, 2))
    )


def _is_nondelegable(
    *, impact: str, uncertainty: str, irreversibility: str, dimension: str,
    changes_objective: bool, changes_feasible_set: bool,
    expected_result_change_pct: float | None,
) -> tuple[bool, int, list[str]]:
    policy = _policy()
    score = _score(impact, uncertainty, irreversibility)
    reasons: list[str] = []
    if impact == "critical": reasons.append("critical impact")
    if changes_objective: reasons.append("changes objective")
    if changes_feasible_set: reasons.append("changes feasible set")
    if dimension in {"target_geometry", "time_semantics", "aggregation_rule", "uncertainty_semantics"}:
        reasons.append(f"structural dimension={dimension}")
    if expected_result_change_pct is not None and expected_result_change_pct >= 10:
        reasons.append("expected result change >= 10%")
    if score >= int(policy.get("nondelegable_score_threshold", 18)):
        reasons.append(f"risk score {score} exceeds threshold")
    return bool(reasons), score, reasons


def _ensure(run: Path) -> None:
    base = run / "interpretation"
    base.mkdir(parents=True, exist_ok=True)
    fork_path, probe_path, fingerprint_path = _paths(run)
    if not fork_path.exists(): write_csv(fork_path, FORK_FIELDS, [])
    if not probe_path.exists(): write_csv(probe_path, PROBE_FIELDS, [])
    require_csv_header(fork_path, FORK_FIELDS)
    require_csv_header(probe_path, PROBE_FIELDS)
    if not fingerprint_path.exists():
        write_json(fingerprint_path, {"schema_version":1,"dimensions":[],"candidates":[],"updated_at":now_iso()})
    for name, text in {
        "PROBLEM_SEMANTICS.md":"# Problem Semantics\n\n[DRAFT_REQUIRED]\n",
        "ASSUMPTION_DIVERSITY.md":"# Assumption Diversity\n\n[DRAFT_REQUIRED]\n",
        "INTERPRETATION_AUDIT.md":"# Interpretation Audit\n\n[DRAFT_REQUIRED]\n",
        "DECISION_LOG.jsonl":"",
    }.items():
        path=base/name
        if not path.exists(): path.write_text(text, encoding="utf-8")
    div=base/"ASSUMPTION_DIVERSITY.json"
    if not div.exists():
        write_json(div,{"schema_version":2,"candidate_count":0,"dimension_count":0,"all_unique_signatures":0,"interpretation_unique_signatures":0,"model_unique_signatures":0,"algorithm_unique_signatures":0,"algorithm_diversity_present":False,"shared_dimensions":{},"variable_dimensions":{},"shared_structural_dimensions":{},"layer_diversity":{},"warnings":[],"generated_at":now_iso()})
    _read_fingerprints(fingerprint_path)


def _read_fingerprints(path: Path) -> dict[str, Any]:
    payload = read_json(path, expect=dict)
    unknown_top = set(payload) - {"schema_version", "dimensions", "candidates", "updated_at"}
    if unknown_top:
        die(f"ASSUMPTION_FINGERPRINTS.json 含契约外字段：{sorted(unknown_top)}")
    if payload.get("schema_version") != 1:
        die(f"ASSUMPTION_FINGERPRINTS.json schema_version 必须为 1：{path}")
    candidates = payload.get("candidates")
    dimensions = payload.get("dimensions")
    if not isinstance(candidates, list) or not isinstance(dimensions, list):
        die(
            f"ASSUMPTION_FINGERPRINTS.json 使用旧契约：{path}。"
            "请先运行 `python tools/migrate_v51.py --run <RUN_ID> --apply`。"
        )
    if any(not isinstance(item, str) or not item for item in dimensions):
        die(f"ASSUMPTION_FINGERPRINTS.json dimensions 必须是非空字符串数组：{path}")
    candidate_ids:set[str]=set()
    for index, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, dict):
            die(f"ASSUMPTION_FINGERPRINTS.json candidate #{index} 必须是对象：{path}")
        unknown_candidate=set(candidate)-{"id","values","source","stage","updated_at"}
        if unknown_candidate:
            die(f"ASSUMPTION_FINGERPRINTS.json candidate #{index} 含契约外字段：{sorted(unknown_candidate)}")
        candidate_id=str(candidate.get("id", "")).strip()
        if not candidate_id:
            die(f"ASSUMPTION_FINGERPRINTS.json candidate #{index} 缺少 id：{path}")
        if candidate_id in candidate_ids:
            die(f"ASSUMPTION_FINGERPRINTS.json candidate id 重复：{candidate_id}")
        candidate_ids.add(candidate_id)
        values = candidate.get("values")
        if not isinstance(values, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in values.items()
        ):
            die(f"ASSUMPTION_FINGERPRINTS.json candidate #{index} values 必须是字符串映射：{path}")
        if not isinstance(candidate.get("source"),str) or not isinstance(candidate.get("updated_at"),str):
            die(f"ASSUMPTION_FINGERPRINTS.json candidate #{index} 缺少 source/updated_at 字符串")
    return payload


def cmd_init(args: argparse.Namespace) -> int:
    run=resolve_run(args.run)
    _ensure(run)
    print(run/"interpretation")
    return 0


def _parse_options(values: list[str]) -> list[dict[str,str]]:
    options=[]
    for raw in values:
        if "=" in raw:
            oid,label=raw.split("=",1)
        elif ":" in raw:
            oid,label=raw.split(":",1)
        else:
            oid,label=raw,raw
        oid=oid.strip(); label=label.strip()
        if not oid or not label: die(f"无效 option：{raw}")
        options.append({"id":oid,"label":label})
    if len(options)<2: die("解释分叉至少需要两个 option")
    ids=[x["id"] for x in options]
    if len(set(ids))!=len(ids): die("option id 重复")
    return options


def cmd_fork_add(args: argparse.Namespace) -> int:
    run=resolve_run(args.run); _ensure(run)
    fork_path,_,_=_paths(run); rows=read_csv(fork_path, expected_fields=FORK_FIELDS)
    fid=args.id or next_numeric_id("IF",[r.get("fork_id","") for r in rows])
    if any(r.get("fork_id")==fid for r in rows): die(f"fork 已存在：{fid}")
    options=_parse_options(args.option)
    expected=float(args.expected_result_change_pct) if args.expected_result_change_pct is not None else None
    nondelegable,score,reasons=_is_nondelegable(
        impact=args.impact, uncertainty=args.uncertainty, irreversibility=args.irreversibility,
        dimension=args.dimension, changes_objective=args.changes_objective,
        changes_feasible_set=args.changes_feasible_set,
        expected_result_change_pct=expected,
    )
    row={
        "fork_id":fid,"dimension":args.dimension,"question":args.question,
        "options_json":json.dumps(options,ensure_ascii=False),"impact":args.impact,
        "uncertainty":args.uncertainty,"irreversibility":args.irreversibility,
        "changes_objective":"true" if args.changes_objective else "false",
        "changes_feasible_set":"true" if args.changes_feasible_set else "false",
        "expected_result_change_pct":"" if expected is None else expected,
        "structural_fork":"true" if args.structural else "false",
        "nondelegable":"true" if nondelegable else "false","risk_score":score,
        "status":"open","selected_option":"","decision_by":"","decision_at":"",
        "evidence":"","reason":"","notes":"; ".join(reasons+[args.note] if args.note else reasons),
    }
    rows.append(row); write_csv(fork_path,FORK_FIELDS,rows)
    print(fid); print(f"nondelegable={nondelegable} risk_score={score}")
    return 0


def _fork(run: Path, fork_id: str) -> tuple[Path,list[dict[str,str]],dict[str,str]]:
    fork_path,_,_=_paths(run); rows=read_csv(fork_path, expected_fields=FORK_FIELDS)
    row=next((r for r in rows if r.get("fork_id")==fork_id),None)
    if row is None: die(f"找不到 fork：{fork_id}")
    return fork_path,rows,row


def cmd_probe_add(args: argparse.Namespace) -> int:
    run=resolve_run(args.run); _ensure(run)
    _,_,fork=_fork(run,args.fork_id)
    options={x["id"] for x in json.loads(fork["options_json"])}
    if args.option_id not in options: die(f"未知 option：{args.option_id}")
    _,probe_path,_=_paths(run); rows=read_csv(probe_path, expected_fields=PROBE_FIELDS)
    pid=args.id or next_numeric_id("PROBE",[r.get("probe_id","") for r in rows])
    if args.status=="verified" and not is_named_human_identity(args.by):
        die("verified probe 必须提供具名真人 --by")
    if args.status=="verified" and not (args.result or args.artifact):
        die("verified probe 必须提供 --result 或 --artifact")
    if args.status=="verified" and args.artifact:
        _, artifact_errors=resolve_run_evidence_ref(run,args.artifact)
        if artifact_errors:
            die(f"{pid} verified artifact 无法解析："+"; ".join(artifact_errors))
    rows.append({
        "probe_id":pid,"fork_id":args.fork_id,"option_id":args.option_id,
        "probe_type":args.probe_type,"command_or_method":args.method,
        "artifact_path":args.artifact or "","metric":args.metric or "",
        "result":args.result or "","status":args.status,
        "verified_by":args.by if args.status=="verified" else "",
        "verified_at":now_iso() if args.status=="verified" else "","notes":args.note or "",
    })
    write_csv(probe_path,PROBE_FIELDS,rows); print(pid); return 0


def cmd_fingerprint_set(args: argparse.Namespace) -> int:
    run=resolve_run(args.run); _ensure(run)
    _,_,path=_paths(run); payload=_read_fingerprints(path)
    values={}
    for raw in args.set:
        if "=" not in raw: die(f"fingerprint 必须为 key=value：{raw}")
        k,v=raw.split("=",1); values[k.strip()]=v.strip()
    candidates=[c for c in payload.get("candidates",[]) if c.get("id")!=args.candidate]
    candidates.append({"id":args.candidate,"values":values,"source":args.source or "","updated_at":now_iso()})
    dims=sorted({k for c in candidates for k in c.get("values",{})})
    payload.update({"dimensions":dims,"candidates":candidates,"updated_at":now_iso()})
    write_json(path,payload); print(args.candidate); return 0


def _layer_report(candidates: list[dict[str, Any]], dimensions: list[str]) -> dict[str, Any]:
    if not dimensions:
        return {
            "dimensions": [], "unique_signatures": 0,
            "shared_dimensions": {}, "variable_dimensions": {},
        }
    signatures = [
        tuple((d, str(c.get("values", {}).get(d, "<missing>"))) for d in dimensions)
        for c in candidates
    ]
    shared: dict[str, str] = {}
    variable: dict[str, list[str]] = {}
    for dimension in dimensions:
        values = {str(c.get("values", {}).get(dimension, "<missing>")) for c in candidates}
        if len(values) == 1:
            shared[dimension] = next(iter(values))
        else:
            variable[dimension] = sorted(values)
    return {
        "dimensions": dimensions,
        "unique_signatures": len(set(signatures)) if candidates else 0,
        "shared_dimensions": shared,
        "variable_dimensions": variable,
    }


def _diversity_markdown(result: dict[str, Any]) -> str:
    layers = result.get("layer_diversity", {})
    interpretation = layers.get("interpretation", {})
    model_structure = layers.get("model_structure", {})
    algorithm = layers.get("algorithm", {})
    shared_structural = result.get("shared_structural_dimensions", {})
    variable_dimensions = result.get("variable_dimensions", {})
    warnings = result.get("warnings", [])
    lines = [
        "# Assumption Diversity", "",
        f"Candidates: {result.get('candidate_count', 0)}", "",
        "## Diversity by layer", "",
        f"- Interpretation signatures: {interpretation.get('unique_signatures', 0)}",
        f"- Model-structure signatures: {model_structure.get('unique_signatures', 0)}",
        f"- Algorithm signatures: {algorithm.get('unique_signatures', 0)}",
        f"- Full signatures: {result.get('all_unique_signatures', 0)}", "",
        "## Shared structural dimensions",
    ]
    lines += [f"- `{key}` = `{value}`" for key, value in shared_structural.items()] or ["- None"]
    lines += ["", "## Variable dimensions"]
    lines += [
        f"- `{key}`: {', '.join(values)}"
        for key, values in variable_dimensions.items()
    ] or ["- None"]
    lines += ["", "## Warnings"]
    lines += [f"- {warning}" for warning in warnings] or ["- None"]
    return "\n".join(lines) + "\n"


def analyze_assumption_diversity(run: Path, *, persist: bool = True) -> dict[str, Any]:
    """Separate interpretation, model-structure and algorithm diversity.

    Earlier versions counted an optimizer change as a new "interpretation"
    signature.  That could hide a shared structural error: GA and MILP are
    algorithmically different while still using the same target definition.
    This report therefore exposes all three layers independently.
    """
    _, _, path = _paths(run)
    payload = _read_fingerprints(path)
    candidates = payload.get("candidates", [])
    dimensions = payload.get("dimensions", [])

    policy = _policy()
    configured_layers = policy.get("dimension_layers", {})
    layer_membership: dict[str, set[str]] = {
        name: set(values) for name, values in configured_layers.items()
        if isinstance(values, list)
    }
    assigned = set().union(*layer_membership.values()) if layer_membership else set()
    layer_dimensions = {
        name: sorted(set(dimensions) & members)
        for name, members in layer_membership.items()
    }
    layer_dimensions["other"] = sorted(set(dimensions) - assigned)
    layer_reports = {
        name: _layer_report(candidates, dims)
        for name, dims in layer_dimensions.items()
    }

    all_report = _layer_report(candidates, sorted(dimensions))
    interpretation = layer_reports.get("interpretation", _layer_report(candidates, []))
    model_structure = layer_reports.get("model_structure", _layer_report(candidates, []))
    algorithm = layer_reports.get("algorithm", _layer_report(candidates, []))

    warnings: list[str] = []
    structural_dimensions = set(layer_dimensions.get("interpretation", []))
    shared_structural = {
        key: value for key, value in all_report["shared_dimensions"].items()
        if key in structural_dimensions
    }
    if len(candidates) >= 2:
        for dimension, value in sorted(shared_structural.items()):
            warnings.append(
                f"所有候选共享结构维度 {dimension}={value}；方法或算法多样性不能替代该世界观分支"
            )
        if algorithm["unique_signatures"] > 1 and interpretation["unique_signatures"] <= 1:
            warnings.append(
                "检测到算法异质性，但解释层只有一个签名；请增加题意/目标/聚合语义分支，而不是继续更换优化器"
            )
        if algorithm["unique_signatures"] > 1 and model_structure["unique_signatures"] <= 1:
            warnings.append(
                "检测到算法异质性，但模型结构层没有扩展；不同求解器可能仍在求解同一个错误模型"
            )

    result = {
        "schema_version": 2,
        "candidate_count": len(candidates),
        "dimension_count": len(dimensions),
        "all_unique_signatures": all_report["unique_signatures"],
        "interpretation_unique_signatures": interpretation["unique_signatures"],
        "model_unique_signatures": model_structure["unique_signatures"],
        "algorithm_unique_signatures": algorithm["unique_signatures"],
        "algorithm_diversity_present": algorithm["unique_signatures"] > 1,
        "shared_dimensions": all_report["shared_dimensions"],
        "variable_dimensions": all_report["variable_dimensions"],
        "shared_structural_dimensions": shared_structural,
        "layer_diversity": layer_reports,
        "warnings": warnings,
        "generated_at": now_iso(),
    }
    if persist:
        write_json(run / "interpretation" / "ASSUMPTION_DIVERSITY.json", result)
        (run / "interpretation" / "ASSUMPTION_DIVERSITY.md").write_text(
            _diversity_markdown(result), encoding="utf-8"
        )
    return result


def cmd_analyze(args: argparse.Namespace) -> int:
    run=resolve_run(args.run); _ensure(run); result=analyze_assumption_diversity(run)
    print(json.dumps(result,ensure_ascii=False,indent=2)); return 0


def cmd_decide(args: argparse.Namespace) -> int:
    run=resolve_run(args.run); _ensure(run)
    fork_path,rows,row=_fork(run,args.fork_id)
    options={x["id"] for x in json.loads(row["options_json"])}
    if args.select not in options: die(f"未知 option：{args.select}")
    policy=_policy(); forbidden={x.upper() for x in policy.get("forbidden_deciders",[])} | FORBIDDEN_DEFAULT
    if not is_named_human_identity(args.by):
        die("关闭 interpretation fork 必须提供具名真人 --by")
    nondelegable=_bool(row.get("nondelegable"))
    if nondelegable:
        if args.by.strip().upper() in forbidden: die(f"{args.by} 不得关闭 nondelegable fork")
        if not args.human_confirm: die("nondelegable fork 必须显式提供 --human-confirm")
    # Structural forks require option coverage by structural probes unless risk is explicitly accepted.
    _,probe_path,_=_paths(run); probes=read_csv(probe_path, expected_fields=PROBE_FIELDS)
    if _bool(row.get("structural_fork")) and not args.accept_unprobed_risk:
        allowed=set(policy.get("structural_probe_types",[]))
        covered={p.get("option_id") for p in probes if p.get("fork_id")==args.fork_id and p.get("status")=="verified" and p.get("probe_type") in allowed}
        if len(covered)<2:
            die(f"结构分叉至少需要两个 option 的 verified structural probes；当前覆盖 {sorted(covered)}。或用 --accept-unprobed-risk 明确接受风险。")
    evidence_refs=[]
    for raw in args.evidence or []:
        evidence_refs.extend(split_values(raw))
    if not args.accept_unprobed_risk and not evidence_refs:
        die("decided fork 必须提供至少一个 --evidence")
    for evidence_ref in evidence_refs:
        _, evidence_errors=resolve_run_evidence_ref(run,evidence_ref)
        if evidence_errors:
            die(f"fork evidence 无法解析：{evidence_ref}: "+"; ".join(evidence_errors))
    row.update({
        "status":"decided" if not args.accept_unprobed_risk else "accepted-risk",
        "selected_option":args.select,"decision_by":args.by,"decision_at":now_iso(),
        "evidence":";".join(evidence_refs),"reason":args.reason,
    })
    write_csv(fork_path,FORK_FIELDS,rows)
    append_jsonl(run/"interpretation"/"DECISION_LOG.jsonl",{
        "timestamp":now_iso(),"fork_id":args.fork_id,"selected_option":args.select,
        "by":args.by,"human_confirm":bool(args.human_confirm),"nondelegable":nondelegable,
        "accepted_unprobed_risk":bool(args.accept_unprobed_risk),"evidence":evidence_refs,"reason":args.reason,
    })
    print(f"{args.fork_id} -> {args.select}"); return 0


def validate_interpretation(run: Path, *, strict: bool=False, require_fingerprints: bool=False) -> dict[str,Any]:
    errors=[]; warnings=[]; resolved_evidence=[]
    semantics=run/"interpretation"/"PROBLEM_SEMANTICS.md"
    if not semantics.is_file() or semantics.stat().st_size == 0:
        errors.append("缺少或为空：interpretation/PROBLEM_SEMANTICS.md")
    if contains_placeholder(semantics,["[DRAFT_REQUIRED]","TODO"]):
        (errors if strict else warnings).append("PROBLEM_SEMANTICS.md 仍含占位符")
    fork_path,probe_path,fingerprint_path=_paths(run)
    for required_path in (fork_path, probe_path, fingerprint_path):
        if not required_path.is_file():
            errors.append(f"缺少 interpretation artifact：{required_path.relative_to(run).as_posix()}")
    forks=read_csv(fork_path, expected_fields=FORK_FIELDS) if fork_path.is_file() else []
    probes=read_csv(probe_path, expected_fields=PROBE_FIELDS) if probe_path.is_file() else []
    if not forks: (errors if strict else warnings).append("尚未登记任何解释分叉；应明确说明题面确无结构性歧义，不能仅留空")
    policy=_policy(); allowed=set(policy.get("structural_probe_types",[]))
    fork_options:dict[str,set[str]]={}
    seen_forks:set[str]=set()
    for row in forks:
        fid=str(row.get("fork_id","")).strip() or "unknown"
        if fid in seen_forks: errors.append(f"解释分叉 ID 重复：{fid}")
        seen_forks.add(fid)
        for field in ("dimension","question","options_json","impact","uncertainty","irreversibility","risk_score","status"):
            if not str(row.get(field,"")).strip(): errors.append(f"{fid} 缺少 {field}")
        if row.get("impact") not in {"low","medium","high","critical"}: errors.append(f"{fid} impact 无效：{row.get('impact')!r}")
        if row.get("uncertainty") not in {"low","medium","high","unknown"}: errors.append(f"{fid} uncertainty 无效：{row.get('uncertainty')!r}")
        if row.get("irreversibility") not in {"low","medium","high"}: errors.append(f"{fid} irreversibility 无效：{row.get('irreversibility')!r}")
        for field in ("changes_objective","changes_feasible_set","structural_fork","nondelegable"):
            if row.get(field) not in {"true","false"}: errors.append(f"{fid} {field} 必须是 true/false")
        try:
            if int(str(row.get("risk_score","")))<0: raise ValueError
        except ValueError:
            errors.append(f"{fid} risk_score 必须是非负整数")
        expected_change=str(row.get("expected_result_change_pct","")).strip()
        if expected_change:
            try: float(expected_change)
            except ValueError: errors.append(f"{fid} expected_result_change_pct 不是数值")
        if row.get("status") not in {"open","decided","accepted-risk"}: errors.append(f"{fid} status 无效：{row.get('status')!r}")
        try:
            options=json.loads(str(row.get("options_json","")))
        except json.JSONDecodeError:
            options=[]; errors.append(f"{fid} options_json 无效")
        option_ids=[]
        if not isinstance(options,list) or len(options)<2:
            errors.append(f"{fid} options_json 至少需要两个 option")
        else:
            for option in options:
                if not isinstance(option,dict) or not str(option.get("id","")).strip() or not str(option.get("label","")).strip():
                    errors.append(f"{fid} option 必须包含非空 id/label")
                    continue
                option_ids.append(str(option["id"]))
            if len(option_ids)!=len(set(option_ids)): errors.append(f"{fid} option id 重复")
        fork_options[fid]=set(option_ids)
        if row.get("status") in {"decided","accepted-risk"}:
            if row.get("selected_option") not in fork_options[fid]: errors.append(f"{fid} 已决但 selected_option 无效")
            if not str(row.get("reason","")).strip(): errors.append(f"{fid} 已决但缺少 reason")
            if not is_named_human_identity(row.get("decision_by")):
                errors.append(f"{fid} 已决但 decision_by 不是具名真人")
            if not is_timezone_aware_iso(row.get("decision_at")):
                errors.append(f"{fid} decision_at 必须是含时区 ISO 8601")
            evidence_refs=split_values(row.get("evidence", ""))
            if row.get("status")=="decided" and not evidence_refs:
                errors.append(f"{fid} decided 但缺少 evidence")
            for evidence_ref in evidence_refs:
                resolved, evidence_errors=resolve_run_evidence_ref(run,evidence_ref)
                resolved_evidence.extend(resolved)
                errors.extend(
                    f"{fid} evidence 无法解析：{evidence_ref}: {item}"
                    for item in evidence_errors
                )
    for probe in probes:
        pid = probe.get("probe_id", "unknown")
        for field in ("probe_id","fork_id","option_id","probe_type","command_or_method","status"):
            if not str(probe.get(field,"")).strip(): errors.append(f"{pid} 缺少 {field}")
        if probe.get("status") not in {"planned","running","verified","failed"}: errors.append(f"{pid} status 无效：{probe.get('status')!r}")
        fork_id=str(probe.get("fork_id","")).strip()
        if fork_id not in fork_options: errors.append(f"{pid} 引用未知 fork：{fork_id}")
        elif probe.get("option_id") not in fork_options[fork_id]: errors.append(f"{pid} 引用未知 option：{probe.get('option_id')}")
        if probe.get("status") != "verified":
            continue
        if not is_named_human_identity(probe.get("verified_by")):
            errors.append(f"verified probe 缺少具名 verified_by：{pid}")
        if not is_timezone_aware_iso(probe.get("verified_at")):
            errors.append(f"verified probe 的 verified_at 必须是含时区 ISO 8601：{pid}")
        if not probe.get("result") and not probe.get("artifact_path"):
            errors.append(f"verified probe 缺少 result/artifact：{pid}")
        artifact = str(probe.get("artifact_path", "")).strip()
        if artifact:
            resolved, artifact_errors = resolve_run_evidence_ref(run, artifact)
            resolved_evidence.extend(resolved)
            errors.extend(f"probe artifact 无法解析：{pid} -> {item}" for item in artifact_errors)
    for row in forks:
        fid=row.get("fork_id","unknown")
        if _bool(row.get("nondelegable")) and row.get("status") not in {"decided","accepted-risk"}:
            errors.append(f"nondelegable fork 未决：{fid}")
        if _bool(row.get("structural_fork")):
            covered={p.get("option_id") for p in probes if p.get("fork_id")==fid and p.get("status")=="verified" and p.get("probe_type") in allowed}
            if len(covered)<2 and row.get("status")!="accepted-risk":
                (errors if strict else warnings).append(f"结构分叉 {fid} 的 verified option probes 少于 2 个")
    try:
        fp=_read_fingerprints(fingerprint_path)
        if len(fp.get("candidates",[]))<2:
            (errors if require_fingerprints else warnings).append("Assumption Fingerprints 少于 2 个候选，尚无法审计共享世界观")
    except SystemExit:
        errors.append("ASSUMPTION_FINGERPRINTS.json 无效")
    if "fp" in locals():
        result=analyze_assumption_diversity(run, persist=False)
        warnings.extend(result.get("warnings",[]))
        diversity_json=run/"interpretation"/"ASSUMPTION_DIVERSITY.json"
        diversity_md=run/"interpretation"/"ASSUMPTION_DIVERSITY.md"
        if not diversity_json.is_file():
            errors.append("缺少 interpretation/ASSUMPTION_DIVERSITY.json；请重新运行 analyze")
        else:
            try:
                recorded=read_json(diversity_json,expect=dict)
            except SystemExit:
                errors.append("ASSUMPTION_DIVERSITY.json 无效")
            else:
                current_semantics={key:value for key,value in result.items() if key!="generated_at"}
                recorded_semantics={key:value for key,value in recorded.items() if key!="generated_at"}
                if recorded_semantics!=current_semantics:
                    errors.append("ASSUMPTION_DIVERSITY.json 与当前 fingerprints/policy 不一致；请重新运行 analyze")
        if not diversity_md.is_file():
            errors.append("缺少 interpretation/ASSUMPTION_DIVERSITY.md；请重新运行 analyze")
        elif diversity_md.read_text(encoding="utf-8-sig")!=_diversity_markdown(result):
            errors.append("ASSUMPTION_DIVERSITY.md 与当前 fingerprints/policy 不一致；请重新运行 analyze")
    else:
        result={"warnings":[],"candidate_count":0,"dimension_count":0}
    interpretation_audit=run/"interpretation"/"INTERPRETATION_AUDIT.md"
    if not interpretation_audit.is_file() or interpretation_audit.stat().st_size == 0:
        errors.append("缺少或为空：interpretation/INTERPRETATION_AUDIT.md")
    elif contains_placeholder(interpretation_audit,["[DRAFT_REQUIRED]","TODO"]):
        (errors if strict else warnings).append("INTERPRETATION_AUDIT.md 仍含占位符")
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
    return {"ok":not errors,"errors":errors,"warnings":warnings,"fork_count":len(forks),"probe_count":len(probes),"diversity":result,"resolved_evidence":resolved_evidence}


def cmd_lint(args: argparse.Namespace) -> int:
    run=resolve_run(args.run); report=validate_interpretation(run,strict=args.strict)
    print(json.dumps(report,ensure_ascii=False,indent=2)); return 0 if report["ok"] else 3


def cmd_status(args: argparse.Namespace) -> int:
    run=resolve_run(args.run); _ensure(run); fork_path,probe_path,_=_paths(run)
    forks=read_csv(fork_path, expected_fields=FORK_FIELDS)
    probes=read_csv(probe_path, expected_fields=PROBE_FIELDS)
    print(f"Forks: {len(forks)}; probes: {len(probes)}")
    for r in forks:
        print(f"- {r.get('fork_id')}: {r.get('dimension')} / {r.get('status')} / nondelegable={r.get('nondelegable')} / selected={r.get('selected_option')}")
    return 0


def ensure(run: Path) -> None:
    """Public initializer used by the v5.1 orchestrator."""
    _ensure(run)


def audit(run: Path, *, strict: bool) -> tuple[dict[str, Any], int]:
    """Return a structured audit result and shell-style status code."""
    result = validate_interpretation(run, strict=strict, require_fingerprints=strict)
    # Orchestrator-compatible naming.
    result = {**result, "passed": result.get("ok", False), "generated_at": now_iso()}
    return result, (0 if result["passed"] else 3)


def build_parser()->argparse.ArgumentParser:
    p=argparse.ArgumentParser(description=__doc__); sub=p.add_subparsers(dest="command",required=True)
    x=sub.add_parser("init"); x.add_argument("--run"); x.set_defaults(func=cmd_init)
    x=sub.add_parser("fork-add"); x.add_argument("--run"); x.add_argument("--id"); x.add_argument("--dimension",required=True); x.add_argument("--question",required=True); x.add_argument("--option",action="append",required=True); x.add_argument("--impact",choices=["low","medium","high","critical"],default="high"); x.add_argument("--uncertainty",choices=["low","medium","high","unknown"],default="unknown"); x.add_argument("--irreversibility",choices=["low","medium","high"],default="high"); x.add_argument("--changes-objective",action="store_true"); x.add_argument("--changes-feasible-set",action="store_true"); x.add_argument("--expected-result-change-pct",type=float); x.add_argument("--structural",action="store_true"); x.add_argument("--note"); x.set_defaults(func=cmd_fork_add)
    x=sub.add_parser("probe-add"); x.add_argument("--run"); x.add_argument("--id"); x.add_argument("--fork-id",required=True); x.add_argument("--option-id",required=True); x.add_argument("--probe-type",required=True); x.add_argument("--method",required=True); x.add_argument("--artifact"); x.add_argument("--metric"); x.add_argument("--result"); x.add_argument("--status",choices=["planned","running","verified","failed"],default="planned"); x.add_argument("--by",default=""); x.add_argument("--note"); x.set_defaults(func=cmd_probe_add)
    x=sub.add_parser("fingerprint-set"); x.add_argument("--run"); x.add_argument("--candidate",required=True); x.add_argument("--set",action="append",required=True); x.add_argument("--source"); x.set_defaults(func=cmd_fingerprint_set)
    x=sub.add_parser("analyze"); x.add_argument("--run"); x.set_defaults(func=cmd_analyze)
    x=sub.add_parser("decide"); x.add_argument("--run"); x.add_argument("--fork-id",required=True); x.add_argument("--select",required=True); x.add_argument("--by",required=True); x.add_argument("--human-confirm",action="store_true"); x.add_argument("--evidence",action="append"); x.add_argument("--reason",required=True); x.add_argument("--accept-unprobed-risk",action="store_true"); x.set_defaults(func=cmd_decide)
    x=sub.add_parser("lint"); x.add_argument("--run"); x.add_argument("--strict",action="store_true"); x.set_defaults(func=cmd_lint)
    x=sub.add_parser("status"); x.add_argument("--run"); x.set_defaults(func=cmd_status)
    return p


def main()->int:
    args=build_parser().parse_args(); return int(args.func(args))
if __name__=="__main__": raise SystemExit(main())
