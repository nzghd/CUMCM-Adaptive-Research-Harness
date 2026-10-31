#!/usr/bin/env python3
"""Evidence-first paper writing workflow for CUMCM Adaptive Research Harness.

This module does not call a language model. It creates and validates the shared
paper state that Codex, Claude Code, DeepSeek, or another supported runtime can
write into. Scientific artifacts are expected to be frozen before drafting.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from common import (
    ROOT,
    atomic_write_text,
    contains_placeholder,
    is_named_human_identity,
    is_timezone_aware_iso,
    is_truthy,
    now_iso,
    read_csv,
    read_json,
    relative_to_run,
    resolve_run,
    sha256_file,
    write_csv,
    write_json,
)

PROFILE_PATH = ROOT / "config" / "paper_profile_cumcm.json"
CLAIM_MAP_FIELDS = [
    "claim_id",
    "claim_text",
    "is_core",
    "target_section",
    "evidence_status",
    "trace_tag_status",
    "notes",
]
SYMBOL_FIELDS = ["symbol", "meaning", "unit", "scope", "source_path", "verified", "notes"]
FIGURE_TABLE_FIELDS = [
    "item_id",
    "kind",
    "title",
    "path",
    "source_claims",
    "target_section",
    "status",
    "verified_by",
    "notes",
]
AUDIT_INPUT_FILES = [
    "paper/PAPER_BRIEF.md",
    "paper/PAPER_PLAN.json",
    "paper/PAPER_CLAIM_MAP.csv",
    "paper/SYMBOL_TABLE.csv",
    "paper/FIGURE_TABLE_REGISTER.csv",
    "paper/PAPER_OUTLINE.md",
    "paper/PAPER_DRAFT.md",
    "paper/CONSISTENCY_AUDIT.md",
    "paper/ABSTRACT_FACTS.json",
    "paper/EQUATION_REGISTER.csv",
    "paper/CITATION_REGISTER.csv",
    "paper/SECTION_QUALITY_REGISTER.csv",
    "paper/TECHNICAL_STYLE_GUIDE.md",
    "evidence/CLAIM_EVIDENCE_LEDGER.csv",
    "claims/STRONG_CLAIMS.csv",
    "sources/SOURCE_LEDGER.csv",
]
ACTIVE_FIGURE_STATUSES = {"used", "approved", "final"}
PLACEHOLDERS = ["TODO", "待填写", "{{", "[DRAFT_REQUIRED]"]
TRACE_RE = re.compile(r"<!--\s*CLAIM:([A-Za-z0-9_.-]+)\s*-->")
TRACE_LINE_RE = re.compile(r"^\s*<!--\s*(?:CLAIM|SOURCE|SECTION_ID):[^>]+-->\s*$", re.MULTILINE)


def _profile() -> dict[str, Any]:
    return read_json(PROFILE_PATH, expect=dict)


def _claim_groups(run: Path) -> dict[str, dict[str, Any]]:
    rows = read_csv(run / "evidence" / "CLAIM_EVIDENCE_LEDGER.csv")
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        claim_id = str(row.get("claim_id", "")).strip()
        if not claim_id:
            continue
        item = groups.setdefault(
            claim_id,
            {
                "claim_id": claim_id,
                "claim_text": str(row.get("claim_text", "")).strip(),
                "is_core": False,
                "paper_location": "",
                "rows": [],
            },
        )
        if row.get("claim_text") and not item["claim_text"]:
            item["claim_text"] = str(row.get("claim_text", "")).strip()
        item["is_core"] = bool(item["is_core"] or is_truthy(row.get("is_core")))
        if row.get("paper_location") and not item["paper_location"]:
            item["paper_location"] = str(row.get("paper_location", "")).strip()
        item["rows"].append(row)
    return groups


def _artifact_path(run: Path, value: str) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    raw = Path(text)
    # New ledgers use run-relative paths.  Keep reading legacy project-relative
    # ``runs/<id>/...`` entries, but never let either form escape this Run.
    candidates = [raw] if raw.is_absolute() else [run / raw, ROOT / raw]
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        try:
            resolved.relative_to(run.resolve())
        except ValueError:
            continue
        if resolved.is_file():
            try:
                if resolved.stat().st_size > 0:
                    return resolved
            except OSError:
                continue
    return None


def _evidence_status(run: Path, group: dict[str, Any]) -> str:
    rows = group.get("rows", [])
    evidence = [row for row in rows if row.get("evidence_id") or row.get("evidence_path")]
    if not evidence:
        return "missing"
    verified = [
        row for row in evidence
        if str(row.get("verification_status", "")) == "verified"
        and is_named_human_identity(row.get("verified_by", ""))
        and is_timezone_aware_iso(row.get("verified_at", ""))
        and _artifact_path(run, str(row.get("evidence_path", ""))) is not None
    ]
    return "verified" if verified else "recorded"


def _match_section(location: str, sections: list[dict[str, Any]]) -> str:
    text = str(location or "").strip().lower()
    if not text:
        return ""
    for section in sections:
        sid = str(section["id"])
        title = str(section["title"])
        if text == sid.lower() or text == title.lower() or sid.lower() in text or title.lower() in text:
            return sid
    return ""


def _section_filename(section: dict[str, Any]) -> str:
    return f"paper/sections/{int(section['order']):03d}_{section['id']}.md"


def _safe_section_path(run: Path, section: dict[str, Any]) -> Path:
    raw = str(section.get("file", "")).strip()
    if not raw or Path(raw).is_absolute():
        raise ValueError(f"invalid section file path: {raw!r}")
    path = (run / raw).resolve()
    base = (run / "paper" / "sections").resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"section file must stay inside paper/sections: {raw}") from exc
    if path == base or path.suffix.lower() != ".md":
        raise ValueError(f"section file must be a Markdown file inside paper/sections: {raw}")
    return path


def resolve_planned_section(
    run: Path,
    section_id: str,
    *,
    require_in_manuscript: bool = True,
) -> tuple[dict[str, Any], Path]:
    """Resolve one section through PAPER_PLAN, the manuscript path source of truth."""
    plan_path = run / "paper" / "PAPER_PLAN.json"
    if not plan_path.exists():
        raise ValueError(
            "paper/PAPER_PLAN.json 不存在；先运行 `python tools/paper.py init` 初始化论文工作区"
        )
    plan = read_json(plan_path, expect=dict)
    sections = plan.get("sections", [])
    if not isinstance(sections, list) or not sections:
        raise ValueError(
            "paper/PAPER_PLAN.json 尚未初始化 sections；先运行 `python tools/paper.py init`"
        )
    invalid_positions = [index for index, item in enumerate(sections, start=1) if not isinstance(item, dict)]
    if invalid_positions:
        raise ValueError(
            "PAPER_PLAN sections contains non-object entries at positions: "
            + ", ".join(str(item) for item in invalid_positions)
        )
    matches = [item for item in sections if str(item.get("id", "")).strip() == section_id]
    if not matches:
        raise ValueError(f"PAPER_PLAN 中不存在 section={section_id}")
    if len(matches) != 1:
        raise ValueError(f"PAPER_PLAN 中 section id 重复：{section_id}")
    section = matches[0]
    if require_in_manuscript and not section.get("include_in_manuscript", True):
        raise ValueError(f"PAPER_PLAN section={section_id} 未包含在最终正文中")
    return section, _safe_section_path(run, section)


def _section_order(section: dict[str, Any]) -> int:
    raw = section.get("order")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and re.fullmatch(r"[+-]?\d+", raw.strip()):
        return int(raw)
    raise ValueError(f"section {section.get('id', '')} 的 order 必须是整数，实际为 {raw!r}")


def _canonical_payload_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _run_local_regular_file(run: Path, value: str) -> tuple[Path | None, str]:
    """Resolve a non-empty regular file while rejecting traversal/symlink escape."""
    text = str(value or "").strip()
    if not text:
        return None, "路径为空"
    raw = Path(text)
    candidates = [raw] if raw.is_absolute() else [run / raw, ROOT / raw]
    run_root = run.resolve()
    escaped = False
    had_inside_candidate = False
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
            resolved.relative_to(run_root)
        except (OSError, ValueError):
            escaped = True
            continue
        had_inside_candidate = True
        try:
            if resolved.is_file() and resolved.stat().st_size > 0:
                return resolved, ""
        except OSError:
            continue
    if escaped and not had_inside_candidate:
        return None, f"路径不在当前 run 内或符号链接逃逸：{text}"
    return None, f"路径不是非空常规文件：{text}"


def _freeze_file_index(run: Path) -> dict[str, str]:
    path = run / "FROZEN_MANIFEST.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        return {}
    result: dict[str, str] = {}
    for item in payload["files"]:
        if not isinstance(item, dict):
            continue
        relative = str(item.get("path", "")).strip()
        digest = str(item.get("sha256", "")).strip()
        if relative and digest:
            result[relative] = digest
    return result


def _is_freeze_bound(run: Path, path: Path, freeze_files: dict[str, str]) -> bool:
    relative = relative_to_run(path, run)
    return freeze_files.get(relative) == sha256_file(path)


def _split_references(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return [item for item in re.split(r"[;,|\s]+", text) if item]


def _audit_input_manifest(run: Path) -> tuple[dict[str, Any], list[str]]:
    """Build the immutable, canonical input set reviewed by a paper audit.

    ASSEMBLY_MANIFEST.json is intentionally excluded: assemble rewrites its
    timestamp.  PAPER_PLAN, every included planned section and PAPER_DRAFT bind
    the equivalent semantic/content state without that unstable artifact.
    """
    issues: list[str] = []
    targets: dict[str, Path] = {}
    freeze_files = _freeze_file_index(run)

    def add_path(path: Path, label: str) -> None:
        try:
            resolved = path.resolve()
            resolved.relative_to(run.resolve())
        except (OSError, ValueError):
            issues.append(f"审计输入不在当前 run 内：{label}")
            return
        try:
            valid = resolved.is_file() and resolved.stat().st_size > 0
        except OSError:
            valid = False
        if not valid:
            issues.append(f"审计输入缺失、非常规文件或为空：{label}")
            return
        relative = relative_to_run(resolved, run)
        targets[relative] = resolved

    for relative in AUDIT_INPUT_FILES:
        add_path(run / relative, relative)

    plan_path = run / "paper" / "PAPER_PLAN.json"
    planned_count = 0
    if plan_path.is_file():
        try:
            plan_payload = json.loads(plan_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            issues.append(f"无法解析审计输入 PAPER_PLAN.json：{exc}")
            plan_payload = {}
        plan_sections = plan_payload.get("sections", []) if isinstance(plan_payload, dict) else []
        if not isinstance(plan_sections, list):
            issues.append("审计输入 PAPER_PLAN.json sections 不是数组")
        else:
            for section in plan_sections:
                if not isinstance(section, dict) or not section.get("include_in_manuscript", True):
                    continue
                planned_count += 1
                try:
                    section_path = _safe_section_path(run, section)
                except ValueError as exc:
                    issues.append(f"审计输入 section 路径无效：{exc}")
                    continue
                add_path(section_path, relative_to_run(section_path, run))
    if not planned_count:
        issues.append("审计输入没有 PAPER_PLAN 选入的 section")

    symbol_path = run / "paper" / "SYMBOL_TABLE.csv"
    if symbol_path.is_file():
        for index, row in enumerate(read_csv(symbol_path), start=1):
            raw = str(row.get("source_path", "")).strip()
            if not raw:
                continue
            resolved, error = _run_local_regular_file(run, raw)
            if resolved is None:
                issues.append(f"符号表第 {index} 行 source_path {error}")
            else:
                add_path(resolved, f"SYMBOL_TABLE source_path {raw}")
                if not _is_freeze_bound(run, resolved, freeze_files):
                    issues.append(
                        f"符号表第 {index} 行 source_path 未绑定到当前 "
                        f"FROZEN_MANIFEST：{relative_to_run(resolved, run)}"
                    )

    figure_path = run / "paper" / "FIGURE_TABLE_REGISTER.csv"
    if figure_path.is_file():
        for index, row in enumerate(read_csv(figure_path), start=1):
            if str(row.get("status", "")).strip().lower() not in ACTIVE_FIGURE_STATUSES:
                continue
            raw = str(row.get("path", "")).strip()
            if not raw:
                continue
            resolved, error = _run_local_regular_file(run, raw)
            if resolved is None:
                issues.append(f"图表登记第 {index} 行 path {error}")
            else:
                add_path(resolved, f"FIGURE_TABLE_REGISTER path {raw}")
                if not _is_freeze_bound(run, resolved, freeze_files):
                    issues.append(
                        f"图表登记第 {index} 行 path 未绑定到当前 "
                        f"FROZEN_MANIFEST：{relative_to_run(resolved, run)}"
                    )

    records = [
        {
            "path": relative,
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for relative, path in sorted(targets.items())
    ]
    return {"schema_version": 1, "hash_algorithm": "sha256", "files": records}, issues


def _audit_manifest_drift(
    stored: Any, current: dict[str, Any]
) -> list[str]:
    if not isinstance(stored, dict) or not isinstance(stored.get("files"), list):
        return ["PAPER_AUDIT.json 缺少有效 audit_input_manifest"]

    def index(payload: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[str]]:
        result: dict[str, dict[str, Any]] = {}
        duplicates: list[str] = []
        for item in payload.get("files", []):
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", "")).strip()
            if not path:
                continue
            if path in result:
                duplicates.append(path)
            result[path] = item
        return result, duplicates

    old, duplicates = index(stored)
    new, _ = index(current)
    issues = [f"PAPER_AUDIT audit_input_manifest 路径重复：{path}" for path in duplicates]
    for path in sorted(set(old) - set(new)):
        issues.append(f"论文审计后输入已删除：{path}")
    for path in sorted(set(new) - set(old)):
        issues.append(f"论文审计后输入已新增：{path}")
    for path in sorted(set(old) & set(new)):
        if old[path].get("sha256") != new[path].get("sha256"):
            issues.append(f"论文审计后输入已修改：{path}")
        elif old[path].get("size") != new[path].get("size"):
            issues.append(f"论文审计后输入大小已变化：{path}")
    return issues


def _plan_projection(working_title: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "working_title": working_title,
        "sections": [
            {
                "section_id": str(item["section_id"]),
                "order": int(item["order"]),
                "path": str(item["path"]),
                "sha256": str(item["sha256"]),
            }
            for item in records
        ],
    }


def _write_section_stub(run: Path, section: dict[str, Any], *, replace: bool) -> None:
    path = _safe_section_path(run, section)
    if path.exists() and not replace:
        return
    trace_hint = "\n".join(f"<!-- CLAIM:{cid} -->" for cid in section.get("claim_ids", []))
    if trace_hint:
        trace_hint = "\n\n预分配的 claim trace tags：\n\n" + trace_hint
    text = (
        f"# {section['title']}\n\n"
        "[DRAFT_REQUIRED]\n\n"
        f"<!-- SECTION_ID:{section['id']} -->\n\n"
        f"写作目的：{section.get('purpose', '')}\n\n"
        "写作时只使用冻结的科学产物、CLAIM_EVIDENCE_LEDGER 和 SOURCE_LEDGER。"
        "每个核心 claim 在支持该结论的正文附近保留 `<!-- CLAIM:Cxxx -->` trace tag。"
        f"{trace_hint}\n"
    )
    atomic_write_text(path, text)


def _outline_text(plan: dict[str, Any], claim_rows: list[dict[str, str]]) -> str:
    mapping: dict[str, list[str]] = {}
    for row in claim_rows:
        target = str(row.get("target_section", "")).strip()
        if target:
            mapping.setdefault(target, []).append(str(row.get("claim_id", "")))
    lines = ["# 论文提纲", "", f"工作标题：{plan.get('working_title', '[DRAFT_REQUIRED]')}", "", "## 章节结构", ""]
    for section in sorted(plan.get("sections", []), key=_section_order):
        if not section.get("include_in_manuscript", True):
            continue
        marks = ", ".join(mapping.get(str(section["id"]), [])) or "未分配"
        lines.extend(
            [
                f"### {section['title']} (`{section['id']}`)",
                "",
                str(section.get("purpose", "")),
                "",
                f"关联 claims：{marks}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _freeze_check(run: Path) -> dict[str, Any]:
    path = run / "FROZEN_MANIFEST.json"
    if not path.exists():
        return {"ok": False, "issues": ["FROZEN_MANIFEST.json 不存在"]}
    # Reuse the harness verifier so paper-only commands also detect files added
    # to the scientific freeze scope after freezing, not just changed/deleted
    # entries already listed in the manifest.
    try:
        from harness import verify_freeze

        result = verify_freeze(run, quiet=True)
    except (Exception, SystemExit) as exc:
        return {"ok": False, "issues": [f"冻结校验器异常：{exc}"]}
    return {"ok": bool(result.get("ok")), "issues": list(result.get("issues", []))}


def _is_template_plan(plan: dict[str, Any]) -> bool:
    return not plan.get("sections") and "[DRAFT_REQUIRED]" in str(plan.get("working_title", ""))


def cmd_init(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    freeze = _freeze_check(run)
    if not freeze["ok"] and not args.allow_unfrozen:
        raise SystemExit("错误：论文阶段默认要求先冻结科学产物：" + "; ".join(freeze["issues"]))

    paper = run / "paper"
    (paper / "sections").mkdir(parents=True, exist_ok=True)
    (paper / "packets").mkdir(parents=True, exist_ok=True)
    plan_path = paper / "PAPER_PLAN.json"
    if plan_path.exists() and not args.replace:
        try:
            existing = read_json(plan_path, expect=dict)
        except SystemExit:
            existing = {}
        if not _is_template_plan(existing):
            raise SystemExit("错误：PAPER_PLAN.json 已初始化。使用 --replace 明确重建写作骨架。")

    profile = _profile()
    sections: list[dict[str, Any]] = []
    for raw in profile.get("sections", []):
        section = copy.deepcopy(raw)
        section["file"] = _section_filename(section)
        section["include_in_manuscript"] = bool(section.get("required", True))
        section["claim_ids"] = []
        section["source_paths"] = []
        section["figure_ids"] = []
        section["table_ids"] = []
        section["status"] = "planned"
        section["writer"] = ""
        section["notes"] = ""
        sections.append(section)

    groups = _claim_groups(run)
    claim_rows: list[dict[str, Any]] = []
    for claim_id in sorted(groups):
        group = groups[claim_id]
        target = _match_section(group.get("paper_location", ""), sections)
        if target:
            for section in sections:
                if section["id"] == target:
                    section["claim_ids"].append(claim_id)
                    break
        claim_rows.append(
            {
                "claim_id": claim_id,
                "claim_text": group.get("claim_text", ""),
                "is_core": "true" if group.get("is_core") else "false",
                "target_section": target,
                "evidence_status": _evidence_status(run, group),
                "trace_tag_status": "pending",
                "notes": "",
            }
        )

    plan = {
        "schema_version": 1,
        "profile": profile.get("profile", "cumcm-default"),
        "working_title": "[DRAFT_REQUIRED]",
        "narrative_thesis": "[DRAFT_REQUIRED]",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "sections": sections,
    }
    write_json(plan_path, plan)
    write_csv(paper / "PAPER_CLAIM_MAP.csv", CLAIM_MAP_FIELDS, claim_rows)
    write_csv(paper / "SYMBOL_TABLE.csv", SYMBOL_FIELDS, [])
    write_csv(paper / "FIGURE_TABLE_REGISTER.csv", FIGURE_TABLE_FIELDS, [])
    atomic_write_text(paper / "PAPER_OUTLINE.md", _outline_text(plan, claim_rows))

    core = [row for row in claim_rows if is_truthy(row.get("is_core"))]
    verified = [row for row in core if row.get("evidence_status") == "verified"]
    unassigned = [row["claim_id"] for row in core if not row.get("target_section")]
    brief = (
        "# 论文写作 Brief\n\n"
        "[DRAFT_REQUIRED]\n\n"
        f"- Run: `{run.name}`\n"
        f"- 写作 profile: `{profile.get('profile', 'cumcm-default')}`\n"
        f"- 核心 claims: {len(core)}\n"
        f"- 已有人工作证据的核心 claims: {len(verified)}\n"
        f"- 尚未分配章节的核心 claims: {', '.join(unassigned) if unassigned else '无'}\n\n"
        "由 `paper_architect` 补全工作标题、全文叙事主线、小问到章节的映射、关键图表/公式、"
        "结论边界以及可以并行写作的章节。不要在这里创造新科学结果。\n"
    )
    atomic_write_text(paper / "PAPER_BRIEF.md", brief)
    atomic_write_text(
        paper / "CONSISTENCY_AUDIT.md",
        "# 论文一致性审计\n\n[DRAFT_REQUIRED]\n\n尚未执行 paper_auditor / red_team / compliance_auditor。\n",
    )
    write_json(
        paper / "PAPER_AUDIT.json",
        {
            "status": "pending",
            "checked_at": "",
            "checked_by": "",
            "blockers": [],
            "warnings": [],
            "notes": "[DRAFT_REQUIRED]",
            "draft_sha256": "",
            "freeze_manifest_sha256": "",
            "audit_input_manifest": {
                "schema_version": 1,
                "hash_algorithm": "sha256",
                "files": [],
            },
            "audit_input_sha256": "",
        },
    )
    atomic_write_text(
        paper / "REVISION_LOG.md",
        "# 论文修订日志\n\n| 时间 | 修改者 | 章节 | 修改原因 | 证据/审计项 | 结果 |\n|---|---|---|---|---|---|\n",
    )
    for section in sections:
        _write_section_stub(run, section, replace=True)

    print(f"Paper workspace initialized: {relative_to_run(paper, run)}")
    print(f"Sections: {len(sections)}; claims: {len(claim_rows)}; unassigned core claims: {len(unassigned)}")
    return 0


def _load_plan(run: Path) -> dict[str, Any]:
    return read_json(run / "paper" / "PAPER_PLAN.json", expect=dict)


def _section_by_id(plan: dict[str, Any], section_id: str) -> dict[str, Any]:
    for section in plan.get("sections", []):
        if str(section.get("id")) == section_id:
            return section
    raise SystemExit(f"错误：PAPER_PLAN 中不存在 section={section_id}")


def _packet_text(run: Path, section: dict[str, Any]) -> str:
    claim_map = read_csv(run / "paper" / "PAPER_CLAIM_MAP.csv")
    ledger = read_csv(run / "evidence" / "CLAIM_EVIDENCE_LEDGER.csv")
    sources = read_csv(run / "sources" / "SOURCE_LEDGER.csv")
    claim_ids = [
        str(row.get("claim_id", ""))
        for row in claim_map
        if str(row.get("target_section", "")) == str(section["id"])
    ]
    mapped_rows = [row for row in claim_map if row.get("claim_id") in claim_ids]
    evidence_rows = [row for row in ledger if row.get("claim_id") in claim_ids]
    source_rows = [
        row for row in sources
        if any(cid and cid in str(row.get("claim_supported", "")) for cid in claim_ids)
    ]
    artifact_paths = set(str(item) for item in section.get("source_paths", []) if item)
    for row in evidence_rows:
        if row.get("evidence_path"):
            artifact_paths.add(str(row["evidence_path"]))
    for default in ("synthesis/FINAL_MODEL_SPEC.md", "problem/QUESTION_MAP.md", "problem/ASSUMPTION_REGISTER.csv"):
        if (run / default).exists():
            artifact_paths.add(default)

    lines = [
        f"# Section writing packet — {section['title']}",
        "",
        f"- section_id: `{section['id']}`",
        f"- output_file: `{section['file']}`",
        f"- purpose: {section.get('purpose', '')}",
        "",
        "## Hard writing rules",
        "",
        "1. Write only this section; do not silently rewrite scientific artifacts.",
        "2. Do not invent numbers, citations, assumptions, experiments, solver settings or comparative claims.",
        "3. Every mapped core claim must have its exact trace tag `<!-- CLAIM:<id> -->` near the supporting prose.",
        "4. If evidence is insufficient, write a visible blocker in the section workspace instead of filling the gap from memory.",
        "5. References may only come from SOURCE_LEDGER; keep citation identifiers/titles faithful to the ledger.",
        "",
        "## Mapped claims",
        "",
    ]
    if mapped_rows:
        for row in mapped_rows:
            lines.extend(
                [
                    f"### {row.get('claim_id')} {'[CORE]' if is_truthy(row.get('is_core')) else ''}",
                    "",
                    str(row.get("claim_text", "")),
                    "",
                    f"Evidence status: `{row.get('evidence_status', '')}`",
                    f"Required trace tag: `<!-- CLAIM:{row.get('claim_id')} -->`",
                    "",
                ]
            )
    else:
        lines.extend(["No claims are mapped yet. `paper_architect` should map claims before substantive writing.", ""])

    lines.extend(["## Evidence rows", ""])
    if evidence_rows:
        for row in evidence_rows:
            lines.append(
                "- "
                + " | ".join(
                    [
                        f"claim={row.get('claim_id','')}",
                        f"evidence={row.get('evidence_id','')}",
                        f"type={row.get('evidence_type','')}",
                        f"path={row.get('evidence_path','')}",
                        f"result={row.get('result_summary','')}",
                        f"verified={row.get('verification_status','')}",
                    ]
                )
            )
    else:
        lines.append("- No evidence rows are currently mapped.")

    lines.extend(["", "## Allowed / relevant frozen artifacts", ""])
    for path in sorted(artifact_paths):
        lines.append(f"- `{path}`")
    if not artifact_paths:
        lines.append("- No explicit artifact paths yet.")

    lines.extend(["", "## Registered external sources for these claims", ""])
    if source_rows:
        for row in source_rows:
            lines.append(
                f"- {row.get('source_id','')}: {row.get('title','')} — {row.get('author_or_org','')} ({row.get('year','')})"
            )
    else:
        lines.append("- None mapped. Do not fabricate references.")
    lines.append("")
    return "\n".join(lines)


def cmd_packet(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    plan = _load_plan(run)
    sections = sorted(plan.get("sections", []), key=_section_order)
    selected = sections if args.all else [_section_by_id(plan, args.section)]
    out_dir = run / "paper" / "packets"
    out_dir.mkdir(parents=True, exist_ok=True)
    for section in selected:
        path = out_dir / f"{int(section['order']):03d}_{section['id']}_PACKET.md"
        atomic_write_text(path, _packet_text(run, section))
        print(relative_to_run(path, run))
    return 0


def _assemble(run: Path) -> dict[str, Any]:
    plan = _load_plan(run)
    title = str(plan.get("working_title", "")).strip()
    parts: list[str] = []
    if title and "[DRAFT_REQUIRED]" not in title:
        parts.extend([f"# {title}", ""])
    records: list[dict[str, Any]] = []
    for section in sorted(plan.get("sections", []), key=_section_order):
        if not section.get("include_in_manuscript", True):
            continue
        try:
            path = _safe_section_path(run, section)
        except ValueError as exc:
            raise SystemExit(f"错误：{exc}") from exc
        if not path.exists():
            raise SystemExit(f"错误：缺少 section 文件：{relative_to_run(path, run)}")
        text = path.read_text(encoding="utf-8-sig").strip()
        parts.extend([text, ""])
        records.append(
            {
                "section_id": section.get("id"),
                "order": _section_order(section),
                "path": relative_to_run(path, run),
                "sha256": sha256_file(path),
            }
        )
    draft = "\n".join(parts).rstrip() + "\n"
    draft_path = run / "paper" / "PAPER_DRAFT.md"
    atomic_write_text(draft_path, draft)
    projection = _plan_projection(title, records)
    manifest = {
        "schema_version": 2,
        "generated_at": now_iso(),
        "sections": records,
        "plan_projection": projection,
        "plan_projection_sha256": _canonical_payload_sha256(projection),
        "draft_sha256": sha256_file(draft_path),
    }
    write_json(run / "paper" / "ASSEMBLY_MANIFEST.json", manifest)
    return manifest


def cmd_assemble(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    manifest = _assemble(run)
    print(f"Assembled {len(manifest['sections'])} sections -> paper/PAPER_DRAFT.md")
    return 0


def _append_issue(container: list[str], message: str) -> None:
    if message not in container:
        container.append(message)


def validate_paper_run(run: Path, *, strict: bool = False, require_final: bool = True, require_audit: bool = True) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    freeze = _freeze_check(run)
    if not freeze["ok"]:
        for issue in freeze["issues"]:
            _append_issue(errors, issue)

    paper = run / "paper"
    required_static = [
        "PAPER_BRIEF.md",
        "PAPER_PLAN.json",
        "PAPER_CLAIM_MAP.csv",
        "SYMBOL_TABLE.csv",
        "FIGURE_TABLE_REGISTER.csv",
        "PAPER_OUTLINE.md",
        "PAPER_DRAFT.md",
        "CONSISTENCY_AUDIT.md",
        "PAPER_AUDIT.json",
        "REVISION_LOG.md",
        "ASSEMBLY_MANIFEST.json",
    ]
    if require_final:
        required_static.extend(["FINAL_MANUSCRIPT.md", "PAPER_BUILD.json"])
    for name in required_static:
        path = paper / name
        if not path.exists() or (path.is_file() and path.stat().st_size == 0):
            _append_issue(errors, f"缺少或为空：paper/{name}")

    if not (paper / "PAPER_PLAN.json").exists():
        return {"ok": False, "errors": errors, "warnings": warnings, "checked_at": now_iso()}
    plan = read_json(paper / "PAPER_PLAN.json", expect=dict)
    sections = plan.get("sections", [])
    if not isinstance(sections, list) or not sections:
        _append_issue(errors, "PAPER_PLAN.json 没有 sections")
        return {"ok": False, "errors": errors, "warnings": warnings, "checked_at": now_iso()}
    working_title = str(plan.get("working_title", "")).strip()
    narrative_thesis = str(plan.get("narrative_thesis", "")).strip()
    if not working_title:
        (errors if strict else warnings).append("PAPER_PLAN working_title 为空")
    elif any(token in working_title for token in PLACEHOLDERS):
        _append_issue(errors, "PAPER_PLAN working_title 仍是占位内容")
    if not narrative_thesis:
        (errors if strict else warnings).append("PAPER_PLAN narrative_thesis 为空")
    elif any(token in narrative_thesis for token in PLACEHOLDERS):
        _append_issue(errors, "PAPER_PLAN narrative_thesis 仍是占位内容")

    section_ids: set[str] = set()
    included_section_ids: set[str] = set()
    section_text: dict[str, str] = {}
    section_orders: dict[int, str] = {}
    section_files: dict[str, str] = {}
    plan_claim_targets: dict[str, set[str]] = {}
    planned_section_paths: set[str] = set()
    projection_rows: list[tuple[int, int, dict[str, Any]]] = []
    projection_ready = True
    included_count = 0
    for position, section in enumerate(sections):
        if not isinstance(section, dict):
            _append_issue(errors, f"PAPER_PLAN section #{position + 1} 必须是对象")
            projection_ready = False
            continue
        sid = str(section.get("id", "")).strip()
        if not sid:
            _append_issue(errors, "PAPER_PLAN 存在无 id 的 section")
            projection_ready = False
            continue
        if sid in section_ids:
            _append_issue(errors, f"PAPER_PLAN section id 重复：{sid}")
            projection_ready = False
        section_ids.add(sid)
        raw_section_claim_ids = section.get("claim_ids", [])
        if not isinstance(raw_section_claim_ids, list):
            _append_issue(errors, f"PAPER_PLAN section {sid} claim_ids 必须是数组")
            section_claim_ids = []
        else:
            section_claim_ids = [
                str(item).strip()
                for item in raw_section_claim_ids
                if str(item).strip()
            ]
        duplicate_section_claims = sorted(
            {item for item in section_claim_ids if section_claim_ids.count(item) > 1}
        )
        if duplicate_section_claims:
            _append_issue(
                errors,
                f"PAPER_PLAN section {sid} claim_ids 重复：{', '.join(duplicate_section_claims)}",
            )
        for claim_id in section_claim_ids:
            plan_claim_targets.setdefault(claim_id, set()).add(sid)
        if not str(section.get("title", "")).strip():
            (errors if strict else warnings).append(f"PAPER_PLAN section {sid} 标题为空")
        try:
            order = _section_order(section)
        except ValueError as exc:
            _append_issue(errors, str(exc))
            projection_ready = False
            order = 0
        else:
            prior_sid = section_orders.get(order)
            if prior_sid is not None:
                _append_issue(errors, f"PAPER_PLAN section order 重复：{order} ({prior_sid}, {sid})")
                projection_ready = False
            else:
                section_orders[order] = sid
        try:
            path = _safe_section_path(run, section)
        except ValueError as exc:
            _append_issue(errors, str(exc))
            projection_ready = False
            path = None
        if path is not None:
            path_key = str(path.resolve()).casefold()
            planned_section_paths.add(path_key)
            prior_sid = section_files.get(path_key)
            if prior_sid is not None:
                _append_issue(errors, f"PAPER_PLAN section file 重复：{relative_to_run(path, run)} ({prior_sid}, {sid})")
                projection_ready = False
            else:
                section_files[path_key] = sid
        if section.get("required", False) and not section.get("include_in_manuscript", True):
            _append_issue(errors, f"必需章节被排除：{sid}")
        if not section.get("include_in_manuscript", True):
            continue
        included_section_ids.add(sid)
        included_count += 1
        if path is None:
            continue
        if not path.exists() or not path.is_file() or path.stat().st_size == 0:
            _append_issue(errors, f"章节缺失或为空：{section.get('file','')}")
            projection_ready = False
            continue
        text = path.read_text(encoding="utf-8-sig")
        section_text[sid] = text
        projection_rows.append(
            (
                order,
                position,
                {
                    "section_id": sid,
                    "order": order,
                    "path": relative_to_run(path, run),
                    "sha256": sha256_file(path),
                },
            )
        )
        placeholders = contains_placeholder(path, PLACEHOLDERS)
        if placeholders:
            _append_issue(errors, f"章节 {sid} 仍含占位符：{sorted(set(placeholders))}")

    projection_records = [item for _, _, item in sorted(projection_rows, key=lambda row: (row[0], row[1]))]
    current_projection = None
    current_projection_sha256 = ""
    if projection_ready and len(projection_records) == included_count:
        current_projection = _plan_projection(str(plan.get("working_title", "")).strip(), projection_records)
        current_projection_sha256 = _canonical_payload_sha256(current_projection)

    sections_root = paper / "sections"
    if sections_root.exists():
        unplanned = [
            relative_to_run(path, run)
            for path in sorted(sections_root.rglob("*.md"), key=lambda item: item.as_posix())
            if str(path.resolve()).casefold() not in planned_section_paths
        ]
        if unplanned:
            _append_issue(
                warnings,
                "paper/sections 存在 PAPER_PLAN 未引用文件；这些文件会保留但不会进入正文：" + ", ".join(unplanned),
            )

    groups = _claim_groups(run)
    claim_rows = read_csv(paper / "PAPER_CLAIM_MAP.csv")
    claim_map_rows: dict[str, list[dict[str, str]]] = {}
    for row in claim_rows:
        claim_id = str(row.get("claim_id", "")).strip()
        if claim_id:
            claim_map_rows.setdefault(claim_id, []).append(row)
    for claim_id, rows in sorted(claim_map_rows.items()):
        if len(rows) != 1:
            _append_issue(errors, f"PAPER_CLAIM_MAP claim_id 重复：{claim_id}")
    mapped = {claim_id: rows[0] for claim_id, rows in claim_map_rows.items() if len(rows) == 1}
    for cid, group in groups.items():
        if not group.get("is_core"):
            continue
        row = mapped.get(cid)
        if not row:
            _append_issue(errors, f"核心 claim 未进入 PAPER_CLAIM_MAP：{cid}")
            continue
        target = str(row.get("target_section", "")).strip()
        if not target:
            _append_issue(errors, f"核心 claim 未分配章节：{cid}")
            continue
        if target not in included_section_ids:
            _append_issue(errors, f"核心 claim {cid} 指向未知或未入选正文的章节：{target}")
            continue
        status = _evidence_status(run, group)
        if status == "missing":
            _append_issue(errors, f"核心 claim 缺少证据：{cid}")
        elif status != "verified":
            (errors if strict else warnings).append(f"核心 claim 尚无人工 verified 证据：{cid}")
        evidence_paths = [str(row.get("evidence_path", "")).strip() for row in group.get("rows", []) if row.get("evidence_path")]
        if evidence_paths and not any(_artifact_path(run, item) for item in evidence_paths):
            _append_issue(errors, f"核心 claim 的证据路径均不存在：{cid} -> {evidence_paths}")
        tag = f"<!-- CLAIM:{cid} -->"
        if tag not in section_text.get(target, ""):
            _append_issue(errors, f"核心 claim {cid} 在章节 {target} 缺少 trace tag `{tag}`")

    for row in claim_rows:
        cid = str(row.get("claim_id", "")).strip()
        target = str(row.get("target_section", "")).strip()
        if cid and cid not in groups:
            _append_issue(warnings, f"PAPER_CLAIM_MAP 含未知 claim：{cid}")
        if target and target not in included_section_ids:
            _append_issue(errors, f"PAPER_CLAIM_MAP {cid} 指向未知或未入选正文的 section：{target}")
        planned_targets = plan_claim_targets.get(cid, set())
        if target and planned_targets != {target}:
            _append_issue(
                errors,
                f"PAPER_CLAIM_MAP 与 PAPER_PLAN claim_ids 不一致：{cid} -> {target}, "
                f"plan={sorted(planned_targets)}",
            )

    for cid, targets in sorted(plan_claim_targets.items()):
        rows = claim_map_rows.get(cid, [])
        if len(rows) != 1:
            _append_issue(
                errors,
                f"PAPER_PLAN claim {cid} 必须在 PAPER_CLAIM_MAP 中恰好出现一次",
            )
            continue
        mapped_target = str(rows[0].get("target_section", "")).strip()
        if len(targets) != 1 or mapped_target not in targets:
            _append_issue(
                errors,
                f"PAPER_PLAN claim {cid} 的章节归属不唯一或与 PAPER_CLAIM_MAP 不一致："
                f"plan={sorted(targets)}, map={mapped_target or '<empty>'}",
            )

    freeze_files = _freeze_file_index(run)
    symbols = read_csv(paper / "SYMBOL_TABLE.csv")
    if not symbols:
        (errors if strict else warnings).append(
            "SYMBOL_TABLE.csv 为空；严格论文校验要求显式符号表"
        )
    seen_symbols: set[str] = set()
    for index, row in enumerate(symbols, start=1):
        symbol = str(row.get("symbol", "")).strip()
        label = symbol or f"第 {index} 行"
        if not symbol:
            _append_issue(errors, f"符号表第 {index} 行缺少 symbol")
        elif symbol in seen_symbols:
            _append_issue(errors, f"SYMBOL_TABLE.csv symbol 重复：{symbol}")
        seen_symbols.add(symbol)
        for field in ("meaning", "unit", "scope", "source_path"):
            if not str(row.get(field, "")).strip():
                _append_issue(errors, f"符号 {label} 缺少 {field}")
        if not is_truthy(row.get("verified")):
            (errors if strict else warnings).append(f"符号 {label} 未标记 verified")
        source_text = str(row.get("source_path", "")).strip()
        if source_text:
            source_path, source_error = _run_local_regular_file(run, source_text)
            if source_path is None:
                _append_issue(errors, f"符号 {label} source_path {source_error}")
            elif not _is_freeze_bound(run, source_path, freeze_files):
                (errors if strict else warnings).append(
                    f"符号 {label} source_path 未绑定到当前 FROZEN_MANIFEST："
                    f"{relative_to_run(source_path, run)}"
                )

    fig_rows = read_csv(paper / "FIGURE_TABLE_REGISTER.csv")
    seen_figure_ids: set[str] = set()
    for index, row in enumerate(fig_rows, start=1):
        item_id = str(row.get("item_id", "")).strip()
        label = item_id or f"第 {index} 行"
        if not item_id:
            _append_issue(errors, f"图表登记第 {index} 行缺少 item_id")
        elif item_id in seen_figure_ids:
            _append_issue(errors, f"FIGURE_TABLE_REGISTER.csv item_id 重复：{item_id}")
        seen_figure_ids.add(item_id)
        status = str(row.get("status", "")).strip().lower()
        if status not in ACTIVE_FIGURE_STATUSES:
            continue
        for field in ("kind", "title", "source_claims", "target_section", "verified_by"):
            if not str(row.get(field, "")).strip():
                _append_issue(errors, f"已使用的图表 {label} 缺少 {field}")
        if not is_named_human_identity(row.get("verified_by", "")):
            _append_issue(
                errors,
                f"已使用的图表 {label} verified_by 必须为具名人工，不能使用 TEAM/UNKNOWN/TODO 等占位值",
            )
        path_text = str(row.get("path", "")).strip()
        if not path_text:
            _append_issue(errors, f"已使用的图表 {label} 缺少 path")
        else:
            candidate, path_error = _run_local_regular_file(run, path_text)
            if candidate is None:
                _append_issue(errors, f"已使用的图表 {label} path {path_error}")
            elif not _is_freeze_bound(run, candidate, freeze_files):
                _append_issue(
                    errors,
                    f"已使用的图表 {label} 未绑定到当前 FROZEN_MANIFEST："
                    f"{relative_to_run(candidate, run)}",
                )
        source_claims = _split_references(row.get("source_claims", ""))
        for claim_id in source_claims:
            if claim_id not in groups:
                _append_issue(errors, f"图表 {label} source_claims 指向未知 claim：{claim_id}")
        target = str(row.get("target_section", "")).strip()
        if target and target not in included_section_ids:
            _append_issue(errors, f"图表 {label} 指向未知或未入选正文的 section：{target}")

    assembly_path = paper / "ASSEMBLY_MANIFEST.json"
    draft_path = paper / "PAPER_DRAFT.md"
    if assembly_path.exists() and draft_path.exists():
        assembly = read_json(assembly_path, expect=dict)
        if assembly.get("draft_sha256") != sha256_file(draft_path):
            _append_issue(errors, "PAPER_DRAFT.md 已在 assemble 后被直接修改；请修改 section 文件并重新 assemble")
        recorded_projection_sha256 = str(assembly.get("plan_projection_sha256", "")).strip()
        if not recorded_projection_sha256:
            _append_issue(errors, "ASSEMBLY_MANIFEST.json 缺少 plan projection；请重新运行 paper.py assemble")
        elif current_projection_sha256 and recorded_projection_sha256 != current_projection_sha256:
            _append_issue(errors, "PAPER_PLAN 标题、章节顺序/包含关系/路径或章节内容在 assemble 后发生变化；请重新运行 paper.py assemble")
        if current_projection is not None and assembly.get("plan_projection") != current_projection:
            _append_issue(errors, "ASSEMBLY_MANIFEST.json 的有序章节投影与当前 PAPER_PLAN 不一致；请重新运行 paper.py assemble")
        if isinstance(assembly.get("plan_projection"), dict) and assembly.get("sections") != assembly["plan_projection"].get("sections"):
            _append_issue(errors, "ASSEMBLY_MANIFEST.json 的 sections 镜像与 plan projection 不一致；请重新运行 paper.py assemble")
        placeholders = contains_placeholder(draft_path, PLACEHOLDERS)
        if placeholders:
            _append_issue(errors, f"PAPER_DRAFT.md 仍含占位符：{sorted(set(placeholders))}")

    paper_text_checks = ["PAPER_BRIEF.md", "PAPER_OUTLINE.md"]
    if require_audit:
        paper_text_checks.append("CONSISTENCY_AUDIT.md")
    for name in paper_text_checks:
        path = paper / name
        if path.exists():
            placeholders = contains_placeholder(path, PLACEHOLDERS)
            if placeholders:
                _append_issue(errors, f"paper/{name} 仍含占位符：{sorted(set(placeholders))}")

    audit_path = paper / "PAPER_AUDIT.json"
    if require_audit and audit_path.exists():
        audit = read_json(audit_path, expect=dict)
        if str(audit.get("status", "")).strip().lower() != "passed":
            _append_issue(errors, "PAPER_AUDIT.json status 必须为 passed")
        if audit.get("blockers"):
            _append_issue(errors, f"PAPER_AUDIT.json 仍有 blockers：{audit.get('blockers')}")
        if any(token in str(audit.get("notes", "")) for token in PLACEHOLDERS):
            _append_issue(errors, "PAPER_AUDIT.json notes 仍是占位内容")
        if str(audit.get("status", "")).strip().lower() == "passed":
            if not is_named_human_identity(audit.get("checked_by", "")):
                _append_issue(
                    errors,
                    "PAPER_AUDIT.json passed 的 checked_by 必须为具名人工，"
                    "不能使用 TEAM/UNKNOWN/TODO 等占位值",
                )
            if not is_timezone_aware_iso(audit.get("checked_at", "")):
                _append_issue(
                    errors,
                    "PAPER_AUDIT.json passed 的 checked_at 必须是带显式时区的 ISO 8601 时间",
                )
            if draft_path.exists() and audit.get("draft_sha256") != sha256_file(draft_path):
                _append_issue(errors, "PAPER_AUDIT.json 对应的 draft 已变化；修改论文后必须重新审计并 audit-set")
            freeze_path = run / "FROZEN_MANIFEST.json"
            if freeze_path.exists() and audit.get("freeze_manifest_sha256") != sha256_file(freeze_path):
                _append_issue(errors, "PAPER_AUDIT.json 对应的科学冻结版本已变化；必须重新审计")
            current_audit_manifest, manifest_issues = _audit_input_manifest(run)
            for issue in manifest_issues:
                _append_issue(errors, issue)
            stored_manifest = audit.get("audit_input_manifest")
            stored_digest = str(audit.get("audit_input_sha256", "")).strip()
            if not stored_digest:
                _append_issue(errors, "PAPER_AUDIT.json 缺少 audit_input_sha256；请重新 audit-set")
            if isinstance(stored_manifest, dict):
                canonical_stored_digest = _canonical_payload_sha256(stored_manifest)
                if stored_digest and stored_digest != canonical_stored_digest:
                    _append_issue(errors, "PAPER_AUDIT.json audit_input_sha256 与其 manifest 不一致")
            for issue in _audit_manifest_drift(stored_manifest, current_audit_manifest):
                _append_issue(errors, issue)
            current_digest = _canonical_payload_sha256(current_audit_manifest)
            if stored_digest and stored_digest != current_digest:
                _append_issue(errors, "PAPER_AUDIT.json 绑定的审计输入总哈希已变化；必须重新审计")

    if require_final:
        final_path = paper / "FINAL_MANUSCRIPT.md"
        build_path = paper / "PAPER_BUILD.json"
        if final_path.exists():
            placeholders = contains_placeholder(final_path, PLACEHOLDERS)
            if placeholders:
                _append_issue(errors, f"FINAL_MANUSCRIPT.md 仍含占位符：{sorted(set(placeholders))}")
            if TRACE_RE.search(final_path.read_text(encoding="utf-8-sig")):
                _append_issue(errors, "FINAL_MANUSCRIPT.md 仍含内部 claim trace tag")
        if build_path.exists() and final_path.exists() and draft_path.exists():
            build = read_json(build_path, expect=dict)
            if build.get("draft_sha256") != sha256_file(draft_path):
                _append_issue(errors, "PAPER_BUILD.json 对应的 draft 已变化；请重新 finalize")
            if build.get("final_sha256") != sha256_file(final_path):
                _append_issue(errors, "FINAL_MANUSCRIPT.md 与 PAPER_BUILD.json 哈希不一致")
            build_projection_sha256 = str(build.get("plan_projection_sha256", "")).strip()
            if not build_projection_sha256:
                _append_issue(errors, "PAPER_BUILD.json 缺少 plan projection；请重新 finalize")
            elif current_projection_sha256 and build_projection_sha256 != current_projection_sha256:
                _append_issue(errors, "PAPER_BUILD.json 对应的 PAPER_PLAN 或 section 内容已变化；请重新 assemble、审计并 finalize")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "checked_at": now_iso(),
        "counts": {
            "sections": len(sections),
            "claims": len(groups),
            "core_claims": sum(1 for item in groups.values() if item.get("is_core")),
            "mapped_claims": len(mapped),
            "symbols": len(symbols),
            "figure_table_items": len(fig_rows),
        },
    }


def cmd_lint(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    require_final = not (args.pre_final or args.pre_audit)
    require_audit = not args.pre_audit
    result = validate_paper_run(run, strict=args.strict, require_final=require_final, require_audit=require_audit)
    write_json(run / "paper" / "PAPER_LINT.json", result)
    print("Paper lint OK" if result["ok"] else "Paper lint FAIL")
    for item in result["errors"]:
        print(f"  ERROR: {item}")
    for item in result["warnings"]:
        print(f"  WARN:  {item}")
    return 0 if result["ok"] else 4


def _strip_internal_trace_tags(text: str) -> str:
    cleaned = TRACE_LINE_RE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip() + "\n"


def cmd_finalize(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    assembly = _assemble(run)
    result = validate_paper_run(run, strict=True, require_final=False, require_audit=True)
    if not result["ok"]:
        write_json(run / "paper" / "PAPER_LINT.json", result)
        print("Finalization blocked by paper lint:")
        for item in result["errors"]:
            print(f"  - {item}")
        return 4
    draft_path = run / "paper" / "PAPER_DRAFT.md"
    final_path = run / "paper" / "FINAL_MANUSCRIPT.md"
    atomic_write_text(final_path, _strip_internal_trace_tags(draft_path.read_text(encoding="utf-8-sig")))
    build = {
        "schema_version": 2,
        "generated_at": now_iso(),
        "draft_sha256": sha256_file(draft_path),
        "final_sha256": sha256_file(final_path),
        "plan_projection_sha256": assembly["plan_projection_sha256"],
        "trace_tags_stripped": True,
    }
    write_json(run / "paper" / "PAPER_BUILD.json", build)
    final_check = validate_paper_run(run, strict=True, require_final=True, require_audit=True)
    write_json(run / "paper" / "PAPER_LINT.json", final_check)
    if not final_check["ok"]:
        print("Final manuscript created, but final lint failed:")
        for item in final_check["errors"]:
            print(f"  - {item}")
        return 4
    print("Final manuscript: paper/FINAL_MANUSCRIPT.md")
    print(build["final_sha256"])
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    paper = run / "paper"
    if not (paper / "PAPER_PLAN.json").exists():
        print("Paper workspace is not initialized. Run `python tools/paper.py init` after freeze.")
        return 0
    plan = _load_plan(run)
    claim_rows = read_csv(paper / "PAPER_CLAIM_MAP.csv")
    section_states = []
    for section in sorted(plan.get("sections", []), key=_section_order):
        try:
            path = _safe_section_path(run, section)
            placeholders = contains_placeholder(path, PLACEHOLDERS) if path.exists() else ["missing"]
        except ValueError:
            placeholders = ["invalid-path"]
        section_states.append((section.get("id"), section.get("title"), "ready" if not placeholders else "draft-required"))
    print(f"Working title: {plan.get('working_title','')}")
    print(f"Claims mapped: {sum(1 for row in claim_rows if row.get('target_section'))}/{len(claim_rows)}")
    for sid, title, state in section_states:
        print(f"- {sid:<24} {state:<14} {title}")
    return 0



def cmd_map_claim(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    plan = _load_plan(run)
    groups = _claim_groups(run)
    if args.claim not in groups:
        raise SystemExit(f"错误：未知 claim_id：{args.claim}")
    section = _section_by_id(plan, args.section)
    rows = read_csv(run / "paper" / "PAPER_CLAIM_MAP.csv")
    found = False
    for row in rows:
        if str(row.get("claim_id", "")) == args.claim:
            row["target_section"] = str(section["id"])
            row["trace_tag_status"] = "pending"
            if args.note:
                row["notes"] = args.note
            found = True
    if not found:
        group = groups[args.claim]
        rows.append({
            "claim_id": args.claim,
            "claim_text": group.get("claim_text", ""),
            "is_core": "true" if group.get("is_core") else "false",
            "target_section": str(section["id"]),
            "evidence_status": _evidence_status(run, group),
            "trace_tag_status": "pending",
            "notes": args.note or "",
        })
    for item in plan.get("sections", []):
        claims = [str(cid) for cid in item.get("claim_ids", []) if str(cid) != args.claim]
        if str(item.get("id")) == str(section["id"]):
            claims.append(args.claim)
        item["claim_ids"] = sorted(set(claims))
    plan["updated_at"] = now_iso()
    write_csv(run / "paper" / "PAPER_CLAIM_MAP.csv", CLAIM_MAP_FIELDS, rows)
    write_json(run / "paper" / "PAPER_PLAN.json", plan)
    atomic_write_text(run / "paper" / "PAPER_OUTLINE.md", _outline_text(plan, rows))
    print(f"Mapped {args.claim} -> {section['id']}")
    return 0


def cmd_section_set(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    plan = _load_plan(run)
    section = _section_by_id(plan, args.section)
    if args.status:
        section["status"] = args.status
    if args.writer is not None:
        section["writer"] = args.writer
    if args.note is not None:
        section["notes"] = args.note
    plan["updated_at"] = now_iso()
    write_json(run / "paper" / "PAPER_PLAN.json", plan)
    print(f"Updated section {args.section}: status={section.get('status')} writer={section.get('writer','')}")
    return 0


def cmd_audit_set(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    draft_path = run / "paper" / "PAPER_DRAFT.md"
    freeze_path = run / "FROZEN_MANIFEST.json"
    if args.status == "passed" and (not draft_path.exists() or not freeze_path.exists()):
        raise SystemExit("错误：status=passed 前必须存在当前 PAPER_DRAFT.md 与 FROZEN_MANIFEST.json")
    if not is_named_human_identity(args.by):
        raise SystemExit(
            "错误：paper audit 的 --by 必须为具名人工，不能使用 TEAM/UNKNOWN/TODO 等占位值"
        )
    audit_input_manifest, manifest_issues = _audit_input_manifest(run)
    if args.status == "passed":
        freeze = _freeze_check(run)
        if not freeze["ok"]:
            raise SystemExit(
                "错误：status=passed 前科学冻结必须仍有效："
                + "; ".join(freeze["issues"])
            )
        if manifest_issues:
            raise SystemExit(
                "错误：status=passed 的审计输入不完整：" + "; ".join(manifest_issues)
            )
        preflight = validate_paper_run(
            run, strict=True, require_final=False, require_audit=False
        )
        if not preflight["ok"]:
            raise SystemExit(
                "错误：status=passed 前论文必须通过 strict pre-audit 校验："
                + "; ".join(preflight["errors"])
            )
        consistency_path = run / "paper" / "CONSISTENCY_AUDIT.md"
        consistency_placeholders = contains_placeholder(consistency_path, PLACEHOLDERS)
        if consistency_placeholders:
            raise SystemExit(
                "错误：status=passed 前 CONSISTENCY_AUDIT.md 不能含占位符："
                + ", ".join(sorted(set(consistency_placeholders)))
            )
    payload = {
        "status": args.status,
        "checked_at": now_iso(),
        "checked_by": args.by,
        "blockers": list(args.blocker or []),
        "warnings": list(args.warning or []),
        "notes": args.note or "Human-reviewed paper audit state recorded through tools/paper.py.",
        "draft_sha256": sha256_file(draft_path) if draft_path.exists() else "",
        "freeze_manifest_sha256": sha256_file(freeze_path) if freeze_path.exists() else "",
        "audit_input_manifest": audit_input_manifest,
        "audit_input_sha256": _canonical_payload_sha256(audit_input_manifest),
    }
    if args.status == "passed" and payload["blockers"]:
        raise SystemExit("错误：status=passed 时 blockers 必须为空")
    write_json(run / "paper" / "PAPER_AUDIT.json", payload)
    print(f"Paper audit status: {args.status}; blockers={len(payload['blockers'])}")
    print(f"Audit input sha256: {payload['audit_input_sha256']}")
    return 0

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="在科学产物冻结后初始化论文写作工作区")
    p.add_argument("--run")
    p.add_argument("--replace", action="store_true")
    p.add_argument("--allow-unfrozen", action="store_true", help="仅用于演练/诊断；正式写作不建议")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("packet", help="为 section writer 生成受限证据写作包")
    p.add_argument("--run")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--section")
    group.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_packet)

    p = sub.add_parser("map-claim", help="把 claim 映射到论文 section，并同步 PAPER_PLAN 与 claim map")
    p.add_argument("--run")
    p.add_argument("--claim", required=True)
    p.add_argument("--section", required=True)
    p.add_argument("--note")
    p.set_defaults(func=cmd_map_claim)

    p = sub.add_parser("section-set", help="更新 section 的 drafting/review 状态和 writer")
    p.add_argument("--run")
    p.add_argument("--section", required=True)
    p.add_argument("--status", choices=["planned", "drafting", "drafted", "reviewed", "final"])
    p.add_argument("--writer")
    p.add_argument("--note")
    p.set_defaults(func=cmd_section_set)

    p = sub.add_parser("assemble", help="按 PAPER_PLAN 顺序从 section 文件组装 PAPER_DRAFT.md")
    p.add_argument("--run")
    p.set_defaults(func=cmd_assemble)

    p = sub.add_parser("audit-set", help="记录人工复核后的论文审计状态")
    p.add_argument("--run")
    p.add_argument("--status", required=True, choices=["pending", "blocked", "passed"])
    p.add_argument("--by", required=True)
    p.add_argument("--blocker", action="append")
    p.add_argument("--warning", action="append")
    p.add_argument("--note")
    p.set_defaults(func=cmd_audit_set)

    p = sub.add_parser("lint", help="检查论文完整性、claim/evidence 映射、冻结状态和组装一致性")
    p.add_argument("--run")
    p.add_argument("--strict", action="store_true")
    stage = p.add_mutually_exclusive_group()
    stage.add_argument("--pre-audit", action="store_true", help="送独立审稿前检查；不要求 audit passed 或 FINAL_MANUSCRIPT")
    stage.add_argument("--pre-final", action="store_true", help="审稿通过后的最终成稿前检查；要求 audit passed，但不要求 FINAL_MANUSCRIPT/PAPER_BUILD")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("finalize", help="严格检查后生成去除内部 trace tag 的 FINAL_MANUSCRIPT.md")
    p.add_argument("--run")
    p.set_defaults(func=cmd_finalize)

    p = sub.add_parser("status", help="显示论文各 section 与 claim 映射状态")
    p.add_argument("--run")
    p.set_defaults(func=cmd_status)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
