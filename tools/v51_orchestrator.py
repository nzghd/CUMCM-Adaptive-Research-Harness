#!/usr/bin/env python3
"""Non-destructive v5.1 stage overlay for interpretation, mathematics and audit."""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib
import json
from pathlib import Path
from typing import Any

from common import (
    ROOT,
    die,
    is_named_human_identity,
    is_timezone_aware_iso,
    load_config,
    now_iso,
    read_csv,
    read_json,
    sha256_file,
    write_json,
)
from v51_common import resolve_run_flexible

STAGE_REGISTRY_PATH = ROOT / "config" / "v51_stage_registry.json"
HARNESS_CONFIG_PATH = ROOT / "harness_config.json"

# Human approvals bind both the artifacts and the code that decided those
# artifacts were valid.  A validator-policy change must therefore require a
# fresh validation and approval even when the run files themselves did not
# change.
COMMON_VALIDATOR_CONTROLS = {
    Path(__file__).resolve(),
    ROOT / "tools" / "common.py",
    ROOT / "tools" / "v51_common.py",
    ROOT / "tools" / "schema_validate.py",
    HARNESS_CONFIG_PATH,
}
STAGE_VALIDATOR_CONTROLS = {
    "interpretation": {
        ROOT / "tools" / "interpretation_engine.py",
        ROOT / "config" / "interpretation_policy.json",
    },
    "structural_validation": {
        ROOT / "tools" / "sensitivity_engine.py",
        ROOT / "tools" / "harness.py",
        ROOT / "tools" / "run_experiments.py",
    },
    "mathematical_construction": {
        ROOT / "tools" / "math_engine.py",
        ROOT / "config" / "interpretation_policy.json",
    },
    "independent_review": {ROOT / "tools" / "blind_review.py"},
    "strong_claim_audit": {ROOT / "tools" / "claim_guard.py"},
    "consistency": {ROOT / "tools" / "consistency_matrix.py"},
    "paper_extension": {
        ROOT / "tools" / "paper_v5.py", ROOT / "tools" / "paper.py",
        ROOT / "config" / "paper_profile_cumcm.json",
    },
    "submission": {
        ROOT / "tools" / "harness.py", ROOT / "tools" / "paper.py",
        ROOT / "harness_config.json",
        ROOT / "config" / "paper_profile_cumcm.json",
    },
}


def _load_stage_registry() -> dict[str, Any]:
    registry = read_json(STAGE_REGISTRY_PATH, expect=dict)
    stages = registry.get("stages", [])
    if not isinstance(stages, list):
        die(f"v5.1 stage registry has invalid stages: {STAGE_REGISTRY_PATH}")
    ids = [str(item.get("id", "")).strip() for item in stages if isinstance(item, dict)]
    if not ids or any(not stage for stage in ids) or len(ids) != len(set(ids)):
        die(f"v5.1 stage registry has missing or duplicate ids: {STAGE_REGISTRY_PATH}")
    version = str(registry.get("version", "")).strip()
    harness_version = str(load_config().get("version", "")).strip()
    if not version or version != harness_version:
        die(f"v5.1 stage registry version {version!r} does not match harness version {harness_version!r}")
    return registry


_STAGE_REGISTRY = _load_stage_registry()
_STAGE_REGISTRY_SHA256_AT_LOAD = sha256_file(STAGE_REGISTRY_PATH)
_HARNESS_CONFIG_SHA256_AT_LOAD = sha256_file(HARNESS_CONFIG_PATH)
_ORCHESTRATOR_SHA256_AT_LOAD = sha256_file(Path(__file__).resolve())
_VALIDATOR_CONTROL_PATHS = {
    path.resolve()
    for path in (
        *COMMON_VALIDATOR_CONTROLS,
        *(control for controls in STAGE_VALIDATOR_CONTROLS.values() for control in controls),
    )
}
_VALIDATOR_CONTROL_HASHES_AT_LOAD: dict[Path, str | None] = {
    path: (sha256_file(path) if path.is_file() else None)
    for path in _VALIDATOR_CONTROL_PATHS
}
_COMPONENT_SOURCE_HASHES: dict[str, str] = {}
STAGE_VERSION = str(_STAGE_REGISTRY["version"])
STAGES = [str(item["id"]) for item in _STAGE_REGISTRY["stages"]]


def _assert_cached_inputs_current() -> None:
    if sha256_file(STAGE_REGISTRY_PATH) != _STAGE_REGISTRY_SHA256_AT_LOAD:
        die(
            "config/v51_stage_registry.json changed after this process loaded it; "
            "restart/reload before validating or approving stages"
        )
    if sha256_file(HARNESS_CONFIG_PATH) != _HARNESS_CONFIG_SHA256_AT_LOAD:
        die(
            "harness_config.json changed after the v5.1 registry was loaded; "
            "restart/reload before validating or approving stages"
        )
    if sha256_file(Path(__file__).resolve()) != _ORCHESTRATOR_SHA256_AT_LOAD:
        die(
            "tools/v51_orchestrator.py changed while this process is running; "
            "restart/reload before validating or approving stages"
        )
    for path, loaded_hash in sorted(
        _VALIDATOR_CONTROL_HASHES_AT_LOAD.items(), key=lambda item: str(item[0])
    ):
        current_hash = sha256_file(path) if path.is_file() else None
        if current_hash != loaded_hash:
            die(
                f"validator control {path} changed after the orchestrator loaded it; "
                "restart/reload before validating or approving stages"
            )


