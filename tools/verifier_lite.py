#!/usr/bin/env python3
"""Discrete pairwise verifier inspired by, but not faithful to, LLM-as-a-Verifier.

The official method uses token-level logprob distributions. This Lite tool uses
structured discrete reviews, position reversal, objective prechecks and local
aggregation. Every output is marked ``faithful: false``.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from common import (
    ROOT,
    RUNS_ROOT,
    append_ai_usage_event,
    atomic_write_text,
    die,
    now_iso,
    read_json,
    relative_to_root,
    relative_to_run,
    resolve_project_path,
    sha256_file,
    write_csv,
    write_json,
)


def resolve_config_path(value: str | Path, base: Path | None = None, *, must_exist: bool = True) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        candidate = (base / path).resolve() if base else (ROOT / path).resolve()
        if not candidate.exists() and base is not None:
            candidate = (ROOT / path).resolve()
        path = candidate
    else:
        path = path.resolve()
    if must_exist and not path.exists():
        die(f"路径不存在：{path}")
    return path


def load_config(path: Path) -> dict[str, Any]:
    config = read_json(path, expect=dict)
    required = ["session_id", "problem_file", "candidate_files", "criteria", "output_dir"]
    missing = [key for key in required if key not in config]
    if missing:
        die(f"Lite verifier 配置缺少字段：{missing}")
    if not isinstance(config["candidate_files"], dict) or len(config["candidate_files"]) < 2:
        die("candidate_files 必须至少包含两个候选")
    if not isinstance(config["criteria"], list) or not config["criteria"]:
        die("criteria 必须是非空数组")
    total_weight = sum(float(item.get("weight", 1.0)) for item in config["criteria"])
    if total_weight <= 0:
        die("criteria 权重之和必须大于 0")
    return config


def _harness_version() -> str:
    """Read release identity from the project-wide source of truth."""
    return str(read_json(ROOT / "harness_config.json", expect=dict).get("version", "5.1.0"))


def ring_pairs(candidate_ids: list[str], seed: int) -> list[tuple[str, str]]:
    ids = list(candidate_ids)
    if len(ids) == 2:
        return [(ids[0], ids[1])]
    rng = random.Random(seed)
    rng.shuffle(ids)
    return [(ids[i], ids[(i + 1) % len(ids)]) for i in range(len(ids))]


def _session_path(config: dict[str, Any], config_path: Path) -> Path:
    run = _validated_config_run(config_path)
    raw = Path(str(config["output_dir"])).expanduser()
    if raw.is_absolute():
        lexical = raw
    else:
        from_config = config_path.parent / raw
        from_project = ROOT / raw
        if raw.parts and raw.parts[0].lower() == "runs":
            lexical = from_project
        elif from_config.exists():
            lexical = from_config
        elif from_project.exists():
            lexical = from_project
        else:
            lexical = from_config
    # abspath collapses dot segments but deliberately does not resolve links;
    # link checks must happen before canonical resolution loses that evidence.
    lexical = Path(os.path.abspath(str(lexical)))
    run_root = run.resolve()
    namespace = run_root / "modes" / "reference-verifier-lite"
    try:
        relative = lexical.relative_to(namespace)
    except ValueError:
        die("config.output_dir 必须位于当前 run 的 modes/reference-verifier-lite/ 下。")
    if not relative.parts:
        die("config.output_dir 不能等于 verifier namespace 根目录。")

    current = run_root
    for part in ("modes", "reference-verifier-lite", *relative.parts):
        current = current / part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            die("config.output_dir 路径不能包含符号链接或目录联接。")

    resolved_namespace = namespace.resolve()
    try:
        resolved_namespace.relative_to(run_root)
    except ValueError:
        die("config.output_dir namespace resolve 后越出当前 run。")
    resolved = lexical.resolve()
    try:
        canonical_relative = resolved.relative_to(resolved_namespace)
    except ValueError:
        die("config.output_dir resolve 后越出 verifier namespace。")
    if not canonical_relative.parts or resolved == run_root:
        die("config.output_dir 必须是 verifier namespace 内的非根目录。")
    return resolved


def _validated_config_run(config_path: Path) -> Path:
    """Bind a Lite session to the real run containing its configuration."""
    run = _run_ancestor(config_path)
    if run is None:
        die("Lite verifier config 必须位于带 RUN_META.json/STATUS.json 的真实 run 内。")
    run = run.resolve()
    try:
        relative = run.relative_to(RUNS_ROOT.resolve())
    except ValueError:
        die("Lite verifier config 所属目录不在 runs/ 内。")
    if len(relative.parts) != 1:
        die("Lite verifier run 必须是 runs/ 的直接子目录。")
    meta = read_json(run / "RUN_META.json", expect=dict)
    status = read_json(run / "STATUS.json", expect=dict)
    expected_relative = relative_to_root(run)
    if (
        meta.get("version") != _harness_version()
        or meta.get("run_id") != run.name
        or meta.get("run_path") != expected_relative
        or Path(str(meta.get("project_root", ""))).expanduser().resolve() != ROOT.resolve()
        or status.get("version") != _harness_version()
        or status.get("run_id") != run.name
    ):
        die("Lite verifier config 所属 run 的 RUN_META/STATUS envelope 无效。")
    return run


def _load_candidates(config: dict[str, Any], config_path: Path) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    candidates: dict[str, str] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for candidate_id, raw_path in config["candidate_files"].items():
        path = resolve_config_path(raw_path, config_path.parent, must_exist=True)
        text = path.read_text(encoding="utf-8-sig").strip()
        if not text:
            die(f"候选为空：{candidate_id} -> {path}")
        candidates[str(candidate_id)] = text
        metadata[str(candidate_id)] = {
            "path": relative_to_root(path),
            "sha256": sha256_file(path),
            "characters": len(text),
        }
    return candidates, metadata


def _load_prechecks(config: dict[str, Any], config_path: Path, candidate_ids: list[str]) -> dict[str, dict[str, Any]]:
    raw = config.get("prechecks_file")
    if not raw:
        return {item: {"pass": True, "checks": [], "notes": "No precheck file supplied"} for item in candidate_ids}
    path = resolve_config_path(raw, config_path.parent, must_exist=True)
    payload = read_json(path, expect=dict)
    result: dict[str, dict[str, Any]] = {}
    for candidate_id in candidate_ids:
        item = payload.get(candidate_id, {"pass": True, "checks": [], "notes": "Missing entry; treated as pass with warning"})
        if not isinstance(item, dict):
            die(f"prechecks[{candidate_id}] 必须是对象")
        result[candidate_id] = {
            "pass": bool(item.get("pass", True)),
            "checks": item.get("checks", []),
            "notes": item.get("notes", ""),
        }
    return result


def _review_skeleton(pair_id: str, stage: str, slot_a: str, slot_b: str, criteria: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "faithful": False,
        "method": "structured-discrete-pairwise-review",
        "pair_id": pair_id,
        "stage": stage,
        "reviewer_id": "",
        "orientation": {"slot_a": slot_a, "slot_b": slot_b},
        "criterion_scores": [
            {
                "criterion_id": item["id"],
                "candidate_a_score": None,
                "candidate_b_score": None,
                "hard_failure_a": False,
                "hard_failure_b": False,
                "evidence": [],
                "reason": "",
            }
            for item in criteria
        ],
        "overall_preference": "unfilled",
        "confidence": None,
        "blocking_notes": [],
        "completed": False,
    }


def _packet_prompt(
    problem: str,
    candidate_a: str,
    candidate_b: str,
    slot_a: str,
    slot_b: str,
    pair_id: str,
    review_path: Path,
    criteria: list[dict[str, Any]],
) -> str:
    criteria_text = "\n".join(
        f"- `{item['id']}` (weight {item.get('weight', 1)}): {item.get('description', '')}"
        + (" [hard gate]" if item.get("hard_gate") else "")
        for item in criteria
    )
    return f"""# Lite Verifier Pair {pair_id}

