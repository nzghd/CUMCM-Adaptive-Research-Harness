#!/usr/bin/env python3
"""Structured mathematical-model construction and obligation ledger.

The tool checks whether a model has explicit symbols, equations, assumptions,
parameter provenance, numerical-method details, validation plans and mappings
to code/evidence.  It does not prove the mathematics by itself.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from common import (
    ROOT,
    die,
    is_named_human_identity,
    is_timezone_aware_iso,
    now_iso,
    read_csv,
    read_json,
    require_safe_component,
    resolve_run_evidence_ref,
    write_csv,
    write_json,
)
from v51_common import resolve_run_flexible, run_relative

SYMBOL_FIELDS = ["symbol_id", "latex", "plain_name", "meaning", "role", "domain", "unit", "source", "notes"]
EQUATION_FIELDS = [
    "equation_id", "latex", "equation_type", "description", "depends_on",
    "assumptions", "units_check", "derivation_location", "verification_ref", "status", "notes",
]
PARAMETER_FIELDS = [
    "parameter_id", "symbol", "meaning", "value_or_estimator", "unit", "source_type",
    "source_ref", "identifiability", "calibration_protocol", "sensitivity_case", "status", "notes",
]
MAP_FIELDS = [
    "map_id", "mathematical_item", "role", "code_path", "code_symbol", "test_path",
    "experiment_id", "evidence_id", "paper_location", "status", "notes",
]
OBLIGATION_FIELDS = [
    "obligation_id", "severity", "claim", "claim_type", "required_conditions",
    "current_justification", "symbolic_check", "numerical_check", "counterexample_test",
    "proof_provenance", "evidence_refs", "status", "resolution", "reviewed_by", "reviewed_at", "notes",
]
PLACEHOLDERS = ("[DRAFT_REQUIRED]", "TODO", "TBD", "待填写", "{{")
CLOSED_OBLIGATIONS = {"discharged", "downgraded", "rejected", "not_applicable"}
EQUATION_STATUSES = {"draft", "checked", "rejected"}
PARAMETER_STATUSES = {"draft", "verified", "blocked"}
MAPPING_STATUSES = {"planned", "implemented", "verified"}
PARAMETER_SOURCE_TYPES = {"problem", "data_estimate", "external_source", "calibrated", "computational", "unknown"}
OBLIGATION_STATUSES = CLOSED_OBLIGATIONS | {"open"}

TEXT_TEMPLATES = {
    "MODEL_INTENT.md": """# Model Intent\n\n## Questions served\n\n- [DRAFT_REQUIRED]\n\n## Decision or explanatory purpose\n\n[DRAFT_REQUIRED]\n\n## Why this mathematical structure is appropriate\n\n[DRAFT_REQUIRED]\n\n## Competing structures deliberately rejected or retained\n\n[DRAFT_REQUIRED]\n""",
    "ASSUMPTIONS.md": """# Assumptions\n\nFor every assumption, state whether it is literal, interpretive, structural, parametric, or computational. Link structural assumptions to interpretation forks.\n\n| ID | Assumption | Type | Source/fork | Consequence if false | Test or sensitivity | Status |\n|---|---|---|---|---|---|---|\n| [DRAFT_REQUIRED] |  |  |  |  |  |  |\n""",
    "FORMULATION.md": """# Mathematical Formulation\n\n## Sets and indices\n\n[DRAFT_REQUIRED]\n\n## Variables and parameters\n\nUse `SYMBOL_TABLE.csv`; explain the key objects here.\n\n## Objective / governing relations\n\n[DRAFT_REQUIRED]\n\n## Constraints, initial conditions and boundary conditions\n\n[DRAFT_REQUIRED]\n\n## Output mapping and feasible domain\n\n[DRAFT_REQUIRED]\n\n## Units and scaling\n\n[DRAFT_REQUIRED]\n""",
    "DERIVATION.md": """# Derivation\n\nFor every nontrivial step: cite prior equation IDs, state the operation/theorem/approximation, list required conditions, and quantify approximation error where relevant.\n\n[DRAFT_REQUIRED]\n""",
    "PARAMETER_IDENTIFICATION.md": """# Parameter Identification\n\nSeparate parameters given by the statement, estimated from data, taken from external sources, calibrated, fixed for computation, or not identifiable.\n\n[DRAFT_REQUIRED]\n""",
    "THEORY_CHECKS.md": """# Theory Checks\n\n## Dimensional consistency\n\n[DRAFT_REQUIRED]\n\n## Existence, uniqueness, convexity, stability or identifiability obligations\n\n[DRAFT_REQUIRED]\n\n## Limiting cases and counterexamples\n\n[DRAFT_REQUIRED]\n\n## What has **not** been proved\n\n[DRAFT_REQUIRED]\n""",
    "NUMERICAL_METHOD.md": """# Numerical Method\n\n## Algorithm and solver\n\n[DRAFT_REQUIRED]\n\n## Initialization, tolerances and stopping criteria\n\n[DRAFT_REQUIRED]\n\n## Scaling, conditioning and complexity\n\n[DRAFT_REQUIRED]\n\n## Exact small cases / synthetic checks / convergence diagnostics\n\n[DRAFT_REQUIRED]\n""",
    "VALIDATION_PROTOCOL.md": """# Validation Protocol\n\n## Baseline\n\n[DRAFT_REQUIRED]\n\n## Falsification tests\n\n[DRAFT_REQUIRED]\n\n## Structural sensitivity\n\n[DRAFT_REQUIRED]\n\n## Parameter sensitivity and uncertainty\n\n[DRAFT_REQUIRED]\n\n## Metrics and acceptance criteria\n\n[DRAFT_REQUIRED]\n""",
}


