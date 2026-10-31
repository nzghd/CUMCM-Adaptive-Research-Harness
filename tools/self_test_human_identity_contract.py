#!/usr/bin/env python3
"""Regression checks for named-human and timezone lifecycle contracts."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import common
import math_engine
import paper
import paper_v5


def _expect_exit(callable_obj, phrase: str | None = None) -> None:
    try:
        callable_obj()
    except SystemExit as exc:
        if phrase is not None:
            assert phrase in str(exc), (phrase, str(exc))
    else:
        raise AssertionError(f"expected SystemExit containing {phrase!r}")


def _paper_plan(run: Path) -> None:
    (run / "paper" / "sections").mkdir(parents=True, exist_ok=True)
    (run / "paper" / "sections" / "010_body.md").write_text(
        "# Body\n\nReviewed body text.\n", encoding="utf-8"
    )
    common.write_json(
        run / "paper" / "PAPER_PLAN.json",
        {
            "schema_version": 1,
            "working_title": "Identity contract fixture",
            "narrative_thesis": "The fixture checks lifecycle identity contracts.",
            "created_at": common.now_iso(),
            "updated_at": common.now_iso(),
            "sections": [
                {
                    "id": "body",
                    "title": "Body",
                    "purpose": "Test identity validation",
                    "order": 10,
                    "file": "paper/sections/010_body.md",
                    "required": True,
                    "include_in_manuscript": True,
                    "claim_ids": [],
                    "figure_ids": [],
                    "table_ids": [],
                    "status": "final",
                    "writer": "Alice Chen",
                    "notes": "",
                }
            ],
        },
    )


def main() -> int:
    runs_root = ROOT / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="_identity_contract_", dir=runs_root))
    try:
        # Mathematical obligation: reject placeholder writes and detect manual edits.
        module_root = run / "research" / "modules" / "M1"
        module_root.mkdir(parents=True)
        math_engine.ensure_tables(module_root)
        common.write_csv(
            module_root / "MATHEMATICAL_OBLIGATIONS.csv",
            math_engine.OBLIGATION_FIELDS,
            [
                {
                    "obligation_id": "MATH-001",
                    "severity": "low",
                    "claim": "A boundary case is excluded.",
                    "claim_type": "scope",
                    "required_conditions": "The boundary is outside the model domain.",
                    "current_justification": "",
                    "symbolic_check": "",
                    "numerical_check": "",
                    "counterexample_test": "",
                    "proof_provenance": "",
                    "evidence_refs": "",
                    "status": "open",
                    "resolution": "",
                    "reviewed_by": "",
                    "reviewed_at": "",
                    "notes": "",
                }
            ],
        )
        math_engine.resolve_run_flexible = lambda _value: run
        base_resolve = dict(
            run="fixture",
            module="M1",
            obligation_id="MATH-001",
            status="not_applicable",
            resolution="Outside the declared domain.",
            proof_provenance=None,
            evidence=[],
            notes=None,
        )
        _expect_exit(
            lambda: math_engine.cmd_obligation_resolve(NS(**base_resolve, by="TEAM")),
        )
        math_engine.cmd_obligation_resolve(NS(**base_resolve, by="Alice Chen"))
        obligation_rows = common.read_csv(module_root / "MATHEMATICAL_OBLIGATIONS.csv")
        obligation_rows[0]["reviewed_by"] = "TODO"
        obligation_rows[0]["reviewed_at"] = "2026-09-04T10:00:00"
        common.write_csv(
            module_root / "MATHEMATICAL_OBLIGATIONS.csv",
            math_engine.OBLIGATION_FIELDS,
            obligation_rows,
        )
        math_result, _ = math_engine.lint_module(run, "M1", strict=True, persist=False)
        assert any("named reviewed_by" in item for item in math_result["errors"]), math_result
        assert any("timezone-aware" in item for item in math_result["errors"]), math_result

        # Paper extension commands reject placeholder verification/review identities.
        _paper_plan(run)
        paper_v5.resolve_run_flexible = lambda _value: run
        paper_v5.ensure(run)
        (run / "evidence").mkdir(exist_ok=True)
        common.write_csv(
            run / "evidence" / "CLAIM_EVIDENCE_LEDGER.csv",
            ["claim_id", "evidence_id"],
            [{"claim_id": "CLAIM-001", "evidence_id": "EVID-001"}],
        )
        fact_args = NS(
            run="fixture",
            fact_id="AF-001",
            question="Question one",
            purpose="Summarize the result",
            method="A deterministic fixture",
            result="The contract rejects placeholders",
            scope="This regression test",
            claim=["CLAIM-001"],
            evidence=["EVID-001"],
            limitations=[],
            frozen=True,
            verified_by="UNKNOWN",
            keyword=["contract"],
        )
        _expect_exit(lambda: paper_v5.cmd_fact_add(fact_args))
        _expect_exit(
            lambda: paper_v5.cmd_citation_add(
                NS(
                    run="fixture",
                    citation_id="CIT-001",
                    source_id="SRC-001",
                    claim_or_context="Test citation identity",
                    paper_location="body",
                    verified=True,
                    verified_by="TODO",
                    notes=None,
                )
            ),
        )
        _expect_exit(
            lambda: paper_v5.cmd_section_quality_set(
                NS(
                    run="fixture",
                    section="body",
                    status="final",
                    editor="TEAM",
                    purpose=None,
                    claim=None,
                    equation=None,
                    figure=None,
                    note=None,
                )
            ),
        )

        # Hand-edited extension rows must fail at read time as well.
        fact_payload = {
            "schema_version": 1,
            "keywords": ["contract"],
            "facts": [
                {
                    "fact_id": "AF-EDITED",
                    "question": "Question one",
                    "purpose": "Summarize",
                    "method": "Fixture",
                    "result": "Rejected",
                    "scope": "Test",
                    "claim_ids": ["CLAIM-001"],
                    "evidence_ids": ["EVID-001"],
                    "limitations": [],
                    "frozen": True,
                    "verified_by": "TEAM",
                    "created_at": "2026-09-04T10:00:00",
                }
            ],
        }
        fact_errors, _, usable, _ = paper_v5._audit_facts(run, fact_payload, strict=True)
        assert any("named verified_by" in item for item in fact_errors), fact_errors
        assert any("timezone-aware" in item for item in fact_errors), fact_errors
        assert not usable, usable

        common.write_csv(
            run / "paper" / "CITATION_REGISTER.csv",
            paper_v5.CITATION_FIELDS,
            [
                {
                    "citation_id": "CIT-EDITED",
                    "source_id": "SRC-MISSING",
                    "claim_or_context": "Edited citation",
                    "paper_location": "body",
                    "verified": "true",
                    "verified_by": "UNKNOWN",
                    "notes": "",
                }
            ],
        )
        section_rows = common.read_csv(run / "paper" / "SECTION_QUALITY_REGISTER.csv")
        section_rows[0]["status"] = "final"
        section_rows[0]["editor"] = "TEAM"
        common.write_csv(
            run / "paper" / "SECTION_QUALITY_REGISTER.csv",
            paper_v5.SECTION_FIELDS,
            section_rows,
        )
        paper_v5._paper_schema_errors = lambda _run: []
        extension_result = paper_v5.validate_paper_extension(run, strict=True)
        assert any("verified_by must name a human" in item for item in extension_result["errors"]), extension_result
        assert any("editor must name a human" in item for item in extension_result["errors"]), extension_result

        # Paper audit write and read contracts are independent of the command path.
        paper.resolve_run = lambda _value: run
        _expect_exit(
            lambda: paper.cmd_audit_set(
                NS(
                    run="fixture",
                    status="pending",
                    by="TEAM",
                    blocker=None,
                    warning=None,
                    note=None,
                )
            ),
            "具名人工",
        )
        common.write_json(
            run / "paper" / "PAPER_AUDIT.json",
            {
                "status": "passed",
                "checked_at": "2026-09-04T10:00:00",
                "checked_by": "TODO",
                "blockers": [],
                "warnings": [],
                "notes": "Manually edited audit fixture.",
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
        audit_result = paper.validate_paper_run(
            run, strict=False, require_final=False, require_audit=True
        )
        assert any("checked_by 必须为具名人工" in item for item in audit_result["errors"]), audit_result
        assert any("checked_at 必须是带显式时区" in item for item in audit_result["errors"]), audit_result

        print("Human identity lifecycle contract self-test passed.")
        return 0
    finally:
        shutil.rmtree(run, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
