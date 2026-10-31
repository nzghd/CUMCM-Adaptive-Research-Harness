#!/usr/bin/env python3
"""Focused offline regression for the G3 experiment receipt contract."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import common  # noqa: E402
import harness  # noqa: E402
import run_experiments  # noqa: E402


EXPERIMENT_ID = "EXP_RECEIPT_CONTRACT"
MODULE_ID = "MOD_RECEIPT_CONTRACT"
SEED = 17


def _write_json(path: Path, payload: Any) -> None:
    common.write_json(path, payload)


def _load_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    assert isinstance(payload, dict), path
    return payload


def _assert_audit_passes(run: Path) -> dict[str, Any]:
    result = harness._experiment_gate_audit(
        run,
        only_experiment_ids={EXPERIMENT_ID},
    )
    assert result["errors"] == [], "\n".join(result["errors"])
    assert result["valid_receipt_count"] == 1, result
    assert result["valid_metric_count"] == 1, result
    assert result["valid_experiment_ids"] == [EXPERIMENT_ID], result
    return result


def _assert_tamper_rejected(
    run: Path,
    request_path: Path,
    receipt_path: Path,
    manifest_path: Path,
    baseline_bytes: dict[Path, bytes],
    label: str,
    mutate: Callable[
        [dict[str, Any], dict[str, Any], dict[str, Any]], None
    ],
) -> None:
    """Mutate all bound surfaces, assert fail-closed, then restore exact bytes."""
    try:
        request = _load_object(request_path)
        receipt = _load_object(receipt_path)
        manifest = _load_object(manifest_path)
        mutate(request, receipt, manifest)

        # A manifest mutation is a stronger attack when the attacker also
        # updates the visible receipt hash.  Do that before serializing the
        # request/run pair so the case cannot fail merely on a stale digest.
        before_manifest = baseline_bytes[manifest_path]
        encoded_manifest = (
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        manifest_path.write_bytes(encoded_manifest)
        if encoded_manifest != before_manifest:
            digest = hashlib.sha256(encoded_manifest).hexdigest()
            request["manifest_sha256"] = digest
            receipt["manifest_sha256"] = digest

        _write_json(request_path, request)
        _write_json(receipt_path, receipt)
        result = harness._experiment_gate_audit(
            run,
            only_experiment_ids={EXPERIMENT_ID},
        )
        assert result["errors"], f"tamper was accepted: {label}"
        assert result["valid_receipt_count"] == 0, (
            f"tampered receipt remained valid: {label}\n"
            + "\n".join(result["errors"])
        )
    finally:
        for path, content in baseline_bytes.items():
            path.write_bytes(content)
        for path, content in baseline_bytes.items():
            assert path.read_bytes() == content, f"failed to restore {path}"


def _delete_environment_policy(
    request: dict[str, Any], receipt: dict[str, Any], _manifest: dict[str, Any]
) -> None:
    request.pop("environment_policy", None)
    receipt.pop("environment_policy", None)


def _claim_network_isolation(
    request: dict[str, Any], receipt: dict[str, Any], _manifest: dict[str, Any]
) -> None:
    request["network_isolated"] = True
    receipt["network_isolated"] = True


def _inject_inherited_secret(
    request: dict[str, Any], receipt: dict[str, Any], _manifest: dict[str, Any]
) -> None:
    keys = sorted(set(request["inherited_environment_keys"]) | {"OPENAI_API_KEY"})
    request["inherited_environment_keys"] = keys
    receipt["inherited_environment_keys"] = keys


def _drop_runner_attempt_key(
    request: dict[str, Any], receipt: dict[str, Any], _manifest: dict[str, Any]
) -> None:
    keys = [
        key
        for key in request["runner_environment_keys"]
        if key != "CUMCM_ATTEMPT_ID"
    ]
    request["runner_environment_keys"] = keys
    receipt["runner_environment_keys"] = keys


def _jointly_tamper_resolved_executable(
    request: dict[str, Any],
    receipt: dict[str, Any],
    _manifest: dict[str, Any],
) -> None:
    # Use an existing in-run file and synchronize all superficial executable
    # metadata.  The contract must still bind it back to command[0].
    forged_path = request["manifest_path"]
    forged_command = [forged_path, *request["executed_command"][1:]]
    forged_metadata = dict(request["executable"])
    forged_metadata.update(
        {
            "resolved_path": forged_path,
            "resolution": "absolute",
            "within_run": True,
            "within_project": True,
        }
    )
    request["executed_command"] = forged_command
    receipt["executed_command"] = list(forged_command)
    request["executable"] = forged_metadata
    receipt["executable"] = dict(forged_metadata)


def _inject_manifest_secret_and_sync_surface(
    request: dict[str, Any],
    receipt: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    experiments = manifest["experiments"]
    assert isinstance(experiments, list) and len(experiments) == 1
    experiment = experiments[0]
    assert isinstance(experiment, dict)
    raw_env = experiment.setdefault("env", {})
    assert isinstance(raw_env, dict)
    raw_env["OPENAI_API_KEY"] = "forged-secret"
    keys = sorted(str(key) for key in raw_env)
    request["environment_override_keys"] = keys
    receipt["environment_override_keys"] = keys


def main() -> int:
    runs_root = (ROOT / "runs").resolve()
    run = (runs_root / f"_SELFTEST_EXPERIMENT_RECEIPT_{os.getpid()}").resolve()
    expected_prefix = "_SELFTEST_EXPERIMENT_RECEIPT_"
    if run.parent != runs_root or not run.name.startswith(expected_prefix):
        raise SystemExit(f"unsafe self-test fixture path: {run}")
    if run.exists():
        raise SystemExit(f"refusing to reuse existing self-test fixture: {run}")

    created = False
    try:
        (run / "experiments").mkdir(parents=True)
        created = True
        config = common.load_config()
        _write_json(
            run / "RUN_META.json",
            {
                "version": config["version"],
                "run_id": run.name,
                "contest": "synthetic-offline-regression",
                "problem": "G3 experiment receipt contract",
                "phase": "rehearsal",
                "runtime": "codex",
                "created_at": common.now_iso(),
                "project_root": str(ROOT.resolve()),
                "run_path": run.relative_to(ROOT).as_posix(),
                "input_policy": "synthetic self-test only",
                "notes": "No network, model, or paid API call.",
            },
        )
        _write_json(run / "STATUS.json", harness._initial_status(config, run.name))

        metrics_path = run / "experiments" / "receipt_contract_metrics.json"
        metrics_relative = metrics_path.relative_to(run).as_posix()
        experiment = {
            "id": EXPERIMENT_ID,
            "module_id": MODULE_ID,
            "description": "deterministic G3 receipt contract fixture",
            "command": [sys.executable, "-c", "pass"],
            "cwd": ".",
            "timeout_sec": 5,
            "seeds": [SEED],
            "expected_artifacts": [metrics_relative],
            "metrics_file": metrics_relative,
            "env": {"SCIENTIFIC_PARAMETER": "alpha"},
        }
        manifest_path = run / "experiments" / "EXPERIMENT_MANIFEST.json"
        _write_json(
            manifest_path,
            {"run_id": run.name, "experiments": [experiment]},
        )

        def emit_metrics(command: list[str], *_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            _write_json(
                metrics_path,
                {
                    "metrics": [
                        {
                            "split": "synthetic",
                            "metric": "objective",
                            "value": 1.25,
                            "unit": "dimensionless",
                            "seed": str(SEED),
                        }
                    ]
                },
            )
            return subprocess.CompletedProcess(command, 0, "objective=1.25\n", "")

        args = argparse.Namespace(
            manifest_path=str(manifest_path.resolve()),
            manifest_sha256=common.sha256_file(manifest_path),
            dry_run=False,
        )
        with patch.object(
            run_experiments.subprocess,
            "run",
            side_effect=emit_metrics,
        ):
            runner_result = run_experiments.execute_one(
                run,
                experiment,
                SEED,
                args,
            )
        assert runner_result["status"] == "passed", runner_result
        assert runner_result["executed_command"][0] == str(Path(sys.executable).resolve())
        assert runner_result["environment_policy"] == run_experiments.ENVIRONMENT_POLICY

        record_dir = run / runner_result["record_dir"]
        request_path = record_dir / "request.json"
        receipt_path = record_dir / "run.json"
        assert request_path.is_file() and receipt_path.is_file()
        _assert_audit_passes(run)

        baseline_bytes = {
            request_path: request_path.read_bytes(),
            receipt_path: receipt_path.read_bytes(),
            manifest_path: manifest_path.read_bytes(),
        }
        cases = (
            ("missing environment_policy", _delete_environment_policy),
            ("network_isolated=true", _claim_network_isolation),
            ("inherited OPENAI_API_KEY", _inject_inherited_secret),
            ("missing CUMCM_ATTEMPT_ID", _drop_runner_attempt_key),
            ("joint executed_command/executable path forgery", _jointly_tamper_resolved_executable),
            ("manifest secret with synchronized surface fields", _inject_manifest_secret_and_sync_surface),
        )
        for label, mutate in cases:
            _assert_tamper_rejected(
                run,
                request_path,
                receipt_path,
                manifest_path,
                baseline_bytes,
                label,
                mutate,
            )

        _assert_audit_passes(run)
        print("OK: G3 experiment receipt contract rejected 6 synchronized tamper cases.")
        return 0
    finally:
        # Recursive cleanup is confined to this process-unique direct child of runs/.
        if run.parent != runs_root or not run.name.startswith(expected_prefix):
            raise RuntimeError(f"refusing unsafe cleanup target: {run}")
        if created and run.exists():
            shutil.rmtree(run)
        if created:
            assert not run.exists(), f"self-test fixture was not cleaned: {run}"


if __name__ == "__main__":
    raise SystemExit(main())
