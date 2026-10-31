#!/usr/bin/env python3
"""Offline regression tests for the experiment runner process boundary."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import run_experiments  # noqa: E402


def _experiment(experiment_id: str, script: Path, env: dict[str, str] | None = None) -> dict:
    return {
        "id": experiment_id,
        "module_id": "MOD_ENV_BOUNDARY",
        "description": "offline child-process environment regression",
        "command": [sys.executable, str(script)],
        "cwd": ".",
        "seeds": [7],
        "timeout_sec": 30,
        "expected_artifacts": [
            f"experiments/runs/{experiment_id}/seed_7/environment.json"
        ],
        "env": env or {},
    }


def _args(manifest_path: Path, *, dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        manifest_path=str(manifest_path.resolve()),
        manifest_sha256="0" * 64,
        dry_run=dry_run,
    )


def _expect_rejected(
    run: Path,
    script: Path,
    manifest_path: Path,
    experiment_id: str,
    env: dict[str, str],
) -> None:
    error = io.StringIO()
    with contextlib.redirect_stderr(error):
        try:
            run_experiments.execute_one(
                run,
                _experiment(experiment_id, script, env),
                7,
                _args(manifest_path, dry_run=True),
            )
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError(f"unsafe manifest env was accepted: {sorted(env)}")
    assert "错误：" in error.getvalue()


def main() -> int:
    runs_root = (ROOT / "runs").resolve()
    run = (runs_root / f"_SELFTEST_EXPERIMENT_ENV_{uuid.uuid4().hex[:10]}").resolve()
    if run.parent != runs_root or not run.name.startswith("_SELFTEST_EXPERIMENT_ENV_"):
        raise SystemExit(f"unsafe self-test fixture path: {run}")
    try:
        synthetic_parent = {
            "PATH": os.pathsep.join([".", str(Path(sys.executable).resolve().parent), ""]),
            "LANG": "C.UTF-8",
            "LC_NUMERIC": "C",
            "OPENAI_API_KEY": "not-in-allowlist",
            "BENIGN_PARENT_SHOULD_NOT_LEAK": "not-in-allowlist",
        }
        minimal = run_experiments._minimal_parent_environment(synthetic_parent)
        assert minimal["PATH"] == str(Path(sys.executable).resolve().parent)
        assert minimal["LANG"] == "C.UTF-8"
        assert minimal["LC_NUMERIC"] == "C"
        assert "OPENAI_API_KEY" not in minimal
        assert "BENIGN_PARENT_SHOULD_NOT_LEAK" not in minimal

        (run / "code").mkdir(parents=True)
        script = run / "code" / "capture_environment.py"
        script.write_text(
            "from __future__ import annotations\n"
            "import json, os\n"
            "from pathlib import Path\n"
            "names = [\n"
            "    'OPENAI_API_KEY', 'DEEPSEEK_API_KEY', 'AWS_SECRET_ACCESS_KEY',\n"
            "    'BENIGN_PARENT_SHOULD_NOT_LEAK', 'PATH', 'LANG',\n"
            "    'EXPERIMENT_LABEL', 'SCIENTIFIC_PARAMETER_KEY',\n"
            "    'CUMCM_RUN_DIR', 'CUMCM_EXPERIMENT_ID',\n"
            "    'CUMCM_EXPERIMENT_OUTPUT_DIR', 'CUMCM_EXPERIMENT_RECORD_DIR',\n"
            "    'CUMCM_ATTEMPT_ID', 'CUMCM_SEED',\n"
            "]\n"
            "payload = {name: os.environ.get(name) for name in names}\n"
            "out = Path(os.environ['CUMCM_EXPERIMENT_OUTPUT_DIR'])\n"
            "out.mkdir(parents=True, exist_ok=True)\n"
            "(out / 'environment.json').write_text(\n"
            "    json.dumps(payload, sort_keys=True) + '\\n', encoding='utf-8'\n"
            ")\n",
            encoding="utf-8",
        )
        manifest_path = run / "experiments" / "environment_manifest.json"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text("{}\n", encoding="utf-8")

        parent_values = {
            "OPENAI_API_KEY": "must-not-reach-child-openai",
            "DEEPSEEK_API_KEY": "must-not-reach-child-deepseek",
            "AWS_SECRET_ACCESS_KEY": "must-not-reach-child-aws",
            "BENIGN_PARENT_SHOULD_NOT_LEAK": "allowlist-not-copy",
        }
        allowed_env = {
            "EXPERIMENT_LABEL": "label-{experiment_id}",
            "SCIENTIFIC_PARAMETER_KEY": "sort-index",
        }
        with patch.dict(os.environ, parent_values, clear=False):
            result = run_experiments.execute_one(
                run,
                _experiment("EXP_ENV_SAFE", script, allowed_env),
                7,
                _args(manifest_path),
            )
        assert result["status"] == "passed", result
        captured = json.loads(
            (
                run
                / "experiments"
                / "runs"
                / "EXP_ENV_SAFE"
                / "seed_7"
                / "environment.json"
            ).read_text(encoding="utf-8")
        )
        for name in parent_values:
            assert captured[name] is None, f"parent environment leaked: {name}"
        assert captured["PATH"], "PATH must remain available for normal executables"
        assert captured["EXPERIMENT_LABEL"] == "label-EXP_ENV_SAFE"
        assert captured["SCIENTIFIC_PARAMETER_KEY"] == "sort-index"
        assert captured["CUMCM_RUN_DIR"] == str(run)
        assert captured["CUMCM_EXPERIMENT_ID"] == "EXP_ENV_SAFE"
        assert captured["CUMCM_SEED"] == "7"
        assert result["environment_policy"] == "minimal-allowlist-v1"
        assert result["network_isolated"] is False
        assert result["filesystem_isolated"] is False
        assert result["cwd_confined_to_run"] is True
        assert result["executable"]["resolved_path"] == str(Path(sys.executable).resolve())
        assert result["executed_command"][0] == str(Path(sys.executable).resolve())
        assert result["runner_environment_keys"] == [
            "CUMCM_ATTEMPT_ID",
            "CUMCM_EXPERIMENT_ID",
            "CUMCM_EXPERIMENT_OUTPUT_DIR",
            "CUMCM_EXPERIMENT_RECORD_DIR",
            "CUMCM_RUN_DIR",
            "CUMCM_SEED",
        ]

        resolved_command, executable = run_experiments._resolve_executable(
            [Path(sys.executable).name, "--version"],
            cwd=run,
            run=run,
            environment={
                "PATH": str(Path(sys.executable).resolve().parent),
                "PATHEXT": os.environ.get("PATHEXT", ".EXE"),
            },
        )
        assert resolved_command[0] == str(Path(sys.executable).resolve())
        assert executable["resolution"] == "sanitized-PATH"

        for index, unsafe in enumerate(
            [
                {"OPENAI_API_KEY": "x"},
                {"MY_SERVICE_TOKEN": "x"},
                {"AWS_SHARED_CREDENTIALS_FILE": "credentials"},
                {"CUMCM_RUN_DIR": "spoof"},
                {"PATH": "spoof"},
                {"PYTHONPATH": "spoof"},
                {"alpha": "1", "ALPHA": "2"},
            ],
            start=1,
        ):
            _expect_rejected(
                run,
                script,
                manifest_path,
                f"EXP_ENV_REJECT_{index}",
                unsafe,
            )

        escape = _experiment("EXP_EXEC_ESCAPE", script)
        escape["command"] = ["..\\outside\\tool.exe"]
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            try:
                run_experiments.execute_one(
                    run,
                    escape,
                    7,
                    _args(manifest_path, dry_run=True),
                )
            except SystemExit as exc:
                assert exc.code == 2
            else:
                raise AssertionError("relative executable path escaped the run directory")
        assert "离开当前 run" in error.getvalue()

        print("OK: experiment child environment boundary regression passed (offline).")
        return 0
    finally:
        # Recursive cleanup is limited to the uniquely named fixture directly below runs/.
        if run.parent != runs_root or not run.name.startswith("_SELFTEST_EXPERIMENT_ENV_"):
            raise RuntimeError(f"refusing unsafe cleanup target: {run}")
        if run.exists():
            shutil.rmtree(run)


if __name__ == "__main__":
    raise SystemExit(main())
