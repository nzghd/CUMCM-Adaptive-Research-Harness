#!/usr/bin/env python3
"""Config-driven wrapper around the pinned official llm_verifier.select API."""
from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from common import (
    ROOT,
    append_ai_usage_event,
    atomic_write_text,
    complete_api_budget_reservation,
    die,
    now_iso,
    read_json,
    relative_to_root,
    relative_to_run,
    require_external_egress_allowed,
    require_safe_component,
    reserve_api_budget,
    resolve_run,
    sha256_file,
    write_json,
)


def _resolve(value: str | Path, config_dir: Path, *, must_exist: bool = True) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        local = (config_dir / path).resolve()
        path = local if local.exists() else (ROOT / path).resolve()
    else:
        path = path.resolve()
    if must_exist and not path.exists():
        die(f"路径不存在：{path}")
    return path


def _load_config(path: Path) -> dict[str, Any]:
    config = read_json(path, expect=dict)
    for key in ("session_id", "problem_file", "candidate_files", "criteria", "output_dir"):
        if key not in config:
            die(f"Faithful verifier 配置缺少：{key}")
    if not isinstance(config["candidate_files"], dict) or len(config["candidate_files"]) < 2:
        die("candidate_files 至少需要两个候选")
    require_safe_component(config["session_id"], label="Faithful verifier session_id")
    for candidate_id in config["candidate_files"]:
        require_safe_component(candidate_id, label="Faithful verifier candidate_id")
    return config


def _prepare(config_path: Path, *, replace: bool = False) -> dict[str, Any]:
    config = _load_config(config_path)
    config_dir = config_path.parent
    output_dir = _resolve(config["output_dir"], config_dir, must_exist=False)
    if replace and output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    input_dir = output_dir / "prepared_inputs"
    input_dir.mkdir(parents=True, exist_ok=True)

    problem_path = _resolve(config["problem_file"], config_dir)
    problem_copy = input_dir / "problem.md"
    shutil.copy2(problem_path, problem_copy)

    candidates_dir = input_dir / "candidates"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    candidate_manifest = []
    for candidate_id, raw_path in sorted(config["candidate_files"].items()):
        source = _resolve(raw_path, config_dir)
        text = source.read_text(encoding="utf-8-sig").strip()
        if not text:
            die(f"候选为空：{candidate_id} -> {source}")
        target = candidates_dir / f"{candidate_id}.txt"
        atomic_write_text(target, text + "\n")
        candidate_manifest.append(
            {
                "id": candidate_id,
                "source": relative_to_root(source),
                "prepared": str(target),
                "sha256": sha256_file(source),
            }
        )

    criteria = config["criteria"]
    criteria_path = input_dir / "criteria.json"
    if isinstance(criteria, str):
        source = _resolve(criteria, config_dir)
        if source.suffix.lower() == ".json":
            criteria_path = source
        else:
            criteria_path = source
    elif isinstance(criteria, (dict, list)):
        write_json(criteria_path, criteria)
    else:
        die("criteria 必须是路径、对象或数组")

    prepared = {
        "config": config,
        "output_dir": output_dir,
        "problem_path": problem_copy,
        "candidates_dir": candidates_dir,
        "criteria_path": criteria_path,
        "candidate_manifest": candidate_manifest,
        "result_path": output_dir / "RESULT.json",
    }
    write_json(
        output_dir / "PREPARED.json",
        {
            "created_at": now_iso(),
            "faithful": True,
            "upstream": read_json(ROOT / "upstream" / "LLM_AS_A_VERIFIER.lock.json", expect=dict),
            "problem": {"source": relative_to_root(problem_path), "prepared": str(problem_copy), "sha256": sha256_file(problem_path)},
            "candidates": candidate_manifest,
            "criteria_path": str(criteria_path),
            "config_snapshot": config,
        },
    )
    return prepared


def _python_for_reference() -> Path:
    if os.name == "nt":
        candidate = ROOT / ".venv-verifier" / "Scripts" / "python.exe"
    else:
        candidate = ROOT / ".venv-verifier" / "bin" / "python"
    return candidate if candidate.exists() else Path(sys.executable)


