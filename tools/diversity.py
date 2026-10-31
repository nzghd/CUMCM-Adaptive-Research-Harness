#!/usr/bin/env python3
"""Deterministic diversity and coverage analysis for structured Idea Cards.

This tool detects textual/tag overlap and uncovered problem nodes. It does not
prove that two mathematical ideas are equivalent or genuinely independent;
human review remains mandatory.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from common import (
    ROOT,
    atomic_write_text,
    die,
    load_idea_cards,
    now_iso,
    read_json,
    relative_to_run,
    resolve_run,
    sha256_file,
    write_csv,
    write_json,
)

DIVERSITY_ALGORITHM_VERSION = "diversity-scan-v1"
DIVERSITY_RECEIPT_PATH = "research/diversity/DIVERSITY_SCAN.json"
DIVERSITY_OUTPUT_PATHS = (
    "research/diversity/DIVERSITY_REPORT.json",
    "research/diversity/DIVERSITY_REPORT.md",
    "research/diversity/DIVERSITY_MATRIX.csv",
    "research/diversity/GAP_REGISTER.csv",
    "reviews/DIVERSITY_REPORT.md",
)
ALGORITHM_PARAMETER_NAMES = (
    "threshold",
    "family_duplicate_text_threshold",
    "text_weight",
    "family_weight",
    "node_weight",
    "assumption_weight",
    "minimum_ideas",
    "duplicate_ratio_threshold",
    "shared_assumption_fraction",
    "minimum_hours_for_expansion",
)

MATRIX_FIELDS = [
    "idea_a",
    "idea_b",
    "text_cosine",
    "family_jaccard",
    "node_jaccard",
    "assumption_jaccard",
    "combined_similarity",
    "possible_duplicate",
    "notes",
]

GAP_FIELDS = [
    "gap_id",
    "gap_type",
    "problem_node",
    "missing_family",
    "reason",
    "priority",
    "status",
    "suggested_specialist",
    "notes",
]

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "for", "in", "on", "with", "by",
    "模型", "方法", "问题", "数据", "分析", "进行", "使用", "通过", "可以", "考虑", "建立", "基于",
    "方案", "结果", "目标", "约束", "算法", "验证", "研究", "一个", "一种", "以及", "需要", "可能",
}


def _algorithm_parameters(source: argparse.Namespace | dict[str, Any]) -> dict[str, Any]:
    if isinstance(source, dict):
        if set(source) != set(ALGORITHM_PARAMETER_NAMES):
            missing = sorted(set(ALGORITHM_PARAMETER_NAMES) - set(source))
            extra = sorted(set(source) - set(ALGORITHM_PARAMETER_NAMES))
            raise ValueError(
                f"algorithm parameter keys mismatch; missing={missing}, extra={extra}"
            )
        value = source.__getitem__
    else:
        value = lambda name: getattr(source, name)
    parameters: dict[str, Any] = {
        "threshold": float(value("threshold")),
        "family_duplicate_text_threshold": float(
            value("family_duplicate_text_threshold")
        ),
        "text_weight": float(value("text_weight")),
        "family_weight": float(value("family_weight")),
        "node_weight": float(value("node_weight")),
        "assumption_weight": float(value("assumption_weight")),
        "minimum_ideas": int(value("minimum_ideas")),
        "duplicate_ratio_threshold": float(value("duplicate_ratio_threshold")),
        "shared_assumption_fraction": float(value("shared_assumption_fraction")),
        "minimum_hours_for_expansion": float(
            value("minimum_hours_for_expansion")
        ),
    }
    finite_names = [
        name for name, item in parameters.items()
        if name != "minimum_ideas" and not math.isfinite(float(item))
    ]
    if finite_names:
        raise ValueError("algorithm parameters must be finite: " + ", ".join(finite_names))
    for name in (
        "threshold",
        "family_duplicate_text_threshold",
        "duplicate_ratio_threshold",
        "shared_assumption_fraction",
    ):
        if not 0.0 <= float(parameters[name]) <= 1.0:
            raise ValueError(f"{name} must be within [0, 1]")
    for name in ("text_weight", "family_weight", "node_weight", "assumption_weight"):
        if float(parameters[name]) < 0:
            raise ValueError(f"{name} must be non-negative")
    if sum(float(parameters[name]) for name in (
        "text_weight", "family_weight", "node_weight", "assumption_weight"
    )) <= 0:
        raise ValueError("similarity weights must sum to > 0")
    if isinstance(value("minimum_ideas"), bool) or parameters["minimum_ideas"] < 0:
        raise ValueError("minimum_ideas must be a non-negative integer")
    if parameters["minimum_hours_for_expansion"] < 0:
        raise ValueError("minimum_hours_for_expansion must be non-negative")
    return parameters


def _canonical_json_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_run_file_record(
    run: Path,
    relative: str,
    *,
    kind: str,
    idea_id: str | None = None,
) -> dict[str, Any]:
    run_root = run.resolve()
    path = run / relative
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"missing or non-canonical input file: {relative}")
    resolved = path.resolve()
    try:
        canonical = resolved.relative_to(run_root).as_posix()
    except ValueError as exc:
        raise ValueError(f"input file leaves run: {relative}") from exc
    if canonical != relative:
        raise ValueError(f"input file is not canonical: {relative}")
    record: dict[str, Any] = {
        "kind": kind,
        "path": relative,
        "sha256": sha256_file(resolved),
    }
    if idea_id is not None:
        record["id"] = idea_id
    return record


def _canonical_budget_projection(run: Path) -> dict[str, Any]:
    budget = read_json(run / "BUDGET.json", expect=dict)
    time_section = budget.get("time", {})
    human_section = budget.get("human_review", {})
    exploration_section = budget.get("exploration", {})
    if not all(
        isinstance(section, dict)
        for section in (time_section, human_section, exploration_section)
    ):
        raise ValueError("BUDGET time/human_review/exploration sections must be objects")

    raw_deadline = time_section.get("deadline")
    if raw_deadline in (None, ""):
        deadline = None
    else:
        try:
            parsed_deadline = datetime.fromisoformat(
                str(raw_deadline).strip().replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError("BUDGET time.deadline must be ISO 8601") from exc
        if parsed_deadline.tzinfo is None:
            raise ValueError("BUDGET time.deadline must include a timezone")
        deadline = parsed_deadline.astimezone(timezone.utc).isoformat()

    def finite_nonnegative(section: dict[str, Any], field: str) -> float:
        raw = section.get(field, 0)
        if isinstance(raw, bool):
            raise ValueError(f"BUDGET {field} must be numeric, not boolean")
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"BUDGET {field} must be numeric") from exc
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"BUDGET {field} must be finite and non-negative")
        return number

    def nonnegative_integer(section: dict[str, Any], field: str) -> int:
        raw = section.get(field, 0)
        if isinstance(raw, bool):
            raise ValueError(f"BUDGET {field} must be an integer, not boolean")
        try:
            number = int(raw)
            numeric = float(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"BUDGET {field} must be an integer") from exc
        if not math.isfinite(numeric) or numeric != number or number < 0:
            raise ValueError(f"BUDGET {field} must be a non-negative integer")
        return number

    return {
        "time": {"deadline": deadline},
        "human_review": {
            "used_hours": finite_nonnegative(human_section, "used_hours"),
            "max_hours": finite_nonnegative(human_section, "max_hours"),
        },
        "exploration": {
            "used_batches": nonnegative_integer(exploration_section, "used_batches"),
            "max_batches": nonnegative_integer(exploration_section, "max_batches"),
        },
    }


def _current_input_manifest(run: Path) -> list[dict[str, Any]]:
    active_cards = [
        (path, payload)
        for path, payload in load_idea_cards(run)
        if str(payload.get("status", "proposed")).lower()
        not in {"rejected", "discarded"}
    ]
    ids = [str(payload.get("id", "")) for _, payload in active_cards]
    if len(ids) != len(set(ids)):
        raise ValueError("active Idea Card ids must be unique")
    records = [
        _canonical_run_file_record(
            run,
            path.resolve().relative_to(run.resolve()).as_posix(),
            kind="idea-card",
            idea_id=str(payload["id"]),
        )
        for path, payload in active_cards
    ]
    for relative in (
        "problem/PROBLEM_GRAPH.json",
        "planning/APPROACH_SPACE.json",
    ):
        records.append(_canonical_run_file_record(run, relative, kind="run-input"))
    budget_projection = _canonical_budget_projection(run)
    records.append(
        {
            "kind": "semantic-budget",
            "path": "BUDGET.json#diversity-capacity",
            "sha256": _canonical_json_sha256(budget_projection),
            "projection": budget_projection,
        }
    )
    for path in (
        Path(__file__).resolve(),
        ROOT / "tools" / "common.py",
        ROOT / "harness_config.json",
    ):
        resolved = path.resolve()
        if not resolved.is_file():
            raise ValueError(f"missing diversity control: {resolved}")
        try:
            relative = resolved.relative_to(ROOT.resolve()).as_posix()
        except ValueError as exc:
            raise ValueError(f"diversity control leaves project: {resolved}") from exc
        records.append(
            {"kind": "control", "path": relative, "sha256": sha256_file(resolved)}
        )
    records.sort(
        key=lambda item: (
            str(item["kind"]),
            str(item["path"]),
            str(item.get("id", "")),
        )
    )
    return records


def _input_sha256(parameters: dict[str, Any], records: list[dict[str, Any]]) -> str:
    # generated_at is deliberately absent: wall-clock noise never makes a
    # logically identical scan stale.
    return _canonical_json_sha256(
        {
            "schema_version": 1,
            "algorithm_version": DIVERSITY_ALGORITHM_VERSION,
            "algorithm_parameters": parameters,
            "input_manifest": records,
        }
    )


def _current_output_manifest(run: Path) -> list[dict[str, Any]]:
    records = [
        _canonical_run_file_record(run, relative, kind="output")
        for relative in DIVERSITY_OUTPUT_PATHS
    ]
    records.sort(key=lambda item: str(item["path"]))
    return records


def _flatten(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(f"{key} {_flatten(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten(item) for item in value)
    return str(value)


def tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens: list[str] = []
    for word in re.findall(r"[a-z][a-z0-9_+.-]{1,}|\d+(?:\.\d+)?", text):
        if word not in STOPWORDS:
            tokens.append(word)
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        if run in STOPWORDS:
            continue
        # Character bigrams work better than whitespace tokenization for Chinese.
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
            if len(run) <= 5:
                tokens.append(run)
    return tokens


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    left, right = set(a), set(b)
    if not left and not right:
        return 0.0
    return len(left & right) / max(1, len(left | right))


def _document_text(card: dict[str, Any]) -> str:
    fields = [
        card.get("title"),
        card.get("summary"),
        card.get("core_assumptions"),
        card.get("mathematical_objects"),
        card.get("data_requirements"),
        card.get("validation_plan"),
        card.get("falsification_test"),
        card.get("main_risks"),
    ]
    return _flatten(fields)


def tfidf_vectors(cards: list[dict[str, Any]]) -> list[dict[str, float]]:
    tokenized = [tokenize(_document_text(card)) for card in cards]
    document_frequency: Counter[str] = Counter()
    for tokens in tokenized:
        document_frequency.update(set(tokens))
    n_docs = max(1, len(cards))
    vectors: list[dict[str, float]] = []
    for tokens in tokenized:
        counts = Counter(tokens)
        total = sum(counts.values()) or 1
        vector = {
            token: (count / total) * (math.log((1 + n_docs) / (1 + document_frequency[token])) + 1.0)
            for token, count in counts.items()
        }
        vectors.append(vector)
    return vectors


def cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    common = set(left) & set(right)
    dot = sum(left[key] * right[key] for key in common)
    norm_l = math.sqrt(sum(value * value for value in left.values()))
    norm_r = math.sqrt(sum(value * value for value in right.values()))
    if norm_l == 0 or norm_r == 0:
        return 0.0
    return dot / (norm_l * norm_r)


class UnionFind:
    def __init__(self, items: list[str]) -> None:
        self.parent = {item: item for item in items}

    def find(self, item: str) -> str:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, a: str, b: str) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self.parent[root_b] = root_a


def _problem_nodes(run: Path) -> tuple[list[str], dict[str, dict[str, Any]]]:
    path = run / "problem" / "PROBLEM_GRAPH.json"
    if not path.exists():
        return [], {}
    payload = read_json(path, expect=dict)
    nodes = [item for item in payload.get("nodes", []) if isinstance(item, dict) and item.get("id")]
    return [str(item["id"]) for item in nodes], {str(item["id"]): item for item in nodes}


def _expected_families(run: Path) -> list[str]:
    path = run / "planning" / "APPROACH_SPACE.json"
    if not path.exists():
        return []
    payload = read_json(path, expect=dict)
    values = payload.get("candidate_families", [])
    return [str(item) for item in values if str(item).strip()]


def _remaining_hours(budget: dict[str, Any]) -> float | None:
    value = budget.get("time", {}).get("deadline")
    if not value:
        return None
    try:
        deadline = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            deadline = deadline.astimezone()
        return (deadline - datetime.now().astimezone()).total_seconds() / 3600
    except ValueError:
        return None


def analyze(run: Path, args: argparse.Namespace) -> dict[str, Any]:
    parameters = _algorithm_parameters(args)
    raw_cards = load_idea_cards(run)
    cards = [payload for _, payload in raw_cards if str(payload.get("status", "proposed")).lower() not in {"rejected", "discarded"}]
    vectors = tfidf_vectors(cards)
    weights = {
        "text": parameters["text_weight"],
        "family": parameters["family_weight"],
        "node": parameters["node_weight"],
        "assumption": parameters["assumption_weight"],
    }
    total_weight = sum(weights.values())
    if total_weight <= 0:
        raise ValueError("similarity weights must sum to > 0")
    weights = {key: value / total_weight for key, value in weights.items()}

    matrix: list[dict[str, Any]] = []
    ids = [str(card.get("id")) for card in cards]
    uf = UnionFind(ids)
    duplicate_edges = 0
    for i in range(len(cards)):
        for j in range(i + 1, len(cards)):
            left, right = cards[i], cards[j]
            text_score = cosine(vectors[i], vectors[j])
            family_score = jaccard(left.get("method_families", []), right.get("method_families", []))
            node_score = jaccard(left.get("target_nodes", []), right.get("target_nodes", []))
            assumption_score = jaccard(
                tokenize(_flatten(left.get("core_assumptions", []))),
                tokenize(_flatten(right.get("core_assumptions", []))),
            )
            combined = (
                weights["text"] * text_score
                + weights["family"] * family_score
                + weights["node"] * node_score
                + weights["assumption"] * assumption_score
            )
            duplicate = combined >= parameters["threshold"] or (
                family_score >= 0.999
                and text_score >= parameters["family_duplicate_text_threshold"]
            )
            if duplicate:
                duplicate_edges += 1
                uf.union(ids[i], ids[j])
            notes = []
            if family_score >= 0.999 and left.get("method_families"):
                notes.append("same method-family set")
            if node_score == 0:
                notes.append("different target nodes")
            if assumption_score >= 0.5:
                notes.append("shared assumptions")
            matrix.append(
                {
                    "idea_a": ids[i],
                    "idea_b": ids[j],
                    "text_cosine": round(text_score, 6),
                    "family_jaccard": round(family_score, 6),
                    "node_jaccard": round(node_score, 6),
                    "assumption_jaccard": round(assumption_score, 6),
                    "combined_similarity": round(combined, 6),
                    "possible_duplicate": duplicate,
                    "notes": "; ".join(notes),
                }
            )

    clusters_by_root: dict[str, list[str]] = defaultdict(list)
    for idea_id in ids:
        clusters_by_root[uf.find(idea_id)].append(idea_id)
    clusters = [sorted(group) for group in clusters_by_root.values()]
    clusters.sort(key=lambda group: (-len(group), group))

    node_ids, node_meta = _problem_nodes(run)
    covered_by: dict[str, list[str]] = {node: [] for node in node_ids}
    for card in cards:
        for node in card.get("target_nodes", []):
            if str(node) in covered_by:
                covered_by[str(node)].append(str(card.get("id")))
    uncovered_nodes = [node for node, values in covered_by.items() if not values]

    family_counts = Counter(
        str(family)
        for card in cards
        for family in card.get("method_families", [])
        if str(family).strip()
    )
    expected_families = _expected_families(run)
    missing_families = [family for family in expected_families if family not in family_counts]

    assumption_counts: Counter[str] = Counter()
    assumption_examples: dict[str, str] = {}
    for card in cards:
        for assumption in card.get("core_assumptions", []):
            normalized = " ".join(tokenize(str(assumption)))
            if normalized:
                assumption_counts[normalized] += 1
                assumption_examples.setdefault(normalized, str(assumption))
    shared_assumptions = [
        {"assumption": assumption_examples[key], "count": count}
        for key, count in assumption_counts.most_common()
        if count
        >= max(2, math.ceil(len(cards) * parameters["shared_assumption_fraction"]))
    ]

    gaps: list[dict[str, Any]] = []
    gap_no = 1
    for node in uncovered_nodes:
        meta = node_meta.get(node, {})
        priority = "critical" if str(meta.get("priority", "")).lower() == "critical" else "high"
        gaps.append(
            {
                "gap_id": f"GAP_{gap_no:03d}",
                "gap_type": "uncovered-problem-node",
                "problem_node": node,
                "missing_family": "",
                "reason": f"No viable Idea Card targets node {node}",
                "priority": priority,
                "status": "open",
                "suggested_specialist": "problem-specific specialist",
                "notes": str(meta.get("label", "")),
            }
        )
        gap_no += 1
    for family in missing_families:
        gaps.append(
            {
                "gap_id": f"GAP_{gap_no:03d}",
                "gap_type": "missing-method-family",
                "problem_node": "",
                "missing_family": family,
                "reason": "APPROACH_SPACE marked this family worth considering, but no current card uses it",
                "priority": "medium",
                "status": "open",
                "suggested_specialist": family,
                "notes": "A missing family is not automatically required; confirm fit to the problem graph.",
            }
        )
        gap_no += 1

    duplicate_clusters = [group for group in clusters if len(group) > 1]
    unique_cluster_count = len(clusters)
    duplicate_ratio = 0.0 if len(cards) < 2 else 1.0 - unique_cluster_count / len(cards)

    budget = _canonical_budget_projection(run)
    remaining = _remaining_hours(budget)
    human_used = float(budget.get("human_review", {}).get("used_hours", 0))
    human_max = float(budget.get("human_review", {}).get("max_hours", 0))
    batches_used = int(budget.get("exploration", {}).get("used_batches", 0))
    batches_max = int(budget.get("exploration", {}).get("max_batches", 0))
    capacity_stop = (
        (
            remaining is not None
            and remaining <= parameters["minimum_hours_for_expansion"]
        )
        or (human_max > 0 and human_used >= human_max)
        or (batches_max > 0 and batches_used >= batches_max)
    )

    reasons: list[str] = []
    if len(cards) < parameters["minimum_ideas"]:
        reasons.append(
            f"only {len(cards)} viable ideas; minimum exploratory baseline is "
            f"{parameters['minimum_ideas']}"
        )
    if uncovered_nodes:
        reasons.append(f"uncovered problem nodes: {', '.join(uncovered_nodes)}")
    if missing_families:
        reasons.append(f"candidate families not represented: {', '.join(missing_families)}")
    if duplicate_ratio >= parameters["duplicate_ratio_threshold"]:
        reasons.append(f"idea pool is highly clustered (duplicate ratio {duplicate_ratio:.2f})")
    if len(family_counts) <= 1 and len(cards) >= 2:
        reasons.append("all ideas occupy one method family")
    if shared_assumptions and len(cards) >= 3:
        reasons.append("multiple ideas share potentially fragile assumptions")

    if capacity_stop:
        recommendation = "stop-expansion-and-deepen"
        reasons.append("time/human/exploration budget does not justify another broad batch")
    elif reasons:
        recommendation = "targeted-expansion"
    else:
        recommendation = "deepen-current-pool"

    report = {
        "version": read_json(ROOT / "harness_config.json", expect=dict).get("version", "5.1.0"),
        "run_id": run.name,
        "created_at": now_iso(),
        "algorithm_version": DIVERSITY_ALGORITHM_VERSION,
        "algorithm_parameters": parameters,
        "limitations": [
            "Similarity is based on text, tags, target nodes and assumption overlap; it is not a proof of mathematical equivalence.",
            "Method-family coverage depends on how accurately humans/agents labeled the cards.",
            "Expansion is a budgeted research decision, not a requirement to reach a fixed Agent count.",
        ],
        "idea_count": len(cards),
        "ideas": ids,
        "weights": weights,
        "thresholds": {
            "combined_duplicate": parameters["threshold"],
            "same_family_text_duplicate": parameters[
                "family_duplicate_text_threshold"
            ],
        },
        "clusters": clusters,
        "duplicate_clusters": duplicate_clusters,
        "duplicate_ratio": round(duplicate_ratio, 6),
        "method_family_counts": dict(sorted(family_counts.items())),
        "expected_families": expected_families,
        "missing_families": missing_families,
        "problem_node_coverage": covered_by,
        "uncovered_problem_nodes": uncovered_nodes,
        "shared_assumptions": shared_assumptions,
        "gaps": gaps,
        "budget_snapshot": {
            "remaining_hours": None if remaining is None else round(remaining, 3),
            "human_review_used": human_used,
            "human_review_max": human_max,
            "exploration_batches_used": batches_used,
            "exploration_batches_max": batches_max,
        },
        "recommendation": recommendation,
        "reasons": reasons,
        "human_review_required": True,
    }
    return {"report": report, "matrix": matrix, "gaps": gaps}


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Diversity and Coverage Report",
        "",
        f"- Run: `{report['run_id']}`",
        f"- Generated: `{report['created_at']}`",
        f"- Viable Idea Cards: `{report['idea_count']}`",
        f"- Unique clusters: `{len(report['clusters'])}`",
        f"- Duplicate ratio: `{report['duplicate_ratio']:.3f}`",
        f"- Recommendation: **{report['recommendation']}**",
        "",
        "> 这是启发式诊断，不是数学等价性证明。队员和 Diversity Auditor 必须阅读原始 Idea Card。",
        "",
        "## Clusters",
        "",
    ]
    for index, cluster in enumerate(report["clusters"], 1):
        marker = "potentially redundant" if len(cluster) > 1 else "singleton"
        lines.append(f"- Cluster {index} ({marker}): {', '.join(cluster)}")
    lines.extend(["", "## Method-family coverage", ""])
    if report["method_family_counts"]:
        for family, count in report["method_family_counts"].items():
            lines.append(f"- `{family}`: {count}")
    else:
        lines.append("- No labeled method families.")
    if report["missing_families"]:
        lines.extend(["", "Expected but missing: " + ", ".join(f"`{x}`" for x in report["missing_families"])])
    lines.extend(["", "## Problem-node coverage", ""])
    for node, ideas in report["problem_node_coverage"].items():
        lines.append(f"- `{node}`: {', '.join(ideas) if ideas else '**uncovered**'}")
    lines.extend(["", "## Shared assumptions worth attacking", ""])
    if report["shared_assumptions"]:
        for item in report["shared_assumptions"]:
            lines.append(f"- ({item['count']} cards) {item['assumption']}")
    else:
        lines.append("- None detected by text normalization.")
    lines.extend(["", "## Gaps", ""])
    if report["gaps"]:
        for gap in report["gaps"]:
            lines.append(f"- `{gap['gap_id']}` [{gap['priority']}] {gap['reason']}")
    else:
        lines.append("- No automatic gaps detected.")
    lines.extend(["", "## Recommendation rationale", ""])
    if report["reasons"]:
        for reason in report["reasons"]:
            lines.append(f"- {reason}")
    else:
        lines.append("- The current pool appears sufficiently diverse for module deepening.")
    lines.extend(["", "## Required human decision", "", "Choose one of:", "", "- targeted expansion for a named gap;", "- deepen current modules;", "- stop a route because evidence/feasibility is weak;", "- revise mislabeled cards and rerun this tool.", ""])
    return "\n".join(lines)


def _output_sha256(records: list[dict[str, Any]]) -> str:
    return _canonical_json_sha256(
        {"schema_version": 1, "outputs": records}
    )


def validate_diversity(
    run: Path, *, strict: bool = True
) -> tuple[dict[str, Any], int]:
    """Purely verify that the canonical diversity scan is present and current."""
    receipt_path = run / DIVERSITY_RECEIPT_PATH
    base = {
        "validator": "diversity-contract-v1",
        "run_id": run.name,
        "strict": strict,
        "receipt_path": DIVERSITY_RECEIPT_PATH,
    }
    if receipt_path.is_symlink() or not receipt_path.is_file():
        result = {
            **base,
            "passed": False,
            "status": "not-run",
            "errors": [
                f"missing canonical diversity receipt: {DIVERSITY_RECEIPT_PATH}; "
                "run diversity.py analyze"
            ],
            "warnings": [],
        }
        return result, 3

    errors: list[str] = []
    warnings: list[str] = []
    stale = False
    tampered = False
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        receipt = {}
        errors.append(f"DIVERSITY_SCAN.json is invalid: {exc}")
        tampered = True
    if not isinstance(receipt, dict):
        receipt = {}
        errors.append("DIVERSITY_SCAN.json must be a JSON object")
        tampered = True

    required = {
        "schema_version",
        "receipt_type",
        "algorithm_version",
        "run_id",
        "generated_at",
        "algorithm_parameters",
        "input_manifest",
        "input_sha256",
        "outputs",
        "output_sha256",
    }
    missing = sorted(required - set(receipt))
    if missing:
        errors.append("DIVERSITY_SCAN.json missing fields: " + ", ".join(missing))
        tampered = True
    if receipt.get("schema_version") != 1:
        errors.append("DIVERSITY_SCAN.json schema_version must be 1")
        tampered = True
    if receipt.get("receipt_type") != "canonical-diversity-scan":
        errors.append("DIVERSITY_SCAN.json receipt_type is invalid")
        tampered = True
    if receipt.get("algorithm_version") != DIVERSITY_ALGORITHM_VERSION:
        errors.append("diversity algorithm version changed or receipt is invalid")
        stale = True
    if receipt.get("run_id") != run.name:
        errors.append("DIVERSITY_SCAN.json run_id does not match this run")
        tampered = True
    if strict and not str(receipt.get("generated_at", "")).strip():
        errors.append("DIVERSITY_SCAN.json generated_at is missing")
        tampered = True

    parameters: dict[str, Any] | None = None
    try:
        parameters = _algorithm_parameters(receipt.get("algorithm_parameters"))
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        errors.append(f"algorithm_parameters are invalid: {exc}")
        tampered = True
    else:
        if receipt.get("algorithm_parameters") != parameters:
            errors.append("algorithm_parameters are not canonical")
            tampered = True

    stored_inputs = receipt.get("input_manifest")
    if not isinstance(stored_inputs, list) or any(
        not isinstance(item, dict) for item in stored_inputs
    ):
        errors.append("input_manifest must be an array of records")
        stored_inputs = []
        tampered = True
    try:
        current_inputs = _current_input_manifest(run)
    except (Exception, SystemExit) as exc:
        current_inputs = []
        errors.append(f"cannot reconstruct current diversity inputs: {exc}")
        stale = True
    if stored_inputs != current_inputs:
        errors.append("diversity inputs are stale: current input manifest differs")
        stale = True
    if parameters is not None:
        stored_input_sha256 = _input_sha256(parameters, stored_inputs)
        if receipt.get("input_sha256") != stored_input_sha256:
            errors.append("input_sha256 does not match receipt inputs/parameters")
            tampered = True

    stored_outputs = receipt.get("outputs")
    if not isinstance(stored_outputs, list) or any(
        not isinstance(item, dict) for item in stored_outputs
    ):
        errors.append("outputs must be an array of hash records")
        stored_outputs = []
        tampered = True
    try:
        current_outputs = _current_output_manifest(run)
    except (Exception, SystemExit) as exc:
        current_outputs = []
        errors.append(f"diversity output set is incomplete or unsafe: {exc}")
        tampered = True
    if stored_outputs != current_outputs:
        errors.append("diversity report/matrix/gap outputs changed after scan")
        tampered = True
    if receipt.get("output_sha256") != _output_sha256(stored_outputs):
        errors.append("output_sha256 does not match receipt output records")
        tampered = True

    report_path = run / "research" / "diversity" / "DIVERSITY_REPORT.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        report = {}
        errors.append(f"DIVERSITY_REPORT.json is invalid: {exc}")
        tampered = True
    if not isinstance(report, dict):
        report = {}
        errors.append("DIVERSITY_REPORT.json must be an object")
        tampered = True
    if report.get("run_id") != run.name:
        errors.append("DIVERSITY_REPORT.json run_id does not match this run")
        tampered = True
    if report.get("algorithm_version") != DIVERSITY_ALGORITHM_VERSION:
        errors.append("DIVERSITY_REPORT.json algorithm_version is invalid")
        tampered = True
    if parameters is not None and report.get("algorithm_parameters") != parameters:
        errors.append("report algorithm_parameters do not match the receipt")
        tampered = True
    expected_idea_ids = sorted(
        str(item.get("id", ""))
        for item in current_inputs
        if item.get("kind") == "idea-card"
    )
    reported_idea_ids = report.get("ideas")
    if (
        not isinstance(reported_idea_ids, list)
        or sorted(str(item) for item in reported_idea_ids) != expected_idea_ids
    ):
        errors.append("report idea ids do not match the active Idea Card manifest")
        tampered = True

    if stale and tampered:
        status = "stale-or-tampered"
    elif stale:
        status = "stale"
    elif tampered:
        status = "tampered"
    else:
        status = "clean"
    result = {
        **base,
        "passed": not errors,
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "input_sha256": receipt.get("input_sha256"),
        "output_sha256": receipt.get("output_sha256"),
        "input_count": len(stored_inputs),
        "output_count": len(stored_outputs),
    }
    return result, 0 if result["passed"] else 3


def cmd_analyze(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    try:
        parameters = _algorithm_parameters(args)
        input_manifest = _current_input_manifest(run)
    except ValueError as exc:
        die(f"cannot create canonical diversity scan: {exc}")
    result = analyze(run, args)
    out = run / "research" / "diversity"
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "DIVERSITY_REPORT.json", result["report"])
    atomic_write_text(out / "DIVERSITY_REPORT.md", render_markdown(result["report"]))
    write_csv(out / "DIVERSITY_MATRIX.csv", MATRIX_FIELDS, result["matrix"])
    write_csv(out / "GAP_REGISTER.csv", GAP_FIELDS, result["gaps"])
    # Compatibility copy for reviewers; canonical data remains under research/diversity.
    atomic_write_text(run / "reviews" / "DIVERSITY_REPORT.md", render_markdown(result["report"]))
    output_manifest = _current_output_manifest(run)
    receipt = {
        "schema_version": 1,
        "receipt_type": "canonical-diversity-scan",
        "algorithm_version": DIVERSITY_ALGORITHM_VERSION,
        "run_id": run.name,
        "generated_at": now_iso(),
        "algorithm_parameters": parameters,
        "input_manifest": input_manifest,
        "input_sha256": _input_sha256(parameters, input_manifest),
        "outputs": output_manifest,
        "output_sha256": _output_sha256(output_manifest),
    }
    write_json(run / DIVERSITY_RECEIPT_PATH, receipt)
    print(out / "DIVERSITY_REPORT.md")
    print(f"Recommendation: {result['report']['recommendation']}")
    for reason in result["report"]["reasons"]:
        print(f"- {reason}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    result, code = validate_diversity(run, strict=bool(args.strict))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


def cmd_list_families(args: argparse.Namespace) -> int:
    payload = read_json(ROOT / "config" / "method_ontology.json", expect=dict)
    for family in payload.get("families", []):
        print(family)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("analyze")
    p.add_argument("--run")
    p.add_argument("--threshold", type=float, default=0.68)
    p.add_argument("--family-duplicate-text-threshold", type=float, default=0.56)
    p.add_argument("--text-weight", type=float, default=0.52)
    p.add_argument("--family-weight", type=float, default=0.25)
    p.add_argument("--node-weight", type=float, default=0.13)
    p.add_argument("--assumption-weight", type=float, default=0.10)
    p.add_argument("--minimum-ideas", type=int, default=3)
    p.add_argument("--duplicate-ratio-threshold", type=float, default=0.45)
    p.add_argument("--shared-assumption-fraction", type=float, default=0.6)
    p.add_argument("--minimum-hours-for-expansion", type=float, default=10.0)
    p.set_defaults(func=cmd_analyze)
    p = sub.add_parser("validate")
    p.add_argument("--run")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_validate)
    p = sub.add_parser("list-families")
    p.set_defaults(func=cmd_list_families)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
