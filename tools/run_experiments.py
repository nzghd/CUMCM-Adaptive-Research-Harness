#!/usr/bin/env python3
"""Execute reproducible, no-shell experiment manifests and capture evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from common import (
    ROOT,
    active_run_id,
    append_csv,
    die,
    now_iso,
    relative_to_run,
    resolve_run,
    sha256_file,
    write_json,
)

LOG_FIELDS = ["experiment_id", "timestamp", "module_id", "command", "seed", "status", "output_path", "duration_sec", "notes"]
METRIC_FIELDS = ["experiment_id", "module_id", "split", "metric", "value", "unit", "seed", "evidence_path", "notes"]
SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
GLOB_CHARS = frozenset("*?[")
UNSPECIFIED_VALUES = {"", "unknown", "unspecified", "n/a", "na", "none", "null"}
RUNNER_RECORD_NAMES = {"request.json", "run.json", "stdout.txt", "stderr.txt"}
ENVIRONMENT_POLICY = "minimal-allowlist-v1"
INHERITED_ENV_NAMES = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "SYSTEMDRIVE",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
)
PROTECTED_ENV_NAMES = {
    # Executable lookup and dynamic-language injection controls must come from
    # the sanitized parent environment, never an experiment manifest.
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "SYSTEMDRIVE",
    "COMSPEC",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONINSPECT",
    "NODE_OPTIONS",
    "RUBYOPT",
    "PERL5OPT",
    "JAVA_TOOL_OPTIONS",
    "_JAVA_OPTIONS",
    "BASH_ENV",
    "ENV",
}
SENSITIVE_ENV_NAMES = {
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONFIG_FILE",
    "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
    "DOCKER_AUTH_CONFIG",
    "DOCKER_CONFIG",
    "GIT_ASKPASS",
    "GH_CONFIG_DIR",
    "KUBECONFIG",
    "NETRC",
    "NPM_CONFIG_USERCONFIG",
    "SSH_ASKPASS",
    "SSH_AUTH_SOCK",
}
SENSITIVE_ENV_RE = re.compile(
    r"(?:^|_)(?:"
    r"SECRET(?:S)?|TOKEN(?:S)?|PASSWORD|PASSWD|CREDENTIAL(?:S)?|"
    r"(?:API|ACCESS|PRIVATE|SECRET|SIGNING|ENCRYPTION|LICENSE)_?KEY|"
    r"DATABASE_URL|CONNECTION_STRING|DSN"
    r")(?:_|$)",
    re.IGNORECASE,
)
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _resolve_run_from_manifest(payload: dict[str, Any], explicit: str | None) -> Path:
    if explicit:
        return resolve_run(explicit)
    run_id = str(payload.get("run_id", "")).strip()
    if not run_id or run_id == "YOUR_RUN":
        run_id = active_run_id()
    return resolve_run(run_id)


def _replace(value: str, mapping: dict[str, str]) -> str:
    for key, item in mapping.items():
        value = value.replace("{" + key + "}", item)
    return value


def _command_list(raw: Any, mapping: dict[str, str]) -> list[str]:
    if isinstance(raw, list) and all(isinstance(item, (str, int, float)) for item in raw):
        return [_replace(str(item), mapping) for item in raw]
    if isinstance(raw, str):
        # Still no shell: parse into argv. Lists are strongly preferred on Windows.
        return [_replace(item, mapping) for item in shlex.split(raw, posix=os.name != "nt")]
    die("experiment.command 必须是字符串数组或命令字符串")


def _is_sensitive_env_name(name: str) -> bool:
    normalized = name.upper()
    return normalized in SENSITIVE_ENV_NAMES or bool(SENSITIVE_ENV_RE.search(normalized))


def _sanitized_path(value: str) -> str:
    """Drop empty/relative PATH entries so child lookup never falls back to cwd."""
    parts: list[str] = []
    seen: set[str] = set()
    for item in value.split(os.pathsep):
        candidate = item.strip().strip('"')
        if not candidate or "\x00" in candidate:
            continue
        path = Path(candidate).expanduser()
        if not path.is_absolute() or not path.is_dir():
            continue
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        parts.append(str(path))
    return os.pathsep.join(parts)


def _minimal_parent_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    parent = os.environ if source is None else source
    by_name = {str(key).upper(): str(value) for key, value in parent.items()}
    environment: dict[str, str] = {}
    for name in INHERITED_ENV_NAMES:
        value = by_name.get(name)
        if value is None or "\x00" in value:
            continue
        if name == "PATH":
            value = _sanitized_path(value)
            if not value:
                continue
        environment[name] = value
    # Preserve locale categories without copying arbitrary parent variables.
    for name, value in by_name.items():
        if name.startswith("LC_") and not _is_sensitive_env_name(name) and "\x00" not in value:
            environment[name] = value
    return environment


def _manifest_environment(raw_env: dict[Any, Any], mapping: dict[str, str]) -> dict[str, str]:
    normalized_names: dict[str, str] = {}
    overrides: dict[str, str] = {}
    for raw_key, raw_value in raw_env.items():
        if not isinstance(raw_key, str):
            die("experiment.env 的变量名必须是字符串")
        key = raw_key.strip()
        if not ENV_NAME_RE.fullmatch(key):
            die(f"experiment.env 变量名无效：{raw_key!r}")
        canonical = key.upper()
        if canonical in normalized_names:
            die(
                "experiment.env 含大小写冲突的变量名："
                f"{normalized_names[canonical]!r} / {key!r}"
            )
        normalized_names[canonical] = key
        if canonical.startswith("CUMCM_"):
            die(f"experiment.env 不得覆盖 runner 保留变量：{key}")
        if canonical in PROTECTED_ENV_NAMES:
            die(f"experiment.env 不得覆盖进程控制变量：{key}")
        if _is_sensitive_env_name(canonical):
            die(f"experiment.env 不得注入 secret/token/key/credential 变量：{key}")
        value = _replace(str(raw_value), mapping)
        if "\x00" in value:
            die(f"experiment.env 变量值不得含 NUL：{key}")
        overrides[key] = value
    return overrides


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _resolve_executable(
    command: list[str],
    *,
    cwd: Path,
    run: Path,
    environment: dict[str, str],
) -> tuple[list[str], dict[str, Any]]:
    argv0 = command[0]
    if not argv0 or any("\x00" in item for item in command):
        die("experiment.command 不能为空或含 NUL")
    has_separator = "/" in argv0 or "\\" in argv0
    platform_argv0 = argv0.replace("\\", os.sep).replace("/", os.sep)
    raw_path = Path(platform_argv0).expanduser()
    if raw_path.drive and not raw_path.is_absolute():
        die(f"实验可执行文件不得使用 drive-relative 路径：{argv0}")
    if raw_path.is_absolute():
        resolved = raw_path.resolve()
        resolution = "absolute"
    elif has_separator:
        resolved = (cwd / raw_path).resolve()
        if not _inside(resolved, run):
            die(f"相对可执行文件路径离开当前 run：{argv0}")
        resolution = "run-relative"
    else:
        safe_path = environment.get("PATH")
        if not safe_path:
            die(f"最小安全环境没有可用 PATH，不能解析实验可执行文件：{argv0}")
        located = shutil.which(argv0, path=safe_path)
        if not located:
            die(f"在最小安全 PATH 中找不到实验可执行文件：{argv0}")
        resolved = Path(located).resolve()
        resolution = "sanitized-PATH"
    if not resolved.is_file():
        die(f"实验可执行文件不存在或不是文件：{resolved}")
    metadata = {
        "argv0": argv0,
        "resolved_path": str(resolved),
        "resolution": resolution,
        "within_run": _inside(resolved, run),
        "within_project": _inside(resolved, ROOT),
    }
    return [str(resolved), *command[1:]], metadata


def _safe_cwd(run: Path, value: str, mapping: dict[str, str]) -> Path:
    raw = Path(_replace(value, mapping)).expanduser()
    path = (run / raw).resolve() if not raw.is_absolute() else raw.resolve()
    try:
        path.relative_to(run.resolve())
    except ValueError:
        die(f"实验 cwd 位于当前 run 之外：{path}")
    if not path.exists() or not path.is_dir():
        die(f"实验 cwd 不存在：{path}")
    return path


def _safe_component(value: str, *, label: str) -> str:
    if not SAFE_COMPONENT_RE.fullmatch(value):
        die(f"{label} 只能包含字母、数字、点、下划线或连字符：{value!r}")
    return value


def _run_relative_pattern(run: Path, value: str, mapping: dict[str, str]) -> str:
    expanded = _replace(value, mapping).strip()
    if not expanded:
        die("expected_artifacts 不能包含空路径")
    raw = Path(expanded).expanduser()
    if raw.is_absolute():
        try:
            raw = raw.resolve().relative_to(run.resolve())
        except ValueError:
            die(f"预期产物位于当前 run 之外：{expanded}")
    elif ".." in raw.parts:
        die(f"预期产物路径不得包含 '..'：{expanded}")
    normalized = raw.as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized in {"", ".", "/"}:
        die("预期产物不得指向整个 run 目录")
    return normalized


def _resolve_run_file(run: Path, value: str, mapping: dict[str, str], *, label: str) -> Path:
    expanded = _replace(value, mapping).strip()
    if not expanded:
        die(f"{label} 不能为空")
    if any(char in expanded for char in GLOB_CHARS):
        die(f"{label} 必须是单个文件，不得含 glob：{expanded}")
    raw = Path(expanded).expanduser()
    path = raw.resolve() if raw.is_absolute() else (run / raw).resolve()
    try:
        path.relative_to(run.resolve())
    except ValueError:
        die(f"{label} 位于当前 run 之外：{path}")
    return path


def _artifact_records(run: Path, patterns: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    missing: list[str] = []
    for pattern in patterns:
        matches: list[Path] = []
        for candidate in run.glob(pattern):
            if not candidate.is_file() or candidate.stat().st_size == 0:
                continue
            resolved = candidate.resolve()
            try:
                relative = resolved.relative_to(run.resolve())
            except ValueError:
                die(f"预期产物解析到当前 run 之外：{candidate}")
            # Runner-owned attempt logs can never satisfy an experiment artifact pattern;
            # experiment outputs placed below {record_dir}/artifacts remain eligible.
            if "_attempts" in relative.parts and relative.name in RUNNER_RECORD_NAMES:
                continue
            matches.append(resolved)
        if not matches:
            missing.append(pattern)
            continue
        for path in sorted(set(matches)):
            stat = path.stat()
            records.append(
                {
                    "expected_pattern": pattern,
                    "path": relative_to_run(path, run),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "sha256": sha256_file(path),
                }
            )
    return records, missing


def _metric_rows(
    run: Path,
    experiment: dict[str, Any],
    seed: str,
    mapping: dict[str, str],
    fresh_paths: set[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    metrics_file = experiment.get("metrics_file")
    if not metrics_file:
        return [], {"configured": False, "row_count": 0, "errors": []}

    path = _resolve_run_file(run, str(metrics_file), mapping, label="metrics_file")
    relative_path = relative_to_run(path, run)
    errors: list[str] = []
    normalized_rows: list[dict[str, Any]] = []
    if not path.is_file():
        errors.append(f"metrics_file 不存在：{relative_path}")
        return [], {"configured": True, "path": relative_path, "row_count": 0, "errors": errors}
    if relative_path not in fresh_paths:
        errors.append(f"metrics_file 不是本次新建或内容已改变的产物：{relative_path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"metrics_file 无法读取为 JSON：{type(exc).__name__}: {exc}")
        return [], {"configured": True, "path": relative_path, "row_count": 0, "errors": errors}

    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("metrics"), list):
        rows = payload["metrics"]
    else:
        rows = []
        errors.append("metrics_file 必须是指标数组，或含有非空 metrics 数组的对象")
    if not rows:
        errors.append("metrics_file 没有可验证的指标行")

    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(rows, start=1):
        prefix = f"metrics[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} 必须是对象")
            continue
        metric = str(item.get("metric", "")).strip()
        split = str(item.get("split", "")).strip()
        unit = str(item.get("unit", "")).strip()
        value = item.get("value")
        row_errors = False
        if not metric:
            errors.append(f"{prefix}.metric 为空")
            row_errors = True
        if split.lower() in UNSPECIFIED_VALUES:
            errors.append(f"{prefix}.split 必须明确填写（不得为 unspecified/unknown）")
            row_errors = True
        if unit.lower() in UNSPECIFIED_VALUES:
            errors.append(f"{prefix}.unit 必须明确填写（无量纲可写 dimensionless）")
            row_errors = True
        finite_value = False
        if not isinstance(value, bool) and isinstance(value, (int, float)):
            try:
                finite_value = math.isfinite(float(value))
            except (OverflowError, TypeError, ValueError):
                finite_value = False
        if not finite_value:
            errors.append(f"{prefix}.value 必须是有限数值：{value!r}")
            row_errors = True
        item_seed = "none" if item.get("seed") is None else str(item.get("seed"))
        if "seed" in item and item_seed != seed:
            errors.append(f"{prefix}.seed={item['seed']!r} 与执行 seed={seed!r} 不一致")
            row_errors = True
        key = (split, metric)
        if metric and split and key in seen:
            errors.append(f"{prefix} 重复指标键 split={split!r}, metric={metric!r}")
            row_errors = True
        seen.add(key)
        if not row_errors:
            normalized_rows.append(
                {
                    "split": split,
                    "metric": metric,
                    "value": value,
                    "unit": unit,
                    "notes": str(item.get("notes", "")).strip(),
                }
            )
    return normalized_rows, {
        "configured": True,
        "path": relative_path,
        "row_count": len(rows),
        "valid_row_count": len(normalized_rows),
        "errors": errors,
    }


def _append_metrics(run: Path, experiment: dict[str, Any], seed: str, record_dir: Path, rows: list[dict[str, Any]]) -> None:
    for item in rows:
        append_csv(
            run / "experiments" / "METRICS.csv",
            METRIC_FIELDS,
            {
                "experiment_id": experiment["id"],
                "module_id": experiment.get("module_id", ""),
                "split": item["split"],
                "metric": item["metric"],
                "value": item["value"],
                "unit": item["unit"],
                "seed": seed,
                "evidence_path": relative_to_run(record_dir / "run.json", run),
                "notes": item["notes"],
            },
        )


def execute_one(run: Path, experiment: dict[str, Any], seed: Any, args: argparse.Namespace) -> dict[str, Any]:
    if not isinstance(experiment.get("id"), str):
        die("experiment.id 必须是字符串")
    experiment_id = _safe_component(experiment["id"].strip(), label="experiment.id")
    if isinstance(seed, bool) or (seed is not None and not isinstance(seed, (str, int))):
        die(f"experiment {experiment_id} 的 seed 必须是整数、字符串或 null：{seed!r}")
    seed_text = "none" if seed is None else str(seed)
    _safe_component(seed_text, label=f"experiment {experiment_id} seed")
    artifact_dir = run / "experiments" / "runs" / experiment_id / f"seed_{seed_text}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    attempt_id = f"attempt_{time.time_ns()}_{uuid.uuid4().hex[:8]}"
    record_dir = artifact_dir / "_attempts" / attempt_id
    mapping = {
        "seed": seed_text,
        "output_dir": str(artifact_dir.resolve()),
        "record_dir": str(record_dir.resolve()),
        "run_dir": str(run.resolve()),
        "project_root": str(ROOT.resolve()),
        "experiment_id": experiment_id,
        "attempt_id": attempt_id,
    }

    raw_patterns = experiment.get("expected_artifacts")
    if not isinstance(raw_patterns, list) or not raw_patterns:
        die(f"experiment {experiment_id} expected_artifacts 必须是非空数组")
    expected_patterns: list[str] = []
    for item in raw_patterns:
        if not isinstance(item, str):
            die(f"experiment {experiment_id} expected_artifacts 只能包含字符串")
        normalized = _run_relative_pattern(run, item, mapping)
        if normalized not in expected_patterns:
            expected_patterns.append(normalized)
    if experiment.get("metrics_file"):
        metrics_path = _resolve_run_file(run, str(experiment["metrics_file"]), mapping, label="metrics_file")
        metrics_pattern = relative_to_run(metrics_path, run)
        if metrics_pattern not in expected_patterns:
            expected_patterns.append(metrics_pattern)

    pre_artifacts, pre_missing = _artifact_records(run, expected_patterns)
    command = _command_list(experiment.get("command"), mapping)
    if not command:
        die(f"experiment {experiment_id} command 为空")
    cwd = _safe_cwd(run, str(experiment.get("cwd", ".")), mapping)
    try:
        timeout = int(experiment.get("timeout_sec", 1800))
    except (TypeError, ValueError):
        die(f"experiment {experiment_id} timeout_sec 必须是正整数")
    if timeout <= 0:
        die(f"experiment {experiment_id} timeout_sec 必须是正整数")
    raw_env = experiment.get("env", {})
    if not isinstance(raw_env, dict):
        die(f"experiment {experiment_id} env 必须是对象")
    manifest_environment = _manifest_environment(raw_env, mapping)
    environment = _minimal_parent_environment()
    inherited_environment_keys = set(environment)
    for key, value in manifest_environment.items():
        # Environment names are case-insensitive on Windows.  Apply the same
        # collision rule everywhere so a manifest has one portable meaning.
        for inherited_key in list(environment):
            if inherited_key.upper() == key.upper():
                del environment[inherited_key]
                inherited_environment_keys.discard(inherited_key)
        environment[key] = value
    executed_command, executable = _resolve_executable(
        command,
        cwd=cwd,
        run=run,
        environment=environment,
    )
    record_dir.mkdir(parents=True, exist_ok=False)
    environment["CUMCM_RUN_DIR"] = str(run.resolve())
    environment["CUMCM_EXPERIMENT_ID"] = experiment_id
    environment["CUMCM_EXPERIMENT_OUTPUT_DIR"] = str(artifact_dir.resolve())
    environment["CUMCM_EXPERIMENT_RECORD_DIR"] = str(record_dir.resolve())
    environment["CUMCM_ATTEMPT_ID"] = attempt_id
    if seed is not None:
        environment["CUMCM_SEED"] = seed_text

    request = {
        "experiment_id": experiment_id,
        "attempt_id": attempt_id,
        "description": experiment.get("description", ""),
        "module_id": experiment.get("module_id", ""),
        "seed": seed,
        "command": command,
        "executed_command": executed_command,
        "executable": executable,
        "cwd": str(cwd),
        "timeout_sec": timeout,
        "artifact_output_dir": relative_to_run(artifact_dir, run),
        "record_dir": relative_to_run(record_dir, run),
        "expected_artifacts": expected_patterns,
        "environment_override_keys": sorted(manifest_environment),
        "inherited_environment_keys": sorted(inherited_environment_keys),
        "runner_environment_keys": sorted(
            key for key in environment if key.startswith("CUMCM_")
        ),
        "environment_policy": ENVIRONMENT_POLICY,
        "manifest_path": args.manifest_path,
        "manifest_sha256": args.manifest_sha256,
        "pre_artifacts": pre_artifacts,
        "missing_before_run": pre_missing,
        "created_at": now_iso(),
        "no_shell": True,
        # These booleans deliberately describe only what this runner enforces.
        # subprocess(shell=False) is not an OS sandbox.
        "cwd_confined_to_run": True,
        "relative_executable_confined_to_run": True,
        "filesystem_isolated": False,
        "network_isolated": False,
    }
    write_json(record_dir / "request.json", request)
    if args.dry_run:
        request["status"] = "dry-run"
        write_json(record_dir / "request.json", request)
        print(json.dumps(request, ensure_ascii=False, indent=2))
        return request

    started = time.monotonic()
    status = "unknown"
    return_code = None
    timed_out = False
    stdout = ""
    stderr = ""
    try:
        completed = subprocess.run(
            executed_command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            check=False,
        )
        return_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
        status = "passed" if return_code == 0 else "failed"
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        status = "timeout"
        stdout = (exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout) or ""
        stderr = (exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr) or ""
    except OSError as exc:
        status = "launch-error"
        stderr = f"{type(exc).__name__}: {exc}"
    duration = time.monotonic() - started
    (record_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
    (record_dir / "stderr.txt").write_text(stderr, encoding="utf-8")

    artifacts, missing = _artifact_records(run, expected_patterns)
    process_status = status
    pre_state = {
        str(item["path"]): (str(item["sha256"]), int(item.get("mtime_ns", 0)))
        for item in pre_artifacts
    }
    fresh_paths: set[str] = set()
    for item in artifacts:
        previous = pre_state.get(str(item["path"]))
        item["pre_sha256"] = previous[0] if previous is not None else None
        item["pre_mtime_ns"] = previous[1] if previous is not None else None
        # A timestamp-only rewrite is not evidence that this attempt produced a
        # new result.  Existing artifacts are fresh only when their content
        # digest changed; a newly created path is fresh by construction.
        item["fresh"] = previous is None or previous[0] != item["sha256"]
        if item["fresh"]:
            fresh_paths.add(str(item["path"]))
    stale = sorted(
        pattern
        for pattern in expected_patterns
        if pattern not in missing
        and not any(item["expected_pattern"] == pattern and item["fresh"] for item in artifacts)
    )
    metric_rows, metrics_validation = _metric_rows(run, experiment, seed_text, mapping, fresh_paths)
    if status == "passed" and missing:
        status = "missing-artifacts"
    elif status == "passed" and stale:
        status = "stale-artifacts"
    elif status == "passed" and metrics_validation["errors"]:
        status = "invalid-metrics"
    result = {
        **request,
        "finished_at": now_iso(),
        "status": status,
        "process_status": process_status,
        "return_code": return_code,
        "timed_out": timed_out,
        "duration_sec": round(duration, 6),
        "stdout_path": relative_to_run(record_dir / "stdout.txt", run),
        "stdout_sha256": sha256_file(record_dir / "stdout.txt"),
        "stderr_path": relative_to_run(record_dir / "stderr.txt", run),
        "stderr_sha256": sha256_file(record_dir / "stderr.txt"),
        "artifacts": artifacts,
        "missing_expected_artifacts": missing,
        "stale_expected_artifacts": stale,
        "fresh_artifact_count": len(fresh_paths),
        "metrics_validation": metrics_validation,
    }
    write_json(record_dir / "run.json", result)
    notes = [f"missing:{item}" for item in missing]
    notes.extend(f"stale:{item}" for item in stale)
    notes.extend(f"metric-error:{item}" for item in metrics_validation["errors"])
    append_csv(
        run / "experiments" / "EXPERIMENT_LOG.csv",
        LOG_FIELDS,
        {
            "experiment_id": experiment_id,
            "timestamp": result["finished_at"],
            "module_id": experiment.get("module_id", ""),
            "command": json.dumps(command, ensure_ascii=False),
            "seed": seed_text,
            "status": status,
            "output_path": relative_to_run(record_dir / "run.json", run),
            "duration_sec": result["duration_sec"],
            "notes": "; ".join(notes),
        },
    )
    if status == "passed":
        _append_metrics(run, experiment, seed_text, record_dir, metric_rows)
    print(f"[{status}] {experiment_id} seed={seed_text} ({duration:.2f}s)")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--run")
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-going", action="store_true")
    args = parser.parse_args()

    manifest_path = Path(args.manifest).expanduser().resolve()
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        die(f"manifest 无法读取为 JSON：{exc}")
    if not isinstance(manifest, dict):
        die("manifest 必须是 JSON 对象")
    args.manifest_path = str(manifest_path)
    args.manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    run = _resolve_run_from_manifest(manifest, args.run)
    experiments = manifest.get("experiments", [])
    if not isinstance(experiments, list) or not experiments:
        die("manifest.experiments 必须是非空数组")

    experiment_ids: list[str] = []
    for experiment in experiments:
        if not isinstance(experiment, dict):
            die("experiment 必须是对象")
        if not isinstance(experiment.get("id"), str):
            die("experiment.id 必须是字符串")
        experiment_id = _safe_component(experiment["id"].strip(), label="experiment.id")
        experiment["id"] = experiment_id
        if experiment_id in experiment_ids:
            die(f"manifest 含重复 experiment.id：{experiment_id}")
        experiment_ids.append(experiment_id)
        seeds = experiment.get("seeds", [None])
        if not isinstance(seeds, list) or not seeds:
            die(f"experiment {experiment_id} seeds 必须是非空数组")
        seed_keys: set[str] = set()
        for seed in seeds:
            if isinstance(seed, bool) or (seed is not None and not isinstance(seed, (str, int))):
                die(f"experiment {experiment_id} 的 seed 必须是整数、字符串或 null：{seed!r}")
            seed_key = "none" if seed is None else str(seed)
            _safe_component(seed_key, label=f"experiment {experiment_id} seed")
            if seed_key in seed_keys:
                die(f"experiment {experiment_id} 含重复 seed：{seed_key}")
            seed_keys.add(seed_key)

    selected = set(args.only or [])
    unknown_selected = sorted(selected.difference(experiment_ids))
    if unknown_selected:
        die(f"--only 指定了 manifest 中不存在的 experiment：{', '.join(unknown_selected)}")
    results = []
    failure = False
    for experiment in experiments:
        if selected and experiment.get("id") not in selected:
            continue
        seeds = experiment.get("seeds", [None])
        for seed in seeds:
            result = execute_one(run, experiment, seed, args)
            results.append(result)
            if not args.dry_run and result.get("status") != "passed":
                failure = True
                if not args.keep_going:
                    write_json(run / "experiments" / "LAST_BATCH.json", {"created_at": now_iso(), "manifest": str(manifest_path), "manifest_sha256": args.manifest_sha256, "results": results})
                    return 4
    write_json(run / "experiments" / "LAST_BATCH.json", {"created_at": now_iso(), "manifest": str(manifest_path), "manifest_sha256": args.manifest_sha256, "results": results})
    return 4 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
