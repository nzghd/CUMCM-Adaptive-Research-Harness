#!/usr/bin/env python3
"""Fast offline regressions for card identity and module-registry contracts."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
RUNS = ROOT / "runs"
PREFIX = "_SELFTEST_SELECTION_CONTRACT_"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import common  # noqa: E402
import harness  # noqa: E402
import math_engine  # noqa: E402
import schema_validate  # noqa: E402


def _new_run() -> Path:
    RUNS.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix=f"{PREFIX}{os.getpid()}_", dir=RUNS))
    if run.resolve().parent != RUNS.resolve():
        raise AssertionError(f"temporary run is not a direct child of runs/: {run}")
    stamp = "2026-09-04T14:00:00+08:00"
    common.write_json(
        run / "RUN_META.json",
        {
            "version": "5.1.0",
            "run_id": run.name,
            "contest": "selection-contract-test",
            "problem": "card identity and registry upsert",
            "phase": "rehearsal",
            "runtime": "codex",
            "created_at": stamp,
            "project_root": str(ROOT),
            "run_path": common.relative_to_root(run),
            "input_policy": "offline fixture only",
            "notes": "focused G2 selection contract regression",
        },
    )
    common.write_json(
        run / "STATUS.json",
        {
            "version": "5.1.0",
            "run_id": run.name,
            "created_at": stamp,
            "updated_at": stamp,
            "current_gate": "G0",
            "gates": {},
            "frozen": False,
            "frozen_at": None,
            "freeze_manifest": None,
        },
    )
    common.write_json(
        run / "ideas" / "IDEA_REGISTRY.json",
        {"next_id": 2, "ideas": []},
    )
    common.write_json(
        run / "research" / "MODULE_REGISTRY.json",
        {"next_id": 1, "modules": []},
    )
    assert common.resolve_run(run.name) == run.resolve()
    return run


def _safe_remove_fixture(path: Path, expected_parent: Path) -> None:
    if not path.exists():
        return
    resolved = path.resolve()
    if resolved.parent != expected_parent.resolve():
        raise AssertionError(f"refusing unsafe fixture cleanup: {resolved}")
    shutil.rmtree(resolved)


def _safe_remove_run(run: Path) -> None:
    if not run.exists():
        return
    resolved = run.resolve()
    if (
        resolved == RUNS.resolve()
        or resolved.parent != RUNS.resolve()
        or not resolved.name.startswith(PREFIX)
    ):
        raise AssertionError(f"refusing unsafe run cleanup: {resolved}")
    shutil.rmtree(resolved)


def _expect_rejected(action: Callable[[], object], phrase: str) -> None:
    stderr = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
        try:
            action()
        except SystemExit:
            pass
        else:
            raise AssertionError("invalid selection-contract fixture was accepted")
    message = stderr.getvalue()
    assert phrase.lower() in message.lower(), (phrase, message)


def _write_idea(namespace: Path, directory_id: str, payload_id: str) -> Path:
    idea_dir = namespace / directory_id
    idea_dir.mkdir(parents=True)
    common.write_json(
        idea_dir / "IDEA_CARD.json",
        {
            "schema_version": 1,
            "id": payload_id,
            "title": f"Synthetic {directory_id}",
            "summary": "Offline identity-contract fixture",
            "status": "proposed",
        },
    )
    return idea_dir


def _test_card_identities(run: Path) -> None:
    research_ideas = run / "research" / "ideas"
    compatibility_ideas = run / "ideas"
    base = _write_idea(research_ideas, "IDEA_BASE", "IDEA_BASE")
    cards = common.load_idea_cards(run)
    assert [(path.parent.name, card["id"]) for path, card in cards] == [
        ("IDEA_BASE", "IDEA_BASE")
    ]

    mismatch = _write_idea(research_ideas, "IDEA_DIRECTORY", "IDEA_PAYLOAD")
    _expect_rejected(lambda: common.load_idea_cards(run), "父目录名")
    _safe_remove_fixture(mismatch, research_ideas)

    first_duplicate = _write_idea(research_ideas, "IDEA_DUP", "IDEA_DUP")
    second_duplicate = _write_idea(
        compatibility_ideas, "IDEA_DUP", "IDEA_DUP"
    )
    _expect_rejected(lambda: common.load_idea_cards(run), "全局唯一")
    _safe_remove_fixture(first_duplicate, research_ideas)
    _safe_remove_fixture(second_duplicate, compatibility_ideas)

    module_root = run / "research" / "modules"
    mismatch_module = module_root / "MOD_DIRECTORY"
    mismatch_module.mkdir(parents=True)
    common.write_json(
        mismatch_module / "MODULE_CARD.json",
        {"id": "MOD_PAYLOAD", "status": "planned"},
    )
    _expect_rejected(lambda: common.load_module_cards(run), "父目录名")
    _safe_remove_fixture(mismatch_module, module_root)

    paired_module = module_root / "MOD_PAIR"
    paired_module.mkdir(parents=True)
    common.write_json(
        paired_module / "MODULE_CARD.json",
        {"id": "MOD_PAIR", "status": "planned"},
    )
    common.write_json(
        paired_module / "MODULE_SPEC.json",
        {"module_id": "MOD_WRONG", "status": "draft"},
    )
    _expect_rejected(lambda: common.load_module_cards(run), "父目录名")
    common.write_json(
        paired_module / "MODULE_SPEC.json",
        {"module_id": "MOD_PAIR", "status": "draft"},
    )
    modules = common.load_module_cards(run)
    assert len(modules) == 1, modules
    assert modules[0][0].name == "MODULE_CARD.json", modules
    assert modules[0][1]["id"] == "MOD_PAIR", modules

    assert base.is_dir()


def _test_math_engine_registry_upsert(run: Path) -> None:
    registry_path = run / "research" / "MODULE_REGISTRY.json"
    unrelated = {
        "id": "MOD_001",
        "path": "research/modules/MOD_001/MODULE_CARD.json",
        "status": "planned",
        "created_at": "2026-09-04T13:00:00+08:00",
        "purpose": "must survive upsert unchanged",
        "custom": {"preserve": True},
    }
    common.write_json(
        registry_path,
        {"schema_version": 1, "next_id": 2, "modules": [unrelated]},
    )

    module = math_engine.create_module(
        run,
        module_id="MOD_007",
        title="First title",
        question_ids=["Q1"],
        method_families=["optimization"],
        overwrite=False,
    )
    assert module == run.resolve() / "research" / "modules" / "MOD_007"
    card = json.loads((module / "MODULE_CARD.json").read_text(encoding="utf-8"))
    spec = json.loads((module / "MODULE_SPEC.json").read_text(encoding="utf-8"))
    assert card["id"] == module.name
    assert isinstance(spec.get("created_at"), str) and spec["created_at"]
    assert not schema_validate.validate_json_file(
        module / "MODULE_CARD.json", ROOT / "schemas" / "module_card.schema.json"
    )

    registry = common.read_json(registry_path, expect=dict)
    assert registry["next_id"] == 8, registry
    assert registry["modules"][0] == unrelated, registry
    records = [row for row in registry["modules"] if row["id"] == "MOD_007"]
    assert len(records) == 1, registry
    record = records[0]
    assert {
        "id": record["id"],
        "path": record["path"],
        "status": record["status"],
        "created_at": record["created_at"],
    } == {
        "id": "MOD_007",
        "path": "research/modules/MOD_007/MODULE_CARD.json",
        "status": "deepening",
        "created_at": spec["created_at"],
    }

    math_engine.create_module(
        run,
        module_id="MOD_007",
        title="Updated title",
        question_ids=["Q1", "Q2"],
        method_families=["simulation"],
        overwrite=True,
    )
    registry = common.read_json(registry_path, expect=dict)
    assert registry["modules"][0] == unrelated, registry
    records = [row for row in registry["modules"] if row["id"] == "MOD_007"]
    assert len(records) == 1, registry
    assert records[0]["title"] == "Updated title", records[0]
    assert registry["next_id"] == 8, registry

    valid_registry = registry_path.read_bytes()
    duplicate_registry = common.read_json(registry_path, expect=dict)
    duplicate_registry["modules"].append(dict(duplicate_registry["modules"][0]))
    common.write_json(registry_path, duplicate_registry)
    rejected_target = run / "research" / "modules" / "MOD_009"
    _expect_rejected(
        lambda: math_engine.create_module(
            run,
            module_id="MOD_009",
            title="Must not be written",
            question_ids=[],
            method_families=[],
            overwrite=False,
        ),
        "重复 id",
    )
    assert not rejected_target.exists()

    common.write_json(
        registry_path,
        {"next_id": 8, "modules": [unrelated, "not-an-object"]},
    )
    _expect_rejected(
        lambda: math_engine.create_module(
            run,
            module_id="MOD_009",
            title="Must not be written",
            question_ids=[],
            method_families=[],
            overwrite=False,
        ),
        "必须是对象",
    )
    assert not rejected_target.exists()

    registry_path.write_bytes(valid_registry)
    registry_path.write_text("[]\n", encoding="utf-8")
    _expect_rejected(
        lambda: math_engine.create_module(
            run,
            module_id="MOD_009",
            title="Must not be written",
            question_ids=[],
            method_families=[],
            overwrite=False,
        ),
        "顶层类型",
    )
    assert not rejected_target.exists()
    registry_path.write_bytes(valid_registry)


def _canonical_selection_registries(
    run: Path,
    ideas: list[tuple[Path, dict[str, object]]],
    modules: list[tuple[Path, dict[str, object]]],
) -> None:
    stamp = "2026-09-04T14:00:00+08:00"
    common.write_json(
        run / "ideas" / "IDEA_REGISTRY.json",
        {
            "next_id": 1,
            "ideas": [
                {
                    "id": payload["id"],
                    "path": path.resolve().relative_to(run.resolve()).as_posix(),
                    "status": str(payload.get("status", "proposed")),
                    "created_at": stamp,
                }
                for path, payload in ideas
            ],
        },
    )
    common.write_json(
        run / "research" / "MODULE_REGISTRY.json",
        {
            "next_id": 8,
            "modules": [
                {
                    "id": payload["id"],
                    "path": path.resolve().relative_to(run.resolve()).as_posix(),
                    "status": str(payload.get("status", "planned")),
                    "created_at": stamp,
                }
                for path, payload in modules
            ],
        },
    )


def _test_harness_selection_identity_audit(run: Path) -> None:
    ideas = common.load_idea_cards(run)
    modules = common.load_module_cards(run)
    _canonical_selection_registries(run, ideas, modules)

    clean = harness._selection_identity_audit(run, ideas, modules)
    assert clean["passed"], clean
    expected_inputs = {
        "ideas/IDEA_REGISTRY.json",
        "research/MODULE_REGISTRY.json",
        *(
            path.resolve().relative_to(run.resolve()).as_posix()
            for path, _ in ideas + modules
        ),
    }
    assert expected_inputs.issubset(set(clean["input_paths"])), clean

    idea_registry_path = run / "ideas" / "IDEA_REGISTRY.json"
    idea_registry = common.read_json(idea_registry_path, expect=dict)
    idea_registry["ideas"] = []
    common.write_json(idea_registry_path, idea_registry)
    missing = harness._selection_identity_audit(run, ideas, modules)
    assert not missing["passed"], missing
    assert any(
        "Idea registry/card mismatch" in message and "missing=" in message
        for message in missing["errors"]
    ), missing
    _canonical_selection_registries(run, ideas, modules)

    module_root = run / "research" / "modules"
    extra_dir = module_root / "MOD_AUDIT_EXTRA"
    extra_dir.mkdir(parents=True)
    extra_card = extra_dir / "MODULE_CARD.json"
    common.write_json(extra_card, {"id": "MOD_AUDIT_EXTRA", "status": "planned"})
    module_registry_path = run / "research" / "MODULE_REGISTRY.json"
    module_registry = common.read_json(module_registry_path, expect=dict)
    module_registry["modules"].append(
        {
            "id": "MOD_AUDIT_EXTRA",
            "path": "research/modules/MOD_AUDIT_EXTRA/MODULE_CARD.json",
            "status": "planned",
            "created_at": "2026-09-04T14:00:00+08:00",
        }
    )
    common.write_json(module_registry_path, module_registry)
    extra = harness._selection_identity_audit(run, ideas, modules)
    assert not extra["passed"], extra
    assert any(
        "Module registry/card mismatch" in message and "extra=" in message
        for message in extra["errors"]
    ), extra
    _safe_remove_fixture(extra_dir, module_root)
    _canonical_selection_registries(run, ideas, modules)

    module_registry = common.read_json(module_registry_path, expect=dict)
    by_id = {row["id"]: row for row in module_registry["modules"]}
    by_id["MOD_PAIR"]["path"] = by_id["MOD_007"]["path"]
    common.write_json(module_registry_path, module_registry)
    wrong_path = harness._selection_identity_audit(run, ideas, modules)
    assert not wrong_path["passed"], wrong_path
    assert any(
        "Module registry id/path directory mismatch" in message
        for message in wrong_path["errors"]
    ), wrong_path
    _canonical_selection_registries(run, ideas, modules)

    common.write_json(
        run / "selection" / "FOCUS_SET.json",
        {
            "selected_ideas": ["IDEA_BASE"],
            "selected_modules": ["MOD_UNKNOWN"],
            "selected_by": "Selection Contract Reviewer",
            "selected_at": "2026-09-04T14:00:00+08:00",
            "evidence": [],
            "rationale": "Exercise unknown selected module rejection.",
            "rejected_or_deferred": [],
        },
    )
    g2 = harness.validate_gate(run, "G2", strict=False)
    assert any("FOCUS_SET 含未知 Module" in message for message in g2["errors"]), g2


def main() -> int:
    run: Path | None = None
    try:
        run = _new_run()
        _test_card_identities(run)
        _test_math_engine_registry_upsert(run)
        _test_harness_selection_identity_audit(run)
        print("selection identity and module registry contract: PASS")
        return 0
    finally:
        if run is not None:
            _safe_remove_run(run)


if __name__ == "__main__":
    raise SystemExit(main())
