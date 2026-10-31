#!/usr/bin/env python3
"""Shared helpers for the v5/v5.1 modeling and interpretation overlay."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from common import ROOT, RUNS_ROOT, active_run_id, die, read_json, resolve_run, write_json

TEXT_SUFFIXES = {".md", ".txt", ".csv", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".py", ".tex"}


def resolve_run_flexible(value: str | None) -> Path:
    """Resolve a run id or a project-relative/absolute run path safely."""
    raw = (value or "").strip()
    if not raw:
        raw = active_run_id()
    candidate = Path(raw).expanduser()
    if candidate.is_absolute() or len(candidate.parts) > 1:
        if not candidate.is_absolute():
            candidate = ROOT / candidate
        run = candidate.resolve()
    else:
        run = (RUNS_ROOT / raw).resolve()
    runs_root = RUNS_ROOT.resolve()
    if run == runs_root or run.parent != runs_root:
        die(f"运行目录必须是 runs/ 的 canonical 直接子目录：{run}")
    return resolve_run(run.name)


def parse_kv(items: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            die(f"参数必须为 key=value：{item}")
        key, value = item.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or not value:
            die(f"参数必须为非空 key=value：{item}")
        result[key] = value
    return result


def split_values(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in re.split(r"[;,|]", value) if part.strip()]


def load_or(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return read_json(path, expect=type(default))


def write_if_missing(path: Path, text: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def json_if_missing(path: Path, payload: Any) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, payload)
    return True


def project_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def run_relative(path: Path, run: Path) -> str:
    try:
        return path.resolve().relative_to(run.resolve()).as_posix()
    except ValueError:
        return project_relative(path)


def safe_run_ref(run: Path, value: str, *, must_exist: bool = False) -> Path:
    """Resolve a path reference under the run directory; reject traversal."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = run / path
    path = path.resolve()
    try:
        path.relative_to(run.resolve())
    except ValueError:
        die(f"路径必须位于当前 run 内：{path}")
    if must_exist and not path.exists():
        die(f"引用文件不存在：{path}")
    return path


def iter_text_files(base: Path, *, excluded_parts: set[str] | None = None) -> Iterable[Path]:
    excluded_parts = excluded_parts or set()
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if any(part in excluded_parts for part in path.parts):
            continue
        if "__pycache__" in path.parts or ".git" in path.parts:
            continue
        yield path


def read_text_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def json_dump_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
