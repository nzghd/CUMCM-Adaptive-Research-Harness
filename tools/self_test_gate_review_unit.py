#!/usr/bin/env python3
"""Focused unit contract for the deterministic Gate review module."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
from pathlib import Path

from gate_review import (
    parse_gate_review_metadata,
    render_gate_review,
    review_path,
    verify_gate_review,
    write_gate_review,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _record(run: Path, relative: str) -> dict[str, str]:
    path = run / relative
    return {
        "kind": "artifact",
        "path": relative,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="gate-review-") as temp:
        run = Path(temp) / "RUN_UNIT"
        _csv(
            run / "evidence/CLAIM_EVIDENCE_LEDGER.csv",
            [
                "claim_id", "claim_text", "claim_type", "is_core", "evidence_id",
                "evidence_type", "evidence_path", "generation_command", "result_summary",
                "verification_status", "verified_by", "verified_at", "paper_location", "notes",
            ],
            [
                {
                    "claim_id": "C001", "claim_text": "精度提升 8.2%", "claim_type": "empirical",
                    "is_core": "true", "evidence_id": "E001", "evidence_type": "experiment",
                    "evidence_path": "results/model.csv", "verification_status": "verified",
                },
                {
                    "claim_id": "C002", "claim_text": "鲁棒性较好", "claim_type": "result",
                    "is_core": "true", "evidence_id": "", "evidence_type": "",
                    "evidence_path": "", "verification_status": "pending",
                },
            ],
        )
        _write(run / "results/model.csv", "name,value\nmodel,0.771\n")
        _csv(
            run / "experiments/EXPERIMENT_LOG.csv",
            ["experiment_id", "timestamp", "module_id", "command", "seed", "status", "output_path", "duration_sec", "notes"],
            [{"experiment_id": "EXP-007", "module_id": "MOD_001", "seed": "7", "status": "passed", "output_path": "experiments/EXP-007/run.json"}],
        )
        _write(run / "experiments/EXP-007/run.json", "{}\n")
        _csv(
            run / "experiments/METRICS.csv",
            ["experiment_id", "module_id", "split", "metric", "value", "unit", "seed", "evidence_path", "notes"],
            [{"experiment_id": "EXP-007", "module_id": "MOD_001", "split": "test", "metric": "accuracy", "value": "0.771", "unit": "ratio", "seed": "7", "evidence_path": "results/model.csv"}],
        )
        _csv(
            run / "experiments/BASELINE_REGISTRY.csv",
            ["baseline_id", "name", "purpose", "implementation_path", "metric", "expected_role", "status", "notes"],
            [{"baseline_id": "B001", "name": "baseline", "status": "ready"}],
        )
        _csv(
            run / "sensitivity/PARAMETER_SENSITIVITY.csv",
            ["sensitivity_id", "kind", "target", "alternatives_or_range", "metric", "experiment_id", "evidence_path", "observed_effect", "effect_pct", "status", "verified_by", "verified_at", "waiver_reason", "notes"],
            [{"sensitivity_id": "PS001", "kind": "parameter", "target": "lambda", "alternatives_or_range": "+-20%", "metric": "rank", "observed_effect": "排序改变", "status": "planned"}],
        )
        input_files = [
            "evidence/CLAIM_EVIDENCE_LEDGER.csv",
            "experiments/EXPERIMENT_LOG.csv",
            "experiments/METRICS.csv",
            "sensitivity/PARAMETER_SENSITIVITY.csv",
        ]
        validation = {
            "gate": "G3",
            "name": "Experiments and evidence",
            "strict": True,
            "ok": False,
            "errors": ["C002 尚无人工 verified 证据"],
            "warnings": ["当前只有一个随机种子"],
            "experiment_audit": {"valid_receipt_count": 1},
            "input_manifest": [_record(run, item) for item in input_files],
            "input_sha256": "a" * 64,
        }

        rendered_once = render_gate_review(run, validation)
        rendered_twice = render_gate_review(run, validation)
        assert rendered_once.encode("utf-8") == rendered_twice.encode("utf-8")
        assert "# G3 人工审核摘要" in rendered_once
        assert "状态：暂不可批准" in rendered_once
        assert "C002" in rendered_once and "PS001" in rendered_once
        assert "accuracy = 0.771 ratio" in rendered_once
        assert "](../../results/model.csv)" in rendered_once
        assert "../outside.txt" not in rendered_once

        binding = write_gate_review(run, validation)
        path = review_path(run, "G3")
        original = path.read_bytes()
        assert binding == {
            "path": "reviews/gates/G3_REVIEW.md",
            "sha256": hashlib.sha256(original).hexdigest(),
            "underlying_input_sha256": "a" * 64,
        }
        assert verify_gate_review(run, validation, binding) == []
        metadata = parse_gate_review_metadata(path)
        assert metadata and metadata["run_id"] == "RUN_UNIT"
        assert metadata["schema_version"] == 2
        assert metadata["input_sha256"] == "a" * 64
        assert metadata["comparison_input_manifest"] is None

        # A current source snapshot is byte-stable, while manual/hostile body
        # changes are detected and restored to the canonical generated body.
        stable_binding = write_gate_review(run, validation)
        assert path.read_bytes() == original
        assert stable_binding == binding
        annotated = original.decode("utf-8").replace(
            "<!-- CUMCM_GATE_REVIEW_METADATA", "人工备注：已现场复核。\n\n<!-- CUMCM_GATE_REVIEW_METADATA"
        )
        path.write_text(annotated, encoding="utf-8", newline="")
        tamper_errors = verify_gate_review(run, validation)
        assert any("生成主体已被修改" in item for item in tamper_errors), tamper_errors
        repaired_binding = write_gate_review(run, validation)
        assert path.read_bytes() == original
        assert "人工备注" not in path.read_text(encoding="utf-8")
        assert repaired_binding == binding
        assert verify_gate_review(run, validation, repaired_binding) == []

        # Every Markdown link must use ../../ and resolve to an existing,
        # canonical file inside the current run.
        external_link = original.decode("utf-8").replace(
            "<!-- CUMCM_GATE_REVIEW_METADATA",
            "[外部链接](https://example.invalid/evidence)\n\n<!-- CUMCM_GATE_REVIEW_METADATA",
        )
        path.write_text(external_link, encoding="utf-8", newline="")
        link_errors = verify_gate_review(run, validation)
        assert any("非 run-local 链接" in item for item in link_errors), link_errors
        write_gate_review(run, validation)
        missing_link = path.read_text(encoding="utf-8").replace(
            "<!-- CUMCM_GATE_REVIEW_METADATA",
            "[失效链接](../../results/missing.csv)\n\n<!-- CUMCM_GATE_REVIEW_METADATA",
        )
        path.write_text(missing_link, encoding="utf-8", newline="")
        link_errors = verify_gate_review(run, validation)
        assert any("非 canonical 或已失效链接" in item for item in link_errors), link_errors
        write_gate_review(run, validation)

        # The heading reports all decisions even when only the first ten are
        # expanded in the body.
        many_issues = dict(validation)
        many_issues["errors"] = [f"阻断问题 {index}" for index in range(12)]
        many_issues["warnings"] = []
        many_text = render_gate_review(run, many_issues)
        assert "## 需要队员判断的 14 项" in many_text, many_text
        assert "其余 4 项已折叠" in many_text, many_text

        # Changed source regenerates the report and exposes manifest delta.
        _write(run / "results/new.csv", "metric,value\nworst,0.702\n")
        updated = dict(validation)
        updated["input_sha256"] = "b" * 64
        updated["input_manifest"] = [
            *validation["input_manifest"],
            _record(run, "results/new.csv"),
        ]
        updated_binding = write_gate_review(run, updated)
        updated_text = path.read_text(encoding="utf-8")
        assert "新增 1 项，删除 0 项，变更 0 项" in updated_text
        assert "results/new.csv" in updated_text
        assert verify_gate_review(run, updated, updated_binding) == []
        updated_metadata = parse_gate_review_metadata(path)
        assert updated_metadata
        assert updated_metadata["comparison_input_manifest"] == metadata["input_manifest"]

        # comparison_input_manifest is sufficient to reproduce the exact diff
        # body on a later call with otherwise unchanged input.
        updated_bytes = path.read_bytes()
        assert write_gate_review(run, updated) == updated_binding
        assert path.read_bytes() == updated_bytes

        path.write_text(
            updated_text.replace(
                "<!-- CUMCM_GATE_REVIEW_METADATA",
                "未绑定的人工改动。\n\n<!-- CUMCM_GATE_REVIEW_METADATA",
                1,
            ),
            encoding="utf-8",
            newline="",
        )
        errors = verify_gate_review(run, updated, updated_binding)
        assert any("审批绑定哈希" in item for item in errors), errors
        assert any("生成主体已被修改" in item for item in errors), errors

        bad_binding = dict(updated_binding)
        bad_binding["path"] = "reviews/gates/../G3_REVIEW.md"
        errors = verify_gate_review(run, updated, bad_binding)
        assert any("canonical 路径" in item for item in errors), errors

        # Symlinked evidence and a symlinked review directory must not escape
        # the run.  Windows installations without symlink privilege skip only
        # this platform capability check.
        outside = Path(temp) / "outside.txt"
        _write(outside, "secret\n")
        link = run / "results/link.csv"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            pass
        else:
            linked = dict(updated)
            linked["input_manifest"] = [*updated["input_manifest"], {
                "kind": "artifact", "path": "results/link.csv", "sha256": "c" * 64,
            }]
            linked["input_sha256"] = "c" * 64
            linked_text = render_gate_review(run, linked)
            assert "](../../results/link.csv)" not in linked_text

        symlink_run = Path(temp) / "RUN_REVIEW_SYMLINK"
        symlink_run.mkdir()
        outside_reviews = Path(temp) / "outside-reviews"
        outside_reviews.mkdir()
        try:
            os.symlink(outside_reviews, symlink_run / "reviews", target_is_directory=True)
        except (OSError, NotImplementedError):
            pass
        else:
            try:
                review_path(symlink_run, "G3")
            except ValueError as exc:
                assert "符号链接" in str(exc) or "离开当前 run" in str(exc)
            else:
                raise AssertionError("review_path accepted a symlink escape")

        # G1 diversity gaps are rendered as a stable human decision line, not
        # Python's dict representation.  Long reasons must not hide the
        # suggested specialist, and old string gaps remain readable.
        _write(
            run / "ideas/IDEA_REGISTRY.json",
            json.dumps({"next_id": 2, "ideas": [{"id": "IDEA_001"}]}, ensure_ascii=False) + "\n",
        )
        long_reason = "当前思路没有覆盖鲁棒优化方向；" * 30
        diversity_gaps: list[object] = [
            {
                "gap_id": "GAP_001",
                "gap_type": "uncovered-problem-node",
                "reason": "No viable Idea Card targets node Q2",
                "priority": "critical",
                "suggested_specialist": "problem-specific specialist",
            },
            {
                "gap_id": "GAP_002",
                "gap_type": "missing-method-family",
                "reason": long_reason,
                "priority": "medium",
                "suggested_specialist": "robust-optimization specialist",
            },
            {},
            "尚未覆盖的 legacy 路线",
        ]
        _write(
            run / "research/diversity/DIVERSITY_REPORT.json",
            json.dumps({"run_id": run.name, "gaps": diversity_gaps}, ensure_ascii=False) + "\n",
        )
        _write(run / "research/diversity/DIVERSITY_REPORT.md", "# Diversity\n")
        _csv(
            run / "research/diversity/GAP_REGISTER.csv",
            [
                "gap_id", "gap_type", "problem_node", "missing_family", "reason",
                "priority", "status", "suggested_specialist", "notes",
            ],
            [diversity_gaps[0], diversity_gaps[1]],  # type: ignore[list-item]
        )
        g1_validation = {
            "gate": "G1",
            "strict": True,
            "ok": False,
            "errors": [],
            "warnings": [],
            "input_manifest": [
                _record(run, "ideas/IDEA_REGISTRY.json"),
                _record(run, "research/diversity/DIVERSITY_REPORT.json"),
                _record(run, "research/diversity/GAP_REGISTER.csv"),
            ],
            "input_sha256": "d" * 64,
        }
        g1_text = render_gate_review(run, g1_validation)
        assert (
            "[红] GAP_001 — uncovered-problem-node — "
            "No viable Idea Card targets node Q2 — 建议专家：problem-specific specialist"
        ) in g1_text
        assert "[黄] GAP_002 — missing-method-family —" in g1_text
        assert "建议专家：robust-optimization specialist" in g1_text
        assert "[黄] 未编号 — 未分类 — 未说明原因 — 建议专家：未指定" in g1_text
        assert "[黄] 未编号 — 未分类 — 尚未覆盖的 legacy 路线 — 建议专家：未指定" in g1_text
        assert "## 需要队员判断的 4 项" in g1_text
        assert "{'gap_id':" not in g1_text
        assert "'suggested_specialist':" not in g1_text
        assert "](../../research/diversity/GAP_REGISTER.csv)" in g1_text

    print("self_test_gate_review_unit: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
