#!/usr/bin/env python3
"""Build and audit an anonymous, explicit official supporting-material ZIP.

This is intentionally separate from ``harness.py package``.  The latter is an
internal evidence/audit bundle; this module creates only the small allowlisted
archive that may be submitted as competition supporting material.  Its named
receipt stays outside the ZIP.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import unicodedata
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from common import (
    is_named_human_identity,
    is_timezone_aware_iso,
    now_iso,
    pdf_envelope_errors,
    sha256_file,
)


DEFAULT_MAX_BYTES = 20_000_000
DEFAULT_OUTPUT_RELATIVE = "deliverables/official/支撑材料.zip"
DEFAULT_RECEIPT_RELATIVE = "deliverables/internal/SUPPORT_PACKAGE_RECEIPT.json"
AI_DISCLOSURE_RELATIVE = "deliverables/AI工具使用详情.pdf"
AI_DISCLOSURE_SOURCE_RELATIVE = "ai/AI_TOOL_USAGE_DETAILS.md"
AI_DISCLOSURE_RECEIPT_RELATIVE = "ai/AI_DISCLOSURE_PDF_RECEIPT.json"
AI_ARCHIVE_NAME = "AI工具使用详情.pdf"
FINAL_MANUSCRIPT_RELATIVE = "deliverables/FINAL_MANUSCRIPT.pdf"
POLICY_VERSION = 1

_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "policy_version",
        "run_id",
        "generated_at",
        "generated_by",
        "archive_path",
        "receipt_path",
        "archive_size",
        "archive_sha256",
        "max_bytes",
        "ai_required",
        "ai_attestation_path",
        "ai_attestation_sha256",
        "known_identities",
        "known_identity_count",
        "known_identity_sha256",
        "files",
        "warnings",
    }
)
_FILE_RECORD_FIELDS = frozenset(
    {"source_path", "archive_path", "size", "sha256"}
)
_INTERNAL_EXACT = frozenset(
    {
        "RUN_META.json",
        "INPUT_MANIFEST.json",
        "STATUS.json",
        "V51_STATUS.json",
        "FROZEN_MANIFEST.json",
        "VALIDATION_REPORT.json",
        "deliverables/DELIVERY_MANIFEST.json",
        "deliverables/PAPER_COMPILE_RECEIPT.json",
        "deliverables/AI_DISCLOSURE_PDF_RECEIPT.json",
        AI_DISCLOSURE_RECEIPT_RELATIVE,
        "ai/AI_USAGE_LOG.jsonl",
        "ai/AI_EXPORT_RECEIPT.json",
        "ai/AI_TOOL_USAGE_DETAILS.md",
    }
)
_INTERNAL_PREFIXES = (
    "decisions/",
    "reviews/",
    "modes/",
    "prompts/",
    ".harness/",
    "deliverables/packages/",
    "deliverables/internal/",
)
_INTERNAL_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".svn",
        ".hg",
        ".harness",
        ".codex",
        ".claude",
        ".agents",
        ".aws",
        ".ssh",
        ".gnupg",
        "decisions",
        "reviews",
        "modes",
        "prompts",
    }
)
_SECRET_EXACT_NAMES = frozenset(
    {
        ".env",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        "credentials.json",
        "credential.json",
        "credentials",
        "secrets.json",
        "secret.json",
        "id_rsa",
        "id_ed25519",
        "known_hosts",
        "authorized_keys",
        ".netrc",
    }
)
_SECRET_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"})
_TEXT_SUFFIXES = frozenset(
    {
        ".txt",
        ".md",
        ".csv",
        ".tsv",
        ".json",
        ".jsonl",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".py",
        ".pyw",
        ".r",
        ".m",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".java",
        ".js",
        ".ts",
        ".sql",
        ".tex",
        ".xml",
        ".html",
        ".htm",
        ".sh",
        ".ps1",
        ".bat",
        ".ipynb",
    }
)
_GENERIC_AUTHOR_RE = re.compile(
    r"(?im)^\s*(?:[#/*;%-]+\s*)?(?:['\"])?"
    r"(author|authors|作者|affiliation|affiliations|单位|学校|institution|institute)"
    r"(?:['\"])?\s*[:=]\s*(?!anonymous\b|匿名\b|none\b|null\b)(\S.+?)\s*$"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?im)(?<![A-Za-z0-9])(?:[A-Za-z0-9]+[_-])*"
    r"(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"aws[_-]?secret[_-]?access[_-]?key|secret|password|passwd)"
    r"\s*[:=]\s*['\"]?[^\s'\"]{6,}"
)
_TOKEN_SECRET_RE = re.compile(
    r"(?i)(?:"
    r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])ghp_[A-Za-z0-9]{30,}(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])glpat-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{20,}(?![A-Za-z0-9])|"
    r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])|"
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
    r")"
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
)

_AI_ATTESTATION_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "attested_at",
        "attested_by",
        "human_visual_reviewed",
        "source_path",
        "source_sha256",
        "pdf_path",
        "pdf_size",
        "pdf_sha256",
        "note",
    }
)


class SupportPackageError(ValueError):
    """Raised when an official support archive cannot be built safely."""


def _require_bool(value: Any, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise SupportPackageError(f"{label} must be boolean")
    return value


def _require_max_bytes(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SupportPackageError("max_bytes must be a positive integer")
    if value > DEFAULT_MAX_BYTES:
        raise SupportPackageError(
            f"max_bytes may not exceed the official {DEFAULT_MAX_BYTES}-byte limit"
        )
    return value


def _canonical_relative(
    run_root: Path,
    value: Any,
    *,
    label: str,
    must_exist: bool,
) -> tuple[Path, str]:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise SupportPackageError(f"{label} must be a non-empty canonical run-relative path")
    text = value
    if "\\" in text:
        raise SupportPackageError(f"{label} is not a canonical POSIX run-relative path: {text}")
    pure = PurePosixPath(text)
    if (
        pure.is_absolute()
        or Path(text).is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
        # Reject drive syntax and NTFS alternate data streams in every path
        # component.  ADS paths can otherwise look run-local while referring
        # to a hidden stream and also create unsafe ZIP member names.
        or any(":" in part for part in pure.parts)
    ):
        raise SupportPackageError(f"{label} is not a canonical run-relative path: {text}")
    candidate = run_root.joinpath(*pure.parts)
    lexical = Path(os.path.abspath(str(candidate)))
    try:
        resolved = candidate.resolve()
        resolved_relative = resolved.relative_to(run_root).as_posix()
    except (OSError, ValueError) as exc:
        raise SupportPackageError(f"{label} is not canonical within the run: {text}: {exc}") from exc
    if lexical != resolved or resolved_relative != text:
        raise SupportPackageError(f"{label} is a symlink or non-canonical path: {text}")
    if must_exist:
        if not resolved.is_file():
            raise SupportPackageError(f"{label} is missing or not a file: {text}")
        try:
            link_count = resolved.stat().st_nlink
        except OSError as exc:
            raise SupportPackageError(f"{label} cannot be inspected: {text}: {exc}") from exc
        if link_count != 1:
            raise SupportPackageError(
                f"{label} is a hardlink and is not an independent run-local file: {text}"
            )
    return resolved, text


def _canonical_output_paths(
    run_root: Path,
    output_relative: Any,
    receipt_relative: Any,
    *,
    must_exist: bool,
) -> tuple[Path, str, Path, str]:
    output, output_text = _canonical_relative(
        run_root, output_relative, label="support archive output", must_exist=must_exist
    )
    receipt, receipt_text = _canonical_relative(
        run_root, receipt_relative, label="support archive receipt", must_exist=must_exist
    )
    if (
        not output_text.startswith("deliverables/official/")
        or output.suffix.lower() != ".zip"
    ):
        raise SupportPackageError(
            "support archive output must be a canonical deliverables/official/*.zip path"
        )
    if (
        not receipt_text.startswith("deliverables/internal/")
        or receipt.suffix.lower() != ".json"
    ):
        raise SupportPackageError(
            "support archive receipt must be a canonical deliverables/internal/*.json path"
        )
    if output == receipt or output_text == receipt_text:
        raise SupportPackageError("support archive output and receipt must use distinct canonical paths")
    return output, output_text, receipt, receipt_text


def _path_policy_error(relative: str) -> str | None:
    folded = relative.casefold()
    name = PurePosixPath(relative).name.casefold()
    parts = {part.casefold() for part in PurePosixPath(relative).parts[:-1]}
    suffix = PurePosixPath(relative).suffix.casefold()
    exact = {item.casefold() for item in _INTERNAL_EXACT}
    if (
        folded in exact
        or any(folded.startswith(prefix.casefold()) for prefix in _INTERNAL_PREFIXES)
        or parts.intersection(_INTERNAL_DIRECTORY_NAMES)
        or name in {"status.json", "v51_status.json", "run_meta.json", "frozen_manifest.json"}
    ):
        return f"internal audit artifact is forbidden in official support material: {relative}"
    if folded == FINAL_MANUSCRIPT_RELATIVE.casefold():
        return f"the manuscript PDF must be submitted separately, not inside support material: {relative}"
    if name in _SECRET_EXACT_NAMES or name.startswith(".env.") or suffix in _SECRET_SUFFIXES:
        return f"secret-bearing path is forbidden in official support material: {relative}"
    data_like = suffix in {".json", ".jsonl", ".csv", ".tsv", ".md", ".txt", ".yaml", ".yml"}
    if data_like and re.search(r"(?:^|[_-])(?:receipt|manifest)(?:[_\-.]|$)", name):
        return f"internal receipt/manifest is forbidden in official support material: {relative}"
    if suffix == ".log" or (
        data_like and re.search(r"(?:^|[_-])log(?:[_\-.]|$)", name)
    ):
        return f"internal log is forbidden in official support material: {relative}"
    return None


def _normalise_identities(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise SupportPackageError("known_identities must be an iterable of identity strings")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise SupportPackageError("known_identities contains an empty/non-string token")
        token = re.sub(r"\s+", " ", value.strip())
        compact = re.sub(r"\s+", "", token)
        # The caller supplied this list explicitly, so a one-character Chinese
        # surname or a two-character Latin name is still meaningful.  Only a
        # single ASCII character is too ambiguous for a blocking scan.
        if not compact or (compact.isascii() and len(compact) < 2):
            raise SupportPackageError(
                f"known identity token is too short for high-confidence scanning: {value!r}"
            )
        key = token.casefold()
        if key not in seen:
            seen.add(key)
            result.append(token)
    return tuple(sorted(result, key=str.casefold))


def _identity_digest(identities: Sequence[str]) -> str:
    payload = json.dumps(
        [item.casefold() for item in identities],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _identity_in_text(value: str, identities: Sequence[str]) -> str | None:
    folded = value.casefold()
    for identity in identities:
        needle = identity.casefold()
        if needle.isascii() and re.fullmatch(r"[a-z0-9_.@+\- ]+", needle):
            if re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", folded):
                return identity
        elif needle in folded:
            return identity
    return None


def _decode_text(payload: bytes) -> str:
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ("utf-16", "utf-8-sig", "gb18030")
    elif payload and payload.count(b"\x00") * 5 >= len(payload):
        # UTF-16 without a BOM is common in Windows-authored support files.
        even_nuls = payload[0::2].count(0)
        odd_nuls = payload[1::2].count(0)
        encodings = (
            ("utf-16-le", "utf-16-be")
            if odd_nuls >= even_nuls
            else ("utf-16-be", "utf-16-le")
        ) + ("utf-8-sig", "gb18030")
    else:
        encodings = ("utf-8-sig", "gb18030", "utf-16")
    for encoding in encodings:
        try:
            return payload.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("utf-8", errors="replace")


def _portable_member_key(value: str) -> str:
    """Fold names that collide on common submission/extraction platforms."""
    return unicodedata.normalize("NFC", value).casefold()


def _ai_attestation_errors(run_root: Path) -> tuple[list[str], str]:
    """Validate and hash the typed, human-reviewed AI disclosure receipt."""
    errors: list[str] = []
    resolved: dict[str, Path] = {}
    for label, relative in (
        ("AI disclosure Markdown", AI_DISCLOSURE_SOURCE_RELATIVE),
        ("AI disclosure PDF", AI_DISCLOSURE_RELATIVE),
        ("AI disclosure attestation", AI_DISCLOSURE_RECEIPT_RELATIVE),
    ):
        try:
            path, _ = _canonical_relative(
                run_root, relative, label=label, must_exist=True
            )
        except SupportPackageError as exc:
            errors.append(str(exc))
        else:
            resolved[relative] = path

    source = resolved.get(AI_DISCLOSURE_SOURCE_RELATIVE)
    pdf = resolved.get(AI_DISCLOSURE_RELATIVE)
    receipt_path = resolved.get(AI_DISCLOSURE_RECEIPT_RELATIVE)
    if source is not None:
        try:
            if source.stat().st_size <= 0:
                errors.append("AI disclosure Markdown is empty")
        except OSError as exc:
            errors.append(f"AI disclosure Markdown cannot be inspected: {exc}")
    if pdf is not None:
        errors.extend(
            f"AI disclosure PDF envelope: {item}" for item in pdf_envelope_errors(pdf)
        )

    if receipt_path is None:
        return errors, ""
    try:
        attestation_sha256 = sha256_file(receipt_path)
        attestation = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"AI disclosure attestation is invalid: {exc}")
        return errors, ""
    if not isinstance(attestation, dict):
        errors.append("AI disclosure attestation must contain an object")
        return errors, attestation_sha256
    missing = sorted(_AI_ATTESTATION_FIELDS - set(attestation))
    extra = sorted(set(attestation) - _AI_ATTESTATION_FIELDS)
    if missing:
        errors.append(f"AI disclosure attestation lacks fields: {missing}")
    if extra:
        errors.append(f"AI disclosure attestation has unsupported fields: {extra}")
    checks = {
        "schema_version": (
            attestation.get("schema_version") == 1
            and not isinstance(attestation.get("schema_version"), bool)
        ),
        "run_id": attestation.get("run_id") == run_root.name,
        "attested_at": is_timezone_aware_iso(attestation.get("attested_at")),
        "attested_by": is_named_human_identity(attestation.get("attested_by")),
        "human_visual_reviewed": attestation.get("human_visual_reviewed") is True,
        "source_path": (
            attestation.get("source_path") == AI_DISCLOSURE_SOURCE_RELATIVE
        ),
        "pdf_path": attestation.get("pdf_path") == AI_DISCLOSURE_RELATIVE,
        "note": (
            isinstance(attestation.get("note"), str)
            and bool(attestation.get("note", "").strip())
        ),
    }
    errors.extend(
        f"AI disclosure attestation {field} is invalid"
        for field, passed in checks.items()
        if not passed
    )
    if source is None:
        errors.append("AI disclosure attestation cannot bind a missing Markdown export")
    else:
        try:
            if attestation.get("source_sha256") != sha256_file(source):
                errors.append(
                    "AI disclosure attestation does not bind the current Markdown export"
                )
        except OSError as exc:
            errors.append(f"AI disclosure Markdown cannot be hashed: {exc}")
    if pdf is None:
        errors.append("AI disclosure attestation cannot bind a missing PDF")
    else:
        try:
            pdf_size = pdf.stat().st_size
            if (
                isinstance(attestation.get("pdf_size"), bool)
                or attestation.get("pdf_size") != pdf_size
                or attestation.get("pdf_sha256") != sha256_file(pdf)
            ):
                errors.append(
                    "AI disclosure attestation does not bind the current PDF bytes"
                )
        except OSError as exc:
            errors.append(f"AI disclosure PDF cannot be hashed: {exc}")
    return errors, attestation_sha256


def _pdf_metadata_values(path: Path) -> tuple[list[tuple[str, str]], str | None]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size <= 8_000_000:
                payload = handle.read()
            else:
                payload = handle.read(4_000_000)
                handle.seek(max(0, size - 4_000_000))
                payload += b"\n" + handle.read(4_000_000)
    except OSError as exc:
        return [], f"could not read PDF metadata bytes from {path.name}: {exc}"

    values: list[tuple[str, str]] = []
    for match in re.finditer(rb"/Author\s*\(((?:\\.|[^\\)])*)\)", payload, re.DOTALL):
        raw = re.sub(
            rb"\\([nrtbf()\\])",
            lambda item: {
                b"n": b"\n",
                b"r": b"\r",
                b"t": b"\t",
                b"b": b"\b",
                b"f": b"\f",
                b"(": b"(",
                b")": b")",
                b"\\": b"\\",
            }[item.group(1)],
            match.group(1),
        )
        if raw.startswith(b"\xfe\xff"):
            value = raw[2:].decode("utf-16-be", errors="replace")
        else:
            value = _decode_text(raw)
        values.append(("PDF /Author", re.sub(r"\s+", " ", value).strip()))
    for match in re.finditer(rb"/Author\s*<(?!<)([0-9A-Fa-f\s]+)>", payload):
        compact = re.sub(rb"\s+", b"", match.group(1))
        if len(compact) % 2:
            compact += b"0"
        try:
            raw = bytes.fromhex(compact.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            continue
        value = (
            raw[2:].decode("utf-16-be", errors="replace")
            if raw.startswith(b"\xfe\xff")
            else _decode_text(raw)
        )
        values.append(("PDF /Author", re.sub(r"\s+", " ", value).strip()))
    for match in re.finditer(rb"/Author\s*/([^\s/<>{}\[\]()]+)", payload):
        values.append(
            ("PDF /Author", match.group(1).decode("latin-1", errors="replace"))
        )
    for decoded in (
        payload.decode("utf-8", errors="ignore"),
        payload.decode("latin-1", errors="ignore"),
    ):
        for match in re.finditer(
            r"<dc:creator\b[^>]*>.*?<rdf:li\b[^>]*>(.*?)</rdf:li>.*?</dc:creator>",
            decoded,
            re.IGNORECASE | re.DOTALL,
        ):
            value = html.unescape(re.sub(r"<[^>]+>", "", match.group(1)))
            values.append(("PDF XMP dc:creator", re.sub(r"\s+", " ", value).strip()))
    unique: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for field, value in values:
        if not value or value.casefold() in {"anonymous", "匿名", "none", "null", "n/a"}:
            continue
        item = (field, value[:240])
        if item not in seen:
            seen.add(item)
            unique.append(item)
    note = (
        f"PDF metadata scan for {path.name} inspected only the first/last 4,000,000 bytes; "
        "manual inspection remains required"
        if size > 8_000_000
        else None
    )
    return unique, note


def _pdf_searchable_text(
    path: Path,
) -> tuple[list[tuple[int, str]], list[str]]:
    """Extract machine-readable PDF page text when a local reader is present.

    Metadata scanning alone cannot find a participant name printed in the page
    body.  Extraction failures remain explicit manual-review warnings because
    scanned/image-only PDFs are legitimate support artifacts; the G7 checklist
    supplies the required human backstop.
    """
    try:
        try:
            from pypdf import PdfReader  # type: ignore[import-not-found]
        except ImportError:
            from PyPDF2 import PdfReader  # type: ignore[import-not-found,no-redef]
    except ImportError:
        return [], [
            f"support PDF {path.name} searchable text was not scanned because "
            "pypdf/PyPDF2 is unavailable; manual review is required"
        ]
    try:
        reader = PdfReader(str(path), strict=False)
    except Exception as exc:
        return [], [
            f"support PDF {path.name} searchable text could not be opened "
            f"({type(exc).__name__}); manual review is required"
        ]
    pages: list[tuple[int, str]] = []
    warnings: list[str] = []
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            warnings.append(
                f"support PDF {path.name} page {page_number} text extraction failed "
                f"({type(exc).__name__}); manual review is required"
            )
            continue
        if text.strip():
            pages.append((page_number, text))
    if not pages:
        warnings.append(
            f"support PDF {path.name} has no extractable page text; image/OCR content "
            "requires manual review"
        )
    return pages, warnings


def _scan_file(
    path: Path,
    relative: str,
    identities: Sequence[str],
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    path_identity = _identity_in_text(relative, identities)
    if path_identity is not None:
        errors.append(f"known identity token appears in support filename/path: {relative}")

    suffix = path.suffix.casefold()
    if suffix == ".pdf":
        metadata, scope_note = _pdf_metadata_values(path)
        if scope_note:
            warnings.append(scope_note)
        for field, value in metadata:
            identity = _identity_in_text(value, identities)
            if identity is not None:
                errors.append(
                    f"known identity token appears in {field} for support PDF: {relative}"
                )
            else:
                warnings.append(
                    f"support PDF {relative} has non-empty {field}={value!r}; manually verify it is not participant identity"
                )
        pages, extraction_warnings = _pdf_searchable_text(path)
        warnings.extend(extraction_warnings)
        for page_number, text in pages:
            identity = _identity_in_text(text, identities)
            if identity is not None:
                errors.append(
                    "known identity token appears in searchable PDF page text: "
                    f"{relative}#page={page_number}"
                )
            if (
                _SECRET_ASSIGNMENT_RE.search(text)
                or _TOKEN_SECRET_RE.search(text)
                or _PRIVATE_KEY_RE.search(text)
            ):
                errors.append(
                    "probable secret material appears in searchable support PDF: "
                    f"{relative}#page={page_number}"
                )
        return errors, warnings

    if suffix not in _TEXT_SUFFIXES:
        warnings.append(
            f"support file {relative} is binary/container content that was not fully "
            "identity/secret-scanned; manual review is required"
        )
        return errors, warnings
    try:
        payload = path.read_bytes()
    except OSError as exc:
        errors.append(f"could not scan support text file {relative}: {exc}")
        return errors, warnings
    text = _decode_text(payload)
    identity = _identity_in_text(text, identities)
    if identity is not None:
        errors.append(f"known identity token appears in readable support file: {relative}")
    if (
        _SECRET_ASSIGNMENT_RE.search(text)
        or _TOKEN_SECRET_RE.search(text)
        or _PRIVATE_KEY_RE.search(text)
    ):
        errors.append(f"probable secret material appears in readable support file: {relative}")
    for match in _GENERIC_AUTHOR_RE.finditer(text):
        field = match.group(1)
        value = re.sub(r"\s+", " ", match.group(2)).strip()[:120]
        warnings.append(
            f"readable support file {relative} has {field}={value!r}; manually verify third-party attribution versus participant identity"
        )
        if len(warnings) >= 20:
            warnings.append("additional generic author/affiliation fields were not expanded")
            break
    return errors, warnings


def _archive_name(relative: str) -> str:
    return AI_ARCHIVE_NAME if relative == AI_DISCLOSURE_RELATIVE else relative


def _prepare_files(
    run_root: Path,
    includes: Iterable[str],
    *,
    output_relative: str,
    receipt_relative: str,
    ai_required: bool,
    identities: Sequence[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    if isinstance(includes, (str, bytes)):
        raise SupportPackageError("includes must be an iterable of explicit run-relative file paths")
    raw_values = list(includes)
    if ai_required and AI_DISCLOSURE_RELATIVE not in raw_values:
        raw_values.append(AI_DISCLOSURE_RELATIVE)
    if not raw_values:
        raise SupportPackageError("official support archive requires at least one explicit include")

    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_sources: set[str] = set()
    seen_member_keys: set[str] = set()
    for index, value in enumerate(raw_values, start=1):
        path, relative = _canonical_relative(
            run_root,
            value,
            label=f"support include #{index}",
            must_exist=True,
        )
        if relative in {output_relative, receipt_relative}:
            raise SupportPackageError(
                f"support include cannot contain its output or receipt: {relative}"
            )
        if relative in seen_sources:
            raise SupportPackageError(f"duplicate support include: {relative}")
        seen_sources.add(relative)
        policy_error = _path_policy_error(relative)
        if policy_error:
            raise SupportPackageError(policy_error)
        if (
            PurePosixPath(relative).name.casefold() == AI_ARCHIVE_NAME.casefold()
            and relative != AI_DISCLOSURE_RELATIVE
        ):
            raise SupportPackageError(
                "AI disclosure PDF may only come from the canonical path "
                f"{AI_DISCLOSURE_RELATIVE}: {relative}"
            )
        if not ai_required and relative == AI_DISCLOSURE_RELATIVE:
            raise SupportPackageError(
                "canonical AI disclosure PDF cannot be included when ai_required=false"
            )
        member = _archive_name(relative)
        member_pure = PurePosixPath(member)
        if (
            member_pure.is_absolute()
            or any(part in {"", ".", ".."} for part in member_pure.parts)
            or "\\" in member
        ):
            raise SupportPackageError(f"derived archive member is non-canonical: {member}")
        member_key = _portable_member_key(member)
        if member_key in seen_member_keys:
            raise SupportPackageError(f"support files collide at archive member: {member}")
        seen_member_keys.add(member_key)
        scan_errors, scan_warnings = _scan_file(path, relative, identities)
        if scan_errors:
            raise SupportPackageError("; ".join(scan_errors))
        warnings.extend(scan_warnings)
        records.append(
            {
                "source_path": relative,
                "archive_path": member,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
                "_path": path,
            }
        )
    records.sort(key=lambda item: item["archive_path"])
    if ai_required and sum(
        item["source_path"] == AI_DISCLOSURE_RELATIVE
        and item["archive_path"] == AI_ARCHIVE_NAME
        for item in records
    ) != 1:
        raise SupportPackageError("AI use requires exactly one canonical AI工具使用详情.pdf member")
    return records, list(dict.fromkeys(warnings))


def _write_deterministic_zip(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for record in records:
            info = zipfile.ZipInfo(str(record["archive_path"]), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.create_system = 3
            with Path(record["_path"]).open("rb") as source, archive.open(info, "w") as target:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)


def _zip_integrity_errors(
    archive_path: Path,
    records: Sequence[Mapping[str, Any]],
) -> list[str]:
    errors: list[str] = []
    expected = [str(item["archive_path"]) for item in records]
    expected_by_member = {str(item["archive_path"]): item for item in records}
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)):
                errors.append("support ZIP contains duplicate member names")
            portable_names = [_portable_member_key(item) for item in names]
            if len(portable_names) != len(set(portable_names)):
                errors.append(
                    "support ZIP contains names that collide after Unicode/case folding"
                )
            if names != expected:
                errors.append(f"support ZIP member list differs from receipt: expected={expected}, actual={names}")
            corrupt = archive.testzip()
            if corrupt is not None:
                errors.append(f"support ZIP CRC failed for member: {corrupt}")
            for info in infos:
                pure = PurePosixPath(info.filename)
                if (
                    pure.is_absolute()
                    or not pure.parts
                    or any(part in {"", ".", ".."} for part in pure.parts)
                    or "\\" in info.filename
                ):
                    errors.append(f"support ZIP has unsafe member path: {info.filename}")
                    continue
                mode = (info.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    errors.append(f"support ZIP contains a symbolic-link member: {info.filename}")
                    continue
                if mode != stat.S_IFREG:
                    errors.append(
                        f"support ZIP contains a non-regular member: {info.filename}"
                    )
                    continue
                record = expected_by_member.get(info.filename)
                if record is None:
                    continue
                if info.file_size != record.get("size"):
                    errors.append(f"support ZIP member size differs from receipt: {info.filename}")
                digest = hashlib.sha256()
                with archive.open(info, "r") as source:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                if digest.hexdigest() != record.get("sha256"):
                    errors.append(f"support ZIP member hash differs from receipt: {info.filename}")
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, zlib.error) as exc:
        errors.append(f"support ZIP cannot be read or verified: {type(exc).__name__}: {exc}")
    return errors


def _public_records(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: item[key] for key in ("source_path", "archive_path", "size", "sha256")}
        for item in records
    ]


def _receipt_bytes(receipt: Mapping[str, Any]) -> bytes:
    return (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _commit_pair_with_rollback(
    archive_temp: Path,
    output: Path,
    receipt_temp: Path,
    receipt_path: Path,
) -> None:
    """Install both staged files and restore the prior pair on detected failure.

    A filesystem cannot atomically rename two unrelated files as one transaction.
    Each rename is atomic; this rollback closes ordinary second-rename failures.
    A support receipt hash still detects an interruption such as sudden power loss.
    """
    pairs = ((archive_temp, output), (receipt_temp, receipt_path))
    backups: dict[Path, Path | None] = {}
    try:
        for _, destination in pairs:
            if not destination.exists():
                backups[destination] = None
                continue
            backup_fd, backup_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".rollback",
                dir=str(destination.parent),
            )
            os.close(backup_fd)
            backup = Path(backup_name)
            shutil.copy2(destination, backup)
            backups[destination] = backup
        try:
            for staged, destination in pairs:
                os.replace(staged, destination)
        except OSError as exc:
            rollback_errors: list[str] = []
            for _, destination in reversed(pairs):
                backup = backups.get(destination)
                try:
                    if backup is None:
                        if destination.exists():
                            destination.unlink()
                    elif backup.exists():
                        os.replace(backup, destination)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{destination}: {rollback_exc}")
            detail = (
                "; rollback also failed: " + "; ".join(rollback_errors)
                if rollback_errors
                else ""
            )
            raise SupportPackageError(
                f"support package commit failed; prior pair was restored: {exc}{detail}"
            ) from exc
    finally:
        for backup in backups.values():
            if backup is None:
                continue
            try:
                backup.unlink()
            except FileNotFoundError:
                pass


def build_support_archive(
    run: Path,
    includes: Iterable[str],
    output_relative: str = DEFAULT_OUTPUT_RELATIVE,
    receipt_relative: str = DEFAULT_RECEIPT_RELATIVE,
    *,
    generated_by: str,
    ai_required: bool,
    known_identities: Iterable[str] = (),
    max_bytes: int = DEFAULT_MAX_BYTES,
    replace: bool = False,
) -> dict[str, Any]:
    """Build an explicit official ZIP and its external hash receipt.

    The returned object is an audit-style result.  Policy violations raise
    :class:`SupportPackageError`; no prior good output is replaced on a failed
    preflight, ZIP verification or size check.  Final renames are individually
    atomic and rollback restores the previous pair on a detected commit error.
    """
    run_root = Path(run).resolve()
    if not run_root.is_dir():
        raise SupportPackageError(f"run directory does not exist: {run_root}")
    if (
        not isinstance(generated_by, str)
        or generated_by != generated_by.strip()
        or not is_named_human_identity(generated_by)
    ):
        raise SupportPackageError("generated_by must name a human; it is stored only in the internal receipt")
    required = _require_bool(ai_required, label="ai_required")
    maximum = _require_max_bytes(max_bytes)
    if not isinstance(replace, bool):
        raise SupportPackageError("replace must be boolean")
    # The builder is itself an identity-bearing operation.  Persist the exact
    # scan vocabulary in the *internal* receipt so a later audit can rescan
    # against the union of build-time and current identities.  This avoids
    # making an otherwise safe archive stale merely because an unrelated
    # internal approval/log later introduces another reviewer name.
    identities = _normalise_identities(
        (*_normalise_identities(known_identities), generated_by)
    )
    output, output_text, receipt_path, receipt_text = _canonical_output_paths(
        run_root, output_relative, receipt_relative, must_exist=False
    )
    ai_attestation_sha256 = ""
    if required:
        ai_errors, ai_attestation_sha256 = _ai_attestation_errors(run_root)
        if ai_errors:
            raise SupportPackageError("; ".join(ai_errors))
    records, warnings = _prepare_files(
        run_root,
        includes,
        output_relative=output_text,
        receipt_relative=receipt_text,
        ai_required=required,
        identities=identities,
    )
    existing = [path for path in (output, receipt_path) if path.exists()]
    if existing and not replace:
        raise SupportPackageError(
            "support archive output/receipt already exists; pass replace=True: "
            + ", ".join(str(path) for path in existing)
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    archive_fd, archive_temp_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=str(output.parent)
    )
    os.close(archive_fd)
    archive_temp = Path(archive_temp_name)
    receipt_temp: Path | None = None
    try:
        _write_deterministic_zip(archive_temp, records)
        integrity_errors = _zip_integrity_errors(archive_temp, records)
        if integrity_errors:
            raise SupportPackageError("; ".join(integrity_errors))
        archive_size = archive_temp.stat().st_size
        if archive_size <= 0:
            raise SupportPackageError("official support archive is empty")
        if archive_size > maximum:
            raise SupportPackageError(
                f"official support archive size {archive_size} exceeds max_bytes {maximum}"
            )
        archive_sha256 = sha256_file(archive_temp)
        receipt = {
            "schema_version": 1,
            "policy_version": POLICY_VERSION,
            "run_id": run_root.name,
            "generated_at": now_iso(),
            "generated_by": generated_by.strip(),
            "archive_path": output_text,
            "receipt_path": receipt_text,
            "archive_size": archive_size,
            "archive_sha256": archive_sha256,
            "max_bytes": maximum,
            "ai_required": required,
            "ai_attestation_path": (
                AI_DISCLOSURE_RECEIPT_RELATIVE if required else None
            ),
            "ai_attestation_sha256": ai_attestation_sha256,
            "known_identities": list(identities),
            "known_identity_count": len(identities),
            "known_identity_sha256": _identity_digest(identities),
            "files": _public_records(records),
            "warnings": warnings,
        }
        receipt_fd, receipt_temp_name = tempfile.mkstemp(
            prefix=f".{receipt_path.name}.", suffix=".tmp", dir=str(receipt_path.parent)
        )
        os.close(receipt_fd)
        receipt_temp = Path(receipt_temp_name)
        receipt_temp.write_bytes(_receipt_bytes(receipt))
        _commit_pair_with_rollback(
            archive_temp, output, receipt_temp, receipt_path
        )
        receipt_temp = None
    finally:
        for path in (archive_temp, receipt_temp):
            if path is None:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    return {
        "passed": True,
        "errors": [],
        "warnings": warnings,
        "archive_path": output_text,
        "receipt_path": receipt_text,
        "archive_size": archive_size,
        "archive_sha256": archive_sha256,
        "members": [item["archive_path"] for item in _public_records(records)],
        "files": _public_records(records),
    }


def _audit_error_result(
    errors: list[str],
    warnings: list[str],
    *,
    output_relative: Any,
    receipt_relative: Any,
) -> dict[str, Any]:
    return {
        "passed": not errors,
        "errors": errors,
        "warnings": list(dict.fromkeys(warnings)),
        "archive_path": str(output_relative),
        "receipt_path": str(receipt_relative),
        "archive_size": None,
        "archive_sha256": "",
        "members": [],
        "files": [],
    }


def audit_support_archive(
    run: Path,
    output_relative: str = DEFAULT_OUTPUT_RELATIVE,
    receipt_relative: str = DEFAULT_RECEIPT_RELATIVE,
    *,
    ai_required: bool,
    known_identities: Iterable[str] = (),
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    """Reconcile current source files, ZIP bytes/members/CRC and receipt."""
    errors: list[str] = []
    warnings: list[str] = []
    run_root = Path(run).resolve()
    if not run_root.is_dir():
        return _audit_error_result(
            [f"run directory does not exist: {run_root}"],
            [],
            output_relative=output_relative,
            receipt_relative=receipt_relative,
        )
    try:
        required = _require_bool(ai_required, label="ai_required")
        maximum = _require_max_bytes(max_bytes)
        current_identities = _normalise_identities(known_identities)
        output, output_text, receipt_path, receipt_text = _canonical_output_paths(
            run_root, output_relative, receipt_relative, must_exist=True
        )
    except SupportPackageError as exc:
        return _audit_error_result(
            [str(exc)], [], output_relative=output_relative, receipt_relative=receipt_relative
        )

    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return _audit_error_result(
            [f"support archive receipt is invalid: {exc}"],
            [],
            output_relative=output_text,
            receipt_relative=receipt_text,
        )
    if not isinstance(receipt, dict):
        return _audit_error_result(
            ["support archive receipt must contain an object"],
            [],
            output_relative=output_text,
            receipt_relative=receipt_text,
        )
    missing = sorted(_RECEIPT_FIELDS - set(receipt))
    extra = sorted(set(receipt) - _RECEIPT_FIELDS)
    if missing:
        errors.append(f"support archive receipt lacks fields: {missing}")
    if extra:
        errors.append(f"support archive receipt has unsupported fields: {extra}")
    current_ai_attestation_sha256 = ""
    if required:
        ai_errors, current_ai_attestation_sha256 = _ai_attestation_errors(run_root)
        errors.extend(ai_errors)
    raw_stored_identities = receipt.get("known_identities")
    stored_identities: tuple[str, ...] = ()
    if not isinstance(raw_stored_identities, list):
        errors.append("support archive receipt known_identities must be an array")
    else:
        try:
            stored_identities = _normalise_identities(raw_stored_identities)
        except SupportPackageError as exc:
            errors.append(f"support archive receipt known_identities is invalid: {exc}")
        else:
            if list(stored_identities) != raw_stored_identities:
                errors.append(
                    "support archive receipt known_identities is not canonical"
                )
    # Always retain the build-time vocabulary, even if a name later disappears
    # from mutable run metadata; also include newly discovered names.  Every
    # current audit therefore scans at least as strictly as the original build.
    identities = _normalise_identities((*stored_identities, *current_identities))
    checks = {
        "schema_version": receipt.get("schema_version") == 1
        and not isinstance(receipt.get("schema_version"), bool),
        "policy_version": receipt.get("policy_version") == POLICY_VERSION
        and not isinstance(receipt.get("policy_version"), bool),
        "run_id": receipt.get("run_id") == run_root.name,
        "generated_at": is_timezone_aware_iso(receipt.get("generated_at")),
        "generated_by": is_named_human_identity(receipt.get("generated_by")),
        "archive_path": receipt.get("archive_path") == output_text,
        "receipt_path": receipt.get("receipt_path") == receipt_text,
        "max_bytes": receipt.get("max_bytes") == maximum
        and not isinstance(receipt.get("max_bytes"), bool),
        "ai_required": receipt.get("ai_required") is required,
        "ai_attestation_path": receipt.get("ai_attestation_path")
        == (AI_DISCLOSURE_RECEIPT_RELATIVE if required else None),
        "ai_attestation_sha256": receipt.get("ai_attestation_sha256")
        == current_ai_attestation_sha256,
        "known_identity_count": receipt.get("known_identity_count") == len(stored_identities)
        and not isinstance(receipt.get("known_identity_count"), bool),
        "known_identity_sha256": receipt.get("known_identity_sha256")
        == _identity_digest(stored_identities),
    }
    errors.extend(
        f"support archive receipt {field} does not match the current policy/run"
        for field, passed in checks.items()
        if not passed
    )

    try:
        archive_size = output.stat().st_size
        archive_sha256 = sha256_file(output)
    except OSError as exc:
        return _audit_error_result(
            [*errors, f"support archive cannot be inspected: {exc}"],
            warnings,
            output_relative=output_text,
            receipt_relative=receipt_text,
        )
    if archive_size <= 0:
        errors.append("official support archive is empty")
    if archive_size > maximum:
        errors.append(f"official support archive size {archive_size} exceeds max_bytes {maximum}")
    if receipt.get("archive_size") != archive_size or isinstance(receipt.get("archive_size"), bool):
        errors.append("support archive size differs from receipt")
    if receipt.get("archive_sha256") != archive_sha256:
        errors.append("support archive hash differs from receipt")

    raw_records = receipt.get("files")
    records: list[dict[str, Any]] = []
    if not isinstance(raw_records, list) or not raw_records:
        errors.append("support archive receipt files must be a non-empty array")
    else:
        seen_sources: set[str] = set()
        seen_member_keys: set[str] = set()
        receipt_member_order: list[str] = []
        for index, raw_record in enumerate(raw_records, start=1):
            if not isinstance(raw_record, dict):
                errors.append(f"support archive receipt files[{index}] is not an object")
                continue
            if set(raw_record) != _FILE_RECORD_FIELDS:
                errors.append(
                    f"support archive receipt files[{index}] fields are non-canonical"
                )
                continue
            try:
                source, source_relative = _canonical_relative(
                    run_root,
                    raw_record.get("source_path"),
                    label=f"support receipt source #{index}",
                    must_exist=True,
                )
            except SupportPackageError as exc:
                errors.append(str(exc))
                continue
            member = raw_record.get("archive_path")
            if not isinstance(member, str) or member != _archive_name(source_relative):
                errors.append(f"support receipt archive path #{index} is non-canonical")
                continue
            if (
                PurePosixPath(source_relative).name.casefold()
                == AI_ARCHIVE_NAME.casefold()
                and source_relative != AI_DISCLOSURE_RELATIVE
            ):
                errors.append(
                    "support receipt AI PDF source is not the canonical reviewed path: "
                    f"{source_relative}"
                )
            policy_error = _path_policy_error(source_relative)
            if policy_error:
                errors.append(policy_error)
            if source_relative in seen_sources:
                errors.append(f"support receipt duplicates source: {source_relative}")
            member_key = _portable_member_key(member)
            if member_key in seen_member_keys:
                errors.append(f"support receipt duplicates archive member: {member}")
            seen_sources.add(source_relative)
            seen_member_keys.add(member_key)
            receipt_member_order.append(member)
            try:
                source_size = source.stat().st_size
                source_sha256 = sha256_file(source)
            except OSError as exc:
                errors.append(f"support source cannot be inspected: {source_relative}: {exc}")
                continue
            if raw_record.get("size") != source_size or isinstance(raw_record.get("size"), bool):
                errors.append(f"support source size differs from receipt: {source_relative}")
            if raw_record.get("sha256") != source_sha256:
                errors.append(f"support source hash differs from receipt: {source_relative}")
            scan_errors, scan_warnings = _scan_file(source, source_relative, identities)
            errors.extend(scan_errors)
            warnings.extend(scan_warnings)
            records.append(dict(raw_record))

        if receipt_member_order != sorted(receipt_member_order):
            errors.append("support receipt files are not in canonical archive-path order")
    records.sort(key=lambda item: str(item.get("archive_path", "")))
    member_names = [str(item.get("archive_path", "")) for item in records]
    if any(item in {output_text, receipt_text, FINAL_MANUSCRIPT_RELATIVE} for item in member_names):
        errors.append("support ZIP attempts to include its output, receipt or manuscript PDF")
    canonical_ai_records = [
        item
        for item in records
        if item.get("source_path") == AI_DISCLOSURE_RELATIVE
        and item.get("archive_path") == AI_ARCHIVE_NAME
    ]
    if required and len(canonical_ai_records) != 1:
        errors.append("AI use requires exactly one AI工具使用详情.pdf archive member")
    if not required and AI_ARCHIVE_NAME in member_names:
        errors.append("AI工具使用详情.pdf is inconsistent with ai_required=false")
    errors.extend(_zip_integrity_errors(output, records))

    return {
        "passed": not errors,
        "errors": list(dict.fromkeys(errors)),
        "warnings": list(dict.fromkeys(warnings)),
        "archive_path": output_text,
        "receipt_path": receipt_text,
        "archive_size": archive_size,
        "archive_sha256": archive_sha256,
        "members": member_names,
        "files": records,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("build", "audit"):
        item = sub.add_parser(command)
        item.add_argument("--run", required=True)
        item.add_argument("--output", default=DEFAULT_OUTPUT_RELATIVE)
        item.add_argument("--receipt", default=DEFAULT_RECEIPT_RELATIVE)
        item.add_argument("--ai-required", action="store_true")
        item.add_argument("--identity", action="append", default=[])
        item.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
        if command == "build":
            item.add_argument("--include", action="append", default=[])
            item.add_argument("--by", required=True)
            item.add_argument("--replace", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "build":
        try:
            result = build_support_archive(
                Path(args.run),
                args.include,
                args.output,
                args.receipt,
                generated_by=args.by,
                ai_required=args.ai_required,
                known_identities=args.identity,
                max_bytes=args.max_bytes,
                replace=args.replace,
            )
        except SupportPackageError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    else:
        result = audit_support_archive(
            Path(args.run),
            args.output,
            args.receipt,
            ai_required=args.ai_required,
            known_identities=args.identity,
            max_bytes=args.max_bytes,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AI_DISCLOSURE_RELATIVE",
    "AI_DISCLOSURE_RECEIPT_RELATIVE",
    "AI_DISCLOSURE_SOURCE_RELATIVE",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_OUTPUT_RELATIVE",
    "DEFAULT_RECEIPT_RELATIVE",
    "SupportPackageError",
    "audit_support_archive",
    "build_support_archive",
]
