#!/usr/bin/env python3
"""Validate configured run artifacts against the repository JSON Schemas.

The core harness intentionally has no third-party runtime dependency.  This
module therefore implements the small, fail-closed JSON Schema subset used by
``schemas/*.json``.  Unsupported validation keywords are reported instead of
being silently ignored.  CSV schemas describe one ``DictReader`` row; their
headers are checked even when the table contains zero data rows.
"""
from __future__ import annotations

import argparse
import csv
import importlib
import json
import math
import re
from pathlib import Path
from typing import Any

from common import ROOT, now_iso, read_json, write_json
from v51_common import resolve_run_flexible

REGISTRY_PATH = ROOT / "config" / "artifact_schema_registry.json"
ANNOTATION_KEYS = {"$schema", "$id", "$defs", "title", "description", "default", "examples"}
VALIDATION_KEYS = {
    "$ref", "type", "const", "enum", "oneOf", "allOf", "anyOf", "if", "then", "else",
    "required", "properties",
    "additionalProperties", "items", "minItems", "maxItems", "uniqueItems",
    "minLength", "maxLength", "pattern", "minimum", "maximum", "minProperties",
    "maxProperties",
}
SUPPORTED_KEYS = ANNOTATION_KEYS | VALIDATION_KEYS


def _join(location: str, part: str | int) -> str:
    if isinstance(part, int):
        return f"{location}[{part}]"
    return f"{location}.{part}" if location else str(part)


