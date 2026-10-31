#!/usr/bin/env python3
"""Build and verify the two canonical source-release SHA-256 manifests.

The release file set is intentionally independent of Git.  This repository may
be distributed without ``.git/``, so relying on ``git ls-files`` or implicit
``.gitignore`` discovery would make the manifest depend on the checkout form.

``check`` is read-only and reports malformed entries, changed or missing listed
files, and current release files that are not listed.  ``write`` derives one
canonical record list and atomically replaces both manifest files with the same
UTF-8/LF payload.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAMES = ("MANIFEST.sha256", "MANIFEST_V5.1.sha256")
RUN_RELEASE_FILES = frozenset({"runs/.gitkeep", "runs/README.md"})
HASH_LINE_RE = re.compile(r"^([0-9a-f]{64})  ([^\r\n]+)$")
TEST_PREFIX = "_SELFTEST_RELEASE_MANIFEST_"


class ManifestBuildError(RuntimeError):
    """The current tree cannot be represented by a safe release manifest."""

    def __init__(self, errors: Iterable[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True, order=True)
class ManifestRecord:
    path: str
    sha256: str


@dataclass
class ManifestAudit:
    manifest: str
    errors: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unlisted: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(
            (self.errors, self.changed, self.missing, self.unlisted, self.unexpected)
        )


@dataclass
class ReleaseAudit:
    record_count: int = 0
    source_errors: list[str] = field(default_factory=list)
    pair_errors: list[str] = field(default_factory=list)
    manifests: list[ManifestAudit] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            not self.source_errors
            and not self.pair_errors
            and bool(self.manifests)
            and all(item.ok for item in self.manifests)
        )


def _canonical_relative_errors(relative: str) -> list[str]:
    errors: list[str] = []
    if not relative:
        return ["path is empty"]
    if "\\" in relative:
        errors.append("path uses a backslash instead of POSIX separators")
    if ":" in relative:
        errors.append("path contains a colon and is not portable across release hosts")
    if any(ord(character) < 32 for character in relative):
        errors.append("path contains an ASCII control character")
    pure = PurePosixPath(relative)
    windows_path = PureWindowsPath(relative)
    if pure.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        errors.append("path is absolute")
    if any(part in {"", ".", ".."} for part in pure.parts):
        errors.append("path contains an empty, dot, or parent component")
    if pure.as_posix() != relative:
        errors.append("path is not in canonical relative POSIX form")
    return errors


def _is_release_excluded(relative: str, *, is_dir: bool = False) -> bool:
    """Return whether *relative* is intentionally absent from source releases."""
    pure = PurePosixPath(relative)
    parts = pure.parts
    if not parts:
        return False

    # Repository/runtime metadata never belongs in a source release manifest.
    if ".git" in parts or "__pycache__" in parts:
        return True
    if any(part in {".venv", ".venv-verifier"} for part in parts):
        return True
    if pure.name.endswith(".pyc") or pure.name in {".DS_Store", "Thumbs.db"}:
        return True
    if pure.name == ".env" or (
        pure.name.startswith(".env.") and pure.name != ".env.example"
    ):
        return True
    if pure.suffix.lower() == ".zip":
        return True

    # Generated top-level workspaces.  Nested template directories named
    # ``results`` remain release inputs (for example templates/run/results).
    if parts[0] in {"cache", "results"}:
        return True
    if relative == ".harness/active_run.txt":
        return True
    if parts[0] == "runs":
        # Traverse the directory itself so the two release placeholders remain
        # discoverable, but prune every generated child directory/file.
        if is_dir and relative == "runs":
            return False
        if relative not in RUN_RELEASE_FILES:
            return True
    if len(parts) == 1 and pure.name in MANIFEST_NAMES:
        return True

    return False


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _stat_signature(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _stable_sha256(path: Path) -> str:
    before = _stat_signature(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    after = _stat_signature(path)
    if before != after:
        raise OSError("file changed while it was being hashed")
    return digest.hexdigest()


def canonical_records(root: Path) -> list[ManifestRecord]:
    """Return the deterministic, safe source-release file list below *root*."""
    if root.is_symlink():
        raise ManifestBuildError([f"release root is a symlink: {root}"])
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise ManifestBuildError([f"release root cannot be resolved: {exc}"]) from exc
    if not root.is_dir():
        raise ManifestBuildError([f"release root is not a directory: {root}"])

    errors: list[str] = []
    records: list[ManifestRecord] = []

    def walk_error(exc: OSError) -> None:
        errors.append(f"cannot traverse release tree: {exc}")

    for current_text, dir_names, file_names in os.walk(
        root, topdown=True, followlinks=False, onerror=walk_error
    ):
        current = Path(current_text)
        kept_dirs: list[str] = []
        for name in sorted(dir_names):
            candidate = current / name
            relative = candidate.relative_to(root).as_posix()
            if _is_release_excluded(relative, is_dir=True):
                continue
            path_errors = _canonical_relative_errors(relative)
            if path_errors:
                errors.extend(
                    f"non-canonical release path {relative!r}: {item}"
                    for item in path_errors
                )
                continue
            if candidate.is_symlink():
                errors.append(f"release tree contains a directory symlink: {relative}")
                try:
                    target = candidate.resolve(strict=True)
                except OSError as exc:
                    errors.append(f"directory symlink cannot be resolved: {relative}: {exc}")
                else:
                    if not _inside(target, root):
                        errors.append(f"directory symlink escapes release root: {relative}")
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except OSError as exc:
                errors.append(f"release directory cannot be resolved: {relative}: {exc}")
                continue
            if not _inside(resolved, root):
                errors.append(f"release directory escapes release root: {relative}")
                continue
            if not resolved.is_dir():
                errors.append(f"release directory entry is not a directory: {relative}")
                continue
            kept_dirs.append(name)
        dir_names[:] = kept_dirs

        for name in sorted(file_names):
            candidate = current / name
            relative = candidate.relative_to(root).as_posix()
            if _is_release_excluded(relative):
                continue
            path_errors = _canonical_relative_errors(relative)
            if path_errors:
                errors.extend(
                    f"non-canonical release path {relative!r}: {item}"
                    for item in path_errors
                )
                continue
            if candidate.is_symlink():
                errors.append(f"release tree contains a file symlink: {relative}")
                try:
                    target = candidate.resolve(strict=True)
                except OSError as exc:
                    errors.append(f"file symlink cannot be resolved: {relative}: {exc}")
                else:
                    if not _inside(target, root):
                        errors.append(f"file symlink escapes release root: {relative}")
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except OSError as exc:
                errors.append(f"release file cannot be resolved: {relative}: {exc}")
                continue
            if not _inside(resolved, root):
                errors.append(f"release file escapes release root: {relative}")
                continue
            if not resolved.is_file():
                errors.append(f"release entry is not a regular file: {relative}")
                continue
            try:
                digest = _stable_sha256(resolved)
            except OSError as exc:
                errors.append(f"cannot hash release file {relative}: {exc}")
                continue
            records.append(ManifestRecord(relative, digest))

    records.sort()
    paths = [item.path for item in records]
    if len(paths) != len(set(paths)):
        errors.append("canonical release discovery produced duplicate paths")
    if errors:
        raise ManifestBuildError(errors)
    return records


def _manifest_target_errors(root: Path, relative: str) -> list[str]:
    errors = _canonical_relative_errors(relative)
    if errors:
        return errors
    if _is_release_excluded(relative):
        errors.append("path is excluded from the canonical release file set")
        return errors

    root = root.resolve(strict=True)
    pure = PurePosixPath(relative)
    cursor = root
    for part in pure.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            errors.append("path contains a symlink")
            try:
                target = cursor.resolve(strict=True)
            except OSError:
                pass
            else:
                if not _inside(target, root):
                    errors.append("path resolves outside the release root")
            return errors
        if not cursor.exists():
            break
    try:
        resolved = (root.joinpath(*pure.parts)).resolve(strict=False)
    except OSError as exc:
        errors.append(f"path cannot be resolved: {exc}")
    else:
        if not _inside(resolved, root):
            errors.append("path resolves outside the release root")
    return errors


def _read_manifest(root: Path, manifest_name: str) -> tuple[list[ManifestRecord], list[str]]:
    manifest_path = root / manifest_name
    errors: list[str] = []
    if manifest_path.is_symlink():
        return [], [f"{manifest_name} is a symlink"]
    if not manifest_path.is_file():
        return [], [f"{manifest_name} does not exist as a regular file"]
    try:
        payload = manifest_path.read_bytes()
        text = payload.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [], [f"{manifest_name} cannot be read as UTF-8: {exc}"]
    if b"\r" in payload:
        errors.append(f"{manifest_name} must use LF, not CR/CRLF, line endings")
    if payload and not payload.endswith(b"\n"):
        errors.append(f"{manifest_name} must end with exactly one LF")
    if payload.endswith(b"\n\n"):
        errors.append(f"{manifest_name} has a trailing blank line")

    records: list[ManifestRecord] = []
    for index, line in enumerate(text.splitlines(), start=1):
        match = HASH_LINE_RE.fullmatch(line)
        if match is None:
            errors.append(f"{manifest_name}:{index}: expected '<lowercase-sha256>  <path>'")
            continue
        digest, relative = match.groups()
        path_errors = _manifest_target_errors(root, relative)
        if path_errors:
            errors.extend(
                f"{manifest_name}:{index}: unsafe/non-canonical path {relative!r}: {item}"
                for item in path_errors
            )
            continue
        records.append(ManifestRecord(relative, digest))

    if not records:
        errors.append(f"{manifest_name} contains no valid records")
    paths = [item.path for item in records]
    if paths != sorted(paths):
        errors.append(f"{manifest_name} records are not sorted by canonical path")
    duplicate_paths = sorted({path for path in paths if paths.count(path) > 1})
    if duplicate_paths:
        errors.append(
            f"{manifest_name} contains duplicate paths: {', '.join(duplicate_paths)}"
        )
    return records, errors


def audit_release(root: Path = ROOT) -> ReleaseAudit:
    if root.is_symlink():
        return ReleaseAudit(source_errors=[f"release root is a symlink: {root}"])
    root = root.resolve(strict=True)
    report = ReleaseAudit()
    try:
        current_records = canonical_records(root)
    except ManifestBuildError as exc:
        report.source_errors.extend(exc.errors)
        current_records = []
    report.record_count = len(current_records)
    current = {item.path: item.sha256 for item in current_records}

    manifest_payloads: list[tuple[str, bytes]] = []
    for manifest_name in MANIFEST_NAMES:
        manifest_path = root / manifest_name
        if manifest_path.is_file() and not manifest_path.is_symlink():
            try:
                manifest_payloads.append((manifest_name, manifest_path.read_bytes()))
            except OSError as exc:
                report.pair_errors.append(f"cannot compare {manifest_name}: {exc}")
    if len(manifest_payloads) == len(MANIFEST_NAMES):
        first_name, first_payload = manifest_payloads[0]
        for name, payload in manifest_payloads[1:]:
            if payload != first_payload:
                report.pair_errors.append(
                    f"{first_name} and {name} are not byte-for-byte identical"
                )

    for manifest_name in MANIFEST_NAMES:
        records, errors = _read_manifest(root, manifest_name)
        item = ManifestAudit(manifest=manifest_name, errors=errors)
        stored = {record.path: record.sha256 for record in records}
        for relative, expected in sorted(stored.items()):
            target = root.joinpath(*PurePosixPath(relative).parts)
            if relative in current:
                if current[relative] != expected:
                    item.changed.append(relative)
            elif not target.exists():
                item.missing.append(relative)
            else:
                item.unexpected.append(relative)
        if not report.source_errors:
            item.unlisted.extend(sorted(set(current) - set(stored)))
        report.manifests.append(item)
    return report


def _payload(records: Iterable[ManifestRecord]) -> bytes:
    return "".join(f"{item.sha256}  {item.path}\n" for item in records).encode("utf-8")


def _stage_atomic_payload(target: Path, payload: bytes) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _restore_target(target: Path, previous: bytes | None) -> None:
    if previous is None:
        target.unlink(missing_ok=True)
        return
    temporary = _stage_atomic_payload(target, previous)
    os.replace(temporary, target)


def write_manifest_pair(root: Path = ROOT) -> int:
    """Atomically replace both manifests from one stable canonical record set."""
    if root.is_symlink():
        raise ManifestBuildError([f"release root is a symlink: {root}"])
    root = root.resolve(strict=True)
    first_records = canonical_records(root)
    second_records = canonical_records(root)
    if first_records != second_records:
        raise ManifestBuildError(["release tree changed while the manifest was built"])
    payload = _payload(first_records)
    targets = [root / name for name in MANIFEST_NAMES]
    previous = {
        target: (target.read_bytes() if target.is_file() and not target.is_symlink() else None)
        for target in targets
    }
    staged: dict[Path, Path] = {}
    replaced: list[Path] = []
    try:
        for target in targets:
            if target.is_symlink():
                raise ManifestBuildError([f"refusing to replace symlink manifest: {target.name}"])
            staged[target] = _stage_atomic_payload(target, payload)
        for target in targets:
            os.replace(staged[target], target)
            replaced.append(target)
        if any(target.read_bytes() != payload for target in targets):
            raise ManifestBuildError(["post-write manifest byte verification failed"])
        post = audit_release(root)
        if not post.ok:
            raise ManifestBuildError(
                ["post-write manifest audit failed", *_audit_lines(post)]
            )
    except BaseException as exc:
        rollback_errors: list[str] = []
        for target in reversed(replaced):
            try:
                _restore_target(target, previous[target])
            except OSError as rollback_exc:
                rollback_errors.append(f"cannot roll back {target.name}: {rollback_exc}")
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
        if rollback_errors:
            raise ManifestBuildError(
                [f"manifest replacement failed: {exc}", *rollback_errors]
            ) from exc
        raise
    return len(first_records)


def _audit_lines(report: ReleaseAudit) -> list[str]:
    lines: list[str] = []
    lines.extend(f"source-error: {item}" for item in report.source_errors)
    lines.extend(f"pair-error: {item}" for item in report.pair_errors)
    for manifest in report.manifests:
        prefix = manifest.manifest
        lines.extend(f"{prefix}: error: {item}" for item in manifest.errors)
        lines.extend(f"{prefix}: changed: {item}" for item in manifest.changed)
        lines.extend(f"{prefix}: missing: {item}" for item in manifest.missing)
        lines.extend(f"{prefix}: unlisted: {item}" for item in manifest.unlisted)
        lines.extend(f"{prefix}: unexpected: {item}" for item in manifest.unexpected)
    return lines


def cmd_check(_args: argparse.Namespace) -> int:
    report = audit_release(ROOT)
    if report.ok:
        print(
            f"OK: {report.record_count} canonical release files; "
            "both manifests are identical and current."
        )
        return 0
    print("ERROR: release manifest audit failed:", file=sys.stderr)
    for line in _audit_lines(report):
        print(f"- {line}", file=sys.stderr)
    return 2


def cmd_write(_args: argparse.Namespace) -> int:
    try:
        count = write_manifest_pair(ROOT)
    except (ManifestBuildError, OSError) as exc:
        print(f"ERROR: could not write release manifests: {exc}", file=sys.stderr)
        return 2
    print(
        f"Wrote {count} canonical records atomically to "
        f"{MANIFEST_NAMES[0]} and {MANIFEST_NAMES[1]}."
    )
    return 0


def _write_test_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _assert_safe_test_container(container: Path) -> None:
    runs = (ROOT / "runs").resolve(strict=True)
    resolved = container.resolve(strict=False)
    if resolved.parent != runs or not resolved.name.startswith(TEST_PREFIX):
        raise AssertionError(f"unsafe self-test cleanup target: {resolved}")


def cmd_self_test(_args: argparse.Namespace) -> int:
    """Run a fast isolated regression entirely below ``runs/``."""
    container = ROOT / "runs" / f"{TEST_PREFIX}{os.getpid()}"
    _assert_safe_test_container(container)
    if container.exists():
        raise AssertionError(f"self-test directory already exists: {container}")
    release = container / "release"
    try:
        _write_test_file(release / ".env.example", "SAFE_EXAMPLE=1\n")
        _write_test_file(release / ".env", "SECRET=excluded\n")
        _write_test_file(release / "src/a.txt", "alpha\n")
        _write_test_file(release / "docs/b.txt", "beta\n")
        _write_test_file(release / "runs/.gitkeep", "")
        _write_test_file(release / "runs/README.md", "fixture\n")
        _write_test_file(release / "runs/transient/output.txt", "excluded\n")
        _write_test_file(release / "tools/__pycache__/ignored.pyc", "excluded\n")
        _write_test_file(release / "results/generated.txt", "excluded\n")

        expected = {
            ".env.example",
            "docs/b.txt",
            "runs/.gitkeep",
            "runs/README.md",
            "src/a.txt",
        }
        assert {item.path for item in canonical_records(release)} == expected
        assert write_manifest_pair(release) == len(expected)
        assert (release / MANIFEST_NAMES[0]).read_bytes() == (
            release / MANIFEST_NAMES[1]
        ).read_bytes()
        assert audit_release(release).ok

        clean_payloads = {
            name: (release / name).read_bytes() for name in MANIFEST_NAMES
        }
        real_replace = os.replace

        def fail_second_manifest(source: str | bytes | Path, destination: str | bytes | Path) -> None:
            if Path(destination).name == MANIFEST_NAMES[1]:
                raise OSError("synthetic second-manifest replacement failure")
            real_replace(source, destination)

        try:
            with patch.object(os, "replace", side_effect=fail_second_manifest):
                write_manifest_pair(release)
        except OSError as exc:
            assert "synthetic second-manifest" in str(exc)
        else:
            raise AssertionError("two-manifest rollback path was not exercised")
        assert all(
            (release / name).read_bytes() == clean_payloads[name]
            for name in MANIFEST_NAMES
        )
        assert not list(release.glob(".MANIFEST*.tmp"))

        _write_test_file(release / "src/a.txt", "changed\n")
        changed = audit_release(release)
        assert all("src/a.txt" in item.changed for item in changed.manifests)
        _write_test_file(release / "new.txt", "new\n")
        (release / "docs/b.txt").unlink()
        drifted = audit_release(release)
        assert all("new.txt" in item.unlisted for item in drifted.manifests)
        assert all("docs/b.txt" in item.missing for item in drifted.manifests)

        _write_test_file(release / "src/a.txt", "alpha\n")
        _write_test_file(release / "docs/b.txt", "beta\n")
        (release / "new.txt").unlink()
        write_manifest_pair(release)
        with (release / MANIFEST_NAMES[1]).open("ab") as handle:
            handle.write(b"\n")
        unequal = audit_release(release)
        assert unequal.pair_errors
        assert not unequal.ok
        write_manifest_pair(release)

        syntactically_unsafe = (
            "../outside.txt",
            "./src/a.txt",
            "src\\a.txt",
            "C:/outside.txt",
        )
        for unsafe in (*syntactically_unsafe, "runs/transient/output.txt"):
            if unsafe in syntactically_unsafe:
                assert _canonical_relative_errors(unsafe), unsafe
            malicious = f"{'0' * 64}  {unsafe}\n".encode("utf-8")
            for name in MANIFEST_NAMES:
                (release / name).write_bytes(malicious)
            rejected = audit_release(release)
            assert not rejected.ok
            assert all(item.errors for item in rejected.manifests), unsafe
        write_manifest_pair(release)

        outside = container / "outside.txt"
        _write_test_file(outside, "outside\n")
        link = release / "outside-link.txt"
        try:
            link.symlink_to(outside)
        except OSError:
            # Windows hosts without Developer Mode cannot create an unprivileged
            # symlink.  Parser-level traversal/absolute-path rejection above is
            # still deterministic; the symlink branch is exercised where the
            # host permits it.
            pass
        else:
            try:
                canonical_records(release)
            except ManifestBuildError as exc:
                message = "\n".join(exc.errors).lower()
                assert "symlink" in message and "escapes" in message, message
            else:
                raise AssertionError("release symlink was accepted")
            link.unlink()

        assert audit_release(release).ok
        print("OK: isolated release-manifest regression passed.")
        return 0
    finally:
        _assert_safe_test_container(container)
        if container.exists():
            shutil.rmtree(container)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check", help="verify both manifests without writing")
    check.set_defaults(func=cmd_check)
    write = subparsers.add_parser("write", help="atomically regenerate both manifests")
    write.set_defaults(func=cmd_write)
    self_test = subparsers.add_parser(
        "self-test", help="run a fast isolated regression below runs/"
    )
    self_test.set_defaults(func=cmd_self_test)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
