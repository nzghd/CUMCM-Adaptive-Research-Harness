#!/usr/bin/env python3
"""Fast offline regression test for CUMCM Adaptive Research Harness v5.1.

The test runs in-process inside a disposable directory under ``runs/``.  It
checks the controls added after the V4.1 target-geometry failure.  It performs
no network access, model invocation, or paid API call.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import subprocess
import sys
import types
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import blind_review  # noqa: E402
import claim_guard  # noqa: E402
import common  # noqa: E402
import consistency_matrix  # noqa: E402
import diversity as diversity_tool  # noqa: E402
import external_review  # noqa: E402
import harness  # noqa: E402
import interpretation_engine  # noqa: E402
import math_engine  # noqa: E402
import math_workbench  # noqa: E402
import migrate_v51  # noqa: E402
import paper  # noqa: E402
import paper_v5  # noqa: E402
import run_experiments  # noqa: E402
import runtime  # noqa: E402
import schema_validate  # noqa: E402
import sensitivity_engine  # noqa: E402
import v51_orchestrator  # noqa: E402


def ns(**kwargs):
    return argparse.Namespace(**kwargs)


def expect_exit(code: int, func, *args, **kwargs) -> None:
    try:
        func(*args, **kwargs)
    except SystemExit as exc:
        actual = int(exc.code or 0)
        if actual != code:
            raise AssertionError(f"expected SystemExit({code}), got {actual}") from exc
    else:
        raise AssertionError(f"expected SystemExit({code})")


def assert_failed(result: dict, code: int, needle: str | None = None) -> None:
    assert code != 0 and not result.get("passed", result.get("ok", False)), result
    if needle:
        messages = "\n".join(str(item) for item in result.get("errors", []))
        assert needle.lower() in messages.lower(), messages


def write_csv_rows(path: Path, fields: list[str], rows: list[dict[str, object]] | None = None) -> None:
    common.write_csv(path, fields, rows or [])


def write_json_file(path: Path, payload: object) -> None:
    common.write_json(path, payload)


def write_long(path: Path, title: str, body: str) -> None:
    path.write_text(f"# {title}\n\n" + (body + "\n\n") * 9, encoding="utf-8")


def write_minimal_pdf(path: Path) -> None:
    """Write a deterministic one-page PDF accepted by a strict parser."""
    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 72 72] /Contents 4 0 R >>\nendobj\n",
        b"4 0 obj\n<< /Length 0 >>\nstream\n\nendstream\nendobj\n",
    ]
    payload = bytearray(header)
    offsets = [0]
    for item in objects:
        offsets.append(len(payload))
        payload.extend(item)
    xref_offset = len(payload)
    payload.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        (
            f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def run_file_snapshot(run: Path) -> dict[str, tuple[int, int, str]]:
    """Capture content and metadata so a nominal validation cannot rewrite files."""
    return {
        path.relative_to(run).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            common.sha256_file(path),
        )
        for path in common.iter_files(run)
    }


def static_checks() -> None:
    required = [
        "tools/interpretation_engine.py",
        "tools/sensitivity_engine.py",
        "tools/math_engine.py",
        "tools/math_workbench.py",
        "tools/migrate_v51.py",
        "tools/claim_guard.py",
        "tools/blind_review.py",
        "tools/consistency_matrix.py",
        "tools/paper.py",
        "tools/paper_v5.py",
        "tools/v51_orchestrator.py",
        "requirements-math.txt",
        "QUICKSTART_V5.1_WINDOWS.md",
        "MIGRATION_V5_TO_V5.1.md",
        "skills/cumcm-interpretation-audit/SKILL.md",
        "skills/cumcm-mathematical-modeling/SKILL.md",
        "skills/cumcm-blind-review/SKILL.md",
        "skills/cumcm-claim-guard/SKILL.md",
        "schemas/interpretation_fork.schema.json",
        "schemas/assumption_fingerprint.schema.json",
        "schemas/math_module_spec.schema.json",
        "schemas/blind_review_meta.schema.json",
        "schemas/claim_evidence_ledger.schema.json",
        "schemas/source_ledger.schema.json",
        "schemas/no_source_attestation.schema.json",
        "schemas/ai_export_receipt.schema.json",
        "schemas/ai_disclosure_pdf_receipt.schema.json",
        "schemas/strong_claim_scan.schema.json",
        "schemas/paper_compile_receipt.schema.json",
        "schemas/consistency_matrix.schema.json",
        "schemas/v51_status.schema.json",
        "schemas/frozen_manifest.schema.json",
        "schemas/delivery_manifest.schema.json",
        "evals/regressions/A008_TARGET_GEOMETRY_REGRESSION.md",
    ]
    missing = [rel for rel in required if not (ROOT / rel).exists()]
    if missing:
        raise AssertionError(f"missing v5.1 files: {missing}")
    assert not (ROOT / "templates/run/claims/STRONG_CLAIMS.csv").exists()
    assert not (ROOT / "templates/run/claims/STRONG_CLAIMS_MATH.csv").exists()

    config = json.loads((ROOT / "harness_config.json").read_text(encoding="utf-8-sig"))
    assert config.get("version") == "5.1.0"
    assert config["gates"]["G5"]["v51_requires"] == [
        "independent_review",
        "consistency",
    ]
    assert {
        "claims/STRONG_CLAIMS_MATH.csv",
        "claims/STRONG_CLAIM_SCAN_MATH.json",
    }.issubset(set(config["gates"]["G5"]["requires"]))
    assert config["gates"]["G6"]["v51_requires"] == [
        "consistency",
        "strong_claim_audit",
        "paper_extension",
    ]
    registry = json.loads(
        (ROOT / "config/v51_stage_registry.json").read_text(encoding="utf-8-sig")
    )
    registry_stage_ids = [str(item["id"]) for item in registry["stages"]]
    assert registry_stage_ids.index("consistency") < registry_stage_ids.index(
        "strong_claim_audit"
    )

    roles = json.loads((ROOT / "config/agent_roles.json").read_text(encoding="utf-8-sig"))
    names = {row.get("name") for row in roles.get("roles", [])}
    needed = {
        "interpretation_analyst",
        "assumption_auditor",
        "model_constructor",
        "derivation_specialist",
        "numerical_analyst",
        "model_simplifier",
        "blind_reviewer",
        "claim_provenance_auditor",
    }
    assert needed.issubset(names), sorted(needed - names)

    for path in (
        list((ROOT / "config").glob("*.json"))
        + list((ROOT / "schemas").glob("*.json"))
        + list((ROOT / "templates").rglob("*.json"))
        + [ROOT / "harness_config.json"]
    ):
        json.loads(path.read_text(encoding="utf-8-sig"))

    # Compile source in memory so the isolated self-test never creates
    # ``tools/__pycache__`` or otherwise mutates the checkout.
    for path in sorted(TOOLS.glob("*.py")):
        compile(path.read_text(encoding="utf-8-sig"), str(path), "exec")
    assert runtime.cmd_check(ns()) == 0
    schema_errors = schema_validate.validate_schema_documents()
    assert not schema_errors, schema_errors


def g7_decision_projection_regression() -> None:
    """A G7 receipt is self-stable but becomes history after invalidation."""
    run = harness.RUNS_ROOT / f"_SELFTEST_G7_PROJECTION_{os.getpid()}"
    if run.exists():
        shutil.rmtree(run)
    try:
        (run / "decisions").mkdir(parents=True)
        status = harness._initial_status(common.load_config(), run.name)
        common.write_json(run / "STATUS.json", status)
        decision_log = run / "decisions" / "DECISION_LOG.jsonl"
        decision_log.write_text("", encoding="utf-8")

        def snapshot() -> str:
            _, digest = harness._gate_input_snapshot(
                run,
                "G7",
                {"requires": []},
                [],
            )
            return digest

        pending_digest = snapshot()
        first_validation = {"input_sha256": pending_digest}
        g7_state = status["gates"]["G7"]
        g7_state.update(
            {
                "status": "approved",
                "approved_by": "Alice Reviewer",
                "validation": first_validation,
            }
        )
        common.write_json(run / "STATUS.json", status)
        first_receipt = {
            "timestamp": "2026-09-04T12:00:00+08:00",
            "type": "gate-approval",
            "gate": "G7",
            "by": "Alice Reviewer",
            "strict": True,
            "validation": first_validation,
        }
        common.append_jsonl(decision_log, first_receipt)
        assert snapshot() == pending_digest
        canonical_first_receipt = decision_log.read_bytes()

        # Only one exact current receipt may be omitted.  A copied duplicate
        # remains visible and invalidates the approval-bound input.
        common.append_jsonl(decision_log, dict(first_receipt))
        assert snapshot() != pending_digest
        decision_log.write_bytes(canonical_first_receipt)

        # A non-self audit event stales G7 and promotes the old receipt into
        # bound history.  Reapproval then excludes only the newly appended
        # receipt and is stable on the next validation.
        common.append_jsonl(
            decision_log,
            {
                "timestamp": "2026-09-04T12:01:00+08:00",
                "type": "package-validation-bypass",
                "by": "Alice Reviewer",
                "note": "targeted regression",
            },
        )
        reapproval_digest = snapshot()
        assert reapproval_digest != pending_digest
        second_validation = {"input_sha256": reapproval_digest}
        g7_state["validation"] = second_validation
        common.write_json(run / "STATUS.json", status)
        second_receipt = {
            "timestamp": "2026-09-04T12:02:00+08:00",
            "type": "gate-approval",
            "gate": "G7",
            "by": "Alice Reviewer",
            "strict": True,
            "validation": second_validation,
        }
        common.append_jsonl(decision_log, second_receipt)
        assert snapshot() == reapproval_digest

        # Historical G7 approvals remain auditable inputs; changing one must
        # not be hidden by the current-receipt projection.
        canonical_reapproval_log = decision_log.read_bytes()
        events = common.read_jsonl(decision_log)
        events[0]["by"] = "Mallory"
        decision_log.write_text(
            "".join(
                json.dumps(event, ensure_ascii=False) + "\n"
                for event in events
            ),
            encoding="utf-8",
        )
        assert snapshot() != reapproval_digest
        decision_log.write_bytes(canonical_reapproval_log)

        common.append_jsonl(decision_log, dict(second_receipt))
        assert snapshot() != reapproval_digest
    finally:
        shutil.rmtree(run, ignore_errors=True)


def solver_status_regression() -> None:
    """Status normalization is testable without importing optional solver packages."""
    normalize = math_workbench._normalized_status
    assert normalize("OPTIMAL") in math_workbench.CVXPY_SOLVE_SUCCESS
    assert normalize("optimal_inaccurate") not in math_workbench.CVXPY_SOLVE_SUCCESS
    assert normalize("infeasible") not in math_workbench.CVXPY_SOLVE_SUCCESS
    assert normalize("globallyOptimal") in math_workbench.PYOMO_SOLVE_SUCCESS
    assert normalize("maxIterations") not in math_workbench.PYOMO_SOLVE_SUCCESS
    assert normalize("warning") not in math_workbench.PYOMO_SOLVER_STATUS_SUCCESS
    assert normalize("ok") in math_workbench.PYOMO_SOLVER_STATUS_SUCCESS


def migration_status_regression(run: Path) -> None:
    """Legacy status migration must never preserve ambiguous approvals."""
    version, stage_ids, registry_hash = migrate_v51._current_stage_contract()
    timestamp = "2026-09-02T12:00:00+08:00"
    digest = "a" * 64

    def pending(*, note: str = "") -> dict[str, object]:
        return {
            "status": "pending",
            "validated_at": None,
            "approved_at": None,
            "approved_by": None,
            "approved_input_sha256": None,
            "note": note,
            "last_result": None,
        }

    def approved(stage_id: str) -> dict[str, object]:
        return {
            "status": "approved",
            "validated_at": timestamp,
            "approved_at": timestamp,
            "approved_by": "SELFTEST_REVIEWER",
            "approved_input_sha256": digest,
            "note": "reviewed",
            "last_result": {
                "stage": stage_id,
                "passed": True,
                "errors": [],
                "input_manifest": [],
                "input_sha256": digest,
                "stage_registry_sha256": registry_hash,
            },
        }

    def payload(*, first_stage: dict[str, object] | None = None) -> dict[str, object]:
        stages = {stage_id: pending() for stage_id in stage_ids}
        if first_stage is not None:
            stages[stage_ids[0]] = first_stage
        return {
            "version": version,
            "run_id": run.name,
            "created_at": timestamp,
            "updated_at": timestamp,
            "stage_registry_sha256": registry_hash,
            "stages": stages,
        }

    # A canonical current payload is a true no-op, which is the migration
    # planner's zero-change/dry-run condition.
    canonical = payload()
    assert migrate_v51._migrate_status(canonical, run) is None

    # Preserve the legacy initialization timestamp while filling every newly
    # registered stage with a complete fail-closed pending record.
    initialized_only = {
        "version": version,
        "run_id": run.name,
        "initialized_at": timestamp,
        "updated_at": timestamp,
        "stage_registry_sha256": registry_hash,
        "stages": {stage_ids[0]: pending()},
    }
    migrated = migrate_v51._migrate_status(initialized_only, run)
    assert migrated is not None
    assert migrated["created_at"] == timestamp
    assert set(migrated["stages"]) == set(stage_ids)
    for stage_id in stage_ids[1:]:
        assert migrated["stages"][stage_id] == pending()

    wrong_run = payload()
    wrong_run["run_id"] = "_ANOTHER_RUN"
    expect_exit(2, migrate_v51._migrate_status, wrong_run, run)
    unknown_stage = payload()
    unknown_stage["stages"]["unregistered-stage"] = pending()
    expect_exit(2, migrate_v51._migrate_status, unknown_stage, run)

    # Any registry change invalidates an old approval even if its internal
    # fields were mutually consistent under the old registry.
    legacy_hash = "0" * 64
    legacy_approved = approved(stage_ids[0])
    legacy_approved["last_result"]["stage_registry_sha256"] = legacy_hash
    old_registry = payload(first_stage=legacy_approved)
    old_registry["stage_registry_sha256"] = legacy_hash
    migrated = migrate_v51._migrate_status(old_registry, run)
    assert migrated is not None
    downgraded = migrated["stages"][stage_ids[0]]
    assert downgraded["status"] == "pending"
    assert migrate_v51.REAPPROVAL_MARKER in downgraded["note"]
    for field in (
        "validated_at",
        "approved_at",
        "approved_by",
        "approved_input_sha256",
        "last_result",
    ):
        assert downgraded[field] is None

    # Current-registry approvals still fail closed if any provenance field is
    # absent, stale, internally inconsistent, or not timezone-qualified.
    invalid_approvals: list[tuple[str, object]] = [
        ("approved_input_sha256", None),
        ("approved_by", ""),
        ("approved_at", "2026-09-02T12:00:00"),
        ("validated_at", "2026-09-02T12:00:00"),
        ("last_result.passed", False),
        ("last_result.errors", ["synthetic failure"]),
        ("last_result.stage", stage_ids[1]),
        ("last_result.input_sha256", "b" * 64),
        ("last_result.stage_registry_sha256", "0" * 64),
    ]
    for dotted_field, invalid_value in invalid_approvals:
        row = approved(stage_ids[0])
        if dotted_field.startswith("last_result."):
            row["last_result"][dotted_field.split(".", 1)[1]] = invalid_value
        else:
            row[dotted_field] = invalid_value
        migrated = migrate_v51._migrate_status(payload(first_stage=row), run)
        assert migrated is not None, dotted_field
        downgraded = migrated["stages"][stage_ids[0]]
        assert downgraded["status"] == "pending", dotted_field
        assert migrate_v51.REAPPROVAL_MARKER in downgraded["note"], dotted_field
        for field in (
            "validated_at",
            "approved_at",
            "approved_by",
            "approved_input_sha256",
            "last_result",
        ):
            assert downgraded[field] is None, (dotted_field, field)

    def validation_result(
        stage_id: str, *, passed: bool, errors: list[str]
    ) -> dict[str, object]:
        return {
            "stage": stage_id,
            "passed": passed,
            "errors": errors,
            "input_manifest": [],
            "input_sha256": digest,
            "stage_registry_sha256": registry_hash,
        }

    # Pending rows never retain lifecycle evidence.  Validated and blocked
    # rows survive only when their timestamp, registry and result semantics are
    # already current; stale variants downgrade to canonical pending.
    pending_residue = pending(note="legacy pending")
    pending_residue["validated_at"] = timestamp
    pending_residue["last_result"] = validation_result(
        stage_ids[0], passed=True, errors=[]
    )
    migrated = migrate_v51._migrate_status(
        payload(first_stage=pending_residue), run
    )
    assert migrated is not None
    cleaned = migrated["stages"][stage_ids[0]]
    assert cleaned["status"] == "pending"
    assert migrate_v51.REAPPROVAL_MARKER in cleaned["note"]
    for field in (
        "validated_at",
        "approved_at",
        "approved_by",
        "approved_input_sha256",
        "last_result",
    ):
        assert cleaned[field] is None

    current_validated = pending(note="current validation")
    current_validated.update(
        {
            "status": "validated",
            "validated_at": timestamp,
            "last_result": validation_result(
                stage_ids[0], passed=True, errors=[]
            ),
        }
    )
    assert migrate_v51._migrate_status(
        payload(first_stage=current_validated), run
    ) is None

    current_blocked = pending(note="current blocked result")
    current_blocked.update(
        {
            "status": "blocked",
            "validated_at": timestamp,
            "last_result": validation_result(
                stage_ids[0], passed=False, errors=["synthetic blocker"]
            ),
        }
    )
    assert migrate_v51._migrate_status(
        payload(first_stage=current_blocked), run
    ) is None

    stale_validated = dict(current_validated)
    stale_validated["validated_at"] = "2026-09-02T12:00:00"
    migrated = migrate_v51._migrate_status(
        payload(first_stage=stale_validated), run
    )
    assert migrated is not None
    assert migrated["stages"][stage_ids[0]]["status"] == "pending"
    assert migrate_v51.REAPPROVAL_MARKER in migrated["stages"][stage_ids[0]]["note"]

    stale_blocked = dict(current_blocked)
    stale_blocked["last_result"] = validation_result(
        stage_ids[0], passed=True, errors=[]
    )
    migrated = migrate_v51._migrate_status(
        payload(first_stage=stale_blocked), run
    )
    assert migrated is not None
    assert migrated["stages"][stage_ids[0]]["status"] == "pending"
    assert migrate_v51.REAPPROVAL_MARKER in migrated["stages"][stage_ids[0]]["note"]

    validation_with_approval_residue = dict(current_validated)
    validation_with_approval_residue.update(
        {
            "approved_at": timestamp,
            "approved_by": "STALE_REVIEWER",
            "approved_input_sha256": digest,
        }
    )
    migrated = migrate_v51._migrate_status(
        payload(first_stage=validation_with_approval_residue), run
    )
    assert migrated is not None
    cleaned = migrated["stages"][stage_ids[0]]
    assert cleaned["status"] == "validated"
    assert cleaned["validated_at"] == timestamp
    assert cleaned["last_result"] == current_validated["last_result"]
    assert cleaned["approved_at"] is None
    assert cleaned["approved_by"] is None
    assert cleaned["approved_input_sha256"] is None

    # Migration output must also satisfy the live reader, not merely look
    # plausible to the migration helper.  Exercise all three non-pending
    # lifecycle shapes, then a stale-registry migration that downgrades each of
    # them to canonical pending records readable by ensure_state.
    status_run = run / "_migration_status_unit"
    status_run.mkdir(parents=True, exist_ok=True)
    current_mixed = payload()
    current_mixed["run_id"] = status_run.name
    current_mixed["stages"][stage_ids[0]] = approved(stage_ids[0])
    current_mixed["stages"][stage_ids[1]] = {
        **pending(note="current validated"),
        "status": "validated",
        "validated_at": timestamp,
        "last_result": validation_result(stage_ids[1], passed=True, errors=[]),
    }
    current_mixed["stages"][stage_ids[2]] = {
        **pending(note="current blocked"),
        "status": "blocked",
        "validated_at": timestamp,
        "last_result": validation_result(
            stage_ids[2], passed=False, errors=["synthetic blocker"]
        ),
    }
    assert migrate_v51._migrate_status(current_mixed, status_run) is None
    write_json_file(status_run / "V51_STATUS.json", current_mixed)
    readable = v51_orchestrator.ensure_state(status_run, persist=False)
    assert readable["stages"][stage_ids[0]]["status"] == "approved"
    assert readable["stages"][stage_ids[1]]["status"] == "validated"
    assert readable["stages"][stage_ids[2]]["status"] == "blocked"

    stale_mixed = json.loads(json.dumps(current_mixed))
    stale_mixed["stage_registry_sha256"] = legacy_hash
    for stage_id in stage_ids[:3]:
        stale_mixed["stages"][stage_id]["last_result"][
            "stage_registry_sha256"
        ] = legacy_hash
    migrated_stale_mixed = migrate_v51._migrate_status(stale_mixed, status_run)
    assert migrated_stale_mixed is not None
    for stage_id in stage_ids[:3]:
        assert migrated_stale_mixed["stages"][stage_id]["status"] == "pending"
        assert migrate_v51.REAPPROVAL_MARKER in migrated_stale_mixed["stages"][stage_id]["note"]
    write_json_file(status_run / "V51_STATUS.json", migrated_stale_mixed)
    readable = v51_orchestrator.ensure_state(status_run, persist=False)
    assert all(
        readable["stages"][stage_id]["status"] == "pending"
        for stage_id in stage_ids[:3]
    )

    # The 13-to-14-column consistency migration inserts waiver_reason without
    # shifting or reinterpreting legacy notes (or any other field).
    migration_run = run / "_migration_unit"
    migration_matrix = migration_run / "consistency/CONSISTENCY_MATRIX.csv"
    legacy_row = {
        field: f"legacy-{index:02d}"
        for index, field in enumerate(migrate_v51.LEGACY_CONSISTENCY_FIELDS, start=1)
    }
    legacy_row["notes"] = "LEGACY_NOTES_SENTINEL"
    write_csv_rows(
        migration_matrix,
        migrate_v51.LEGACY_CONSISTENCY_FIELDS,
        [legacy_row],
    )
    migration_plan = migrate_v51.build_plan(migration_run, fork_decisions={})
    assert len(migration_plan) == 1, migration_plan
    migration_item = migration_plan[0]
    assert migration_item["relative"] == "consistency/CONSISTENCY_MATRIX.csv"
    migrated_row = migration_item["payload"][0]
    for field in migrate_v51.LEGACY_CONSISTENCY_FIELDS:
        assert migrated_row[field] == legacy_row[field], field
    assert migrated_row["waiver_reason"] == ""
    migration_manifest = migrate_v51.apply_plan(
        migration_run,
        migration_plan,
        decision_source=None,
    )
    assert migration_manifest is not None and migration_manifest.is_file()
    applied_rows = common.read_csv(migration_matrix)
    assert applied_rows[0]["notes"] == "LEGACY_NOTES_SENTINEL"
    assert applied_rows[0]["waiver_reason"] == ""
    assert migrate_v51.build_plan(migration_run, fork_decisions={}) == []

    canonical_matrix_text = migration_matrix.read_text(encoding="utf-8")
    migration_matrix.write_text("unknown_legacy_column\n", encoding="utf-8")
    expect_exit(
        2,
        migrate_v51.build_plan,
        migration_run,
        fork_decisions={},
    )
    migration_matrix.write_text(canonical_matrix_text, encoding="utf-8")

    # Abstract-fact migration is fail-closed: missing claim IDs are derived
    # only through a unique evidence-ledger binding, keywords cannot be
    # invented for nonempty facts, and an already-present claim_ids field must
    # not skip created_at migration/validation.
    abstract_run = run / "_migration_abstract_unit"
    write_csv_rows(
        abstract_run / "evidence/CLAIM_EVIDENCE_LEDGER.csv",
        harness.CLAIM_FIELDS,
        [
            {
                "claim_id": "CLAIM-MIGRATION",
                "evidence_id": "EVID-MIGRATION",
            }
        ],
    )
    legacy_fact = {
        "fact_id": "AF-MIGRATION",
        "question": "Q1",
        "purpose": "exercise migration",
        "method": "deterministic fixture",
        "result": "a bounded synthetic result",
        "scope": "migration unit",
        "evidence_ids": ["EVID-MIGRATION"],
        "limitations": ["synthetic only"],
        "frozen": True,
        "verified_by": "SELFTEST_REVIEWER",
    }
    migrated_facts = migrate_v51._migrate_abstract_facts(
        {"keywords": [" migration "], "facts": [legacy_fact]}, abstract_run
    )
    assert migrated_facts is not None
    assert migrated_facts["schema_version"] == 1
    assert migrated_facts["keywords"] == ["migration"]
    migrated_fact = migrated_facts["facts"][0]
    assert migrated_fact["claim_ids"] == ["CLAIM-MIGRATION"]
    assert migrate_v51._valid_iso_timestamp(migrated_fact["created_at"])

    existing_claim_without_time = {
        **legacy_fact,
        "claim_ids": ["CLAIM-MIGRATION"],
    }
    migrated_missing_time = migrate_v51._migrate_abstract_facts(
        {
            "schema_version": 1,
            "keywords": ["migration"],
            "facts": [existing_claim_without_time],
        },
        abstract_run,
    )
    assert migrated_missing_time is not None
    assert migrate_v51._valid_iso_timestamp(
        migrated_missing_time["facts"][0]["created_at"]
    )

    expect_exit(
        2,
        migrate_v51._migrate_abstract_facts,
        {"schema_version": 1, "keywords": [], "facts": [legacy_fact]},
        abstract_run,
    )
    unknown_evidence_fact = {**legacy_fact, "evidence_ids": ["EVID-UNKNOWN"]}
    expect_exit(
        2,
        migrate_v51._migrate_abstract_facts,
        {"schema_version": 1, "keywords": ["migration"], "facts": [unknown_evidence_fact]},
        abstract_run,
    )
    invalid_time_fact = {
        **existing_claim_without_time,
        "created_at": "2026-09-02T12:00:00",
    }
    expect_exit(
        2,
        migrate_v51._migrate_abstract_facts,
        {"schema_version": 1, "keywords": ["migration"], "facts": [invalid_time_fact]},
        abstract_run,
    )
    canonical_fact_payload = {
        "schema_version": 1,
        "keywords": ["migration"],
        "facts": [
            {
                **existing_claim_without_time,
                "created_at": timestamp,
            }
        ],
    }
    assert migrate_v51._migrate_abstract_facts(
        canonical_fact_payload, abstract_run
    ) is None


def solver_execution_regression(scratch: Path) -> None:
    """Exercise fail-closed solver checks without optional solver packages."""
    scratch.mkdir(parents=True, exist_ok=True)

    class FakeMatrix:
        def __init__(self, values):
            self.values = values
            if isinstance(values, list) and values and isinstance(values[0], list):
                self.shape = (len(values), len(values[0]))
            elif isinstance(values, list):
                self.shape = (len(values), 1)
            else:
                self.shape = (1, 1)

        def jacobian(self, _variables):
            return FakeMatrix([[1]])

        def __sub__(self, other):
            return FakeMatrix(
                [
                    [left - right for left, right in zip(left_row, right_row)]
                    for left_row, right_row in zip(self.values, other.values)
                ]
            )

        def __iter__(self):
            for row in self.values:
                if isinstance(row, list):
                    yield from row
                else:
                    yield row

        def __str__(self):
            return str(self.values)

    fake_sympy = types.ModuleType("sympy")
    fake_sympy.MatrixBase = FakeMatrix
    fake_sympy.Symbol = lambda value: value
    fake_sympy.sympify = lambda value, locals=None: value
    fake_sympy.simplify = lambda value: value
    fake_sympy.Matrix = FakeMatrix
    fake_sympy.hessian = lambda _expression, _variables: FakeMatrix([[1]])
    symbolic_spec = scratch / "symbolic.json"
    symbolic_output = scratch / "symbolic-output.json"
    write_json_file(
        symbolic_spec,
        {
            "checks": [
                {
                    "id": "J-MISSING",
                    "type": "jacobian",
                    "symbols": ["x"],
                    "variables": ["x"],
                    "expressions": ["x"],
                },
                {
                    "id": "H-WRONG-ELEMENT",
                    "type": "hessian",
                    "symbols": ["x"],
                    "variables": ["x"],
                    "expression": "x*x",
                    "expected": [[0]],
                },
            ]
        },
    )
    with patch.dict(sys.modules, {"sympy": fake_sympy}):
        assert math_workbench.cmd_symbolic(
            ns(spec=str(symbolic_spec), output=str(symbolic_output))
        ) == 3
    symbolic_result = json.loads(symbolic_output.read_text(encoding="utf-8-sig"))
    symbolic_status = {
        item["id"]: item["status"] for item in symbolic_result["results"]
    }
    assert symbolic_status == {"J-MISSING": "ERROR", "H-WRONG-ELEMENT": "FAIL"}

    class FakeArray:
        def __init__(self, value):
            self.value = float(value)
            self.size = 1

    class FakeFinite:
        def __init__(self, value):
            self.value = value

        def all(self):
            return self.value

    fake_numpy = types.ModuleType("numpy")
    fake_numpy.asarray = lambda value, dtype=float: FakeArray(value)
    fake_numpy.isfinite = lambda array: FakeFinite(math.isfinite(array.value))
    fake_numpy.abs = lambda array: abs(array.value)
    fake_numpy.max = lambda value: value

    class FakeCvxVariable:
        value = float("nan")

        @staticmethod
        def name():
            return "x"

    class FakeCvxConstraint:
        @staticmethod
        def name():
            return "capacity"

        @staticmethod
        def violation():
            return 0.25

    class FakeCvxProblem:
        status = "optimal"
        constraints = [FakeCvxConstraint()]

        @staticmethod
        def is_dcp():
            return True

        @staticmethod
        def is_dpp():
            return True

        @staticmethod
        def variables():
            return [FakeCvxVariable()]

        @staticmethod
        def solve(**_kwargs):
            return 1.0

    fake_cvxpy = types.ModuleType("cvxpy")
    cvx_plugin = types.SimpleNamespace(build_problem=lambda: FakeCvxProblem())
    cvx_output = scratch / "cvxpy-output.json"
    with (
        patch.dict(sys.modules, {"cvxpy": fake_cvxpy, "numpy": fake_numpy}),
        patch.object(math_workbench, "_load_plugin", return_value=cvx_plugin),
    ):
        assert math_workbench.cmd_cvxpy(
            ns(
                plugin="unused.py",
                output=str(cvx_output),
                require_dcp=True,
                solve=True,
                solver=None,
                feasibility_tol=1e-7,
            )
        ) == 3
    cvx_result = json.loads(cvx_output.read_text(encoding="utf-8-sig"))
    assert cvx_result["solve_succeeded"] is False, cvx_result
    assert cvx_result["nonfinite_variables"] == ["x"], cvx_result
    assert cvx_result["violated_constraints"] == ["capacity"], cvx_result

    var_kind, constraint_kind, objective_kind = object(), object(), object()

    class FakePyomoVariable:
        def __init__(self, name, value, lb, ub):
            self.name, self.value, self.lb, self.ub = name, value, lb, ub

        def has_lb(self):
            return self.lb is not None

        def has_ub(self):
            return self.ub is not None

    class FakePyomoObjective:
        name = "objective"
        value = 1.0

    variables = [
        FakePyomoVariable("x", 2.0, 0.0, 1.0),
        FakePyomoVariable("y", 0.0, float("nan"), 1.0),
    ]

    class FakePyomoModel:
        @staticmethod
        def component_data_objects(kind, active=False):
            if kind is var_kind:
                return list(variables)
            if kind is constraint_kind:
                return []
            if kind is objective_kind:
                return [FakePyomoObjective()]
            return []

    solved = types.SimpleNamespace(
        solver=types.SimpleNamespace(termination_condition="optimal", status="ok"),
        problem=[types.SimpleNamespace(lower_bound=1.0, upper_bound=1.0)],
    )

    class FakePyomoSolver:
        @staticmethod
        def available(_exception_flag):
            return True

        @staticmethod
        def solve(_model, tee=False):
            return solved

    fake_pyo = types.ModuleType("pyomo.environ")
    fake_pyo.Var = var_kind
    fake_pyo.Constraint = constraint_kind
    fake_pyo.Objective = objective_kind
    fake_pyo.SolverFactory = lambda _name: FakePyomoSolver()
    fake_pyo.value = lambda value, exception=False: getattr(value, "value", value)
    fake_pyomo_package = types.ModuleType("pyomo")
    fake_pyomo_package.__path__ = []
    fake_pyomo_package.environ = fake_pyo
    pyomo_plugin = types.SimpleNamespace(build_model=lambda: FakePyomoModel())
    pyomo_output = scratch / "pyomo-output.json"
    with (
        patch.dict(
            sys.modules,
            {"pyomo": fake_pyomo_package, "pyomo.environ": fake_pyo},
        ),
        patch.object(math_workbench, "_load_plugin", return_value=pyomo_plugin),
    ):
        assert math_workbench.cmd_pyomo(
            ns(
                plugin="unused.py",
                output=str(pyomo_output),
                solve=True,
                solver="fake",
                feasibility_tol=1e-7,
            )
        ) == 3
    pyomo_result = json.loads(pyomo_output.read_text(encoding="utf-8-sig"))
    assert pyomo_result["solve_succeeded"] is False, pyomo_result
    assert pyomo_result["violated_variable_bounds"] == ["x"], pyomo_result
    assert pyomo_result["invalid_variable_bounds"] == ["y"], pyomo_result


def run_regression() -> None:
    run_id = f"_SELFTEST_V51_{os.getpid()}"
    run = ROOT / "runs" / run_id
    if run.exists():
        shutil.rmtree(run)
    try:
        for rel in (
            "problem",
            "interpretation",
            "data",
            "planning",
            "research/ideas",
            "research/modules",
            "selection",
            "synthesis",
            "code",
            "results",
            "experiments",
            "evidence",
            "figures",
            "paper",
            "reviews",
            "claims",
            "consistency",
            "sources",
            "decisions",
            "ai",
            "deliverables",
            "ideas",
            "disputes",
            "math",
            "sensitivity",
        ):
            (run / rel).mkdir(parents=True, exist_ok=True)
        (run / "problem/PROBLEM_BRIEF.md").write_text(
            "# Synthetic problem\n\nA target may be modeled as its centroid or full geometry.\n",
            encoding="utf-8",
        )
        (run / "data/DATA_MANIFEST.json").write_text('{"files": []}\n', encoding="utf-8")
        (run / "data/DATA_AUDIT.md").write_text("# Data audit\n\nSynthetic only.\n", encoding="utf-8")
        config = common.load_config()
        write_json_file(
            run / "RUN_META.json",
            {
                "version": config["version"],
                "run_id": run_id,
                "contest": "synthetic-offline-regression",
                "problem": "v5.1 lifecycle controls",
                "phase": "rehearsal",
                "runtime": "codex",
                "created_at": common.now_iso(),
                "project_root": str(ROOT),
                "run_path": run.relative_to(ROOT).as_posix(),
                "input_policy": "synthetic self-test; canonical inputs remain read-only",
                "notes": "No network, model, or paid API call.",
            },
        )
        write_json_file(run / "STATUS.json", harness._initial_status(config, run_id))
        budget_fixture = common.read_json(
            ROOT / "config" / "default_budget.json", expect=dict
        )
        budget_fixture["version"] = config["version"]
        budget_fixture["created_at"] = common.now_iso()
        budget_fixture["updated_at"] = common.now_iso()
        write_json_file(run / "BUDGET.json", budget_fixture)
        migration_status_regression(run)
        solver_execution_regression(run / "_solver_unit")

        # paper_v5 must compile into a real PAPER_PLAN section, never an orphan
        # fallback.  Merely initializing the extension is not enough.
        paper_v5.ensure(run)
        expect_exit(2, paper_v5.cmd_build_abstract, ns(run=run_id))

        # A scaffolded, header-only strong-claim table is not proof that a scan
        # actually happened.
        write_csv_rows(run / "claims/STRONG_CLAIMS.csv", claim_guard.FIELDS)
        report, code = claim_guard.lint(run, strict=True)
        assert_failed(report, code, "scan")
        write_csv_rows(run / "claims/STRONG_CLAIMS_MATH.csv", claim_guard.FIELDS)
        math_report, math_code = claim_guard.lint(
            run, strict=True, scope="math"
        )
        assert_failed(math_report, math_code, "scan")
        # Keep this isolated negative fixture from poisoning the later all-scope
        # scan, which correctly requires the math registry and receipt as a pair.
        (run / "claims/STRONG_CLAIMS_MATH.csv").unlink()

        # Strict consistency cannot pass on an empty scaffold.
        consistency_matrix.cmd_init(ns(run=run_id))
        report, code = consistency_matrix.derive(run, strict=True)
        assert_failed(report, code, "No consistency checks")

        # G3 must reject present-but-empty experiment/evidence scaffolds.
        (run / "experiments/EXPERIMENT_PLAN.md").write_text(
            "# Experiment plan\n\nSynthetic regression fixture with declared replay requirements.\n",
            encoding="utf-8",
        )
        write_csv_rows(
            run / "experiments/BASELINE_REGISTRY.csv",
            ["baseline_id", "name", "purpose", "implementation_path", "metric", "expected_role", "status", "notes"],
        )
        write_csv_rows(run / "experiments/EXPERIMENT_LOG.csv", run_experiments.LOG_FIELDS)
        write_csv_rows(run / "experiments/METRICS.csv", run_experiments.METRIC_FIELDS)
        write_csv_rows(run / "evidence/CLAIM_EVIDENCE_LEDGER.csv", harness.CLAIM_FIELDS)
        g3_empty = harness.validate_gate(run, "G3", strict=True)
        assert not g3_empty["ok"], g3_empty
        g3_errors = "\n".join(g3_empty["errors"])
        assert "核心主张" in g3_errors or "论文主张" in g3_errors, g3_errors
        assert "EXPERIMENT_LOG.csv" in g3_errors, g3_errors
        assert "METRICS.csv" in g3_errors, g3_errors
        assert g3_empty["experiment_audit"]["passed_log_rows"] == 0
        assert g3_empty["experiment_audit"]["valid_receipt_count"] == 0
        assert g3_empty["experiment_audit"]["valid_metric_count"] == 0

        # --force only bypasses prerequisite order. Approval remains read-only
        # until a current human review exists, even for an invalid Gate.
        pre_invalid_status = (run / "STATUS.json").read_bytes()
        invalid_log_path = run / "decisions/DECISION_LOG.jsonl"
        pre_invalid_log = (
            invalid_log_path.read_bytes() if invalid_log_path.is_file() else None
        )
        expect_exit(
            2,
            harness.cmd_approve,
            ns(
                run=run_id,
                gate="G3",
                strict=True,
                force=True,
                by="SELFTEST_REVIEWER",
                note="force must not bypass an invalid current gate",
            ),
        )
        assert (run / "STATUS.json").read_bytes() == pre_invalid_status
        assert (
            invalid_log_path.read_bytes() if invalid_log_path.is_file() else None
        ) == pre_invalid_log
        assert not (run / "reviews/gates/G3_REVIEW.md").exists()

        # Keep the runner's narrow freshness unit cases outside the main run's
        # experiment ledger.  G3 below is populated only by a genuine,
        # provenance-complete EXP-SS001 execution.
        runner_unit = run / "_runner_unit"
        (runner_unit / "results").mkdir(parents=True, exist_ok=True)
        (runner_unit / "experiments").mkdir(parents=True, exist_ok=True)
        write_csv_rows(runner_unit / "experiments/EXPERIMENT_LOG.csv", run_experiments.LOG_FIELDS)
        write_csv_rows(runner_unit / "experiments/METRICS.csv", run_experiments.METRIC_FIELDS)

        # A zero-exit process may not reuse an unchanged expected artifact.
        stale_artifact = runner_unit / "results/stale-output.json"
        stale_artifact.write_text('{"old": true}\n', encoding="utf-8")
        executable_fixture = [sys.executable, "-c", "pass"]
        completed = subprocess.CompletedProcess(executable_fixture, 0, "", "")
        with patch.object(run_experiments.subprocess, "run", return_value=completed):
            stale_result = run_experiments.execute_one(
                runner_unit,
                {
                    "id": "EXP-STALE",
                    "module_id": "MOD-STALE",
                    "description": "unchanged-artifact regression",
                    "command": executable_fixture,
                    "cwd": ".",
                    "timeout_sec": 5,
                    "expected_artifacts": ["results/stale-output.json"],
                },
                7,
                ns(manifest_path="synthetic-manifest.json", manifest_sha256="0" * 64, dry_run=False),
            )
        assert stale_result["status"] == "stale-artifacts", stale_result
        assert stale_result["stale_expected_artifacts"] == ["results/stale-output.json"]

        # A timestamp-only rewrite is not evidence that an attempt produced a
        # new result. Content-identical output must remain stale.
        mtime_artifact = runner_unit / "results/mtime-output.json"
        mtime_artifact.write_text('{"same": true}\n', encoding="utf-8")
        original_mtime = mtime_artifact.stat().st_mtime_ns

        def touch_without_content_change(*_args, **_kwargs):
            stat = mtime_artifact.stat()
            os.utime(
                mtime_artifact,
                ns=(stat.st_atime_ns, max(stat.st_mtime_ns + 1_000_000_000, original_mtime + 1)),
            )
            return subprocess.CompletedProcess(executable_fixture, 0, "", "")

        with patch.object(run_experiments.subprocess, "run", side_effect=touch_without_content_change):
            mtime_result = run_experiments.execute_one(
                runner_unit,
                {
                    "id": "EXP-MTIME",
                    "module_id": "MOD-STALE",
                    "description": "same bytes with a changed mtime",
                    "command": executable_fixture,
                    "cwd": ".",
                    "timeout_sec": 5,
                    "expected_artifacts": ["results/mtime-output.json"],
                },
                7,
                ns(manifest_path="synthetic-manifest.json", manifest_sha256="0" * 64, dry_run=False),
            )
        assert mtime_result["status"] == "stale-artifacts", mtime_result
        assert mtime_result["artifacts"][0]["fresh"] is False, mtime_result
        assert mtime_result["artifacts"][0]["pre_sha256"] == mtime_result["artifacts"][0]["sha256"]

        # Canonical all-scope scan with no strong claims is a valid clean scan.
        # Explicit denials and open derivation statements must not be promoted
        # into claims merely because they contain trigger words.
        neutral_claim_text = (
            "# Neutral derivation language\n\n"
            "有限搜索并非理论上界，也不能由此证明全局最优。\n"
            "唯一性尚未证明；显著优于的说法在统计检验前不成立。\n"
            "本段只讨论唯一性；样例均值显著优于旧值；“由此证明”是推导连接语。\n"
            "我们采用统计显著性检验，显著性水平设为 0.05。\n"
            "This numerical envelope is not a rigorous upper bound. The global optimum remains open.\n"
            "# 中性的审计词表示例标题\n"
            "```text\n该模型存在唯一解；差异统计显著；我们已经证明全局性质。\n```\n"
            "<!-- 该模型存在唯一解；差异统计显著；我们已经证明全局性质。 -->\n"
        )
        (run / "synthesis/FINAL_MODEL_SPEC.md").write_text(neutral_claim_text, encoding="utf-8")
        assert claim_guard.cmd_scan(ns(run=run_id, scope="all", path=[])) == 0
        with (run / "claims/STRONG_CLAIMS.csv").open(encoding="utf-8", newline="") as handle:
            assert list(csv.DictReader(handle)) == []
        report, code = claim_guard.lint(run, strict=True)
        assert code == 0, report

        clean_receipt_path = run / "claims/STRONG_CLAIM_SCAN.json"
        clean_receipt_bytes = clean_receipt_path.read_bytes()
        foreign_receipt = json.loads(clean_receipt_bytes.decode("utf-8"))
        foreign_receipt["run_id"] = "_ANOTHER_RUN"
        write_json_file(clean_receipt_path, foreign_receipt)
        foreign_report, foreign_code = claim_guard.lint(run, strict=True)
        assert_failed(foreign_report, foreign_code, "belongs to run")
        clean_receipt_path.write_bytes(clean_receipt_bytes)

        scan_receipt_path = run / "claims/STRONG_CLAIM_SCAN.json"
        canonical_scan_receipt_bytes = scan_receipt_path.read_bytes()
        for field, value in (
            ("file_count", "not-an-integer"),
            ("unexpected_receipt_field", "must be rejected"),
        ):
            malformed_receipt = json.loads(canonical_scan_receipt_bytes.decode("utf-8"))
            malformed_receipt[field] = value
            write_json_file(scan_receipt_path, malformed_receipt)
            malformed_report, malformed_code = claim_guard.lint(run, strict=True)
            assert_failed(malformed_report, malformed_code)
            malformed_messages = "\n".join(malformed_report["errors"])
            assert (
                field in malformed_messages
                or "schema" in malformed_messages.lower()
            ), malformed_messages
            scan_receipt_path.write_bytes(canonical_scan_receipt_bytes)
        report, code = claim_guard.lint(run, strict=True)
        assert code == 0, report

        # Bare/negated phrases and Markdown metadata above stay quiet, while
        # explicit assertion forms in prose remain discoverable.
        # Use a canonical all-scope directory; synthesis scans only the final spec.
        language_probe = run / "problem/claim-language-probe.md"
        language_probe.write_text(
            "# Claim language probe\n\n"
            "## 本文方法达到全局最优\n\n"
            "基线并非全局最优；本文方法达到全局最优。\n"
            "我们不声称该模型存在唯一解，也不声称这是全局最优解。\n"
            "该模型存在唯一解。组间差异统计显著。我们已经证明全局性质成立。\n"
            "This method is not heuristic and is globally optimal.\n"
            "Without loss of generality, the stated solution is globally optimal.\n",
            encoding="utf-8",
        )
        assert claim_guard.cmd_scan(ns(run=run_id, scope="all", path=[])) == 0
        with (run / "claims/STRONG_CLAIMS.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            language_claims = list(csv.DictReader(handle))
        assert {
            "unique_solution",
            "statistical_significance",
            "proof_claim",
        }.issubset({row["claim_type"] for row in language_claims}), language_claims
        global_claims = [
            row for row in language_claims if row["claim_type"] == "global_optimum"
        ]
        assert len(global_claims) >= 4, global_claims
        assert any(row["excerpt"].startswith("## ") for row in global_claims), global_claims
        mixed_global_claims = [
            row for row in global_claims if row["excerpt"].startswith("基线并非全局最优")
        ]
        assert len(mixed_global_claims) == 1, mixed_global_claims
        assert not any(
            row["excerpt"].startswith("我们不声称") for row in language_claims
        ), language_claims
        assert any("not heuristic" in row["excerpt"] for row in global_claims)
        assert any("Without loss of generality" in row["excerpt"] for row in global_claims)
        language_probe.unlink()
        assert claim_guard.cmd_scan(ns(run=run_id, scope="all", path=[])) == 0
        with (run / "claims/STRONG_CLAIMS.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            assert list(csv.DictReader(handle)) == []

        # A partial path scan cannot stand in for a canonical all-scope scan.
        assert claim_guard.cmd_scan(
            ns(run=run_id, scope="all", path=["synthesis/FINAL_MODEL_SPEC.md"])
        ) == 0
        report, code = claim_guard.lint(run, strict=True)
        assert_failed(report, code, "partial")
        assert claim_guard.cmd_scan(ns(run=run_id, scope="all", path=[])) == 0

        # The receipt is content-bound and becomes stale as soon as the scanned
        # corpus changes; restoring the exact corpus makes the receipt current.
        (run / "synthesis/FINAL_MODEL_SPEC.md").write_text(
            neutral_claim_text + "\nA new unscanned sentence.\n", encoding="utf-8"
        )
        report, code = claim_guard.lint(run, strict=True)
        assert_failed(report, code, "changed after scan")
        (run / "synthesis/FINAL_MODEL_SPEC.md").write_text(neutral_claim_text, encoding="utf-8")
        report, code = claim_guard.lint(run, strict=True)
        assert code == 0, report

        v51_orchestrator.ensure_state(run)
        assert v51_orchestrator.cmd_approve(
            ns(
                run=run_id,
                stage="interpretation",
                review_id=None,
                by="SELFTEST_REVIEWER",
                note="incomplete interpretation must remain blocked",
            )
        ) == 3
        blocked_v51_state = json.loads(
            (run / "V51_STATUS.json").read_text(encoding="utf-8-sig")
        )
        blocked_interpretation = blocked_v51_state["stages"]["interpretation"]
        assert blocked_interpretation["status"] == "blocked", blocked_interpretation
        assert blocked_interpretation["last_result"]["passed"] is False
        assert blocked_interpretation["last_result"]["errors"]
        assert blocked_interpretation["approved_input_sha256"] is None
        interpretation_engine.ensure(run)
        (run / "interpretation/PROBLEM_SEMANTICS.md").write_text(
            "# Problem Semantics\n\nThe statement gives a geometric target but does not settle "
            "whether coverage refers to a centroid point or the complete target set. The choice "
            "changes the objective and feasible set, so it is a structural interpretation fork.\n",
            encoding="utf-8",
        )
        (run / "interpretation/INTERPRETATION_AUDIT.md").write_text(
            "# Interpretation Audit\n\nThe two target-geometry branches were independently rebuilt, "
            "re-optimized, compared on identical inputs, and closed by a named human reviewer.\n",
            encoding="utf-8",
        )

        interpretation_engine.cmd_fork_add(
            ns(
                run=run_id,
                id="IF-001",
                dimension="target_geometry",
                question="Q1/Q2: point target versus full target set",
                option=["P=centroid point", "V=full target set"],
                impact="critical",
                uncertainty="high",
                irreversibility="high",
                changes_objective=True,
                changes_feasible_set=True,
                expected_result_change_pct=49.0,
                structural=True,
                note="regression case",
            )
        )
        expect_exit(
            2,
            interpretation_engine.cmd_decide,
            ns(
                run=run_id,
                fork_id="IF-001",
                select="V",
                by="AUTO",
                human_confirm=True,
                evidence=[],
                reason="automatic default",
                accept_unprobed_risk=True,
            ),
        )

        # Algorithm diversity must not inflate interpretation diversity.
        for candidate, algorithm in (("IDEA_001", "genetic_algorithm"), ("IDEA_002", "milp")):
            interpretation_engine.cmd_fingerprint_set(
                ns(
                    run=run_id,
                    candidate=candidate,
                    set=[
                        "target_geometry=point_centroid",
                        "aggregation_rule=interval_union",
                        "objective_definition=covered_time",
                        f"algorithm={algorithm}",
                    ],
                    source=f"research/ideas/{candidate}/IDEA_CARD.json",
                )
            )
        diversity = interpretation_engine.analyze_assumption_diversity(run)
        assert diversity["shared_structural_dimensions"]["target_geometry"] == "point_centroid"
        assert diversity["algorithm_diversity_present"] is True
        assert diversity["interpretation_unique_signatures"] == 1

        result_file = run / "results/SS-001.json"
        result_file.write_text(
            json.dumps({"point": 1.0, "full_target": 1.49, "effect_pct": 49}, indent=2) + "\n",
            encoding="utf-8",
        )
        # The core quantitative claim must bind a runner-owned metrics
        # artifact, not an arbitrary result-shaped text/JSON file.  Generate
        # the canonical receipt/request/stdout chain through execute_one before
        # any mathematical-stage validation resolves EVID-001.
        metrics_artifact = run / "results/EXP-SS001-metrics.json"
        metrics_relative = metrics_artifact.relative_to(run).as_posix()
        experiment_definition = {
            "id": "EXP-SS001",
            "module_id": "MOD_001",
            "description": "deterministic synthetic structural comparison",
            "command": [sys.executable, "-c", "pass"],
            "cwd": ".",
            "timeout_sec": 5,
            "seeds": [7],
            "expected_artifacts": [metrics_relative],
            "metrics_file": metrics_relative,
        }
        runner_manifest_path = run / "experiments/EXPERIMENT_MANIFEST.json"
        write_json_file(
            runner_manifest_path,
            {"run_id": run_id, "experiments": [experiment_definition]},
        )

        def emit_core_metrics(command, *_args, **_kwargs):
            write_json_file(
                metrics_artifact,
                {
                    "metrics": [
                        {
                            "split": "synthetic",
                            "metric": "objective",
                            "value": 1.49,
                            "unit": "dimensionless",
                            "seed": "7",
                        }
                    ]
                },
            )
            return subprocess.CompletedProcess(command, 0, "synthetic objective=1.49\n", "")

        with patch.object(
            run_experiments.subprocess, "run", side_effect=emit_core_metrics
        ):
            runner_result = run_experiments.execute_one(
                run,
                experiment_definition,
                7,
                ns(
                    manifest_path=str(runner_manifest_path.resolve()),
                    manifest_sha256=common.sha256_file(runner_manifest_path),
                    dry_run=False,
                ),
            )
        assert runner_result["status"] == "passed", runner_result
        receipt = run / str(runner_result["record_dir"]) / "run.json"
        request_path = run / str(runner_result["record_dir"]) / "request.json"
        stdout_path = run / str(runner_result["stdout_path"])
        assert receipt.is_file() and request_path.is_file() and stdout_path.is_file()
        write_csv_rows(
            run / "evidence/CLAIM_EVIDENCE_LEDGER.csv",
            harness.CLAIM_FIELDS,
            [
                {
                    "claim_id": "CLAIM-001",
                    "claim_text": "The full-target branch changes the synthetic objective by 49%.",
                    "claim_type": "quantitative",
                    "is_core": "true",
                    "evidence_id": "EVID-001",
                    "evidence_type": "experiment",
                    "evidence_path": "results/EXP-SS001-metrics.json",
                    "generation_command": "synthetic-self-test",
                    "result_summary": "+49% under the declared fixture",
                    "verification_status": "verified",
                    "verified_by": "SELFTEST_REVIEWER",
                    "verified_at": "2026-09-02T12:00:00+08:00",
                    "paper_location": "validation_and_results",
                    "notes": "offline regression fixture",
                }
            ],
        )
        # This regression builds a minimal run by hand instead of using
        # harness init, so create both ledgers required by strict audit.
        sensitivity_engine.ensure(run)
        sensitivity_engine.cmd_add(
            ns(
                run=run_id,
                kind="structural",
                id="SS-001",
                target="IF-001 / target_geometry",
                alternatives="point_centroid vs full_target_set",
                metric="objective and ranking",
                experiment="EXP-SS001",
                evidence="results/SS-001.json",
                effect="+49%; principal conclusion changes",
                effect_pct=49.0,
                status="verified",
                by="SELFTEST_REVIEWER",
                note="same inputs; rebuild and re-optimize both structural branches",
            )
        )
        for probe_id, option_id, result in (
            ("PROBE-P", "P", "point objective=1.00"),
            ("PROBE-V", "V", "full-target objective=1.49"),
        ):
            interpretation_engine.cmd_probe_add(
                ns(
                    run=run_id,
                    id=probe_id,
                    fork_id="IF-001",
                    option_id=option_id,
                    probe_type="structural",
                    method="same inputs; independently rebuild and re-optimize branch",
                    artifact="results/SS-001.json",
                    metric="objective and ranking",
                    result=result,
                    status="verified",
                    by="SELFTEST_REVIEWER",
                    note="",
                )
            )
        interpretation_engine.cmd_decide(
            ns(
                run=run_id,
                fork_id="IF-001",
                select="V",
                by="SELFTEST_REVIEWER",
                human_confirm=True,
                evidence=["results/SS-001.json"],
                reason="Full-target semantics match the operation and materially change the result.",
                accept_unprobed_risk=False,
            )
        )
        report, code = interpretation_engine.audit(run, strict=True)
        assert code == 0, report
        report, code = sensitivity_engine.audit(run, strict=True)
        assert code == 0, report

        # Runtime/schema contracts are executable: a temporary legacy header
        # must fail validation, and restoration must recover a clean result.
        forks_path = run / "interpretation/INTERPRETATION_FORKS.csv"
        forks_text = forks_path.read_text(encoding="utf-8")
        forks_lines = forks_text.splitlines()
        forks_path.write_text(
            forks_lines[0].replace("options_json", "options")
            + "\n"
            + "\n".join(forks_lines[1:])
            + "\n",
            encoding="utf-8",
        )
        schema_result = schema_validate.validate_run(run, stage="interpretation")
        assert not schema_result["passed"], schema_result
        assert any("INTERPRETATION_FORKS.csv" in item for item in schema_result["errors"])
        forks_path.write_text(forks_text, encoding="utf-8")
        schema_result = schema_validate.validate_run(run, stage="interpretation")
        assert schema_result["passed"], schema_result

        # Base gates execute the mapped v5.1 validators rather than trusting a
        # pre-existing V51_STATUS.json.  Removing the live fork rows must be
        # visible as a G0 v5.1 failure.
        g0_live = harness.validate_gate(run, "G0", strict=True)
        assert g0_live["schema"]["passed"], g0_live["schema"]
        assert any(
            item["stage"] == "interpretation" and item["result"].get("passed")
            for item in g0_live["v51"]
        ), g0_live
        with forks_path.open(encoding="utf-8-sig", newline="") as handle:
            fork_header = list(csv.reader(handle))[0]
        write_csv_rows(forks_path, fork_header)
        g0 = harness.validate_gate(run, "G0", strict=True)
        assert any("v5.1 interpretation:" in item for item in g0["errors"]), g0
        forks_path.write_text(forks_text, encoding="utf-8")
        report, code = interpretation_engine.audit(run, strict=True)
        assert code == 0, report

        # Build an auditable mathematical module.
        math_engine.create_module(
            run,
            module_id="MOD_001",
            title="Synthetic full-target model",
            question_ids=["Q1", "Q2"],
            method_families=["geometric-spatial"],
            overwrite=False,
        )
        module = run / "research/modules/MOD_001"
        write_long(module / "MODEL_INTENT.md", "Model Intent", "The model represents the complete target set and states purpose, inputs, outputs, rejected alternatives, acceptance criteria and failure scope.")
        write_long(module / "ASSUMPTIONS.md", "Assumptions", "A1 is the human-approved IF-001 branch. Every assumption records source, type, falsification route, impact and structural or parameter sensitivity test.")
        write_long(module / "FORMULATION.md", "Mathematical Formulation", "Let T be the compact target set and x the decision vector. Maximize f(x,T) over a closed feasible set X with explicit kinematic constraints, domains, units, boundary conditions and output mapping.")
        write_long(module / "DERIVATION.md", "Derivation", "EQ-001 follows from the operational definition and interval union. Each nontrivial step names prior equations, conditions and approximation errors. Finite search is not a proof certificate.")
        write_long(module / "PARAMETER_IDENTIFICATION.md", "Parameter Identification", "The target radius is given. Estimated parameters record estimators, sample scope, units, identifiability, calibration protocol, uncertainty and sensitivity cases.")
        write_long(module / "THEORY_CHECKS.md", "Theory Checks", "Units and limiting cases are checked. Existence requires verified continuity and compactness. Convexity, uniqueness and global optimality remain open without independent proof.")
        write_long(module / "NUMERICAL_METHOD.md", "Numerical Method", "A deterministic baseline precedes bounded refinement. Solver status, scaling, initialization, tolerances, stopping rules, complexity, exact small cases and multi-start diagnostics are recorded.")
        write_long(module / "VALIDATION_PROTOCOL.md", "Validation Protocol", "Rebuild point and full-target structures. Run exact small cases, feasibility replay, multiple seeds, structural and parameter sensitivity, ablation and predeclared acceptance tests.")

        math_engine.cmd_symbol_add(
            ns(
                run=run_id,
                module="MOD_001",
                symbol_id="SYM-001",
                latex="x",
                name="decision",
                meaning="decision vector",
                role="decision_variable",
                domain="X subset R^d",
                unit="mixed",
                source="FORMULATION.md",
                notes="",
            )
        )
        math_engine.cmd_equation_add(
            ns(
                run=run_id,
                module="MOD_001",
                equation_id="EQ-001",
                latex="max_x f(x,T)",
                type="objective",
                description="maximize full-target coverage",
                depends_on=[],
                assumptions=["A1"],
                units_check="pass",
                derivation_location="DERIVATION.md",
                verification_ref="results/SS-001.json",
                status="checked",
                notes="",
            )
        )
        math_engine.cmd_parameter_add(
            ns(
                run=run_id,
                module="MOD_001",
                parameter_id="PAR-001",
                symbol="r",
                meaning="target radius",
                value_or_estimator="1.0",
                unit="m",
                source_type="computational",
                source_ref="EVID-001",
                identifiability="identified",
                calibration_protocol="",
                sensitivity_case="SS-001",
                status="verified",
                notes="",
            )
        )
        code_file = run / "code/model.py"
        code_file.write_text("def objective(x):\n    return float(x)\n", encoding="utf-8")
        math_engine.cmd_map_code(
            ns(
                run=run_id,
                module="MOD_001",
                map_id="MAP-001",
                item="EQ-001",
                role="objective",
                code="code/model.py",
                symbol="objective",
                test="",
                experiment="",
                evidence="EVID-001",
                paper="4.2",
                status="implemented",
                notes="",
            )
        )
        math_engine.cmd_obligation_add(
            ns(
                run=run_id,
                module="MOD_001",
                obligation_id="MATH-001",
                severity="critical",
                claim="The numerical envelope is a rigorous upper bound",
                claim_type="rigorous_upper_bound",
                conditions="all feasible decisions are certified below the envelope",
                justification="",
                symbolic_check="",
                numerical_check="finite numerical search",
                counterexample_test="",
                proof_provenance="",
                evidence=[],
                notes="",
            )
        )
        report, code = math_engine.lint_module(run, "MOD_001", strict=True)
        assert code == 3 and "MATH-001" in " ".join(report["errors"])
        math_engine.cmd_obligation_resolve(
            ns(
                run=run_id,
                module="MOD_001",
                obligation_id="MATH-001",
                status="downgraded",
                resolution="Renamed to numerical reference envelope; no rigorous bound is claimed.",
                proof_provenance="",
                evidence=["results/SS-001.json"],
                by="SELFTEST_REVIEWER",
                notes="",
            )
        )
        report, code = math_engine.lint_module(run, "MOD_001", strict=True)
        assert code == 0, report
        (run / "math/DERIVATION_CHECK.md").write_text(
            "# Independent derivation check\n\nThe finite envelope is reported as numerical evidence, "
            "not as a proof certificate or a global-optimality guarantee.\n",
            encoding="utf-8",
        )

        # Strong-claim guard: numerical search cannot certify a rigorous upper bound.
        synthesis = run / "synthesis/FINAL_MODEL_SPEC.md"
        synthesis.write_text("# Final model\n\n数值搜索得到理论上界 100.26。\n", encoding="utf-8")
        assert claim_guard.cmd_scan(ns(run=run_id, scope="math", path=[])) == 0
        with (run / "claims/STRONG_CLAIMS_MATH.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            bound_claims = list(csv.DictReader(handle))
            upper_bound_row = next(
                row
                for row in bound_claims
                if row["claim_type"] == "rigorous_upper_bound"
                and row["path"] == "synthesis/FINAL_MODEL_SPEC.md"
            )
            claim_id = upper_bound_row["claim_id"]
        # The resolved obligation retains the old wording as audit history.
        # Review that historical record separately from the live model claim.
        for row in bound_claims:
            if row["path"].endswith("/MATHEMATICAL_OBLIGATIONS.csv"):
                assert claim_guard.cmd_provenance_set(
                    ns(
                        run=run_id, scope="math", claim_id=row["claim_id"],
                        evidence_type="", evidence_ref="", status="not_applicable",
                        recommended_wording="", by="SELFTEST_REVIEWER",
                        notes="Historical obligation MATH-001 records a resolved downgrade, not a retained bound claim.",
                    )
                ) == 0
        expect_exit(
            2,
            claim_guard.cmd_provenance_set,
            ns(
                run=run_id,
                scope="math",
                claim_id=claim_id,
                evidence_type="numerical_search",
                evidence_ref="results/SS-001.json",
                status="supported",
                recommended_wording="",
                by="SELFTEST_REVIEWER",
                notes="",
            ),
        )
        claim_guard.cmd_provenance_set(
            ns(
                run=run_id,
                scope="math",
                claim_id=claim_id,
                evidence_type="numerical_search",
                evidence_ref="results/SS-001.json",
                status="downgraded",
                recommended_wording="当前搜索区域中观察到的参考上包络为 100.26。",
                by="SELFTEST_REVIEWER",
                notes="",
            )
        )
        report, code = claim_guard.lint(run, strict=True, scope="math")
        assert_failed(report, code)

        # A terminal downgrade is not a waiver for leaving the strong source
        # sentence in place.  Apply the reviewed replacement and rescan the
        # complete canonical corpus; the obsolete claim must disappear.
        synthesis.write_text(
            "# Final model\n\n当前搜索区域中观察到的参考上包络为 100.26。\n",
            encoding="utf-8",
        )
        assert claim_guard.cmd_scan(ns(run=run_id, scope="math", path=[])) == 0
        with (run / "claims/STRONG_CLAIMS_MATH.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            rescanned_claims = list(csv.DictReader(handle))
        assert all(row["claim_id"] != claim_id for row in rescanned_claims), rescanned_claims
        report, code = claim_guard.lint(run, strict=True, scope="math")
        assert code == 0, report

        # G5 consumes a dedicated math-only claim registry/receipt.  Keep one
        # analytically supported claim in stable source text so the later
        # all-corpus scan can prove that reviewed provenance is inherited by
        # claim_id without rewriting the pre-freeze math artifacts.
        math_proof = module / "ANALYTIC_PROOF.md"
        math_proof.write_text(
            "# Analytic proof fixture\n\n"
            "For q(z)=(z-1)^2 on the real line, strict convexity establishes "
            "a unique solution z=1.\n",
            encoding="utf-8",
        )
        assert claim_guard.cmd_scan(ns(run=run_id, scope="math", path=[])) == 0
        math_registry_path = run / "claims/STRONG_CLAIMS_MATH.csv"
        with math_registry_path.open(encoding="utf-8", newline="") as handle:
            math_claims = list(csv.DictReader(handle))
        unique_math_row = next(
            row for row in math_claims if row["claim_type"] == "unique_solution"
        )
        math_claim_id = unique_math_row["claim_id"]
        assert claim_guard.cmd_provenance_set(
            ns(
                run=run_id,
                scope="math",
                claim_id=math_claim_id,
                evidence_type="analytic_proof",
                evidence_ref="research/modules/MOD_001/ANALYTIC_PROOF.md",
                status="supported",
                recommended_wording="",
                by="SELFTEST_REVIEWER",
                notes="The closed-form scalar proof was independently checked.",
            )
        ) == 0
        math_report, math_code = claim_guard.lint(
            run, strict=True, scope="math"
        )
        assert math_code == 0, math_report
        math_receipt_path = run / "claims/STRONG_CLAIM_SCAN_MATH.json"
        math_receipt_text = math_receipt_path.read_text(encoding="utf-8")
        math_registry_text = math_registry_path.read_text(encoding="utf-8")

        # Blind review export must not leak prior reviewer material.
        (run / "reviews/RED_TEAM_REPORT.md").write_text(
            "SECRET_PRIOR_REVIEW_SHOULD_NOT_LEAK\n", encoding="utf-8"
        )
        blind_review.cmd_export(ns(run=run_id, review_id="BR-001", overwrite=True, max_text_mb=5))
        packet_zip = run / "reviews/blind/BR-001/BR-001_packet.zip"
        with zipfile.ZipFile(packet_zip) as archive:
            packet_names = set(archive.namelist())
            assert "interpretation/INTERPRETATION_FORKS.csv" in packet_names
            assert "sensitivity/STRUCTURAL_SENSITIVITY.csv" in packet_names
            assert "math/DERIVATION_CHECK.md" in packet_names
            assert "research/modules/MOD_001/MODULE_SPEC.json" in packet_names
            assert "paper/FINAL_MANUSCRIPT.md" not in packet_names
            for name in packet_names:
                assert not name.startswith("reviews/"), name
                if name.endswith((".md", ".txt", ".json", ".csv")):
                    assert b"SECRET_PRIOR_REVIEW_SHOULD_NOT_LEAK" not in archive.read(name)
        response = run / "blind_response_input.md"

        def write_blind_response(body: str, *, packet_sha256: str | None = None) -> None:
            state = json.loads(
                (run / "reviews/blind/BR-001/REVIEW_STATE.json").read_text(encoding="utf-8")
            )
            response.write_text(
                f"Packet-SHA256: {packet_sha256 or state['packet_sha256']}\n"
                f"Run-ID: {run_id}\nReview-ID: BR-001\n\n{body}",
                encoding="utf-8",
            )

        blocking_response_body = (
            "# Blind review\n\nCritical finding: compare full-target and centroid branches. "
            "A finite search is not a rigorous upper bound. Required corrections include a "
            "structural rerun and proof-provenance downgrade.\n\n"
            "Verdict: PASS_WITH_BLOCKERS\nBlockers: rerun the unresolved branch comparison\n"
        )
        write_blind_response(blocking_response_body)
        expect_exit(
            2,
            blind_review.cmd_import,
            ns(
                run=run_id,
                review_id="BR-001",
                response=str(response),
                reviewer_kind="external-pro",
                session_id="FRESH-SESSION-PLACEHOLDER",
                fresh_context=True,
                attested_by="TEAM",
                note="placeholder identity must fail",
            ),
        )
        blind_review.cmd_import(
            ns(
                run=run_id,
                review_id="BR-001",
                response=str(response),
                reviewer_kind="external-pro",
                session_id="FRESH-SESSION-001",
                fresh_context=True,
                attested_by="SELFTEST_REVIEWER",
                note="synthetic offline test",
            )
        )
        report, code = blind_review.validate(run, "BR-001", strict=True)
        assert_failed(report, code, "PASS_WITH_BLOCKERS")

        passing_response_body = (
            "# Blind review\n\nCritical findings: none after replaying the supplied synthetic branch "
            "comparison. Major findings: none. The finite search remains described only as a "
            "numerical envelope, and the checked evidence paths are internally consistent.\n\n"
            "Verdict: PASS\nBlockers: NONE\n"
        )
        write_blind_response(passing_response_body, packet_sha256="0" * 64)
        expect_exit(
            2,
            blind_review.cmd_import,
            ns(
                run=run_id,
                review_id="BR-001",
                response=str(response),
                reviewer_kind="external-pro",
                session_id="FRESH-SESSION-STALE-PACKET",
                fresh_context=True,
                attested_by="SELFTEST_REVIEWER",
                note="stale packet binding must fail",
            ),
        )
        write_blind_response(passing_response_body)
        blind_review.cmd_import(
            ns(
                run=run_id,
                review_id="BR-001",
                response=str(response),
                reviewer_kind="external-pro",
                session_id="FRESH-SESSION-002",
                fresh_context=True,
                attested_by="SELFTEST_REVIEWER",
                note="synthetic offline pass fixture",
            )
        )
        report, code = blind_review.validate(run, "BR-001", strict=True)
        assert code == 0, report

        # Validation must independently reject a placeholder attestor and a
        # timezone-naive import time, even if metadata is edited after import.
        blind_meta_path = run / "reviews/blind/BR-001/REVIEW_META.json"
        blind_meta_text = blind_meta_path.read_text(encoding="utf-8")
        blind_meta = json.loads(blind_meta_text)
        blind_meta["attested_by"] = "UNKNOWN"
        blind_meta_path.write_text(json.dumps(blind_meta), encoding="utf-8")
        report, code = blind_review.validate(run, "BR-001", strict=True, persist=False)
        assert_failed(report, code, "named human")
        blind_meta["attested_by"] = "SELFTEST_REVIEWER"
        blind_meta["imported_at"] = "2026-09-04T12:00:00"
        blind_meta_path.write_text(json.dumps(blind_meta), encoding="utf-8")
        report, code = blind_review.validate(run, "BR-001", strict=True, persist=False)
        assert_failed(report, code, "timezone")
        blind_meta_path.write_text(blind_meta_text, encoding="utf-8")

        # A review is bound to the packet's exact source corpus.
        brief_path = run / "problem/PROBLEM_BRIEF.md"
        brief_text = brief_path.read_text(encoding="utf-8")
        brief_path.write_text(brief_text + "\nChanged after packet export.\n", encoding="utf-8")
        report, code = blind_review.validate(run, "BR-001", strict=True)
        assert_failed(report, code, "changed after packet export")
        brief_path.write_text(brief_text, encoding="utf-8")
        report, code = blind_review.validate(run, "BR-001", strict=True)
        assert code == 0, report

        # Dimension-specific consistency resolves evidence references and
        # enforces the canonical required-dimension registry.
        replay = run / "evidence/forward_replay.json"
        replay.write_text(json.dumps({"duration": "pass", "vertical_error_m": 0.00054}) + "\n", encoding="utf-8")
        consistency_matrix.cmd_set(
            ns(
                run=run_id,
                check_id="CK-BAD-REF",
                dimension="duration_replay",
                scope="synthetic table",
                expected="all durations reproduce",
                observed="all durations reproduced",
                status="PASS",
                tolerance="",
                critical=True,
                evidence_ref="evidence/does-not-exist.json",
                command="",
                reviewed_by="SELFTEST_REVIEWER",
                waiver_reason="",
                notes="",
            )
        )
        report, code = consistency_matrix.derive(run, strict=True)
        assert_failed(report, code, "evidence_ref")
        assert any("does-not-exist.json" in item for item in report["errors"]), report

        consistency_matrix.cmd_set(
            ns(
                run=run_id,
                check_id="CK-BAD-URL",
                dimension="objective_replay",
                scope="unregistered external reference",
                expected="only reviewed sources are accepted",
                observed="an unregistered URL was supplied",
                status="PASS",
                tolerance="",
                critical=True,
                evidence_ref="https://example.invalid/unregistered-source",
                command="",
                reviewed_by="SELFTEST_REVIEWER",
                waiver_reason="",
                notes="",
            )
        )
        report, code = consistency_matrix.derive(run, strict=True)
        assert_failed(report, code, "external URL is not a verified SOURCE_LEDGER reference")

        # Required dimensions cannot be waived, and even explicitly waivable
        # optional dimensions reject placeholder reviewers at write time.
        expect_exit(
            2,
            consistency_matrix.cmd_set,
            ns(
                run=run_id,
                check_id="CK-NA-REQUIRED",
                dimension="duration_replay",
                scope="required replay",
                expected="document a reviewed waiver",
                observed="not applicable to this fixture",
                status="NOT_APPLICABLE",
                tolerance="",
                critical=False,
                evidence_ref="",
                command="",
                reviewed_by="SELFTEST_REVIEWER",
                waiver_reason="This required replay cannot be waived in strict mode.",
                notes="",
            ),
        )
        expect_exit(
            2,
            consistency_matrix.cmd_set,
            ns(
                run=run_id,
                check_id="CK-NA-PLACEHOLDER",
                dimension="horizontal_kinematics",
                scope="optional horizontal state",
                expected="document a reviewed waiver",
                observed="not applicable to this fixture",
                status="NOT_APPLICABLE",
                tolerance="",
                critical=False,
                evidence_ref="",
                command="",
                reviewed_by="TEAM",
                waiver_reason="The synthetic fixture defines no horizontal state variable.",
                notes="",
            ),
        )

        # The reader independently rejects a hand-edited waiver that omits the
        # reviewer, timezone-qualified review time, and concrete waiver reason.
        matrix_path = run / "consistency/CONSISTENCY_MATRIX.csv"
        write_csv_rows(
            matrix_path,
            consistency_matrix.FIELDS,
            [
                {
                    "check_id": "CK-NA",
                    "dimension": "horizontal_kinematics",
                    "scope": "synthetic fixture without horizontal state",
                    "expected": "document a reviewed waiver",
                    "observed": "not applicable to this fixture",
                    "status": "NOT_APPLICABLE",
                    "tolerance": "",
                    "critical": "false",
                    "evidence_ref": "",
                    "command": "",
                    "reviewed_by": "",
                    "reviewed_at": "2026-09-02T12:00:00",
                    "waiver_reason": "",
                    "notes": "A note is not a structured waiver.",
                }
            ],
        )
        report, code = consistency_matrix.derive(run, strict=True)
        assert_failed(report, code, "NOT_APPLICABLE requires a named reviewer")
        assert any("timezone-aware" in item for item in report["errors"]), report
        assert any("waiver_reason" in item for item in report["errors"]), report

        # Replace the deliberately bad row, close the waiver, then supply one
        # valid reviewed check for every required configured dimension.
        write_csv_rows(matrix_path, consistency_matrix.FIELDS)
        consistency_matrix.cmd_set(
            ns(
                run=run_id,
                check_id="CK-NA",
                dimension="horizontal_kinematics",
                scope="synthetic fixture without horizontal state",
                expected="document a reviewed waiver",
                observed="not applicable to this fixture",
                status="NOT_APPLICABLE",
                tolerance="",
                critical=False,
                evidence_ref="",
                command="",
                reviewed_by="SELFTEST_REVIEWER",
                waiver_reason="The synthetic fixture has no horizontal state variable by construction.",
                notes="Reviewed optional-dimension waiver.",
            )
        )
        dimension_config = json.loads(
            (ROOT / "config/consistency_dimensions.json").read_text(encoding="utf-8-sig")
        )
        required_dimensions = [
            str(item["name"])
            for item in dimension_config["dimensions"]
            if item.get("required") is True
        ]
        for index, dimension in enumerate(required_dimensions, start=1):
            consistency_matrix.cmd_set(
                ns(
                    run=run_id,
                    check_id=f"CK-REQ-{index:02d}",
                    dimension=dimension,
                    scope=f"required regression scope {index}",
                    expected="declared quantity replays exactly",
                    observed="declared quantity replayed exactly",
                    status="PASS",
                    tolerance="",
                    critical=True,
                    evidence_ref="evidence/forward_replay.json",
                    command="synthetic-self-test",
                    reviewed_by="SELFTEST_REVIEWER",
                    waiver_reason="",
                    notes="",
                )
            )
        consistency_matrix.cmd_set(
            ns(
                run=run_id,
                check_id="CK-TOL",
                dimension="vertical_roundtrip",
                scope="synthetic table",
                expected="coordinates reproduce under exported precision",
                observed="maximum discrepancy 5.4e-4 m",
                status="PASS_WITH_TOLERANCE",
                tolerance="1e-3 m",
                critical=True,
                evidence_ref="evidence/forward_replay.json",
                command="",
                reviewed_by="SELFTEST_REVIEWER",
                waiver_reason="",
                notes="",
            )
        )
        report, code = consistency_matrix.derive(run, strict=True)
        assert code == 0, report
        assert report["overall"] == "PASS_WITH_TOLERANCE"
        assert report["missing_required_dimensions"] == []

        # Complete the retained base-gate science fixtures before freezing.
        # These are deliberately small, but they satisfy the live schemas and
        # bind G3 to a fresh runner-style receipt and metric artifact.
        (run / "problem/QUESTION_MAP.md").write_text(
            "# Question map\n\nQ1 compares point-target and full-target formulations.\n",
            encoding="utf-8",
        )
        write_json_file(
            run / "problem/PROBLEM_GRAPH.json",
            {
                "version": "1",
                "nodes": [{"id": "Q1", "label": "target-geometry comparison"}],
                "edges": [],
            },
        )
        write_csv_rows(
            run / "problem/ASSUMPTION_REGISTER.csv",
            ["assumption_id", "statement", "status", "reviewed_by"],
            [{"assumption_id": "A1", "statement": "Use the approved full-target branch", "status": "approved", "reviewed_by": "SELFTEST_REVIEWER"}],
        )
        (run / "problem/VALIDATION_MAP.md").write_text(
            "# Validation map\n\nQ1 is checked by paired structural probes and EXP-SS001.\n",
            encoding="utf-8",
        )
        write_json_file(
            run / "planning/APPROACH_SPACE.json",
            {"candidate_families": ["geometric-spatial", "optimization", "baseline"]},
        )
        write_json_file(
            run / "planning/TEAM_PLAN.json",
            {"plan_id": "PLAN-SELFTEST", "goal": "Exercise live gate hashing", "workstreams": []},
        )
        idea_registry_rows = []
        for index, family in enumerate(("geometric-spatial", "mixed-integer", "deterministic-baseline"), start=1):
            idea_id = f"IDEA_{index:03d}"
            idea_dir = run / "research" / "ideas" / idea_id
            idea_dir.mkdir(parents=True, exist_ok=True)
            write_json_file(
                idea_dir / "IDEA_CARD.json",
                {
                    "id": idea_id,
                    "title": f"Synthetic branch candidate {index}",
                    "target_nodes": ["Q1"],
                    "summary": "A bounded offline candidate used to verify deterministic gate approval hashing.",
                    "method_families": [family],
                    "validation_plan": ["Replay the paired structural fixture"],
                    "main_risks": ["Synthetic scope only"],
                    "status": "explored",
                },
            )
            idea_registry_rows.append(
                {
                    "id": idea_id,
                    "title": f"Synthetic branch candidate {index}",
                    "path": f"research/ideas/{idea_id}/IDEA_CARD.json",
                    "source": "self-test",
                    "status": "explored",
                    "created_at": "2026-09-02T12:00:00+08:00",
                }
            )
        write_json_file(
            run / "ideas/IDEA_REGISTRY.json",
            {"next_id": 4, "ideas": idea_registry_rows},
        )
        diversity_args = diversity_tool.build_parser().parse_args(
            ["analyze", "--run", run_id]
        )
        assert diversity_args.func(diversity_args) == 0
        write_json_file(
            run / "selection/FOCUS_SET.json",
            {
                "selected_ideas": ["IDEA_001"],
                "selected_modules": ["MOD_001"],
                "selected_by": "SELFTEST_REVIEWER",
                "selected_at": "2026-09-02T12:00:00+08:00",
                "evidence": ["research/diversity/DIVERSITY_REPORT.json"],
                "rationale": "Retain one transparent branch for the offline regression.",
                "rejected_or_deferred": ["IDEA_002", "IDEA_003"],
            },
        )
        (run / "reviews/FEASIBILITY_REVIEW.md").write_text(
            "# Feasibility review\n\nThe synthetic module is executable, bounded, and reviewable.\n",
            encoding="utf-8",
        )
        initial_ai_events = common.read_jsonl(run / "ai/AI_USAGE_LOG.jsonl")
        assert harness._disclosure_ai_event_count(initial_ai_events) == 0, initial_ai_events
        forged_no_model_event = {
            **harness._TRUSTED_AI_INIT_SENTINEL,
            "timestamp": common.now_iso(),
        }
        forged_no_model_event.update(
            {
                "event_id": "AI-FORGED-NO-MODEL",
                "tool": "external AI tool",
                "model": "N/A",
                "stage": "research",
                "purpose": "Attempt to evade disclosure",
                "prompt_or_task_summary": "This is a real AI event despite the model label",
                "notes": "Regression fixture only.",
            }
        )
        assert harness._disclosure_ai_event_count([forged_no_model_event]) == 1
        adopted_without_review = dict(forged_no_model_event)
        adopted_without_review["adopted"] = True
        assert any(
            "adopted event must be human_verified" in item
            for item in harness._ai_usage_event_errors([adopted_without_review])
        )
        placeholder_review = dict(forged_no_model_event)
        placeholder_review.update({"human_verified": True, "verified_by": "TEAM"})
        assert any(
            "lacks a named verifier" in item
            for item in harness._ai_usage_event_errors([placeholder_review])
        )
        expect_exit(
            2,
            common.append_ai_usage_event,
            run,
            tool="offline self-test",
            model="none",
            stage="regression",
            purpose="Reject unreviewed adoption",
            prompt_or_task_summary="No external call",
            adopted=True,
            human_verified=False,
        )
        expect_exit(
            2,
            harness.cmd_ai_log,
            ns(run=run_id, adopted=True, verified=False, by="SELFTEST_REVIEWER"),
        )
        expect_exit(
            2,
            external_review.cmd_import,
            ns(run=run_id, adopted=True, verified=False, by="SELFTEST_REVIEWER"),
        )
        expect_exit(
            2,
            external_review.cmd_import,
            ns(run=run_id, adopted=False, verified=True, by="TEAM"),
        )

        common.append_jsonl(
            run / "decisions/DECISION_LOG.jsonl",
            {"timestamp": common.now_iso(), "type": "fixture-decision", "by": "TEAM", "note": "retain IDEA_001"},
        )

        receipt_relative = receipt.relative_to(run).as_posix()
        log_rows = common.read_csv(run / "experiments/EXPERIMENT_LOG.csv")
        metric_rows = common.read_csv(run / "experiments/METRICS.csv")
        assert len(log_rows) == 1 and log_rows[0]["output_path"] == receipt_relative, log_rows
        assert len(metric_rows) == 1 and metric_rows[0]["evidence_path"] == receipt_relative, metric_rows
        write_csv_rows(
            run / "experiments/BASELINE_REGISTRY.csv",
            ["baseline_id", "name", "purpose", "implementation_path", "metric", "expected_role", "status", "notes"],
            [
                {
                    "baseline_id": "BASE-001",
                    "name": "point target",
                    "purpose": "structural reference",
                    "implementation_path": "code/model.py",
                    "metric": "objective",
                    "expected_role": "lower reference",
                    "status": "verified",
                    "notes": "offline fixture",
                }
            ],
        )
        # G3 protects ledger identity and claim semantics in addition to
        # validating the runner receipt.  Exercise each failure independently
        # and restore the exact canonical ledger before approvals.
        ledger_path = run / "evidence/CLAIM_EVIDENCE_LEDGER.csv"
        ledger_text = ledger_path.read_text(encoding="utf-8")
        ledger_rows = common.read_csv(ledger_path)
        assert len(ledger_rows) == 1, ledger_rows
        canonical_evidence = dict(ledger_rows[0])

        write_csv_rows(
            ledger_path,
            harness.CLAIM_FIELDS,
            [canonical_evidence, dict(canonical_evidence)],
        )
        duplicate_g3 = harness.validate_gate(run, "G3", strict=True)
        duplicate_messages = "\n".join(duplicate_g3["errors"])
        assert not duplicate_g3["ok"], duplicate_g3
        assert "evidence_id" in duplicate_messages and (
            "重复" in duplicate_messages or "duplicate" in duplicate_messages.lower()
        ), duplicate_messages

        conflicting_evidence = dict(canonical_evidence)
        conflicting_evidence["evidence_id"] = "EVID-002"
        conflicting_evidence["claim_text"] = "Conflicting metadata for the same claim ID."
        write_csv_rows(
            ledger_path,
            harness.CLAIM_FIELDS,
            [canonical_evidence, conflicting_evidence],
        )
        metadata_g3 = harness.validate_gate(run, "G3", strict=True)
        metadata_messages = "\n".join(metadata_g3["errors"])
        assert not metadata_g3["ok"], metadata_g3
        assert "claim_text/claim_type/is_core/paper_location" in metadata_messages

        management_evidence = dict(canonical_evidence)
        management_evidence["evidence_path"] = "experiments/METRICS.csv"
        write_csv_rows(ledger_path, harness.CLAIM_FIELDS, [management_evidence])
        management_g3 = harness.validate_gate(run, "G3", strict=True)
        management_messages = "\n".join(management_g3["errors"])
        assert not management_g3["ok"], management_g3
        assert "管理" in management_messages or "management" in management_messages.lower()

        arbitrary_result = run / "results/arbitrary-claim.txt"
        arbitrary_result.write_text(
            "A plausible-looking result file without a runner receipt is not evidence.\n",
            encoding="utf-8",
        )
        arbitrary_evidence = dict(canonical_evidence)
        arbitrary_evidence["evidence_path"] = arbitrary_result.relative_to(run).as_posix()
        write_csv_rows(ledger_path, harness.CLAIM_FIELDS, [arbitrary_evidence])
        arbitrary_g3 = harness.validate_gate(run, "G3", strict=True)
        arbitrary_messages = "\n".join(arbitrary_g3["errors"])
        assert not arbitrary_g3["ok"], arbitrary_g3
        assert "passed experiment run.json" in arbitrary_messages or "metrics artifact" in arbitrary_messages

        ledger_path.write_text(ledger_text, encoding="utf-8")
        valid_g3 = harness.validate_gate(run, "G3", strict=True)
        assert valid_g3["ok"], valid_g3

        # The valid G3 fixture above came from execute_one, not a hand-written
        # lookalike receipt.  Its location/attempt identity and every upstream
        # request, process-output and manifest hash are provenance boundaries.
        canonical_receipt_bytes = receipt.read_bytes()

        def assert_receipt_rejected(
            mutated: dict[str, object], categories: tuple[str, ...]
        ) -> None:
            write_json_file(receipt, mutated)
            rejected = harness.validate_gate(run, "G3", strict=True)
            assert not rejected["ok"], rejected
            messages = "\n".join(str(item) for item in rejected["errors"]).lower()
            assert any(category.lower() in messages for category in categories), messages
            receipt.write_bytes(canonical_receipt_bytes)
            restored = harness.validate_gate(run, "G3", strict=True)
            assert restored["ok"], restored

        mutated_receipt = json.loads(canonical_receipt_bytes.decode("utf-8"))
        mutated_receipt["record_dir"] = (
            "experiments/runs/EXP-SS001/seed_7/_attempts/attempt_wrong"
        )
        assert_receipt_rejected(mutated_receipt, ("record_dir", "path", "路径"))

        mutated_receipt = json.loads(canonical_receipt_bytes.decode("utf-8"))
        mutated_receipt["attempt_id"] = "attempt_wrong"
        assert_receipt_rejected(mutated_receipt, ("attempt", "路径"))

        mutated_receipt = json.loads(canonical_receipt_bytes.decode("utf-8"))
        mutated_receipt["stdout_sha256"] = "0" * 64
        assert_receipt_rejected(mutated_receipt, ("stdout", "sha256", "hash"))

        mutated_receipt = json.loads(canonical_receipt_bytes.decode("utf-8"))
        mutated_receipt["manifest_sha256"] = "0" * 64
        assert_receipt_rejected(mutated_receipt, ("manifest", "清单", "sha256"))

        request_bytes = request_path.read_bytes()
        mutated_request = json.loads(request_bytes.decode("utf-8"))
        mutated_request["description"] = "tampered request field"
        write_json_file(request_path, mutated_request)
        rejected_request = harness.validate_gate(run, "G3", strict=True)
        assert not rejected_request["ok"], rejected_request
        request_messages = "\n".join(
            str(item) for item in rejected_request["errors"]
        ).lower()
        assert "request" in request_messages or "请求" in request_messages
        request_path.write_bytes(request_bytes)
        assert harness.validate_gate(run, "G3", strict=True)["ok"]

        def assert_provenance_file_rejected(
            path: Path, categories: tuple[str, ...]
        ) -> None:
            canonical_bytes = path.read_bytes()
            path.write_bytes(canonical_bytes + b"\n")
            rejected = harness.validate_gate(run, "G3", strict=True)
            assert not rejected["ok"], rejected
            messages = "\n".join(str(item) for item in rejected["errors"]).lower()
            assert any(category.lower() in messages for category in categories), messages
            # Restore exact bytes: common.atomic_write_text deliberately writes
            # LF, while Path.write_text would translate newlines on Windows and
            # turn this provenance test itself into a persistent mutation.
            path.write_bytes(canonical_bytes)
            restored = harness.validate_gate(run, "G3", strict=True)
            assert restored["ok"], restored

        assert_provenance_file_rejected(stdout_path, ("stdout", "标准输出", "sha256"))
        assert_provenance_file_rejected(
            runner_manifest_path, ("manifest", "实验清单", "sha256")
        )
        write_json_file(
            run / "synthesis/SYNTHESIS_MAP.json",
            {"selected_modules": ["MOD_001"], "resolved_forks": ["IF-001"]},
        )
        write_json_file(run / "disputes/DISPUTE_REGISTER.json", {"disputes": []})
        (run / "reviews/SYNTHESIS_REVIEW.md").write_text(
            "# Synthesis review\n\nThe retained model preserves the approved full-target semantics.\n",
            encoding="utf-8",
        )
        for name, title in (
            ("RED_TEAM_REPORT.md", "Red-team report"),
            ("ROBUSTNESS_REPORT.md", "Robustness report"),
            ("CODE_AUDIT.md", "Code audit"),
            ("EVIDENCE_AUDIT.md", "Evidence audit"),
        ):
            (run / "reviews" / name).write_text(
                f"# {title}\n\nSynthetic fixture reviewed with no unresolved blockers.\n",
                encoding="utf-8",
            )
        write_json_file(
            run / "reviews/blind/BLIND_REVIEW_INDEX.json",
            {"reviews": [{"review_id": "BR-001", "status": "passed"}]},
        )
        # No external source was used.  Keep the canonical ledger header-only;
        # bind an explicit human no-source attestation to those exact bytes.
        # Both files exist before mathematical-stage approval because that
        # stage hashes the complete sources/ corpus.
        write_csv_rows(run / "sources/SOURCE_LEDGER.csv", harness.SOURCE_FIELDS)
        assert harness.cmd_source_attest_none(
            ns(
                run=run_id,
                by="SELFTEST",
                reason="No external sources are used in this offline synthetic fixture.",
            )
        ) == 0

        # G5 must bind the whole prospective freeze scope, including science
        # files not mentioned by any individual validator.
        unreferenced_science = run / "figures/prospective-freeze-scope.txt"
        unreferenced_science.write_text(
            "Unreferenced but canonical scientific freeze-scope artifact.\n",
            encoding="utf-8",
        )

        # A base gate must not treat a merely successful overlay validator as
        # a human approval.  First bind the real read-only project inputs so
        # that pending interpretation approval is the isolated G0 blocker.
        assert harness.cmd_regenerate_manifest(ns(run=run_id)) == 0
        pending_interpretation, pending_interpretation_code = (
            v51_orchestrator.validate_stage(run, "interpretation")
        )
        assert pending_interpretation_code == 0, pending_interpretation
        canonical_g0 = harness.validate_gate(run, "G0", strict=True)
        assert not canonical_g0["ok"], canonical_g0
        assert any(
            "interpretation" in item.lower()
            and ("批准" in item or "approved" in item.lower())
            for item in canonical_g0["errors"]
        ), canonical_g0

        # Approve the first three v5.1 stages in registry order.  The validators
        # are expected to be stable under immediate revalidation.
        for stage in ("interpretation", "structural_validation", "mathematical_construction"):
            assert v51_orchestrator.cmd_approve(
                ns(
                    run=run_id,
                    stage=stage,
                    review_id=None,
                    by="SELFTEST_REVIEWER",
                    note="pre-freeze scientific approval",
                )
            ) == 0
            report, code = v51_orchestrator.validate_stage(run, stage)
            assert code == 0, (stage, report)
            state = json.loads((run / "V51_STATUS.json").read_text(encoding="utf-8-sig"))
            approved = state["stages"][stage]
            assert approved["status"] == "approved", approved
            assert approved["approved_input_sha256"] == report["input_sha256"], approved

        # Exercise the immutable input snapshot without mutating the project's
        # real read-only inputs/: temporarily point the harness at an isolated
        # source root, change one file, and restore both bytes and mtime.
        synthetic_inputs = run / "_synthetic_input_root"
        synthetic_inputs.mkdir(parents=True, exist_ok=True)
        synthetic_input = synthetic_inputs / "problem.txt"
        synthetic_input_text = "immutable synthetic statement\n"
        synthetic_input.write_text(synthetic_input_text, encoding="utf-8")
        synthetic_input_stat = synthetic_input.stat()
        with patch.object(harness, "INPUTS_ROOT", synthetic_inputs):
            assert harness.cmd_regenerate_manifest(ns(run=run_id)) == 0
            fresh_g0_input = harness.validate_gate(run, "G0", strict=True)
            assert fresh_g0_input["ok"], fresh_g0_input
            fresh_g0_input_hash = fresh_g0_input["input_sha256"]

            synthetic_input.write_text(
                synthetic_input_text + "changed after manifest capture\n",
                encoding="utf-8",
            )
            stale_g0_input = harness.validate_gate(run, "G0", strict=True)
            stale_g0_messages = "\n".join(stale_g0_input["errors"])
            assert not stale_g0_input["ok"], stale_g0_input
            assert "INPUT_MANIFEST" in stale_g0_messages, stale_g0_messages
            assert stale_g0_input["input_sha256"] != fresh_g0_input_hash

            synthetic_input.write_text(synthetic_input_text, encoding="utf-8")
            os.utime(
                synthetic_input,
                ns=(synthetic_input_stat.st_atime_ns, synthetic_input_stat.st_mtime_ns),
            )
            restored_g0_input = harness.validate_gate(run, "G0", strict=True)
            assert restored_g0_input["ok"], restored_g0_input
            assert restored_g0_input["input_sha256"] == fresh_g0_input_hash

        # Return to the canonical project input root before base approvals.
        # This reads that root and never modifies it.
        assert harness.cmd_regenerate_manifest(ns(run=run_id)) == 0
        approved_g0 = harness.validate_gate(run, "G0", strict=True)
        assert approved_g0["ok"], approved_g0

        # Re-export once, before the freeze, after every blind-review science
        # input is final.  Later paper/final-manuscript work is outside the
        # blind science corpus and must not require another review packet.
        # Earlier selection/setup steps changed the canonical math corpus.
        assert claim_guard.cmd_scan(ns(run=run_id, scope="math", path=[])) == 0
        report, code = claim_guard.lint(run, strict=True, scope="math")
        assert code == 0, report
        # The later full scan must preserve this final pre-freeze receipt.
        math_receipt_text = math_receipt_path.read_text(encoding="utf-8")
        math_registry_text = math_registry_path.read_text(encoding="utf-8")
        blind_review.cmd_export(ns(run=run_id, review_id="BR-001", overwrite=True, max_text_mb=5))
        write_blind_response(passing_response_body)
        blind_review.cmd_import(
            ns(
                run=run_id,
                review_id="BR-001",
                response=str(response),
                reviewer_kind="external-pro",
                session_id="FRESH-SESSION-PREFREEZE",
                fresh_context=True,
                attested_by="SELFTEST_REVIEWER",
                note="final pre-freeze synthetic science review",
            )
        )
        report, code = blind_review.validate(run, "BR-001", strict=True)
        assert code == 0, report

        # --allow-before-g5 is narrowly scoped: it cannot bypass missing or
        # stale G0-G4 approvals.
        expect_exit(
            2,
            harness.cmd_freeze,
            ns(
                run=run_id,
                replace=False,
                allow_before_g5=True,
                by="SELFTEST",
                note="must not bypass missing G0-G4",
            ),
        )
        assert not (run / "FROZEN_MANIFEST.json").exists()

        gate_args = {
            "run": run_id,
            "strict": True,
            "force": False,
            "by": "SELFTEST_REVIEWER",
            "note": "isolated full-lifecycle approval",
        }

        def review_then_approve(gate_id: str, **overrides: object) -> int:
            assert harness.cmd_review(ns(run=run_id, gate=gate_id)) == 0
            values = {**gate_args, **overrides, "gate": gate_id}
            return harness.cmd_approve(ns(**values))

        # Conversely, --force is useful when only the prior Gate ordering is
        # incomplete and this Gate's own strict inputs are valid.  Approving G0
        # afterward must invalidate that out-of-order G1 approval normally.
        force_ready_g1 = harness.validate_gate(run, "G1", strict=True)
        assert force_ready_g1["ok"], force_ready_g1
        pre_review_status = (run / "STATUS.json").read_bytes()
        pre_review_log = (run / "decisions/DECISION_LOG.jsonl").read_bytes()
        expect_exit(
            2,
            harness.cmd_approve,
            ns(
                run=run_id,
                gate="G1",
                strict=True,
                force=True,
                by="SELFTEST_REVIEWER",
                note="current G1 is valid while G0 ordering is intentionally incomplete",
            ),
        )
        assert (run / "STATUS.json").read_bytes() == pre_review_status
        assert (run / "decisions/DECISION_LOG.jsonl").read_bytes() == pre_review_log
        assert not (run / "reviews/gates/G1_REVIEW.md").exists()
        assert review_then_approve(
            "G1",
            force=True,
            note="current G1 is valid while G0 ordering is intentionally incomplete",
        ) == 0
        forced_ready_status = common.load_status(run)
        assert forced_ready_status["gates"]["G1"]["status"] == "approved"
        forced_ready_events = [
            item
            for item in common.read_jsonl(run / "decisions/DECISION_LOG.jsonl")
            if item.get("type") == "gate-approval" and item.get("gate") == "G1"
        ]
        assert forced_ready_events and forced_ready_events[-1]["forced"] is True

        assert review_then_approve("G0") == 0
        assert common.load_status(run)["gates"]["G1"]["status"] == "pending"
        for gate_id in ("G1", "G2"):
            assert review_then_approve(gate_id) == 0
        status = common.load_status(run)
        approved_g2_hash = status["gates"]["G2"]["validation"]["input_sha256"]
        fresh_g2 = harness.validate_gate(run, "G2", strict=True)
        assert fresh_g2["ok"], fresh_g2
        assert fresh_g2["input_sha256"] == approved_g2_hash, fresh_g2
        assert review_then_approve("G3") == 0

        # Operational audit rows such as gate approvals do not self-invalidate
        # G2, while each model-selection event type remains part of its hash.
        post_g3_g2 = harness.validate_gate(run, "G2", strict=True)
        assert post_g3_g2["input_sha256"] == approved_g2_hash, post_g3_g2
        decision_path = run / "decisions/DECISION_LOG.jsonl"
        decision_bytes = decision_path.read_bytes()
        for event_type in ("focus-set", "idea-created", "module-created"):
            common.append_jsonl(
                decision_path,
                {
                    "timestamp": common.now_iso(),
                    "type": event_type,
                    "by": "SELFTEST_REVIEWER",
                    "note": f"semantic hash regression for {event_type}",
                },
            )
            semantic_change = harness.validate_gate(run, "G2", strict=True)
            assert semantic_change["ok"], semantic_change
            assert semantic_change["input_sha256"] != approved_g2_hash, semantic_change
            decision_path.write_bytes(decision_bytes)
        restored_g2 = harness.validate_gate(run, "G2", strict=True)
        assert restored_g2["input_sha256"] == approved_g2_hash, restored_g2

        assert review_then_approve("G4") == 0

        # G5 now consumes actual v5.1 approvals, not validator PASS booleans.
        # Approve its two pre-paper controls in registry order; consistency is
        # deliberately ordered before the post-paper strong-claim stage so the
        # latter cannot invalidate this freeze dependency.
        for stage, review_id in (
            ("independent_review", "BR-001"),
            ("consistency", None),
        ):
            assert v51_orchestrator.cmd_approve(
                ns(
                    run=run_id,
                    stage=stage,
                    review_id=review_id,
                    by="SELFTEST_REVIEWER",
                    note="pre-freeze G5 dependency approval",
                )
            ) == 0

        # With G0-G4 current and G5 never approved, the legacy exception is
        # explicit, audited and verifiable.  Unfreeze it again so the retained
        # paper lifecycle below can exercise an ordinary G5-approved freeze.
        expect_exit(
            2,
            harness.cmd_freeze,
            ns(
                run=run_id,
                replace=False,
                allow_before_g5=False,
                by="SELFTEST",
                note="pending G5 requires an explicit exception",
            ),
        )
        assert harness.cmd_freeze(
            ns(
                run=run_id,
                replace=False,
                allow_before_g5=True,
                by="SELFTEST",
                note="isolated pre-G5 exception regression",
            )
        ) == 0
        exception_freeze = json.loads(
            (run / "FROZEN_MANIFEST.json").read_text(encoding="utf-8-sig")
        )
        assert exception_freeze["before_g5_exception"] is True
        assert harness.verify_freeze(run, quiet=True)["ok"]
        assert harness.cmd_unfreeze(
            ns(
                run=run_id,
                by="SELFTEST",
                reason="Continue the same disposable fixture through approved G5.",
            )
        ) == 0
        assert not (run / "FROZEN_MANIFEST.json").exists()

        assert review_then_approve("G5") == 0

        # An already approved but stale G5 is a real error, not a legacy-flow
        # exception.  A canonical science change may also invalidate the blind
        # review that covered it; either way the approval digest must drift and
        # freeze must fail closed.  Restore it before the canonical freeze.
        g5_input = unreferenced_science
        g5_input_text = g5_input.read_text(encoding="utf-8")
        approved_g5_validation = common.load_status(run)["gates"]["G5"]["validation"]
        approved_g5_hash = approved_g5_validation["input_sha256"]
        g5_control_paths = {
            item["path"]
            for item in approved_g5_validation["input_manifest"]
            if item.get("kind") == "control"
        }
        expected_g5_claim_controls = {
            "tools/claim_guard.py",
            "tools/v51_common.py",
            "config/claim_provenance.json",
            "schemas/strong_claim.schema.json",
            "schemas/strong_claim_scan.schema.json",
        }
        assert expected_g5_claim_controls.issubset(g5_control_paths), (
            expected_g5_claim_controls - g5_control_paths
        )

        # Simulate a policy-control digest change without touching repository
        # source.  The G5 validator itself still passes against real bytes, but
        # its approval input hash must change because the policy is bound as a
        # control artifact.
        policy_control = (ROOT / "config/claim_provenance.json").resolve()
        real_harness_sha256 = harness.sha256_file

        def shifted_policy_digest(path: Path) -> str:
            digest = real_harness_sha256(path)
            if Path(path).resolve() == policy_control:
                return "0" * 64 if digest != "0" * 64 else "1" * 64
            return digest

        with patch.object(harness, "sha256_file", side_effect=shifted_policy_digest):
            changed_control_g5 = harness.validate_gate(run, "G5", strict=True)
        assert changed_control_g5["ok"], changed_control_g5
        assert changed_control_g5["input_sha256"] != approved_g5_hash

        math_proof_text = math_proof.read_text(encoding="utf-8")
        math_proof.write_text(
            math_proof_text + "\nChanged after the G5 math-scope scan.\n",
            encoding="utf-8",
        )
        stale_math_g5 = harness.validate_gate(run, "G5", strict=True)
        stale_math_messages = "\n".join(stale_math_g5["errors"])
        assert not stale_math_g5["ok"], stale_math_g5
        assert stale_math_g5["input_sha256"] != approved_g5_hash, stale_math_g5
        assert (
            "STRONG_CLAIM_SCAN_MATH" in stale_math_messages
            or "strong-claim" in stale_math_messages.lower()
            or "corpus" in stale_math_messages.lower()
        ), stale_math_messages
        math_proof.write_text(math_proof_text, encoding="utf-8")
        restored_math_g5 = harness.validate_gate(run, "G5", strict=True)
        assert restored_math_g5["ok"], restored_math_g5
        assert restored_math_g5["input_sha256"] == approved_g5_hash

        g5_input.write_text(g5_input_text + "\nChanged after G5 approval.\n", encoding="utf-8")
        stale_g5 = harness.validate_gate(run, "G5", strict=True)
        assert not stale_g5["ok"], stale_g5
        assert stale_g5["input_sha256"] != approved_g5_hash, stale_g5
        expect_exit(
            2,
            harness.cmd_freeze,
            ns(
                run=run_id,
                replace=False,
                allow_before_g5=True,
                by="SELFTEST",
                note="must not bypass stale approved G5",
            ),
        )
        g5_input.write_text(g5_input_text, encoding="utf-8")
        restored_g5 = harness.validate_gate(run, "G5", strict=True)
        assert restored_g5["ok"], restored_g5
        assert restored_g5["input_sha256"] == approved_g5_hash, restored_g5
        assert harness.cmd_freeze(
            ns(
                run=run_id,
                replace=False,
                allow_before_g5=False,
                by="SELFTEST",
                note="canonical approved-G5 regression fixture",
            )
        ) == 0
        freeze_payload = json.loads(
            (run / "FROZEN_MANIFEST.json").read_text(encoding="utf-8-sig")
        )
        assert freeze_payload["before_g5_exception"] is False
        frozen_paths = {str(item["path"]): str(item["sha256"]) for item in freeze_payload["files"]}
        assert frozen_paths["results/SS-001.json"] == common.sha256_file(result_file)
        assert frozen_paths[metrics_relative] == common.sha256_file(metrics_artifact)
        freeze_events = [
            item for item in common.read_jsonl(run / "decisions/DECISION_LOG.jsonl")
            if item.get("type") == "freeze"
        ]
        assert any(item.get("before_g5_exception") is True for item in freeze_events)
        assert freeze_events[-1].get("before_g5_exception") is False
        freeze_check = harness.verify_freeze(run, quiet=True)
        assert freeze_check["ok"], freeze_check

        # The manifest is an integrity boundary, not a bag of trusted paths.
        # Corrupt every structural field class plus recorded size/hash, and
        # restore the canonical bytes after each independent probe.
        freeze_path = run / "FROZEN_MANIFEST.json"
        canonical_freeze_bytes = freeze_path.read_bytes()

        def assert_bad_freeze(mutated: dict[str, object], *needles: str) -> None:
            write_json_file(freeze_path, mutated)
            rejected = harness.verify_freeze(run, quiet=True)
            assert not rejected["ok"], rejected
            messages = "\n".join(str(item) for item in rejected["issues"])
            assert all(needle.lower() in messages.lower() for needle in needles), messages
            freeze_path.write_bytes(canonical_freeze_bytes)
            restored = harness.verify_freeze(run, quiet=True)
            assert restored["ok"], restored

        malformed = json.loads(canonical_freeze_bytes.decode("utf-8"))
        malformed["schema_version"] = 99
        assert_bad_freeze(malformed, "schema_version")
        malformed = json.loads(canonical_freeze_bytes.decode("utf-8"))
        malformed["run_id"] = "another-run"
        assert_bad_freeze(malformed, "run_id")
        malformed = json.loads(canonical_freeze_bytes.decode("utf-8"))
        malformed["scope"] = [*malformed["scope"], "paper"]
        assert_bad_freeze(malformed, "scope")
        malformed = json.loads(canonical_freeze_bytes.decode("utf-8"))
        malformed["file_count"] = int(malformed["file_count"]) + 1
        assert_bad_freeze(malformed, "file_count")
        malformed = json.loads(canonical_freeze_bytes.decode("utf-8"))
        malformed["files"][0].pop("size")
        assert_bad_freeze(malformed, ".size")
        malformed = json.loads(canonical_freeze_bytes.decode("utf-8"))
        malformed["files"].append(dict(malformed["files"][0]))
        malformed["file_count"] = len(malformed["files"])
        assert_bad_freeze(malformed, "重复", "path")
        malformed = json.loads(canonical_freeze_bytes.decode("utf-8"))
        malformed["files"][0]["path"] = "../outside.txt"
        assert_bad_freeze(malformed, "run-relative")
        malformed = json.loads(canonical_freeze_bytes.decode("utf-8"))
        malformed["files"][0]["sha256"] = "not-a-sha256"
        assert_bad_freeze(malformed, "sha256")
        malformed = json.loads(canonical_freeze_bytes.decode("utf-8"))
        malformed["files"][0]["size"] = int(malformed["files"][0]["size"]) + 1
        assert_bad_freeze(malformed, "已修改")
        malformed = json.loads(canonical_freeze_bytes.decode("utf-8"))
        current_digest = str(malformed["files"][0]["sha256"])
        malformed["files"][0]["sha256"] = (
            "0" * 64 if current_digest != "0" * 64 else "1" * 64
        )
        assert_bad_freeze(malformed, "已修改")

        # The freeze also binds the human-approved G5 dependencies.  A
        # machine-valid edit to the consistency approval projection makes the
        # stored gate freshness stale until the exact approval record returns.
        pre_paper_v51_path = run / "V51_STATUS.json"
        pre_paper_v51_bytes = pre_paper_v51_path.read_bytes()
        mutated_v51 = json.loads(pre_paper_v51_bytes.decode("utf-8"))
        mutated_v51["stages"]["consistency"]["note"] = (
            "changed after canonical freeze"
        )
        write_json_file(pre_paper_v51_path, mutated_v51)
        stale_v51_freeze = harness.verify_freeze(run, quiet=True)
        assert not stale_v51_freeze["ok"], stale_v51_freeze
        assert any(
            "gate_freshness" in item or "G5" in item
            for item in stale_v51_freeze["issues"]
        ), stale_v51_freeze
        pre_paper_v51_path.write_bytes(pre_paper_v51_bytes)
        assert harness.verify_freeze(run, quiet=True)["ok"]

        # Initialize the retained paper pipeline before compiling the v5.1
        # facts-first abstract.
        paper.cmd_init(ns(run=run_id, replace=False, allow_unfrozen=False))
        assert paper.cmd_map_claim(
            ns(
                run=run_id,
                claim="CLAIM-001",
                section="validation_and_results",
                note="Synthetic core result is reported with its frozen evidence.",
            )
        ) == 0
        # Abstract facts may only cite claims already trace-tagged in their
        # planned body section.  Seed that real section before the first strict
        # abstract build; the complete manuscript fixture replaces it later.
        _, early_claim_section = paper.resolve_planned_section(
            run, "validation_and_results", require_in_manuscript=True
        )
        early_claim_section.write_text(
            "# Validation and results\n\n"
            "Synthetic result trace prepared before abstract compilation.\n\n"
            "<!-- CLAIM:CLAIM-001 -->\n",
            encoding="utf-8",
        )
        empty_keyword_extension = paper_v5.validate_paper_extension(run, strict=True)
        assert not empty_keyword_extension["passed"], empty_keyword_extension
        assert any(
            "keyword" in item.lower() for item in empty_keyword_extension["errors"]
        ), empty_keyword_extension
        write_json_file(
            run / "paper/ABSTRACT_FACTS.json",
            {"schema_version": 1, "keywords": ["结构敏感性"], "facts": []},
        )
        empty_extension = paper_v5.validate_paper_extension(run, strict=True)
        assert not empty_extension["passed"], empty_extension
        assert any("没有可用于摘要" in item for item in empty_extension["errors"])

        unknown_fact = {
            "schema_version": 1,
            "keywords": ["结构敏感性"],
            "facts": [
                {
                    "fact_id": "AF-UNKNOWN",
                    "question": "问题一",
                    "purpose": "校验证据绑定",
                    "method": "合成回归",
                    "result": "未知证据不得进入摘要",
                    "scope": "当前自测",
                    "evidence_ids": ["EVID-DOES-NOT-EXIST"],
                    "claim_ids": ["CLAIM-UNKNOWN"],
                    "limitations": ["仅用于失败路径"],
                    "frozen": True,
                    "verified_by": "SELFTEST_REVIEWER",
                    "created_at": "2026-09-02T12:00:00+08:00",
                }
            ],
        }
        write_json_file(run / "paper/ABSTRACT_FACTS.json", unknown_fact)
        unknown_extension = paper_v5.validate_paper_extension(run, strict=True)
        assert not unknown_extension["passed"], unknown_extension
        unknown_messages = "\n".join(str(item) for item in unknown_extension["errors"])
        assert "EVID-DOES-NOT-EXIST" in unknown_messages, unknown_messages
        assert "CLAIM-UNKNOWN" in unknown_messages, unknown_messages
        assert "does not resolve" in unknown_messages.lower() or "unknown" in unknown_messages.lower()
        write_json_file(
            run / "paper/ABSTRACT_FACTS.json",
            {"schema_version": 1, "keywords": [], "facts": []},
        )

        sentinel = "SENTINEL_ABSTRACT_RESULT_49_PERCENT"
        paper_v5.cmd_fact_add(
            ns(
                run=run_id,
                fact_id="AF-001",
                question="问题一",
                purpose="比较结构解释",
                method="全目标集合模型和同输入重优化",
                result=sentinel,
                scope="当前合成数据和给定扰动范围",
                evidence=["EVID-001"],
                claim=["CLAIM-001"],
                limitations=["只验证了两个结构分支"],
                keyword=["结构敏感性"],
                frozen=True,
                verified_by="SELFTEST_REVIEWER",
            )
        )
        paper_v5.cmd_build_abstract(ns(run=run_id))
        _, abstract_path = paper.resolve_planned_section(run, "abstract")
        assert abstract_path.name == "010_abstract.md", abstract_path
        assert sentinel in abstract_path.read_text(encoding="utf-8-sig")
        assert not (run / "paper/sections/00_abstract.md").exists()
        assert paper_v5.cmd_equation_add(
            ns(
                run=run_id,
                equation_id="EQ-001",
                module_id="MOD_001",
                latex="max_x f(x,T)",
                meaning="maximize the synthetic full-target objective",
                section="model_and_solution",
                source_ref="EVID-001",
                status="final",
                notes="Mirrors the checked mathematical registry equation.",
            )
        ) == 0

        # A facts edit invalidates the compiled abstract until it is rebuilt.
        facts_path = run / "paper/ABSTRACT_FACTS.json"
        facts_bytes = facts_path.read_bytes()
        changed_facts = json.loads(facts_bytes.decode("utf-8"))
        changed_facts["facts"][0]["result"] = sentinel + "_CHANGED"
        write_json_file(facts_path, changed_facts)
        stale_abstract = paper_v5.validate_paper_extension(run, strict=True)
        assert not stale_abstract["passed"], stale_abstract
        assert any("stale" in item.lower() for item in stale_abstract["errors"])
        facts_path.write_bytes(facts_bytes)
        restored_abstract = paper_v5.validate_paper_extension(run, strict=True)
        assert not any(
            "compiled abstract is stale" in item.lower()
            for item in restored_abstract["errors"]
        ), restored_abstract

        # Complete every included section with non-placeholder content, then
        # assemble and finalize through the real retained paper pipeline.
        plan_path = run / "paper/PAPER_PLAN.json"
        plan_payload = json.loads(plan_path.read_text(encoding="utf-8-sig"))
        ai_section = next(
            item for item in plan_payload["sections"]
            if item.get("id") == "ai_usage_declaration"
        )
        assert ai_section["file"] == "paper/sections/085_ai_usage_declaration.md"
        assert int(ai_section["order"]) < next(
            int(item["order"])
            for item in plan_payload["sections"]
            if item.get("id") == "references"
        )
        plan_payload["working_title"] = "Synthetic full-target regression manuscript"
        plan_payload["narrative_thesis"] = "A full-target interpretation changes the synthetic objective."
        for section in plan_payload["sections"]:
            if not section.get("include_in_manuscript", True):
                continue
            section["status"] = "final"
            section["writer"] = "SELFTEST_EDITOR"
            if section.get("id") == "abstract":
                continue
            section_path = run / str(section["file"])
            trace = (
                "\n\n<!-- CLAIM:CLAIM-001 -->"
                if section.get("id") == "validation_and_results"
                else ""
            )
            if section.get("id") == "ai_usage_declaration":
                section_body = (
                    "# AI工具使用声明\n\n"
                    "本参赛队在竞赛过程中使用了AI工具，主要用于代码调试和离线回归验证，"
                    "详细使用情况见支撑材料。\n"
                )
            else:
                section_body = (
                    f"# {section['title']}\n\n"
                    f"Synthetic reviewed content for section {section['id']}."
                    f"{trace}\n"
                )
            section_path.write_text(section_body, encoding="utf-8")
        write_json_file(plan_path, plan_payload)
        for section in plan_payload["sections"]:
            if not section.get("include_in_manuscript", True):
                continue
            paper_v5.cmd_section_quality_set(
                ns(
                    run=run_id,
                    section=str(section["id"]),
                    status="final",
                    editor="SELFTEST_EDITOR",
                    purpose=None,
                    claim=None,
                    equation=(
                        ["EQ-001"]
                        if section.get("id") == "model_and_solution"
                        else []
                    ),
                    figure=None,
                    note="Reviewed synthetic section fixture.",
                )
            )
        (run / "paper/PAPER_BRIEF.md").write_text(
            "# Paper brief\n\nEvidence-bound synthetic regression manuscript.\n", encoding="utf-8"
        )
        (run / "paper/PAPER_OUTLINE.md").write_text(
            "# Paper outline\n\nAll planned required sections are included.\n", encoding="utf-8"
        )
        (run / "paper/CONSISTENCY_AUDIT.md").write_text(
            "# Paper consistency audit\n\nReviewed against the frozen synthetic evidence.\n", encoding="utf-8"
        )
        write_csv_rows(
            run / "paper/SYMBOL_TABLE.csv",
            paper.SYMBOL_FIELDS,
            [
                {
                    "symbol": "x",
                    "meaning": "synthetic decision variable",
                    "unit": "dimensionless",
                    "scope": "full-target model",
                    "source_path": "results/SS-001.json",
                    "verified": "true",
                    "notes": "Checked against the frozen structural comparison.",
                }
            ],
        )
        # An unplanned section is never assembled, but the strong-claim scan
        # still sees the complete canonical paper/sections corpus.  Create it
        # before the final scan/audit and confirm later that assembly preserves
        # it as a warning-only workspace artifact.
        orphan = run / "paper/sections/999_orphan.md"
        orphan.write_text(
            "# Orphan\n\nIntentionally unplanned neutral appendix fragment.\n",
            encoding="utf-8",
        )
        assert paper_v5.cmd_lint(ns(run=run_id, strict=True)) == 0
        assert paper.cmd_assemble(ns(run=run_id)) == 0
        draft_path = run / "paper/PAPER_DRAFT.md"
        assert sentinel in draft_path.read_text(encoding="utf-8-sig")
        assert orphan.is_file()

        # The paper audit binds the reviewed strong-claim registry, so perform
        # the final canonical scan after every section is complete and before
        # recording the audit approval.
        assert claim_guard.cmd_scan(ns(run=run_id, scope="all", path=[])) == 0
        report, code = claim_guard.lint(run, strict=True)
        assert code == 0, report
        with (run / "claims/STRONG_CLAIMS.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            inherited_math_claim = next(
                row for row in csv.DictReader(handle) if row["claim_id"] == math_claim_id
            )
        assert inherited_math_claim["status"] == "supported", inherited_math_claim
        assert inherited_math_claim["evidence_type"] == "analytic_proof"
        assert inherited_math_claim["evidence_ref"] == (
            "research/modules/MOD_001/ANALYTIC_PROOF.md"
        )
        assert math_receipt_path.read_text(encoding="utf-8") == math_receipt_text
        assert math_registry_path.read_text(encoding="utf-8") == math_registry_text
        assert harness.verify_freeze(run, quiet=True)["ok"]
        assert paper.cmd_audit_set(
            ns(
                run=run_id,
                status="passed",
                by="SELFTEST_REVIEWER",
                blocker=[],
                warning=[],
                note="Synthetic paper audit passed with no blockers.",
            )
        ) == 0
        assert paper.cmd_finalize(ns(run=run_id)) == 0
        final_path = run / "paper/FINAL_MANUSCRIPT.md"
        assert sentinel in final_path.read_text(encoding="utf-8-sig")

        # Paper approval binds prose, evidence ledger, and symbol metadata.
        # Each machine-valid byte drift must invalidate the prior audit, while
        # restoring the exact input recovers the finalized manuscript state.
        for audit_input in (
            run / "paper/PAPER_BRIEF.md",
            run / "evidence/CLAIM_EVIDENCE_LEDGER.csv",
            run / "paper/SYMBOL_TABLE.csv",
        ):
            audit_input_bytes = audit_input.read_bytes()
            audit_input.write_bytes(audit_input_bytes + b"\n")
            audit_drift = paper.validate_paper_run(
                run, strict=True, require_final=True, require_audit=True
            )
            assert not audit_drift["ok"], (audit_input, audit_drift)
            audit_messages = "\n".join(audit_drift["errors"])
            assert audit_input.relative_to(run).as_posix() in audit_messages, audit_messages
            assert "审计" in audit_messages or "audit" in audit_messages.lower()
            audit_input.write_bytes(audit_input_bytes)
            audit_restored = paper.validate_paper_run(
                run, strict=True, require_final=True, require_audit=True
            )
            assert audit_restored["ok"], (audit_input, audit_restored)

        # Even dry-run compilation must validate both paths before printing a
        # command plan; traversal and absolute-path escapes are never accepted.
        expect_exit(
            2,
            paper_v5.cmd_compile,
            ns(
                run=run_id,
                source="../../harness_config.json",
                output="deliverables/traversal.pdf",
                engine="pandoc",
                replace=False,
                dry_run=True,
            ),
        )
        expect_exit(
            2,
            paper_v5.cmd_compile,
            ns(
                run=run_id,
                source=str((ROOT / "harness_config.json").resolve()),
                output="deliverables/absolute-source.pdf",
                engine="pandoc",
                replace=False,
                dry_run=True,
            ),
        )
        expect_exit(
            2,
            paper_v5.cmd_compile,
            ns(
                run=run_id,
                source="paper/FINAL_MANUSCRIPT.md",
                output=str((ROOT / "escaped-manuscript.pdf").resolve()),
                engine="pandoc",
                replace=False,
                dry_run=True,
            ),
        )
        expect_exit(
            2,
            paper_v5.cmd_compile,
            ns(
                run=run_id,
                source="paper/FINAL_MANUSCRIPT.md",
                output="deliverables/packages/reserved-paper.pdf",
                engine="pandoc",
                replace=False,
                dry_run=True,
            ),
        )
        expect_exit(
            2,
            paper_v5.cmd_compile,
            ns(
                run=run_id,
                source="paper/FINAL_MANUSCRIPT.md",
                output="deliverables/AI工具使用详情.pdf",
                engine="pandoc",
                replace=False,
                dry_run=True,
            ),
        )
        assert paper_v5.cmd_compile(
            ns(
                run=run_id,
                source="paper/FINAL_MANUSCRIPT.md",
                output="deliverables/FINAL_MANUSCRIPT.pdf",
                engine="pandoc",
                replace=False,
                dry_run=True,
            )
        ) == 0

        # Exercise the real compile control flow without depending on an
        # optional local Pandoc installation.  The mocked process writes the
        # exact temporary PDF requested by cmd_compile, so atomic replacement,
        # hashing and receipt generation remain production code paths.
        def fake_pandoc(command, **_kwargs):
            if "--version" in command:
                return subprocess.CompletedProcess(
                    command, 0, "pandoc 3.0 (synthetic self-test)\n", ""
                )
            write_minimal_pdf(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, "", "")

        compile_output = run / "deliverables/FINAL_MANUSCRIPT.pdf"
        compile_receipt = run / "deliverables/PAPER_COMPILE_RECEIPT.json"
        compile_args = {
            "run": run_id,
            "source": "paper/FINAL_MANUSCRIPT.md",
            "output": "deliverables/FINAL_MANUSCRIPT.pdf",
            "engine": "pandoc",
            "dry_run": False,
        }
        with (
            patch.object(
                paper_v5.shutil,
                "which",
                return_value=str((ROOT / "tools/synthetic-pandoc.exe").resolve()),
            ),
            patch.object(paper_v5.subprocess, "run", side_effect=fake_pandoc),
        ):
            assert paper_v5.cmd_compile(ns(**compile_args, replace=False)) == 0
        assert compile_output.is_file() and compile_output.stat().st_size > 0
        assert compile_receipt.is_file()
        receipt_payload = json.loads(compile_receipt.read_text(encoding="utf-8-sig"))
        assert receipt_payload["source_sha256"] == common.sha256_file(final_path)
        assert receipt_payload["output_sha256"] == common.sha256_file(compile_output)
        expect_exit(2, paper_v5.cmd_compile, ns(**compile_args, replace=False))
        with (
            patch.object(
                paper_v5.shutil,
                "which",
                return_value=str((ROOT / "tools/synthetic-pandoc.exe").resolve()),
            ),
            patch.object(paper_v5.subprocess, "run", side_effect=fake_pandoc),
        ):
            assert paper_v5.cmd_compile(ns(**compile_args, replace=True)) == 0
        receipt_payload = json.loads(compile_receipt.read_text(encoding="utf-8-sig"))
        assert receipt_payload["output_path"] == "deliverables/FINAL_MANUSCRIPT.pdf"
        assert receipt_payload["output_sha256"] == common.sha256_file(compile_output)

        # Assembly/build receipts bind title, order, inclusion and section
        # path.  Every plan mutation after finalize must invalidate the build.
        canonical_plan_bytes = plan_path.read_bytes()
        for mutation in ("title", "order", "include", "path"):
            mutated = json.loads(canonical_plan_bytes.decode("utf-8"))
            abstract = next(item for item in mutated["sections"] if item["id"] == "abstract")
            if mutation == "title":
                mutated["working_title"] += " changed"
            elif mutation == "order":
                abstract["order"] = max(int(item["order"]) for item in mutated["sections"]) + 100
            elif mutation == "include":
                abstract["include_in_manuscript"] = False
            else:
                abstract["file"] = "paper/sections/998_abstract_changed.md"
            write_json_file(plan_path, mutated)
            paper_check = paper.validate_paper_run(
                run, strict=True, require_final=True, require_audit=True
            )
            assert not paper_check["ok"], (mutation, paper_check)
            plan_path.write_bytes(canonical_plan_bytes)
            restored = paper.validate_paper_run(
                run, strict=True, require_final=True, require_audit=True
            )
            assert restored["ok"], (mutation, restored)

        # Unplanned section files are retained and reported as warnings only.
        orphan_check = paper.validate_paper_run(
            run, strict=True, require_final=True, require_audit=True
        )
        assert orphan_check["ok"], orphan_check
        assert any("999_orphan.md" in item for item in orphan_check["warnings"]), orphan_check
        assert paper.cmd_assemble(ns(run=run_id)) == 0
        assert orphan.is_file()
        post_orphan = paper.validate_paper_run(
            run, strict=True, require_final=True, require_audit=True
        )
        assert post_orphan["ok"], post_orphan

        # Finalization and assembly do not mutate the scanned section corpus.
        # Lint the existing receipt instead of rewriting an audit-bound claim
        # registry after human paper approval.
        report, code = claim_guard.lint(run, strict=True)
        assert code == 0, report

        # FINAL_MANUSCRIPT and the paper workspace are not blind-science inputs:
        # the pre-freeze packet must remain fresh without a post-finalize export.
        report, code = blind_review.validate(run, "BR-001", strict=True)
        assert code == 0, report

        # Approving the post-paper strong-claim stage must preserve the earlier
        # G5-bound consistency approval now that registry order encodes the
        # actual lifecycle.  Paper extension follows the finalized manuscript.
        before_strong_state = json.loads(
            (run / "V51_STATUS.json").read_text(encoding="utf-8-sig")
        )
        before_consistency = before_strong_state["stages"]["consistency"]
        assert v51_orchestrator.cmd_approve(
            ns(
                run=run_id,
                stage="strong_claim_audit",
                review_id=None,
                by="SELFTEST_REVIEWER",
                note="post-paper strong-claim approval",
            )
        ) == 0
        after_strong_state = json.loads(
            (run / "V51_STATUS.json").read_text(encoding="utf-8-sig")
        )
        assert after_strong_state["stages"]["consistency"] == before_consistency

        for stage, review_id in (("paper_extension", None),):
            assert v51_orchestrator.cmd_approve(
                ns(
                    run=run_id,
                    stage=stage,
                    review_id=review_id,
                    by="SELFTEST_REVIEWER",
                    note="post-paper stage approval",
                )
            ) == 0
            report, code = v51_orchestrator.validate_stage(run, stage, review_id=review_id)
            assert code == 0, (stage, report)
            state = json.loads((run / "V51_STATUS.json").read_text(encoding="utf-8-sig"))
            approved = state["stages"][stage]
            assert approved["status"] == "approved", approved
            assert approved["approved_input_sha256"] == report["input_sha256"], approved

        common.append_jsonl(
            run / "ai/AI_USAGE_LOG.jsonl",
            {
                "event_id": "AI-SELFTEST-000",
                "timestamp": "2026-09-02T12:00:00+08:00",
                "tool": "offline harness self-test",
                "model": "none",
                "stage": "regression",
                "purpose": "Validate deterministic local controls",
                "prompt_or_task_summary": "No model or network call",
                "input_artifacts": [],
                "output_artifacts": [],
                "adopted": True,
                "human_modified": False,
                "human_verified": True,
                "verified_by": "SELFTEST_REVIEWER",
                "notes": "Tooling provenance only.",
            },
        )
        expect_exit(
            2,
            harness.build_parser().parse_args,
            ["export-ai", "--run", run_id],
        )
        assert harness.cmd_export_ai(ns(run=run_id, by="SELFTEST")) == 0
        public_ai_details = (run / "ai/AI_TOOL_USAGE_DETAILS.md").read_text(
            encoding="utf-8-sig"
        )
        assert "SELFTEST_REVIEWER" not in public_ai_details, public_ai_details
        assert f"Run: `{run_id}`" not in public_ai_details, public_ai_details
        ai_disclosure_pdf = run / harness.AI_DISCLOSURE_PDF_RELATIVE
        write_minimal_pdf(ai_disclosure_pdf)
        orphan_ai_pdf_g7 = harness.validate_gate(run, "G7", strict=True)
        orphan_ai_pdf_audit = orphan_ai_pdf_g7["compliance_audit"][
            "ai_disclosure_pdf"
        ]
        assert orphan_ai_pdf_audit["errors"], orphan_ai_pdf_audit
        assert any(
            "exists without" in item and "RECEIPT" in item
            for item in orphan_ai_pdf_audit["errors"]
        ), orphan_ai_pdf_audit
        expect_exit(
            2,
            harness.build_parser().parse_args,
            ["attest-ai-disclosure-pdf", "--run", run_id],
        )
        expect_exit(
            2,
            harness.cmd_attest_ai_disclosure_pdf,
            ns(run=run_id, by="TEAM"),
        )
        assert harness.cmd_attest_ai_disclosure_pdf(
            ns(run=run_id, by="SELFTEST_REVIEWER")
        ) == 0
        ai_disclosure_receipt = run / harness.AI_DISCLOSURE_PDF_RECEIPT_RELATIVE
        assert ai_disclosure_receipt.is_file()
        ai_pdf_ready_g7 = harness.validate_gate(run, "G7", strict=True)
        assert not ai_pdf_ready_g7["compliance_audit"]["ai_disclosure_pdf"][
            "errors"
        ], ai_pdf_ready_g7
        (run / "deliverables/FINAL_CHECKLIST.md").write_text(
            "# Final checklist\n\n"
            "- [x] [final-manuscript-current] Final manuscript and compile receipt are current.\n"
            "- [x] [pre-g7-approvals-current] Freeze and all pre-G7 human approvals are current.\n"
            "- [x] [claims-and-evidence-reviewed] Claims and evidence were independently reviewed.\n"
            "- [x] [sources-and-citations-reviewed] Sources and citations were independently reviewed.\n"
            "- [x] [ai-disclosure-reviewed] AI-use disclosure and rendered PDF were reviewed.\n"
            "- [x] [ai-log-completeness-reviewed] Every AI event is logged and the declaration purpose matches the log.\n"
            "- [x] [submission-anonymity-reviewed] Every rendered page and metadata field was checked for identity leakage.\n"
            "- [x] [support-materials-anonymity-reviewed] Every support member, filename and metadata field was checked for identity leakage.\n"
            "- [x] [delivery-files-opened] Delivery files were opened and checked.\n\n"
            "Reviewer: SELFTEST\n",
            encoding="utf-8",
        )
        assert harness.cmd_build_support(
            ns(
                run=run_id,
                include=["code/model.py"],
                identity=[],
                replace=False,
                by="SELFTEST_REVIEWER",
            )
        ) == 0
        support_policy, support_policy_errors = harness._official_support_policy()
        assert not support_policy_errors, support_policy_errors
        support_zip = run / str(support_policy["output"])
        support_receipt = run / str(support_policy["receipt"])
        assert support_zip.is_file() and support_receipt.is_file()
        support_ready = harness._official_support_archive_audit(
            run, ai_required=True
        )
        assert support_ready["passed"], support_ready
        assert support_ready["within_limit"] is True, support_ready
        with zipfile.ZipFile(support_zip, "r") as official_archive:
            official_members = official_archive.namelist()
        assert official_members == ["AI工具使用详情.pdf", "code/model.py"], official_members
        assert "STATUS.json" not in official_members
        assert str(support_policy["receipt"]) not in official_members

        # G7 compliance receipts are content-bound.  Probe AI export drift,
        # no-source attestation lifecycle, malformed nonempty source rows, and
        # every checklist terminal condition before approving submission.
        def g7_error_text() -> tuple[dict[str, object], str]:
            result = harness.validate_gate(run, "G7", strict=True)
            return result, "\n".join(str(item) for item in result["errors"])

        compile_receipt_bytes = compile_receipt.read_bytes()
        compile_pdf_bytes = compile_output.read_bytes()
        final_manuscript_bytes = final_path.read_bytes()
        compile_receipt.unlink()
        _, compliance_messages = g7_error_text()
        assert (
            "PDF artifacts" in compliance_messages
            and "FINAL_MANUSCRIPT.pdf" in compliance_messages
        ), compliance_messages
        compile_receipt.write_bytes(compile_receipt_bytes)

        malformed_compile_receipt = json.loads(compile_receipt_bytes.decode("utf-8"))
        malformed_compile_receipt.pop("engine_version")
        write_json_file(compile_receipt, malformed_compile_receipt)
        _, compliance_messages = g7_error_text()
        assert (
            "schema" in compliance_messages.lower()
            and "engine_version" in compliance_messages
        ), compliance_messages
        expect_exit(
            2,
            harness._collect_delivery_files,
            run,
            allow_missing_fixed=True,
        )
        compile_receipt.write_bytes(compile_receipt_bytes)

        compile_output.unlink()
        _, compliance_messages = g7_error_text()
        assert "output_path does not resolve" in compliance_messages, compliance_messages
        compile_output.write_bytes(compile_pdf_bytes)

        final_path.write_bytes(final_manuscript_bytes + b"\npost-compile drift\n")
        _, compliance_messages = g7_error_text()
        assert "does not bind the current FINAL_MANUSCRIPT.md" in compliance_messages
        final_path.write_bytes(final_manuscript_bytes)

        compile_output.write_bytes(compile_pdf_bytes + b"tampered")
        _, compliance_messages = g7_error_text()
        assert "does not bind the current PDF bytes" in compliance_messages
        compile_output.write_bytes(compile_pdf_bytes)

        orphan_compile_pdf = run / "deliverables/orphan-manuscript.pdf"
        orphan_compile_pdf.write_bytes(b"%PDF-1.4\n% unreceipted orphan\n")
        orphan_pdf_g7, compliance_messages = g7_error_text()
        assert not orphan_pdf_g7["ok"], orphan_pdf_g7
        assert (
            "not the unique output" in compliance_messages
            or "orphan" in compliance_messages.lower()
            or "extra" in compliance_messages.lower()
        ), compliance_messages
        orphan_compile_pdf.unlink()

        ai_log_path = run / "ai/AI_USAGE_LOG.jsonl"
        ai_log_bytes = ai_log_path.read_bytes()
        ai_log_path.write_bytes(ai_log_bytes + b"\n")
        _, compliance_messages = g7_error_text()
        assert "source_sha256" in compliance_messages, compliance_messages
        ai_log_path.write_bytes(ai_log_bytes)

        ai_details_path = run / "ai/AI_TOOL_USAGE_DETAILS.md"
        ai_details_bytes = ai_details_path.read_bytes()
        ai_details_path.write_bytes(ai_details_bytes + b"\n")
        _, compliance_messages = g7_error_text()
        assert "export_sha256" in compliance_messages, compliance_messages
        assert "current Markdown export" in compliance_messages, compliance_messages
        ai_details_path.write_bytes(ai_details_bytes)

        ai_disclosure_pdf_bytes = ai_disclosure_pdf.read_bytes()
        ai_disclosure_pdf.write_bytes(ai_disclosure_pdf_bytes + b"tampered")
        ai_pdf_tamper_g7, compliance_messages = g7_error_text()
        assert not ai_pdf_tamper_g7["ok"], ai_pdf_tamper_g7
        assert "current PDF bytes" in compliance_messages, compliance_messages
        ai_disclosure_pdf.write_bytes(ai_disclosure_pdf_bytes)

        source_ledger_path = run / "sources/SOURCE_LEDGER.csv"
        source_ledger_bytes = source_ledger_path.read_bytes()
        no_source_path = run / "sources/NO_SOURCE_ATTESTATION.json"
        no_source_bytes = no_source_path.read_bytes()
        no_source_path.unlink()
        _, compliance_messages = g7_error_text()
        assert "NO_SOURCE_ATTESTATION" in compliance_messages, compliance_messages
        no_source_path.write_bytes(no_source_bytes)

        source_ledger_path.write_bytes(source_ledger_bytes + b"\n")
        _, compliance_messages = g7_error_text()
        assert "NO_SOURCE_ATTESTATION" in compliance_messages, compliance_messages
        assert "SOURCE_LEDGER" in compliance_messages, compliance_messages
        source_ledger_path.write_bytes(source_ledger_bytes)

        invalid_source = {
            "source_id": "SRC-DUP",
            "title": "",
            "author_or_org": "",
            "year": "2026",
            "url": "ftp://invalid.example/source",
            "source_type": "web",
            "claim_supported": "Synthetic invalid-row regression",
            "accessed_at": "2026-09-02T12:00:00",
            "verified_by": "",
            "notes": "deliberately malformed",
        }
        duplicate_source = dict(invalid_source)
        duplicate_source.update(
            {
                "title": "Second row",
                "author_or_org": "Harness self-test",
                "url": "https://example.invalid/source",
                "accessed_at": "2026-09-02T12:00:00+08:00",
                "verified_by": "SELFTEST",
            }
        )
        write_csv_rows(
            source_ledger_path,
            harness.SOURCE_FIELDS,
            [invalid_source, duplicate_source],
        )
        _, compliance_messages = g7_error_text()
        assert "重复" in compliance_messages or "duplicate" in compliance_messages.lower()
        assert "缺少字段" in compliance_messages or "missing" in compliance_messages.lower()
        assert "url" in compliance_messages.lower(), compliance_messages
        assert "时区" in compliance_messages or "timezone" in compliance_messages.lower()
        source_ledger_path.write_bytes(source_ledger_bytes)

        checklist_path = run / "deliverables/FINAL_CHECKLIST.md"
        checklist_bytes = checklist_path.read_bytes()
        checklist_path.write_text("# Final checklist\n\nReviewer: SELFTEST\n", encoding="utf-8")
        _, compliance_messages = g7_error_text()
        assert "checkbox" in compliance_messages.lower(), compliance_messages
        checklist_path.write_text(
            "# Final checklist\n\n- [ ] unresolved item\n\nReviewer: SELFTEST\n",
            encoding="utf-8",
        )
        _, compliance_messages = g7_error_text()
        assert "未勾选" in compliance_messages or "unchecked" in compliance_messages.lower()
        checklist_path.write_text(
            "# Final checklist\n\n- [x] reviewed item\n",
            encoding="utf-8",
        )
        _, compliance_messages = g7_error_text()
        assert "Reviewer" in compliance_messages or "审核人" in compliance_messages
        checklist_path.write_bytes(checklist_bytes)

        compliance_ready, _ = g7_error_text()
        assert not compliance_ready["compliance_audit"]["ai_export"]["errors"], compliance_ready
        assert not compliance_ready["compliance_audit"]["ai_disclosure_pdf"]["errors"], compliance_ready
        assert not compliance_ready["compliance_audit"]["sources"]["errors"], compliance_ready
        assert not compliance_ready["compliance_audit"]["final_checklist"]["errors"], compliance_ready
        assert not compliance_ready["compliance_audit"]["paper_compile"]["errors"], compliance_ready
        submission_delivery = compliance_ready["compliance_audit"]["submission_delivery"]
        assert not submission_delivery["errors"], submission_delivery
        assert submission_delivery["final_pdf"]["within_limit"] is True
        assert submission_delivery["final_pdf"]["max_bytes"] == 20_000_000
        assert submission_delivery["ai_statement"]["before_references"] is True
        assert submission_delivery["ai_statement"]["body_present"] is True
        assert submission_delivery["ai_statement"]["declared_usage"] == "used"
        assert submission_delivery["ai_statement"]["log_usage"] == "used"
        assert submission_delivery["ai_statement"]["usage_consistent"] is True
        assert submission_delivery["ai_statement"]["purpose_consistency_attested"] is True
        assert submission_delivery["anonymity"]["count"] == 0
        official_support = compliance_ready["compliance_audit"][
            "official_support_archive"
        ]
        assert official_support["passed"], official_support
        assert official_support["within_limit"] is True, official_support

        # Submission validation can run while its approval is still pending,
        # but G7 itself must reject that pending state.
        report, code = v51_orchestrator.validate_stage(run, "submission")
        assert code == 0, report
        g7_unapproved = harness.validate_gate(run, "G7", strict=True)
        assert not g7_unapproved["ok"], g7_unapproved
        submission_check = next(
            item["result"] for item in g7_unapproved["v51"] if item["stage"] == "submission"
        )
        assert submission_check["passed"], submission_check
        assert any("submission" in item.lower() for item in g7_unapproved["errors"]), g7_unapproved

        assert v51_orchestrator.cmd_approve(
            ns(
                run=run_id,
                stage="submission",
                review_id=None,
                by="SELFTEST_REVIEWER",
                note="all seven prerequisite stages are current",
            )
        ) == 0
        report, code = v51_orchestrator.validate_stage(run, "submission")
        assert code == 0, report
        state = json.loads((run / "V51_STATUS.json").read_text(encoding="utf-8-sig"))
        assert list(state["stages"]) == list(v51_orchestrator.STAGES), state
        assert all(state["stages"][stage]["status"] == "approved" for stage in v51_orchestrator.STAGES), state
        assert state["stages"]["submission"]["approved_input_sha256"] == report["input_sha256"]

        final_freeze_check = harness.verify_freeze(run, quiet=True)
        assert final_freeze_check["ok"], final_freeze_check

        g7 = harness.validate_gate(run, "G7", strict=True)
        assert g7["ok"], g7

        # Approval projections deliberately ignore validation timestamps and
        # generated-at noise, but bind every human approval field and stored
        # input manifest.  A normal revalidation therefore remains current;
        # semantic/status tampering changes both submission and G7 hashes.
        approved_submission_hash = report["input_sha256"]
        approved_g7_hash = g7["input_sha256"]
        assert v51_orchestrator.cmd_validate(
            ns(run=run_id, stage="interpretation", review_id=None)
        ) == 0
        revalidated_submission, revalidated_code = v51_orchestrator.validate_stage(
            run, "submission"
        )
        assert revalidated_code == 0, revalidated_submission
        assert revalidated_submission["input_sha256"] == approved_submission_hash
        revalidated_g7 = harness.validate_gate(run, "G7", strict=True)
        assert revalidated_g7["ok"], revalidated_g7
        assert revalidated_g7["input_sha256"] == approved_g7_hash

        v51_path = run / "V51_STATUS.json"
        canonical_v51_bytes = v51_path.read_bytes()
        v51_schema_path = ROOT / "schemas/v51_status.schema.json"
        assert not schema_validate.validate_json_file(v51_path, v51_schema_path)

        malformed_status = json.loads(canonical_v51_bytes.decode("utf-8"))
        malformed_status["stages"]["interpretation"]["approved_at"] = (
            "2026-09-02T13:00:00"
        )
        write_json_file(v51_path, malformed_status)
        assert schema_validate.validate_json_file(v51_path, v51_schema_path)
        malformed_status_g7 = harness.validate_gate(run, "G7", strict=True)
        assert not malformed_status_g7["ok"], malformed_status_g7
        assert any(
            "V51_STATUS" in item or "v5.1" in item.lower()
            for item in malformed_status_g7["errors"]
        ), malformed_status_g7
        v51_path.write_bytes(canonical_v51_bytes)

        malformed_status = json.loads(canonical_v51_bytes.decode("utf-8"))
        malformed_status["stages"]["interpretation"]["status"] = "pending"
        write_json_file(v51_path, malformed_status)
        assert schema_validate.validate_json_file(v51_path, v51_schema_path)
        v51_path.write_bytes(canonical_v51_bytes)

        approval_mutations = {
            "approved_by": "ANOTHER_REVIEWER",
            "approved_at": "2026-09-02T13:00:00+08:00",
            "note": "tampered approval note",
            "approved_input_sha256": "0" * 64,
        }
        for field, value in approval_mutations.items():
            tampered_state = json.loads(canonical_v51_bytes.decode("utf-8"))
            tampered_state["stages"]["interpretation"][field] = value
            write_json_file(v51_path, tampered_state)
            tampered_submission, tampered_code = v51_orchestrator.validate_stage(
                run, "submission"
            )
            assert tampered_submission["input_sha256"] != approved_submission_hash, (
                field,
                tampered_submission,
            )
            # Every approval field is part of the semantic projection captured
            # by the scientific freeze.  Even a schema-valid reviewer/time/note
            # change therefore invalidates downstream validation; a changed
            # approved-input hash additionally makes the status record
            # internally inconsistent with its stored validation result.
            assert_failed(tampered_submission, tampered_code)
            tampered_g7 = harness.validate_gate(run, "G7", strict=True)
            assert not tampered_g7["ok"], (field, tampered_g7)
            assert tampered_g7["input_sha256"] != approved_g7_hash
            v51_path.write_bytes(canonical_v51_bytes)
            restored_projection, restored_projection_code = v51_orchestrator.validate_stage(
                run, "submission"
            )
            assert restored_projection_code == 0, restored_projection
            assert restored_projection["input_sha256"] == approved_submission_hash

        tampered_state = json.loads(canonical_v51_bytes.decode("utf-8"))
        prior_manifest = tampered_state["stages"]["interpretation"]["last_result"]["input_manifest"]
        assert prior_manifest, tampered_state
        prior_manifest[0]["sha256"] = "1" * 64
        write_json_file(v51_path, tampered_state)
        tampered_prior_manifest, tampered_prior_code = v51_orchestrator.validate_stage(
            run, "submission"
        )
        assert_failed(tampered_prior_manifest, tampered_prior_code, "stored input manifest")
        assert tampered_prior_manifest["input_sha256"] != approved_submission_hash
        tampered_prior_g7 = harness.validate_gate(run, "G7", strict=True)
        assert not tampered_prior_g7["ok"], tampered_prior_g7
        assert tampered_prior_g7["input_sha256"] != approved_g7_hash
        v51_path.write_bytes(canonical_v51_bytes)

        tampered_state = json.loads(canonical_v51_bytes.decode("utf-8"))
        submission_manifest = tampered_state["stages"]["submission"]["last_result"]["input_manifest"]
        assert submission_manifest, tampered_state
        submission_manifest[0]["sha256"] = "2" * 64
        write_json_file(v51_path, tampered_state)
        tampered_stored_g7 = harness.validate_gate(run, "G7", strict=True)
        assert not tampered_stored_g7["ok"], tampered_stored_g7
        assert tampered_stored_g7["input_sha256"] != approved_g7_hash
        assert any(
            "已存输入清单" in item or "stored" in item.lower()
            for item in tampered_stored_g7["errors"]
        ), tampered_stored_g7
        v51_path.write_bytes(canonical_v51_bytes)
        restored_projection_g7 = harness.validate_gate(run, "G7", strict=True)
        assert restored_projection_g7["ok"], restored_projection_g7
        assert restored_projection_g7["input_sha256"] == approved_g7_hash

        # Revalidation is observational.  It must not create, delete, rewrite,
        # or merely touch any run artifact, including derived audit reports.
        for stage, review_id in (
            ("mathematical_construction", None),
            ("independent_review", "BR-001"),
            ("consistency", None),
            ("submission", None),
        ):
            before_validation = run_file_snapshot(run)
            pure_result, pure_code = v51_orchestrator.validate_stage(
                run, stage, review_id=review_id
            )
            assert pure_code == 0, (stage, pure_result)
            after_validation = run_file_snapshot(run)
            assert after_validation == before_validation, stage

        # A machine-valid byte drift is distinct from validator failure.  The
        # former demotes an approved stage to `validated`; the latter records a
        # canonical `blocked` result.  Use the post-freeze strong-claim stage so
        # this lifecycle test does not rewrite the G5-bound consistency
        # approval and thereby invalidate the scientific freeze.
        strong_receipt_path = run / "claims/STRONG_CLAIM_SCAN.json"
        strong_receipt_bytes = strong_receipt_path.read_bytes()

        def approve_strong_downstream(note: str) -> None:
            for stage in ("strong_claim_audit", "paper_extension", "submission"):
                assert v51_orchestrator.cmd_approve(
                    ns(
                        run=run_id,
                        stage=stage,
                        review_id=None,
                        by="SELFTEST_REVIEWER",
                        note=note,
                    )
                ) == 0

        clean_drift_receipt = json.loads(strong_receipt_bytes.decode("utf-8"))
        clean_drift_receipt["generated_at"] = "2000-01-01T00:00:00+00:00"
        write_json_file(strong_receipt_path, clean_drift_receipt)
        strong_live, strong_live_code = v51_orchestrator.validate_stage(
            run, "strong_claim_audit"
        )
        assert strong_live_code == 0 and strong_live["passed"], strong_live
        assert v51_orchestrator.cmd_validate(
            ns(run=run_id, stage="strong_claim_audit", review_id=None)
        ) == 0
        drifted_state = json.loads(
            (run / "V51_STATUS.json").read_text(encoding="utf-8-sig")
        )
        drifted_strong = drifted_state["stages"]["strong_claim_audit"]
        assert drifted_strong["status"] == "validated", drifted_strong
        assert drifted_strong["last_result"]["passed"] is True
        assert drifted_strong["approved_input_sha256"] is None
        for downstream in ("paper_extension", "submission"):
            downstream_state = drifted_state["stages"][downstream]
            assert downstream_state["status"] == "pending", downstream_state
            assert downstream_state["approved_input_sha256"] is None, downstream_state
        stale_submission, stale_code = v51_orchestrator.validate_stage(run, "submission")
        assert_failed(stale_submission, stale_code)
        stale_g7 = harness.validate_gate(run, "G7", strict=True)
        assert not stale_g7["ok"], stale_g7

        strong_receipt_path.write_bytes(strong_receipt_bytes)
        approve_strong_downstream("re-approved after clean receipt drift restoration")
        assert harness.verify_freeze(run, quiet=True)["ok"]
        assert harness.validate_gate(run, "G7", strict=True)["ok"]

        failing_receipt = json.loads(strong_receipt_bytes.decode("utf-8"))
        failing_receipt["canonical_complete"] = False
        write_json_file(strong_receipt_path, failing_receipt)
        assert v51_orchestrator.cmd_validate(
            ns(run=run_id, stage="strong_claim_audit", review_id=None)
        ) == 3
        blocked_state = json.loads(
            (run / "V51_STATUS.json").read_text(encoding="utf-8-sig")
        )
        blocked_strong = blocked_state["stages"]["strong_claim_audit"]
        assert blocked_strong["status"] == "blocked", blocked_strong
        assert blocked_strong["last_result"]["passed"] is False
        assert blocked_strong["last_result"]["errors"]
        assert blocked_strong["approved_input_sha256"] is None
        for downstream in ("paper_extension", "submission"):
            downstream_state = blocked_state["stages"][downstream]
            assert downstream_state["status"] == "pending", downstream_state
            assert downstream_state["last_result"] is None, downstream_state

        strong_receipt_path.write_bytes(strong_receipt_bytes)
        approve_strong_downstream("re-approved after failing receipt restoration")
        restored_submission, restored_code = v51_orchestrator.validate_stage(run, "submission")
        assert restored_code == 0, restored_submission
        restored_g7 = harness.validate_gate(run, "G7", strict=True)
        assert restored_g7["ok"], restored_g7
        assert harness.verify_freeze(run, quiet=True)["ok"]

        # G0-G4 were approved before the narrowly scoped freeze exception.
        # Complete the retained review and paper gates after all v5.1 stages
        # have been approved and revalidated.
        for gate_id in ("G6",):
            assert review_then_approve(gate_id) == 0

        # Packaging defaults to fail closed while G7 itself is unapproved.  A
        # diagnostic bypass is explicit and audited; after G7 approval the
        # ordinary package path succeeds without a bypass.
        package_root = run / "deliverables/packages"
        package_root.mkdir(parents=True, exist_ok=True)
        diagnostic_package = package_root / "diagnostic-package.zip"
        expect_exit(
            2,
            harness.cmd_package,
            ns(
                run=run_id,
                out=str(diagnostic_package),
                replace=False,
                allow_unvalidated=False,
                by="SELFTEST_REVIEWER",
                note="must reject pending G7",
            ),
        )
        assert harness.cmd_package(
            ns(
                run=run_id,
                out=str(diagnostic_package),
                replace=False,
                allow_unvalidated=True,
                by="SELFTEST_REVIEWER",
                note="diagnostic lifecycle regression",
            )
        ) == 0
        assert diagnostic_package.is_file()
        assert diagnostic_package.with_suffix(".zip.sha256").is_file()
        bypass_events = [
            item
            for item in common.read_jsonl(run / "decisions/DECISION_LOG.jsonl")
            if item.get("type") == "package-validation-bypass"
        ]
        assert bypass_events and bypass_events[-1]["by"] == "SELFTEST_REVIEWER", bypass_events
        post_bypass_submission, post_bypass_code = v51_orchestrator.validate_stage(run, "submission")
        assert post_bypass_code == 0, post_bypass_submission
        post_bypass_g7 = harness.validate_gate(run, "G7", strict=True)
        assert post_bypass_g7["ok"], post_bypass_g7

        pre_g7_approval_submission_hash = post_bypass_submission["input_sha256"]
        pre_g7_approval_hash = post_bypass_g7["input_sha256"]

        assert review_then_approve("G7") == 0

        # G7 approval appends its own complete validation receipt to the shared
        # decision log.  That audit write must not make either the submission
        # stage or the just-approved G7 snapshot stale.  The G7 decision-log
        # projection excludes only this self-referential event class while
        # retaining all other audit history.
        post_g7_approval_submission, post_g7_approval_code = (
            v51_orchestrator.validate_stage(run, "submission")
        )
        assert post_g7_approval_code == 0, post_g7_approval_submission
        assert (
            post_g7_approval_submission["input_sha256"]
            == pre_g7_approval_submission_hash
        ), post_g7_approval_submission
        post_g7_approval = harness.validate_gate(run, "G7", strict=True)
        assert post_g7_approval["ok"], post_g7_approval
        assert post_g7_approval["input_sha256"] == pre_g7_approval_hash
        g7_freshness, g7_freshness_errors = harness._approved_gate_freshness(
            run, ["G7"]
        )
        assert not g7_freshness_errors, g7_freshness_errors
        assert g7_freshness == [
            {
                "gate": "G7",
                "status": "current",
                "approved_input_sha256": pre_g7_approval_hash,
                "current_input_sha256": pre_g7_approval_hash,
                "validation_ok": True,
                "input_matches": True,
            }
        ], g7_freshness

        # Repeating an unchanged G7 approval is an idempotent no-op.  Appending
        # another self-referential receipt would otherwise make the freshly
        # approved snapshot stale immediately.
        decision_log_path = run / "decisions/DECISION_LOG.jsonl"
        repeat_log_bytes = decision_log_path.read_bytes()
        repeat_status_bytes = (run / "STATUS.json").read_bytes()
        assert harness.cmd_approve(ns(gate="G7", **gate_args)) == 0
        assert decision_log_path.read_bytes() == repeat_log_bytes
        assert (run / "STATUS.json").read_bytes() == repeat_status_bytes
        repeat_g7 = harness.validate_gate(run, "G7", strict=True)
        assert repeat_g7["ok"], repeat_g7
        assert repeat_g7["input_sha256"] == pre_g7_approval_hash

        # The projection must not hide unrelated decision-log tampering.  A
        # historical freeze event is non-self-referential, so changing its
        # reviewer leaves the current strict validation structurally valid but
        # makes the already-approved G7 snapshot stale.  Formal packaging must
        # therefore reject it while the submission-stage science/paper hash
        # remains untouched.
        canonical_decision_log_bytes = decision_log_path.read_bytes()
        decision_events = common.read_jsonl(decision_log_path)
        freeze_event = next(
            event for event in decision_events if event.get("type") == "freeze"
        )
        freeze_event["by"] = "TAMPERED_REVIEWER"
        decision_log_path.write_text(
            "".join(
                json.dumps(event, ensure_ascii=False) + "\n"
                for event in decision_events
            ),
            encoding="utf-8",
        )
        tampered_submission, tampered_submission_code = (
            v51_orchestrator.validate_stage(run, "submission")
        )
        assert tampered_submission_code == 0, tampered_submission
        assert (
            tampered_submission["input_sha256"]
            == pre_g7_approval_submission_hash
        ), tampered_submission
        tampered_g7_decision = harness.validate_gate(run, "G7", strict=True)
        assert tampered_g7_decision["ok"], tampered_g7_decision
        assert tampered_g7_decision["input_sha256"] != pre_g7_approval_hash
        tampered_freshness, tampered_freshness_errors = (
            harness._approved_gate_freshness(run, ["G7"])
        )
        assert tampered_freshness_errors, tampered_freshness
        assert tampered_freshness == [
            {
                "gate": "G7",
                "status": "stale",
                "approved_input_sha256": pre_g7_approval_hash,
                "current_input_sha256": tampered_g7_decision["input_sha256"],
                "validation_ok": True,
                "input_matches": False,
            }
        ], tampered_freshness
        expect_exit(
            2,
            harness.cmd_package,
            ns(
                run=run_id,
                out=str(package_root / "reject-tampered-decision-log.zip"),
                replace=False,
                allow_unvalidated=False,
                by="SELFTEST_REVIEWER",
                note="reject stale G7 decision history",
            ),
        )

        decision_log_path.write_bytes(canonical_decision_log_bytes)
        restored_post_approval_submission, restored_submission_code = (
            v51_orchestrator.validate_stage(run, "submission")
        )
        assert restored_submission_code == 0, restored_post_approval_submission
        assert (
            restored_post_approval_submission["input_sha256"]
            == pre_g7_approval_submission_hash
        )
        restored_post_approval_g7 = harness.validate_gate(run, "G7", strict=True)
        assert restored_post_approval_g7["ok"], restored_post_approval_g7
        assert restored_post_approval_g7["input_sha256"] == pre_g7_approval_hash

        def reapprove_after_package_bypass(
            approved_hash: str, *, note: str
        ) -> str:
            current = harness.validate_gate(run, "G7", strict=True)
            assert current["ok"], current
            assert current["input_sha256"] != approved_hash, current
            freshness, freshness_errors = harness._approved_gate_freshness(
                run, ["G7"]
            )
            assert freshness_errors, freshness
            assert freshness == [
                {
                    "gate": "G7",
                    "status": "stale",
                    "approved_input_sha256": approved_hash,
                    "current_input_sha256": current["input_sha256"],
                    "validation_ok": True,
                    "input_matches": False,
                }
            ], freshness
            assert review_then_approve("G7", note=note) == 0
            rebound = harness.validate_gate(run, "G7", strict=True)
            assert rebound["ok"], rebound
            rebound_hash = rebound["input_sha256"]
            rebound_freshness, rebound_errors = harness._approved_gate_freshness(
                run, ["G7"]
            )
            assert not rebound_errors, rebound_errors
            assert rebound_freshness[0]["status"] == "current", rebound_freshness
            assert rebound_freshness[0]["current_input_sha256"] == rebound_hash
            return rebound_hash

        current_g7_hash = pre_g7_approval_hash

        # Strict packaging is an approval-derived allowlist.  Ordinary
        # unbound notes are reported as omitted; unbound delivery/PDF outputs,
        # conflicting snapshot hashes, and current artifact drift fail closed.
        ordinary_unbound = run / "reviews/unbound-packaging-note.txt"
        ordinary_unbound.write_text(
            "This ordinary working note is intentionally outside every approved snapshot.\n",
            encoding="utf-8",
        )
        forged_freeze = json.loads(canonical_freeze_bytes.decode("utf-8"))
        forged_freeze["files"][0]["path"] = "../forged-science.txt"
        write_json_file(freeze_path, forged_freeze)
        expect_exit(
            2,
            harness.cmd_package,
            ns(
                run=run_id,
                out=str(package_root / "reject-forged-freeze.zip"),
                replace=False,
                allow_unvalidated=True,
                by="SELFTEST_REVIEWER",
                note="gate bypass cannot trust a forged freeze allowlist",
            ),
        )
        freeze_path.write_bytes(canonical_freeze_bytes)
        assert harness.verify_freeze(run, quiet=True)["ok"]
        current_g7_hash = reapprove_after_package_bypass(
            current_g7_hash,
            note="re-approved after forged-freeze package bypass regression",
        )

        unbound_pdf = run / "paper/unbound-preview.pdf"
        unbound_pdf.write_bytes(b"%PDF-1.4\n% synthetic unbound preview\n")
        expect_exit(
            2,
            harness.cmd_package,
            ns(
                run=run_id,
                out=str(package_root / "reject-unbound-paper.zip"),
                replace=False,
                allow_unvalidated=False,
                by="SELFTEST_REVIEWER",
                note="reject unbound paper PDF",
            ),
        )
        unbound_pdf.unlink()

        unbound_delivery = run / "deliverables/unbound-output.txt"
        unbound_delivery.write_text("unapproved delivery output\n", encoding="utf-8")
        expect_exit(
            2,
            harness.cmd_package,
            ns(
                run=run_id,
                out=str(package_root / "reject-unbound-delivery.zip"),
                replace=False,
                allow_unvalidated=True,
                by="SELFTEST_REVIEWER",
                note="bypass gate freshness but never bypass the delivery allowlist",
            ),
        )
        unbound_delivery.unlink()
        current_g7_hash = reapprove_after_package_bypass(
            current_g7_hash,
            note="re-approved after unbound-delivery package bypass regression",
        )

        status_path = run / "STATUS.json"
        canonical_status_bytes = status_path.read_bytes()
        conflicting_status = json.loads(canonical_status_bytes.decode("utf-8"))
        g5_manifest = conflicting_status["gates"]["G5"]["validation"]["input_manifest"]
        science_relative = unreferenced_science.relative_to(run).as_posix()
        g5_science_record = next(
            item
            for item in g5_manifest
            if item.get("kind") == "artifact" and item.get("path") == science_relative
        )
        actual_science_sha = common.sha256_file(unreferenced_science)
        g5_science_record["sha256"] = (
            "0" * 64 if actual_science_sha != "0" * 64 else "1" * 64
        )
        write_json_file(status_path, conflicting_status)
        expect_exit(
            2,
            harness.cmd_package,
            ns(
                run=run_id,
                out=str(package_root / "reject-conflicting-snapshot.zip"),
                replace=False,
                allow_unvalidated=False,
                by="SELFTEST_REVIEWER",
                note="approval snapshots may not disagree",
            ),
        )
        status_path.write_bytes(canonical_status_bytes)

        science_text = unreferenced_science.read_text(encoding="utf-8")
        unreferenced_science.write_text(
            science_text + "changed after frozen/approved snapshots\n",
            encoding="utf-8",
        )
        expect_exit(
            2,
            harness.cmd_package,
            ns(
                run=run_id,
                out=str(package_root / "reject-current-drift.zip"),
                replace=False,
                allow_unvalidated=True,
                by="SELFTEST_REVIEWER",
                note="diagnostic bypass cannot package snapshot drift",
            ),
        )
        unreferenced_science.write_text(science_text, encoding="utf-8")
        assert harness.verify_freeze(run, quiet=True)["ok"]
        current_g7_hash = reapprove_after_package_bypass(
            current_g7_hash,
            note="re-approved after snapshot-drift package bypass regression",
        )

        validated_package = package_root / "validated-package.zip"
        assert harness.cmd_package(
            ns(
                run=run_id,
                out=str(validated_package),
                replace=False,
                allow_unvalidated=False,
                by="SELFTEST_REVIEWER",
                note="all gates current",
            )
        ) == 0
        assert validated_package.is_file()
        assert validated_package.with_suffix(".zip.sha256").is_file()
        delivery_manifest = json.loads(
            (run / "deliverables/DELIVERY_MANIFEST.json").read_text(encoding="utf-8-sig")
        )
        package_schema = schema_validate.validate_run(run, stage="package")
        assert package_schema["passed"], package_schema
        delivered_paths = {str(item["path"]) for item in delivery_manifest["files"]}
        omitted_paths = {
            str(item["path"]): str(item["reason"])
            for item in delivery_manifest["omitted"]
        }
        assert ordinary_unbound.relative_to(run).as_posix() not in delivered_paths
        assert omitted_paths[ordinary_unbound.relative_to(run).as_posix()] == (
            "not-bound-by-approved-snapshot"
        )
        assert set(frozen_paths).issubset(delivered_paths), (
            set(frozen_paths) - delivered_paths
        )
        assert harness.AI_DISCLOSURE_PDF_RELATIVE in delivered_paths
        assert harness.AI_DISCLOSURE_PDF_RECEIPT_RELATIVE in delivered_paths
        allowed_binding_prefixes = (
            "freeze:",
            "gate:",
            "v51:",
            "fixed-delivery-contract",
            "paper-compile-audit:",
            "ai-disclosure-pdf-audit:",
        )
        for item in delivery_manifest["files"]:
            assert item["bound_by"], item
            assert all(
                str(source).startswith(allowed_binding_prefixes)
                for source in item["bound_by"]
            ), item
        bindings_by_path = {
            str(item["path"]): {str(source) for source in item["bound_by"]}
            for item in delivery_manifest["files"]
        }
        for relative in (
            "deliverables/FINAL_MANUSCRIPT.pdf",
            "deliverables/PAPER_COMPILE_RECEIPT.json",
        ):
            assert any(
                source.startswith("paper-compile-audit:")
                for source in bindings_by_path[relative]
            ), (relative, bindings_by_path[relative])
        for relative in (
            harness.AI_DISCLOSURE_PDF_RELATIVE,
            harness.AI_DISCLOSURE_PDF_RECEIPT_RELATIVE,
        ):
            assert any(
                source.startswith("ai-disclosure-pdf-audit:")
                for source in bindings_by_path[relative]
            ), (relative, bindings_by_path[relative])
        with zipfile.ZipFile(validated_package) as archive:
            validated_names = set(archive.namelist())
        expected_names = {
            f"{run_id}/{relative}" for relative in delivered_paths
        } | {f"{run_id}/deliverables/DELIVERY_MANIFEST.json"}
        assert validated_names == expected_names, (
            sorted(validated_names - expected_names),
            sorted(expected_names - validated_names),
        )
        assert not any("/deliverables/packages/" in name for name in validated_names)
        post_package_submission, post_package_code = v51_orchestrator.validate_stage(run, "submission")
        assert post_package_code == 0, post_package_submission
        post_package_g7 = harness.validate_gate(run, "G7", strict=True)
        assert post_package_g7["ok"], post_package_g7

        renamed_package = package_root / "renamed-package.zip"
        assert harness.cmd_package(
            ns(
                run=run_id,
                out=str(renamed_package),
                replace=False,
                allow_unvalidated=False,
                by="SELFTEST_REVIEWER",
                note="renamed output excludes prior package artifacts",
            )
        ) == 0
        with zipfile.ZipFile(renamed_package) as archive:
            packaged_names = archive.namelist()
        assert not any("/deliverables/packages/" in name for name in packaged_names), packaged_names
        final_submission, final_submission_code = v51_orchestrator.validate_stage(run, "submission")
        assert final_submission_code == 0, final_submission
        final_g7 = harness.validate_gate(run, "G7", strict=True)
        assert final_g7["ok"], final_g7
    finally:
        shutil.rmtree(run, ignore_errors=True)


def main() -> int:
    static_checks()
    g7_decision_projection_regression()
    solver_status_regression()
    run_regression()
    print(
        "OK: CUMCM Adaptive Research Harness v5.1 static + isolated regression "
        "tests passed; no network/model/API calls made."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
