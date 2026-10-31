#!/usr/bin/env python3
"""Focused destructive-boundary regressions for run-scoped identifiers/paths."""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Callable
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
RUNS = ROOT / "runs"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import blind_review
import common
import math_engine
import verifier_lite


def _new_run(prefix: str) -> Path:
    RUNS.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix=prefix, dir=RUNS))
    stamp = "2026-09-04T14:00:00+08:00"
    common.write_json(
        run / "RUN_META.json",
        {
            "version": "5.1.0",
            "run_id": run.name,
            "contest": "path-safety-test",
            "problem": "filesystem containment",
            "phase": "rehearsal",
            "runtime": "codex",
            "created_at": stamp,
            "project_root": str(ROOT),
            "run_path": run.relative_to(ROOT).as_posix(),
            "input_policy": "offline fixture only",
            "notes": "focused path-safety regression",
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
    return run


def _expect_rejected(
    callable_obj: Callable[[], object],
    *,
    phrase: str,
    sentinel: Path | None = None,
) -> None:
    expected = sentinel.read_bytes() if sentinel is not None else None
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
        try:
            callable_obj()
        except SystemExit:
            pass
        else:
            raise AssertionError("unsafe filesystem target was accepted")
    if sentinel is not None:
        assert sentinel.is_file(), f"sentinel was deleted: {sentinel}"
        assert sentinel.read_bytes() == expected, f"sentinel was changed: {sentinel}"
    message = stderr.getvalue()
    assert phrase.lower() in message.lower(), (phrase, message)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _make_directory_link(target: Path, link: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError as exc:
        if os.name != "nt" or getattr(exc, "winerror", None) != 1314:
            raise
    # Directory junctions do not need the Windows symlink privilege and must
    # receive the same fail-closed treatment as symbolic links.
    completed = subprocess.run(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)


def _remove_directory_link(link: Path) -> None:
    if link.is_symlink():
        link.unlink()
    elif getattr(link, "is_junction", lambda: False)():
        link.rmdir()


def _blind_dot_segments() -> None:
    for review_id in (".", ".."):
        run = _new_run("_path_blind_dot_")
        try:
            protected = run / "reviews" / ("blind" if review_id == "." else "")
            protected.mkdir(parents=True, exist_ok=True)
            sentinel = protected / "KEEP.txt"
            sentinel.write_text("must survive rejected overwrite\n", encoding="utf-8")
            _expect_rejected(
                lambda: blind_review.cmd_export(
                    NS(run=str(run), review_id=review_id, overwrite=True, max_text_mb=1)
                ),
                phrase="review-id",
                sentinel=sentinel,
            )
            _expect_rejected(
                lambda: blind_review.cmd_import(
                    NS(
                        run=str(run),
                        review_id=review_id,
                        response=str(sentinel),
                        reviewer_kind="human",
                        session_id="path-safety",
                        fresh_context=True,
                        attested_by="Alice Chen",
                        note="",
                    )
                ),
                phrase="review-id",
            )
            _expect_rejected(
                lambda: blind_review.validate(run, review_id, strict=False),
                phrase="review-id",
            )
        finally:
            shutil.rmtree(run, ignore_errors=True)


def _blind_namespace_symlink() -> None:
    run = _new_run("_path_blind_link_")
    link = run / "reviews" / "blind"
    try:
        target = run / "decoy-blind-root"
        protected = target / "BR-001"
        protected.mkdir(parents=True)
        sentinel = protected / "KEEP.txt"
        sentinel.write_text("namespace link target must survive\n", encoding="utf-8")
        link.parent.mkdir(parents=True)
        _make_directory_link(target, link)
        _expect_rejected(
            lambda: blind_review.cmd_export(
                NS(run=str(run), review_id="BR-001", overwrite=True, max_text_mb=1)
            ),
            phrase="review-id",
            sentinel=sentinel,
        )
    finally:
        _remove_directory_link(link)
        shutil.rmtree(run, ignore_errors=True)


def _write_lite_config(run: Path, output_dir: Path) -> Path:
    mode = run / "modes" / "reference-verifier-lite"
    candidates = mode / "candidates"
    problem = run / "problem" / "VERIFIER_PROBLEM.md"
    candidates.mkdir(parents=True, exist_ok=True)
    problem.parent.mkdir(parents=True, exist_ok=True)
    problem.write_text("Choose the auditable candidate.\n", encoding="utf-8")
    (candidates / "A.txt").write_text("candidate A\n", encoding="utf-8")
    (candidates / "B.txt").write_text("candidate B\n", encoding="utf-8")
    config_path = mode / "config.json"
    common.write_json(
        config_path,
        {
            "session_id": "PATH_SAFETY",
            "problem_file": str(problem),
            "candidate_files": {
                "A": str(candidates / "A.txt"),
                "B": str(candidates / "B.txt"),
            },
            "criteria": [{"id": "audit", "description": "auditability", "weight": 1.0}],
            "seed": 0,
            "output_dir": str(output_dir),
        },
    )
    return config_path


def _lite_unsafe_outputs() -> None:
    # Cross-run output: even another valid run is outside the config's run.
    run = _new_run("_path_lite_owner_")
    other = _new_run("_path_lite_other_")
    try:
        output = other / "modes" / "reference-verifier-lite" / "session"
        output.mkdir(parents=True)
        sentinel = output / "KEEP.txt"
        sentinel.write_text("cross-run target must survive\n", encoding="utf-8")
        config = _write_lite_config(run, output)
        _expect_rejected(
            lambda: verifier_lite.cmd_init(NS(config=str(config), replace=True)),
            phrase="output_dir",
            sentinel=sentinel,
        )
    finally:
        shutil.rmtree(run, ignore_errors=True)
        shutil.rmtree(other, ignore_errors=True)

    # The project root is exercised with a deletion spy: a regression fails
    # loudly without ever placing the repository itself at risk.
    run = _new_run("_path_lite_project_root_")
    try:
        sentinel = run / "KEEP.txt"
        sentinel.write_text("project root attempt must be rejected\n", encoding="utf-8")
        config = _write_lite_config(run, ROOT)
        with patch.object(
            verifier_lite.shutil,
            "rmtree",
            side_effect=AssertionError("rmtree reached for project root"),
        ):
            _expect_rejected(
                lambda: verifier_lite.cmd_init(NS(config=str(config), replace=True)),
                phrase="output_dir",
                sentinel=sentinel,
            )
    finally:
        shutil.rmtree(run, ignore_errors=True)

    # The run root itself is never a disposable verifier session.
    run = _new_run("_path_lite_root_")
    try:
        sentinel = run / "KEEP.txt"
        sentinel.write_text("run root must survive\n", encoding="utf-8")
        config = _write_lite_config(run, run)
        _expect_rejected(
            lambda: verifier_lite.cmd_init(NS(config=str(config), replace=True)),
            phrase="output_dir",
            sentinel=sentinel,
        )
    finally:
        shutil.rmtree(run, ignore_errors=True)

    # A link beneath the allowed namespace must not redirect recursive delete.
    run = _new_run("_path_lite_link_owner_")
    other = _new_run("_path_lite_link_target_")
    link = run / "modes" / "reference-verifier-lite" / "session-link"
    try:
        target = other / "session-target"
        target.mkdir(parents=True)
        sentinel = target / "KEEP.txt"
        sentinel.write_text("linked target must survive\n", encoding="utf-8")
        link.parent.mkdir(parents=True, exist_ok=True)
        _make_directory_link(target, link)
        config = _write_lite_config(run, link)
        _expect_rejected(
            lambda: verifier_lite.cmd_init(NS(config=str(config), replace=True)),
            phrase="output_dir",
            sentinel=sentinel,
        )
    finally:
        _remove_directory_link(link)
        shutil.rmtree(run, ignore_errors=True)
        shutil.rmtree(other, ignore_errors=True)


def _lite_valid_output() -> None:
    run = _new_run("_path_lite_valid_")
    try:
        output = run / "modes" / "reference-verifier-lite" / "session"
        config = _write_lite_config(run, output)
        with contextlib.redirect_stdout(io.StringIO()):
            code = verifier_lite.cmd_init(NS(config=str(config), replace=False))
        assert code == 0
        assert (output / "STATE.json").is_file()
    finally:
        shutil.rmtree(run, ignore_errors=True)


def _math_dot_segments() -> None:
    for module_id in (".", ".."):
        run = _new_run("_path_math_dot_")
        try:
            (run / "research" / "modules").mkdir(parents=True)
            before = _snapshot(run)
            _expect_rejected(
                lambda: math_engine.create_module(
                    run,
                    module_id=module_id,
                    title="must not be written",
                    question_ids=["Q1"],
                    method_families=["test"],
                    overwrite=True,
                ),
                phrase="module id",
            )
            assert _snapshot(run) == before, f"module {module_id!r} wrote before rejection"
        finally:
            shutil.rmtree(run, ignore_errors=True)


def main() -> int:
    _blind_dot_segments()
    _blind_namespace_symlink()
    _math_dot_segments()
    _lite_unsafe_outputs()
    _lite_valid_output()
    print("Run-scoped path safety self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