def _stage_definition(stage: str) -> dict[str, Any]:
    _assert_cached_inputs_current()
    for item in _STAGE_REGISTRY["stages"]:
        if str(item.get("id")) == stage:
            return item
    raise ValueError(stage)


def _expand_input_glob(run: Path, pattern: str) -> list[Path]:
    normalized = pattern.replace("\\", "/")
    if normalized.endswith("/**"):
        base = run / normalized[:-3]
        return [path for path in base.rglob("*") if path.is_file()] if base.exists() else []
    return [path for path in run.glob(normalized) if path.is_file()]


def _stage_input_snapshot(
    run: Path,
    stage: str,
    *,
    extra_paths: list[str] | None = None,
    extra_records: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    definition = _stage_definition(stage)
    excludes = [str(item).replace("\\", "/") for item in definition.get("exclude_globs", [])]
    run_root = run.resolve()
    files: set[Path] = set()
    errors: list[str] = []
    for pattern_value in definition.get("input_globs", []):
        pattern = str(pattern_value)
        for path in _expand_input_glob(run, pattern):
            relative = path.relative_to(run).as_posix()
            if any(fnmatch.fnmatch(relative, excluded) for excluded in excludes):
                continue
            resolved = path.resolve()
            try:
                resolved.relative_to(run_root)
            except ValueError:
                errors.append(f"stage input leaves the run directory: {relative}")
                continue
            files.add(resolved)

    dynamic_paths = list(extra_paths or [])
    if stage == "strong_claim_audit":
        receipt_path = run / "claims" / "STRONG_CLAIM_SCAN.json"
        if receipt_path.is_file():
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                receipt = {}
            if isinstance(receipt, dict):
                dynamic_paths.extend(
                    str(item.get("path", ""))
                    for item in receipt.get("files", [])
                    if isinstance(item, dict) and item.get("path")
                )
    for value in dynamic_paths:
        raw = Path(value).expanduser()
        candidate = (raw if raw.is_absolute() else run / raw).resolve()
        try:
            candidate.relative_to(run_root)
        except ValueError:
            errors.append(f"dynamic stage input leaves the run directory: {value}")
            continue
        if not candidate.is_file():
            errors.append(f"dynamic stage input does not exist: {value}")
            continue
        files.add(candidate)

    records: list[dict[str, Any]] = [
        {
            "kind": "artifact",
            "path": path.relative_to(run_root).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(files)
    ]
    extra_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for raw_record in extra_records or []:
        if not isinstance(raw_record, dict):
            errors.append("dynamic input record is not an object")
            continue
        record = dict(raw_record)
        kind = str(record.get("kind", "")).strip()
        path_text = str(record.get("path", "")).strip().replace("\\", "/")
        digest = str(record.get("sha256", "")).strip()
        if kind not in {"semantic-projection", "source"}:
            errors.append(f"dynamic input record has unsupported kind: {kind or '<empty>'}")
            continue
        if not path_text or len(digest) != 64 or digest.lower() != digest or any(
            char not in "0123456789abcdef" for char in digest
        ):
            errors.append(f"dynamic input record has invalid path/hash: {path_text or '<empty>'}")
            continue
        if kind == "semantic-projection":
            if "#" not in path_text:
                errors.append(f"semantic projection lacks selector: {path_text}")
                continue
            base_text = path_text.split("#", 1)[0]
            base_path = (run / base_text).resolve()
            try:
                base_path.relative_to(run_root)
            except ValueError:
                errors.append(f"semantic projection leaves the run directory: {path_text}")
                continue
            if not base_path.is_file():
                errors.append(f"semantic projection base does not exist: {base_text}")
                continue
            if "row_count" in record and (
                isinstance(record["row_count"], bool)
                or not isinstance(record["row_count"], int)
                or record["row_count"] < 0
            ):
                errors.append(f"semantic projection row_count is invalid: {path_text}")
                continue
        else:
            source_path = (ROOT / path_text).resolve()
            try:
                source_path.relative_to(ROOT.resolve())
            except ValueError:
                errors.append(f"source input leaves the project directory: {path_text}")
                continue
            if not source_path.is_file() or source_path.stat().st_size <= 0:
                errors.append(f"source input does not exist or is empty: {path_text}")
                continue
            if digest != sha256_file(source_path):
                errors.append(f"source input hash changed during validation: {path_text}")
                continue
            if record.get("size") != source_path.stat().st_size:
                errors.append(f"source input size is invalid: {path_text}")
                continue
        key = (kind, path_text)
        if key in extra_by_key and extra_by_key[key] != record:
            errors.append(f"conflicting dynamic input records: {kind}:{path_text}")
            continue
        extra_by_key[key] = record
    records.extend(extra_by_key.values())
    control_files = {
        STAGE_REGISTRY_PATH,
        ROOT / "config" / "artifact_schema_registry.json",
        *COMMON_VALIDATOR_CONTROLS,
        *STAGE_VALIDATOR_CONTROLS.get(stage, set()),
    }
    if stage == "strong_claim_audit":
        control_files.add(ROOT / "config" / "claim_provenance.json")
    schema_registry = ROOT / "config" / "artifact_schema_registry.json"
    if schema_registry.is_file():
        try:
            schema_payload = read_json(schema_registry, expect=dict)
        except SystemExit:
            schema_payload = {}
        for rule in schema_payload.get("artifacts", []):
            if not isinstance(rule, dict) or stage not in [str(item) for item in rule.get("stages", [])]:
                continue
            schema_path = ROOT / str(rule.get("schema", ""))
            if schema_path.is_file():
                control_files.add(schema_path)
            if rule.get("base") == "project":
                control_files.update(
                    path.resolve()
                    for path in ROOT.glob(str(rule.get("glob", "")))
                    if path.is_file()
                )
    for path in sorted(control_files):
        if path.is_file():
            resolved_control = path.resolve()
            try:
                control_relative = resolved_control.relative_to(ROOT.resolve()).as_posix()
            except ValueError:
                errors.append(f"validator control leaves the project directory: {path}")
                continue
            records.append({
                "kind": "control",
                "path": control_relative,
                "size": resolved_control.stat().st_size,
                "sha256": sha256_file(resolved_control),
            })
    records.sort(key=lambda item: (item["kind"], item["path"]))
    encoded = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return records, hashlib.sha256(encoded).hexdigest(), errors


def _reference_tokens(value: str) -> list[str]:
    normalized = str(value or "").replace("|", ";").replace(",", ";")
    return [item.strip() for item in normalized.split(";") if item.strip()]


def _resolve_stage_file(run: Path, value: str, *, local_root: Path | None = None) -> Path | None:
    token = str(value or "").strip().split("#", 1)[0]
    if not token or token.startswith(("http://", "https://")):
        return None
    raw = Path(token).expanduser()
    if raw.is_absolute():
        candidates = [raw]
    else:
        candidates = [base / raw for base in (local_root, run, ROOT) if base is not None]
    run_root = run.resolve()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(run_root)
        except (OSError, ValueError):
            continue
        if resolved.is_file() and resolved.stat().st_size > 0:
            return resolved
    return None


def _provenance_universe_inputs(run: Path) -> set[Path]:
    """Files consulted when math/consistency validators resolve ledger IDs."""
    files: set[Path] = set()
    ledger_path = run / "evidence" / "CLAIM_EVIDENCE_LEDGER.csv"
    source_path = run / "sources" / "SOURCE_LEDGER.csv"
    experiment_path = run / "experiments" / "EXPERIMENT_LOG.csv"
    for table in (ledger_path, source_path, experiment_path):
        if table.is_file():
            files.add(table.resolve())
    if ledger_path.is_file():
        for row in read_csv(ledger_path):
            artifact = _resolve_stage_file(run, str(row.get("evidence_path", "")))
            if artifact is not None:
                files.add(artifact)
    if experiment_path.is_file():
        for row in read_csv(experiment_path):
            if str(row.get("status", "")).strip().lower() != "passed":
                continue
            receipt = _resolve_stage_file(run, str(row.get("output_path", "")))
            if receipt is not None:
                files.add(receipt)
    return files


def _math_direct_reference_inputs(run: Path) -> set[Path]:
    files: set[Path] = set()
    modules_root = run / "research" / "modules"
    if not modules_root.is_dir():
        return files
    table_fields = {
        "EQUATION_REGISTRY.csv": ("derivation_location", "verification_ref"),
        "PARAMETER_REGISTER.csv": ("source_ref",),
        "MODEL_TO_CODE_MAP.csv": ("code_path", "test_path"),
    }
    for module_root in sorted(path for path in modules_root.iterdir() if path.is_dir()):
        for filename, fields in table_fields.items():
            table = module_root / filename
            if not table.is_file():
                continue
            for row in read_csv(table):
                for field in fields:
                    for token in _reference_tokens(str(row.get(field, ""))):
                        artifact = _resolve_stage_file(run, token, local_root=module_root)
                        if artifact is not None:
                            files.add(artifact)
    return files


def _consistency_direct_reference_inputs(run: Path) -> set[Path]:
    files: set[Path] = set()
    matrix = run / "consistency" / "CONSISTENCY_MATRIX.csv"
    if not matrix.is_file():
        return files
    for row in read_csv(matrix):
        for token in _reference_tokens(str(row.get("evidence_ref", ""))):
            artifact = _resolve_stage_file(run, token)
            if artifact is not None:
                files.add(artifact)
    return files


def _referenced_stage_inputs(
    run: Path, stage: str, details: list[dict[str, Any]]
) -> tuple[list[str], list[dict[str, Any]]]:
    """Expand the evidence manifests returned by the stage validators."""
    relative_paths: set[str] = set()
    semantic_records: dict[str, dict[str, Any]] = {}
    for detail in details:
        component_result = detail.get("result", {}) if isinstance(detail, dict) else {}
        if not isinstance(component_result, dict):
            continue
        for item in component_result.get("resolved_evidence", []):
            if not isinstance(item, dict):
                continue
            relative = str(item.get("path", "")).strip()
            if relative:
                relative_paths.add(relative)
            evidence_id = str(item.get("evidence_id", "")).strip()
            row_sha256 = str(item.get("ledger_row_sha256", "")).strip()
            if evidence_id and len(row_sha256) == 64:
                projection_path = (
                    "evidence/CLAIM_EVIDENCE_LEDGER.csv#" + evidence_id
                )
                semantic_records[projection_path] = {
                    "kind": "semantic-projection",
                    "path": projection_path,
                    "sha256": row_sha256,
                }
        for item in component_result.get("input_records", []):
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", "")).strip()
            path_text = str(item.get("path", "")).strip().replace("\\", "/")
            if kind and path_text:
                semantic_records[f"{kind}:{path_text}"] = dict(item)
    if stage == "interpretation":
        for relative in (
            "interpretation/ASSUMPTION_DIVERSITY.json",
            "interpretation/ASSUMPTION_DIVERSITY.md",
        ):
            if (run / relative).is_file():
                relative_paths.add(relative)
    if stage in {"mathematical_construction", "consistency"}:
        referenced_files = _provenance_universe_inputs(run)
        if stage == "mathematical_construction":
            referenced_files.update(_math_direct_reference_inputs(run))
        else:
            referenced_files.update(_consistency_direct_reference_inputs(run))
        relative_paths.update(
            path.relative_to(run.resolve()).as_posix() for path in referenced_files
        )
    return sorted(relative_paths), [
        semantic_records[key] for key in sorted(semantic_records)
    ]


def _state_path(run: Path) -> Path:
    return run / "V51_STATUS.json"


def approval_projection(
    state: dict[str, Any], *, stage_ids: list[str] | None = None
) -> dict[str, Any]:
    """Return the stable, approval-relevant projection of V51_STATUS."""
    _assert_cached_inputs_current()
    selected = STAGES if stage_ids is None else stage_ids
    stages_state = state.get("stages", {}) if isinstance(state, dict) else {}
    projected: list[dict[str, Any]] = []
    for stage_id in selected:
        item = stages_state.get(stage_id, {}) if isinstance(stages_state, dict) else {}
        if not isinstance(item, dict):
            item = {}
        last_result = item.get("last_result") or {}
        if not isinstance(last_result, dict):
            last_result = {}
        last_manifest = last_result.get("input_manifest", [])
        if not isinstance(last_manifest, list):
            last_manifest = []
        manifest_encoded = json.dumps(
            last_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        projected.append({
            "stage": stage_id,
            "status": item.get("status"),
            "approved_input_sha256": item.get("approved_input_sha256"),
            "approved_by": item.get("approved_by"),
            "approved_at": item.get("approved_at"),
            "note": item.get("note", ""),
            "last_result_input_sha256": last_result.get("input_sha256"),
            "last_result_manifest_sha256": hashlib.sha256(manifest_encoded).hexdigest(),
        })
    return {
        "schema_version": 1,
        "stage_registry_sha256": state.get("stage_registry_sha256") if isinstance(state, dict) else None,
        "stages": projected,
    }


def approval_projection_record(
    state: dict[str, Any], *, stage_ids: list[str] | None = None, label: str
) -> dict[str, Any]:
    projection = approval_projection(state, stage_ids=stage_ids)
    encoded = json.dumps(
        projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "kind": "approval-projection",
        "path": label,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _valid_timestamp(value: Any) -> bool:
    return is_timezone_aware_iso(value)


def approved_stage_record_errors(
    stage: str,
    item: dict[str, Any],
    *,
    current_result: dict[str, Any] | None = None,
) -> list[str]:
    """Validate the human approval and its exact stored/current input binding."""
    errors: list[str] = []
    if item.get("status") != "approved":
        return [f"{stage}: stage is not approved"]
    if not is_named_human_identity(item.get("approved_by")):
        errors.append(f"{stage}: approved stage lacks a named approved_by")
    if not _valid_timestamp(item.get("approved_at")):
        errors.append(f"{stage}: approved_at is missing or lacks an ISO-8601 timezone")
    if not _valid_timestamp(item.get("validated_at")):
        errors.append(f"{stage}: validated_at is missing or lacks an ISO-8601 timezone")
    approved_hash = str(item.get("approved_input_sha256") or "").strip()
    if len(approved_hash) != 64 or approved_hash.lower() != approved_hash or any(
        char not in "0123456789abcdef" for char in approved_hash
    ):
        errors.append(f"{stage}: approved_input_sha256 is missing or invalid")
    stored_result = item.get("last_result")
    if not isinstance(stored_result, dict):
        errors.append(f"{stage}: approved stage lacks a stored validation result")
        return errors
    if stored_result.get("stage") != stage:
        errors.append(f"{stage}: stored validation result names a different stage")
    if stored_result.get("passed") is not True or stored_result.get("errors") != []:
        errors.append(f"{stage}: stored validation result is not a clean pass")
    stored_manifest = stored_result.get("input_manifest")
    if not isinstance(stored_manifest, list):
        errors.append(f"{stage}: stored validation input_manifest is not an array")
    if stored_result.get("input_sha256") != approved_hash:
        errors.append(f"{stage}: stored result hash does not match its approval")
    if stored_result.get("stage_registry_sha256") != _STAGE_REGISTRY_SHA256_AT_LOAD:
        errors.append(f"{stage}: stored validation used a different stage registry")
    if current_result is not None:
        if current_result.get("stage") != stage or current_result.get("passed") is not True:
            errors.append(f"{stage}: current strict validation did not pass")
        if current_result.get("input_sha256") != approved_hash:
            errors.append(f"{stage}: inputs changed after approval; re-approve the stage")
        if stored_manifest != current_result.get("input_manifest"):
            errors.append(f"{stage}: stored input manifest differs from current validation")
    return errors


def ensure_state(run: Path, *, persist: bool = True) -> dict[str, Any]:
    _assert_cached_inputs_current()
    path = _state_path(run)
    current_registry_sha256 = _STAGE_REGISTRY_SHA256_AT_LOAD
    if not path.exists():
        timestamp = now_iso()
        state = {
            "version": STAGE_VERSION,
            "run_id": run.name,
            "created_at": timestamp,
            "updated_at": timestamp,
            "stage_registry_sha256": current_registry_sha256,
            "stages": {
                stage: {
                    "status": "pending",
                    "validated_at": None,
                    "approved_at": None,
                    "approved_by": None,
                    "approved_input_sha256": None,
                    "note": "",
                    "last_result": None,
                }
                for stage in STAGES
            },
        }
        if persist:
            write_json(path, state)
        return state

    # Existing status is never repaired in-place.  Silent field insertion or
    # registry-hash replacement would turn a legacy/stale approval into a
    # seemingly current one and bypass the dry-run-first migration workflow.
    state = read_json(path, expect=dict)
    expected_top_fields = {
        "version", "run_id", "created_at", "updated_at",
        "stage_registry_sha256", "stages",
    }
    top_fields = set(state)
    if top_fields != expected_top_fields:
        missing = sorted(expected_top_fields - top_fields)
        extra = sorted(top_fields - expected_top_fields)
        die(
            "V51_STATUS.json is legacy/non-canonical "
            f"(missing={missing}, extra={extra}); run tools/migrate_v51.py first"
        )
    if state.get("version") != STAGE_VERSION:
        die("V51_STATUS.json version is stale; run tools/migrate_v51.py first")
    if state.get("run_id") != run.name:
        die(f"V51_STATUS.json run_id={state.get('run_id')!r} 与当前 run={run.name!r} 不一致")
    if not _valid_timestamp(state.get("created_at")) or not _valid_timestamp(state.get("updated_at")):
        die("V51_STATUS.json created_at/updated_at must be timezone-aware ISO-8601 timestamps")
    if state.get("stage_registry_sha256") != current_registry_sha256:
        die(
            "V51_STATUS.json was created against a different v5.1 stage registry; "
            "run tools/migrate_v51.py and explicitly re-approve affected stages"
        )
    stages_state = state.get("stages")
    if not isinstance(stages_state, dict) or set(stages_state) != set(STAGES):
        missing = sorted(set(STAGES) - set(stages_state or {})) if isinstance(stages_state, dict) else STAGES
        extra = sorted(set(stages_state or {}) - set(STAGES)) if isinstance(stages_state, dict) else []
        die(
            "V51_STATUS.json stage keys do not exactly match the registry "
            f"(missing={missing}, extra={extra}); run tools/migrate_v51.py first"
        )
    expected_stage_fields = {
        "status", "validated_at", "approved_at", "approved_by",
        "approved_input_sha256", "note", "last_result",
    }
    for stage in STAGES:
        item = stages_state[stage]
        if not isinstance(item, dict) or set(item) != expected_stage_fields:
            actual = set(item) if isinstance(item, dict) else set()
            die(
                f"V51_STATUS.json stage {stage!r} is non-canonical "
                f"(missing={sorted(expected_stage_fields - actual)}, "
                f"extra={sorted(actual - expected_stage_fields)}); "
                "run tools/migrate_v51.py first"
            )
    try:
        from schema_validate import validate_json_file

        schema_errors = validate_json_file(path, ROOT / "schemas" / "v51_status.schema.json")
    except (Exception, SystemExit) as exc:
        die(f"V51_STATUS.json schema validator failed: {exc}")
    if schema_errors:
        die(
            "V51_STATUS.json is not canonical; run tools/migrate_v51.py first: "
            + "; ".join(schema_errors)
        )
    lifecycle_errors: list[str] = []
    for stage in STAGES:
        item = stages_state[stage]
        stage_status = item.get("status")
        last_result = item.get("last_result")
        if stage_status == "pending":
            if item.get("validated_at") is not None or last_result is not None:
                lifecycle_errors.append(
                    f"{stage}: pending stage must not retain validated_at/last_result"
                )
            continue
        if not _valid_timestamp(item.get("validated_at")):
            lifecycle_errors.append(
                f"{stage}: {stage_status} stage lacks a timezone-aware validated_at"
            )
        if not isinstance(last_result, dict):
            lifecycle_errors.append(f"{stage}: {stage_status} stage lacks last_result")
            continue
        if last_result.get("stage") != stage:
            lifecycle_errors.append(f"{stage}: last_result names a different stage")
        if not isinstance(last_result.get("passed"), bool):
            lifecycle_errors.append(f"{stage}: last_result.passed is not boolean")
        if not isinstance(last_result.get("errors"), list):
            lifecycle_errors.append(f"{stage}: last_result.errors is not an array")
        if not isinstance(last_result.get("input_manifest"), list):
            lifecycle_errors.append(f"{stage}: last_result.input_manifest is not an array")
        if not (
            isinstance(last_result.get("input_sha256"), str)
            and len(last_result["input_sha256"]) == 64
            and last_result["input_sha256"].lower() == last_result["input_sha256"]
            and all(char in "0123456789abcdef" for char in last_result["input_sha256"])
        ):
            lifecycle_errors.append(f"{stage}: last_result.input_sha256 is invalid")
        if last_result.get("stage_registry_sha256") != current_registry_sha256:
            lifecycle_errors.append(f"{stage}: last_result used a different stage registry")
        if stage_status == "validated" and (
            last_result.get("passed") is not True or last_result.get("errors") != []
        ):
            lifecycle_errors.append(f"{stage}: validated stage is not a clean pass")
        if stage_status == "blocked" and (
            last_result.get("passed") is not False or not last_result.get("errors")
        ):
            lifecycle_errors.append(f"{stage}: blocked stage lacks a failing result/errors")
    if lifecycle_errors:
        die(
            "V51_STATUS.json lifecycle is non-canonical; run tools/migrate_v51.py first: "
            + "; ".join(lifecycle_errors)
        )
    approval_errors = [
        message
        for stage in STAGES
        if stages_state[stage].get("status") == "approved"
        for message in approved_stage_record_errors(stage, stages_state[stage])
    ]
    if approval_errors:
        die("V51_STATUS.json approval records are invalid: " + "; ".join(approval_errors))
    return state


def _init_layers(run: Path) -> None:
    from interpretation_engine import ensure as ensure_interpretation
    from sensitivity_engine import ensure as ensure_sensitivity
    from consistency_matrix import _paths as ensure_consistency
    from paper_v5 import ensure as ensure_paper
    ensure_interpretation(run)
    ensure_sensitivity(run)
    ensure_consistency(run)
    ensure_paper(run)
    (run / "claims").mkdir(parents=True, exist_ok=True)
    (run / "reviews" / "blind").mkdir(parents=True, exist_ok=True)


def cmd_init(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    _init_layers(run)
    state = ensure_state(run)
    print(json.dumps({"run": run.name, "version": state["version"], "stages": list(state["stages"])}, ensure_ascii=False, indent=2))
    return 0


def _run_component_validator(
    module_name: str,
    function_name: str,
    *args: Any,
    **kwargs: Any,
) -> tuple[dict[str, Any], int]:
    """Turn malformed component inputs into an auditable failed result."""
    try:
        module = importlib.import_module(module_name)
        # import_module may return a module cached by unrelated embedding code.
        # Reload once at the validation boundary so the callable necessarily
        # corresponds to the disk bytes snapshotted when this orchestrator was
        # loaded; subsequent control-hash checks reject in-process drift.
        module = importlib.reload(module)
        module_path = Path(str(getattr(module, "__file__", ""))).resolve()
        if not module_path.is_file():
            raise RuntimeError(f"cannot locate validator module source for {module_name}")
        current_source_hash = sha256_file(module_path)
        loaded_source_hash = _COMPONENT_SOURCE_HASHES.setdefault(
            module_name, current_source_hash
        )
        if loaded_source_hash != current_source_hash:
            raise RuntimeError(
                f"{module_name} changed after it was imported; restart/reload the process"
            )
        function = getattr(module, function_name)
        output = function(*args, **kwargs)
        if isinstance(output, tuple) and output and isinstance(output[0], dict):
            result = output[0]
            code = int(output[1]) if len(output) > 1 else 0
        elif isinstance(output, dict):
            result = output
            code = 0
        else:
            raise TypeError(
                f"validator returned {type(output).__name__}, expected dict or (dict, code)"
            )
        return result, code
    except (Exception, SystemExit) as exc:
        message = (
            f"validator exception in {module_name}.{function_name}: "
            f"{type(exc).__name__}: {exc}"
        )
        return {
            "errors": [message],
            "warnings": [],
            "passed": False,
            "validator_exception": message,
        }, 3


def validate_stage(run: Path, stage: str, *, review_id: str | None = None) -> tuple[dict[str, Any], int]:
    _assert_cached_inputs_current()
    errors: list[str] = []
    warnings: list[str] = []
    details: list[dict[str, Any]] = []
    submission_state: dict[str, Any] | None = None
    submission_prior: list[str] = []

    if stage == "interpretation":
        result, code = _run_component_validator(
            "interpretation_engine", "audit", run, strict=True
        )
        details.append({"component": "interpretation_engine", "result": result})
        errors += result.get("errors", [])
        warnings += result.get("warnings", [])

    elif stage == "structural_validation":
        result, code = _run_component_validator(
            "sensitivity_engine", "audit", run, strict=True,
            kinds=("structural",),
        )
        details.append({"component": "sensitivity_engine", "result": result})
        errors += result.get("errors", [])
        warnings += result.get("warnings", [])

    elif stage == "mathematical_construction":
        specs = sorted((run / "research" / "modules").glob("*/MODULE_SPEC.json")) if (run / "research/modules").exists() else []
        if not specs:
            errors.append("No mathematical modules with MODULE_SPEC.json were found.")
        for spec_path in specs:
            module_id = spec_path.parent.name
            result, _ = _run_component_validator(
                "math_engine", "lint_module", run, module_id, strict=True, persist=False
            )
            details.append({"component": "math_engine", "module": module_id, "result": result})
            errors += [f"{module_id}: {x}" for x in result.get("errors", [])]
            warnings += [f"{module_id}: {x}" for x in result.get("warnings", [])]

    elif stage == "independent_review":
        blind_root = run / "reviews" / "blind"
        ids = [review_id] if review_id else (
            [p.name for p in sorted(blind_root.iterdir()) if p.is_dir()]
            if blind_root.is_dir() else []
        )
        if not ids:
            errors.append("No blind-review package/review was found.")
        else:
            passed_any = False
            for rid in ids:
                result, _ = _run_component_validator(
                    "blind_review", "validate", run, rid, strict=True, persist=False
                )
                details.append({"component": "blind_review", "review_id": rid, "result": result})
                if result.get("passed"):
                    passed_any = True
                else:
                    warnings += [f"{rid}: {x}" for x in result.get("errors", [])]
            if not passed_any:
                errors.append("No imported blind review passes independent-review validation.")

    elif stage == "strong_claim_audit":
        registry = run / "claims" / "STRONG_CLAIMS.csv"
        if not registry.exists():
            errors.append("Strong-claim registry missing; run claim_guard.py scan first.")
        else:
            result, _ = _run_component_validator(
                "claim_guard", "lint", run, strict=True, persist=False
            )
            details.append({"component": "claim_guard", "result": result})
            errors += result.get("errors", [])
            warnings += result.get("warnings", [])

    elif stage == "consistency":
        result, _ = _run_component_validator(
            "consistency_matrix", "derive", run, strict=True, persist=False
        )
        details.append({"component": "consistency_matrix", "result": result})
        errors += result.get("errors", [])
        warnings += result.get("warnings", [])

    elif stage == "paper_extension":
        result, _ = _run_component_validator(
            "paper_v5", "validate_paper_extension", run, strict=True
        )
        details.append({"component": "paper_v5", "result": result})
        errors += result.get("errors", [])
        warnings += result.get("warnings", [])

    elif stage == "submission":
        if not _state_path(run).is_file():
            errors.append("V51_STATUS.json missing; initialize and approve all stages first")
            state = {"stages": {stage_id: {} for stage_id in STAGES}}
        else:
            try:
                state = ensure_state(run, persist=False)
            except (Exception, SystemExit):
                errors.append("V51_STATUS.json is invalid or incompatible with the stage registry")
                state = {"stages": {stage_id: {} for stage_id in STAGES}}
        prior = [s for s in STAGES if s != "submission"]
        submission_state = state
        submission_prior = prior
        unapproved = [s for s in prior if state["stages"][s].get("status") != "approved"]
        if unapproved:
            errors.append("v5.1 stages not approved: " + ", ".join(unapproved))
        for prior_stage in prior:
            prior_item = state["stages"].get(prior_stage, {})
            prior_review_id = None
            if prior_stage == "independent_review":
                last_result = prior_item.get("last_result") or {}
                prior_review_id = last_result.get("review_id")
            prior_result, _ = validate_stage(run, prior_stage, review_id=prior_review_id)
            details.append({
                "component": "v51_stage_revalidation",
                "stage": prior_stage,
                "result": prior_result,
            })
            errors += [f"{prior_stage}: {item}" for item in prior_result.get("errors", [])]
            warnings += [f"{prior_stage}: {item}" for item in prior_result.get("warnings", [])]
            errors += approved_stage_record_errors(
                prior_stage,
                prior_item,
                current_result=prior_result,
            )
        # G7 invokes this submission stage, so recursively validating G7 here
        # would never terminate.  The outer G7 validation owns compliance;
        # submission rechecks the complete scientific/paper gate immediately
        # below it.
        for gate in ("G6",):
            result, _ = _run_component_validator(
                "harness", "validate_gate", run, gate, strict=True
            )
            details.append({"component": "harness", "gate": gate, "result": result})
            errors += [f"Base harness {gate}: {item}" for item in result.get("errors", [])]
            warnings += [f"Base harness {gate}: {item}" for item in result.get("warnings", [])]
            if not result.get("ok"):
                errors.append(f"Base harness {gate} validation failed")
    else:
        raise ValueError(stage)

    try:
        from schema_validate import validate_run as validate_run_schemas
        schema_result = validate_run_schemas(run, stage=stage)
        details.append({"component": "schema_validate", "result": schema_result})
        errors += [f"schema: {item}" for item in schema_result.get("errors", [])]
    except (Exception, SystemExit) as exc:
        errors.append(f"schema: validator exception: {exc}")

    try:
        dynamic_inputs, semantic_inputs = _referenced_stage_inputs(
            run, stage, details
        )
    except (Exception, SystemExit) as exc:
        dynamic_inputs = []
        semantic_inputs = []
        errors.append(
            f"input reference resolution exception: {type(exc).__name__}: {exc}"
        )
    try:
        input_manifest, input_sha256, input_errors = _stage_input_snapshot(
            run,
            stage,
            extra_paths=dynamic_inputs,
            extra_records=semantic_inputs,
        )
        errors += [f"input: {item}" for item in input_errors]
    except (Exception, SystemExit) as exc:
        input_manifest = []
        errors.append(f"input snapshot exception: {type(exc).__name__}: {exc}")
        input_sha256 = hashlib.sha256(
            json.dumps(
                {"stage": stage, "snapshot_failed": True},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    if stage == "submission" and submission_state is not None:
        input_manifest.append(approval_projection_record(
            submission_state,
            stage_ids=submission_prior,
            label="V51_STATUS.json#prior-approval-projection",
        ))
        input_manifest.sort(key=lambda item: (item["kind"], item["path"]))
        encoded = json.dumps(
            input_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        input_sha256 = hashlib.sha256(encoded).hexdigest()

    result = {
        "stage": stage, "generated_at": now_iso(), "review_id": review_id,
        "errors": errors, "warnings": warnings, "details": details,
        "passed": not errors,
        "input_manifest": input_manifest,
        "input_sha256": input_sha256,
        "stage_registry_sha256": _STAGE_REGISTRY_SHA256_AT_LOAD,
        "note": "Passing a stage validates recorded artifacts and controls; it does not guarantee a correct model or award outcome.",
    }
    return result, (0 if not errors else 3)


def _invalidate_downstream(
    state: dict[str, Any], stage: str, *, note: str
) -> None:
    """Clear every downstream result/approval after an upstream transition."""
    index = STAGES.index(stage)
    for downstream in STAGES[index + 1:]:
        item = state["stages"][downstream]
        if item.get("status") != "pending" or item.get("last_result") is not None:
            item.update({
                "status": "pending",
                "validated_at": None,
                "approved_at": None,
                "approved_by": None,
                "approved_input_sha256": None,
                "note": note,
                "last_result": None,
            })


def cmd_validate(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    state = ensure_state(run)
    result, code = validate_stage(run, args.stage, review_id=args.review_id)
    item = state["stages"][args.stage]
    was_approved = item.get("status") == "approved"
    approved_input_sha256 = item.get("approved_input_sha256")
    item["last_result"] = result
    item["validated_at"] = now_iso()
    current_input_sha256 = result.get("input_sha256")
    if not result["passed"]:
        item.update({
            "status": "blocked", "approved_at": None, "approved_by": None,
            "approved_input_sha256": None,
            "note": "validation failed after approval" if was_approved else item.get("note", ""),
        })
    elif was_approved and approved_input_sha256 == current_input_sha256:
        # A read-only revalidation of unchanged inputs must not silently revoke a
        # human approval.  This is important because base gates re-run stages.
        item["status"] = "approved"
    elif was_approved:
        item.update({
            "status": "validated", "approved_at": None, "approved_by": None,
            "approved_input_sha256": None,
            "note": "inputs changed after approval; re-approval required",
        })
    else:
        item.update({
            "status": "validated", "approved_at": None, "approved_by": None,
            "approved_input_sha256": None,
        })
    if item.get("status") != "approved":
        _invalidate_downstream(
            state,
            args.stage,
            note=f"invalidated because upstream stage {args.stage} is not approved",
        )
    state["updated_at"] = now_iso()
    write_json(_state_path(run), state)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


def cmd_approve(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    reviewer = str(args.by or "").strip()
    if not is_named_human_identity(reviewer):
        die("v5.1 stage approval requires a named --by reviewer")
    state = ensure_state(run)
    result, code = validate_stage(run, args.stage, review_id=args.review_id)
    if code:
        item = state["stages"][args.stage]
        item.update({
            "status": "blocked",
            "validated_at": now_iso(),
            "approved_at": None,
            "approved_by": None,
            "approved_input_sha256": None,
            "note": "approval blocked because current validation failed",
            "last_result": result,
        })
        _invalidate_downstream(
            state,
            args.stage,
            note=f"invalidated because upstream stage {args.stage} approval failed",
        )
        state["updated_at"] = now_iso()
        write_json(_state_path(run), state)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    item = state["stages"][args.stage]
    item.update({
        "status": "approved", "validated_at": now_iso(), "approved_at": now_iso(),
        "approved_by": reviewer, "approved_input_sha256": result["input_sha256"],
        "note": args.note or "", "last_result": result,
    })
    # Changing/approving an upstream stage invalidates downstream approvals.
    _invalidate_downstream(state, args.stage, note="invalidated by upstream approval")
    state["updated_at"] = now_iso()
    write_json(_state_path(run), state)
    print(f"Approved v5.1 stage {args.stage} by {reviewer}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    run = resolve_run_flexible(args.run)
    state = ensure_state(run)
    print("stage                       status      approved_by       validated_at")
    print("--------------------------  ----------  ----------------  -------------------------")
    for stage in STAGES:
        item = state["stages"][stage]
        print(f"{stage:<26}  {item.get('status',''):<10}  {str(item.get('approved_by') or ''):<16}  {str(item.get('validated_at') or '')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init"); p.add_argument("--run"); p.set_defaults(func=cmd_init)
    p = sub.add_parser("validate"); p.add_argument("--run"); p.add_argument("--stage", choices=STAGES, required=True); p.add_argument("--review-id"); p.set_defaults(func=cmd_validate)
    p = sub.add_parser("approve"); p.add_argument("--run"); p.add_argument("--stage", choices=STAGES, required=True); p.add_argument("--review-id"); p.add_argument("--by", required=True); p.add_argument("--note"); p.set_defaults(func=cmd_approve)
    p = sub.add_parser("status"); p.add_argument("--run"); p.set_defaults(func=cmd_status)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
