r"""Verify that a published NeuralScreen release satisfies its local contract.

The verifier downloads the public release set through the GitHub API, validates
the outer SHA256SUMS, then validates every ZIP member against the checksum file
inside the archive. The ZIP's tag, commit and runtime manifest must agree with
the requested Git tag. Documentation is compared with the tagged Git blobs,
not with a moving branch.

Usage:  runtime\python.exe verify_github.py [tag]
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Mapping, Sequence


ROOT = Path(__file__).resolve().parent
REPOSITORY = "perseval-BLR/NeuralScreen"
RAW_ROOT = f"https://raw.githubusercontent.com/{REPOSITORY}"
RUNTIME_MANIFEST = "runtime-manifest.json"
THIRD_PARTY_NOTICES = "THIRD-PARTY-NOTICES.md"
CHECKSUMS = "SHA256SUMS"
# The documents the release page must carry, on top of the four emitted
# artifacts. build_release_zip.RELEASE_DOCUMENTS is the same set - a test
# compares the two, because a doc missing from one list and present in the
# other is exactly how "release vX is missing asset README.md" happened.
RELEASE_DOCUMENTS = ("README.md", "README.ru.md", "TECHNICAL.md", "TECHNICAL.ru.md")
VERSION_SOURCE_PATHS = (
    "build_release_zip.py",
    "app/settings_io.py",
    "native/launcher.rc",
)
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
ZIP_MODE = 0o100644

# This pre-existing parallel change is part of the release contract.
RELEASE_DRIVER_WARNING = (
    "> [!WARNING]\n"
    "> NeuralScreen requires the latest NVIDIA driver. Operation with older "
    "drivers or unsupported/non-standard configurations is not guaranteed."
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _crlf_bytes(data: bytes) -> bytes:
    """The bytes a checkout with eol=crlf writes for a (LF) Git blob.

    The builder packages files attributed eol=crlf (the .bat launcher) in
    this form and marks their package record "eol": "crlf"; the record's size
    and sha256 are those of these bytes, its git_blob is the tagged blob.
    """
    return re.sub(rb"\r?\n", b"\r\n", data)


def _fetch(url: str, dest: Path) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            dest.write_bytes(response.read())
        return True
    except Exception as exc:
        print(f"    [FAIL] could not fetch {url}: {exc}")
        return False


def _gh(args: Sequence[str]) -> str:
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=True,
    ).stdout.strip()


def _fetch_asset(asset_id: int, dest: Path) -> bool:
    try:
        with dest.open("wb") as stream:
            subprocess.run(
                [
                    "gh", "api", f"repos/{REPOSITORY}/releases/assets/{asset_id}",
                    "-H", "Accept: application/octet-stream",
                ],
                stdout=stream, check=True, timeout=900,
            )
        return True
    except Exception as exc:
        print(f"    [FAIL] could not fetch asset {asset_id}: {exc}")
        return False


def _safe_archive_path(name: object) -> bool:
    if (
        not isinstance(name, str)
        or not name
        or "\0" in name
        or "\\" in name
        or ":" in name
    ):
        return False
    posix = PurePosixPath(name)
    windows = PureWindowsPath(name)
    return not (
        name.startswith(("/", "\\"))
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or ntpath.splitdrive(name)[0]
        or any(part in ("", ".", "..") for part in name.split("/"))
        or posix.as_posix() != name
    )


def parse_sha256sums(data: bytes, label: str = CHECKSUMS) -> dict[str, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not UTF-8") from exc
    result: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line:
            continue
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line)
        if not match:
            raise ValueError(f"{label}:{number}: malformed checksum line")
        digest, name = match.groups()
        if not _safe_archive_path(name):
            raise ValueError(f"{label}:{number}: unsafe path {name!r}")
        if name in result:
            raise ValueError(f"{label}:{number}: duplicate path {name!r}")
        result[name] = digest
    if not result:
        raise ValueError(f"{label} is empty")
    return result


def _field(text: str, name: str) -> str | None:
    match = re.search(rf"^{re.escape(name)}:\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def _runtime_tree_digest_from_zip(
    archive: zipfile.ZipFile, names: Sequence[str]
) -> tuple[int, str]:
    records = []
    for name in sorted(item for item in names if item.startswith("runtime/")):
        data = archive.read(name)
        records.append((name, len(data), hashlib.sha256(data).hexdigest()))
    digest = hashlib.sha256()
    for name, size, checksum in records:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(checksum.encode("ascii"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\n")
    return len(records), digest.hexdigest()


def _valid_record(record: object, *, require_git: bool = False) -> bool:
    if not isinstance(record, dict) or not _safe_archive_path(record.get("path")):
        return False
    if not isinstance(record.get("size"), int) or record["size"] < 0:
        return False
    if re.fullmatch(r"[0-9a-f]{64}", record.get("sha256", "")) is None:
        return False
    if require_git and re.fullmatch(
        r"[0-9a-f]{40}|[0-9a-f]{64}", record.get("git_blob", "")
    ) is None:
        return False
    return True


def _record_tree_digest(records: Sequence[dict]) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(record["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(record["sha256"].encode("ascii"))
        digest.update(b"\0")
        digest.update(str(record["size"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _validate_package_inventory(
    manifest: dict,
) -> tuple[list[str], dict[str, dict]]:
    failures: list[str] = []
    package = manifest.get("package")
    if not isinstance(package, dict):
        return ["runtime manifest has no package inventory"], {}
    records = package.get("files")
    if not isinstance(records, list):
        return ["runtime manifest has no package file inventory"], {}
    by_path: dict[str, dict] = {}
    paths: list[str] = []
    metadata = {"VERSION.txt", RUNTIME_MANIFEST, THIRD_PARTY_NOTICES, CHECKSUMS}
    for record in records:
        origin = record.get("origin") if isinstance(record, dict) else None
        require_git = origin == "git"
        if origin not in {"git", "generated"} or not _valid_record(
            record, require_git=require_git
        ):
            failures.append("runtime manifest contains an invalid package record")
            continue
        path = record["path"]
        if path in metadata:
            failures.append(f"package inventory contains metadata member: {path}")
        if origin == "generated" and "git_blob" in record:
            failures.append(f"generated package record has a Git blob: {path}")
        if "eol" in record and (origin != "git" or record["eol"] != "crlf"):
            failures.append(f"package record has an unknown eol conversion: {path}")
        if path in by_path:
            failures.append(f"runtime manifest repeats package path: {path}")
        by_path[path] = record
        paths.append(path)
    if paths != sorted(paths):
        failures.append("runtime manifest package inventory is not sorted")
    if package.get("file_count") != len(records):
        failures.append("runtime manifest package file count is inconsistent")
    valid_records = [by_path[path] for path in sorted(by_path)]
    if len(valid_records) == len(records):
        if package.get("tree_sha256") != _record_tree_digest(valid_records):
            failures.append("runtime manifest package tree digest is inconsistent")

    source_records = manifest.get("source_inventory")
    source_by_path = {
        record.get("path"): record for record in source_records
        if isinstance(record, dict) and isinstance(record.get("path"), str)
    } if isinstance(source_records, list) else {}
    runtime = manifest.get("runtime")
    runtime_files = runtime.get("files") if isinstance(runtime, dict) else None
    artifacts = runtime.get("artifacts") if isinstance(runtime, dict) else None
    generated_by_path: dict[str, dict] = {}
    for group in (runtime_files, artifacts):
        if isinstance(group, list):
            for record in group:
                if isinstance(record, dict) and isinstance(record.get("path"), str):
                    generated_by_path[record["path"]] = record

    for path, record in by_path.items():
        origin = record.get("origin")
        counterpart = (
            source_by_path.get(path) if origin == "git"
            else generated_by_path.get(path)
        )
        # A CRLF record pins the converted bytes and its source record the
        # blob, so the two differ by design; the ZIP member is tied back to
        # the blob in validate_release_set and to the tag in the Git binding.
        compared = () if "eol" in record else ("size", "sha256")
        if counterpart is None or any(
            counterpart.get(field) != record.get(field)
            for field in compared
        ):
            failures.append(
                f"package record is not backed by its {origin} inventory: {path}"
            )
        if origin == "git" and counterpart is not None:
            if counterpart.get("git_blob") != record.get("git_blob"):
                failures.append(f"package Git blob differs from source inventory: {path}")

    for group_name, group in (("runtime file", runtime_files), ("runtime artifact", artifacts)):
        if not isinstance(group, list):
            continue
        for record in group:
            if not isinstance(record, dict) or not isinstance(record.get("path"), str):
                continue
            packaged = by_path.get(record["path"])
            if packaged is None or any(
                packaged.get(field) != record.get(field)
                for field in ("size", "sha256")
            ):
                failures.append(
                    f"{group_name} is not identical to package inventory: "
                    f"{record.get('path')}"
                )
    return failures, by_path


def _single_version_match(
    pattern: str, text: str, path: str, field: str
) -> str:
    values = re.findall(pattern, text, re.MULTILINE)
    if len(values) != 1:
        raise ValueError(f"{path} does not declare exactly one {field}")
    value = values[0]
    return ".".join(value) if isinstance(value, tuple) else value


def _version_values(path: str, data: bytes) -> dict[str, str]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"version source is not UTF-8: {path}") from exc
    if path == "build_release_zip.py":
        return {
            "VERSION": _single_version_match(
                r'^VERSION\s*=\s*["\']([^"\']+)["\']\s*$',
                text, path, "VERSION",
            )
        }
    if path == "app/settings_io.py":
        return {
            "APP_VERSION": _single_version_match(
                r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']\s*$',
                text, path, "APP_VERSION",
            )
        }
    if path == "native/launcher.rc":
        return {
            "FILEVERSION": _single_version_match(
                r"^\s*FILEVERSION\s+(\d+),(\d+),(\d+),(\d+)\s*$",
                text, path, "FILEVERSION",
            ),
            "PRODUCTVERSION": _single_version_match(
                r"^\s*PRODUCTVERSION\s+(\d+),(\d+),(\d+),(\d+)\s*$",
                text, path, "PRODUCTVERSION",
            ),
            "FileVersion": _single_version_match(
                r'^\s*VALUE\s+"FileVersion",\s*"([^"]+)"\s*$',
                text, path, "FileVersion string",
            ),
            "ProductVersion": _single_version_match(
                r'^\s*VALUE\s+"ProductVersion",\s*"([^"]+)"\s*$',
                text, path, "ProductVersion string",
            ),
        }
    raise ValueError(f"unknown version source: {path}")


def _validate_version_sources(manifest: dict, version: str) -> list[str]:
    failures: list[str] = []
    records = manifest.get("version_sources")
    if not isinstance(records, list):
        return ["runtime manifest has no version source inventory"]
    by_path: dict[str, dict] = {}
    for record in records:
        if not _valid_record(record, require_git=True):
            failures.append("runtime manifest contains an invalid version source")
            continue
        path = record["path"]
        if path in by_path:
            failures.append(f"runtime manifest repeats version source: {path}")
        by_path[path] = record
    if set(by_path) != set(VERSION_SOURCE_PATHS):
        failures.append("runtime manifest version source inventory is incomplete")
        return failures
    parts = version.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        failures.append("runtime manifest version is not X.Y.Z")
        return failures
    rc_version = ".".join([*parts, "0"])
    expected = {
        "build_release_zip.py": {"VERSION": version},
        "app/settings_io.py": {"APP_VERSION": version},
        "native/launcher.rc": {
            "FILEVERSION": rc_version,
            "PRODUCTVERSION": rc_version,
            "FileVersion": rc_version,
            "ProductVersion": version,
        },
    }
    for path, values in expected.items():
        if by_path[path].get("values") != values:
            failures.append(f"version source differs from manifest version: {path}")
    source_records = manifest.get("source_inventory")
    source_by_path = {
        record.get("path"): record for record in source_records
        if isinstance(record, dict)
    } if isinstance(source_records, list) else {}
    for path, record in by_path.items():
        source = source_by_path.get(path)
        if source is None or any(
            source.get(field) != record.get(field)
            for field in ("size", "sha256", "git_blob")
        ):
            failures.append(
                f"version source is not identical to source inventory: {path}"
            )
    return failures


def _git_blob(repo: Path, tag: str, path: str) -> tuple[str, bytes] | None:
    try:
        oid = subprocess.run(
            ["git", "rev-parse", f"{tag}:{path}"], cwd=repo,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=True,
        ).stdout.strip()
        data = subprocess.run(
            ["git", "cat-file", "blob", oid], cwd=repo,
            capture_output=True, check=True,
        ).stdout
        return oid, data
    except subprocess.CalledProcessError:
        return None


def validate_manifest_git_binding(
    repo: Path, tag: str, manifest: dict
) -> list[str]:
    """Prove every repository record against the exact tagged Git blob."""
    failures: list[str] = []
    groups = (
        ("source inventory", manifest.get("source_inventory")),
        ("version source inventory", manifest.get("version_sources")),
    )
    for label, records in groups:
        if not isinstance(records, list):
            failures.append(f"runtime manifest has no {label}")
            continue
        seen: set[str] = set()
        for record in records:
            if not _valid_record(record, require_git=True):
                failures.append(f"runtime manifest contains invalid {label} record")
                continue
            path = record["path"]
            if path in seen:
                failures.append(f"runtime manifest repeats Git path: {path}")
                continue
            seen.add(path)
            tagged = _git_blob(repo, tag, path)
            if tagged is None:
                failures.append(f"manifest path is absent from tag {tag}: {path}")
                continue
            oid, data = tagged
            if record.get("git_blob") != oid:
                failures.append(f"tagged Git blob differs from manifest: {path}")
            if record.get("size") != len(data):
                failures.append(f"tagged Git blob size differs from manifest: {path}")
            if record.get("sha256") != hashlib.sha256(data).hexdigest():
                failures.append(f"tagged Git blob checksum differs from manifest: {path}")
            if label == "version source inventory":
                try:
                    actual_values = _version_values(path, data)
                except ValueError as exc:
                    failures.append(str(exc))
                else:
                    if record.get("values") != actual_values:
                        failures.append(
                            f"tagged version values differ from manifest: {path}"
                        )
    package = manifest.get("package")
    package_records = package.get("files") if isinstance(package, dict) else None
    if isinstance(package_records, list):
        for record in package_records:
            if not isinstance(record, dict) or not _safe_archive_path(record.get("path")):
                continue
            path = record["path"]
            tagged = _git_blob(repo, tag, path)
            if record.get("origin") == "generated":
                if tagged is not None:
                    failures.append(
                        f"tracked package path is declared generated: {path}"
                    )
                continue
            if record.get("origin") != "git":
                continue
            if tagged is None:
                failures.append(f"package Git path is absent from tag {tag}: {path}")
                continue
            oid, data = tagged
            if record.get("eol") == "crlf":
                data = _crlf_bytes(data)
            if record.get("git_blob") != oid:
                failures.append(f"package Git blob differs from tag: {path}")
            if record.get("size") != len(data):
                failures.append(f"package Git file size differs from tag: {path}")
            if record.get("sha256") != hashlib.sha256(data).hexdigest():
                failures.append(f"package Git file checksum differs from tag: {path}")
    return failures


def asset_set_failures(
    required: set[str], assets: Mapping[str, dict], tag: str = "",
) -> list[str]:
    """Every required release asset is present, and nothing else is.

    A "required are present" check alone lets anything else ride along
    unnoticed. Documentation images were uploaded that way: they are not part
    of a release set (the verifier reads them from the tagged Git blobs, so
    nothing needs them as assets) and a downloader has no use for them next to
    the archive. An extra asset is either a mistake or a stray, so it is
    reported rather than ignored.
    """
    label = f"release {tag}" if tag else "release"
    failures = [f"{label} is missing asset {name}" for name in sorted(required)
                if name not in assets]
    extra = sorted(set(assets) - required)
    if extra:
        failures.append(
            f"{label} carries {len(extra)} asset(s) beyond the release set: "
            f"{', '.join(extra)}"
        )
    return failures


def validate_release_set(
    directory: Path,
    *,
    tag: str,
    tag_commit: str,
    repo: Path | None = None,
) -> list[str]:
    """Validate already-downloaded release assets without network access."""
    directory = Path(directory)
    failures: list[str] = []
    manifest_path = directory / RUNTIME_MANIFEST
    notices_path = directory / THIRD_PARTY_NOTICES
    checksums_path = directory / CHECKSUMS
    for path in (manifest_path, notices_path, checksums_path):
        if not path.is_file():
            failures.append(f"release is missing asset {path.name}")
    if failures:
        return failures

    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"invalid {RUNTIME_MANIFEST}: {exc}"]
    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        failures.append(f"{RUNTIME_MANIFEST} has no version")
        return failures
    if re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
        failures.append(f"{RUNTIME_MANIFEST} version is not X.Y.Z")
        return failures
    if re.fullmatch(r"v\d+\.\d+\.\d+", tag) is None:
        failures.append(f"requested tag is not vX.Y.Z: {tag!r}")
        return failures
    canonical_manifest = (
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    if manifest_bytes != canonical_manifest:
        failures.append(f"{RUNTIME_MANIFEST} is not canonical UTF-8 JSON")
    if manifest.get("schema_version") != 3 or manifest.get("product") != "NeuralScreen":
        failures.append(f"{RUNTIME_MANIFEST} schema/product identity is invalid")
    expected_tag = f"v{version}"
    if tag != expected_tag or manifest.get("expected_tag") != tag:
        failures.append(
            f"tag/manifest mismatch: requested={tag!r}, "
            f"manifest={manifest.get('expected_tag')!r}, version={version!r}"
        )
    failures.extend(_validate_version_sources(manifest, version))
    package_failures, package_by_path = _validate_package_inventory(manifest)
    failures.extend(package_failures)
    if repo is not None:
        failures.extend(validate_manifest_git_binding(Path(repo), tag, manifest))

    archive_name = f"neuralscreen-v{version}-full.zip"
    archive_path = directory / archive_name
    if not archive_path.is_file():
        failures.append(f"release is missing asset {archive_name}")
        return failures

    try:
        outer = parse_sha256sums(checksums_path.read_bytes(), "outer SHA256SUMS")
    except (OSError, ValueError) as exc:
        failures.append(str(exc))
        return failures
    expected_outer = {archive_name, RUNTIME_MANIFEST, THIRD_PARTY_NOTICES}
    if set(outer) != expected_outer:
        failures.append(
            "outer SHA256SUMS inventory mismatch: "
            f"expected {sorted(expected_outer)}, got {sorted(outer)}"
        )
    for name in sorted(expected_outer & set(outer)):
        actual = _sha256(directory / name)
        if outer[name] != actual:
            failures.append(f"outer checksum mismatch: {name}")

    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                failures.append("ZIP contains duplicate member names")
            metadata_order = [
                "VERSION.txt", RUNTIME_MANIFEST, THIRD_PARTY_NOTICES, CHECKSUMS,
            ]
            expected_order = metadata_order + sorted(
                name for name in names if name not in metadata_order
            )
            if names != expected_order:
                failures.append("ZIP member order is not canonical")
            for info in infos:
                name = info.filename
                if not _safe_archive_path(name):
                    failures.append(f"ZIP contains unsafe member {name!r}")
                if info.date_time != ZIP_TIMESTAMP:
                    failures.append(f"ZIP member timestamp is not deterministic: {name}")
                if info.create_system != 3 or (info.external_attr >> 16) != ZIP_MODE:
                    failures.append(f"ZIP member mode is not canonical: {name}")
            metadata = {
                "VERSION.txt", RUNTIME_MANIFEST, THIRD_PARTY_NOTICES, CHECKSUMS,
            }
            missing_metadata = metadata - set(names)
            if missing_metadata:
                failures.append(
                    f"ZIP is missing metadata: {sorted(missing_metadata)}"
                )
                return failures
            if "native/libraries/README.md" not in package_by_path:
                failures.append(
                    "package inventory is missing native/libraries/README.md"
                )
            expected_names = metadata | set(package_by_path)
            extra_members = set(names) - expected_names
            missing_members = expected_names - set(names)
            if extra_members:
                failures.append(
                    f"ZIP has undeclared payload members: {sorted(extra_members)}"
                )
            if missing_members:
                failures.append(
                    f"ZIP is missing declared payload members: {sorted(missing_members)}"
                )
            for name in sorted(set(package_by_path) & set(names)):
                data = archive.read(name)
                record = package_by_path[name]
                if record.get("size") != len(data):
                    failures.append(f"package file size mismatch: {name}")
                if record.get("sha256") != hashlib.sha256(data).hexdigest():
                    failures.append(f"package file checksum mismatch: {name}")
            if archive.read(RUNTIME_MANIFEST) != manifest_bytes:
                failures.append("ZIP runtime manifest differs from release asset")
            if archive.read(THIRD_PARTY_NOTICES) != notices_path.read_bytes():
                failures.append("ZIP third-party notices differ from release asset")
            runtime = manifest.get("runtime")
            if not isinstance(runtime, dict):
                failures.append("runtime manifest has no runtime section")
            else:
                declared_runtime = runtime.get("files")
                declared_by_path: dict[str, dict] = {}
                if not isinstance(declared_runtime, list):
                    failures.append("runtime manifest has no complete file inventory")
                else:
                    for record in declared_runtime:
                        if not _valid_record(record):
                            failures.append("runtime manifest contains an invalid runtime file")
                            continue
                        path = record["path"]
                        if not path.startswith("runtime/"):
                            failures.append(
                                f"runtime manifest file is outside runtime/: {path}"
                            )
                        if path in declared_by_path:
                            failures.append(f"runtime manifest repeats file: {path}")
                        declared_by_path[path] = record
                    zip_runtime = {name for name in names if name.startswith("runtime/")}
                    if set(declared_by_path) != zip_runtime:
                        failures.append(
                            "runtime manifest file inventory differs from ZIP"
                        )
                    for name in sorted(set(declared_by_path) & zip_runtime):
                        data = archive.read(name)
                        record = declared_by_path[name]
                        if record.get("size") != len(data):
                            failures.append(f"runtime file size mismatch: {name}")
                        if record.get("sha256") != hashlib.sha256(data).hexdigest():
                            failures.append(f"runtime file checksum mismatch: {name}")
                runtime_count, runtime_digest = _runtime_tree_digest_from_zip(
                    archive, names
                )
                if runtime.get("file_count") != runtime_count:
                    failures.append("runtime manifest file count differs from ZIP")
                if runtime.get("tree_sha256") != runtime_digest:
                    failures.append("runtime manifest tree digest differs from ZIP")
                artifacts = runtime.get("artifacts")
                if not isinstance(artifacts, list):
                    failures.append("runtime manifest has no artifact inventory")
                else:
                    for artifact in artifacts:
                        if not isinstance(artifact, dict):
                            failures.append("runtime manifest contains an invalid artifact")
                            continue
                        name = artifact.get("path")
                        if name not in names:
                            failures.append(f"runtime artifact is missing from ZIP: {name}")
                            continue
                        data = archive.read(name)
                        if artifact.get("size") != len(data):
                            failures.append(f"runtime artifact size mismatch: {name}")
                        if artifact.get("sha256") != hashlib.sha256(data).hexdigest():
                            failures.append(f"runtime artifact checksum mismatch: {name}")
            source_inventory = manifest.get("source_inventory")
            source_by_path: dict[str, dict] = {}
            if not isinstance(source_inventory, list):
                failures.append("runtime manifest has no source inventory")
            else:
                for record in source_inventory:
                    if not _valid_record(record, require_git=True):
                        failures.append("runtime manifest contains an invalid source record")
                        continue
                    path = record["path"]
                    if path in source_by_path:
                        failures.append(f"runtime manifest repeats source path: {path}")
                    source_by_path[path] = record
                for name in sorted(set(names) & set(source_by_path)):
                    data = archive.read(name)
                    record = source_by_path[name]
                    if package_by_path.get(name, {}).get("eol") == "crlf":
                        # Packaged as a CRLF checkout of the pinned blob: it
                        # must be exactly that form, and undo to the blob.
                        blob = data.replace(b"\r\n", b"\n")
                        if _crlf_bytes(blob) != data:
                            failures.append(
                                f"packaged CRLF file has stray line endings: {name}"
                            )
                        data = blob
                    if record.get("size") != len(data):
                        failures.append(f"packaged Git file size mismatch: {name}")
                    if record.get("sha256") != hashlib.sha256(data).hexdigest():
                        failures.append(f"packaged Git file checksum mismatch: {name}")
            try:
                inner = parse_sha256sums(
                    archive.read(CHECKSUMS), "ZIP SHA256SUMS"
                )
            except ValueError as exc:
                failures.append(str(exc))
                return failures
            expected_inner = set(names) - {CHECKSUMS}
            if set(inner) != expected_inner:
                failures.append("ZIP SHA256SUMS does not cover exactly every member")
            for name in sorted(expected_inner & set(inner)):
                actual = hashlib.sha256(archive.read(name)).hexdigest()
                if inner[name] != actual:
                    failures.append(f"ZIP checksum mismatch: {name}")

            version_text = archive.read("VERSION.txt").decode("utf-8", "strict")
            if not version_text.startswith(f"NeuralScreen {version}\n"):
                failures.append("VERSION.txt product version differs from manifest")
            if _field(version_text, "tag") != tag:
                failures.append("VERSION.txt tag differs from requested tag")
            if _field(version_text, "commit") != tag_commit:
                failures.append("VERSION.txt commit differs from tagged commit")
            declared_manifest = _field(version_text, "runtime manifest")
            expected_declaration = (
                f"{RUNTIME_MANIFEST} sha256 "
                f"{hashlib.sha256(manifest_bytes).hexdigest()}"
            )
            if declared_manifest != expected_declaration:
                failures.append("VERSION.txt runtime manifest digest is wrong")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile, KeyError) as exc:
        failures.append(f"invalid release ZIP: {exc}")
    return failures


def _local_tag_commit(tag: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", f"{tag}^{{commit}}"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode:
        return ""
    return result.stdout.strip()


def _remote_tag_commit(tag: str) -> str:
    """Resolve lightweight or annotated GitHub tags to their commit SHA."""
    encoded = urllib.parse.quote(tag, safe="")
    try:
        ref = json.loads(
            _gh(["api", f"repos/{REPOSITORY}/git/ref/tags/{encoded}"])
        )
        obj = ref["object"]
        for _ in range(8):
            kind, sha = obj.get("type"), obj.get("sha")
            if kind == "commit" and isinstance(sha, str):
                return sha
            if kind != "tag" or not isinstance(sha, str):
                return ""
            tag_object = json.loads(
                _gh(["api", f"repos/{REPOSITORY}/git/tags/{sha}"])
            )
            obj = tag_object.get("object", {})
    except (KeyError, TypeError, json.JSONDecodeError, subprocess.CalledProcessError):
        return ""
    return ""


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    source = (ROOT / "build_release_zip.py").read_text(encoding="utf-8")
    match = re.search(r'^VERSION = "([^"]+)"', source, re.MULTILINE)
    version = match.group(1) if match else "?"
    tag = args[0] if args else f"v{version}"
    failures: list[str] = []
    if tag != f"v{version}":
        failures.append(f"requested tag {tag!r} does not match builder v{version}")
    local_tag_commit = _local_tag_commit(tag)
    if not local_tag_commit:
        failures.append(f"local tag {tag} is missing or invalid")

    tagged_raw = f"{RAW_ROOT}/{urllib.parse.quote(tag, safe='')}"
    docs = [
        "docs/screenshot-main-light.png", "docs/screenshot-main-dark.png",
        "docs/screenshot-settings.png", "docs/screenshot-windows.png",
        *RELEASE_DOCUMENTS,
    ]
    temp = ROOT / "_work" / "verify-github"
    temp.mkdir(parents=True, exist_ok=True)
    for name in docs:
        try:
            blob = subprocess.run(
                ["git", "show", f"{tag}:{name}"], cwd=ROOT,
                capture_output=True, check=True,
            ).stdout
            local = hashlib.sha256(blob).hexdigest()
        except subprocess.CalledProcessError:
            failures.append(f"{name}: missing from local tag {tag}")
            continue
        remote_path = temp / name.replace("/", "_")
        if not _fetch(f"{tagged_raw}/{name}", remote_path):
            failures.append(f"{name}: fetch failed")
            continue
        remote = _sha256(remote_path)
        if local != remote:
            failures.append(
                f"{name}: tagged blob mismatch local={local[:12]} github={remote[:12]}"
            )
        else:
            print(f"    [OK] {name} matches tag {tag} ({local[:12]})")

    try:
        release = json.loads(_gh(["api", f"repos/{REPOSITORY}/releases/tags/{tag}"]))
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        failures.append(f"release {tag} does not exist")
        release = {}
    if release:
        remote_tag_commit = _remote_tag_commit(tag)
        if not remote_tag_commit:
            failures.append(f"could not resolve remote tag {tag} to a commit")
        elif local_tag_commit and remote_tag_commit != local_tag_commit:
            failures.append(
                f"local/remote tag mismatch: {local_tag_commit[:12]} != "
                f"{remote_tag_commit[:12]}"
            )
        tag_commit = remote_tag_commit or local_tag_commit
        body = release.get("body") or ""
        markers = {
            "standard NVIDIA notice": "> **Notice.** Not affiliated with NVIDIA;",
            "driver warning alert": RELEASE_DRIVER_WARNING,
        }
        for label, marker in markers.items():
            if marker not in body:
                failures.append(f"release {tag} is missing {label}")

        archive_name = f"neuralscreen-v{version}-full.zip"
        required_assets = {
            archive_name, CHECKSUMS, RUNTIME_MANIFEST, THIRD_PARTY_NOTICES,
            *RELEASE_DOCUMENTS,
        }
        assets: Mapping[str, dict] = {
            asset["name"]: asset for asset in release.get("assets", [])
        }
        failures.extend(asset_set_failures(required_assets, assets, tag))
        for name in (archive_name, CHECKSUMS, RUNTIME_MANIFEST, THIRD_PARTY_NOTICES):
            asset = assets.get(name)
            if asset and not _fetch_asset(asset["id"], temp / name):
                failures.append(f"could not download release asset {name}")
        if all((temp / name).is_file() for name in (
            archive_name, CHECKSUMS, RUNTIME_MANIFEST, THIRD_PARTY_NOTICES
        )) and tag_commit:
            failures.extend(
                validate_release_set(
                    temp, tag=tag, tag_commit=tag_commit, repo=ROOT,
                )
            )
        local_archive = ROOT / archive_name
        if local_archive.is_file() and (temp / archive_name).is_file():
            if _sha256(local_archive) != _sha256(temp / archive_name):
                failures.append("published ZIP differs from the local release ZIP")

        latest = _gh(
            ["api", f"repos/{REPOSITORY}/releases/latest", "--jq", ".tag_name"]
        )
        if latest != tag:
            failures.append(
                f"{tag} is not the Latest release (GitHub says {latest!r})"
            )

    try:
        description = _gh(
            ["repo", "view", REPOSITORY, "--json", "description", "-q", ".description"]
        )
        for marker in ("user presets", "12 languages"):
            if marker not in description:
                failures.append(f"repo description lost the marker {marker!r}")
        print(f"    [OK] repo description: {description}")
    except subprocess.CalledProcessError:
        failures.append("could not read repository description")

    print("=" * 60)
    if failures:
        print(f"FAIL: {len(failures)}")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"OK: {tag} on GitHub matches tag, manifest and checksums")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
