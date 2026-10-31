#!/usr/bin/env python3
"""Detect strong mathematical claims and enforce appropriate evidence provenance.

The scanner is deliberately conservative: it flags language for review.  It
does not decide whether a theorem is true.  A numerical search, finite sample,
or LLM judgment cannot by itself certify a rigorous bound or global optimum.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from pathlib import Path
from typing import Any

from common import (
    PLACEHOLDER_HUMAN_IDENTITIES,
    ROOT,
    die,
    is_named_human_identity,
    is_timezone_aware_iso,
    now_iso,
    read_csv,
    resolve_run_evidence_ref,
    sha256_file,
    write_csv,
    write_json,
)
from v51_common import iter_text_files, read_text_safe, resolve_run_flexible, split_values

FIELDS = [
    "claim_id", "path", "line", "claim_type", "matched_phrase", "excerpt",
    "evidence_type", "evidence_ref", "status", "recommended_wording",
    "reviewed_by", "reviewed_at", "notes",
]
# Descriptive alias used by the artifact schema registry.  Keep ``FIELDS`` as
# the public compatibility name for existing callers.
CLAIM_FIELDS = FIELDS

CLAIM_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("rigorous_upper_bound", re.compile(r"理论上界|严格上界|rigorous\s+upper\s+bound|certified\s+upper\s+bound", re.I)),
    ("rigorous_lower_bound", re.compile(r"理论下界|严格下界|rigorous\s+lower\s+bound|certified\s+lower\s+bound", re.I)),
    (
        "global_optimum",
        re.compile(
            r"全局最优(?:解|值)?|global(?:ly)?\s+optimal\b|global\s+optimum\b",
            re.I,
        ),
    ),
    (
        "unique_solution",
        re.compile(
            r"唯一(?:最优)?解|解(?:具有|满足)唯一性|唯一性(?:成立|已(?:经)?证明|得到保证)|"
            r"unique\s+(?:optimal\s+)?solution|(?:proved|established|guaranteed)\s+uniqueness|"
            r"uniqueness\s+(?:holds|is\s+(?:proved|established|guaranteed))",
            re.I,
        ),
    ),
    ("convergence_guarantee", re.compile(r"保证收敛|必然收敛|收敛保证|guaranteed\s+convergence|converges\s+for\s+all", re.I)),
    ("stability_guarantee", re.compile(r"稳定性保证|保证稳定|globally\s+stable|guaranteed\s+stability", re.I)),
    (
        "statistical_significance",
        re.compile(
            r"统计(?:学)?(?:上)?显著(?!性(?:检验|水平|阈值|分析))(?:优于|提高|降低|差异)?|"
            r"statistically\s+significant|statistically\s+significantly\s+(?:better|higher|lower)",
            re.I,
        ),
    ),
    ("robustness_guarantee", re.compile(r"保证鲁棒|鲁棒性保证|certified\s+robust|robust\s+for\s+all", re.I)),
    (
        "proof_claim",
        re.compile(
            r"严格证明|已经证明|由此(?:严格)?证明(?:了)?(?:全局|唯一|严格|必然|保证)|"
            r"we\s+(?:rigorously\s+)?prove|this\s+(?:rigorously\s+)?proves|therefore\s+proved",
            re.I,
        ),
    ),
    ("optimality_gap", re.compile(r"最优性间隙|optimality\s+gap", re.I)),
]

PROVENANCE_CONFIG = ROOT / "config" / "claim_provenance.json"


def _load_provenance_policy() -> tuple[dict[str, Any], str]:
    try:
        raw = PROVENANCE_CONFIG.read_bytes()
        payload = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        die(f"强主张来源策略无效：{PROVENANCE_CONFIG}: {exc}")
    if not isinstance(payload, dict) or not isinstance(payload.get("claim_types"), dict):
        die(f"强主张来源策略缺少 claim_types：{PROVENANCE_CONFIG}")
    return payload, hashlib.sha256(raw).hexdigest()


_PROVENANCE_POLICY, _PROVENANCE_CONFIG_SHA256_AT_LOAD = _load_provenance_policy()
_SCANNER_SOURCE_SHA256_AT_LOAD = sha256_file(Path(__file__).resolve())
_CLAIM_TYPES = _PROVENANCE_POLICY["claim_types"]
ALLOWED: dict[str, set[str]] = {
    claim_type: set(item.get("allowed_evidence_types", []))
    for claim_type, item in _CLAIM_TYPES.items()
    if isinstance(item, dict)
}
RECOMMENDED: dict[str, str] = {
    claim_type: str(item.get("recommended_downgrade", "")).strip()
    for claim_type, item in _CLAIM_TYPES.items()
    if isinstance(item, dict)
}
WEAK_EVIDENCE = set(_PROVENANCE_POLICY.get("weak_evidence_types", []))
PLACEHOLDER_REVIEWERS = PLACEHOLDER_HUMAN_IDENTITIES

missing_policy = sorted({claim_type for claim_type, _ in CLAIM_RULES} - set(ALLOWED))
if missing_policy:
    die("claim_provenance.json 缺少规则类型：" + ", ".join(missing_policy))


def _assert_cached_policy_current() -> None:
    if sha256_file(PROVENANCE_CONFIG) != _PROVENANCE_CONFIG_SHA256_AT_LOAD:
        die(
            "config/claim_provenance.json changed after this process loaded it; "
            "restart/reload before scanning or linting strong claims"
        )
    if sha256_file(Path(__file__).resolve()) != _SCANNER_SOURCE_SHA256_AT_LOAD:
        die(
            "tools/claim_guard.py changed while this process is running; "
            "restart/reload before scanning or linting strong claims"
        )


SCOPE_ROOTS: dict[str, list[str]] = {
    "all": [
        "problem", "data/DATA_AUDIT.md", "data/DATA_MANIFEST.json",
        "interpretation", "sensitivity", "math", "research/modules",
        "synthesis/FINAL_MODEL_SPEC.md", "code", "results", "experiments",
        "evidence", "figures",
    ],
    "math": ["research/modules", "synthesis"],
    "paper": ["paper/sections"],
}
EXCLUDED_PARTS = {"__pycache__", ".git"}
EXCLUDED_NAMES = {
    "MATH_LINT.json",
    "STRONG_CLAIM_REGISTER.csv",
    "STRONG_CLAIM_SCAN_MATH.json",
    "STRONG_CLAIM_SCAN_G5_FULL.json",
    "STRONG_CLAIMS_G5_FULL.csv",
}
CANONICAL_SUFFIXES = {".csv", ".json", ".md", ".py", ".tex", ".txt"}
DIRECT_EVIDENCE_FORBIDDEN_PREFIXES = (
    "claims/", "reviews/", "consistency/", "decisions/", "ai/",
    "deliverables/", "sources/", "paper/",
)
DIRECT_EVIDENCE_FORBIDDEN_NAMES = {
    "STATUS.JSON", "V51_STATUS.JSON", "RUN_META.JSON", "BUDGET.JSON",
    "FROZEN_MANIFEST.JSON", "INPUT_MANIFEST.JSON", "DATA_MANIFEST.JSON",
    "CLAIM_EVIDENCE_LEDGER.CSV", "SOURCE_LEDGER.CSV",
    "VALIDATION_REPORT.JSON", "FREEZE_VERIFICATION.JSON", "DECISION_LOG.JSONL",
    "EXPERIMENT_LOG.CSV", "METRICS.CSV", "BASELINE_REGISTRY.CSV",
}

# These patterns suppress explicit disclaimers, not merely hedged assertions.
# The matched strong phrase must occur in the same short clause as the negation.
NEGATION_PATTERNS = [
    r"(?:并非|不是|不能|无法|不足以)\s*{phrase}",
    r"(?:并非|不是|不构成)\s*(?:严格意义上的?)?\s*{phrase}",
    r"(?:不能|无法|不声称|未声称)\s*(?:由此|据此|仅凭[^，。；;]{0,10})?\s*(?:严格)?(?:证明|保证|认证|确认|达到|获得)(?:了)?\s*{phrase}",
    r"(?:我们)?(?:并不|不|未|无意)(?:声称|主张|认为)[^，。；;.!?！？]{0,32}{phrase}",
    r"{phrase}\s*(?:(?:相对)?(?:基线|对照|现有方法)(?:的)?)?(?:结论|说法|主张)?\s*(?:尚未(?:得到|被)?证明|未被证明|不能保证|无法保证|不成立)",
    r"\b(?:not|never)\s+(?:a\s+)?{phrase}\b",
    r"\bwe\s+(?:do\s+not|don't|cannot|can't)\s+(?:claim|assert|contend)\s+(?:that\s+)?[^.;!?]{0,32}{phrase}\b",
    r"\b(?:cannot|can't|does\s+not|do\s+not)\s+(?:rigorously\s+)?(?:prove|establish|guarantee|certify)\s+(?:that\s+)?{phrase}\b",
    r"{phrase}\s+(?:(?:is|are|was|were|remains?)\s+)?(?:unproven|unknown|open|not\s+established|not\s+guaranteed)\b",
]


def _registry_path(run: Path, scope: str | None = None) -> Path:
    # The pre-freeze scientific audit must survive the later, full manuscript
    # scan.  A dedicated math registry prevents the canonical all-scope scan
    # from overwriting the exact artifact that G5 approved.
    name = "STRONG_CLAIMS_MATH.csv" if scope == "math" else "STRONG_CLAIMS.csv"
    return run / "claims" / name


def _receipt_path(run: Path, scope: str | None = None) -> Path:
    name = (
        "STRONG_CLAIM_SCAN_MATH.json"
        if scope == "math"
        else "STRONG_CLAIM_SCAN.json"
    )
    return run / "claims" / name


def _rules_hash() -> str:
    _assert_cached_policy_current()
    payload = {
        "claim_rules": [
            {"claim_type": claim_type, "pattern": pattern.pattern, "flags": pattern.flags}
            for claim_type, pattern in CLAIM_RULES
        ],
        "negation_patterns": NEGATION_PATTERNS,
        "scope_roots": SCOPE_ROOTS,
        "excluded_parts": sorted(EXCLUDED_PARTS),
        "excluded_names": sorted(EXCLUDED_NAMES),
        "canonical_suffixes": sorted(CANONICAL_SUFFIXES),
        "provenance_config_sha256": _PROVENANCE_CONFIG_SHA256_AT_LOAD,
        "scanner_source_sha256": _SCANNER_SOURCE_SHA256_AT_LOAD,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _discover_scope(
    run: Path, scope: str, explicit_paths: list[str]
) -> tuple[list[Path], dict[str, Any]]:
    run_root = run.resolve()
    roots: list[Path] = []
    missing_explicit: list[str] = []
    missing_roots: list[str] = []
    rejected_outside: list[str] = []
    requested = [str(value) for value in explicit_paths]

    if explicit_paths:
        for value in explicit_paths:
            raw = Path(value).expanduser()
            candidate = (raw if raw.is_absolute() else run / raw).resolve()
            try:
                candidate.relative_to(run_root)
            except ValueError:
                rejected_outside.append(str(value))
                continue
            if not candidate.exists():
                missing_explicit.append(str(value))
                continue
            roots.append(candidate)
    else:
        for relative in SCOPE_ROOTS[scope]:
            candidate = run / relative
            if not candidate.exists():
                missing_roots.append(relative)
            else:
                roots.append(candidate)

    files: list[Path] = []
    for root in roots:
        candidates = [root] if root.is_file() else list(iter_text_files(root, excluded_parts=EXCLUDED_PARTS))
        for path in candidates:
            if path.name in EXCLUDED_NAMES:
                continue
            if not explicit_paths and path.suffix.lower() not in CANONICAL_SUFFIXES:
                continue
            resolved = path.resolve()
            try:
                resolved.relative_to(run_root)
            except ValueError:
                rejected_outside.append(str(path))
                continue
            files.append(resolved)

    metadata = {
        "scope": scope,
        "canonical_roots": list(SCOPE_ROOTS[scope]),
        "canonical_suffixes": sorted(CANONICAL_SUFFIXES),
        "excluded_parts": sorted(EXCLUDED_PARTS),
        "excluded_names": sorted(EXCLUDED_NAMES),
        "requested_paths": requested,
        "missing_explicit_paths": sorted(set(missing_explicit)),
        "missing_canonical_roots": sorted(set(missing_roots)),
        "rejected_outside_paths": sorted(set(rejected_outside)),
        "complete": not missing_explicit and not missing_roots and not rejected_outside,
        "canonical_complete": not explicit_paths and not missing_roots and not rejected_outside,
    }
    return sorted(set(files)), metadata


def _file_manifest(run: Path, files: list[Path]) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(run.resolve()).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in files
    ]


def _manifest_hash(records: list[dict[str, Any]]) -> str:
    encoded = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _explicitly_negated(line: str, match: re.Match[str]) -> bool:
    # Bind the negation test to this exact occurrence.  Reusing the raw phrase
    # could let an earlier negated occurrence suppress a later positive claim
    # on the same line ("not globally optimal; ours is globally optimal").
    hard_boundaries = "。；;.!?！？"
    start = max((line.rfind(mark, 0, match.start()) for mark in hard_boundaries), default=-1) + 1
    right_edges = [
        position
        for mark in hard_boundaries
        if (position := line.find(mark, match.end())) >= 0
    ]
    end = min(right_edges) if right_edges else len(line)
    clause = line[start:end]
    relative_start = match.start() - start
    relative_end = match.end() - start
    marker = "CLAIM_GUARD_TARGET"
    marked_clause = clause[:relative_start] + marker + clause[relative_end:]
    phrase = re.escape(marker)
    return any(
        re.search(pattern.replace("{phrase}", phrase), marked_clause, re.I)
        for pattern in NEGATION_PATTERNS
    )


def _claim_id(
    relative: str,
    line_no: int,
    claim_type: str,
    source_line: str,
    match: re.Match[str],
) -> str:
    """Identify the complete claim context, not a truncated display excerpt."""
    normalized = re.sub(r"\s+", " ", source_line.strip())
    digest = hashlib.sha256(
        (
            f"{relative}|{line_no}|{claim_type}|{match.start()}|{match.end()}|"
            f"{match.group(0)}|{normalized}"
        ).encode("utf-8")
    ).hexdigest()[:10].upper()
    return f"CLM-{digest}"


def _valid_review_time(value: Any) -> bool:
    return is_timezone_aware_iso(value)


def _strip_html_comments(line: str, in_comment: bool) -> tuple[str, bool]:
    """Remove Markdown HTML comments while preserving visible text on the line."""
    visible: list[str] = []
    cursor = 0
    while cursor < len(line):
        if in_comment:
            end = line.find("-->", cursor)
            if end < 0:
                return "".join(visible), True
            cursor = end + 3
            in_comment = False
            continue
        start = line.find("<!--", cursor)
        if start < 0:
            visible.append(line[cursor:])
            break
        visible.append(line[cursor:start])
        cursor = start + 4
        in_comment = True
    return "".join(visible), in_comment


def _opening_fence(line: str) -> tuple[str, int] | None:
    """Return the CommonMark fence character/length for an opening fence."""
    match = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$", line)
    if match is None:
        return None
    marker, info = match.groups()
    if marker[0] == "`" and "`" in info:
        return None
    return marker[0], len(marker)


def _is_closing_fence(line: str, fence: tuple[str, int]) -> bool:
    char, minimum = fence
    return re.fullmatch(rf"[ \t]{{0,3}}{re.escape(char)}{{{minimum},}}[ \t]*", line) is not None


def _visible_markdown_text(line: str) -> str:
    """Normalize common inline Markdown without exposing link destinations."""
    text = re.sub(r"<https?://[^>]+>", "", line, flags=re.IGNORECASE)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", text)
    text = re.sub(r"</?[A-Za-z][^>]*>", "", text)
    text = re.sub(r"(?<!\\)[*_~`]+", "", text)
    text = re.sub(r"\\([\\`*{}\[\]()#+\-.!_>~|])", r"\1", text)
    return html.unescape(text)


def cmd_scan(args: argparse.Namespace) -> int:
    _assert_cached_policy_current()
    run = resolve_run_flexible(args.run)
    registry = _registry_path(run, args.scope)
    receipt_path = _receipt_path(run, args.scope)
    explicit_paths = list(getattr(args, "path", []) or [])
    files, discovery = _discover_scope(run, args.scope, explicit_paths)
    manifest_before = _file_manifest(run, files)
    scan_errors = []
    inherited_math_rows: list[dict[str, str]] = []
    if discovery["missing_explicit_paths"]:
        scan_errors.append("Explicit scan paths do not exist: " + ", ".join(discovery["missing_explicit_paths"]))
    if discovery["missing_canonical_roots"]:
        scan_errors.append("Canonical scan roots do not exist: " + ", ".join(discovery["missing_canonical_roots"]))
    if discovery["rejected_outside_paths"]:
        scan_errors.append("Scan paths leave the run directory: " + ", ".join(discovery["rejected_outside_paths"]))
    if args.scope == "all":
        math_registry = _registry_path(run, "math")
        math_receipt = _receipt_path(run, "math")
        if math_registry.exists() or math_receipt.exists():
            if not math_registry.is_file() or not math_receipt.is_file():
                scan_errors.append(
                    "Math-scope provenance is incomplete; both "
                    "STRONG_CLAIMS_MATH.csv and STRONG_CLAIM_SCAN_MATH.json are required"
                )
            else:
                try:
                    inherited_math_rows = read_csv(
                        math_registry, expected_fields=FIELDS
                    )
                except SystemExit as exc:
                    scan_errors.append(f"Math-scope registry is invalid: {exc}")
                    inherited_math_rows = []
                if inherited_math_rows or not scan_errors:
                    _, math_receipt_errors = _validate_scan_receipt(
                        run,
                        math_registry,
                        inherited_math_rows,
                        "math",
                        receipt_path=math_receipt,
                    )
                    scan_errors.extend(
                        f"Math-scope provenance is stale: {item}"
                        for item in math_receipt_errors
                    )
    if scan_errors:
        summary = {
            "schema_version": 2,
            "run_id": run.name,
            "generated_at": now_iso(),
            **discovery,
            "passed": False,
            "errors": scan_errors,
            "rules_sha256": _rules_hash(),
            "provenance_config_sha256": _PROVENANCE_CONFIG_SHA256_AT_LOAD,
            "files": manifest_before,
            "file_count": len(manifest_before),
            "corpus_sha256": _manifest_hash(manifest_before),
            "claim_count": None,
            "open_count": None,
            "registry": registry.relative_to(run).as_posix(),
            "registry_sha256": sha256_file(registry) if registry.exists() else "",
        }
        write_json(receipt_path, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 3

    existing_rows = read_csv(registry, expected_fields=FIELDS) if registry.exists() else []
    existing = {row.get("claim_id", ""): row for row in existing_rows}
    if args.scope == "all":
        # Math claims have already been reviewed before G5.  Reuse that human
        # provenance in the later all-corpus scan when the same stable claim ID
        # is rediscovered, while never letting an open math row overwrite a
        # previously reviewed all-scope row.
        if inherited_math_rows:
            for math_row in inherited_math_rows:
                claim_id = str(math_row.get("claim_id", "")).strip()
                if (
                    claim_id
                    and math_row.get("status", "open") != "open"
                    and str(math_row.get("reviewed_by", "")).strip()
                    and _valid_review_time(math_row.get("reviewed_at"))
                ):
                    existing[claim_id] = math_row
    rows: list[dict[str, str]] = []
    for path in files:
        relative = path.relative_to(run.resolve()).as_posix()
        text = read_text_safe(path)
        active_fence: tuple[str, int] | None = None
        in_html_comment = False
        for line_no, line in enumerate(text.splitlines(), start=1):
            if active_fence is not None:
                if _is_closing_fence(line, active_fence):
                    active_fence = None
                continue
            visible_line, in_html_comment = _strip_html_comments(
                line, in_html_comment
            )
            opening_fence = _opening_fence(visible_line)
            if opening_fence is not None:
                active_fence = opening_fence
                continue
            visible_line = _visible_markdown_text(visible_line)
            stripped = visible_line.strip()
            if not stripped:
                continue
            for claim_type, pattern in CLAIM_RULES:
                match = next(
                    (
                        candidate
                        for candidate in pattern.finditer(visible_line)
                        if not _explicitly_negated(visible_line, candidate)
                    ),
                    None,
                )
                if match is None:
                    continue
                excerpt = stripped[:500]
                cid = _claim_id(
                    relative, line_no, claim_type, visible_line, match
                )
                prior = existing.get(cid, {})
                current_identity = {
                    "path": relative,
                    "line": str(line_no),
                    "claim_type": claim_type,
                    "matched_phrase": match.group(0),
                    "excerpt": excerpt,
                }
                if prior and any(
                    str(prior.get(field, "")) != value
                    for field, value in current_identity.items()
                ):
                    # Even an accidental/truncated-ID collision may never carry
                    # a prior human decision onto different visible wording.
                    prior = {}
                row = {
                    "claim_id": cid,
                    **current_identity,
                    "evidence_type": prior.get("evidence_type", ""),
                    "evidence_ref": prior.get("evidence_ref", ""),
                    "status": prior.get("status", "open"),
                    "recommended_wording": prior.get("recommended_wording", RECOMMENDED.get(claim_type, "")),
                    "reviewed_by": prior.get("reviewed_by", ""),
                    "reviewed_at": prior.get("reviewed_at", ""),
                    "notes": prior.get("notes", ""),
                }
                rows.append(row)
    manifest_after = _file_manifest(run, files)
    if manifest_after != manifest_before:
        summary = {
            "schema_version": 2,
            "run_id": run.name,
            "generated_at": now_iso(),
            **discovery,
            "passed": False,
            "errors": ["Canonical corpus changed while it was being scanned; rerun the scan."],
            "rules_sha256": _rules_hash(),
            "provenance_config_sha256": _PROVENANCE_CONFIG_SHA256_AT_LOAD,
            "files": manifest_after,
            "file_count": len(manifest_after),
            "corpus_sha256": _manifest_hash(manifest_after),
            "claim_count": None,
            "open_count": None,
            "registry": registry.relative_to(run).as_posix(),
            "registry_sha256": sha256_file(registry) if registry.exists() else "",
        }
        write_json(receipt_path, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 3
    rows.sort(key=lambda r: (r["path"], int(r["line"]), r["claim_type"]))
    write_csv(registry, FIELDS, rows)
    summary = {
        "schema_version": 2,
        "run_id": run.name,
        "generated_at": now_iso(),
        **discovery,
        "passed": True,
        "errors": [],
        "rules_sha256": _rules_hash(),
        "provenance_config_sha256": _PROVENANCE_CONFIG_SHA256_AT_LOAD,
        "files": manifest_after,
        "file_count": len(manifest_after),
        "corpus_sha256": _manifest_hash(manifest_after),
        "claim_count": len(rows),
        "open_count": sum(1 for r in rows if r.get("status") == "open"),
        "registry": registry.relative_to(run).as_posix(),
        "registry_sha256": sha256_file(registry),
    }
    write_json(receipt_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _evidence_tokens(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return split_values(text)
        if isinstance(payload, list):
            return [str(item).strip() for item in payload if str(item).strip()]
    return split_values(text)


def _resolve_evidence_ref(run: Path, value: str) -> tuple[list[dict[str, str]], list[str]]:
    resolved: list[dict[str, str]] = []
    errors: list[str] = []
    run_root = run.resolve()

    for token in _evidence_tokens(value):
        candidate_token = token.split("#", 1)[0]
        items, item_errors = resolve_run_evidence_ref(run, candidate_token)
        errors.extend(item_errors)
        for item in items:
            relative = str(item.get("path", "")).strip()
            candidate = (run / relative).resolve()
            via_ledger = bool(str(item.get("evidence_id", "")).strip())
            if not via_ledger:
                try:
                    candidate.relative_to(run_root)
                except ValueError:
                    errors.append(f"evidence reference leaves the run directory: {token}")
                    continue
            upper_name = candidate.name.upper()
            management_shaped = (
                relative.startswith(DIRECT_EVIDENCE_FORBIDDEN_PREFIXES)
                or upper_name in DIRECT_EVIDENCE_FORBIDDEN_NAMES
                or candidate.stem.upper() in {
                    "AUDIT", "REGISTER", "LEDGER", "MANIFEST", "VALIDATION",
                    "STATUS", "CHECKLIST", "RECEIPT",
                }
                or any(
                    marker in upper_name
                    for marker in (
                        "_AUDIT.", "_REGISTER.", "_LEDGER.", "_MANIFEST.",
                        "_VALIDATION.", "_CHECKLIST.", "_STATUS.", "_RECEIPT.",
                    )
                )
            )
            if not via_ledger and management_shaped:
                errors.append(
                    f"management/registry artifact cannot directly support a strong claim: {relative}"
                )
                continue
            resolved.append({**item, "reference": token})
    return resolved, errors


def _refresh_receipt_registry_binding(
    run: Path, rows: list[dict[str, str]], *, scope: str | None = None
) -> None:
    receipt_path = _receipt_path(run, scope)
    if not receipt_path.exists():
        die(f"缺少 {receipt_path.name}；先完成扫描再设置来源。")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        die(f"{receipt_path.name} 无效：{exc}")
    registry = _registry_path(run, scope)
    receipt["registry_sha256"] = sha256_file(registry)
    receipt["registry_bound_at"] = now_iso()
    receipt["claim_count"] = len(rows)
    receipt["open_count"] = sum(1 for row in rows if row.get("status", "open") == "open")
    write_json(receipt_path, receipt)


def cmd_provenance_set(args: argparse.Namespace) -> int:
    _assert_cached_policy_current()
    run = resolve_run_flexible(args.run)
    reviewer = str(args.by or "").strip()
    if not reviewer or reviewer.upper() in PLACEHOLDER_REVIEWERS:
        die(
            "provenance-set --by 必须是具名 reviewer，"
            "不能使用 TEAM/UNKNOWN/TODO 等占位值"
        )
    scope = getattr(args, "scope", None)
    registry = _registry_path(run, scope)
    if not registry.exists():
        die(f"缺少 {registry.name}；先运行 claim_guard.py scan --scope {scope or 'all'}。")
    receipt_path = _receipt_path(run, scope)
    if not receipt_path.exists():
        die(f"缺少 {receipt_path.name}；先运行 claim_guard.py scan。")
    try:
        receipt_payload = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        die(f"{receipt_path.name} 无效：{exc}")
    if not isinstance(receipt_payload, dict) or not receipt_payload.get("passed"):
        die("最近一次 strong-claim scan 未成功；修复扫描错误后重试。")
    rows = read_csv(registry, expected_fields=FIELDS)
    receipt_scope = str(receipt_payload.get("scope", "all"))
    validation_scope = scope or (None if receipt_scope == "all" else receipt_scope)
    _, receipt_errors = _validate_scan_receipt(run, registry, rows, validation_scope)
    if receipt_errors:
        die("strong-claim scan 已失效：" + "; ".join(receipt_errors))
    for row in rows:
        if row.get("claim_id") != args.claim_id:
            continue
        ctype = row.get("claim_type", "")
        if args.status == "supported":
            allowed = ALLOWED.get(ctype, set())
            if args.evidence_type not in allowed:
                die(f"{args.evidence_type} 不能支持 {ctype}。允许：{sorted(allowed)}")
            if not args.evidence_ref:
                die("supported 状态必须提供 --evidence-ref。")
            resolved, evidence_errors = _resolve_evidence_ref(run, args.evidence_ref)
            if evidence_errors:
                die("supported 证据引用无效：" + "; ".join(evidence_errors))
            mismatched_types = sorted({
                item.get("evidence_type", "") for item in resolved
                if item.get("evidence_type") and item.get("evidence_type") != args.evidence_type
            })
            if mismatched_types:
                die(
                    f"声明的 evidence_type={args.evidence_type!r} 与 ledger 类型不一致："
                    + ", ".join(mismatched_types)
                )
        if args.status == "downgraded" and not args.recommended_wording:
            die("downgraded 状态必须提供 --recommended-wording。")
        if args.evidence_type in WEAK_EVIDENCE and args.status == "supported":
            die(f"弱证据 {args.evidence_type} 不能标成 supported。")
        row.update({
            "evidence_type": args.evidence_type or row.get("evidence_type", ""),
            "evidence_ref": args.evidence_ref or row.get("evidence_ref", ""),
            "status": args.status,
            "recommended_wording": args.recommended_wording or row.get("recommended_wording", ""),
            "reviewed_by": reviewer,
            "reviewed_at": now_iso(),
            "notes": args.notes or row.get("notes", ""),
        })
        write_csv(registry, FIELDS, rows)
        _refresh_receipt_registry_binding(run, rows, scope=scope)
        print(f"Updated {args.claim_id}: {args.status}")
        return 0
    die(f"未找到 claim_id：{args.claim_id}")


def _validate_scan_receipt(
    run: Path,
    registry: Path,
    rows: list[dict[str, str]],
    scope: str | None,
    *,
    receipt_path: Path | None = None,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    receipt_path = receipt_path or _receipt_path(run, scope)
    if not receipt_path.exists():
        return {}, [f"{receipt_path.name} missing; run claim_guard.py scan first"]
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"{receipt_path.name} is invalid: {exc}"]
    if not isinstance(receipt, dict):
        return {}, [f"{receipt_path.name} must contain a JSON object"]
    try:
        from schema_validate import validate_json_file

        errors.extend(
            "scan receipt schema: " + item
            for item in validate_json_file(
                receipt_path, ROOT / "schemas" / "strong_claim_scan.schema.json"
            )
        )
    except (Exception, SystemExit) as exc:
        errors.append(f"scan receipt schema validator exception: {exc}")
    if receipt.get("schema_version") != 2:
        errors.append("strong-claim scan receipt uses an unsupported schema; rerun scan")
    if receipt.get("run_id") != run.name:
        errors.append(
            f"strong-claim scan receipt belongs to run {receipt.get('run_id')!r}, "
            f"not {run.name!r}; rerun scan"
        )
    if not receipt.get("passed"):
        errors.append("the most recent strong-claim scan did not pass")
    if not receipt.get("complete"):
        errors.append("the strong-claim scan did not cover every requested path")
    if not receipt.get("canonical_complete"):
        errors.append("the strong-claim scan was partial; rerun without --path for canonical coverage")
    for key in ("missing_explicit_paths", "missing_canonical_roots", "rejected_outside_paths"):
        if receipt.get(key):
            errors.append(f"scan receipt records {key}: {receipt.get(key)}")

    receipt_scope = str(receipt.get("scope", ""))
    required_scope = scope or "all"
    if receipt_scope not in SCOPE_ROOTS:
        errors.append(f"scan receipt has invalid scope: {receipt_scope!r}")
    elif required_scope == "all" and receipt_scope != "all":
        errors.append(f"full lint requires an all-scope scan, found {receipt_scope}")
    elif required_scope != "all" and receipt_scope not in {"all", required_scope}:
        errors.append(f"{required_scope} lint is not covered by {receipt_scope} scan")

    current_rules_hash = _rules_hash()
    if receipt.get("rules_sha256") != current_rules_hash:
        errors.append("strong-claim rules or provenance policy changed after scan")
    if receipt.get("provenance_config_sha256") != _PROVENANCE_CONFIG_SHA256_AT_LOAD:
        errors.append("claim_provenance.json changed after scan")
    if not registry.exists():
        errors.append(f"{registry.name} missing; lint never creates an empty registry")
    else:
        expected_registry = registry.relative_to(run).as_posix()
        if receipt.get("registry") != expected_registry:
            errors.append(
                f"scan receipt registry={receipt.get('registry')!r} does not name {expected_registry}"
            )
        if receipt.get("registry_sha256") != sha256_file(registry):
            errors.append(f"{registry.name} changed outside the scan/provenance workflow")
        if receipt.get("claim_count") != len(rows):
            errors.append(f"scan receipt claim_count does not match {registry.name}")
        current_open_count = sum(1 for row in rows if row.get("status", "open") == "open")
        if receipt.get("open_count") != current_open_count:
            errors.append(f"scan receipt open_count does not match {registry.name}")

    if receipt_scope in SCOPE_ROOTS:
        current_files, discovery = _discover_scope(run, receipt_scope, [])
        for key in ("missing_canonical_roots", "rejected_outside_paths"):
            if discovery.get(key):
                errors.append(f"current canonical corpus has {key}: {discovery.get(key)}")
        current_manifest = _file_manifest(run, current_files)
        if receipt.get("files") != current_manifest:
            errors.append("canonical strong-claim corpus changed after scan")
        if receipt.get("corpus_sha256") != _manifest_hash(current_manifest):
            errors.append("strong-claim corpus hash mismatch")
        if receipt.get("file_count") != len(current_manifest):
            errors.append("strong-claim scan file_count is stale")
    return receipt, errors


def lint(
    run: Path, *, strict: bool, scope: str | None = None, persist: bool = True
) -> tuple[dict[str, Any], int]:
    _assert_cached_policy_current()
    registry = _registry_path(run, scope)
    errors: list[str] = []
    warnings: list[str] = []
    if registry.exists():
        try:
            all_rows = read_csv(registry, expected_fields=FIELDS)
        except SystemExit:
            all_rows = []
            errors.append(f"{registry.name} header does not match the current contract")
    else:
        all_rows = []
    receipt, receipt_errors = _validate_scan_receipt(
        run,
        registry,
        all_rows,
        scope,
        receipt_path=_receipt_path(run, scope),
    )
    errors.extend(receipt_errors)
    seen_claim_ids: set[str] = set()
    duplicate_claim_ids: set[str] = set()
    for row in all_rows:
        claim_id = str(row.get("claim_id", "")).strip()
        if claim_id in seen_claim_ids:
            duplicate_claim_ids.add(claim_id or "<empty>")
        seen_claim_ids.add(claim_id)
    if duplicate_claim_ids:
        errors.append("duplicate claim_id values: " + ", ".join(sorted(duplicate_claim_ids)))
    rows = all_rows
    if scope == "paper":
        rows = [r for r in rows if r.get("path", "").startswith("paper/sections/")]
    elif scope == "math":
        rows = [
            r for r in rows
            if r.get("path", "").startswith("research/modules/")
            or r.get("path", "").startswith("synthesis/")
        ]
    evidence_manifest: list[dict[str, str]] = []
    for row in rows:
        cid = row.get("claim_id", "")
        ctype = row.get("claim_type", "")
        status = row.get("status", "open")
        evidence_type = row.get("evidence_type", "")
        if status == "supported":
            if evidence_type not in ALLOWED.get(ctype, set()):
                errors.append(f"{cid}: invalid proof provenance {evidence_type!r} for {ctype}")
            evidence_ref = row.get("evidence_ref", "").strip()
            if not evidence_ref:
                errors.append(f"{cid}: supported claim lacks evidence_ref")
            else:
                resolved, ref_errors = _resolve_evidence_ref(run, evidence_ref)
                evidence_manifest.extend(resolved)
                errors.extend(f"{cid}: {message}" for message in ref_errors)
                mismatched_types = sorted({
                    item.get("evidence_type", "") for item in resolved
                    if item.get("evidence_type") and item.get("evidence_type") != evidence_type
                })
                if mismatched_types:
                    errors.append(
                        f"{cid}: declared evidence_type {evidence_type!r} differs from ledger "
                        + ", ".join(mismatched_types)
                    )
        elif status == "downgraded":
            if not row.get("recommended_wording", "").strip():
                errors.append(f"{cid}: downgraded claim lacks replacement wording")
            message = (
                f"{cid}: downgraded wording has not been applied at "
                f"{row.get('path')}:{row.get('line')}; edit the source and rescan"
            )
            (errors if strict else warnings).append(message)
        elif status == "rejected":
            message = (
                f"{cid}: rejected claim remains in canonical source at "
                f"{row.get('path')}:{row.get('line')}; remove it and rescan"
            )
            (errors if strict else warnings).append(message)
        elif status == "not_applicable":
            if not row.get("notes", "").strip():
                errors.append(f"{cid}: not_applicable requires a concrete false-positive rationale in notes")
        else:
            message = f"{cid}: open strong claim at {row.get('path')}:{row.get('line')} ({ctype})"
            (errors if strict else warnings).append(message)
        if status in {"supported", "downgraded", "rejected", "not_applicable"}:
            reviewed_by = row.get("reviewed_by", "").strip()
            if not reviewed_by:
                errors.append(f"{cid}: closed claim lacks reviewer")
            elif reviewed_by.upper() in PLACEHOLDER_REVIEWERS:
                errors.append(f"{cid}: closed claim uses a placeholder reviewer")
            if not _valid_review_time(row.get("reviewed_at")):
                errors.append(f"{cid}: closed claim lacks a timezone-aware reviewed_at")
        if evidence_type in WEAK_EVIDENCE and status != "downgraded":
            warnings.append(f"{cid}: weak evidence {evidence_type!r} cannot certify {ctype}")
    evidence_manifest = sorted(
        {item["path"]: item for item in evidence_manifest}.values(), key=lambda item: item["path"]
    )
    result = {
        "generated_at": now_iso(), "strict": strict, "scope": scope or "all",
        "claim_count": len(rows), "errors": errors, "warnings": warnings,
        "passed": not errors,
        "scan_receipt": {
            "generated_at": receipt.get("generated_at"),
            "scope": receipt.get("scope"),
            "rules_sha256": receipt.get("rules_sha256"),
            "corpus_sha256": receipt.get("corpus_sha256"),
            "registry_sha256": receipt.get("registry_sha256"),
        } if receipt else None,
        "resolved_evidence": evidence_manifest,
        "note": "The guard validates claim/evidence categories and review state; it does not validate the proof itself.",
    }
    if persist:
        (run / "claims").mkdir(parents=True, exist_ok=True)
        audit_name = (
            "STRONG_CLAIM_AUDIT_MATH.json"
            if scope == "math"
            else "STRONG_CLAIM_AUDIT.json"
        )
        write_json(run / "claims" / audit_name, result)
    return result, (0 if not errors else 3)


def cmd_lint(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    result, code = lint(run, strict=args.strict, scope=args.scope)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


def cmd_list(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    scope = getattr(args, "scope", None)
    registry = _registry_path(run, scope)
    if not registry.exists():
        die(f"缺少 {registry.name}；先运行 claim_guard.py scan --scope {scope or 'all'}。")
    rows = read_csv(registry, expected_fields=FIELDS)
    print("claim_id       type                    status       evidence              location")
    print("-------------  ----------------------  -----------  --------------------  ------------------------------")
    for row in rows:
        print(f"{row.get('claim_id',''):<13}  {row.get('claim_type',''):<22}  {row.get('status',''):<11}  {row.get('evidence_type',''):<20}  {row.get('path','')}:{row.get('line','')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan")
    p.add_argument("--run")
    p.add_argument("--scope", choices=["all", "paper", "math"], default="all")
    p.add_argument("--path", action="append", default=[])
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("provenance-set")
    p.add_argument("--run"); p.add_argument("--claim-id", required=True)
    p.add_argument("--scope", choices=["all", "math"], default="all")
    p.add_argument("--evidence-type", default=""); p.add_argument("--evidence-ref")
    p.add_argument("--status", choices=["supported", "downgraded", "rejected", "not_applicable"], required=True)
    p.add_argument("--recommended-wording"); p.add_argument("--by", required=True); p.add_argument("--notes")
    p.set_defaults(func=cmd_provenance_set)

    p = sub.add_parser("lint")
    p.add_argument("--run"); p.add_argument("--strict", action="store_true")
    p.add_argument("--scope", choices=["all", "paper", "math"])
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("list")
    p.add_argument("--run")
    p.add_argument("--scope", choices=["all", "math"], default="all")
    p.set_defaults(func=cmd_list)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
