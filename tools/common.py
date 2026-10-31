#!/usr/bin/env python3
"""Shared, standard-library-only helpers for the CUMCM harness tools."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, NoReturn, Sequence


def root_path() -> Path:
    """Return the project root, optionally overridden for tests."""
    override = os.getenv("CUMCM_HARNESS_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


ROOT = root_path()
CONFIG_PATH = ROOT / "harness_config.json"
RUNS_ROOT = ROOT / "runs"
INPUTS_ROOT = ROOT / "inputs"
HARNESS_STATE = ROOT / ".harness"
ACTIVE_FILE = HARNESS_STATE / "active_run.txt"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


PLACEHOLDER_HUMAN_IDENTITIES = frozenset({
    "TEAM", "UNKNOWN", "UNSPECIFIED", "N/A", "NA", "NONE", "TODO", "TBD",
    "ANONYMOUS", "HUMAN", "REVIEWER", "AUTO", "DEFAULT", "AGENT", "SYSTEM",
    "ORCHESTRATOR", "CODEX", "GPT", "CLAUDE", "DEEPSEEK",
})

CLAIM_EVIDENCE_FIELDS = (
    "claim_id",
    "claim_text",
    "claim_type",
    "is_core",
    "evidence_id",
    "evidence_type",
    "evidence_path",
    "generation_command",
    "result_summary",
    "verification_status",
    "verified_by",
    "verified_at",
    "paper_location",
    "notes",
)

RUN_PHASES = frozenset({"rehearsal", "competition", "benchmark", "calibration"})
WINDOWS_RESERVED_COMPONENTS = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


def is_named_human_identity(value: Any) -> bool:
    """Return whether ``value`` names a concrete human rather than a placeholder."""
    text = str(value or "").strip()
    return bool(text) and text.upper() not in PLACEHOLDER_HUMAN_IDENTITIES


def is_timezone_aware_iso(value: Any) -> bool:
    """Accept an ISO-8601 timestamp only when it carries an explicit timezone."""
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return False
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def die(message: str, code: int = 2) -> NoReturn:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(code)


def warn(message: str) -> None:
    print(f"警告：{message}", file=sys.stderr)


def slugify(value: str) -> str:
    value = value.strip().replace(" ", "-")
    value = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-._")
    return value or "run"


def require_safe_component(value: Any, *, label: str) -> str:
    """Return an ASCII identifier that cannot change a filesystem parent."""
    raw = str(value or "")
    text = raw.strip()
    windows_stem = text.split(".", 1)[0].upper()
    if (
        not text
        or text != raw
        or text in {".", ".."}
        or text.endswith(".")
        or windows_stem in WINDOWS_RESERVED_COMPONENTS
        or len(text) > 160
        or re.fullmatch(r"[A-Za-z0-9_.-]+", text) is None
    ):
        die(
            f"{label} 必须是 1-160 位 [A-Za-z0-9_.-] 标识符，"
            "不能有首尾空白/末尾点，也不能是 .、.. 或 Windows 设备名"
        )
    return text


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def read_json(path: Path, *, expect: type | tuple[type, ...] | None = dict) -> Any:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        die(f"找不到文件：{path}")
    except json.JSONDecodeError as exc:
        die(f"JSON 无效 {path}: {exc}")
    if expect is not None and not isinstance(payload, expect):
        die(f"JSON 顶层类型错误：{path}，期望 {expect}，实际 {type(payload).__name__}")
    return payload


def write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            die(f"JSONL 无效 {path}:{line_no}: {exc}")
        if not isinstance(item, dict):
            die(f"JSONL 每行必须是对象：{path}:{line_no}")
        rows.append(item)
    return rows


def csv_header(path: Path) -> list[str]:
    """Return a CSV header exactly as stored, after UTF-8 BOM handling."""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            return next(reader)
        except StopIteration:
            return []


def require_csv_header(path: Path, expected_fields: Sequence[str]) -> None:
    """Fail closed when an existing CSV does not use the expected contract.

    Rewriting a CSV under a different set of columns can silently discard data.
    Schema changes therefore require an explicit migration before normal tools
    may read or write the file.
    """
    if not path.exists():
        return
    expected = list(expected_fields)
    actual = csv_header(path)
    if actual != expected:
        die(
            "CSV 表头与当前契约不一致，拒绝继续以避免静默丢列："
            f"{path}\n实际：{actual}\n期望：{expected}\n"
            "请先运行显式迁移工具 `python tools/migrate_v51.py --run <RUN_ID> --apply`。"
        )


def _require_known_row_fields(
    path: Path, fieldnames: Sequence[str], rows: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    materialized = list(rows)
    allowed = set(fieldnames)
    for index, row in enumerate(materialized, start=1):
        unknown = sorted(repr(key) for key in set(row) - allowed)
        if unknown:
            die(
                f"CSV 写入包含契约外字段，拒绝丢弃：{path} row={index} "
                f"unknown={unknown}"
            )
    return materialized


def read_csv(
    path: Path, *, expected_fields: Sequence[str] | None = None
) -> list[dict[str, str]]:
    if not path.exists():
        return []
    if expected_fields is not None:
        require_csv_header(path, expected_fields)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    require_csv_header(path, fieldnames)
    materialized = _require_known_row_fields(path, fieldnames, rows)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="raise")
            writer.writeheader()
            for row in materialized:
                writer.writerow({key: _csv_value(row.get(key, "")) for key in fieldnames})
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def append_csv(path: Path, fieldnames: Sequence[str], row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    if exists:
        require_csv_header(path, fieldnames)
    _require_known_row_fields(path, fieldnames, [row])
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="raise")
        if not exists:
            writer.writeheader()
        writer.writerow({key: _csv_value(row.get(key, "")) for key in fieldnames})


def resolve_run_evidence_ref(
    run: Path, value: str
) -> tuple[list[dict[str, str]], list[str]]:
    """Resolve one scientific-evidence reference to non-empty run-local files.

    A reference may be a path below ``run`` or an ``evidence_id`` from the
    claim-evidence ledger. URLs are deliberately rejected: a citation is not
    itself a reproduced scientific-result artifact. Returning errors instead
    of a boolean keeps failed IDs, traversal and stale ledger paths auditable.
    """
    reference = str(value or "").strip()
    if not reference:
        return [], ["evidence reference is empty"]
    if reference.startswith(("http://", "https://")):
        return [], [f"external URL is not a run-local evidence artifact: {reference}"]

    run_root = run.resolve()

    def resolve_local(raw_value: str) -> tuple[Path | None, str | None]:
        token = raw_value.split("#", 1)[0].strip()
        if not token:
            return None, "evidence artifact path is empty"
        raw = Path(token).expanduser()
        candidate = raw.resolve() if raw.is_absolute() else (run_root / raw).resolve()
        try:
            candidate.relative_to(run_root)
        except ValueError:
            return None, f"evidence reference leaves the run directory: {raw_value}"
        if not candidate.exists():
            return None, None
        if not candidate.is_file():
            return None, f"evidence reference is not a file: {raw_value}"
        try:
            if candidate.stat().st_size <= 0:
                return None, f"evidence artifact is empty: {raw_value}"
        except OSError as exc:
            return None, f"cannot inspect evidence artifact {raw_value}: {exc}"
        return candidate, None

    ledger_path = run_root / "evidence" / "CLAIM_EVIDENCE_LEDGER.csv"
    reference_base, separator, reference_selector = reference.partition("#")
    ledger_reference = False
    try:
        raw_base = Path(reference_base.strip()).expanduser()
        resolved_base = (
            raw_base.resolve() if raw_base.is_absolute() else (run_root / raw_base).resolve()
        )
        resolved_base.relative_to(run_root)
        ledger_reference = resolved_base == ledger_path.resolve()
        if not ledger_reference and resolved_base.exists() and ledger_path.exists():
            try:
                ledger_reference = os.path.samefile(resolved_base, ledger_path)
            except OSError:
                ledger_reference = False
    except (OSError, ValueError):
        ledger_reference = False
    ledger_selector = reference_selector.strip() if separator and ledger_reference else ""
    if ledger_reference and not ledger_selector:
        return [], [
            "claim-evidence ledger is an index, not evidence; append #<evidence_id>"
        ]
    lookup_id = ledger_selector or reference
    matches = []
    if ledger_path.is_file():
        try:
            with ledger_path.open("r", encoding="utf-8-sig", newline="") as handle:
                header = next(csv.reader(handle), [])
        except (OSError, UnicodeError, csv.Error) as exc:
            return [], [f"evidence ledger cannot be read: {exc}"]
        if header != list(CLAIM_EVIDENCE_FIELDS):
            return [], [
                "evidence ledger header is non-canonical: "
                f"expected={list(CLAIM_EVIDENCE_FIELDS)!r}, actual={header!r}"
            ]
        matches = [
            row for row in read_csv(ledger_path)
            if str(row.get("evidence_id", "")).strip() == lookup_id
        ]
    if len(matches) > 1:
        return [], [f"evidence ID is not unique in ledger: {lookup_id}"]
    if matches:
        ledger_row = matches[0]
        if ledger_row.get("evidence_id") != lookup_id:
            return [], [
                f"ledger evidence_id is not canonical for {lookup_id}"
            ]
        required_text = (
            "claim_id", "claim_text", "claim_type", "evidence_id",
            "evidence_type", "result_summary",
        )
        missing = [
            field for field in required_text
            if not str(ledger_row.get(field, "")).strip()
        ]
        if missing:
            return [], [
                f"ledger evidence row is non-canonical for {reference}: "
                f"missing={missing}"
            ]
        if ledger_row.get("is_core") not in {"true", "false"}:
            return [], [
                f"ledger evidence row has non-canonical is_core for {reference}"
            ]
        verification_status = str(ledger_row.get("verification_status", ""))
        if verification_status != "verified":
            return [], [
                f"ledger evidence is not human-verified: {reference} "
                f"(verification_status={verification_status!r})"
            ]
        verified_by = str(ledger_row.get("verified_by", "")).strip()
        if not is_named_human_identity(verified_by):
            return [], [
                f"ledger evidence lacks a named verified_by: {reference}"
            ]
        verified_at = str(ledger_row.get("verified_at", "")).strip()
        if not is_timezone_aware_iso(verified_at):
            return [], [
                f"ledger evidence lacks a timezone-aware verified_at: {reference}"
            ]
        ledger_target = str(ledger_row.get("evidence_path", "")).strip()
        if not ledger_target:
            return [], [f"ledger evidence_path is empty: {reference}"]
        resolved, ledger_error = resolve_local(ledger_target)
        if ledger_error:
            return [], [f"{reference}: {ledger_error}"]
        if resolved is None:
            return [], [
                f"ledger evidence artifact does not exist: {reference} -> {ledger_target}"
            ]
        return [{
            "reference": reference,
            "path": resolved.relative_to(run_root).as_posix(),
            "sha256": sha256_file(resolved),
            "evidence_id": lookup_id,
            "evidence_type": str(ledger_row.get("evidence_type", "")).strip(),
            "verified_by": verified_by,
            "verified_at": verified_at,
            "ledger_row_sha256": hashlib.sha256(
                json.dumps(
                    ledger_row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        }], []

    # Tokens shaped like ledger IDs may not fall back to coincidentally named
    # files; otherwise an unregistered ``EVID-*`` could bypass ledger review.
    if ledger_selector:
        return [], [f"evidence ID does not resolve in ledger: {lookup_id}"]
    if re.fullmatch(r"EVID[-_].+", reference, re.IGNORECASE):
        if not ledger_path.is_file():
            return [], [f"evidence ledger is missing for ID: {reference}"]
        return [], [f"evidence ID does not resolve in ledger: {reference}"]

    direct, direct_error = resolve_local(reference)
    if direct_error:
        return [], [direct_error]
    if direct is not None:
        return [{
            "reference": reference,
            "path": direct.relative_to(run_root).as_posix(),
            "sha256": sha256_file(direct),
        }], []
    return [], [f"evidence reference does not resolve: {reference}"]


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def load_config() -> dict[str, Any]:
    return read_json(CONFIG_PATH, expect=dict)


def resolve_project_path(value: str | Path, *, must_exist: bool = False) -> Path:
    """Resolve a project-relative path and reject traversal outside ROOT."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    try:
        path.relative_to(ROOT.resolve())
    except ValueError:
        die(f"路径位于项目根目录之外：{path}")
    if must_exist and not path.exists():
        die(f"路径不存在：{path}")
    return path