def _require_output_within_run(config_path: Path, run: Path) -> Path:
    config = _load_config(config_path)
    output_dir = _resolve(config["output_dir"], config_path.parent, must_exist=False)
    run_root = run.resolve()
    try:
        relative = output_dir.relative_to(run_root)
    except ValueError:
        die(f"Faithful verifier output_dir 必须位于显式绑定的 run 内：{output_dir}")
    if not relative.parts:
        die("Faithful verifier output_dir 不能等于 run 根目录")
    return output_dir


def _provider_is_external(provider: str) -> bool:
    if provider != "local":
        return True
    raw = str(os.getenv("OPENAI_BASE_URL", "")).strip()
    try:
        parsed = urlparse(raw)
    except ValueError:
        parsed = None
    try:
        address_is_loopback = bool(
            parsed is not None
            and parsed.hostname
            and ipaddress.ip_address(parsed.hostname).is_loopback
        )
    except ValueError:
        address_is_loopback = False
    if parsed is None or parsed.scheme not in {"http", "https"} or not address_is_loopback:
        die(
            "--provider local 要求 OPENAI_BASE_URL 明确指向 "
            "http(s) loopback IP（例如 127.0.0.1 或 [::1]）；不接受主机名"
        )
    return False


def _subprocess_environment(provider: str) -> dict[str, str]:
    """Remove credentials for backends outside the declared budget domain."""
    environment = os.environ.copy()
    if provider == "deepseek":
        for key in ("VERTEX_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
            environment.pop(key, None)
    elif provider == "openai":
        for key in ("DEEPSEEK_API_KEY", "VERTEX_API_KEY"):
            environment.pop(key, None)
    else:
        # The loopback URL is already checked. Prevent model-based fallback to
        # remote DeepSeek/Vertex credentials while retaining an optional dummy
        # OPENAI_API_KEY used by many local OpenAI-compatible servers.
        for key in ("DEEPSEEK_API_KEY", "VERTEX_API_KEY"):
            environment.pop(key, None)
    return environment


def _faithful_request_id(config: dict[str, Any]) -> str:
    session = str(config.get("session_id", "")).strip()
    if not session:
        die("Faithful verifier session_id 不能为空")
    return "FAITHFUL_" + "".join(
        char if char.isalnum() or char in "._-" else "-" for char in session
    )


def cmd_dry_run(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    config_path = Path(args.config).expanduser().resolve()
    _require_output_within_run(config_path, run)
    external = _provider_is_external(args.provider)
    phase = require_external_egress_allowed(
        run,
        operation=f"Faithful verifier ({args.provider})",
        dry_run=True,
        external=external,
    )
    prepared = _prepare(config_path, replace=args.replace)
    config = prepared["config"]
    python_exe = _python_for_reference()
    environment = {key: bool(os.getenv(key)) for key in ("DEEPSEEK_API_KEY", "VERTEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_KEY")}
    package_installed = False
    try:
        completed = subprocess.run(
            [str(python_exe), "-c", "import llm_verifier; print(getattr(llm_verifier,'__version__','unknown'))"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        package_installed = completed.returncode == 0
        package_version = completed.stdout.strip() if package_installed else completed.stderr.strip()
    except OSError as exc:
        package_version = str(exc)
    payload = {
        "faithful": True,
        "network_request_sent": False,
        "status": "prepared",
        "run_id": run.name,
        "run_phase": phase,
        "provider": args.provider,
        "backend_scope": "external" if external else "loopback-local",
        "python": str(python_exe),
        "llm_verifier_available": package_installed,
        "llm_verifier_version_or_error": package_version,
        "environment": environment,
        "model": config.get("model"),
        "n_evaluations": config.get("n_evaluations", 4),
        "pivots": config.get("pivots", 2),
        "candidate_count": len(config["candidate_files"]),
        "prepared_dir": str(prepared["output_dir"]),
        "warning": "A successful HTTP response is insufficient; reject flat or near-0.5 scores.",
    }
    write_json(prepared["output_dir"] / "DRY_RUN.json", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not package_installed:
        print("\n安装：powershell -ExecutionPolicy Bypass -File .\\scripts\\install_reference_verifier.ps1", file=sys.stderr)
        return 3
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    run = resolve_run(args.run)
    config_path = Path(args.config).expanduser().resolve()
    output_dir = _require_output_within_run(config_path, run)
    external = _provider_is_external(args.provider)
    require_external_egress_allowed(
        run,
        operation=f"Faithful verifier ({args.provider})",
        dry_run=False,
        external=external,
    )
    if external and not args.ack_separate_billing:
        die(
            "外部 Faithful verifier 必须传入 --ack-separate-billing，"
            "确认后端 API 单独计费。"
        )
    if external and float(args.reserve_usd) <= 0:
        die("外部 Faithful verifier 的 --reserve-usd 必须 > 0")
    if not external and (
        float(args.reserve_usd) != 0.0 or args.actual_usd is not None
    ):
        die("loopback-local Faithful verifier 不接受 API 美元预算参数")
    if (output_dir / "EXECUTION.json").exists():
        die("Faithful verifier EXECUTION.json 已存在；请使用新的 session/output_dir")
    prepared = _prepare(config_path, replace=args.replace)
    config = prepared["config"]
    python_exe = _python_for_reference()
    adapter = ROOT / "integrations" / "llm_as_a_verifier" / "run_faithful_verifier.py"
    command = [
        str(python_exe),
        str(adapter),
        "--problem",
        str(prepared["problem_path"]),
        "--candidates-dir",
        str(prepared["candidates_dir"]),
        "--criteria",
        str(prepared["criteria_path"]),
        "--expected-version",
        "0.2.0",
        "--n-evaluations",
        str(int(config.get("n_evaluations", 4))),
        "--pivots",
        str(int(config.get("pivots", 2))),
        "--seed",
        str(int(config.get("seed", 0))),
        "--out",
        str(prepared["result_path"]),
        "--flat-tolerance",
        str(float(config.get("flat_tolerance", 1e-6))),
    ]
    if config.get("model"):
        command.extend(["--model", str(config["model"])])
    if str(config.get("ground_truth_note", "")).strip():
        command.extend(["--ground-truth-note", str(config["ground_truth_note"])])
    if config.get("max_workers") is not None:
        command.extend(["--max-workers", str(int(config["max_workers"]))])
    if config.get("cache_file"):
        cache_path = _resolve(config["cache_file"], config_path.parent, must_exist=False)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        command.extend(["--cache", str(cache_path)])
    if args.allow_flat:
        command.append("--allow-flat")
    if args.allow_version_mismatch:
        command.append("--allow-version-mismatch")

    request_id = _faithful_request_id(config)
    accounted = 0.0
    accounting_basis = "loopback-local backend; no external API budget"
    if external:
        accounted, accounting_basis = reserve_api_budget(
            run,
            provider=args.provider,
            request_id=request_id,
            reserve_usd=float(args.reserve_usd),
            actual_usd=args.actual_usd,
            metadata={
                "model": str(config.get("model") or "upstream default"),
                "session_id": str(config.get("session_id")),
                "mode": "reference-verifier-faithful",
            },
        )
    execution_path = prepared["output_dir"] / "EXECUTION.json"
    execution = {
        "request_id": request_id,
        "run_id": run.name,
        "created_at": now_iso(),
        "provider": args.provider,
        "backend_scope": "external" if external else "loopback-local",
        "status": "attempted",
        "network_request_attempted": bool(external),
        "network_request_sent": None if external else False,
        "command": command,
        "prepared_sha256": sha256_file(prepared["output_dir"] / "PREPARED.json"),
        "budget_reserved_usd": accounted,
        "accounting_basis": accounting_basis,
        "completed_at": None,
    }
    write_json(execution_path, execution)
    append_ai_usage_event(
        run,
        event_id=request_id,
        tool="LLM-as-a-Verifier (pinned official package)",
        model=str(config.get("model") or "upstream default"),
        stage="reference-verifier-faithful",
        purpose="Best-of-N selection with logprob fine-grained rewards and PPT",
        prompt_or_task_summary=(
            f"session={config.get('session_id')}; "
            f"candidates={len(config['candidate_files'])}; provider={args.provider}"
        ),
        input_artifacts=[
            relative_to_run(prepared["problem_path"], run),
            *[
                relative_to_run(Path(item["source"]), run)
                if Path(item["source"]).is_absolute()
                else item["source"]
                for item in prepared["candidate_manifest"]
            ],
        ],
        output_artifacts=[
            relative_to_run(execution_path, run),
            relative_to_run(prepared["result_path"], run),
            relative_to_run(prepared["output_dir"] / "ERROR.json", run),
        ],
        adopted=False,
        human_modified=False,
        human_verified=False,
        notes=(
            "Verifier preference evidence, not a mathematical proof or correctness "
            f"probability; attempt recorded before subprocess; {accounting_basis}"
        ),
    )
    print("Executing pinned official verifier adapter:")
    print(" ".join(command))
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            env=_subprocess_environment(args.provider),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except BaseException as exc:
        write_json(
            prepared["output_dir"] / "ERROR.json",
            {
                "created_at": now_iso(),
                "status": "failed-or-unknown",
                "error_type": type(exc).__name__,
                "error": str(exc)[:4000],
            },
        )
        execution.update(
            {
                "status": "failed-or-unknown",
                "completed_at": now_iso(),
                "error_type": type(exc).__name__,
            }
        )
        write_json(execution_path, execution)
        if external:
            complete_api_budget_reservation(
                run,
                provider=args.provider,
                request_id=request_id,
                status="failed-or-unknown",
                error_type=type(exc).__name__,
            )
        raise

    atomic_write_text(prepared["output_dir"] / "STDOUT.log", completed.stdout or "")
    atomic_write_text(prepared["output_dir"] / "STDERR.log", completed.stderr or "")
    if completed.stdout:
        print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="" if completed.stderr.endswith("\n") else "\n")

    result_exists = prepared["result_path"].is_file()
    succeeded = completed.returncode == 0 and result_exists
    usage: Any = None
    if result_exists:
        try:
            result_payload = read_json(prepared["result_path"], expect=dict)
            usage = result_payload.get("usage")
        except SystemExit:
            result_payload = None
    status = "succeeded" if succeeded else "failed-or-unknown"
    execution.update(
        {
            "status": status,
            "network_request_sent": True if external and succeeded else (None if external else False),
            "returncode": int(completed.returncode),
            "result_exists": result_exists,
            "completed_at": now_iso(),
        }
    )
    write_json(execution_path, execution)
    if not succeeded:
        write_json(
            prepared["output_dir"] / "ERROR.json",
            {
                "created_at": now_iso(),
                "status": "failed-or-unknown",
                "error_type": "VerifierProcessFailure",
                "error": (
                    f"returncode={completed.returncode}; result_exists={result_exists}; "
                    "see STDOUT.log and STDERR.log"
                ),
            },
        )
    if external:
        complete_api_budget_reservation(
            run,
            provider=args.provider,
            request_id=request_id,
            status=status,
            usage=usage,
            error_type="" if succeeded else "VerifierProcessFailure",
        )
    return int(completed.returncode if completed.returncode != 0 else (0 if result_exists else 3))


def cmd_diagnose(args: argparse.Namespace) -> int:
    python_exe = _python_for_reference()
    script = ROOT / "integrations" / "llm_as_a_verifier" / "diagnose.py"
    return int(subprocess.run([str(python_exe), str(script)], cwd=ROOT, check=False).returncode)


def _run_ancestor(path: Path) -> Path | None:
    resolved = path.resolve()
    for parent in (resolved, *resolved.parents):
        if (parent / "RUN_META.json").is_file() and (parent / "STATUS.json").is_file():
            return parent
        if parent == ROOT.resolve():
            break
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("dry-run")
    p.add_argument("--run", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--provider", choices=["openai", "deepseek", "local"], required=True)
    p.add_argument("--replace", action="store_true")
    p.set_defaults(func=cmd_dry_run)
    p = sub.add_parser("run")
    p.add_argument("--run", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--provider", choices=["openai", "deepseek", "local"], required=True)
    p.add_argument("--reserve-usd", type=float, default=0.0)
    p.add_argument("--actual-usd", type=float)
    p.add_argument("--ack-separate-billing", action="store_true")
    p.add_argument("--replace", action="store_true")
    p.add_argument("--allow-flat", action="store_true")
    p.add_argument("--allow-version-mismatch", action="store_true")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("diagnose")
    p.set_defaults(func=cmd_diagnose)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
