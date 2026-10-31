#!/usr/bin/env python3
"""Export a sanitized blind-review packet and import an independently produced review."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

from common import (
    ROOT,
    die,
    is_named_human_identity,
    is_timezone_aware_iso,
    now_iso,
    require_safe_component,
    sha256_file,
    write_json,
)
from v51_common import TEXT_SUFFIXES, read_text_safe, resolve_run_flexible, run_relative

INCLUDE_ROOTS = [
    "problem", "data/DATA_AUDIT.md", "data/DATA_MANIFEST.json",
    "interpretation", "sensitivity", "math", "research/modules",
    "synthesis/FINAL_MODEL_SPEC.md", "code", "results", "experiments",
    "evidence", "figures",
]
EXCLUDED_PARTS = {
    "reviews", "decisions", "selection", "ai", "external", "relay", "verifier",
    "packets", "blind", "claims", "diversity",
}
EXCLUDED_NAMES = {
    "DECISION_LOG.jsonl", "AI_USAGE_LOG.jsonl", "SYNTHESIS_REVIEW.md",
    "RED_TEAM_REPORT.md", "FEASIBILITY_REVIEW.md", "PAPER_AUDIT.json",
    "MATH_LINT.json",
}

PROMPT = """# Blind Independent Review Task

You are receiving a deliberately sanitized packet. You are not told which
candidate was preferred, how prior reviewers voted, or which model/provider
created the work.

Your job is to try to falsify the submitted modeling solution.

Evaluate at least:

1. Whether the operational interpretation actually matches the statement.
2. Whether alternative structural interpretations could change the answer.
3. Mathematical formulation, derivation conditions, units and limiting cases.
4. Whether terms such as upper bound, global optimum, convergence, robustness
   and significance have valid proof/evidence provenance.
5. Whether code implements the stated model and whether experiments can be replayed.
6. Simple baselines, counterexamples, extreme cases and missing uncertainty.
7. Claims that exceed the tested scope.

Return a report with:

- Critical findings
- Major findings
- Minor findings
- Reproducible tests or commands
- Required corrections
- Verdict: PASS / PASS_WITH_BLOCKERS / FAIL
- Blockers: NONE, or a concise semicolon-separated blocker list

Your RESPONSE.md must also copy the three exact binding lines from the
RESPONSE_TEMPLATE.md / RESPONSE_CONTRACT.json supplied next to the packet ZIP:

- Packet-SHA256: ...
- Run-ID: ...
- Review-ID: ...