def relative_to_root(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def relative_to_run(path: Path, run: Path) -> str:
    try:
        return path.resolve().relative_to(run.resolve()).as_posix()
    except ValueError:
        return relative_to_root(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def iter_files(base: Path, *, exclude_names: set[str] | None = None) -> Iterable[Path]:
    exclude_names = exclude_names or set()
    if not base.exists():
        return []
    return (
        path
        for path in sorted(base.rglob("*"))
        if path.is_file()
        and path.name not in exclude_names
        and "__pycache__" not in path.parts
        and ".git" not in path.parts
    )


def write_active(run_id: str) -> None:
    HARNESS_STATE.mkdir(parents=True, exist_ok=True)
    atomic_write_text(ACTIVE_FILE, run_id + "\n")


def active_run_id() -> str:
    if not ACTIVE_FILE.exists():
        die("尚未设置 active run。先执行 `python tools/harness.py init ...`。")
    run_id = ACTIVE_FILE.read_text(encoding="utf-8").strip()
    if not run_id:
        die("active_run.txt 为空。")
    return run_id


def resolve_run(run_arg: str | None = None) -> Path:
    run_id = str(run_arg or active_run_id()).strip()
    if not run_id:
        die("run_id 为空。")
    run = (RUNS_ROOT / run_id).resolve()
    runs_root = RUNS_ROOT.resolve()
    if run == runs_root or run.parent != runs_root:
        die(f"run 必须是 runs/ 的直接子目录，不能是根目录或嵌套路径：{run_id}")
    if not run.exists() or not run.is_dir():
        die(f"运行目录不存在：{run}")
    expected_run_path = relative_to_root(run)
    for filename in ("RUN_META.json", "STATUS.json"):
        envelope_path = run / filename
        if envelope_path.is_symlink() or not envelope_path.is_file():
            die(f"运行目录缺少 canonical {filename} envelope：{run}")
        try:
            if envelope_path.resolve(strict=True).parent != run:
                die(f"运行目录 {filename} envelope 离开当前 run：{run}")
        except OSError as exc:
            die(f"运行目录 {filename} envelope 无法解析：{exc}")
    meta = read_json(run / "RUN_META.json", expect=dict)
    status = read_json(run / "STATUS.json", expect=dict)
    if meta.get("run_id") != run.name:
        die("RUN_META.json run_id 与 run 目录名不一致")
    if str(meta.get("run_path", "")).replace("\\", "/") != expected_run_path:
        die("RUN_META.json run_path 与 canonical run 路径不一致")
    if status.get("run_id") != run.name:
        die("STATUS.json run_id 与 run 目录名不一致")
    return run


def replace_placeholders(root: Path, mapping: dict[str, str]) -> None:
    extensions = {".md", ".txt", ".csv", ".json", ".jsonl", ".toml", ".yaml", ".yml", ".py"}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        original = text
        for key, value in mapping.items():
            text = text.replace("{{" + key + "}}", value)
        if text != original:
            atomic_write_text(path, text)


def load_run_meta(run: Path) -> dict[str, Any]:
    return read_json(run / "RUN_META.json", expect=dict)


def require_external_egress_allowed(
    run: Path,
    *,
    operation: str,
    dry_run: bool = False,
    external: bool = True,
) -> str:
    """Fail closed before a run-backed operation can disclose material externally.

    A competition run may still prepare a local dry-run preview.  It may not
    execute an external request or create a packet whose declared purpose is
    manual external upload.  Explicitly local/loopback computation passes
    ``external=False`` and remains available in competition runs.
    """
    meta = load_run_meta(run)
    phase = str(meta.get("phase", "")).strip()
    if phase not in RUN_PHASES:
        die(
            f"{operation} 拒绝执行：RUN_META.json phase 缺失或未知 "
            f"({phase or 'missing'})"
        )
    if phase == "competition" and external and not dry_run:
        die(
            f"{operation} 在 competition phase 禁止外发；"
            "本工具不提供 force 绕过。可使用只写本地预览的 dry-run。"
        )
    return phase


@contextmanager
def run_budget_lock(run: Path, *, timeout_seconds: float = 15.0) -> Iterator[None]:
    """Serialize cross-process BUDGET.json read-modify-write operations."""
    lock_path = run / ".locks" / "BUDGET.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    if lock_path.stat().st_size == 0:
        handle.write(b"0")
        handle.flush()
        os.fsync(handle.fileno())
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        die("等待 BUDGET.json 跨进程锁超时")
                    time.sleep(0.05)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        die("等待 BUDGET.json 跨进程锁超时")
                    time.sleep(0.05)
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _provider_budget_item(budget: dict[str, Any], provider: str) -> dict[str, Any]:
    providers = budget.setdefault("api_providers", {})
    item = providers.get(provider)
    if not isinstance(item, dict) and provider == "openai":
        legacy = budget.get("openai_api", {})
        item = dict(legacy) if isinstance(legacy, dict) else {}
        providers[provider] = item
    if not isinstance(item, dict):
        die(f"BUDGET.json 缺少 api_providers.{provider}")
    return item


def _sync_legacy_openai_budget(budget: dict[str, Any]) -> None:
    provider = budget.get("api_providers", {}).get("openai", {})
    if not isinstance(provider, dict):
        return
    legacy = budget.setdefault("openai_api", {})
    for key in (
        "enabled", "max_usd", "used_usd", "default_model", "call_count",
        "usage_records",
    ):
        if key in provider:
            legacy[key] = provider[key]


def reserve_api_budget(
    run: Path,
    *,
    provider: str,
    request_id: str,
    reserve_usd: float,
    actual_usd: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[float, str]:
    """Atomically reserve conservative API cost before an external attempt.

    Caller-supplied ``actual_usd`` can increase the reservation but can never
    reduce it.  A failed/uncertain request keeps the reservation until a human
    reconciles provider billing.
    """
    reserve = float(reserve_usd)
    actual = None if actual_usd is None else float(actual_usd)
    if not math.isfinite(reserve) or reserve <= 0:
        die("--reserve-usd 必须是有限且 > 0 的数")
    if actual is not None and (not math.isfinite(actual) or actual < 0):
        die("--actual-usd 必须是有限且非负的数")
    accounted = max(reserve, actual) if actual is not None else reserve
    ident = str(request_id or "").strip()
    if not ident:
        die("API budget reservation requires a non-empty request id")

    with run_budget_lock(run):
        budget = load_budget(run)
        item = _provider_budget_item(budget, provider)
        if item.get("enabled") is not True:
            die(f"BUDGET.json 中 api_providers.{provider}.enabled=false")
        maximum = float(item.get("max_usd", 0.0))
        used = float(item.get("used_usd", 0.0))
        if not math.isfinite(maximum) or not math.isfinite(used):
            die(f"BUDGET.json 中 api_providers.{provider} 金额不是有限数")
        if used + accounted > maximum + 1e-12:
            die(
                f"{provider} API 预算不足：${used:.4f} + "
                f"${accounted:.4f} > ${maximum:.4f}"
            )
        records = item.setdefault("usage_records", [])
        if not isinstance(records, list):
            die(f"BUDGET.json 中 api_providers.{provider}.usage_records 必须是数组")
        if any(isinstance(row, dict) and row.get("id") == ident for row in records):
            die(f"API budget reservation id 已存在：{ident}")
        record = {
            "id": ident,
            "provider": provider,
            "timestamp": now_iso(),
            "status": "reserved-before-attempt",
            "reserved_usd": accounted,
            "caller_actual_usd": actual,
        }
        record.update(dict(metadata or {}))
        # Provider identity is accounting provenance, not caller metadata.
        record["provider"] = provider
        records.append(record)
        item["used_usd"] = round(used + accounted, 6)
        item["call_count"] = int(item.get("call_count", 0)) + 1
        if provider == "openai":
            _sync_legacy_openai_budget(budget)
        save_budget(run, budget)
    basis = (
        "max(conservative reservation, caller-supplied actual); reconcile with provider dashboard"
        if actual is not None
        else "conservative pre-call reservation; reconcile with provider dashboard"
    )
    return accounted, basis


def complete_api_budget_reservation(
    run: Path,
    *,
    provider: str,
    request_id: str,
    status: str,
    usage: Any = None,
    error_type: str = "",
) -> None:
    """Attach a terminal/uncertain outcome without refunding a reservation."""
    allowed = {"succeeded", "failed-or-unknown", "post-response-error"}
    if status not in allowed:
        die(f"未知 API budget outcome：{status}")
    with run_budget_lock(run):
        budget = load_budget(run)
        item = _provider_budget_item(budget, provider)
        records = item.get("usage_records", [])
        matches = [
            row for row in records
            if isinstance(row, dict) and row.get("id") == request_id
        ]
        if len(matches) != 1:
            die(f"找不到唯一 API budget reservation：{request_id}")
        record = matches[0]
        record["status"] = status
        record["completed_at"] = now_iso()
        record["usage"] = usage
        record["error_type"] = str(error_type or "")
        if provider == "openai":
            _sync_legacy_openai_budget(budget)
        save_budget(run, budget)


def load_status(run: Path) -> dict[str, Any]:
    return read_json(run / "STATUS.json", expect=dict)


def save_status(run: Path, status: dict[str, Any]) -> None:
    status["updated_at"] = now_iso()
    write_json(run / "STATUS.json", status)


def load_budget(run: Path) -> dict[str, Any]:
    return read_json(run / "BUDGET.json", expect=dict)


def save_budget(run: Path, budget: dict[str, Any]) -> None:
    budget["updated_at"] = now_iso()
    write_json(run / "BUDGET.json", budget)


def is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是", "已验证", "verified"}


def nonempty_file(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def pdf_envelope_errors(path: Path) -> list[str]:
    """Require a parseable, non-empty-page PDF for a typed delivery slot."""
    if not path.is_file():
        return ["file is missing"]
    try:
        size = path.stat().st_size
        if size < 32:
            return [f"file is too short to be a PDF ({size} bytes)"]
        with path.open("rb") as handle:
            header = handle.read(16)
            handle.seek(max(0, size - 8192))
            tail = handle.read()
    except OSError as exc:
        return [f"file cannot be inspected: {exc}"]
    errors: list[str] = []
    if re.match(rb"^%PDF-[0-9]+\.[0-9]+(?:\r\n|\r|\n)", header) is None:
        errors.append("missing canonical %PDF-version header")
    stripped_tail = tail.rstrip(b"\x00\t\n\f\r ")
    if not stripped_tail.endswith(b"%%EOF"):
        errors.append("missing terminal %%EOF marker")
    if b"startxref" not in tail:
        errors.append("missing trailing startxref marker")
    if errors:
        return errors
    try:
        try:
            from pypdf import PdfReader  # type: ignore[import-not-found]
        except ImportError:
            from PyPDF2 import PdfReader  # type: ignore[import-not-found,no-redef]
    except ImportError:
        return [
            "no PDF parser is installed; install requirements-core.txt "
            "before validating typed PDF deliverables"
        ]
    try:
        reader = PdfReader(str(path), strict=True)
        page_count = len(reader.pages)
    except Exception as exc:
        return [f"PDF parser rejected the file: {type(exc).__name__}: {exc}"]
    if page_count < 1:
        return ["PDF contains no readable pages"]
    return errors


def contains_placeholder(path: Path, tokens: Sequence[str]) -> list[str]:
    if not path.is_file() or path.suffix.lower() not in {".md", ".txt", ".csv", ".json", ".jsonl", ".toml", ".yaml", ".yml", ".py"}:
        return []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return []
    return [token for token in tokens if token and token in text]


def next_numeric_id(prefix: str, existing: Iterable[str], width: int = 3) -> str:
    pattern = re.compile(rf"^{re.escape(prefix)}_(\d+)$", re.IGNORECASE)
    used = []
    for value in existing:
        match = pattern.match(str(value))
        if match:
            used.append(int(match.group(1)))
    return f"{prefix}_{(max(used, default=0) + 1):0{width}d}"


def _require_canonical_run_namespace_path(
    run: Path,
    namespace: Path,
    path: Path,
    *,
    label: str,
) -> Path:
    """Reject symlink/non-canonical card paths before they affect counts."""
    run_root = run.resolve()
    lexical_namespace = namespace.relative_to(run).as_posix()
    if namespace.is_symlink():
        die(f"{label} namespace 不能是 symlink：{lexical_namespace}")
    try:
        resolved_namespace = namespace.resolve(strict=True)
        namespace_relative = resolved_namespace.relative_to(run_root).as_posix()
    except (OSError, ValueError):
        die(f"{label} namespace 离开当前 run：{lexical_namespace}")
    if namespace_relative != lexical_namespace:
        die(f"{label} namespace 不是 canonical run 路径：{lexical_namespace}")
    lexical = path.relative_to(run).as_posix()
    if path.is_symlink() or path.parent.is_symlink():
        die(f"{label} 不能来自 symlink 文件或目录：{lexical}")
    try:
        resolved = path.resolve(strict=True)
        resolved_relative = resolved.relative_to(run_root).as_posix()
        parent_relative = path.parent.resolve(strict=True).relative_to(run_root).as_posix()
    except (OSError, ValueError):
        die(f"{label} 离开当前 run 或无法解析：{lexical}")
    if resolved_relative != lexical or parent_relative != path.parent.relative_to(run).as_posix():
        die(f"{label} 不是 canonical run 路径：{lexical}")
    if not resolved.is_file():
        die(f"{label} 不是普通文件：{lexical}")
    return resolved


def load_idea_cards(run: Path) -> list[tuple[Path, dict[str, Any]]]:
    cards: list[tuple[Path, dict[str, Any]]] = []
    roots = [run / "research" / "ideas", run / "ideas"]
    seen: set[Path] = set()
    seen_ids: dict[str, Path] = {}
    for root in roots:
        if root.is_symlink():
            die(f"Idea Card namespace 不能是 symlink：{root.relative_to(run).as_posix()}")
        if not root.exists():
            continue
        for path in sorted(root.glob("*/IDEA_CARD.json")):
            resolved = _require_canonical_run_namespace_path(
                run, root, path, label="Idea Card"
            )
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                die(f"Idea Card JSON 无效 {path}: {exc}")
            if not isinstance(payload, dict) or not payload.get("id"):
                die(f"Idea Card 必须是含非空 id 的对象：{path}")
            if not isinstance(payload["id"], str):
                die(f"Idea Card id 必须是字符串：{path}")
            card_id = require_safe_component(payload["id"], label="Idea Card id")
            if card_id != path.parent.name:
                die(
                    f"Idea Card id 必须与父目录名一致：{path} "
                    f"(id={card_id!r}, 目录={path.parent.name!r})"
                )
            previous = seen_ids.get(card_id)
            if previous is not None and previous != resolved:
                die(
                    f"Idea Card id 在 run 内必须全局唯一：{card_id!r} "
                    f"同时出现于 {previous} 和 {resolved}"
                )
            seen_ids[card_id] = resolved
            cards.append((path, payload))
    return cards


def load_module_cards(run: Path) -> list[tuple[Path, dict[str, Any]]]:
    cards: list[tuple[Path, dict[str, Any]]] = []
    root = run / "research" / "modules"
    if root.is_symlink():
        die("Module Card namespace 不能是 symlink：research/modules")
    if not root.exists():
        return cards
    seen_dirs: set[Path] = set()
    seen_ids: dict[str, Path] = {}
    for pattern in ("*/MODULE_CARD.json", "*/MODULE_SPEC.json"):
        for path in sorted(root.glob(pattern)):
            resolved = _require_canonical_run_namespace_path(
                run, root, path, label="Module Card"
            )
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                die(f"Module Card JSON 无效 {path}: {exc}")
            if not isinstance(payload, dict):
                die(f"Module Card 必须是对象：{path}")
            if not payload.get("id") and payload.get("module_id"):
                payload = dict(payload)
                payload["id"] = payload["module_id"]
            if not payload.get("id"):
                die(f"Module Card 缺少非空 id：{path}")
            if not isinstance(payload["id"], str):
                die(f"Module Card id 必须是字符串：{path}")
            card_id = require_safe_component(payload["id"], label="Module Card id")
            if path.name == "MODULE_SPEC.json" and payload.get("module_id"):
                if not isinstance(payload["module_id"], str):
                    die(f"MODULE_SPEC module_id 必须是字符串：{path}")
                spec_id = require_safe_component(
                    payload["module_id"], label="MODULE_SPEC module_id"
                )
                if spec_id != card_id:
                    die(
                        f"MODULE_SPEC module_id 与 id 不一致：{path} "
                        f"(module_id={spec_id!r}, id={card_id!r})"
                    )
            if card_id != path.parent.name:
                die(
                    f"Module Card id 必须与父目录名一致：{path} "
                    f"(id={card_id!r}, 目录={path.parent.name!r})"
                )
            previous_dir = seen_ids.get(card_id)
            if previous_dir is not None and previous_dir != resolved.parent:
                die(
                    f"Module Card id 在 run 内必须全局唯一：{card_id!r} "
                    f"同时出现于 {previous_dir} 和 {resolved.parent}"
                )
            seen_ids[card_id] = resolved.parent
            if resolved.parent in seen_dirs:
                continue
            cards.append((path, payload))
            seen_dirs.add(resolved.parent)
    return cards


def require_within_project(path: Path) -> Path:
    """Reject paths outside the harness project root and return the resolved path."""
    resolved = Path(path).expanduser().resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError:
        die(f"路径位于项目根目录之外：{resolved}")
    return resolved


def manifest_for(base: Path, *, excludes: set[str] | None = None) -> list[dict[str, Any]]:
    """Build a deterministic SHA-256 manifest for all files below *base*."""
    records: list[dict[str, Any]] = []
    for path in iter_files(base, exclude_names=excludes):
        records.append(
            {
                "path": relative_to_root(path),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return records


def next_id(rows: Iterable[dict[str, Any]], prefix: str, width: int = 3) -> str:
    """Return PREFIX_001-style next id from a sequence of JSON objects."""
    existing = [str(row.get("id", "")) for row in rows]
    return next_numeric_id(prefix, existing, width=width)


def set_active_run(run_id: str) -> None:
    """Compatibility name used by the command-line manager."""
    write_active(run_id)


def append_ai_usage_event(
    run: Path,
    *,
    tool: str,
    model: str,
    stage: str,
    purpose: str,
    prompt_or_task_summary: str,
    output_artifacts: Sequence[str] | None = None,
    input_artifacts: Sequence[str] | None = None,
    adopted: bool = False,
    human_modified: bool = False,
    human_verified: bool = False,
    verified_by: str = "",
    notes: str = "",
    event_id: str | None = None,
) -> str:
    """Append one normalized AI-use event and return its audit id."""
    verifier = str(verified_by or "").strip()
    if human_verified and not is_named_human_identity(verifier):
        die(
            "human_verified=true 的 AI 使用事件必须通过 verified_by "
            "记录具名队员，不能使用 TEAM/UNKNOWN/TODO/AUTO/CODEX 等占位或机器身份"
        )
    if adopted and not human_verified:
        die("adopted=true 的 AI 使用事件必须先由具名队员完成人工核验")
    path = run / "ai" / "AI_USAGE_LOG.jsonl"
    rows = read_jsonl(path)
    existing = [str(row.get("event_id") or row.get("id") or "") for row in rows]
    ident = event_id or next_numeric_id("AI", existing)
    payload = {
        "event_id": ident,
        "timestamp": now_iso(),
        "tool": tool,
        "model": model,
        "stage": stage,
        "purpose": purpose,
        "prompt_or_task_summary": prompt_or_task_summary,
        "input_artifacts": list(input_artifacts or []),
        "output_artifacts": list(output_artifacts or []),
        "adopted": bool(adopted),
        "human_modified": bool(human_modified),
        "human_verified": bool(human_verified),
        "verified_by": verifier if human_verified else "",
        "notes": notes,
    }
    append_jsonl(path, payload)
    return ident