def module_dir(run: Path, module_id: str) -> Path:
    mid = require_safe_component(module_id, label="module id")
    run_root = run.resolve()
    research_root = run_root / "research"
    namespace = research_root / "modules"
    for component in (research_root, namespace):
        if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
            die("module id namespace 不能是符号链接或目录联接。")
    parent = namespace.resolve()
    try:
        parent.relative_to(run_root)
    except ValueError:
        die("module id 目标根目录越出运行目录。")
    lexical_target = parent / mid
    if lexical_target.is_symlink() or getattr(lexical_target, "is_junction", lambda: False)():
        die("module id 目标不能是符号链接或目录联接。")
    target = lexical_target.resolve()
    if target.parent != parent or target.name != mid:
        die("module id 目标必须是 research/modules 的直接子目录。")
    return target


def ensure_tables(root: Path) -> None:
    specs = {
        "SYMBOL_TABLE.csv": SYMBOL_FIELDS,
        "EQUATION_REGISTRY.csv": EQUATION_FIELDS,
        "PARAMETER_REGISTER.csv": PARAMETER_FIELDS,
        "MODEL_TO_CODE_MAP.csv": MAP_FIELDS,
        "MATHEMATICAL_OBLIGATIONS.csv": OBLIGATION_FIELDS,
    }
    for name, fields in specs.items():
        path = root / name
        if not path.exists():
            write_csv(path, fields, [])


def _load_module_registry(run: Path) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    """Load a producer-compatible module registry and reject ambiguous rows."""
    path = run / "research" / "MODULE_REGISTRY.json"
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        die(f"Module registry 不能是符号链接或目录联接：{path}")
    if path.exists() and not path.is_file():
        die(f"Module registry 必须是普通文件：{path}")
    registry = read_json(path, expect=dict) if path.exists() else {"next_id": 1, "modules": []}
    rows = registry.get("modules", [])
    if not isinstance(rows, list):
        die(f"Module registry modules 必须是数组：{path}")
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            die(f"Module registry 第 {index + 1} 条记录必须是对象：{path}")
        raw_id = row.get("id")
        if not isinstance(raw_id, str) or not raw_id:
            die(f"Module registry 第 {index + 1} 条记录缺少字符串 id：{path}")
        record_id = require_safe_component(raw_id, label="Module registry id")
        if record_id in seen:
            die(f"Module registry 存在重复 id：{record_id}")
        seen.add(record_id)
    next_value = registry.get("next_id", 1)
    if isinstance(next_value, bool) or not isinstance(next_value, int) or next_value < 1:
        die(f"Module registry next_id 必须是正整数：{path}")
    return path, registry, rows


def _upsert_module_registry(
    path: Path,
    registry: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    module_id: str,
    card_path: Path,
    run: Path,
    status: str,
    created_at: str,
    title: str,
) -> None:
    record = {
        "id": module_id,
        "path": run_relative(card_path, run),
        "status": status,
        "created_at": created_at,
        "title": title,
    }
    updated_rows: list[dict[str, Any]] = []
    found = False
    for row in rows:
        if row["id"] == module_id:
            updated = dict(row)
            updated.update(record)
            updated_rows.append(updated)
            found = True
        else:
            updated_rows.append(row)
    if not found:
        updated_rows.append(record)

    numeric_ids: list[int] = []
    pattern = re.compile(r"^MOD_(\d+)$", re.I)
    for row in updated_rows:
        match = pattern.fullmatch(str(row.get("id", "")))
        if match:
            numeric_ids.append(int(match.group(1)))
    registry["modules"] = updated_rows
    registry["next_id"] = max(
        registry.get("next_id", 1),
        max(numeric_ids, default=0) + 1,
    )
    write_json(path, registry)


