
#!/usr/bin/env python3
"""Thin, auditable adapter around the official llm_verifier.select API."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_criteria(path: Path) -> Any:
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, (dict, list)):
            raise ValueError("criteria JSON must be an object or list")
        return payload
    return str(path)


def candidate_files(directory: Path) -> list[Path]:
    files = sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in {".md", ".txt"})
    if len(files) < 2:
        raise ValueError("need at least two .md/.txt candidate files")
    for p in files:
        if not p.read_text(encoding="utf-8").strip():
            raise ValueError(f"empty candidate: {p}")
    return files


def flat_score_diagnostic(scores: list[float], tolerance: float) -> tuple[bool, str]:
    if not scores:
        return True, "no scores returned"
    spread = max(scores) - min(scores)
    near_half = all(abs(s - 0.5) <= tolerance for s in scores)
    if near_half:
        return True, "all scores collapsed near 0.5; check logprob/reasoning parser support"
    if spread <= tolerance:
        return True, f"score spread {spread:.3g} is effectively flat"
    if any(not math.isfinite(s) for s in scores):
        return True, "non-finite score returned"
    return False, "ok"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--problem", type=Path, required=True)
    ap.add_argument("--candidates-dir", type=Path, required=True)
    ap.add_argument("--criteria", type=Path, required=True)
    ap.add_argument("--model", default=None)
    ap.add_argument("--ground-truth-note", default=None)
    ap.add_argument("--expected-version", default="0.2.0")
    ap.add_argument("--allow-version-mismatch", action="store_true")
    ap.add_argument("--n-evaluations", type=int, default=4)
    ap.add_argument("--pivots", type=int, default=2)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-workers", type=int, default=None)
    ap.add_argument("--cache", type=Path, default=None)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--flat-tolerance", type=float, default=1e-6)
    ap.add_argument("--allow-flat", action="store_true", help="not recommended; save flat output instead of failing")
    args = ap.parse_args()

    try:
        import llm_verifier
    except ImportError:
        print("llm_verifier is not installed. Run the integration install script.", file=sys.stderr)
        return 2

    installed_version = str(getattr(llm_verifier, "__version__", "unknown"))
    if installed_version != args.expected_version and not args.allow_version_mismatch:
        print(
            f"llm_verifier version mismatch: installed={installed_version}, expected={args.expected_version}. "
            "Reinstall the pinned commit or pass --allow-version-mismatch explicitly.",
            file=sys.stderr,
        )
        return 4

    problem = args.problem.read_text(encoding="utf-8")
    files = candidate_files(args.candidates_dir)
    candidates = [p.read_text(encoding="utf-8") for p in files]
    criteria = load_criteria(args.criteria)

    kwargs: dict[str, Any] = {
        "problem": problem,
        "candidates": candidates,
        "criteria": criteria,
        "n_evaluations": args.n_evaluations,
        "pivots": args.pivots,
        "seed": args.seed,
        "progress": True,
        "on_error": "raise",
    }
    if args.ground_truth_note:
        kwargs["ground_truth_note"] = args.ground_truth_note
    if args.model:
        kwargs["model"] = args.model
    if args.max_workers is not None:
        kwargs["max_workers"] = args.max_workers
    if args.cache is not None:
        kwargs["cache"] = str(args.cache)

    result = llm_verifier.select(**kwargs)
    scores = [float(x) for x in result.scores]
    invalid, diagnostic = flat_score_diagnostic(scores, args.flat_tolerance)

    usage = None
    try:
        raw_usage = llm_verifier.token_usage()
        usage = raw_usage.__dict__ if hasattr(raw_usage, "__dict__") else str(raw_usage)
    except Exception:
        usage = None

    payload = {
        "created_at": now_iso(),
        "upstream_version": installed_version,
        "expected_upstream_version": args.expected_version,
        "mode": "reference_verifier_faithful",
        "problem_path": str(args.problem),
        "criteria_path": str(args.criteria),
        "candidate_files": [str(p) for p in files],
        "model": args.model,
        "config": {
            "n_evaluations": args.n_evaluations,
            "pivots": args.pivots,
            "seed": args.seed,
            "max_workers": args.max_workers,
        },
        "winner_index": int(result.index),
        "winner_file": str(files[result.index]),
        "ranking": [int(i) for i in result.ranking],
        "scores": scores,
        "n_comparisons": int(result.n_comparisons),
        "criteria_ids": list(result.criteria),
        "usage": usage,
        "diagnostic": diagnostic,
        "valid": not invalid,
        "warning": "Scores are verifier preferences, not mathematical proof or correctness probabilities."
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = args.out.with_suffix(".md")
    report.write_text(
        "# Faithful Verifier Result\n\n"
        f"- Valid: `{not invalid}`\n"
        f"- Diagnostic: {diagnostic}\n"
        f"- Winner: `{files[result.index].name}`\n"
        f"- Scores: `{scores}`\n"
        f"- Ranking: `{payload['ranking']}`\n"
        f"- Comparisons: `{result.n_comparisons}`\n\n"
        "These values are verifier preferences, not correctness probabilities.\n",
        encoding="utf-8",
    )

    if invalid and not args.allow_flat:
        print(f"Invalid verifier output: {diagnostic}", file=sys.stderr)
        print(f"Saved diagnostic to {args.out}", file=sys.stderr)
        return 3

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
