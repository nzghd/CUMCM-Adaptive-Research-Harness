#!/usr/bin/env python3
"""Fast offline regression for the diversity scan provenance contract."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import common  # noqa: E402
import diversity  # noqa: E402


def _file_snapshot(run: Path) -> dict[str, str]:
    return {
        path.relative_to(run).as_posix(): common.sha256_file(path)
        for path in common.iter_files(run)
    }


def _args(run_id: str, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "run": run_id,
        "threshold": 0.68,
        "family_duplicate_text_threshold": 0.56,
        "text_weight": 0.52,
        "family_weight": 0.25,
        "node_weight": 0.13,
        "assumption_weight": 0.10,
        "minimum_ideas": 3,
        "duplicate_ratio_threshold": 0.45,
        "shared_assumption_fraction": 0.6,
        "minimum_hours_for_expansion": 10.0,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _write_fixture(run: Path) -> None:
    (run / "problem").mkdir(parents=True)
    (run / "planning").mkdir()
    common.write_json(
        run / "RUN_META.json",
        {"run_id": run.name, "run_path": common.relative_to_root(run)},
    )
    common.write_json(run / "STATUS.json", {"run_id": run.name})
    for idea_id, family, node, status in (
        ("IDEA_001", "optimization", "Q1", "proposed"),
        ("IDEA_002", "simulation", "Q2", "explored"),
        ("IDEA_IGNORED", "discarded-route", "Q1", "rejected"),
    ):
        idea_dir = run / "research" / "ideas" / idea_id
        idea_dir.mkdir(parents=True)
        common.write_json(
            idea_dir / "IDEA_CARD.json",
            {
                "id": idea_id,
                "title": f"Synthetic {family} route",
                "summary": f"Use {family} to study {node}",
                "target_nodes": [node],
                "method_families": [family],
                "core_assumptions": [f"{node} is observable"],
                "validation_plan": ["deterministic replay"],
                "status": status,
                "created_at": "2000-01-01T00:00:00+00:00",
            },
        )
    common.write_json(
        run / "problem" / "PROBLEM_GRAPH.json",
        {
            "nodes": [
                {"id": "Q1", "label": "first question", "priority": "critical"},
                {"id": "Q2", "label": "second question"},
            ],
            "edges": [],
        },
    )
    common.write_json(
        run / "planning" / "APPROACH_SPACE.json",
        {"candidate_families": ["optimization", "simulation", "statistics"]},
    )
    common.write_json(
        run / "BUDGET.json",
        {
            "time": {},
            "human_review": {"used_hours": 0, "max_hours": 8},
            "exploration": {"used_batches": 0, "max_batches": 4},
        },
    )


def main() -> int:
    run = common.RUNS_ROOT / f"_SELFTEST_DIVERSITY_{os.getpid()}"
    if run.exists():
        if run.resolve().parent != common.RUNS_ROOT.resolve():
            raise AssertionError(f"unsafe fixture path: {run}")
        shutil.rmtree(run)
    try:
        _write_fixture(run)

        never_run, never_code = diversity.validate_diversity(run, strict=True)
        assert never_code != 0 and never_run["status"] == "not-run", never_run
        assert not (run / "research" / "diversity").exists()

        args = _args(run.name)
        assert diversity.cmd_analyze(args) == 0
        before_clean_validation = _file_snapshot(run)
        clean, clean_code = diversity.validate_diversity(run, strict=True)
        assert clean_code == 0 and clean["passed"], clean
        assert clean["status"] == "clean"
        assert _file_snapshot(run) == before_clean_validation

        receipt_path = run / "research" / "diversity" / "DIVERSITY_SCAN.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
        input_records = receipt["input_manifest"]
        idea_records = [item for item in input_records if item["kind"] == "idea-card"]
        assert [item["id"] for item in idea_records] == ["IDEA_001", "IDEA_002"]
        bound_paths = {item["path"] for item in input_records}
        assert {
            "problem/PROBLEM_GRAPH.json",
            "planning/APPROACH_SPACE.json",
            "BUDGET.json#diversity-capacity",
            "tools/diversity.py",
        }.issubset(bound_paths), bound_paths
        output_paths = {item["path"] for item in receipt["outputs"]}
        assert output_paths == set(diversity.DIVERSITY_OUTPUT_PATHS), output_paths

        budget_path = run / "BUDGET.json"
        canonical_budget = budget_path.read_bytes()
        budget_with_unrelated_noise = json.loads(canonical_budget.decode("utf-8"))
        budget_with_unrelated_noise.update(
            {
                "updated_at": "2099-01-01T00:00:00+00:00",
                "api": {
                    "used_calls": 999,
                    "reserved_calls": 123,
                    "provider": "irrelevant-to-diversity",
                },
            }
        )
        common.write_json(budget_path, budget_with_unrelated_noise)
        budget_noise_result, budget_noise_code = diversity.validate_diversity(
            run, strict=True
        )
        assert budget_noise_code == 0 and budget_noise_result["passed"], (
            budget_noise_result
        )
        budget_path.write_bytes(canonical_budget)

        for section, field in (
            ("human_review", "used_hours"),
            ("exploration", "used_batches"),
        ):
            changed_budget = json.loads(canonical_budget.decode("utf-8"))
            changed_budget[section][field] += 1
            common.write_json(budget_path, changed_budget)
            budget_stale, budget_stale_code = diversity.validate_diversity(
                run, strict=True
            )
            assert budget_stale_code != 0 and budget_stale["status"] == "stale", (
                section,
                field,
                budget_stale,
            )
            budget_path.write_bytes(canonical_budget)
        budget_restored, budget_restored_code = diversity.validate_diversity(
            run, strict=True
        )
        assert budget_restored_code == 0 and budget_restored["passed"], (
            budget_restored
        )

        first_input_sha = receipt["input_sha256"]
        with patch.object(
            diversity,
            "now_iso",
            return_value="2099-12-31T23:59:59+00:00",
        ):
            assert diversity.cmd_analyze(args) == 0
        rerun_receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
        assert rerun_receipt["input_sha256"] == first_input_sha
        rerun_clean, rerun_code = diversity.validate_diversity(run, strict=True)
        assert rerun_code == 0 and rerun_clean["passed"], rerun_clean

        card_path = run / "research" / "ideas" / "IDEA_001" / "IDEA_CARD.json"
        canonical_card = card_path.read_bytes()
        card = json.loads(canonical_card.decode("utf-8"))
        card["title"] = "Changed after diversity scan"
        common.write_json(card_path, card)
        stale, stale_code = diversity.validate_diversity(run, strict=True)
        assert stale_code != 0 and stale["status"] == "stale", stale
        card_path.write_bytes(canonical_card)
        restored, restored_code = diversity.validate_diversity(run, strict=True)
        assert restored_code == 0 and restored["passed"], restored

        matrix_path = run / "research" / "diversity" / "DIVERSITY_MATRIX.csv"
        canonical_matrix = matrix_path.read_bytes()
        matrix_path.write_bytes(canonical_matrix + b"\n")
        tampered, tampered_code = diversity.validate_diversity(run, strict=True)
        assert tampered_code != 0 and tampered["status"] == "tampered", tampered
        matrix_path.write_bytes(canonical_matrix)
        final, final_code = diversity.validate_diversity(run, strict=True)
        assert final_code == 0 and final["passed"], final

        changed_parameters = _args(run.name, threshold=0.72)
        assert diversity.cmd_analyze(changed_parameters) == 0
        changed_receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
        assert changed_receipt["input_sha256"] != first_input_sha
        assert changed_receipt["algorithm_parameters"]["threshold"] == 0.72
        print("OK: diversity receipt distinguishes not-run, stale, tampered, and clean")
        return 0
    finally:
        if run.exists():
            if run.resolve().parent != common.RUNS_ROOT.resolve():
                raise AssertionError(f"unsafe cleanup path: {run}")
            shutil.rmtree(run)


if __name__ == "__main__":
    raise SystemExit(main())