def create_module(run: Path, *, module_id: str, title: str, question_ids: list[str], method_families: list[str], overwrite: bool) -> Path:
    root = module_dir(run, module_id)
    if root.exists() and any(root.iterdir()) and not overwrite:
        die(f"模块目录已存在且非空：{root}")
    registry_path, registry, registry_rows = _load_module_registry(run)
    root.mkdir(parents=True, exist_ok=True)
    for name, text in TEXT_TEMPLATES.items():
        path = root / name
        if overwrite or not path.exists():
            path.write_text(text, encoding="utf-8")
    ensure_tables(root)
    created_at = now_iso()
    spec = {
        "schema_version": 1,
        "module_id": module_id,
        "title": title,
        "question_ids": question_ids,
        "method_families": method_families,
        "created_at": created_at,
        "updated_at": created_at,
        "status": "draft",
        "requires_code": True,
        "requires_parameters": True,
        "interpretation_forks": [],
        "source_idea_ids": [],
        "limitations": [],
        "human_owner": "",
    }
    if overwrite or not (root / "MODULE_SPEC.json").exists():
        write_json(root / "MODULE_SPEC.json", spec)
    card_path = root / "MODULE_CARD.json"
    if overwrite or not card_path.exists():
        card = {
            "schema_version": 1,
            "id": module_id,
            "title": title,
            "source_ideas": [],
            "target_nodes": question_ids,
            "method_families": method_families,
            "formulation_summary": "See FORMULATION.md and EQUATION_REGISTRY.csv",
            "equations_or_algorithm": "See FORMULATION.md, DERIVATION.md and NUMERICAL_METHOD.md",
            "assumptions": [],
            "parameters": [],
            "validation_plan": ["See VALIDATION_PROTOCOL.md"],
            "baseline": "See VALIDATION_PROTOCOL.md",
            "risks": [],
            "status": "deepening",
        }
        write_json(card_path, card)
        _upsert_module_registry(
            registry_path,
            registry,
            registry_rows,
            module_id=module_id,
            card_path=card_path,
            run=run,
            status=card["status"],
            created_at=created_at,
            title=title,
        )
    return root


