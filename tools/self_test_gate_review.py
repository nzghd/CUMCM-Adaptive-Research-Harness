#!/usr/bin/env python3
"""Focused integration checks for ``harness review`` and approval binding."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import common  # noqa: E402
import harness  # noqa: E402
from gate_review import parse_gate_review_metadata, review_path  # noqa: E402


def ns(**values: object) -> SimpleNamespace:
    return SimpleNamespace(**values)


def expect_exit(code: int, func, *args, **kwargs) -> None:
    try:
        func(*args, **kwargs)
    except SystemExit as exc:
        assert exc.code == code, exc
    else:
        raise AssertionError(f"expected SystemExit({code})")


def main() -> int:
    run_id = f"SELFTEST_GATE_REVIEW_{os.getpid()}"
    run = common.RUNS_ROOT / run_id
    active = common.ACTIVE_FILE
    active_existed = active.is_file()
    active_bytes = active.read_bytes() if active_existed else b""
    assert run.parent.resolve() == common.RUNS_ROOT.resolve()
    if run.exists():
        raise AssertionError(f"refusing to reuse fixture path: {run}")
    try:
        assert harness.cmd_init(
            ns(
                contest="SELFTEST",
                problem="GATE-REVIEW",
                phase="rehearsal",
                run_id=run_id,
                time_budget_hours=1.0,
                deadline=None,
                human_review_hours=0.5,
                max_exploration_batches=1,
                runtime="codex",
                max_agent_threads=1,
                api_budget_usd=0.0,
                openai_api_budget_usd=0.0,
                anthropic_api_budget_usd=0.0,
                deepseek_api_budget_usd=0.0,
                by="SELFTEST_REVIEWER",
                note="isolated gate-review integration fixture",
            )
        ) == 0

        before = harness.validate_gate(run, "G3", strict=True)
        assert before["ok"] is False
        assert any(
            item.get("kind") == "control"
            and item.get("path") == "tools/gate_review.py"
            for item in before["input_manifest"]
        ), before["input_manifest"]
        # A valid Gate cannot be approved until an existing, current review is
        # available.  Approval itself must not create the missing report or
        # mutate either STATUS or the decision log.
        clean_g0 = dict(before)
        clean_g0.update(
            {
                "gate": "G0",
                "name": "Synthetic current G0",
                "strict": True,
                "ok": True,
                "errors": [],
                "warnings": [],
                "input_manifest": [],
                "input_sha256": "e" * 64,
            }
        )
        g0_path = review_path(run, "G0")
        status_bytes = (run / "STATUS.json").read_bytes()
        decision_bytes = (run / "decisions/DECISION_LOG.jsonl").read_bytes()
        with patch.object(harness, "validate_gate", return_value=clean_g0):
            expect_exit(
                2,
                harness.cmd_approve,
                ns(
                    run=run_id,
                    gate="G0",
                    strict=True,
                    force=False,
                    by="SELFTEST_REVIEWER",
                    note="missing review must fail without writes",
                ),
            )
        assert not g0_path.exists()
        assert (run / "STATUS.json").read_bytes() == status_bytes
        assert (run / "decisions/DECISION_LOG.jsonl").read_bytes() == decision_bytes

        harness._write_current_gate_review(run, clean_g0)
        stale_g0 = dict(clean_g0)
        stale_g0.pop("review_report", None)
        stale_g0["input_sha256"] = "d" * 64
        status_bytes = (run / "STATUS.json").read_bytes()
        decision_bytes = (run / "decisions/DECISION_LOG.jsonl").read_bytes()
        review_bytes = g0_path.read_bytes()
        with patch.object(harness, "validate_gate", return_value=stale_g0):
            expect_exit(
                2,
                harness.cmd_approve,
                ns(
                    run=run_id,
                    gate="G0",
                    strict=True,
                    force=False,
                    by="SELFTEST_REVIEWER",
                    note="stale review must fail without writes",
                ),
            )
        assert (run / "STATUS.json").read_bytes() == status_bytes
        assert (run / "decisions/DECISION_LOG.jsonl").read_bytes() == decision_bytes
        assert g0_path.read_bytes() == review_bytes

        harness._write_current_gate_review(run, clean_g0)
        tampered = g0_path.read_text(encoding="utf-8").replace(
            "<!-- CUMCM_GATE_REVIEW_METADATA",
            "人工改写但未重新生成。\n\n<!-- CUMCM_GATE_REVIEW_METADATA",
            1,
        )
        g0_path.write_text(tampered, encoding="utf-8")
        status_bytes = (run / "STATUS.json").read_bytes()
        decision_bytes = (run / "decisions/DECISION_LOG.jsonl").read_bytes()
        tampered_bytes = g0_path.read_bytes()
        with patch.object(harness, "validate_gate", return_value=clean_g0):
            expect_exit(
                2,
                harness.cmd_approve,
                ns(
                    run=run_id,
                    gate="G0",
                    strict=True,
                    force=False,
                    by="SELFTEST_REVIEWER",
                    note="tampered review must fail without writes",
                ),
            )
        assert (run / "STATUS.json").read_bytes() == status_bytes
        assert (run / "decisions/DECISION_LOG.jsonl").read_bytes() == decision_bytes
        assert g0_path.read_bytes() == tampered_bytes

        harness._write_current_gate_review(run, clean_g0)
        with patch.object(harness, "validate_gate", return_value=clean_g0):
            assert harness.cmd_approve(
                ns(
                    run=run_id,
                    gate="G0",
                    strict=True,
                    force=False,
                    by="SELFTEST_REVIEWER",
                    note="current reviewed Gate succeeds",
                )
            ) == 0

        # A stale approved prerequisite must not be rolled back before the
        # target Gate's pre-existing review is checked.  This is the subtle
        # ordering case that distinguishes a genuinely read-only failed
        # approval attempt from an implicit repair/revalidation command.
        clean_g1 = dict(clean_g0)
        clean_g1.update(
            {
                "gate": "G1",
                "name": "Synthetic current G1",
                "input_sha256": "c" * 64,
            }
        )
        stale_prerequisite_g0 = dict(clean_g0)
        stale_prerequisite_g0["input_sha256"] = "b" * 64
        g1_path = review_path(run, "G1")
        assert not g1_path.exists()
        status_bytes = (run / "STATUS.json").read_bytes()
        decision_bytes = (run / "decisions/DECISION_LOG.jsonl").read_bytes()

        def validate_with_stale_prerequisite(
            _run: Path, gate: str, *, strict: bool
        ) -> dict:
            assert strict is True
            return clean_g1 if gate == "G1" else stale_prerequisite_g0

        with patch.object(
            harness,
            "validate_gate",
            side_effect=validate_with_stale_prerequisite,
        ):
            expect_exit(
                2,
                harness.cmd_approve,
                ns(
                    run=run_id,
                    gate="G1",
                    strict=True,
                    force=False,
                    by="SELFTEST_REVIEWER",
                    note="missing target review precedes stale-upstream rollback",
                ),
            )
        assert not g1_path.exists()
        assert (run / "STATUS.json").read_bytes() == status_bytes
        assert (run / "decisions/DECISION_LOG.jsonl").read_bytes() == decision_bytes

        # Even a failed Gate needs a current report before an approval attempt
        # may write state. Missing review therefore fails with zero mutations.
        g3_path = review_path(run, "G3")
        assert not g3_path.exists()
        status_bytes = (run / "STATUS.json").read_bytes()
        decision_bytes = (run / "decisions/DECISION_LOG.jsonl").read_bytes()
        expect_exit(
            2,
            harness.cmd_approve,
            ns(
                run=run_id,
                gate="G3",
                strict=True,
                force=True,
                by="SELFTEST_REVIEWER",
                note="invalid Gate remains blocked without auto-review",
            ),
        )
        assert not g3_path.exists()
        assert (run / "STATUS.json").read_bytes() == status_bytes
        assert (run / "decisions/DECISION_LOG.jsonl").read_bytes() == decision_bytes

        reviewed_g3 = harness.validate_gate(run, "G3", strict=True)
        assert harness.cmd_review(ns(run=run_id, gate="G3")) == 0
        path = review_path(run, "G3")
        assert path.is_file()
        assert harness.cmd_approve(
            ns(
                run=run_id,
                gate="G3",
                strict=True,
                force=True,
                by="SELFTEST_REVIEWER",
                note="reviewed invalid Gate remains blocked",
            )
        ) == 3
        blocked_state = common.load_status(run)["gates"]["G3"]
        assert blocked_state["status"] == "stale", blocked_state
        assert blocked_state["validation"] and not blocked_state["validation"]["ok"]
        blocked_events = [
            item
            for item in common.read_jsonl(run / "decisions/DECISION_LOG.jsonl")
            if item.get("type") == "gate-approval-blocked"
        ]
        assert blocked_events and blocked_events[-1]["gate"] == "G3", blocked_events

        text = path.read_text(encoding="utf-8-sig")
        assert "状态：暂不可批准" in text
        metadata = parse_gate_review_metadata(path)
        assert metadata is not None
        assert metadata["input_sha256"] == reviewed_g3["input_sha256"]
        assert metadata["input_manifest"] == reviewed_g3["input_manifest"]

        # The review is a derived decision surface, not a Gate input.  Merely
        # generating it must not perturb the underlying validation snapshot.
        after = harness.validate_gate(run, "G3", strict=True)
        assert after["input_sha256"] == reviewed_g3["input_sha256"]
        assert after["input_manifest"] == reviewed_g3["input_manifest"]

        # Exercise the exact approval freshness helper with a synthetic clean
        # result.  Full lifecycle tests cover successful real Gate approvals.
        current = dict(after)
        current.update(
            {
                "ok": True,
                "errors": [],
                "warnings": [],
                "input_sha256": "f" * 64,
            }
        )
        binding = harness._write_current_gate_review(run, current)
        current_binding, current_errors = harness._current_gate_review_binding(
            run, current
        )
        assert current_errors == [], current_errors
        assert current_binding == binding
        current["review_report"] = binding
        item = {
            "status": "approved",
            "approved_by": "SELFTEST_REVIEWER",
            "approved_at": "2026-09-04T16:00:00+08:00",
            "validation": current,
        }
        assert harness._gate_approval_record_errors(
            "G3", item, run=run, current_result=current
        ) == []

        legacy_item = dict(item)
        legacy_validation = dict(current)
        legacy_validation.pop("review_report")
        legacy_item["validation"] = legacy_validation
        legacy_errors = harness._gate_approval_record_errors(
            "G3", legacy_item, run=run, current_result=legacy_validation
        )
        assert any("缺少已绑定" in message for message in legacy_errors), legacy_errors

        # Read-only binding rejects stale metadata without repairing it; the
        # old bytes remain available as the next explicit review's diff base.
        current_without_binding = dict(current)
        current_without_binding.pop("review_report", None)
        stale = dict(current_without_binding)
        stale["input_sha256"] = "d" * 64
        before_stale_check = path.read_bytes()
        stale_binding, stale_errors = harness._current_gate_review_binding(run, stale)
        assert stale_binding is None
        assert stale_errors, stale_errors
        assert path.read_bytes() == before_stale_check

        tampered_text = path.read_text(encoding="utf-8").replace(
            "<!-- CUMCM_GATE_REVIEW_METADATA",
            "未绑定改动。\n\n<!-- CUMCM_GATE_REVIEW_METADATA",
            1,
        )
        path.write_text(tampered_text, encoding="utf-8")
        tampered_bytes = path.read_bytes()
        tampered_binding, tampered_errors = harness._current_gate_review_binding(
            run, current_without_binding
        )
        assert tampered_binding is None
        assert any("生成主体已被修改" in message for message in tampered_errors), tampered_errors
        assert path.read_bytes() == tampered_bytes

        errors = harness._gate_approval_record_errors(
            "G3", item, run=run, current_result=current
        )
        assert any("哈希" in message for message in errors), errors

        parsed = harness.build_parser().parse_args(
            ["review", "--run", run_id, "--gate", "G3"]
        )
        assert parsed.func is harness.cmd_review
        print("self_test_gate_review: PASS")
        return 0
    finally:
        if run.exists():
            assert run.parent.resolve() == common.RUNS_ROOT.resolve()
            shutil.rmtree(run)
        if active_existed:
            active.parent.mkdir(parents=True, exist_ok=True)
            active.write_bytes(active_bytes)
        elif active.exists():
            active.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