> This is **not** the official logprob verifier. Fill a structured discrete review only.

## Instructions

1. Judge only the two supplied candidates under the stated problem and criteria.
2. Use concrete derivations, test outputs or cited artifact paths; do not reward style.
3. Give each criterion a score from 0 to 4 for both candidates.
4. Mark a hard failure only when a hard requirement is demonstrably violated.
5. Do not read other pair reviews. Position will be reversed in another packet.
6. Fill `{review_path.as_posix()}` exactly and set `completed=true`.
7. Do not report a probability that either candidate is mathematically correct.

## Criteria

{criteria_text}

## Problem

{problem}

## Candidate A — {slot_a}

{candidate_a}

## Candidate B — {slot_b}

{candidate_b}
"""


def _generate_packets(
    session: Path,
    stage: str,
    pairs: Iterable[tuple[str, str]],
    candidates: dict[str, str],
    problem: str,
    criteria: list[dict[str, Any]],
    repeats: int,
    start_index: int = 1,
) -> list[dict[str, Any]]:
    assignments: list[dict[str, Any]] = []
    counter = start_index
    stage_root = session / stage
    stage_root.mkdir(parents=True, exist_ok=True)
    for left, right in pairs:
        for repeat in range(1, repeats + 1):
            for slot_a, slot_b in [(left, right), (right, left)]:
                pair_id = f"{stage.upper()}_{counter:03d}"
                packet_dir = stage_root / pair_id
                packet_dir.mkdir(parents=True, exist_ok=False)
                review_path = packet_dir / "REVIEW.json"
                skeleton = _review_skeleton(pair_id, stage, slot_a, slot_b, criteria)
                write_json(review_path, skeleton)
                atomic_write_text(
                    packet_dir / "PROMPT.md",
                    _packet_prompt(
                        problem,
                        candidates[slot_a],
                        candidates[slot_b],
                        slot_a,
                        slot_b,
                        pair_id,
                        review_path,
                        criteria,
                    ),
                )
                assignments.append(
                    {
                        "pair_id": pair_id,
                        "stage": stage,
                        "slot_a": slot_a,
                        "slot_b": slot_b,
                        "repeat": repeat,
                        "prompt": str((packet_dir / "PROMPT.md").resolve()),
                        "review": str(review_path.resolve()),
                    }
                )
                counter += 1
    return assignments


def cmd_init(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    session = _session_path(config, config_path)
    if session.exists() and any(session.iterdir()) and not args.replace:
        die(f"session 目录非空：{session}。使用 --replace 明确覆盖。")
    if session.exists() and args.replace:
        # Repeat every containment/link/envelope check immediately before the
        # recursive delete rather than trusting the earlier resolved value.
        delete_session = _session_path(config, config_path)
        if delete_session != session:
            die("config.output_dir 在删除前发生变化，拒绝覆盖。")
        shutil.rmtree(delete_session)
    session.mkdir(parents=True, exist_ok=True)

    problem_path = resolve_config_path(config["problem_file"], config_path.parent, must_exist=True)
    problem = problem_path.read_text(encoding="utf-8-sig").strip()
    if not problem:
        die("problem_file 为空")
    candidates, metadata = _load_candidates(config, config_path)
    prechecks = _load_prechecks(config, config_path, list(candidates))
    eligible = [candidate_id for candidate_id in candidates if prechecks[candidate_id]["pass"]]
    excluded = [candidate_id for candidate_id in candidates if not prechecks[candidate_id]["pass"]]
    if len(eligible) < 2:
        die(f"客观预检后仅剩 {len(eligible)} 个候选，无法进行 pairwise verifier")

    repeats = int(config.get("review_repeats_per_orientation", 1))
    if repeats < 1:
        die("review_repeats_per_orientation 必须 >= 1")
    pairs = ring_pairs(eligible, int(config.get("seed", 0)))
    assignments = _generate_packets(session, "stage1", pairs, candidates, problem, config["criteria"], repeats)

    state = {
        "version": _harness_version(),
        "faithful": False,
        "warning": "Discrete reviews are not token-logprob expectations and are not correctness probabilities.",
        "session_id": config["session_id"],
        "created_at": now_iso(),
        "config_path": str(config_path),
        "problem": {"path": relative_to_root(problem_path), "sha256": sha256_file(problem_path)},
        "candidates": metadata,
        "prechecks": prechecks,
        "eligible_candidates": eligible,
        "excluded_candidates": excluded,
        "criteria": config["criteria"],
        "seed": int(config.get("seed", 0)),
        "pivots": min(int(config.get("pivots", 2)), len(eligible)),
        "review_repeats_per_orientation": repeats,
        "min_winner_margin": float(config.get("min_winner_margin", 0.03)),
        "stage": "stage1-awaiting-reviews",
        "stage1_assignments": assignments,
        "stage2_assignments": [],
        "pivots_selected": [],
    }
    write_json(session / "STATE.json", state)
    write_json(session / "CONFIG_SNAPSHOT.json", config)
    write_json(session / "ASSIGNMENTS.json", assignments)
    atomic_write_text(
        session / "README.md",
        "# Lite Verifier Session\n\n"
        "1. Assign each `stage1/*/PROMPT.md` to an isolated Reviewer.\n"
        "2. Reviewer fills the adjacent `REVIEW.json`.\n"
        "3. Run `python tools/verifier_lite.py advance --session ...`.\n"
        "4. Complete stage2 packets, then run `aggregate`.\n\n"
        "This session is not faithful to LLM-as-a-Verifier because it does not use score-token logprobs.\n",
    )
    print(session)
    print(f"Eligible: {eligible}; excluded by precheck: {excluded}")
    print(f"Generated {len(assignments)} stage1 review packets.")
    return 0


def _load_state(session: Path) -> dict[str, Any]:
    return read_json(session / "STATE.json", expect=dict)


def _validate_review(
    review_path: Path,
    criteria: list[dict[str, Any]],
    assignment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    review = read_json(review_path, expect=dict)
    if not review.get("completed"):
        die(f"Review 尚未完成：{review_path}")
    if not str(review.get("reviewer_id", "")).strip():
        die(f"Review 缺少 reviewer_id：{review_path}")
    orientation = review.get("orientation")
    if not isinstance(orientation, dict) or not orientation.get("slot_a") or not orientation.get("slot_b"):
        die(f"Review orientation 无效：{review_path}")
    if assignment is not None:
        if str(review.get("pair_id")) != str(assignment.get("pair_id")):
            die(f"Review pair_id 与 assignment 不一致：{review_path}")
        if str(orientation.get("slot_a")) != str(assignment.get("slot_a")) or str(orientation.get("slot_b")) != str(assignment.get("slot_b")):
            die(f"Review orientation 被修改或与 assignment 不一致：{review_path}")
    confidence = review.get("confidence")
    if confidence is None:
        die(f"Review 缺少 confidence：{review_path}")
    confidence = float(confidence)
    if not 0 <= confidence <= 1:
        die(f"confidence 必须在 [0,1]：{review_path}")
    preference = str(review.get("overall_preference", "")).lower()
    if preference not in {"a", "b", "tie", "uncertain"}:
        die(f"overall_preference 必须是 a/b/tie/uncertain：{review_path}")
    expected_ids = {str(item["id"]) for item in criteria}
    scores = review.get("criterion_scores")
    if not isinstance(scores, list):
        die(f"criterion_scores 无效：{review_path}")
    seen = set()
    for item in scores:
        criterion_id = str(item.get("criterion_id", ""))
        if criterion_id not in expected_ids:
            die(f"未知 criterion_id {criterion_id}：{review_path}")
        if criterion_id in seen:
            die(f"criterion_id 重复：{criterion_id}：{review_path}")
        seen.add(criterion_id)
        for key in ("candidate_a_score", "candidate_b_score"):
            value = item.get(key)
            if value is None:
                die(f"{key} 未填写：{review_path}")
            numeric = float(value)
            if not 0 <= numeric <= 4:
                die(f"{key} 必须在 [0,4]：{review_path}")
    if seen != expected_ids:
        die(f"Review criteria 不完整：{review_path}；缺少 {sorted(expected_ids - seen)}")
    return review


def _review_reward(review: dict[str, Any], criteria: list[dict[str, Any]]) -> tuple[str, str, float, float, dict[str, Any]]:
    orientation = review["orientation"]
    slot_a, slot_b = str(orientation["slot_a"]), str(orientation["slot_b"])
    criteria_by_id = {str(item["id"]): item for item in criteria}
    weighted_a = weighted_b = total = 0.0
    hard_a = hard_b = False
    details = []
    for score in review["criterion_scores"]:
        criterion = criteria_by_id[str(score["criterion_id"])]
        weight = float(criterion.get("weight", 1.0))
        a = float(score["candidate_a_score"]) / 4.0
        b = float(score["candidate_b_score"]) / 4.0
        fail_a = bool(score.get("hard_failure_a", False))
        fail_b = bool(score.get("hard_failure_b", False))
        if criterion.get("hard_gate"):
            hard_a |= fail_a
            hard_b |= fail_b
        weighted_a += weight * a
        weighted_b += weight * b
        total += weight
        details.append({"criterion_id": criterion["id"], "a": a, "b": b, "weight": weight, "hard_failure_a": fail_a, "hard_failure_b": fail_b})
    absolute_a = weighted_a / total if total else 0.5
    absolute_b = weighted_b / total if total else 0.5
    if hard_a and not hard_b:
        raw_a, raw_b = 0.0, 1.0
    elif hard_b and not hard_a:
        raw_a, raw_b = 1.0, 0.0
    elif hard_a and hard_b:
        raw_a = raw_b = 0.5
    else:
        denom = absolute_a + absolute_b
        raw_a = absolute_a / denom if denom > 0 else 0.5
        raw_b = absolute_b / denom if denom > 0 else 0.5
    confidence = float(review["confidence"])
    reward_a = 0.5 + confidence * (raw_a - 0.5)
    reward_b = 0.5 + confidence * (raw_b - 0.5)
    return slot_a, slot_b, reward_a, reward_b, {
        "absolute_a": absolute_a,
        "absolute_b": absolute_b,
        "hard_failure_a": hard_a,
        "hard_failure_b": hard_b,
        "confidence": confidence,
        "criteria": details,
    }


def _completed_assignments(session: Path, assignments: list[dict[str, Any]], criteria: list[dict[str, Any]]) -> list[dict[str, Any]]:
    completed = []
    for assignment in assignments:
        review_path = Path(assignment["review"])
        review = _validate_review(review_path, criteria, assignment)
        a, b, reward_a, reward_b, detail = _review_reward(review, criteria)
        completed.append(
            {
                **assignment,
                "slot_a": a,
                "slot_b": b,
                "reward_a": reward_a,
                "reward_b": reward_b,
                "review_detail": detail,
                "reviewer_id": review.get("reviewer_id", ""),
            }
        )
    return completed


def _scores(candidate_ids: list[str], reviews: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    wins = defaultdict(float)
    counts = defaultdict(int)
    for item in reviews:
        wins[item["slot_a"]] += float(item["reward_a"])
        wins[item["slot_b"]] += float(item["reward_b"])
        counts[item["slot_a"]] += 1
        counts[item["slot_b"]] += 1
    return {
        candidate_id: {
            "score": wins[candidate_id] / counts[candidate_id] if counts[candidate_id] else 0.0,
            "comparisons": counts[candidate_id],
        }
        for candidate_id in candidate_ids
    }


def cmd_advance(args: argparse.Namespace) -> int:
    session = Path(args.session).expanduser().resolve()
    state = _load_state(session)
    if state.get("stage") not in {"stage1-awaiting-reviews", "stage2-awaiting-reviews", "ready-to-aggregate"}:
        die(f"当前 session stage 不支持 advance：{state.get('stage')}")
    if state.get("stage") != "stage1-awaiting-reviews":
        print(f"当前 stage 已是 {state.get('stage')}；无需重复生成 stage2。")
        return 0
    reviews = _completed_assignments(session, state["stage1_assignments"], state["criteria"])
    scores = _scores(state["eligible_candidates"], reviews)
    pivots = sorted(state["eligible_candidates"], key=lambda item: (-scores[item]["score"], item))[: int(state["pivots"])]
    nonpivots = [item for item in state["eligible_candidates"] if item not in pivots]
    stage2_pairs = [(pivot, candidate) for pivot in pivots for candidate in nonpivots]

    config_path = Path(state["config_path"])
    config = load_config(config_path)
    problem_path = resolve_config_path(config["problem_file"], config_path.parent, must_exist=True)
    problem = problem_path.read_text(encoding="utf-8-sig").strip()
    candidates, _ = _load_candidates(config, config_path)
    assignments = _generate_packets(
        session,
        "stage2",
        stage2_pairs,
        candidates,
        problem,
        state["criteria"],
        int(state["review_repeats_per_orientation"]),
        start_index=1,
    ) if stage2_pairs else []
    state["pivots_selected"] = pivots
    state["stage1_scores"] = scores
    state["stage2_assignments"] = assignments
    state["stage"] = "stage2-awaiting-reviews" if assignments else "ready-to-aggregate"
    state["advanced_at"] = now_iso()
    write_json(session / "STATE.json", state)
    write_json(session / "STAGE1_SCORES.json", {"scores": scores, "pivots": pivots, "reviews": reviews})
    write_json(session / "ASSIGNMENTS.json", state["stage1_assignments"] + assignments)
    print(f"Pivots: {pivots}")
    print(f"Generated {len(assignments)} stage2 packets.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    session = Path(args.session).expanduser().resolve()
    state = _load_state(session)
    print(f"Session   : {state.get('session_id')}")
    print(f"Faithful  : {state.get('faithful')}")
    print(f"Stage     : {state.get('stage')}")
    print(f"Eligible  : {state.get('eligible_candidates')}")
    print(f"Excluded  : {state.get('excluded_candidates')}")
    for stage_key in ("stage1_assignments", "stage2_assignments"):
        assignments = state.get(stage_key, [])
        complete = 0
        for assignment in assignments:
            try:
                review = read_json(Path(assignment["review"]), expect=dict)
                complete += int(bool(review.get("completed")))
            except SystemExit:
                pass
        print(f"{stage_key}: {complete}/{len(assignments)} complete")
    return 0


def cmd_aggregate(args: argparse.Namespace) -> int:
    session = Path(args.session).expanduser().resolve()
    state = _load_state(session)
    assignments = state.get("stage1_assignments", []) + state.get("stage2_assignments", [])
    if not assignments:
        die("没有 pairwise assignments")
    if args.allow_incomplete:
        completed_assignments = []
        incomplete = []
        for assignment in assignments:
            review_path = Path(assignment["review"])
            review = read_json(review_path, expect=dict)
            if not review.get("completed"):
                incomplete.append(assignment["pair_id"])
                continue
            validated = _validate_review(review_path, state["criteria"], assignment)
            a, b, reward_a, reward_b, detail = _review_reward(validated, state["criteria"])
            completed_assignments.append({**assignment, "slot_a": a, "slot_b": b, "reward_a": reward_a, "reward_b": reward_b, "review_detail": detail, "reviewer_id": validated.get("reviewer_id", "")})
    else:
        completed_assignments = _completed_assignments(session, assignments, state["criteria"])
        incomplete = []

    scores = _scores(state["eligible_candidates"], completed_assignments)
    ranking = sorted(state["eligible_candidates"], key=lambda item: (-scores[item]["score"], item))
    for excluded in state.get("excluded_candidates", []):
        ranking.append(excluded)
        scores[excluded] = {"score": 0.0, "comparisons": 0, "excluded_by_precheck": True}

    top_margin = None
    winner = None
    selection_status = "insufficient-completed-reviews" if not completed_assignments else "selected"
    if len(state["eligible_candidates"]) >= 2 and completed_assignments:
        top_margin = scores[ranking[0]]["score"] - scores[ranking[1]]["score"]
        if top_margin < float(state.get("min_winner_margin", 0.03)):
            selection_status = "inconclusive-small-margin"
        else:
            winner = ranking[0]
    elif completed_assignments:
        winner = ranking[0]

    result = {
        "version": _harness_version(),
        "faithful": False,
        "method": "objective-precheck + reversed discrete pairwise reviews + pivot-style aggregation",
        "warning": "Scores are aggregate discrete reviewer preferences, not logprob expectations and not correctness probabilities.",
        "session_id": state.get("session_id"),
        "created_at": now_iso(),
        "selection_status": selection_status,
        "winner": winner,
        "ranking": ranking,
        "scores": scores,
        "top_margin": top_margin,
        "min_winner_margin": state.get("min_winner_margin", 0.03),
        "pivots": state.get("pivots_selected", []),
        "completed_review_count": len(completed_assignments),
        "incomplete_pairs": incomplete,
        "prechecks": state.get("prechecks", {}),
        "review_details": completed_assignments,
        "human_confirmation_required": True,
    }
    write_json(session / "RESULT.json", result)
    rows = [
        {
            "rank": index + 1,
            "candidate": candidate_id,
            "score": round(float(scores[candidate_id]["score"]), 8),
            "comparisons": scores[candidate_id]["comparisons"],
            "excluded_by_precheck": scores[candidate_id].get("excluded_by_precheck", False),
        }
        for index, candidate_id in enumerate(ranking)
    ]
    write_csv(session / "RANKING.csv", ["rank", "candidate", "score", "comparisons", "excluded_by_precheck"], rows)
    lines = [
        "# Lite Verifier Result",
        "",
        "**Faithful to LLM-as-a-Verifier:** `false`",
        "",
        f"- Status: `{selection_status}`",
        f"- Winner: `{winner}`" if winner else "- Winner: **not declared**",
        f"- Top margin: `{top_margin}`",
        f"- Completed reviews: `{len(completed_assignments)}`",
        "",
        "## Ranking",
        "",
    ]
    for row in rows:
        lines.append(f"{row['rank']}. `{row['candidate']}` — score `{row['score']}` over `{row['comparisons']}` comparisons")
    lines.extend([
        "",
        "> These scores are not probabilities of correctness. Confirm with tests, mathematics and human review.",
        "",
    ])
    atomic_write_text(session / "RESULT.md", "\n".join(lines))
    state["stage"] = "aggregated"
    state["aggregated_at"] = now_iso()
    state["result"] = "RESULT.json"
    write_json(session / "STATE.json", state)
    run = _run_ancestor(session)
    if run is not None:
        append_ai_usage_event(
            run,
            tool="CUMCM Reference Verifier Lite",
            model="structured isolated reviews",
            stage="reference-verifier-lite",
            purpose="Rank comparable candidates after objective prechecks",
            prompt_or_task_summary=f"Lite session {state.get('session_id')}; {len(completed_assignments)} completed pairwise reviews",
            input_artifacts=[relative_to_run(Path(state["config_path"]), run)],
            output_artifacts=[relative_to_run(session / "RESULT.json", run), relative_to_run(session / "RESULT.md", run)],
            adopted=False,
            human_modified=False,
            human_verified=False,
            notes="faithful=false; discrete reviews are not logprob expectations or correctness probabilities",
        )
    print(session / "RESULT.md")
    print(f"Status: {selection_status}; winner: {winner}")
    return 0 if winner or selection_status.startswith("inconclusive") else 4


def _run_ancestor(path: Path) -> Path | None:
    resolved = path.resolve()
    for parent in (resolved, *resolved.parents):
        if (parent / "RUN_META.json").is_file() and (parent / "STATUS.json").is_file():
            return parent
        if parent == ROOT.resolve():
            break
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init", help="Create ring-pass review packets")
    p.add_argument("--config", required=True)
    p.add_argument("--replace", action="store_true")
    p.set_defaults(func=cmd_init)
    p = sub.add_parser("advance", help="Select pivots from stage1 and generate stage2 packets")
    p.add_argument("--session", required=True)
    p.set_defaults(func=cmd_advance)
    p = sub.add_parser("status")
    p.add_argument("--session", required=True)
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("aggregate")
    p.add_argument("--session", required=True)
    p.add_argument("--allow-incomplete", action="store_true")
    p.set_defaults(func=cmd_aggregate)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
