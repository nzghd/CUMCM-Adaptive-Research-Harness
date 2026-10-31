#!/usr/bin/env python3
"""Focused regression tests for the official supporting-material ZIP."""
from __future__ import annotations

import json
import hashlib
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path


TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import support_package as support_package_module  # noqa: E402
from support_package import (  # noqa: E402
    AI_DISCLOSURE_RELATIVE,
    AI_DISCLOSURE_RECEIPT_RELATIVE,
    AI_DISCLOSURE_SOURCE_RELATIVE,
    SupportPackageError,
    audit_support_archive,
    build_support_archive,
)


OUTPUT = "deliverables/official/支撑材料.zip"
RECEIPT = "deliverables/internal/SUPPORT_PACKAGE_RECEIPT.json"


def _write(run: Path, relative: str, payload: bytes | str) -> Path:
    path = run / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(payload, encoding="utf-8")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_pdf(run: Path, relative: str, *, author: str = "anonymous") -> Path:
    try:
        from pypdf import PdfWriter
    except ImportError:
        from PyPDF2 import PdfWriter  # type: ignore[no-redef]

    path = run / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/Author": author})
    with path.open("wb") as target:
        writer.write(target)
    return path


def _searchable_pdf_bytes(text: str) -> bytes:
    """Return a tiny valid PDF whose uncompressed page text is extractable."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 20 100 Td ({escaped}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode("ascii"))
        payload.extend(obj)
        payload.extend(b"\nendobj\n")
    xref = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(payload)


def _prepare_ai_disclosure(run: Path) -> None:
    source = _write(
        run,
        AI_DISCLOSURE_SOURCE_RELATIVE,
        "# AI 工具使用详情\n\n已由具名队员逐页核验。\n",
    )
    pdf = _write_pdf(run, AI_DISCLOSURE_RELATIVE)
    receipt = {
        "schema_version": 1,
        "run_id": run.name,
        "attested_at": "2026-09-04T12:00:00+08:00",
        "attested_by": "测试复核员",
        "human_visual_reviewed": True,
        "source_path": AI_DISCLOSURE_SOURCE_RELATIVE,
        "source_sha256": _sha256(source),
        "pdf_path": AI_DISCLOSURE_RELATIVE,
        "pdf_size": pdf.stat().st_size,
        "pdf_sha256": _sha256(pdf),
        "note": "具名队员已核验 PDF 可读性及其与 Markdown 源文件的一致性。",
    }
    _write(
        run,
        AI_DISCLOSURE_RECEIPT_RELATIVE,
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
    )


def _expect_error(fragment: str, func, /, *args, **kwargs) -> None:
    try:
        func(*args, **kwargs)
    except SupportPackageError as exc:
        assert fragment.casefold() in str(exc).casefold(), (fragment, str(exc))
    else:
        raise AssertionError(f"expected SupportPackageError containing {fragment!r}")


def _build(
    run: Path,
    includes: list[str],
    *,
    ai_required: bool = False,
    known_identities: tuple[str, ...] = (),
    max_bytes: int = 20_000_000,
    replace: bool = False,
) -> dict:
    return build_support_archive(
        run,
        includes,
        OUTPUT,
        RECEIPT,
        generated_by="测试复核员",
        ai_required=ai_required,
        known_identities=known_identities,
        max_bytes=max_bytes,
        replace=replace,
    )


def main() -> int:
    runs_root = ROOT / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    fixture_root = Path(tempfile.mkdtemp(prefix="_support_package_", dir=runs_root))
    try:
        # Happy path: only explicit files enter the official ZIP.  The internal
        # receipt and final manuscript PDF never become archive members.
        run = fixture_root / "happy"
        _write(run, "code/main.py", "print('ok')\n")
        _write(run, "results/summary.csv", "metric,value\nscore,0.9\n")
        _write(run, "deliverables/FINAL_MANUSCRIPT.pdf", b"%PDF-1.4\n%%EOF\n")
        built = _build(run, ["results/summary.csv", "code/main.py"])
        assert built["passed"], built
        assert not built["errors"], built
        assert (run / OUTPUT).is_file()
        assert (run / RECEIPT).is_file()
        with zipfile.ZipFile(run / OUTPUT) as archive:
            assert archive.namelist() == ["code/main.py", "results/summary.csv"]
            assert archive.testzip() is None
            assert all(
                "测试复核员" not in archive.read(name).decode("utf-8")
                for name in archive.namelist()
            )
        assert RECEIPT not in built["members"]
        assert "deliverables/FINAL_MANUSCRIPT.pdf" not in built["members"]
        audited = audit_support_archive(
            run,
            OUTPUT,
            RECEIPT,
            ai_required=False,
        )
        assert audited["passed"], audited
        receipt_payload = json.loads((run / RECEIPT).read_text(encoding="utf-8"))
        assert receipt_payload["known_identities"] == ["测试复核员"], receipt_payload
        # A newly observed internal reviewer must trigger a fresh rescan, not
        # invalidate a safe archive merely because the scan vocabulary grew.
        expanded_identity_audit = audit_support_archive(
            run,
            OUTPUT,
            RECEIPT,
            ai_required=False,
            known_identities=("新增复核员",),
        )
        assert expanded_identity_audit["passed"], expanded_identity_audit
        copied_receipt = "deliverables/internal/COPIED_SUPPORT_RECEIPT.json"
        _write(run, copied_receipt, (run / RECEIPT).read_bytes())
        misplaced = audit_support_archive(
            run,
            OUTPUT,
            copied_receipt,
            ai_required=False,
        )
        assert not misplaced["passed"] and any(
            "receipt_path" in item for item in misplaced["errors"]
        ), misplaced

        # Existing outputs require an explicit replacement.  A deterministic
        # rebuild has the same archive hash and leaves a current receipt.
        _expect_error("replace", _build, run, ["code/main.py", "results/summary.csv"])
        prior_sha = built["archive_sha256"]
        rebuilt = _build(
            run,
            ["results/summary.csv", "code/main.py"],
            replace=True,
        )
        assert rebuilt["archive_sha256"] == prior_sha, rebuilt

        # The compressed-byte limit is inclusive.  A failed replacement
        # must preserve the last good archive and receipt.
        exact_size = rebuilt["archive_size"]
        boundary = _build(
            run,
            ["code/main.py", "results/summary.csv"],
            max_bytes=exact_size,
            replace=True,
        )
        assert boundary["archive_size"] == exact_size, boundary
        stable_archive = (run / OUTPUT).read_bytes()
        stable_receipt = (run / RECEIPT).read_bytes()
        _expect_error(
            "exceeds",
            _build,
            run,
            ["code/main.py", "results/summary.csv"],
            max_bytes=exact_size - 1,
            replace=True,
        )
        assert (run / OUTPUT).read_bytes() == stable_archive
        assert (run / RECEIPT).read_bytes() == stable_receipt
        _expect_error(
            "official",
            _build,
            run,
            ["code/main.py", "results/summary.csv"],
            max_bytes=20_000_001,
            replace=True,
        )

        # The two independently atomic replacements are guarded by rollback:
        # failure while installing the receipt cannot expose a mismatched pair.
        original_replace = support_package_module.os.replace
        replace_calls = 0

        def fail_second_replace(source, destination):
            nonlocal replace_calls
            replace_calls += 1
            if replace_calls == 2:
                raise OSError("injected receipt replace failure")
            return original_replace(source, destination)

        support_package_module.os.replace = fail_second_replace
        try:
            _expect_error(
                "commit",
                _build,
                run,
                ["code/main.py", "results/summary.csv"],
                replace=True,
            )
        finally:
            support_package_module.os.replace = original_replace
        assert (run / OUTPUT).read_bytes() == stable_archive
        assert (run / RECEIPT).read_bytes() == stable_receipt

        new_pair = fixture_root / "new_pair"
        _write(new_pair, "code/main.py", "print(1)\n")
        replace_calls = 0
        support_package_module.os.replace = fail_second_replace
        try:
            _expect_error("commit", _build, new_pair, ["code/main.py"])
        finally:
            support_package_module.os.replace = original_replace
        assert not (new_pair / OUTPUT).exists()
        assert not (new_pair / RECEIPT).exists()

        # AI use makes the canonical, reviewed disclosure PDF mandatory and it
        # is automatically added at the archive root, not under deliverables/.
        ai_run = fixture_root / "ai"
        _write(ai_run, "code/model.py", "print(1)\n")
        _expect_error("AI", _build, ai_run, ["code/model.py"], ai_required=True)
        _prepare_ai_disclosure(ai_run)
        ai_built = _build(ai_run, ["code/model.py"], ai_required=True)
        assert "AI工具使用详情.pdf" in ai_built["members"], ai_built
        assert AI_DISCLOSURE_RELATIVE not in ai_built["members"], ai_built
        assert audit_support_archive(
            ai_run,
            OUTPUT,
            RECEIPT,
            ai_required=True,
        )["passed"]

        # AI support material is accepted only from the canonical reviewed PDF
        # and only while its typed human-attestation receipt remains current.
        fake_ai = fixture_root / "fake_ai"
        _write(fake_ai, "code/model.py", "print(1)\n")
        _write(fake_ai, AI_DISCLOSURE_SOURCE_RELATIVE, "details\n")
        empty_pdf = _write(fake_ai, AI_DISCLOSURE_RELATIVE, b"")
        _write(
            fake_ai,
            AI_DISCLOSURE_RECEIPT_RELATIVE,
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": fake_ai.name,
                    "attested_at": "2026-09-04T12:00:00+08:00",
                    "attested_by": "测试复核员",
                    "human_visual_reviewed": True,
                    "source_path": AI_DISCLOSURE_SOURCE_RELATIVE,
                    "source_sha256": _sha256(fake_ai / AI_DISCLOSURE_SOURCE_RELATIVE),
                    "pdf_path": AI_DISCLOSURE_RELATIVE,
                    "pdf_size": 0,
                    "pdf_sha256": _sha256(empty_pdf),
                    "note": "reviewed",
                },
                ensure_ascii=False,
            ),
        )
        _expect_error("PDF", _build, fake_ai, ["code/model.py"], ai_required=True)

        stale_ai = fixture_root / "stale_ai"
        _write(stale_ai, "code/model.py", "print(1)\n")
        _prepare_ai_disclosure(stale_ai)
        _build(stale_ai, ["code/model.py"], ai_required=True)
        _write(stale_ai, AI_DISCLOSURE_SOURCE_RELATIVE, "tampered after review\n")
        stale_ai_audit = audit_support_archive(
            stale_ai, OUTPUT, RECEIPT, ai_required=True
        )
        assert not stale_ai_audit["passed"] and any(
            "attestation" in item.casefold() or "markdown" in item.casefold()
            for item in stale_ai_audit["errors"]
        ), stale_ai_audit

        spoofed_ai = fixture_root / "spoofed_ai"
        _write(spoofed_ai, "code/model.py", "print(1)\n")
        _write(spoofed_ai, "AI工具使用详情.pdf", b"not the reviewed canonical file")
        _expect_error(
            "canonical",
            _build,
            spoofed_ai,
            ["code/model.py", "AI工具使用详情.pdf"],
        )

        forged_ai = fixture_root / "forged_ai"
        _write(forged_ai, "code/model.py", "print(1)\n")
        _prepare_ai_disclosure(forged_ai)
        _build(forged_ai, ["code/model.py"], ai_required=True)
        canonical_pdf = forged_ai / AI_DISCLOSURE_RELATIVE
        root_pdf = _write(forged_ai, "AI工具使用详情.pdf", canonical_pdf.read_bytes())
        forged_receipt = json.loads((forged_ai / RECEIPT).read_text(encoding="utf-8"))
        ai_record = next(
            item for item in forged_receipt["files"]
            if item["archive_path"] == "AI工具使用详情.pdf"
        )
        ai_record["source_path"] = "AI工具使用详情.pdf"
        ai_record["size"] = root_pdf.stat().st_size
        ai_record["sha256"] = _sha256(root_pdf)
        _write(
            forged_ai,
            RECEIPT,
            json.dumps(forged_receipt, ensure_ascii=False, indent=2) + "\n",
        )
        forged_ai_audit = audit_support_archive(
            forged_ai, OUTPUT, RECEIPT, ai_required=True
        )
        assert not forged_ai_audit["passed"] and any(
            "canonical" in item.casefold() for item in forged_ai_audit["errors"]
        ), forged_ai_audit

        # Traversal, absolute/non-canonical paths, internal audit state, the
        # paper PDF, receipts/manifests/logs and common secret paths fail closed.
        rejected = fixture_root / "rejected"
        _write(rejected, "code/main.py", "print(1)\n")
        _write(rejected, "STATUS.json", "{}\n")
        _write(rejected, "reviews/private.md", "internal\n")
        _write(rejected, "code/reviews/private.md", "internal\n")
        _write(rejected, "code/BUILD_RECEIPT.json", "{}\n")
        _write(rejected, "code/run.log", "trace\n")
        _write(rejected, ".env", "API_KEY=secret\n")
        _write(rejected, "deliverables/FINAL_MANUSCRIPT.pdf", b"%PDF-1.4\n%%EOF\n")
        for relative, fragment in (
            ("../outside.txt", "canonical"),
            (str((rejected / "code/main.py").resolve()), "canonical"),
            ("code\\main.py", "canonical"),
            ("STATUS.json", "internal"),
            ("reviews/private.md", "internal"),
            ("code/reviews/private.md", "internal"),
            ("code/BUILD_RECEIPT.json", "internal"),
            ("code/run.log", "internal"),
            (".env", "secret"),
            ("deliverables/FINAL_MANUSCRIPT.pdf", "manuscript"),
        ):
            _expect_error(fragment, _build, rejected, [relative])
        _expect_error(
            "canonical",
            build_support_archive,
            rejected,
            ["code/main.py"],
            "../支撑材料.zip",
            RECEIPT,
            generated_by="测试复核员",
            ai_required=False,
        )
        _expect_error(
            "canonical",
            build_support_archive,
            rejected,
            ["code/model.py:stream"],
            OUTPUT,
            RECEIPT,
            generated_by="测试复核员",
            ai_required=False,
        )
        _expect_error(
            "generated_by",
            build_support_archive,
            rejected,
            ["code/main.py"],
            OUTPUT,
            RECEIPT,
            generated_by=123,
            ai_required=False,
        )
        _expect_error(
            "canonical",
            build_support_archive,
            rejected,
            ["code/main.py"],
            OUTPUT,
            "../SUPPORT_PACKAGE_RECEIPT.json",
            generated_by="测试复核员",
            ai_required=False,
        )

        # Link/junction-style redirection is rejected when the platform permits
        # creation of a symlink without elevation.
        outside = _write(fixture_root, "outside.txt", "outside\n")
        link = rejected / "code/link.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pass
        else:
            _expect_error("canonical", _build, rejected, ["code/link.txt"])

        # A hardlink can alias data owned outside the run even though resolve()
        # remains lexically inside it.  Reject it where the platform exposes a
        # link count and permits creating the test fixture.
        hardlink_target = _write(fixture_root, "outside-hardlink.txt", "outside\n")
        hardlink = rejected / "code/hardlink.txt"
        try:
            os.link(hardlink_target, hardlink)
        except OSError:
            pass
        else:
            _expect_error("hardlink", _build, rejected, ["code/hardlink.txt"])

        # Obvious secret material is rejected by content, not only by filename.
        secret_content = fixture_root / "secret_content"
        _write(secret_content, "code/config.txt", "OPENAI_API_KEY=sk-test-secret-value\n")
        _expect_error("secret", _build, secret_content, ["code/config.txt"])
        for index, secret in enumerate(
            (
                "ghp_0123456789abcdefghijklmnopqrstuvwxyz",
                "github_pat_0123456789abcdefghijklmnopqrstuvwxyz",
                "glpat-0123456789abcdefghijklmnopqrstuvwxyz",
                "AKIA0123456789ABCDEF",
                "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature1234567890",
                "-----BEGIN ENCRYPTED PRIVATE KEY-----",
            )
        ):
            path = f"code/secret_{index}.txt"
            _write(secret_content, path, secret + "\n")
            _expect_error("secret", _build, secret_content, [path])
        _write(secret_content, ".npmrc", "//registry/:_authToken=value\n")
        _expect_error("secret", _build, secret_content, [".npmrc"])

        _write(secret_content, "results/opaque.bin", b"\x00\x01\x02")
        opaque = _build(secret_content, ["results/opaque.bin"])
        assert any("manual" in item.casefold() for item in opaque["warnings"]), opaque

        # Known participant identities are high-confidence blockers in paths,
        # readable text and PDF metadata.  Generic third-party author fields are
        # warnings so ordinary source headers do not create hard false positives.
        identity = fixture_root / "identity"
        _write(identity, "code/张三_model.py", "print(1)\n")
        _expect_error("identity", _build, identity, ["code/张三_model.py"], known_identities=("张三",))
        (identity / "code/张三_model.py").unlink()
        _write(identity, "code/model.py", "# 队员：张三\nprint(1)\n")
        _expect_error("identity", _build, identity, ["code/model.py"], known_identities=("张三",))
        _write(identity, "code/Li_model.py", "print(1)\n")
        _expect_error(
            "appears",
            _build,
            identity,
            ["code/Li_model.py"],
            known_identities=("Li",),
        )
        _write(identity, "code/chinese_name.txt", "复核人：李\n")
        _expect_error(
            "appears",
            _build,
            identity,
            ["code/chinese_name.txt"],
            known_identities=("李",),
        )
        _write(identity, "code/model_utf16.txt", "队员：张三\n".encode("utf-16-le"))
        _expect_error(
            "identity",
            _build,
            identity,
            ["code/model_utf16.txt"],
            known_identities=("张三",),
        )
        _write(identity, "code/model.py", "# Author: NumPy contributors\nprint(1)\n")
        warned = _build(identity, ["code/model.py"])
        assert warned["passed"] and warned["warnings"], warned

        pdf_identity = fixture_root / "pdf_identity"
        _write(
            pdf_identity,
            "results/plot.pdf",
            b"%PDF-1.4\n1 0 obj << /Author (Alice Chen) >> endobj\n%%EOF\n",
        )
        _expect_error(
            "identity",
            _build,
            pdf_identity,
            ["results/plot.pdf"],
            known_identities=("Alice Chen",),
        )
        searchable_pdf = fixture_root / "searchable_pdf"
        _write(
            searchable_pdf,
            "results/report.pdf",
            _searchable_pdf_bytes("Alice Chen"),
        )
        _expect_error(
            "searchable PDF page text",
            _build,
            searchable_pdf,
            ["results/report.pdf"],
            known_identities=("Alice Chen",),
        )

        # Receipt/archive/source drift is detected independently of CRC.
        drift = fixture_root / "drift"
        _write(drift, "code/main.py", "print(1)\n")
        _build(drift, ["code/main.py"])
        _write(drift, "code/main.py", "print(2)\n")
        stale = audit_support_archive(drift, OUTPUT, RECEIPT, ai_required=False)
        assert not stale["passed"] and any("source" in item.lower() for item in stale["errors"]), stale
        _write(drift, "code/main.py", "print(1)\n")
        payload = bytearray((drift / OUTPUT).read_bytes())
        payload[max(0, len(payload) // 2)] ^= 0x01
        (drift / OUTPUT).write_bytes(payload)
        corrupt = audit_support_archive(drift, OUTPUT, RECEIPT, ai_required=False)
        assert not corrupt["passed"], corrupt
        assert any(
            token in " ".join(corrupt["errors"]).casefold()
            for token in ("hash", "crc", "zip")
        ), corrupt

        # Output and receipt themselves must be canonical run-local paths.
        path_case = fixture_root / "paths"
        _write(path_case, "code/main.py", "print(1)\n")
        _expect_error(
            "canonical",
            build_support_archive,
            path_case,
            ["code/main.py"],
            "../support.zip",
            RECEIPT,
            generated_by="测试复核员",
            ai_required=False,
        )
        _expect_error(
            "official",
            build_support_archive,
            path_case,
            ["code/main.py"],
            "deliverables/support.zip",
            RECEIPT,
            generated_by="测试复核员",
            ai_required=False,
        )
        _expect_error(
            "internal",
            build_support_archive,
            path_case,
            ["code/main.py"],
            OUTPUT,
            "deliverables/SUPPORT_PACKAGE_RECEIPT.json",
            generated_by="测试复核员",
            ai_required=False,
        )
        _expect_error(
            "canonical",
            build_support_archive,
            path_case,
            ["code/main.py"],
            OUTPUT,
            OUTPUT,
            generated_by="测试复核员",
            ai_required=False,
        )

        # A hand-edited receipt cannot redefine the approved member list.
        receipt_case = fixture_root / "receipt"
        _write(receipt_case, "code/main.py", "print(1)\n")
        _build(receipt_case, ["code/main.py"])
        receipt_payload = json.loads((receipt_case / RECEIPT).read_text(encoding="utf-8"))
        receipt_payload["files"][0]["sha256"] = "0" * 64
        (receipt_case / RECEIPT).write_text(
            json.dumps(receipt_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        forged = audit_support_archive(receipt_case, OUTPUT, RECEIPT, ai_required=False)
        assert not forged["passed"], forged

        # The build-time identity vocabulary is itself receipt-bound.  It may
        # not be edited away to weaken later anonymity scans.
        identity_receipt_case = fixture_root / "identity_receipt"
        _write(identity_receipt_case, "code/main.py", "print(1)\n")
        _build(
            identity_receipt_case,
            ["code/main.py"],
            known_identities=("张三",),
        )
        identity_receipt_payload = json.loads(
            (identity_receipt_case / RECEIPT).read_text(encoding="utf-8")
        )
        identity_receipt_payload["known_identities"] = ["测试复核员"]
        (identity_receipt_case / RECEIPT).write_text(
            json.dumps(identity_receipt_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        weakened = audit_support_archive(
            identity_receipt_case,
            OUTPUT,
            RECEIPT,
            ai_required=False,
        )
        assert not weakened["passed"] and any(
            "known_identity" in item for item in weakened["errors"]
        ), weakened

        print("support package self-test: PASS")
        return 0
    finally:
        shutil.rmtree(fixture_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