Do not assign a vague confidence percentage. Cite packet paths and exact
formulas/results wherever possible.
"""

VERDICT_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*|[-*]\s*)?(?:\*\*)?Verdict(?::\*\*|\*\*:|:)\s*(?:\*\*)?"
    r"(PASS_WITH_BLOCKERS|PASS|FAIL)\b"
)
BLOCKERS_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*|[-*]\s*)?(?:\*\*)?Blockers?(?::\*\*|\*\*:|:)\s*(.+?)\s*$"
)
EMPTY_BLOCKERS = {"none", "none.", "[]", "no blockers", "no blocker", "无", "无。", "没有"}
INDEPENDENT_REVIEWERS = {"external-pro", "external-codex", "different-model", "human"}
RESPONSE_BINDING_HEADERS = ("Packet-SHA256", "Run-ID", "Review-ID")
RESPONSE_BINDING_RE = {
    header: re.compile(rf"(?m)^\s*{re.escape(header)}:\s*(\S+)\s*$")
    for header in RESPONSE_BINDING_HEADERS
}


def _root(run: Path, review_id: str, *, candidate: Path | None = None) -> Path:
    """Return one canonical direct child of ``reviews/blind``.

    ``require_safe_component`` rejects the syntactically dangerous dot
    segments.  The resolved-parent checks are deliberately repeated at the
    destructive call site so a symlink or caller-supplied path cannot turn an
    allowed identifier into an ancestor/sibling deletion target.
    """
    safe_id = require_safe_component(review_id, label="review-id")
    run_root = run.resolve()
    reviews_root = run_root / "reviews"
    namespace = reviews_root / "blind"
    for component in (reviews_root, namespace):
        if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
            die("review-id namespace 不能是符号链接或目录联接。")
    parent = namespace.resolve()
    try:
        parent.relative_to(run_root)
    except ValueError:
        die("review-id 目标根目录越出运行目录。")
    lexical_target = parent / safe_id
    if lexical_target.is_symlink() or getattr(lexical_target, "is_junction", lambda: False)():
        die("review-id 目标不能是符号链接或目录联接。")
    resolved = lexical_target.resolve()
    if candidate is not None and Path(candidate).resolve() != resolved:
        die("review-id 删除目标与已验证目录不一致。")
    if resolved.parent != parent or resolved.name != safe_id:
        die("review-id 目标必须是 reviews/blind 的直接子目录。")
    return resolved


def _is_excluded(relative: Path) -> bool:
    return any(part in EXCLUDED_PARTS for part in relative.parts) or relative.name in EXCLUDED_NAMES


def _scrub(text: str) -> str:
    replacements = [
        (r"\b(?:IDEA|CANDIDATE)[_-]?\d{1,4}\b", "ANON_ITEM"),
        (r"\b(?:winner|champion|selected candidate|top-ranked|ranked first)\b", "submitted model", re.I),
        (r"\b(?:OpenAI|ChatGPT|GPT(?:-[0-9.]+)?|Codex|Claude|Anthropic|DeepSeek|Gemini)\b", "MODEL_PROVIDER", re.I),
        (r"(?im)^.*(?:prior review|reviewer vote|model ranking|selection rationale).*$", "[SANITIZED PRIOR-SELECTION CONTEXT]"),
    ]
    out = text
    for item in replacements:
        if len(item) == 2:
            pattern, repl = item
            out = re.sub(pattern, repl, out)
        else:
            pattern, repl, flags = item
            out = re.sub(pattern, repl, out, flags=flags)
    return out


def _collect(run: Path) -> list[Path]:
    files: list[Path] = []
    for rel in INCLUDE_ROOTS:
        root = run / rel
        if root.is_file():
            if not _is_excluded(root.relative_to(run)):
                files.append(root)
        elif root.is_dir():
            for path in sorted(root.rglob("*")):
                if path.is_file() and not _is_excluded(path.relative_to(run)) and "__pycache__" not in path.parts:
                    files.append(path)
    # deterministic, unique
    return sorted(set(files), key=lambda p: p.relative_to(run).as_posix())


def _review_decision(text: str) -> tuple[str | None, str | None, bool]:
    """Return one structured verdict/blocker declaration and whether blockers are explicitly empty."""
    verdict_matches = list(VERDICT_RE.finditer(text))
    verdict = verdict_matches[0].group(1).upper() if len(verdict_matches) == 1 else None
    blocker_matches = list(BLOCKERS_RE.finditer(text))
    blockers = blocker_matches[0].group(1).strip().strip("*_`") if len(blocker_matches) == 1 else None
    blockers_empty = blockers is not None and blockers.strip().lower() in EMPTY_BLOCKERS
    return verdict, blockers, blockers_empty


def _response_binding(text: str) -> tuple[dict[str, str], list[str]]:
    """Read the exact packet identity declared by a blind-review response."""
    values: dict[str, str] = {}
    errors: list[str] = []
    for header, pattern in RESPONSE_BINDING_RE.items():
        matches = pattern.findall(text)
        if len(matches) != 1:
            errors.append(f"response must contain exactly one {header} binding line")
            continue
        values[header] = matches[0]
    digest = values.get("Packet-SHA256", "")
    if digest and not re.fullmatch(r"[0-9a-f]{64}", digest):
        errors.append("response Packet-SHA256 must be 64 lowercase hexadecimal characters")
    return values, errors


def _response_binding_contract(run: Path, review_id: str, packet_sha256: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "packet_sha256": packet_sha256,
        "run_id": run.name,
        "review_id": review_id,
        "required_response_headers": list(RESPONSE_BINDING_HEADERS),
    }


def _validate_contract(
    run: Path,
    root: Path,
    state: dict[str, Any],
    packet_sha256: str,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    relative = str(state.get("response_contract", "")).strip()
    if not relative:
        return {}, ["response binding contract path missing"]
    path = (run / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return {}, ["response binding contract path escapes the review directory"]
    if not path.is_file():
        return {}, ["response binding contract missing"]
    expected_hash = str(state.get("response_contract_sha256", ""))
    if not expected_hash or sha256_file(path) != expected_hash:
        errors.append("response binding contract hash mismatch")
    try:
        contract = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, errors + [f"response binding contract invalid: {exc}"]
    if not isinstance(contract, dict):
        return {}, errors + ["response binding contract must be a JSON object"]
    expected = _response_binding_contract(run, str(state.get("review_id", "")), packet_sha256)
    if set(contract) != set(expected):
        errors.append("response binding contract has unexpected or missing fields")
    for field in ("schema_version", "packet_sha256", "run_id", "review_id", "required_response_headers"):
        if contract.get(field) != expected[field]:
            errors.append(f"response binding contract {field} mismatch")

    template_relative = str(state.get("response_template", "")).strip()
    if not template_relative:
        errors.append("response template path missing")
        return contract, errors
    template_path = (run / template_relative).resolve()
    try:
        template_path.relative_to(root.resolve())
    except ValueError:
        errors.append("response template path escapes the review directory")
        return contract, errors
    if not template_path.is_file():
        errors.append("response template missing")
        return contract, errors
    template_hash = str(state.get("response_template_sha256", ""))
    if not template_hash or sha256_file(template_path) != template_hash:
        errors.append("response template hash mismatch")
    template_binding, template_errors = _response_binding(
        template_path.read_text(encoding="utf-8-sig", errors="replace")
    )
    errors.extend(f"response template: {item}" for item in template_errors)
    expected_template_binding = {
        "Packet-SHA256": packet_sha256,
        "Run-ID": run.name,
        "Review-ID": str(state.get("review_id", "")),
    }
    if template_binding and template_binding != expected_template_binding:
        errors.append("response template binding does not match the current packet")
    return contract, errors


def _source_freshness(run: Path, packet_zip: Path | None) -> tuple[list[str], dict[str, Any]]:
    """Bind a review to the exact current corpus, not merely to an old packet ZIP."""
    issues: list[str] = []
    if packet_zip is None or not packet_zip.is_file():
        return ["packet manifest missing"], {"source_count": 0, "current_count": 0}
    try:
        with zipfile.ZipFile(packet_zip, "r") as archive:
            manifest = json.loads(archive.read("PACKET_MANIFEST.json").decode("utf-8-sig"))
    except (OSError, KeyError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        return [f"packet manifest invalid: {exc}"], {"source_count": 0, "current_count": 0}
    if not isinstance(manifest, dict):
        return ["packet manifest must be a JSON object"], {"source_count": 0, "current_count": 0}

    source_rows = manifest.get("source_files", [])
    if not isinstance(source_rows, list):
        return ["packet manifest source_files must be a list"], {"source_count": 0, "current_count": 0}
    recorded: dict[str, str] = {}
    for item in source_rows:
        if not isinstance(item, dict):
            issues.append("packet manifest contains a non-object source record")
            continue
        relative = str(item.get("path", "")).strip()
        digest = str(item.get("source_sha256", "")).strip()
        if not relative or not digest:
            issues.append("packet manifest contains an incomplete source record")
            continue
        if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            issues.append(f"packet manifest contains invalid source hash: {relative}")
            continue
        if relative in recorded:
            issues.append(f"packet manifest contains duplicate source path: {relative}")
        recorded[relative] = digest

    current_files = _collect(run)
    current = {path.relative_to(run).as_posix(): sha256_file(path) for path in current_files}
    for relative in sorted(set(recorded) - set(current)):
        issues.append(f"reviewed source missing from current corpus: {relative}")
    for relative in sorted(set(current) - set(recorded)):
        issues.append(f"current corpus contains unreviewed source: {relative}")
    for relative in sorted(set(recorded) & set(current)):
        if recorded[relative] != current[relative]:
            issues.append(f"reviewed source changed after packet export: {relative}")
    return issues, {
        "source_count": len(recorded),
        "current_count": len(current),
        "manifest_review_id": manifest.get("review_id"),
        "manifest_run_id": manifest.get("run_id"),
    }


def cmd_export(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    root = _root(run, args.review_id)
    packet = root / "packet"
    zip_path = root / f"{args.review_id}_packet.zip"
    if root.exists() and any(root.iterdir()) and not args.overwrite:
        die(f"盲审目录已存在：{root}；使用 --overwrite。")
    if root.exists() and args.overwrite:
        # Re-canonicalize immediately before recursive deletion.  Do not rely
        # only on the earlier construction-time validation.
        delete_root = _root(run, args.review_id, candidate=root)
        shutil.rmtree(delete_root)
    packet.mkdir(parents=True)
    source_records = []
    packet_records = []
    for source in _collect(run):
        relative = source.relative_to(run)
        target = packet / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix.lower() in TEXT_SUFFIXES and source.stat().st_size <= args.max_text_mb * 1024 * 1024:
            text = _scrub(read_text_safe(source))
            target.write_text(text, encoding="utf-8")
            sanitized = True
        else:
            shutil.copy2(source, target)
            sanitized = False
        source_records.append({
            "path": relative.as_posix(), "source_sha256": sha256_file(source),
            "size_bytes": source.stat().st_size, "text_sanitized": sanitized,
        })
        packet_records.append({
            "path": relative.as_posix(), "packet_sha256": sha256_file(target),
            "size_bytes": target.stat().st_size,
        })
    (packet / "BLIND_REVIEW_PROMPT.md").write_text(PROMPT, encoding="utf-8")
    packet_records.append({
        "path": "BLIND_REVIEW_PROMPT.md", "packet_sha256": sha256_file(packet / "BLIND_REVIEW_PROMPT.md"),
        "size_bytes": (packet / "BLIND_REVIEW_PROMPT.md").stat().st_size,
    })
    manifest = {
        "schema_version": 1, "review_id": args.review_id, "generated_at": now_iso(),
        "run_id": run.name, "policy": {
            "excluded_parts": sorted(EXCLUDED_PARTS), "excluded_names": sorted(EXCLUDED_NAMES),
            "text_scrubbed": True, "reasoning_or_prior_reviews_excluded": True,
        },
        "source_files": source_records, "packet_files": packet_records,
        "response_binding": {
            "contract_version": 1,
            "required_headers": list(RESPONSE_BINDING_HEADERS),
            "value_source": "RESPONSE_CONTRACT.json sidecar next to the packet ZIP",
        },
        "limitations": [
            "Sanitization cannot guarantee the reviewer has never seen the task or solution elsewhere.",
            "Binary artifacts are copied without semantic redaction.",
            "The final model itself may reveal its mathematical lineage; blindness is about prior preference/history, not anonymity from content.",
        ],
    }
    write_json(packet / "PACKET_MANIFEST.json", manifest)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(packet.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(packet).as_posix())
    packet_sha256 = sha256_file(zip_path)
    contract_path = root / "RESPONSE_CONTRACT.json"
    contract = _response_binding_contract(run, args.review_id, packet_sha256)
    write_json(contract_path, contract)
    template_path = root / "RESPONSE_TEMPLATE.md"
    template_path.write_text(
        "# Blind independent review\n\n"
        f"Packet-SHA256: {packet_sha256}\n"
        f"Run-ID: {run.name}\n"
        f"Review-ID: {args.review_id}\n\n"
        "## Critical findings\n\n"
        "## Major findings\n\n"
        "## Minor findings\n\n"
        "## Reproducible tests or commands\n\n"
        "## Required corrections\n\n"
        "Verdict: FAIL\n"
        "Blockers: review not completed\n",
        encoding="utf-8",
    )
    meta = {
        "review_id": args.review_id, "packet_zip": zip_path.relative_to(run).as_posix(),
        "packet_sha256": packet_sha256, "generated_at": now_iso(),
        "response_contract": contract_path.relative_to(run).as_posix(),
        "response_contract_sha256": sha256_file(contract_path),
        "response_template": template_path.relative_to(run).as_posix(),
        "response_template_sha256": sha256_file(template_path),
        "status": "exported", "response": None,
    }
    write_json(root / "REVIEW_STATE.json", meta)
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


def _resolve_response(value: str) -> Path:
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = ROOT / p
    p = p.resolve()
    try:
        p.relative_to(ROOT.resolve())
    except ValueError:
        die(f"response 必须位于项目目录内：{p}")
    if not p.exists() or not p.is_file():
        die(f"response 文件不存在：{p}")
    return p


def cmd_import(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    root = _root(run, args.review_id)
    state_path = root / "REVIEW_STATE.json"
    if not state_path.exists():
        die("请先 export 盲审包。")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        die(f"REVIEW_STATE.json 无效：{exc}")
    if not isinstance(state, dict):
        die("REVIEW_STATE.json 必须是 JSON 对象")
    if state.get("review_id") != args.review_id:
        die("REVIEW_STATE.json review_id 与导入目标不一致。")
    packet_zip = (run / str(state.get("packet_zip", ""))).resolve()
    try:
        packet_zip.relative_to(root.resolve())
    except ValueError:
        die(f"盲审包路径越出 review 目录：{packet_zip}")
    if not packet_zip.is_file() or sha256_file(packet_zip) != state.get("packet_sha256"):
        die("盲审包缺失或哈希已变化。")
    packet_sha256 = sha256_file(packet_zip)
    _, contract_errors = _validate_contract(run, root, state, packet_sha256)
    if contract_errors:
        die("盲审 response 绑定契约无效：" + "; ".join(contract_errors))
    response = _resolve_response(args.response)
    response_text = response.read_text(encoding="utf-8-sig", errors="replace")
    response_binding, binding_errors = _response_binding(response_text)
    if binding_errors:
        die("盲审 response 绑定声明无效：" + "; ".join(binding_errors))
    expected_binding = {
        "Packet-SHA256": packet_sha256,
        "Run-ID": run.name,
        "Review-ID": args.review_id,
    }
    for field, expected in expected_binding.items():
        if response_binding.get(field) != expected:
            die(f"盲审 response {field} 与当前盲审包不一致。")
    if args.reviewer_kind == "internal-same-session" and args.fresh_context:
        die("internal-same-session 不能声明 fresh-context。")
    if args.fresh_context and not args.session_id.strip():
        die("fresh-context 必须提供非空 --session-id。")
    attested_by = str(args.attested_by or "").strip()
    if not is_named_human_identity(attested_by):
        die("--attested-by 必须是具名人工身份，不能使用 TEAM/UNKNOWN/TODO 等占位值。")
    destination = root / "RESPONSE.md"
    shutil.copy2(response, destination)
    meta = {
        "schema_version": 1, "review_id": args.review_id, "imported_at": now_iso(),
        "reviewer_kind": args.reviewer_kind, "session_id": args.session_id,
        "fresh_context_attested": bool(args.fresh_context), "attested_by": attested_by,
        "note": args.note or "", "packet_sha256": state.get("packet_sha256"),
        "response_binding": {
            "packet_sha256": response_binding["Packet-SHA256"],
            "run_id": response_binding["Run-ID"],
            "review_id": response_binding["Review-ID"],
        },
        "response_sha256": sha256_file(destination), "response_path": destination.relative_to(run).as_posix(),
    }
    write_json(root / "REVIEW_META.json", meta)
    state.update({"status": "imported", "response": meta})
    write_json(state_path, state)
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


def validate(
    run: Path,
    review_id: str,
    *,
    strict: bool,
    persist: bool = True,
) -> tuple[dict[str, Any], int]:
    root = _root(run, review_id)
    errors: list[str] = []
    warnings: list[str] = []
    state_path = root / "REVIEW_STATE.json"
    meta_path = root / "REVIEW_META.json"
    response_path = root / "RESPONSE.md"
    packet_sha256 = ""
    zip_path: Path | None = None
    contract: dict[str, Any] = {}
    if not state_path.exists():
        errors.append("REVIEW_STATE.json missing")
        state = {}
    else:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"REVIEW_STATE.json invalid: {exc}")
            state = {}
        if not isinstance(state, dict):
            errors.append("REVIEW_STATE.json must be a JSON object")
            state = {}
        candidate_zip = (run / str(state.get("packet_zip", ""))).resolve()
        try:
            candidate_zip.relative_to(root.resolve())
        except ValueError:
            errors.append("packet ZIP path escapes the review directory")
        else:
            zip_path = candidate_zip
        if zip_path is None or not zip_path.is_file():
            errors.append("packet ZIP missing")
        else:
            packet_sha256 = sha256_file(zip_path)
            if packet_sha256 != state.get("packet_sha256"):
                errors.append("packet ZIP hash mismatch")
        if state.get("review_id") != review_id:
            errors.append("REVIEW_STATE.json review_id mismatch")
        if zip_path is not None and zip_path.is_file():
            contract, contract_errors = _validate_contract(run, root, state, packet_sha256)
            errors.extend(contract_errors)
    if not meta_path.exists():
        errors.append("REVIEW_META.json missing")
        meta = {}
    else:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"REVIEW_META.json invalid: {exc}")
            meta = {}
        if not isinstance(meta, dict):
            errors.append("REVIEW_META.json must be a JSON object")
            meta = {}
        if meta.get("review_id") != review_id:
            errors.append("REVIEW_META.json review_id mismatch")
        if state and meta.get("packet_sha256") != state.get("packet_sha256"):
            errors.append("review metadata is not bound to this packet hash")
        if not is_named_human_identity(meta.get("attested_by")):
            errors.append("human attestor must be a named human, not a placeholder")
        if not is_timezone_aware_iso(meta.get("imported_at")):
            errors.append("review imported_at must be an ISO-8601 timestamp with timezone")
    response_binding: dict[str, str] = {}
    if not response_path.is_file() or response_path.stat().st_size < 100:
        errors.append("blind response missing or too short")
    elif meta and sha256_file(response_path) != meta.get("response_sha256"):
        errors.append("blind response hash mismatch")
    verdict: str | None = None
    blockers: str | None = None
    blockers_empty = False
    if response_path.exists() and response_path.is_file():
        response_text = response_path.read_text(encoding="utf-8-sig", errors="replace")
        response_binding, binding_errors = _response_binding(response_text)
        errors.extend(binding_errors)
        expected_binding = {
            "Packet-SHA256": packet_sha256,
            "Run-ID": run.name,
            "Review-ID": review_id,
        }
        for field, expected in expected_binding.items():
            if response_binding.get(field) and response_binding[field] != expected:
                errors.append(f"blind response {field} does not match the current packet")
        if meta:
            expected_meta_binding = {
                "packet_sha256": response_binding.get("Packet-SHA256", ""),
                "run_id": response_binding.get("Run-ID", ""),
                "review_id": response_binding.get("Review-ID", ""),
            }
            if meta.get("response_binding") != expected_meta_binding:
                errors.append("review metadata response_binding mismatch")
        verdict, blockers, blockers_empty = _review_decision(
            response_text
        )
    freshness_issues, freshness = _source_freshness(run, zip_path)
    if strict:
        if state.get("status") != "imported":
            errors.append("review state is not imported")
        if meta.get("fresh_context_attested") is not True:
            errors.append("fresh-context attestation missing")
        if meta.get("reviewer_kind") not in INDEPENDENT_REVIEWERS:
            errors.append("reviewer is not independent")
        if not str(meta.get("session_id", "")).strip():
            errors.append("independent review session_id missing")
        if verdict is None:
            errors.append("exactly one structured Verdict line is required")
        elif verdict != "PASS":
            errors.append(f"blind-review verdict is blocking: {verdict}")
        if blockers is None:
            errors.append("structured Blockers line is required")
        elif not blockers_empty:
            errors.append(f"blind review still has blockers: {blockers}")
        if freshness.get("source_count", 0) == 0:
            errors.append("blind-review packet contains no source corpus")
        if freshness.get("manifest_review_id") != review_id:
            errors.append("packet manifest review_id mismatch")
        if freshness.get("manifest_run_id") != run.name:
            errors.append("packet manifest run_id mismatch")
        errors.extend(freshness_issues)
    elif freshness_issues:
        warnings.extend(freshness_issues)
    if meta.get("fresh_context_attested"):
        warnings.append("Fresh-context status is an attestation, not independently verifiable by this tool.")
    result = {
        "generated_at": now_iso(), "review_id": review_id, "strict": strict,
        "verdict": verdict, "blockers": blockers, "blockers_empty": blockers_empty,
        "packet_sha256": packet_sha256, "response_binding": response_binding,
        "response_contract": contract, "source_freshness": freshness,
        "errors": errors, "warnings": warnings, "passed": not errors,
    }
    if persist:
        write_json(root / "VALIDATION.json", result)
    return result, (0 if not errors else 3)


def cmd_validate(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    result, code = validate(run, args.review_id, strict=args.strict)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("export")
    p.add_argument("--run"); p.add_argument("--review-id", required=True); p.add_argument("--overwrite", action="store_true")
    p.add_argument("--max-text-mb", type=int, default=5)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import")
    p.add_argument("--run"); p.add_argument("--review-id", required=True); p.add_argument("--response", required=True)
    p.add_argument("--reviewer-kind", choices=["external-pro", "external-codex", "different-model", "human", "internal-same-session"], required=True)
    p.add_argument("--session-id", default=""); p.add_argument("--fresh-context", action="store_true")
    p.add_argument("--attested-by", required=True); p.add_argument("--note")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("validate")
    p.add_argument("--run"); p.add_argument("--review-id", required=True); p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_validate)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
