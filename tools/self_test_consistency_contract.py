#!/usr/bin/env python3
"""Focused offline regression checks for the consistency fail-closed contract."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import consistency_matrix as subject


REVIEWED_AT = "2026-09-02T12:00:00+08:00"


def _row(**changes: str) -> dict[str, str]:
    row = {
        "check_id": "CK-001",
        "dimension": "duration_replay",
        "scope": "focused regression",
        "expected": "the recorded duration replays",
        "observed": "the recorded duration replayed",
        "status": "NOT_APPLICABLE",
        "tolerance": "",
        "critical": "false",
        "evidence_ref": "",
        "command": "",
        "reviewed_by": "Alice Chen",
        "reviewed_at": REVIEWED_AT,
        "waiver_reason": "The source fixture contains no duration field.",
        "notes": "",
    }
    row.update(changes)
    return row


def _derive(
    run: Path,
    row: dict[str, str],
    *,
    allow_not_applicable: bool,
) -> tuple[dict, int]:
    (run / "consistency").mkdir(parents=True, exist_ok=True)
    subject.write_csv(
        run / "consistency" / "CONSISTENCY_MATRIX.csv",
        subject.FIELDS,
        [row],
    )
    policy = (
        ["duration_replay"],
        ["duration_replay"],
        {"PASS", "PASS_WITH_TOLERANCE", "FAIL", "NOT_TESTED", "NOT_APPLICABLE"},
        {"duration_replay": allow_not_applicable},
    )
    with (
        patch.object(subject, "_dimension_policy", return_value=policy),
        patch.object(subject, "_evidence_universe", return_value=(set(), set())),
    ):
        return subject.derive(run, strict=True, persist=False)


def _assert_failed(result: dict, code: int, phrase: str) -> None:
    assert code != 0 and not result["passed"], result
    messages = "\n".join(result["errors"])
    assert phrase.lower() in messages.lower(), messages


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cumcm-consistency-contract-") as raw:
        run = Path(raw)

        result, code = _derive(run, _row(), allow_not_applicable=False)
        _assert_failed(result, code, "does not explicitly allow NOT_APPLICABLE")

        result, code = _derive(run, _row(), allow_not_applicable=True)
        assert code == 0 and result["passed"], result
        assert result["not_applicable_waivers"] == [
            {
                "check_id": "CK-001",
                "dimension": "duration_replay",
                "waiver_reason": "The source fixture contains no duration field.",
                "reviewed_by": "Alice Chen",
                "reviewed_at": REVIEWED_AT,
            }
        ]

        for changes, phrase in (
            ({"reviewed_by": "TEAM"}, "requires a named reviewer"),
            ({"reviewed_at": "2026-09-02T12:00:00"}, "timezone-aware"),
            ({"waiver_reason": "too short"}, "waiver_reason"),
        ):
            result, code = _derive(
                run, _row(**changes), allow_not_applicable=True
            )
            _assert_failed(result, code, phrase)

        result_path = run / "results" / "replay.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text('{"duration_error": 0.0}\n', encoding="utf-8")
        passing = _row(
            status="PASS",
            critical="true",
            evidence_ref="results/replay.json",
            waiver_reason="",
        )
        result, code = _derive(run, passing, allow_not_applicable=False)
        assert code == 0 and result["passed"], result

        for evidence_ref, phrase in (
            ("consistency/CONSISTENCY_MATRIX.csv", "management/self-referential"),
            ("STATUS.json", "management/self-referential"),
        ):
            if evidence_ref == "STATUS.json":
                (run / evidence_ref).write_text("{}\n", encoding="utf-8")
            result, code = _derive(
                run,
                _row(
                    status="PASS",
                    critical="true",
                    evidence_ref=evidence_ref,
                    waiver_reason="",
                ),
                allow_not_applicable=False,
            )
            _assert_failed(result, code, phrase)

    print("Consistency contract self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