def cmd_init_module(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    root = create_module(
        run, module_id=args.module, title=args.title,
        question_ids=args.question_ids, method_families=args.method_family,
        overwrite=args.overwrite,
    )
    print(f"Initialized mathematical module: {run_relative(root, run)}")
    return 0


def _next_id(rows: list[dict[str, str]], field: str, prefix: str) -> str:
    used = []
    pattern = re.compile(rf"^{re.escape(prefix)}[-_](\d+)$", re.I)
    for row in rows:
        match = pattern.match(row.get(field, ""))
        if match:
            used.append(int(match.group(1)))
    return f"{prefix}-{max(used, default=0)+1:03d}"


def cmd_symbol_add(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    root = module_dir(run, args.module)
    ensure_tables(root)
    path = root / "SYMBOL_TABLE.csv"
    rows = read_csv(path)
    sid = args.symbol_id or _next_id(rows, "symbol_id", "SYM")
    if any(r.get("symbol_id") == sid or r.get("latex") == args.latex for r in rows):
        die(f"符号 ID 或 LaTeX 已存在：{sid}/{args.latex}")
    rows.append({
        "symbol_id": sid, "latex": args.latex, "plain_name": args.name,
        "meaning": args.meaning, "role": args.role, "domain": args.domain,
        "unit": args.unit or "dimensionless", "source": args.source or "", "notes": args.notes or "",
    })
    write_csv(path, SYMBOL_FIELDS, rows)
    print(f"Added symbol {sid}")
    return 0


def cmd_equation_add(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    root = module_dir(run, args.module)
    ensure_tables(root)
    path = root / "EQUATION_REGISTRY.csv"
    rows = read_csv(path)
    eid = args.equation_id or _next_id(rows, "equation_id", "EQ")
    if any(r.get("equation_id") == eid for r in rows):
        die(f"equation_id 已存在：{eid}")
    rows.append({
        "equation_id": eid, "latex": args.latex, "equation_type": args.type,
        "description": args.description, "depends_on": ";".join(args.depends_on),
        "assumptions": ";".join(args.assumptions), "units_check": args.units_check,
        "derivation_location": args.derivation_location or "DERIVATION.md",
        "verification_ref": args.verification_ref or "", "status": args.status, "notes": args.notes or "",
    })
    write_csv(path, EQUATION_FIELDS, rows)
    print(f"Added equation {eid}")
    return 0


def cmd_parameter_add(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    root = module_dir(run, args.module)
    ensure_tables(root)
    path = root / "PARAMETER_REGISTER.csv"
    rows = read_csv(path)
    pid = args.parameter_id or _next_id(rows, "parameter_id", "PAR")
    if any(r.get("parameter_id") == pid for r in rows):
        die(f"parameter_id 已存在：{pid}")
    rows.append({
        "parameter_id": pid, "symbol": args.symbol, "meaning": args.meaning,
        "value_or_estimator": args.value_or_estimator, "unit": args.unit or "dimensionless",
        "source_type": args.source_type, "source_ref": args.source_ref or "",
        "identifiability": args.identifiability, "calibration_protocol": args.calibration_protocol or "",
        "sensitivity_case": args.sensitivity_case or "", "status": args.status, "notes": args.notes or "",
    })
    write_csv(path, PARAMETER_FIELDS, rows)
    print(f"Added parameter {pid}")
    return 0


def cmd_map_code(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    root = module_dir(run, args.module)
    ensure_tables(root)
    path = root / "MODEL_TO_CODE_MAP.csv"
    rows = read_csv(path)
    map_id = args.map_id or _next_id(rows, "map_id", "MAP")
    if any(r.get("map_id") == map_id for r in rows):
        die(f"map_id 已存在：{map_id}")
    rows.append({
        "map_id": map_id, "mathematical_item": args.item, "role": args.role,
        "code_path": args.code, "code_symbol": args.symbol or "", "test_path": args.test or "",
        "experiment_id": args.experiment or "", "evidence_id": args.evidence or "",
        "paper_location": args.paper or "", "status": args.status, "notes": args.notes or "",
    })
    write_csv(path, MAP_FIELDS, rows)
    print(f"Added model-to-code map {map_id}")
    return 0


def cmd_obligation_add(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    root = module_dir(run, args.module)
    ensure_tables(root)
    path = root / "MATHEMATICAL_OBLIGATIONS.csv"
    rows = read_csv(path)
    oid = args.obligation_id or _next_id(rows, "obligation_id", "MATH")
    if any(r.get("obligation_id") == oid for r in rows):
        die(f"obligation_id 已存在：{oid}")
    rows.append({
        "obligation_id": oid, "severity": args.severity, "claim": args.claim,
        "claim_type": args.claim_type, "required_conditions": args.conditions,
        "current_justification": args.justification or "", "symbolic_check": args.symbolic_check or "",
        "numerical_check": args.numerical_check or "", "counterexample_test": args.counterexample_test or "",
        "proof_provenance": args.proof_provenance or "", "evidence_refs": ";".join(args.evidence),
        "status": "open", "resolution": "", "reviewed_by": "", "reviewed_at": "", "notes": args.notes or "",
    })
    write_csv(path, OBLIGATION_FIELDS, rows)
    print(f"Added mathematical obligation {oid}")
    return 0


def cmd_obligation_resolve(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    root = module_dir(run, args.module)
    path = root / "MATHEMATICAL_OBLIGATIONS.csv"
    rows = read_csv(path)
    if not is_named_human_identity(args.by):
        die("关闭 mathematical obligation 必须提供具名 --by，不能使用 TEAM/UNKNOWN/TODO 等占位值。")
    for row in rows:
        if row.get("obligation_id") == args.obligation_id:
            row.update({
                "status": args.status,
                "resolution": args.resolution,
                "proof_provenance": args.proof_provenance or row.get("proof_provenance", ""),
                "evidence_refs": ";".join(args.evidence) or row.get("evidence_refs", ""),
                "reviewed_by": args.by,
                "reviewed_at": now_iso(),
                "notes": args.notes or row.get("notes", ""),
            })
            if args.status == "discharged" and not row.get("proof_provenance", "").strip():
                die("discharged obligation 必须说明 --proof-provenance。")
            write_csv(path, OBLIGATION_FIELDS, rows)
            print(f"Resolved {args.obligation_id}: {args.status}")
            return 0
    die(f"未找到 obligation：{args.obligation_id}")


def _text_ok(path: Path, minimum: int) -> tuple[bool, str]:
    if not path.exists():
        return False, "missing"
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    hits = [token for token in PLACEHOLDERS if token in text]
    if hits:
        return False, "placeholder:" + ",".join(hits)
    substantive = re.sub(r"[#>|*`_\-\s]", "", text)
    if len(substantive) < minimum:
        return False, f"too_short:{len(substantive)}<{minimum}"
    return True, "ok"


def _split_refs(value: Any) -> list[str]:
    return [item.strip() for item in re.split(r"[;,|]", str(value or "")) if item.strip()]


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


def _known_ledger_refs(run: Path) -> dict[str, set[str]]:
    """Return only ledger references backed by usable run-local provenance."""
    refs = {"evidence": set(), "experiments": set(), "sources": set()}

    for row in read_csv(run / "evidence" / "CLAIM_EVIDENCE_LEDGER.csv"):
        evidence_id = str(row.get("evidence_id", "")).strip()
        resolved, evidence_errors = resolve_run_evidence_ref(run, evidence_id)
        if evidence_id and resolved and not evidence_errors:
            refs["evidence"].add(evidence_id)

    # Reuse the strict G3 receipt verifier so a self-reported ``passed`` JSON
    # cannot satisfy mathematical provenance on its own.
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
                refs["experiments"].add(experiment_id)

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
            and is_named_human_identity(row.get("verified_by", ""))
            and is_timezone_aware_iso(row.get("accessed_at", ""))
        ):
            refs["sources"].add(source_id)
            refs["sources"].add(url)
    return refs


def _reference_exists(run: Path, module_root: Path, value: str, known_refs: set[str]) -> bool:
    ref = str(value or "").strip()
    if not ref:
        return False
    if ref in known_refs:
        return True
    if ref.startswith(("http://", "https://")):
        return ref in known_refs
    raw = Path(ref.split("#", 1)[0]).expanduser()
    candidates = [raw] if raw.is_absolute() else [module_root / raw, run / raw]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(run.resolve())
        except (OSError, ValueError):
            continue
        if resolved.is_file() and resolved.stat().st_size > 0:
            return True
    return False


def _references_resolve(run: Path, module_root: Path, value: str, known_refs: set[str]) -> bool:
    refs = _split_refs(value)
    return bool(refs) and all(_reference_exists(run, module_root, item, known_refs) for item in refs)


def _ledger_ids_resolve(value: str, known_refs: set[str]) -> bool:
    refs = _split_refs(value)
    return bool(refs) and all(item in known_refs for item in refs)


def _duplicates(rows: list[dict[str, str]], field: str) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for row in rows:
        value = str(row.get(field, "")).strip()
        if not value:
            continue
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


def lint_module(
    run: Path,
    module_id: str,
    *,
    strict: bool,
    persist: bool = True,
) -> tuple[dict[str, Any], int]:
    root = module_dir(run, module_id)
    if not root.exists():
        die(f"模块目录不存在：{root}")
    errors: list[str] = []
    warnings: list[str] = []
    file_checks: dict[str, str] = {}
    minimums = {
        "MODEL_INTENT.md": 120, "ASSUMPTIONS.md": 180, "FORMULATION.md": 250,
        "DERIVATION.md": 250, "PARAMETER_IDENTIFICATION.md": 150,
        "THEORY_CHECKS.md": 180, "NUMERICAL_METHOD.md": 180, "VALIDATION_PROTOCOL.md": 180,
    }
    for name, minimum in minimums.items():
        ok, detail = _text_ok(root / name, minimum if strict else min(60, minimum))
        file_checks[name] = detail
        if not ok:
            (errors if strict else warnings).append(f"{name}: {detail}")
    spec_path = root / "MODULE_SPEC.json"
    if not spec_path.exists():
        errors.append("MODULE_SPEC.json missing")
        spec = {}
    else:
        try:
            spec = read_json(spec_path, expect=dict)
        except SystemExit:
            raise
        for key in ("module_id", "title", "question_ids", "method_families"):
            if not spec.get(key):
                errors.append(f"MODULE_SPEC.json missing {key}")
    table_specs = {
        "SYMBOL_TABLE.csv": SYMBOL_FIELDS,
        "EQUATION_REGISTRY.csv": EQUATION_FIELDS,
        "PARAMETER_REGISTER.csv": PARAMETER_FIELDS,
        "MODEL_TO_CODE_MAP.csv": MAP_FIELDS,
        "MATHEMATICAL_OBLIGATIONS.csv": OBLIGATION_FIELDS,
    }
    for name in table_specs:
        if not (root / name).is_file():
            errors.append(f"{name} missing")
    symbols = read_csv(root / "SYMBOL_TABLE.csv", expected_fields=SYMBOL_FIELDS)
    equations = read_csv(root / "EQUATION_REGISTRY.csv", expected_fields=EQUATION_FIELDS)
    parameters = read_csv(root / "PARAMETER_REGISTER.csv", expected_fields=PARAMETER_FIELDS)
    mappings = read_csv(root / "MODEL_TO_CODE_MAP.csv", expected_fields=MAP_FIELDS)
    obligations = read_csv(root / "MATHEMATICAL_OBLIGATIONS.csv", expected_fields=OBLIGATION_FIELDS)
    ledger_refs = _known_ledger_refs(run)
    all_known_refs = set().union(*ledger_refs.values())
    if strict and not symbols:
        errors.append("No symbols registered")
    if strict and not equations:
        errors.append("No equations registered")
    if strict and spec.get("requires_parameters", True) and not parameters:
        errors.append("No parameters registered")
    if strict and spec.get("requires_code", True) and not mappings:
        errors.append("No model-to-code mappings registered")
    for rows, field, label in (
        (symbols, "symbol_id", "symbol"),
        (equations, "equation_id", "equation"),
        (parameters, "parameter_id", "parameter"),
        (mappings, "map_id", "mapping"),
        (obligations, "obligation_id", "obligation"),
    ):
        missing_ids = [str(index + 1) for index, row in enumerate(rows) if not str(row.get(field, "")).strip()]
        if missing_ids:
            errors.append(f"{label} rows missing {field}: " + ", ".join(missing_ids))
        duplicate_ids = _duplicates(rows, field)
        if duplicate_ids:
            errors.append(f"Duplicate {label} IDs: " + ", ".join(duplicate_ids))
    duplicate_latex = _duplicates(symbols, "latex")
    if duplicate_latex:
        errors.append("Duplicate symbol LaTeX values: " + ", ".join(duplicate_latex))
    if strict:
        for row in symbols:
            sid = str(row.get("symbol_id", "")).strip() or "<missing-symbol-id>"
            for field in ("latex", "meaning", "role", "domain", "unit"):
                if not str(row.get(field, "")).strip():
                    errors.append(f"{sid}: symbol missing {field}")

    active_equation_ids = {
        str(r.get("equation_id", "")).strip()
        for r in equations
        if r.get("equation_id") and str(r.get("status", "")).strip().lower() in {"draft", "checked"}
    }
    active_equations = []
    for row in equations:
        eid = str(row.get("equation_id", "")).strip() or "<missing-equation-id>"
        status = str(row.get("status", "")).strip().lower()
        units_check = str(row.get("units_check", "")).strip().lower()
        if status not in EQUATION_STATUSES:
            errors.append(f"{eid}: invalid equation status {status!r}")
            continue
        if status == "rejected":
            continue
        active_equations.append(row)
        if strict:
            for field in ("latex", "equation_type", "description"):
                if not str(row.get(field, "")).strip():
                    errors.append(f"{eid}: equation missing {field}")
        if strict and status != "checked":
            errors.append(f"{eid}: equation is not checked (status={status})")
        if units_check == "fail":
            errors.append(f"{eid}: units_check=fail")
        elif strict and units_check not in {"pass", "not_applicable"}:
            errors.append(f"{eid}: units are not closed (units_check={units_check or 'missing'})")
        derivation = str(row.get("derivation_location", "")).strip()
        if strict and (not derivation or not _reference_exists(run, root, derivation, set())):
            errors.append(f"{eid}: derivation_location does not resolve: {derivation!r}")
        verification = str(row.get("verification_ref", "")).strip()
        if strict and not _references_resolve(run, root, verification, all_known_refs):
            errors.append(f"{eid}: verification_ref does not resolve: {verification!r}")
        for dependency in _split_refs(row.get("depends_on", "")):
            if dependency == eid or dependency not in active_equation_ids:
                errors.append(f"{eid}: invalid equation dependency {dependency!r}")
    if strict and equations and not active_equations:
        errors.append("No active checked equations registered")

    for row in parameters:
        pid = str(row.get("parameter_id", "")).strip() or "<missing-parameter-id>"
        status = str(row.get("status", "")).strip().lower()
        source_type = str(row.get("source_type", "")).strip().lower()
        if status not in PARAMETER_STATUSES:
            errors.append(f"{pid}: invalid parameter status {status!r}")
        elif strict and status != "verified":
            errors.append(f"{pid}: parameter is not verified (status={status})")
        if source_type not in PARAMETER_SOURCE_TYPES:
            errors.append(f"{pid}: invalid parameter source_type {source_type!r}")
        if strict and status == "verified" and source_type == "unknown":
            errors.append(f"{pid}: verified parameter cannot have source_type=unknown")
        if strict:
            for field in ("symbol", "meaning", "value_or_estimator", "unit", "identifiability"):
                if not str(row.get(field, "")).strip():
                    errors.append(f"{pid}: parameter missing {field}")
        source_ref = str(row.get("source_ref", "")).strip()
        if source_ref and not _references_resolve(run, root, source_ref, all_known_refs):
            errors.append(f"{pid}: source_ref does not resolve: {source_ref!r}")
        if strict and source_type not in {"computational", "unknown"} and not source_ref:
            errors.append(f"{pid}: parameter provenance requires source_ref")

    for row in mappings:
        mid = str(row.get("map_id", "")).strip() or "<missing-map-id>"
        status = str(row.get("status", "")).strip().lower()
        if status not in MAPPING_STATUSES:
            errors.append(f"{mid}: invalid mapping status {status!r}")
        elif strict and status not in {"implemented", "verified"}:
            errors.append(f"{mid}: mapping is not implemented (status={status})")
        code_path = str(row.get("code_path", "")).strip()
        if not code_path or not _reference_exists(run, root, code_path, set()):
            errors.append(f"{mid}: code_path does not resolve: {code_path!r}")
        test_path = str(row.get("test_path", "")).strip()
        if test_path and not _reference_exists(run, root, test_path, set()):
            errors.append(f"{mid}: test_path does not resolve: {test_path!r}")
        evidence_id = str(row.get("evidence_id", "")).strip()
        if evidence_id and not _ledger_ids_resolve(evidence_id, ledger_refs["evidence"]):
            errors.append(f"{mid}: evidence_id does not resolve: {evidence_id!r}")
        experiment_id = str(row.get("experiment_id", "")).strip()
        if experiment_id and not _ledger_ids_resolve(experiment_id, ledger_refs["experiments"]):
            errors.append(f"{mid}: experiment_id does not resolve: {experiment_id!r}")
        if strict and status == "verified" and not (test_path or evidence_id or experiment_id):
            errors.append(f"{mid}: verified mapping lacks test/evidence/experiment provenance")

    for row in obligations:
        oid = str(row.get("obligation_id", "")).strip() or "<missing-obligation-id>"
        status = str(row.get("status", "")).strip().lower()
        if status not in OBLIGATION_STATUSES:
            errors.append(f"{oid}: invalid obligation status {status!r}")
        if strict and status in CLOSED_OBLIGATIONS:
            if not str(row.get("resolution", "")).strip():
                errors.append(f"{oid}: closed obligation lacks resolution")
            if not is_named_human_identity(row.get("reviewed_by", "")):
                errors.append(
                    f"{oid}: closed obligation requires a named reviewed_by, "
                    "not TEAM/UNKNOWN/TODO or another placeholder"
                )
            if not is_timezone_aware_iso(row.get("reviewed_at", "")):
                errors.append(
                    f"{oid}: closed obligation reviewed_at must be timezone-aware ISO 8601"
                )
            evidence_refs = str(row.get("evidence_refs", "")).strip()
            evidence_resolves = bool(evidence_refs) and _references_resolve(
                run, root, evidence_refs, all_known_refs
            )
            if evidence_refs and not evidence_resolves:
                errors.append(f"{oid}: evidence_refs do not resolve: {evidence_refs!r}")
            proof_provenance = str(row.get("proof_provenance", "")).strip()
            proof_resolves = bool(proof_provenance) and _references_resolve(
                run, root, proof_provenance, all_known_refs
            )
            if status == "discharged":
                if not proof_provenance:
                    errors.append(f"{oid}: discharged obligation lacks proof_provenance")
                if not (proof_resolves or evidence_resolves):
                    errors.append(
                        f"{oid}: discharged obligation lacks resolvable proof/evidence provenance"
                    )
            elif status in {"downgraded", "rejected"} and not evidence_resolves:
                errors.append(f"{oid}: {status} obligation lacks resolvable evidence_refs")

    open_critical = [r for r in obligations if r.get("severity") in {"high", "critical"} and r.get("status") not in CLOSED_OBLIGATIONS]
    if open_critical:
        errors.append("Open high/critical obligations: " + ", ".join(r.get("obligation_id", "") for r in open_critical))
    open_other = [r for r in obligations if r.get("status") not in CLOSED_OBLIGATIONS]
    if open_other and not open_critical:
        warnings.append("Open noncritical obligations: " + ", ".join(r.get("obligation_id", "") for r in open_other))
    unknown_maps = [r.get("map_id") for r in mappings if r.get("mathematical_item") not in active_equation_ids and not str(r.get("mathematical_item", "")).startswith(("OBJ", "CON", "ALG", "DEF", "OUT"))]
    if unknown_maps:
        (errors if strict else warnings).append("Mappings reference unregistered mathematical items: " + ", ".join(str(x) for x in unknown_maps))
    result = {
        "module_id": module_id, "generated_at": now_iso(), "strict": strict,
        "file_checks": file_checks, "counts": {
            "symbols": len(symbols), "equations": len(equations), "parameters": len(parameters),
            "mappings": len(mappings), "obligations": len(obligations),
        },
        "errors": errors, "warnings": warnings, "passed": not errors,
        "note": "Passing checks structure and traceability; it does not establish mathematical correctness.",
    }
    if persist:
        write_json(root / "MATH_LINT.json", result)
    return result, (0 if not errors else 3)


def cmd_lint(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    result, code = lint_module(run, args.module, strict=args.strict)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


def cmd_status(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    roots = sorted((run / "research" / "modules").glob("*/MODULE_SPEC.json")) if (run / "research/modules").exists() else []
    if args.module:
        roots = [module_dir(run, args.module) / "MODULE_SPEC.json"]
    for spec_path in roots:
        if not spec_path.exists():
            continue
        spec = read_json(spec_path, expect=dict)
        lint_path = spec_path.parent / "MATH_LINT.json"
        lint = read_json(lint_path, expect=dict) if lint_path.exists() else {}
        print(f"{spec.get('module_id', spec_path.parent.name)}\t{spec.get('status','')}\tpassed={lint.get('passed','not-run')}\t{spec.get('title','')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-module")
    p.add_argument("--run")
    p.add_argument("--module", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--question-ids", nargs="+", required=True)
    p.add_argument("--method-family", action="append", required=True)
    p.add_argument("--overwrite", action="store_true")
    p.set_defaults(func=cmd_init_module)

    p = sub.add_parser("symbol-add")
    p.add_argument("--run"); p.add_argument("--module", required=True)
    p.add_argument("--symbol-id"); p.add_argument("--latex", required=True); p.add_argument("--name", required=True)
    p.add_argument("--meaning", required=True); p.add_argument("--role", required=True)
    p.add_argument("--domain", required=True); p.add_argument("--unit"); p.add_argument("--source"); p.add_argument("--notes")
    p.set_defaults(func=cmd_symbol_add)

    p = sub.add_parser("equation-add")
    p.add_argument("--run"); p.add_argument("--module", required=True); p.add_argument("--equation-id")
    p.add_argument("--latex", required=True); p.add_argument("--type", required=True); p.add_argument("--description", required=True)
    p.add_argument("--depends-on", action="append", default=[]); p.add_argument("--assumptions", action="append", default=[])
    p.add_argument("--units-check", choices=["pass", "fail", "not_checked", "not_applicable"], default="not_checked")
    p.add_argument("--derivation-location"); p.add_argument("--verification-ref")
    p.add_argument("--status", choices=["draft", "checked", "rejected"], default="draft"); p.add_argument("--notes")
    p.set_defaults(func=cmd_equation_add)

    p = sub.add_parser("parameter-add")
    p.add_argument("--run"); p.add_argument("--module", required=True); p.add_argument("--parameter-id")
    p.add_argument("--symbol", required=True); p.add_argument("--meaning", required=True); p.add_argument("--value-or-estimator", required=True)
    p.add_argument("--unit"); p.add_argument("--source-type", choices=["problem", "data_estimate", "external_source", "calibrated", "computational", "unknown"], required=True)
    p.add_argument("--source-ref"); p.add_argument("--identifiability", choices=["identified", "partially_identified", "not_identified", "not_assessed"], default="not_assessed")
    p.add_argument("--calibration-protocol"); p.add_argument("--sensitivity-case")
    p.add_argument("--status", choices=["draft", "verified", "blocked"], default="draft"); p.add_argument("--notes")
    p.set_defaults(func=cmd_parameter_add)

    p = sub.add_parser("map-code")
    p.add_argument("--run"); p.add_argument("--module", required=True); p.add_argument("--map-id")
    p.add_argument("--item", required=True); p.add_argument("--role", required=True); p.add_argument("--code", required=True)
    p.add_argument("--symbol"); p.add_argument("--test"); p.add_argument("--experiment"); p.add_argument("--evidence"); p.add_argument("--paper")
    p.add_argument("--status", choices=["planned", "implemented", "verified"], default="planned"); p.add_argument("--notes")
    p.set_defaults(func=cmd_map_code)

    p = sub.add_parser("obligation-add")
    p.add_argument("--run"); p.add_argument("--module", required=True); p.add_argument("--obligation-id")
    p.add_argument("--severity", choices=["low", "medium", "high", "critical"], default="high")
    p.add_argument("--claim", required=True); p.add_argument("--claim-type", required=True); p.add_argument("--conditions", required=True)
    p.add_argument("--justification"); p.add_argument("--symbolic-check"); p.add_argument("--numerical-check"); p.add_argument("--counterexample-test")
    p.add_argument("--proof-provenance"); p.add_argument("--evidence", action="append", default=[]); p.add_argument("--notes")
    p.set_defaults(func=cmd_obligation_add)

    p = sub.add_parser("obligation-resolve")
    p.add_argument("--run"); p.add_argument("--module", required=True); p.add_argument("--obligation-id", required=True)
    p.add_argument("--status", choices=sorted(CLOSED_OBLIGATIONS), required=True); p.add_argument("--resolution", required=True)
    p.add_argument("--proof-provenance"); p.add_argument("--evidence", action="append", default=[]); p.add_argument("--by", required=True); p.add_argument("--notes")
    p.set_defaults(func=cmd_obligation_resolve)

    p = sub.add_parser("lint")
    p.add_argument("--run"); p.add_argument("--module", required=True); p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("status")
    p.add_argument("--run"); p.add_argument("--module")
    p.set_defaults(func=cmd_status)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
