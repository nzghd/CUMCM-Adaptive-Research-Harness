#!/usr/bin/env python3
"""Incremental evidence-to-paper utilities on top of the v4.1 paper pipeline."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from common import (
    ROOT,
    atomic_write_text,
    contains_placeholder,
    die,
    is_named_human_identity,
    is_timezone_aware_iso,
    is_truthy,
    now_iso,
    pdf_envelope_errors,
    read_csv,
    read_json,
    relative_to_run,
    resolve_run_evidence_ref,
    sha256_file,
    write_csv,
    write_json,
)
from paper import (
    ACTIVE_FIGURE_STATUSES,
    CLAIM_MAP_FIELDS,
    FIGURE_TABLE_FIELDS,
    PLACEHOLDERS,
    _safe_section_path,
    _section_order,
    resolve_planned_section,
)
from v51_common import resolve_run_flexible

ABSTRACT_TEMPLATE = """# 摘要

本文围绕题目提出的各子问题，基于冻结后的模型、实验和证据进行概括。

{body}

**关键词：** {keywords}
"""

EQUATION_FIELDS = ["equation_id", "module_id", "latex", "meaning", "section", "source_ref", "status", "notes"]
CITATION_FIELDS = ["citation_id", "source_id", "claim_or_context", "paper_location", "verified", "verified_by", "notes"]
SECTION_FIELDS = ["section_id", "purpose", "required_claims", "required_equations", "required_figures", "status", "editor", "notes"]
SECTION_STATUSES = {"planned", "drafting", "drafted", "reviewed", "final"}
COMPILE_RECEIPT_RELATIVE = "deliverables/PAPER_COMPILE_RECEIPT.json"


def _paper_schema_errors(run: Path) -> list[str]:
    """Run the registered paper-extension schemas before semantic inspection."""
    try:
        from schema_validate import validate_run as validate_run_schemas

        result = validate_run_schemas(run, stage="paper_extension")
    except (Exception, SystemExit) as exc:
        return [f"paper schema validator exception: {exc}"]
    return [f"schema: {item}" for item in result.get("errors", [])]


def _freeze_validation_errors(run: Path) -> list[str]:
    """Validate both the freeze contract and the complete current freeze scope."""
    errors: list[str] = []
    try:
        from harness import verify_freeze

        result = verify_freeze(run, quiet=True)
        errors.extend(str(item) for item in result.get("issues", []))
    except (Exception, SystemExit) as exc:
        errors.append(f"freeze verifier exception: {exc}")
    freeze_path = run / "FROZEN_MANIFEST.json"
    if freeze_path.is_file():
        try:
            from schema_validate import validate_json_file

            schema_errors = validate_json_file(
                freeze_path, ROOT / "schemas" / "frozen_manifest.schema.json"
            )
            errors.extend(f"freeze schema: {item}" for item in schema_errors)
        except (Exception, SystemExit) as exc:
            errors.append(f"freeze schema validator exception: {exc}")
    return list(dict.fromkeys(errors))


def _paper_claim_context(
    run: Path,
) -> tuple[
    dict[str, dict[str, Any]],
    set[str],
    dict[str, set[str]],
    dict[str, list[dict[str, str]]],
    list[str],
]:
    """Return a validated plan/claim-map projection used by abstract checks."""
    errors: list[str] = []
    plan_by_id: dict[str, dict[str, Any]] = {}
    included_ids: set[str] = set()
    plan_claim_targets: dict[str, set[str]] = {}
    try:
        plan = read_json(run / "paper" / "PAPER_PLAN.json", expect=dict)
    except SystemExit as exc:
        return {}, set(), {}, {}, [f"PAPER_PLAN.json invalid: {exc}"]
    sections = plan.get("sections", [])
    if not isinstance(sections, list):
        return {}, set(), {}, {}, ["PAPER_PLAN.json sections must be an array"]
    seen_orders: dict[int, str] = {}
    seen_paths: dict[str, str] = {}
    for index, section in enumerate(sections, start=1):
        if not isinstance(section, dict):
            errors.append(f"PAPER_PLAN section #{index} must be an object")
            continue
        sid = str(section.get("id", "")).strip()
        if not sid:
            errors.append(f"PAPER_PLAN section #{index} lacks id")
            continue
        if sid in plan_by_id:
            errors.append(f"PAPER_PLAN section id is duplicated: {sid}")
            continue
        plan_by_id[sid] = section
        try:
            order = _section_order(section)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            if order in seen_orders:
                errors.append(
                    f"PAPER_PLAN section order is duplicated: {order} "
                    f"({seen_orders[order]}, {sid})"
                )
            seen_orders[order] = sid
        try:
            section_path = _safe_section_path(run, section)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            key = str(section_path.resolve()).casefold()
            if key in seen_paths:
                errors.append(
                    "PAPER_PLAN section file is duplicated: "
                    f"{relative_to_run(section_path, run)} ({seen_paths[key]}, {sid})"
                )
            seen_paths[key] = sid
        if section.get("include_in_manuscript", True):
            included_ids.add(sid)
        section_claim_ids = _string_list(section.get("claim_ids"))
        duplicate_claim_ids = sorted(
            {item for item in section_claim_ids if section_claim_ids.count(item) > 1}
        )
        if duplicate_claim_ids:
            errors.append(
                f"PAPER_PLAN section {sid} has duplicate claim_ids: "
                + ", ".join(duplicate_claim_ids)
            )
        for claim_id in section_claim_ids:
            plan_claim_targets.setdefault(claim_id, set()).add(sid)

    claim_map_by_id: dict[str, list[dict[str, str]]] = {}
    claim_map_path = run / "paper" / "PAPER_CLAIM_MAP.csv"
    if not claim_map_path.is_file():
        errors.append("paper/PAPER_CLAIM_MAP.csv is missing")
    else:
        try:
            rows = read_csv(claim_map_path, expected_fields=CLAIM_MAP_FIELDS)
        except SystemExit as exc:
            errors.append(f"PAPER_CLAIM_MAP.csv invalid: {exc}")
            rows = []
        for index, row in enumerate(rows, start=1):
            claim_id = str(row.get("claim_id", "")).strip()
            if not claim_id:
                errors.append(f"PAPER_CLAIM_MAP row {index} lacks claim_id")
                continue
            claim_map_by_id.setdefault(claim_id, []).append(row)
    for claim_id, mappings in sorted(claim_map_by_id.items()):
        if len(mappings) != 1:
            errors.append(f"PAPER_CLAIM_MAP claim_id is duplicated: {claim_id}")
            continue
        target_section = str(mappings[0].get("target_section", "")).strip()
        if not target_section:
            if is_truthy(mappings[0].get("is_core")):
                errors.append(f"Core PAPER_CLAIM_MAP claim {claim_id} has no target_section")
            continue
        if target_section not in included_ids:
            errors.append(
                f"PAPER_CLAIM_MAP claim {claim_id} targets an unknown or excluded section: "
                f"{target_section or '<empty>'}"
            )
        planned_targets = plan_claim_targets.get(claim_id, set())
        if planned_targets != {target_section}:
            errors.append(
                f"PAPER_CLAIM_MAP claim {claim_id} disagrees with PAPER_PLAN claim_ids: "
                f"map={target_section or '<empty>'}, plan={sorted(planned_targets)}"
            )
    for claim_id, targets in sorted(plan_claim_targets.items()):
        mappings = claim_map_by_id.get(claim_id, [])
        if len(mappings) != 1:
            errors.append(
                f"PAPER_PLAN claim {claim_id} must resolve exactly once in PAPER_CLAIM_MAP"
            )
        elif str(mappings[0].get("target_section", "")).strip() not in targets:
            errors.append(
                f"PAPER_PLAN claim {claim_id} target disagrees with PAPER_CLAIM_MAP: "
                f"plan={sorted(targets)}, map={str(mappings[0].get('target_section', '')).strip() or '<empty>'}"
            )
        if len(targets) != 1:
            errors.append(
                f"PAPER_PLAN claim {claim_id} is assigned to multiple sections: {sorted(targets)}"
            )
    return plan_by_id, included_ids, plan_claim_targets, claim_map_by_id, errors


def _paths(run: Path) -> dict[str, Path]:
    root = run / "paper"
    return {
        "root": root,
        "facts": root / "ABSTRACT_FACTS.json",
        "equations": root / "EQUATION_REGISTER.csv",
        "citations": root / "CITATION_REGISTER.csv",
        "sections": root / "SECTION_QUALITY_REGISTER.csv",
        "style": root / "TECHNICAL_STYLE_GUIDE.md",
    }


def ensure(run: Path) -> dict[str, Path]:
    p = _paths(run)
    p["root"].mkdir(parents=True, exist_ok=True)
    (p["root"] / "sections").mkdir(parents=True, exist_ok=True)
    if not p["facts"].exists():
        write_json(p["facts"], {"schema_version": 1, "keywords": [], "facts": []})
    for key, fields in (("equations", EQUATION_FIELDS), ("citations", CITATION_FIELDS), ("sections", SECTION_FIELDS)):
        if not p[key].exists():
            write_csv(p[key], fields, [])
    if not p["style"].exists():
        p["style"].write_text(
            "# Technical Style Guide\n\n"
            "- Write the model, evidence and scope; do not narrate Agent history.\n"
            "- Every strong numerical adjective needs an evidence reference.\n"
            "- Keep one symbol for one meaning and include units.\n"
            "- State whether an optimum/bound is rigorous, certified, or merely numerical.\n"
            "- The abstract is written last from frozen facts.\n",
            encoding="utf-8",
        )
    plan_path = p["root"] / "PAPER_PLAN.json"
    if plan_path.is_file():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            plan = {}
        sections = plan.get("sections", []) if isinstance(plan, dict) else []
        if isinstance(sections, list):
            rows = read_csv(p["sections"], expected_fields=SECTION_FIELDS)
            known = {str(row.get("section_id", "")).strip() for row in rows}
            changed = False
            for section in sections:
                if not isinstance(section, dict):
                    continue
                section_id = str(section.get("id", "")).strip()
                if not section_id or section_id in known:
                    continue
                rows.append({
                    "section_id": section_id,
                    "purpose": str(section.get("purpose", "")).strip(),
                    "required_claims": ";".join(_string_list(section.get("claim_ids"))),
                    "required_equations": ";".join(
                        str(row.get("equation_id", "")).strip()
                        for row in read_csv(p["equations"], expected_fields=EQUATION_FIELDS)
                        if str(row.get("section", "")).strip() == section_id
                        and str(row.get("equation_id", "")).strip()
                    ),
                    "required_figures": ";".join(
                        _string_list(section.get("figure_ids"))
                        + _string_list(section.get("table_ids"))
                    ),
                    "status": str(section.get("status", "planned")).strip() or "planned",
                    "editor": str(section.get("writer", "")).strip(),
                    "notes": "",
                })
                known.add(section_id)
                changed = True
            if changed:
                write_csv(p["sections"], SECTION_FIELDS, rows)
    return p


def cmd_init(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    p = ensure(run)
    print(f"Initialized paper v5 extension at {p['root'].relative_to(run)}")
    return 0


def cmd_fact_add(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    p = ensure(run)
    payload = json.loads(p["facts"].read_text(encoding="utf-8-sig"))
    facts = payload.setdefault("facts", [])
    if any(str(x.get("fact_id")) == args.fact_id for x in facts):
        die(f"fact_id 已存在：{args.fact_id}")
    evidence_ids = [str(item).strip() for item in args.evidence if str(item).strip()]
    ledger_rows = read_csv(run / "evidence" / "CLAIM_EVIDENCE_LEDGER.csv")
    evidence_claim_ids = {
        str(row.get("claim_id", "")).strip()
        for row in ledger_rows
        if str(row.get("evidence_id", "")).strip() in evidence_ids
        and str(row.get("claim_id", "")).strip()
    }
    requested_claim_ids = {
        str(item).strip()
        for item in getattr(args, "claim", [])
        if str(item).strip()
    }
    if requested_claim_ids and requested_claim_ids != evidence_claim_ids:
        die(
            "摘要事实 claim_ids 必须与 evidence_ids 在 CLAIM_EVIDENCE_LEDGER.csv 中的 claim_id 完全一致："
            f"requested={sorted(requested_claim_ids)}, evidence={sorted(evidence_claim_ids)}"
        )
    claim_ids = sorted(requested_claim_ids or evidence_claim_ids)
    if not claim_ids:
        die("摘要事实必须通过 evidence_ids 解析到至少一个 claim_id。")
    if args.frozen and not is_named_human_identity(args.verified_by):
        die(
            "--frozen 摘要事实必须提供具名 --verified-by，"
            "不能使用 TEAM/UNKNOWN/TODO 等占位值。"
        )
    keywords = sorted(
        {
            str(item).strip()
            for item in (_string_list(payload.get("keywords")) + list(args.keyword or []))
            if str(item).strip()
        }
    )
    if not keywords:
        die("摘要事实必须至少提供一个经审核关键词；首次 fact-add 请使用 --keyword。")
    facts.append({
        "fact_id": args.fact_id, "question": args.question, "purpose": args.purpose,
        "method": args.method, "result": args.result, "scope": args.scope,
        "claim_ids": claim_ids, "evidence_ids": evidence_ids, "limitations": args.limitations,
        "frozen": bool(args.frozen), "verified_by": args.verified_by or "",
        "created_at": now_iso(),
    })
    payload["keywords"] = keywords
    write_json(p["facts"], payload)
    print(f"Added abstract fact {args.fact_id}")
    return 0


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _usable_facts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    facts = payload.get("facts", [])
    if not isinstance(facts, list):
        return []
    return [
        fact
        for fact in facts
        if isinstance(fact, dict)
        and is_truthy(fact.get("frozen"))
        and _string_list(fact.get("claim_ids"))
        and _string_list(fact.get("evidence_ids"))
        and is_named_human_identity(fact.get("verified_by", ""))
        and is_timezone_aware_iso(fact.get("created_at", ""))
    ]


def _render_abstract(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    usable = _usable_facts(payload)
    paragraphs = []
    for fact in usable:
        pieces = [str(fact.get("question", "")).strip()]
        if fact.get("purpose"):
            pieces.append(str(fact["purpose"]).strip())
        if fact.get("method"):
            pieces.append("采用" + str(fact["method"]).strip())
        if fact.get("result"):
            pieces.append("得到" + str(fact["result"]).strip())
        if fact.get("scope"):
            pieces.append("该结论适用于" + str(fact["scope"]).strip())
        limitations = _string_list(fact.get("limitations"))
        if limitations:
            pieces.append("局限为" + "；".join(limitations))
        paragraphs.append("，".join(piece.rstrip("。") for piece in pieces if piece) + "。")
    keywords = "；".join(_string_list(payload.get("keywords")))
    return ABSTRACT_TEMPLATE.format(body="\n\n".join(paragraphs), keywords=keywords), usable


def _audit_facts(
    run: Path,
    payload: Any,
    *,
    strict: bool,
) -> tuple[list[str], list[str], list[dict[str, Any]], list[dict[str, str]]]:
    errors: list[str] = []
    warnings: list[str] = []
    resolved_evidence: list[dict[str, str]] = []
    if not isinstance(payload, dict):
        return ["ABSTRACT_FACTS.json 顶层必须是对象"], warnings, [], resolved_evidence
    facts = payload.get("facts", [])
    if not isinstance(facts, list):
        return ["ABSTRACT_FACTS.json facts 必须是数组"], warnings, [], resolved_evidence
    keywords = _string_list(payload.get("keywords"))
    if not keywords:
        (errors if strict else warnings).append(
            "ABSTRACT_FACTS.json must contain at least one reviewed keyword; defaults are not invented"
        )
    freeze_errors = _freeze_validation_errors(run)
    if freeze_errors:
        (errors if strict or any(
            isinstance(item, dict) and is_truthy(item.get("frozen")) for item in facts
        ) else warnings).extend(f"freeze: {item}" for item in freeze_errors)
    (
        plan_by_id,
        included_section_ids,
        plan_claim_targets,
        claim_map_by_id,
        claim_context_errors,
    ) = _paper_claim_context(run)
    (errors if strict else warnings).extend(claim_context_errors)
    ledger_rows = read_csv(run / "evidence" / "CLAIM_EVIDENCE_LEDGER.csv")
    ledger_by_evidence: dict[str, list[dict[str, str]]] = {}
    known_claim_ids: set[str] = set()
    for row in ledger_rows:
        claim_id = str(row.get("claim_id", "")).strip()
        evidence_id = str(row.get("evidence_id", "")).strip()
        if claim_id:
            known_claim_ids.add(claim_id)
        if evidence_id:
            ledger_by_evidence.setdefault(evidence_id, []).append(row)
    freeze_path = run / "FROZEN_MANIFEST.json"
    frozen_files: dict[str, str] = {}
    if freeze_path.is_file():
        try:
            freeze = json.loads(freeze_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(f"FROZEN_MANIFEST.json 无效：{exc}")
        else:
            raw_files = freeze.get("files", []) if isinstance(freeze, dict) else []
            if not isinstance(raw_files, list):
                errors.append("FROZEN_MANIFEST.json files must be an array")
                raw_files = []
            if isinstance(freeze, dict):
                for item in raw_files:
                    if isinstance(item, dict) and item.get("path") and item.get("sha256"):
                        frozen_files[str(item["path"])] = str(item["sha256"])

    seen_fact_ids: set[str] = set()
    for index, fact in enumerate(facts, 1):
        if not isinstance(fact, dict):
            errors.append(f"Abstract fact #{index} 必须是对象")
            continue
        raw_fact_id = str(fact.get("fact_id", "")).strip()
        fact_id = raw_fact_id or f"#{index}"
        if not raw_fact_id:
            errors.append(f"Abstract fact #{index} lacks fact_id")
        if fact_id in seen_fact_ids:
            errors.append(f"Abstract fact_id 重复：{fact_id}")
        seen_fact_ids.add(fact_id)
        for field in ("question", "purpose", "method", "result", "scope"):
            if not str(fact.get(field, "")).strip():
                errors.append(f"Abstract fact {fact_id} lacks {field}")
        evidence_ids = _string_list(fact.get("evidence_ids"))
        claim_ids = _string_list(fact.get("claim_ids"))
        if strict and not claim_ids:
            errors.append(f"Abstract fact {fact_id} lacks claim_ids")
        unknown_claims = sorted(set(claim_ids) - known_claim_ids)
        if unknown_claims:
            (errors if strict else warnings).append(
                f"Abstract fact {fact_id} references unknown claim_ids: {', '.join(unknown_claims)}"
            )
        for claim_id in sorted(set(claim_ids)):
            mappings = claim_map_by_id.get(claim_id, [])
            target = errors if strict else warnings
            if len(mappings) != 1:
                target.append(
                    f"Abstract fact {fact_id} claim {claim_id} must resolve exactly once "
                    "in PAPER_CLAIM_MAP.csv"
                )
                continue
            target_section = str(mappings[0].get("target_section", "")).strip()
            if target_section not in included_section_ids:
                target.append(
                    f"Abstract fact {fact_id} claim {claim_id} targets an unknown or "
                    f"excluded section: {target_section or '<empty>'}"
                )
                continue
            if target_section not in plan_claim_targets.get(claim_id, set()):
                target.append(
                    f"Abstract fact {fact_id} claim {claim_id} is not listed in "
                    f"PAPER_PLAN section {target_section}"
                )
            section = plan_by_id.get(target_section)
            if section is not None:
                try:
                    section_path = _safe_section_path(run, section)
                    section_text = section_path.read_text(encoding="utf-8-sig")
                except (OSError, UnicodeError, ValueError) as exc:
                    target.append(
                        f"Abstract fact {fact_id} claim {claim_id} body section cannot be read: {exc}"
                    )
                else:
                    if f"<!-- CLAIM:{claim_id} -->" not in section_text:
                        target.append(
                            f"Abstract fact {fact_id} claim {claim_id} lacks a trace tag "
                            f"in PAPER_PLAN section {target_section}"
                        )
        if is_truthy(fact.get("frozen")) and not evidence_ids:
            errors.append(f"Frozen abstract fact {fact_id} lacks evidence_ids")
        if is_truthy(fact.get("frozen")) and not is_named_human_identity(
            fact.get("verified_by", "")
        ):
            errors.append(
                f"Frozen abstract fact {fact_id} requires a named verified_by, "
                "not TEAM/UNKNOWN/TODO or another placeholder"
            )
        if not is_timezone_aware_iso(fact.get("created_at", "")):
            errors.append(
                f"Abstract fact {fact_id} created_at must be timezone-aware ISO 8601"
            )
        evidence_claim_ids: set[str] = set()
        for evidence_id in sorted(set(evidence_ids)):
            for item in ledger_by_evidence.get(evidence_id, []):
                owner_claim_id = str(item.get("claim_id", "")).strip()
                if owner_claim_id:
                    evidence_claim_ids.add(owner_claim_id)
            resolved, evidence_errors = resolve_run_evidence_ref(run, evidence_id)
            if evidence_errors:
                target = errors if strict else warnings
                target.extend(
                    f"Abstract fact {fact_id} evidence {evidence_id}: {item}"
                    for item in evidence_errors
                )
                continue
            if is_truthy(fact.get("frozen")):
                for item in resolved:
                    expected_hash = frozen_files.get(item["path"])
                    if not expected_hash or expected_hash != item["sha256"]:
                        errors.append(
                            f"Abstract fact {fact_id} evidence {evidence_id} is not bound "
                            "to the current frozen manifest"
                        )
            resolved_evidence.extend(resolved)
        if claim_ids and set(claim_ids) != evidence_claim_ids:
            errors.append(
                f"Abstract fact {fact_id} claim_ids do not match evidence ownership: "
                f"fact={sorted(set(claim_ids))}, evidence={sorted(evidence_claim_ids)}"
            )
        serialized = json.dumps(fact, ensure_ascii=False)
        placeholders = [token for token in PLACEHOLDERS if token in serialized]
        if placeholders:
            (errors if strict else warnings).append(
                f"Abstract fact {fact_id} contains placeholders: {sorted(set(placeholders))}"
            )

    usable = _usable_facts(payload)
    if not usable:
        target = errors if strict else warnings
        target.append("没有可用于摘要的冻结、已验证且有证据的事实")
    resolved_evidence = sorted(
        {item["path"]: item for item in resolved_evidence}.values(),
        key=lambda item: item["path"],
    )
    return errors, warnings, usable, resolved_evidence


def cmd_build_abstract(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    p = ensure(run)
    schema_errors = _paper_schema_errors(run)
    if schema_errors:
        die("论文扩展 schema 未通过：" + "; ".join(schema_errors))
    try:
        _, abstract_path = resolve_planned_section(run, "abstract", require_in_manuscript=True)
    except ValueError as exc:
        die(str(exc))
    payload = json.loads(p["facts"].read_text(encoding="utf-8-sig"))
    errors, _, usable, _ = _audit_facts(run, payload, strict=True)
    if errors:
        die("摘要事实未通过校验：" + "; ".join(errors))
    text, _ = _render_abstract(payload)
    atomic_write_text(abstract_path, text)
    print(f"Built abstract from {len(usable)} frozen facts: {relative_to_run(abstract_path, run)}")
    return 0


def _add_csv(run: Path, key: str, fields: list[str], row: dict[str, str], id_field: str) -> None:
    p = ensure(run)
    path = p[key]
    rows = read_csv(path)
    if any(r.get(id_field) == row[id_field] for r in rows):
        die(f"{id_field} 已存在：{row[id_field]}")
    rows.append(row)
    write_csv(path, fields, rows)


def cmd_equation_add(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    _add_csv(run, "equations", EQUATION_FIELDS, {
        "equation_id": args.equation_id, "module_id": args.module_id, "latex": args.latex,
        "meaning": args.meaning, "section": args.section, "source_ref": args.source_ref,
        "status": args.status, "notes": args.notes or "",
    }, "equation_id")
    print(f"Added paper equation {args.equation_id}")
    return 0


def cmd_citation_add(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    if args.verified and not is_named_human_identity(args.verified_by):
        die(
            "已核验 citation 必须提供具名 --verified-by，"
            "不能使用 TEAM/UNKNOWN/TODO 等占位值。"
        )
    _add_csv(run, "citations", CITATION_FIELDS, {
        "citation_id": args.citation_id, "source_id": args.source_id,
        "claim_or_context": args.claim_or_context, "paper_location": args.paper_location,
        "verified": str(bool(args.verified)).lower(), "verified_by": args.verified_by or "",
        "notes": args.notes or "",
    }, "citation_id")
    print(f"Added citation mapping {args.citation_id}")
    return 0


def cmd_section_quality_set(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    if args.status in {"reviewed", "final"} and not is_named_human_identity(args.editor):
        die(
            "reviewed/final section 必须提供具名 --editor，"
            "不能使用 TEAM/UNKNOWN/TODO 等占位值。"
        )
    p = ensure(run)
    plan = read_json(run / "paper" / "PAPER_PLAN.json", expect=dict)
    plan_section = next(
        (
            item for item in plan.get("sections", [])
            if isinstance(item, dict) and str(item.get("id", "")) == args.section
        ),
        None,
    )
    if plan_section is None:
        die(f"PAPER_PLAN 中没有 section：{args.section}")
    rows = read_csv(p["sections"], expected_fields=SECTION_FIELDS)
    row = next((item for item in rows if item.get("section_id") == args.section), None)
    if row is None:
        die(f"SECTION_QUALITY_REGISTER 中没有 PAPER_PLAN section：{args.section}")
    row["status"] = args.status
    row["editor"] = args.editor
    if args.purpose is not None:
        row["purpose"] = args.purpose
    if args.claim is not None:
        row["required_claims"] = ";".join(args.claim)
    else:
        row["required_claims"] = ";".join(_string_list(plan_section.get("claim_ids")))
    if args.equation is not None:
        row["required_equations"] = ";".join(args.equation)
    else:
        row["required_equations"] = ";".join(
            str(item.get("equation_id", "")).strip()
            for item in read_csv(p["equations"], expected_fields=EQUATION_FIELDS)
            if str(item.get("section", "")).strip() == args.section
            and str(item.get("equation_id", "")).strip()
        )
    if args.figure is not None:
        row["required_figures"] = ";".join(args.figure)
    else:
        row["required_figures"] = ";".join(
            _string_list(plan_section.get("figure_ids"))
            + _string_list(plan_section.get("table_ids"))
        )
    if args.note is not None:
        row["notes"] = args.note
    plan_section["status"] = args.status
    plan_section["writer"] = args.editor
    plan["updated_at"] = now_iso()
    write_csv(p["sections"], SECTION_FIELDS, rows)
    write_json(run / "paper" / "PAPER_PLAN.json", plan)
    print(f"Updated section quality {args.section}: {args.status}")
    return 0


def _source_index(run: Path) -> tuple[dict[str, list[dict[str, str]]], dict[str, list[dict[str, str]]]]:
    by_id: dict[str, list[dict[str, str]]] = {}
    by_url: dict[str, list[dict[str, str]]] = {}
    for row in read_csv(run / "sources" / "SOURCE_LEDGER.csv"):
        source_id = str(row.get("source_id", "")).strip()
        url = str(row.get("url", "")).strip()
        if source_id:
            by_id.setdefault(source_id, []).append(row)
        if url:
            by_url.setdefault(url, []).append(row)
    return by_id, by_url


def _source_row_errors(row: dict[str, str], reference: str) -> list[str]:
    errors: list[str] = []
    for field in ("title", "author_or_org", "source_type", "accessed_at", "verified_by"):
        if not str(row.get(field, "")).strip():
            errors.append(f"source {reference} lacks {field}")
    if not is_named_human_identity(row.get("verified_by", "")):
        errors.append(
            f"source {reference} verified_by must name a human, not a placeholder"
        )
    if not is_timezone_aware_iso(row.get("accessed_at", "")):
        errors.append(
            f"source {reference} accessed_at must be timezone-aware ISO 8601"
        )
    url = str(row.get("url", "")).strip()
    if not url.startswith(("http://", "https://")):
        errors.append(f"source {reference} has no valid http(s) URL")
    return errors


def _resolve_paper_source(
    run: Path,
    reference: str,
    source_by_id: dict[str, list[dict[str, str]]],
    source_by_url: dict[str, list[dict[str, str]]],
) -> tuple[list[dict[str, str]], list[str]]:
    ref = reference.strip()
    source_rows = source_by_url.get(ref, []) if ref.startswith(("http://", "https://")) else source_by_id.get(ref, [])
    if source_rows:
        if len(source_rows) != 1:
            return [], [f"source reference is not unique: {ref}"]
        return [], _source_row_errors(source_rows[0], ref)
    if ref.startswith(("http://", "https://")) or re.fullmatch(r"SRC[-_].+", ref, re.IGNORECASE):
        return [], [f"source reference is not registered in SOURCE_LEDGER.csv: {ref}"]
    evidence_rows = [
        row
        for row in read_csv(run / "evidence" / "CLAIM_EVIDENCE_LEDGER.csv")
        if str(row.get("evidence_id", "")).strip() == ref
    ]
    if not evidence_rows:
        return [], [
            "source_ref must be a registered source_id/URL or evidence_id; "
            f"direct run paths are not accepted: {ref}"
        ]
    return resolve_run_evidence_ref(run, ref)


def validate_paper_extension(run: Path, *, strict: bool = False) -> dict[str, Any]:
    """Validate the facts-first paper extension without invoking the CLI."""
    p = _paths(run)
    errors: list[str] = _paper_schema_errors(run)
    warnings: list[str] = []
    if errors:
        return {
            "generated_at": now_iso(),
            "abstract_path": "",
            "abstract_sha256": "",
            "usable_fact_ids": [],
            "resolved_evidence": [],
            "errors": errors,
            "warnings": warnings,
            "passed": False,
        }
    for key in ("facts", "equations", "citations", "sections", "style"):
        if not p[key].exists() or not p[key].is_file():
            (errors if strict else warnings).append(f"Paper v5 artifact missing: {relative_to_run(p[key], run)}")
    payload: Any = {}
    if p["facts"].is_file():
        try:
            payload = json.loads(p["facts"].read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(f"ABSTRACT_FACTS.json 无效：{exc}")
    fact_errors, fact_warnings, usable, resolved_evidence = _audit_facts(
        run, payload, strict=strict
    )
    errors.extend(fact_errors)
    warnings.extend(fact_warnings)
    abstract_path: Path | None = None
    (
        plan_by_id,
        included_section_ids,
        _plan_claim_targets,
        _claim_map_by_id,
        claim_context_errors,
    ) = _paper_claim_context(run)
    for item in claim_context_errors:
        target = errors if strict else warnings
        if item not in target:
            target.append(item)
    try:
        _, abstract_path = resolve_planned_section(run, "abstract", require_in_manuscript=True)
    except ValueError as exc:
        (errors if strict else warnings).append(str(exc))
    expected_abstract, _ = _render_abstract(payload) if isinstance(payload, dict) else ("", [])
    if abstract_path is not None:
        if not abstract_path.exists() or not abstract_path.is_file():
            (errors if strict else warnings).append(
                f"Compiled abstract section missing: {relative_to_run(abstract_path, run)}"
            )
        else:
            placeholders = contains_placeholder(abstract_path, PLACEHOLDERS)
            if placeholders:
                (errors if strict else warnings).append(
                    f"Compiled abstract contains placeholders: {sorted(set(placeholders))}"
                )
            if usable and abstract_path.read_text(encoding="utf-8-sig") != expected_abstract:
                (errors if strict else warnings).append(
                    "Compiled abstract is stale or was edited outside ABSTRACT_FACTS.json; rerun paper_v5.py build-abstract"
                )
    source_by_id, source_by_url = _source_index(run)
    for source_id, rows in source_by_id.items():
        if len(rows) > 1:
            errors.append(f"Duplicate SOURCE_LEDGER source_id: {source_id}")
    citations = read_csv(p["citations"], expected_fields=CITATION_FIELDS) if p["citations"].is_file() else []
    seen_citations: set[str] = set()
    for index, row in enumerate(citations, start=1):
        raw_citation_id = str(row.get("citation_id", "")).strip()
        citation_id = raw_citation_id or f"row-{index}"
        if not raw_citation_id:
            errors.append(f"Citation row {index} lacks citation_id")
        if citation_id in seen_citations:
            errors.append(f"Duplicate citation_id: {citation_id}")
        seen_citations.add(citation_id)
        if not is_truthy(row.get("verified")):
            (errors if strict else warnings).append(f"Unverified citation: {citation_id}")
        if is_truthy(row.get("verified")) and not is_named_human_identity(
            row.get("verified_by", "")
        ):
            (errors if strict else warnings).append(
                f"Citation {citation_id} verified_by must name a human, not a placeholder"
            )
        for field in ("source_id", "claim_or_context", "paper_location"):
            if not str(row.get(field, "")).strip():
                errors.append(f"Citation {citation_id} lacks {field}")
        paper_location = str(row.get("paper_location", "")).strip()
        if paper_location and paper_location not in included_section_ids:
            (errors if strict else warnings).append(
                f"Citation {citation_id} paper_location must exactly name an included PAPER_PLAN section: {paper_location}"
            )
        source_id = str(row.get("source_id", "")).strip()
        matches = source_by_id.get(source_id, [])
        if len(matches) != 1:
            errors.append(
                f"Citation {citation_id} source_id must resolve exactly once in SOURCE_LEDGER.csv: {source_id!r}"
            )
        else:
            errors.extend(f"Citation {citation_id}: {item}" for item in _source_row_errors(matches[0], source_id))
    equations = read_csv(p["equations"], expected_fields=EQUATION_FIELDS) if p["equations"].is_file() else []
    module_ids: set[str] = set()
    module_equations: dict[tuple[str, str], list[dict[str, str]]] = {}
    modules_root = run / "research" / "modules"
    if modules_root.is_dir():
        for spec_path in modules_root.glob("*/MODULE_SPEC.json"):
            try:
                spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(spec, dict) and str(spec.get("module_id", "")).strip() == spec_path.parent.name:
                module_ids.add(spec_path.parent.name)
                for math_row in read_csv(spec_path.parent / "EQUATION_REGISTRY.csv"):
                    math_equation_id = str(math_row.get("equation_id", "")).strip()
                    if math_equation_id:
                        module_equations.setdefault(
                            (spec_path.parent.name, math_equation_id), []
                        ).append(math_row)
    seen_equations: set[str] = set()
    equation_ids_by_section: dict[str, set[str]] = {}
    for index, row in enumerate(equations, start=1):
        raw_equation_id = str(row.get("equation_id", "")).strip()
        equation_id = raw_equation_id or f"row-{index}"
        if not raw_equation_id:
            errors.append(f"Paper equation row {index} lacks equation_id")
        if equation_id in seen_equations:
            errors.append(f"Duplicate paper equation_id: {equation_id}")
        seen_equations.add(equation_id)
        for field in ("module_id", "latex", "meaning", "section"):
            if not str(row.get(field, "")).strip():
                errors.append(f"Paper equation {equation_id} lacks {field}")
        module_id = str(row.get("module_id", "")).strip()
        if module_id and module_id not in module_ids:
            (errors if strict else warnings).append(
                f"Paper equation {equation_id} references unknown module_id: {module_id}"
            )
        math_matches = module_equations.get((module_id, equation_id), [])
        if module_id and len(math_matches) != 1:
            (errors if strict else warnings).append(
                f"Paper equation {equation_id} must resolve exactly once in {module_id}/EQUATION_REGISTRY.csv"
            )
        elif math_matches:
            math_row = math_matches[0]
            if str(math_row.get("latex", "")).strip() != str(row.get("latex", "")).strip():
                errors.append(f"Paper equation {equation_id} LaTeX differs from the mathematical registry")
            if str(math_row.get("status", "")).strip().lower() != "checked":
                errors.append(f"Paper equation {equation_id} mathematical source is not checked")
            units_check = str(math_row.get("units_check", "")).strip().lower()
            if units_check not in {"pass", "not_applicable"}:
                errors.append(
                    f"Paper equation {equation_id} mathematical source has unresolved units_check="
                    f"{units_check or 'missing'}"
                )
        section_id = str(row.get("section", "")).strip()
        if section_id and raw_equation_id:
            equation_ids_by_section.setdefault(section_id, set()).add(raw_equation_id)
        if section_id and section_id not in included_section_ids:
            (errors if strict else warnings).append(
                f"Paper equation {equation_id} references unknown or excluded PAPER_PLAN section: {section_id}"
            )
        status = str(row.get("status", "")).strip().lower()
        if status not in {"draft", "checked", "final"}:
            errors.append(f"Paper equation {equation_id} has invalid status: {status!r}")
        elif strict and status != "final":
            errors.append(f"Paper equation {equation_id} is not final (status={status})")
        raw_refs = str(row.get("source_ref", "")).strip()
        refs = [item.strip() for item in re.split(r"[;,|]", raw_refs) if item.strip()]
        if not refs:
            (errors if strict else warnings).append(f"Paper equation {equation_id} lacks source_ref")
        for ref in refs:
            resolved, source_errors = _resolve_paper_source(
                run, ref, source_by_id, source_by_url
            )
            resolved_evidence.extend(resolved)
            (errors if strict else warnings).extend(
                f"Paper equation {equation_id}: {item}" for item in source_errors
            )
    section_rows = read_csv(p["sections"], expected_fields=SECTION_FIELDS) if p["sections"].is_file() else []
    claim_ids = {
        str(row.get("claim_id", "")).strip()
        for row in read_csv(run / "evidence" / "CLAIM_EVIDENCE_LEDGER.csv")
        if str(row.get("claim_id", "")).strip()
    }
    equation_ids = {
        str(row.get("equation_id", "")).strip()
        for row in equations
        if str(row.get("equation_id", "")).strip()
    }
    figure_rows = read_csv(
        run / "paper" / "FIGURE_TABLE_REGISTER.csv",
        expected_fields=FIGURE_TABLE_FIELDS,
    )
    active_figure_rows: dict[str, list[dict[str, str]]] = {}
    all_figure_ids: set[str] = set()
    for index, figure_row in enumerate(figure_rows, start=1):
        item_id = str(figure_row.get("item_id", "")).strip()
        if not item_id:
            errors.append(f"Figure/table register row {index} lacks item_id")
            continue
        if item_id in all_figure_ids:
            errors.append(f"Duplicate FIGURE_TABLE_REGISTER item_id: {item_id}")
        all_figure_ids.add(item_id)
        if str(figure_row.get("status", "")).strip().lower() in ACTIVE_FIGURE_STATUSES:
            active_figure_rows.setdefault(item_id, []).append(figure_row)

    expected_figures_by_section: dict[str, set[str]] = {}
    for sid, plan_section in plan_by_id.items():
        expected = set(
            _string_list(plan_section.get("figure_ids"))
            + _string_list(plan_section.get("table_ids"))
        )
        expected_figures_by_section[sid] = expected
        if sid not in included_section_ids:
            continue
        for item_id in sorted(expected):
            matches = active_figure_rows.get(item_id, [])
            if len(matches) != 1:
                (errors if strict else warnings).append(
                    f"PAPER_PLAN section {sid} figure/table {item_id} must resolve exactly once "
                    "to an active FIGURE_TABLE_REGISTER row"
                )
                continue
            target_section = str(matches[0].get("target_section", "")).strip()
            if target_section != sid:
                (errors if strict else warnings).append(
                    f"Figure/table {item_id} target_section differs from PAPER_PLAN: "
                    f"register={target_section or '<empty>'}, plan={sid}"
                )
    for item_id, rows in sorted(active_figure_rows.items()):
        for figure_row in rows:
            target_section = str(figure_row.get("target_section", "")).strip()
            if target_section not in included_section_ids:
                errors.append(
                    f"Active figure/table {item_id} targets an unknown or excluded section: "
                    f"{target_section or '<empty>'}"
                )
            elif item_id not in expected_figures_by_section.get(target_section, set()):
                (errors if strict else warnings).append(
                    f"Active figure/table {item_id} is not declared in PAPER_PLAN section "
                    f"{target_section}"
                )

    seen_quality_ids: set[str] = set()
    for index, row in enumerate(section_rows, start=1):
        raw_sid = str(row.get("section_id", "")).strip()
        sid = raw_sid or f"row-{index}"
        if not raw_sid:
            errors.append(f"Section quality row {index} lacks section_id")
        if sid in seen_quality_ids:
            errors.append(f"Duplicate section quality section_id: {sid}")
        seen_quality_ids.add(sid)
        if sid not in plan_by_id:
            errors.append(f"Section quality row references unknown PAPER_PLAN section: {sid}")
        status = str(row.get("status", "")).strip().lower()
        if status not in SECTION_STATUSES:
            errors.append(f"Section quality {sid} has invalid status: {status!r}")
        elif strict and sid in included_section_ids and status != "final":
            errors.append(f"Section quality {sid} is not final (status={status})")
        if not str(row.get("purpose", "")).strip():
            errors.append(f"Section quality {sid} lacks purpose")
        if (
            status in {"reviewed", "final"}
            and not is_named_human_identity(row.get("editor", ""))
        ):
            (errors if strict or sid in included_section_ids else warnings).append(
                f"Section quality {sid} editor must name a human, not a placeholder"
            )
        plan_section = plan_by_id.get(sid, {})
        plan_status = str(plan_section.get("status", "")).strip().lower()
        plan_writer = str(plan_section.get("writer", "")).strip()
        if sid in included_section_ids and strict and plan_status != "final":
            errors.append(f"PAPER_PLAN section {sid} is not final (status={plan_status or '<empty>'})")
        if sid in plan_by_id and status != plan_status:
            (errors if strict else warnings).append(
                f"Section quality {sid} status differs from PAPER_PLAN: "
                f"register={status or '<empty>'}, plan={plan_status or '<empty>'}"
            )
        editor = str(row.get("editor", "")).strip()
        if sid in included_section_ids and strict and not plan_writer:
            errors.append(f"PAPER_PLAN section {sid} lacks writer")
        if sid in plan_by_id and editor != plan_writer:
            (errors if strict else warnings).append(
                f"Section quality {sid} editor differs from PAPER_PLAN writer: "
                f"register={editor or '<empty>'}, plan={plan_writer or '<empty>'}"
            )
        planned_purpose = str(plan_section.get("purpose", "")).strip()
        registered_purpose = str(row.get("purpose", "")).strip()
        if sid in plan_by_id and registered_purpose != planned_purpose:
            (errors if strict else warnings).append(
                f"Section quality {sid} purpose differs from PAPER_PLAN"
            )
        registered: dict[str, set[str]] = {}
        for field, known in (
            ("required_claims", claim_ids),
            ("required_equations", equation_ids),
            ("required_figures", set(active_figure_rows)),
        ):
            values = [item.strip() for item in re.split(r"[;,|]", str(row.get(field, ""))) if item.strip()]
            registered[field] = set(values)
            duplicates = sorted({item for item in values if values.count(item) > 1})
            if duplicates:
                errors.append(
                    f"Section quality {sid} has duplicate {field}: {', '.join(duplicates)}"
                )
            unknown = sorted(set(values) - known)
            if unknown:
                errors.append(f"Section quality {sid} has unknown {field}: {', '.join(unknown)}")
        expected_registers = {
            "required_claims": set(_string_list(plan_section.get("claim_ids"))),
            "required_equations": equation_ids_by_section.get(sid, set()),
            "required_figures": expected_figures_by_section.get(sid, set()),
        }
        if sid in plan_by_id:
            for field, expected in expected_registers.items():
                if registered.get(field, set()) != expected:
                    (errors if strict else warnings).append(
                        f"Section quality {sid} {field} differs from canonical inputs: "
                        f"register={sorted(registered.get(field, set()))}, "
                        f"expected={sorted(expected)}"
                    )
    missing_quality = sorted(included_section_ids - seen_quality_ids)
    if strict and missing_quality:
        errors.append("Included PAPER_PLAN sections lack quality rows: " + ", ".join(missing_quality))
    resolved_evidence = sorted(
        {item["path"]: item for item in resolved_evidence}.values(),
        key=lambda item: item["path"],
    )
    result = {
        "generated_at": now_iso(),
        "abstract_path": relative_to_run(abstract_path, run) if abstract_path is not None else "",
        "abstract_sha256": sha256_file(abstract_path) if abstract_path is not None and abstract_path.is_file() else "",
        "usable_fact_ids": [str(item.get("fact_id", "")) for item in usable],
        "resolved_evidence": resolved_evidence,
        "errors": errors,
        "warnings": warnings,
        "passed": not errors,
    }
    return result


def cmd_lint(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    result = validate_paper_extension(run, strict=args.strict)
    write_json(run / "paper" / "PAPER_V5_AUDIT.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 3


def _resolve_compile_path(
    run: Path,
    value: str,
    *,
    label: str,
    must_exist: bool,
) -> Path:
    """Resolve a compiler path without permitting traversal or symlink escape."""
    run_root = run.resolve()
    raw = Path(str(value)).expanduser()
    candidate = raw if raw.is_absolute() else run_root / raw
    try:
        resolved = candidate.resolve()
        resolved.relative_to(run_root)
    except (OSError, ValueError) as exc:
        die(f"{label}必须位于当前 run 内：{value!r} ({exc})")
    if must_exist:
        if not resolved.is_file():
            die(f"{label}不是当前 run 内的现存文件：{value!r}")
        if resolved.stat().st_size <= 0:
            die(f"{label}为空文件：{value!r}")
    elif resolved.exists() and not resolved.is_file():
        die(f"{label}不能指向目录或特殊文件：{value!r}")
    return resolved


def cmd_compile(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    source = _resolve_compile_path(
        run, args.source, label="源手稿", must_exist=True
    )
    output = _resolve_compile_path(
        run, args.output, label="编译输出", must_exist=False
    )
    canonical_source = (run / "paper" / "FINAL_MANUSCRIPT.md").resolve()
    if source != canonical_source:
        die("正式编译源必须是 paper/FINAL_MANUSCRIPT.md。")
    deliverables_root = (run / "deliverables").resolve()
    try:
        output.relative_to(deliverables_root)
    except ValueError:
        die("正式编译输出必须位于当前 run 的 deliverables/ 内。")
    output_relative = relative_to_run(output, run)
    if output_relative.startswith("deliverables/packages/"):
        die("正式编译输出不能位于 deliverables/packages/；该目录只存放生成的归档包。")
    if output_relative == "deliverables/AI工具使用详情.pdf":
        die("该路径保留给具名人工复核的 AI 使用披露 PDF，不能作为论文编译输出。")
    if output.suffix.lower() != ".pdf":
        die("正式编译输出必须是 .pdf 文件。")
    if output == source or (output.exists() and source.exists() and os.path.samefile(source, output)):
        die("编译输出不能覆盖源手稿。")
    if output.exists() and not args.replace:
        die("编译输出已存在；确认覆盖时显式传入 --replace。")
    if args.dry_run:
        print(json.dumps({
            "source": relative_to_run(source, run),
            "output": relative_to_run(output, run),
            "engine": args.engine,
            "replace": bool(args.replace),
            "dry_run": True,
        }, ensure_ascii=False, indent=2))
        return 0
    if args.engine == "pandoc":
        exe = shutil.which("pandoc")
        if not exe:
            die("未找到 pandoc。")
    else:
        die(f"暂不支持编译引擎：{args.engine}")
    output.parent.mkdir(parents=True, exist_ok=True)
    version_result = subprocess.run(
        [exe, "--version"],
        cwd=run,
        text=True,
        capture_output=True,
        check=False,
    )
    if version_result.returncode:
        die("无法读取 pandoc 版本；拒绝生成无工具溯源的正式 PDF。")
    engine_version = (version_result.stdout or version_result.stderr).splitlines()
    if not engine_version or not engine_version[0].strip():
        die("pandoc 未返回可记录的版本信息。")

    fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp.pdf", dir=str(output.parent))
    os.close(fd)
    temp_output = Path(temp_name)
    logical_command = [
        "pandoc",
        relative_to_run(source, run),
        "--resource-path",
        ".",
        "-o",
        relative_to_run(output, run),
    ]
    cmd = [
        exe,
        str(source),
        "--resource-path",
        str(run),
        "-o",
        str(temp_output),
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=run,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            print(result.stdout)
            print(result.stderr)
            return result.returncode
        if not temp_output.is_file() or temp_output.stat().st_size <= 0:
            die("pandoc 返回成功但没有生成非空 PDF。")
        envelope_errors = pdf_envelope_errors(temp_output)
        if envelope_errors:
            die("pandoc 输出不是结构有效的 PDF：" + "; ".join(envelope_errors))
        receipt = {
            "schema_version": 1,
            "run_id": run.name,
            "generated_at": now_iso(),
            "engine": "pandoc",
            "engine_path": str(Path(exe).resolve()),
            "engine_version": engine_version[0].strip(),
            "source_path": relative_to_run(source, run),
            "source_sha256": sha256_file(source),
            "output_path": output_relative,
            "output_sha256": sha256_file(temp_output),
            "resource_root": ".",
            "command": logical_command,
        }
        from schema_validate import validate_instance

        receipt_schema = read_json(
            ROOT / "schemas" / "paper_compile_receipt.schema.json", expect=dict
        )
        receipt_errors = validate_instance(receipt, receipt_schema)
        if receipt_errors:
            die("编译回执 schema 无效：" + "; ".join(receipt_errors))
        os.replace(temp_output, output)
        write_json(run / COMPILE_RECEIPT_RELATIVE, receipt)
    finally:
        try:
            temp_output.unlink()
        except FileNotFoundError:
            pass
    if sha256_file(output) != receipt["output_sha256"]:
        die("编译输出在原子替换后发生哈希漂移。")
    print(f"Compiled {relative_to_run(output, run)}; receipt={COMPILE_RECEIPT_RELATIVE}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init"); p.add_argument("--run"); p.set_defaults(func=cmd_init)
    p = sub.add_parser("fact-add")
    p.add_argument("--run"); p.add_argument("--fact-id", required=True); p.add_argument("--question", required=True)
    p.add_argument("--purpose", required=True); p.add_argument("--method", required=True); p.add_argument("--result", required=True)
    p.add_argument("--scope", required=True); p.add_argument("--claim-id", dest="claim", action="append", default=[]); p.add_argument("--evidence", action="append", required=True)
    p.add_argument("--limitations", action="append", default=[]); p.add_argument("--keyword", action="append", default=[])
    p.add_argument("--frozen", action="store_true"); p.add_argument("--verified-by")
    p.set_defaults(func=cmd_fact_add)
    p = sub.add_parser("build-abstract"); p.add_argument("--run"); p.set_defaults(func=cmd_build_abstract)
    p = sub.add_parser("equation-add")
    p.add_argument("--run"); p.add_argument("--equation-id", required=True); p.add_argument("--module-id", required=True)
    p.add_argument("--latex", required=True); p.add_argument("--meaning", required=True); p.add_argument("--section", required=True)
    p.add_argument("--source-ref", required=True); p.add_argument("--status", choices=["draft", "checked", "final"], default="draft"); p.add_argument("--notes")
    p.set_defaults(func=cmd_equation_add)
    p = sub.add_parser("citation-add")
    p.add_argument("--run"); p.add_argument("--citation-id", required=True); p.add_argument("--source-id", required=True)
    p.add_argument("--claim-or-context", required=True); p.add_argument("--paper-location", required=True)
    p.add_argument("--verified", action="store_true"); p.add_argument("--verified-by"); p.add_argument("--notes")
    p.set_defaults(func=cmd_citation_add)
    p = sub.add_parser("section-quality-set")
    p.add_argument("--run"); p.add_argument("--section", required=True)
    p.add_argument("--status", choices=sorted(SECTION_STATUSES), required=True)
    p.add_argument("--editor", required=True); p.add_argument("--purpose")
    p.add_argument("--claim", action="append"); p.add_argument("--equation", action="append")
    p.add_argument("--figure", action="append"); p.add_argument("--note")
    p.set_defaults(func=cmd_section_quality_set)
    p = sub.add_parser("lint"); p.add_argument("--run"); p.add_argument("--strict", action="store_true"); p.set_defaults(func=cmd_lint)
    p = sub.add_parser("compile")
    p.add_argument("--run"); p.add_argument("--source", default="paper/FINAL_MANUSCRIPT.md"); p.add_argument("--output", default="deliverables/FINAL_MANUSCRIPT.pdf")
    p.add_argument("--engine", choices=["pandoc"], default="pandoc"); p.add_argument("--replace", action="store_true"); p.add_argument("--dry-run", action="store_true"); p.set_defaults(func=cmd_compile)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