def _resolve_ref(root_schema: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise ValueError(f"only local JSON Pointer refs are supported: {reference}")
    node: Any = root_schema
    for raw in reference[2:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise ValueError(f"unresolved JSON Schema ref: {reference}")
        node = node[part]
    if not isinstance(node, dict):
        raise ValueError(f"JSON Schema ref does not resolve to an object: {reference}")
    return node


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    raise ValueError(f"unsupported JSON Schema type: {expected}")


def _schema_shape_errors(schema: Any, root_schema: dict[str, Any], location: str = "$") -> list[str]:
    if not isinstance(schema, dict):
        return [f"{location}: schema node must be an object"]
    errors: list[str] = []
    unsupported = sorted(set(schema) - SUPPORTED_KEYS)
    if unsupported:
        errors.append(f"{location}: unsupported schema keywords: {', '.join(unsupported)}")
    if "$ref" in schema:
        try:
            _resolve_ref(root_schema, str(schema["$ref"]))
        except ValueError as exc:
            errors.append(f"{location}: {exc}")
    for key in ("properties", "$defs"):
        child_map = schema.get(key, {})
        if child_map is not None and not isinstance(child_map, dict):
            errors.append(f"{location}.{key}: must be an object")
        elif isinstance(child_map, dict):
            for name, child in child_map.items():
                errors.extend(_schema_shape_errors(child, root_schema, f"{location}.{key}.{name}"))
    if isinstance(schema.get("items"), dict):
        errors.extend(_schema_shape_errors(schema["items"], root_schema, f"{location}.items"))
    additional = schema.get("additionalProperties")
    if isinstance(additional, dict):
        errors.extend(_schema_shape_errors(additional, root_schema, f"{location}.additionalProperties"))
    for keyword in ("oneOf", "allOf", "anyOf"):
        branches = schema.get(keyword, [])
        if branches is not None and not isinstance(branches, list):
            errors.append(f"{location}.{keyword}: must be an array")
        elif isinstance(branches, list):
            for index, branch in enumerate(branches):
                errors.extend(_schema_shape_errors(branch, root_schema, f"{location}.{keyword}[{index}]"))
    for keyword in ("if", "then", "else"):
        if keyword in schema:
            errors.extend(_schema_shape_errors(schema[keyword], root_schema, f"{location}.{keyword}"))
    return errors


def validate_instance(
    value: Any,
    schema: dict[str, Any],
    *,
    root_schema: dict[str, Any] | None = None,
    location: str = "$",
) -> list[str]:
    """Return all validation errors for one JSON-compatible value."""
    root = root_schema or schema
    if "$ref" in schema:
        try:
            target = _resolve_ref(root, str(schema["$ref"]))
        except ValueError as exc:
            return [f"{location}: {exc}"]
        return validate_instance(value, target, root_schema=root, location=location)

    if "oneOf" in schema:
        branches = schema.get("oneOf")
        if not isinstance(branches, list) or not branches:
            return [f"{location}: oneOf must contain at least one schema"]
        outcomes = [validate_instance(value, branch, root_schema=root, location=location) for branch in branches]
        matches = sum(not outcome for outcome in outcomes)
        if matches != 1:
            details = "; ".join(
                f"branch {index + 1}: {', '.join(outcome[:3]) or 'matched'}"
                for index, outcome in enumerate(outcomes)
            )
            return [f"{location}: oneOf matched {matches} branches ({details})"]

    composition_errors: list[str] = []
    for index, branch in enumerate(schema.get("allOf", [])):
        composition_errors.extend(
            validate_instance(value, branch, root_schema=root, location=f"{location}.allOf[{index}]")
        )
    if "anyOf" in schema:
        branches = schema.get("anyOf")
        if not isinstance(branches, list) or not branches:
            composition_errors.append(f"{location}: anyOf must contain at least one schema")
        else:
            outcomes = [validate_instance(value, branch, root_schema=root, location=location) for branch in branches]
            if not any(not outcome for outcome in outcomes):
                details = "; ".join(
                    f"branch {index + 1}: {', '.join(outcome[:3])}"
                    for index, outcome in enumerate(outcomes)
                )
                composition_errors.append(f"{location}: anyOf matched no branches ({details})")
    conditional = schema.get("if")
    if isinstance(conditional, dict):
        matched_if = not validate_instance(value, conditional, root_schema=root, location=location)
        selected = schema.get("then") if matched_if else schema.get("else")
        if isinstance(selected, dict):
            composition_errors.extend(validate_instance(value, selected, root_schema=root, location=location))

    errors: list[str] = list(composition_errors)
    expected = schema.get("type")
    if expected is not None:
        expected_types = [expected] if isinstance(expected, str) else expected
        if not isinstance(expected_types, list) or not expected_types:
            return [f"{location}: schema type must be a string or non-empty array"]
        try:
            matches_type = any(_type_matches(value, str(item)) for item in expected_types)
        except ValueError as exc:
            return [f"{location}: {exc}"]
        if not matches_type:
            return [f"{location}: expected type {expected_types}, got {type(value).__name__}"]

    if "const" in schema and value != schema["const"]:
        errors.append(f"{location}: expected constant {schema['const']!r}, got {value!r}")
    if "enum" in schema and value not in schema.get("enum", []):
        errors.append(f"{location}: value {value!r} is not in enum {schema.get('enum')!r}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            errors.append(f"{location}: string shorter than minLength={schema['minLength']}")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            errors.append(f"{location}: string longer than maxLength={schema['maxLength']}")
        if "pattern" in schema:
            try:
                matched = re.search(str(schema["pattern"]), value) is not None
            except re.error as exc:
                errors.append(f"{location}: invalid schema pattern: {exc}")
            else:
                if not matched:
                    errors.append(f"{location}: value does not match pattern {schema['pattern']!r}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{location}: value is below minimum={schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{location}: value is above maximum={schema['maximum']}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            errors.append(f"{location}: array shorter than minItems={schema['minItems']}")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append(f"{location}: array longer than maxItems={schema['maxItems']}")
        if schema.get("uniqueItems"):
            encoded = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in value]
            if len(encoded) != len(set(encoded)):
                errors.append(f"{location}: array items are not unique")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(validate_instance(item, item_schema, root_schema=root, location=_join(location, index)))

    if isinstance(value, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if key not in value:
                    errors.append(f"{_join(location, str(key))}: required property is missing")
        properties = schema.get("properties", {})
        properties = properties if isinstance(properties, dict) else {}
        for key, child in properties.items():
            if key in value and isinstance(child, dict):
                errors.extend(validate_instance(value[key], child, root_schema=root, location=_join(location, key)))
        extras = set(value) - set(properties)
        additional = schema.get("additionalProperties", True)
        if additional is False and extras:
            errors.append(f"{location}: unexpected properties: {', '.join(sorted(extras))}")
        elif isinstance(additional, dict):
            for key in sorted(extras):
                errors.extend(validate_instance(value[key], additional, root_schema=root, location=_join(location, key)))
        if "minProperties" in schema and len(value) < int(schema["minProperties"]):
            errors.append(f"{location}: object has fewer than minProperties={schema['minProperties']}")
        if "maxProperties" in schema and len(value) > int(schema["maxProperties"]):
            errors.append(f"{location}: object has more than maxProperties={schema['maxProperties']}")
    return errors


def _load_schema(path: Path) -> tuple[dict[str, Any], list[str]]:
    try:
        schema = read_json(path, expect=dict)
    except SystemExit as exc:
        return {}, [str(exc)]
    return schema, _schema_shape_errors(schema, schema)


def validate_json_file(path: Path, schema_path: Path) -> list[str]:
    schema, errors = _load_schema(schema_path)
    if errors:
        return errors
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"$: invalid JSON: {exc}"]
    return validate_instance(payload, schema)


def validate_csv_file(path: Path, schema_path: Path) -> list[str]:
    schema, errors = _load_schema(schema_path)
    if errors:
        return errors
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            header = list(reader.fieldnames or [])
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        return [f"$: invalid CSV: {exc}"]
    if not header:
        return ["$: CSV header is missing"]
    required = [str(item) for item in schema.get("required", [])]
    missing = [item for item in required if item not in header]
    if missing:
        errors.append("$header: missing required columns: " + ", ".join(missing))
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False and isinstance(properties, dict):
        extra = [item for item in header if item not in properties]
        if extra:
            errors.append("$header: unexpected columns: " + ", ".join(extra))
    if len(header) != len(set(header)):
        errors.append("$header: duplicate columns")
    for index, row in enumerate(rows, start=2):
        errors.extend(validate_instance(row, schema, location=f"$row[{index}]"))
    return errors


def validate_schema_documents() -> list[str]:
    """Validate that every schema parses, uses supported keywords and is registered."""
    errors: list[str] = []
    registry = read_json(REGISTRY_PATH, expect=dict)
    registry_schema = ROOT / "schemas" / "artifact_schema_registry.schema.json"
    errors.extend(
        "config/artifact_schema_registry.json: " + item
        for item in validate_json_file(REGISTRY_PATH, registry_schema)
    )
    harness_config = read_json(ROOT / "harness_config.json", expect=dict)
    if registry.get("schema_version") != 1:
        errors.append("artifact_schema_registry.json schema_version must be 1")
    if registry.get("version") != harness_config.get("version"):
        errors.append(
            "artifact_schema_registry.json version does not match harness_config.json"
        )
    artifacts = registry.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return errors + ["artifact_schema_registry.json artifacts must be a non-empty array"]
    self_rules = [
        item for item in artifacts
        if isinstance(item, dict) and item.get("id") == "artifact-schema-registry-config"
    ]
    expected_self_rule = {
        "id": "artifact-schema-registry-config",
        "base": "project",
        "glob": "config/artifact_schema_registry.json",
        "format": "json",
        "schema": "schemas/artifact_schema_registry.schema.json",
        "stages": ["static", "submission"],
    }
    if self_rules != [expected_self_rule]:
        errors.append(
            "artifact schema registry must contain exactly one canonical "
            "artifact-schema-registry-config trust-root rule"
        )
    rule_ids = [
        str(item.get("id", "")).strip() for item in artifacts if isinstance(item, dict)
    ]
    if len(rule_ids) != len(artifacts) or any(not item for item in rule_ids):
        errors.append("artifact schema registry contains a non-object rule or empty id")
    duplicates = sorted({item for item in rule_ids if rule_ids.count(item) > 1})
    if duplicates:
        errors.append("artifact schema registry has duplicate ids: " + ", ".join(duplicates))
    routes = [
        (str(item.get("base", "")), str(item.get("glob", "")))
        for item in artifacts
        if isinstance(item, dict)
    ]
    duplicate_routes = sorted({route for route in routes if routes.count(route) > 1})
    if duplicate_routes:
        errors.append(
            "artifact schema registry has duplicate base/glob routes: "
            + ", ".join(f"{base}:{glob}" for base, glob in duplicate_routes)
        )
    registered = {str(item.get("schema", "")) for item in registry.get("artifacts", []) if isinstance(item, dict)}
    schemas = sorted((ROOT / "schemas").glob("*.json"))
    for path in schemas:
        _, shape_errors = _load_schema(path)
        errors.extend(f"{path.relative_to(ROOT).as_posix()}: {item}" for item in shape_errors)
        relative = path.relative_to(ROOT).as_posix()
        if relative not in registered:
            errors.append(f"{relative}: schema is not used by artifact_schema_registry.json")
    missing = sorted(item for item in registered if item and not (ROOT / item).is_file())
    errors.extend(f"{item}: registered schema does not exist" for item in missing)
    for rule in registry.get("artifacts", []):
        if not isinstance(rule, dict) or not rule.get("producer_contract"):
            continue
        contract = str(rule["producer_contract"])
        if ":" not in contract:
            errors.append(f"{rule.get('id')}: invalid producer_contract {contract!r}")
            continue
        module_name, attribute = contract.split(":", 1)
        try:
            module = importlib.import_module(module_name)
            producer_fields = list(getattr(module, attribute))
            schema = read_json(ROOT / str(rule.get("schema", "")), expect=dict)
            schema_fields = [str(item) for item in schema.get("required", [])]
        except (Exception, SystemExit) as exc:
            errors.append(f"{rule.get('id')}: cannot load producer contract {contract}: {exc}")
            continue
        if producer_fields != schema_fields:
            errors.append(
                f"{rule.get('id')}: {contract} differs from schema required-column order; "
                f"producer={producer_fields}, schema={schema_fields}"
            )
    return errors


def validate_run(run: Path, *, stage: str | None = None) -> dict[str, Any]:
    registry = read_json(REGISTRY_PATH, expect=dict)
    errors = validate_schema_documents()
    registry_schema_errors = validate_json_file(
        REGISTRY_PATH, ROOT / "schemas" / "artifact_schema_registry.schema.json"
    )
    checked: list[dict[str, Any]] = [{
        "rule_id": "artifact-schema-registry-trust-root",
        "path": "config/artifact_schema_registry.json",
        "schema": "schemas/artifact_schema_registry.schema.json",
        "passed": not registry_schema_errors,
    }]
    for rule in registry.get("artifacts", []):
        if not isinstance(rule, dict):
            errors.append("artifact schema registry contains a non-object rule")
            continue
        stages = [str(item) for item in rule.get("stages", [])]
        if stage and stage not in stages:
            continue
        base = ROOT if rule.get("base") == "project" else run
        pattern = str(rule.get("glob", ""))
        matches = sorted(path for path in base.glob(pattern) if path.is_file())
        schema_path = ROOT / str(rule.get("schema", ""))
        artifact_format = str(rule.get("format", ""))
        for path in matches:
            if artifact_format == "json":
                artifact_errors = validate_json_file(path, schema_path)
            elif artifact_format == "csv":
                artifact_errors = validate_csv_file(path, schema_path)
            else:
                artifact_errors = [f"unsupported artifact format: {artifact_format!r}"]
            relative = path.relative_to(base).as_posix()
            checked.append({
                "rule_id": rule.get("id"),
                "path": relative,
                "schema": rule.get("schema"),
                "passed": not artifact_errors,
            })
            errors.extend(f"{relative}: {item}" for item in artifact_errors)
    return {
        "schema_version": 1,
        "generated_at": now_iso(),
        "run_id": run.name,
        "stage": stage or "all",
        "checked_count": len(checked),
        "checked": checked,
        "errors": errors,
        "passed": not errors,
    }


def validate_static() -> dict[str, Any]:
    """Validate repository schemas, producer contracts, and static config artifacts."""
    registry = read_json(REGISTRY_PATH, expect=dict)
    errors = validate_schema_documents()
    registry_schema_errors = validate_json_file(
        REGISTRY_PATH, ROOT / "schemas" / "artifact_schema_registry.schema.json"
    )
    checked: list[dict[str, Any]] = [{
        "rule_id": "artifact-schema-registry-trust-root",
        "path": "config/artifact_schema_registry.json",
        "schema": "schemas/artifact_schema_registry.schema.json",
        "passed": not registry_schema_errors,
    }]
    for rule in registry.get("artifacts", []):
        if not isinstance(rule, dict):
            errors.append("artifact schema registry contains a non-object rule")
            continue
        if rule.get("base") != "project" or "static" not in [
            str(item) for item in rule.get("stages", [])
        ]:
            continue
        schema_path = ROOT / str(rule.get("schema", ""))
        artifact_format = str(rule.get("format", ""))
        for path in sorted(candidate for candidate in ROOT.glob(str(rule.get("glob", ""))) if candidate.is_file()):
            if artifact_format == "json":
                artifact_errors = validate_json_file(path, schema_path)
            elif artifact_format == "csv":
                artifact_errors = validate_csv_file(path, schema_path)
            else:
                artifact_errors = [f"unsupported artifact format: {artifact_format!r}"]
            relative = path.relative_to(ROOT).as_posix()
            checked.append({
                "rule_id": rule.get("id"),
                "path": relative,
                "schema": rule.get("schema"),
                "passed": not artifact_errors,
            })
            errors.extend(f"{relative}: {item}" for item in artifact_errors)
    return {
        "schema_version": 1,
        "generated_at": now_iso(),
        "run_id": None,
        "stage": "static",
        "checked_count": len(checked),
        "checked": checked,
        "errors": errors,
        "passed": not errors,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", help="run id or path below runs/")
    parser.add_argument("--stage", help="only validate artifact rules mapped to this gate/stage")
    parser.add_argument("--output", help="optional run-relative JSON report path")
    parser.add_argument(
        "--static",
        action="store_true",
        help="validate repository schemas/config/producer contracts without requiring a run",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.static:
        if args.run or args.stage or args.output:
            raise SystemExit("错误：--static 不能与 --run、--stage 或 --output 同时使用")
        result = validate_static()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 3
    run = resolve_run_flexible(args.run)
    result = validate_run(run, stage=args.stage)
    if args.output:
        raw = Path(args.output)
        output = (run / raw).resolve() if not raw.is_absolute() else raw.resolve()
        try:
            output.relative_to(run.resolve())
        except ValueError:
            raise SystemExit("错误：schema report 必须写在当前 run 内")
        write_json(output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
