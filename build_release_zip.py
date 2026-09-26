r"""Build a fail-closed NeuralScreen release set.

The release set consists of the application ZIP plus three public sidecars:
``runtime-manifest.json`` pins every runtime byte and the complete tracked
source/resource inventory; ``SHA256SUMS`` authenticates the ZIP, manifest and
third-party notice. The ZIP carries the same manifest and notice, and its own
member-level ``SHA256SUMS``.

Normal use (after the release commit has been tagged):

    runtime\python.exe build_release_zip.py v1.15.1

The command deliberately refuses release candidates, dirty tracked trees and
tags that do not resolve to HEAD. Ignored local files do not affect the gate.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import ntpath
import os
import re
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable, Sequence


BASE = Path(__file__).resolve().parent
VERSION = "2.1.6"
EXPECTED_TAG = f"v{VERSION}"
TARGET_ARCHS = (
    "RTX 30/40/50 (sm_86/89/120 kernels, spoof 0x1B0; "
    "RTX 20 cannot run - below minimum)"
)
RUNTIME_MANIFEST = "runtime-manifest.json"
THIRD_PARTY_NOTICES = "THIRD-PARTY-NOTICES.md"
CHECKSUMS = "SHA256SUMS"

# The documents a release page must carry, on top of the four artifacts this
# builder emits. They are inside the archive as payload, but GitHub needs them
# as separate assets, and uploading them by hand is exactly the step that was
# forgotten once: the verifier reported "release vX is missing asset README.md"
# after everything else had already passed. verify_github.py holds the same set
# in `required_assets`; the release commands in RELEASING.md are built from it.
RELEASE_DOCUMENTS = ("README.md", "README.ru.md", "TECHNICAL.md", "TECHNICAL.ru.md")

MANDATORY_FILES = (
    "config.default.json",
    "native/libraries/README.md",
    "native/nvngx_dlssg.dll",
    "app/resolution_limits.py",
    "NeuralScreen.exe",
    "NeuralScreen.vbs",
    "NeuralScreen-diag.vbs",
    "README.md",
    "README.ru.md",
    "LICENSE",
    "native/nvngx.dll",
    "native/nvngx.dll_ns-forwarder.dll",
    "native/nvngx_dlssnr.dll",
    "native/Spout.dll",
    "native/SpoutDX.dll",
    RUNTIME_MANIFEST,
    THIRD_PARTY_NOTICES,
)

# Pinned individually in addition to the complete runtime tree digest.
REQUIRED_RUNTIME_ARTIFACTS = (
    "NeuralScreen.exe",
    "native/nvngx.dll",
    "native/nvngx.dll_ns-forwarder.dll",
    "native/nvngx_dlssg.dll",
    "native/nvngx_dlssnr.dll",
    "native/Spout.dll",
    "native/SpoutDX.dll",
    "runtime/python.exe",
    "runtime/pythonw.exe",
)

TK_SKIP = (
    "runtime/tcl/",
    "runtime/tcl86t.dll",
    "runtime/tk86t.dll",
    "runtime/_tkinter.pyd",
    "runtime/Lib/tkinter/",
)
SP = "runtime/Lib/site-packages/"
DROP_PACKAGES = {
    "gradio", "gradio_client", "hf_gradio", "huggingface_hub", "hf_xet",
    "fastapi", "starlette", "uvicorn", "pydantic", "pydantic_core",
    "annotated_types", "annotated_doc", "typing_inspection", "safehttpx",
    "groovy", "pydub", "python_multipart", "multipart", "orjson", "httpx",
    "httpcore", "h11", "anyio", "idna", "certifi", "fsspec", "filelock",
    "jinja2", "markupsafe", "tqdm", "audioop", "audioop_lts", "brotli",
    "_brotli", "yaml", "_yaml", "pyyaml", "semantic_version", "tomlkit",
    "typer", "click", "shellingham", "rich", "markdown_it",
    "markdown_it_py", "mdurl", "pygments", "pandas", "pytz", "tzdata",
    "dateutil", "python_dateutil", "PyInstaller", "pyinstaller",
    "_pyinstaller_hooks_contrib", "pyinstaller_hooks_contrib", "altgraph",
    "pefile", "peutils", "ordlookup", "psutil", "pip", "setuptools",
    "pkg_resources", "_distutils_hack", "distutils-precedence.pth",
}
DEV_ONLY = {
    "autocheck.py", "run_tests.py", "build_release_zip.py",
    "verify_github.py", "docs/", "native/include/spout/",
    "native/spout_bridge.h", "native/spout_bridge.cpp",
    "native/spout_sender.cpp", "native/spout_receiver.cpp",
    "native/spout_roundtrip.cpp", "native/spout_compile_check.cpp",
    "native/spout_adapter_check.cpp", "native/build-spout-test.bat",
    "native/build-spout-check.bat", "native/build-spout-adapter.bat",
    "native/SpoutDX.lib",
    # The GPU recorder's own check: a stand-alone exercise of gpu_recorder.cpp
    # with no worker and no NGX, built and run by tests/test_gpu_recorder.py.
    # Same kind of thing as the spout checks above - a development harness,
    # not something the release ships.
    "native/gpu_recorder_check.cpp", "native/build-gpu-recorder-check.bat",
}
META_FILES = {RUNTIME_MANIFEST, THIRD_PARTY_NOTICES, CHECKSUMS, "VERSION.txt"}
SOURCE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".inl",
    ".hlsl", ".glsl", ".vert", ".frag", ".comp", ".metal", ".wgsl",
    ".rc",
}
VERSION_SOURCE_PATHS = (
    "build_release_zip.py",
    "app/settings_io.py",
    "native/launcher.rc",
)
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
ZIP_MODE = 0o100644 << 16


class ReleaseContractError(RuntimeError):
    """The release cannot be proven to match its declared contract."""


def _norm(path: str | Path) -> str:
    return os.fspath(path).replace("\\", "/")


def _safe_relpath(path: str | Path, *, label: str = "release path") -> str:
    """Return one canonical archive path or reject Windows/POSIX escapes."""
    raw = os.fspath(path)
    norm = _norm(raw)
    posix = PurePosixPath(norm)
    windows = PureWindowsPath(norm)
    if (
        not norm
        or "\0" in norm
        or ":" in norm
        or norm.startswith(("/", "\\"))
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or ntpath.splitdrive(norm)[0]
        or any(part in ("", ".", "..") for part in norm.split("/"))
        or posix.as_posix() != norm
    ):
        raise ReleaseContractError(f"unsafe {label}: {raw!r}")
    return norm


def _repo_file(repo: Path, rel: str | Path) -> Path:
    norm = _safe_relpath(rel)
    path = repo.joinpath(*PurePosixPath(norm).parts)
    try:
        path.resolve().relative_to(repo.resolve())
    except ValueError as exc:
        raise ReleaseContractError(f"release path escapes repository: {norm!r}") from exc
    if path.is_symlink():
        raise ReleaseContractError(f"symbolic links are not release inputs: {norm}")
    return path


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    if not check and result.returncode:
        return ""
    if check and result.returncode:
        raise ReleaseContractError(
            f"git {' '.join(args)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _git_bytes(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, check=False,
    )
    if result.returncode:
        error = result.stderr.decode("utf-8", "replace").strip()
        raise ReleaseContractError(f"git {' '.join(args)} failed: {error}")
    return result.stdout


def git_tree(repo: Path, ref: str = "HEAD") -> dict[str, dict[str, str]]:
    """Return regular tracked blobs from a commit/tree, keyed by safe path."""
    output = _git_bytes(repo, "ls-tree", "-r", "-z", "--full-tree", ref)
    result: dict[str, dict[str, str]] = {}
    for raw in output.split(b"\0"):
        if not raw:
            continue
        try:
            metadata, raw_path = raw.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split(" ", 2)
            rel = _safe_relpath(raw_path.decode("utf-8"), label="Git path")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ReleaseContractError(f"invalid Git tree entry at {ref!r}") from exc
        if kind != "blob" or mode == "120000":
            continue
        result[rel] = {"mode": mode, "oid": oid}
    return result


def _git_blob_bytes(repo: Path, oid: str) -> bytes:
    return _git_bytes(repo, "cat-file", "blob", oid)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _drop_sitepackage(norm: str) -> bool:
    if not norm.startswith(SP):
        return False
    entry = norm[len(SP):].split("/", 1)[0]
    return any(
        entry == name or entry == name + ".py" or entry == name + ".libs"
        or entry.startswith(name + "-")
        for name in DROP_PACKAGES
    )


def _skip(path: str | Path) -> bool:
    norm = _norm(path)
    if norm == "config.json":
        return True
    if any(norm == item or norm.startswith(item) for item in TK_SKIP):
        return True
    if norm in DEV_ONLY or norm.startswith("tests/") or norm.startswith("test_"):
        return True
    if norm.startswith("docs/"):
        return True
    if norm.startswith("native/include/spout/") or norm.startswith("native/spout_"):
        return True
    if norm.startswith("native/build-spout-") or norm == "native/SpoutDX.lib":
        return True
    runtime_assets = {"native/neuralscreen.ico"}
    if norm.startswith("native/") and not norm.endswith(".dll") and norm not in runtime_assets:
        return True
    if "/test/" in norm or "/tests/" in norm or "/testing/" in norm:
        return True
    if "_tests." in norm or norm.startswith(SP + "pygame/tests/"):
        return True
    if norm.startswith(SP + "pygame/examples/") or norm.startswith(SP + "pygame/docs/"):
        return True
    if norm == SP + "numpy/conftest.py" or norm.startswith(SP + "numpy/ma/testutils"):
        return True
    if norm == SP + "numpy/_pytesttester.pyi":
        return True
    if norm.endswith(".pyc") or "/__pycache__/" in norm:
        return True
    return _drop_sitepackage(norm)


def runtime_files(repo: Path) -> list[str]:
    root = repo / "runtime"
    if not root.is_dir():
        return []
    result = []
    for path in root.rglob("*"):
        if path.is_file():
            rel = _safe_relpath(path.relative_to(repo), label="runtime path")
            _repo_file(repo, rel)
            if not _skip(rel):
                result.append(rel)
    return sorted(result)


def assert_clean_tracked_tree(repo: Path) -> None:
    dirty = _git(repo, "status", "--porcelain=v1", "--untracked-files=no")
    if dirty:
        raise ReleaseContractError(
            "tracked working tree is dirty; commit or restore these paths:\n" + dirty
        )


def assert_release_tag(repo: Path, expected_tag: str, version: str) -> str:
    canonical = f"v{version}"
    if expected_tag != canonical:
        raise ReleaseContractError(
            f"expected tag must be {canonical}, got {expected_tag!r}"
        )
    head = _git(repo, "rev-parse", "HEAD")
    tagged = _git(
        repo, "rev-parse", "--verify", f"{expected_tag}^{{commit}}", check=False
    )
    if not tagged:
        raise ReleaseContractError(f"required tag {expected_tag} does not exist")
    if tagged != head:
        raise ReleaseContractError(
            f"tag {expected_tag} resolves to {tagged[:12]}, HEAD is {head[:12]}"
        )
    return head


def _file_record(repo: Path, rel: str, role: str | None = None) -> dict:
    rel = _safe_relpath(rel)
    path = _repo_file(repo, rel)
    record = {
        "path": rel,
        "size": path.stat().st_size,
        "sha256": _sha256_file(path),
    }
    if role:
        record["role"] = role
    return record


def _git_file_record(
    repo: Path,
    rel: str,
    entry: dict[str, str],
    role: str | None = None,
) -> dict:
    rel = _safe_relpath(rel, label="Git record path")
    data = _git_blob_bytes(repo, entry["oid"])
    record = {
        "path": rel,
        "size": len(data),
        "sha256": _sha256_bytes(data),
        "git_blob": entry["oid"],
    }
    if role:
        record["role"] = role
    return record


def _source_inventory_paths(
    repo: Path,
    tracked: Sequence[str],
    mandatory_files: Sequence[str] = MANDATORY_FILES,
) -> list[str]:
    """Every tracked native source/shader plus every tracked shipped resource."""
    del repo  # kept in the signature for a stable public helper
    mandatory = {
        _safe_relpath(item, label="mandatory release path")
        for item in mandatory_files
    }
    generated_meta = {RUNTIME_MANIFEST, CHECKSUMS, "VERSION.txt"}
    result = set()
    for rel in tracked:
        native_source = Path(rel).suffix.lower() in SOURCE_SUFFIXES
        version_source = rel in VERSION_SOURCE_PATHS
        shipped_resource = (
            (not _skip(rel) or rel in mandatory)
            and rel not in generated_meta
        )
        if native_source or version_source or shipped_resource:
            result.add(rel)
    return sorted(result)


def _decode_source(data: bytes, path: str) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ReleaseContractError(f"version source is not UTF-8: {path}") from exc


def _single_match(pattern: str, text: str, path: str, field: str) -> str:
    values = re.findall(pattern, text, re.MULTILINE)
    if len(values) != 1:
        raise ReleaseContractError(
            f"{path} must declare exactly one {field}; found {len(values)}"
        )
    value = values[0]
    if isinstance(value, tuple):
        value = ".".join(value)
    return value


def _version_values(path: str, data: bytes) -> dict[str, str]:
    text = _decode_source(data, path)
    if path == "build_release_zip.py":
        return {
            "VERSION": _single_match(
                r'^VERSION\s*=\s*["\']([^"\']+)["\']\s*$',
                text, path, "VERSION",
            )
        }
    if path == "app/settings_io.py":
        return {
            "APP_VERSION": _single_match(
                r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']\s*$',
                text, path, "APP_VERSION",
            )
        }
    if path == "native/launcher.rc":
        return {
            "FILEVERSION": _single_match(
                r"^\s*FILEVERSION\s+(\d+),(\d+),(\d+),(\d+)\s*$",
                text, path, "FILEVERSION",
            ),
            "PRODUCTVERSION": _single_match(
                r"^\s*PRODUCTVERSION\s+(\d+),(\d+),(\d+),(\d+)\s*$",
                text, path, "PRODUCTVERSION",
            ),
            "FileVersion": _single_match(
                r'^\s*VALUE\s+"FileVersion",\s*"([^"]+)"\s*$',
                text, path, "FileVersion string",
            ),
            "ProductVersion": _single_match(
                r'^\s*VALUE\s+"ProductVersion",\s*"([^"]+)"\s*$',
                text, path, "ProductVersion string",
            ),
        }
    raise ReleaseContractError(f"unknown version source: {path}")


def _version_source_records(
    repo: Path, tree: dict[str, dict[str, str]]
) -> list[dict]:
    records = []
    for rel in VERSION_SOURCE_PATHS:
        entry = tree.get(rel)
        if entry is None:
            raise ReleaseContractError(f"version source is not tracked: {rel}")
        data = _git_blob_bytes(repo, entry["oid"])
        record = _git_file_record(repo, rel, entry, "version-source")
        record["values"] = _version_values(rel, data)
        records.append(record)
    return records


def assert_version_coherence(version: str, records: Sequence[dict]) -> None:
    parts = version.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise ReleaseContractError(f"release version is not X.Y.Z: {version!r}")
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
    actual = {record.get("path"): record.get("values") for record in records}
    if actual != expected:
        raise ReleaseContractError(
            "version drift: builder, APP_VERSION and launcher.rc disagree; "
            f"expected {expected}, got {actual}"
        )


def _runtime_tree_digest(records: Sequence[dict]) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(record["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(record["sha256"].encode("ascii"))
        digest.update(b"\0")
        digest.update(str(record["size"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _package_paths(
    tree: dict[str, dict[str, str]],
    mandatory_files: Sequence[str],
    runtime_paths: Sequence[str],
) -> list[str]:
    mandatory = {
        _safe_relpath(rel, label="mandatory release path")
        for rel in mandatory_files
    }
    result = set()
    for rel in [*sorted(tree), *sorted(mandatory), *sorted(runtime_paths)]:
        rel = _safe_relpath(rel)
        if rel in META_FILES or (_skip(rel) and rel not in mandatory):
            continue
        result.add(rel)
    return sorted(result)


def _package_inventory_records(
    repo: Path,
    tree: dict[str, dict[str, str]],
    mandatory_files: Sequence[str],
    required_runtime_artifacts: Sequence[str],
    runtime_paths: Sequence[str],
) -> list[dict]:
    records = []
    for rel in _package_paths(tree, mandatory_files, runtime_paths):
        entry = tree.get(rel)
        if entry is not None:
            record = _git_file_record(repo, rel, entry)
            record["origin"] = "git"
        else:
            if not _generated_input_allowed(rel, required_runtime_artifacts):
                raise ReleaseContractError(
                    f"release input is not tracked by the tag: {rel}"
                )
            path = _repo_file(repo, rel)
            if not path.is_file():
                raise ReleaseContractError(f"release input is missing: {rel}")
            record = _file_record(repo, rel)
            record["origin"] = "generated"
        records.append(record)
    return records


def create_runtime_manifest(
    repo: Path = BASE,
    *,
    version: str = VERSION,
    expected_tag: str | None = None,
    git_ref: str = "HEAD",
    mandatory_files: Sequence[str] = MANDATORY_FILES,
    required_runtime_artifacts: Sequence[str] = REQUIRED_RUNTIME_ARTIFACTS,
) -> dict:
    repo = Path(repo).resolve()
    tag = expected_tag or f"v{version}"
    tree = git_tree(repo, git_ref)
    tracked = sorted(tree)
    inventory = [
        _git_file_record(
            repo, rel, tree[rel],
            "native-source"
            if Path(rel).suffix.lower() in SOURCE_SUFFIXES
            else "version-source"
            if rel in VERSION_SOURCE_PATHS
            else "shipped-resource",
        )
        for rel in _source_inventory_paths(repo, tracked, mandatory_files)
    ]
    runtime_paths = runtime_files(repo)
    runtime_records = [_file_record(repo, rel) for rel in runtime_paths]
    artifacts = []
    for rel in required_runtime_artifacts:
        rel = _safe_relpath(rel, label="runtime artifact path")
        path = _repo_file(repo, rel)
        if not path.is_file():
            raise ReleaseContractError(f"required runtime artifact is missing: {rel}")
        if rel in tree:
            artifacts.append(_git_file_record(repo, rel, tree[rel]))
        else:
            artifacts.append(_file_record(repo, rel))
    version_sources = _version_source_records(repo, tree)
    package_records = _package_inventory_records(
        repo,
        tree,
        mandatory_files,
        required_runtime_artifacts,
        runtime_paths,
    )
    return {
        "schema_version": 3,
        "product": "NeuralScreen",
        "version": version,
        "expected_tag": tag,
        "version_sources": version_sources,
        "runtime": {
            "file_count": len(runtime_records),
            "tree_sha256": _runtime_tree_digest(runtime_records),
            "files": runtime_records,
            "artifacts": artifacts,
        },
        "package": {
            "file_count": len(package_records),
            "tree_sha256": _runtime_tree_digest(package_records),
            "files": package_records,
        },
        "source_inventory": inventory,
    }


def write_runtime_manifest(
    repo: Path = BASE,
    *,
    version: str = VERSION,
    expected_tag: str | None = None,
    mandatory_files: Sequence[str] = MANDATORY_FILES,
    required_runtime_artifacts: Sequence[str] = REQUIRED_RUNTIME_ARTIFACTS,
) -> Path:
    repo = Path(repo).resolve()
    assert_clean_tracked_tree(repo)
    tree = git_tree(repo, "HEAD")
    _assert_mandatory_inputs(
        repo,
        tree,
        mandatory_files,
        required_runtime_artifacts,
        ref="HEAD",
        allow_manifest_output=True,
    )
    manifest = create_runtime_manifest(
        repo, version=version, expected_tag=expected_tag,
        git_ref="HEAD",
        mandatory_files=mandatory_files,
        required_runtime_artifacts=required_runtime_artifacts,
    )
    assert_version_coherence(version, manifest["version_sources"])
    destination = Path(repo) / RUNTIME_MANIFEST
    destination.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n",
    )
    return destination


def validate_runtime_manifest(
    repo: Path,
    *,
    version: str,
    expected_tag: str,
    git_ref: str,
    mandatory_files: Sequence[str] = MANDATORY_FILES,
    required_runtime_artifacts: Sequence[str] = REQUIRED_RUNTIME_ARTIFACTS,
) -> tuple[dict, bytes]:
    tree = git_tree(repo, git_ref)
    entry = tree.get(RUNTIME_MANIFEST)
    if entry is None:
        raise ReleaseContractError(
            f"{RUNTIME_MANIFEST} is not tracked by release ref {git_ref}"
        )
    raw = _git_blob_bytes(repo, entry["oid"])
    try:
        expected = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseContractError(f"cannot read {RUNTIME_MANIFEST}: {exc}") from exc
    actual = create_runtime_manifest(
        repo, version=version, expected_tag=expected_tag,
        git_ref=git_ref,
        mandatory_files=mandatory_files,
        required_runtime_artifacts=required_runtime_artifacts,
    )
    assert_version_coherence(version, actual["version_sources"])
    if expected != actual:
        raise ReleaseContractError(
            f"{RUNTIME_MANIFEST} does not match tagged runtime/source inventory; "
            "regenerate and review it before the release commit"
        )
    canonical = (
        json.dumps(expected, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    if raw != canonical:
        raise ReleaseContractError(
            f"{RUNTIME_MANIFEST} must use canonical UTF-8 JSON formatting"
        )
    return expected, canonical


_FATBIN_MAGIC = struct.pack("<I", 0xBA55ED50)
SM_NAMES = {75: "sm_75", 86: "sm_86", 89: "sm_89", 120: "sm_120"}
KNOWN_SM = set(SM_NAMES) | {50, 52, 53, 60, 61, 62, 70, 72, 80, 90, 100, 101, 110}


def dll_architectures(path: str | Path) -> set[int]:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return set()
    found: collections.Counter[int] = collections.Counter()
    offset = 0
    while True:
        index = data.find(_FATBIN_MAGIC, offset)
        if index < 0:
            break
        offset = index + 4
        try:
            header_size = struct.unpack_from("<H", data, index + 6)[0]
            fat_size = struct.unpack_from("<Q", data, index + 8)[0]
            if header_size < 16 or not (0 < fat_size <= len(data)):
                continue
            cursor, end = index + header_size, index + header_size + fat_size
            while cursor < end - 32:
                entry_header = struct.unpack_from("<I", data, cursor + 4)[0]
                payload = struct.unpack_from("<Q", data, cursor + 8)[0]
                if entry_header < 24 or entry_header > 4096 or not (0 < payload <= len(data)):
                    break
                for sm_offset in (24, 28, 20):
                    if cursor + sm_offset + 4 <= len(data):
                        sm = struct.unpack_from("<I", data, cursor + sm_offset)[0]
                        if sm in KNOWN_SM:
                            found[sm] += 1
                            break
                cursor += entry_header + payload
        except (IndexError, struct.error):
            continue
    return set(found)


def _checksum_text(records: Iterable[tuple[str, str]]) -> bytes:
    normalized = [
        (_safe_relpath(name, label="checksum path"), digest)
        for name, digest in records
    ]
    lines = [f"{digest} *{name}" for name, digest in sorted(normalized)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(_safe_relpath(name, label="ZIP member"), ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = ZIP_MODE
    info.flag_bits = 0
    return info


def _write_zip_member(bundle: zipfile.ZipFile, name: str, data: bytes) -> None:
    bundle.writestr(
        _zip_info(name), data, compress_type=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    )


def _generated_input_allowed(
    rel: str, required_runtime_artifacts: Sequence[str]
) -> bool:
    allowed = {
        _safe_relpath(item, label="runtime artifact path")
        for item in required_runtime_artifacts
    }
    return rel.startswith("runtime/") or rel in allowed


def _assert_mandatory_inputs(
    repo: Path,
    tree: dict[str, dict[str, str]],
    mandatory_files: Sequence[str],
    required_runtime_artifacts: Sequence[str],
    *,
    ref: str,
    allow_manifest_output: bool = False,
) -> list[str]:
    mandatory = [
        _safe_relpath(rel, label="mandatory release path")
        for rel in mandatory_files
    ]
    missing = []
    for rel in mandatory:
        if rel in tree or (allow_manifest_output and rel == RUNTIME_MANIFEST):
            continue
        path = _repo_file(repo, rel)
        if not path.is_file():
            missing.append(rel)
        elif not _generated_input_allowed(rel, required_runtime_artifacts):
            raise ReleaseContractError(
                f"mandatory release input is untracked at {ref}: {rel}"
            )
    if missing:
        raise ReleaseContractError(
            "mandatory release files are missing: " + ", ".join(missing)
        )
    return mandatory


def _release_input_bytes(
    repo: Path,
    rel: str,
    tree: dict[str, dict[str, str]],
    required_runtime_artifacts: Sequence[str],
) -> bytes:
    rel = _safe_relpath(rel)
    entry = tree.get(rel)
    if entry is not None:
        return _git_blob_bytes(repo, entry["oid"])
    if not _generated_input_allowed(rel, required_runtime_artifacts):
        raise ReleaseContractError(
            f"release input is not tracked by the tag: {rel}"
        )
    path = _repo_file(repo, rel)
    if not path.is_file():
        raise ReleaseContractError(f"release input is missing: {rel}")
    return path.read_bytes()


def _assert_record_matches_bytes(record: dict, data: bytes, label: str) -> None:
    path = record.get("path", "?")
    if record.get("size") != len(data):
        raise ReleaseContractError(f"{label} size changed during build: {path}")
    if record.get("sha256") != _sha256_bytes(data):
        raise ReleaseContractError(f"{label} bytes changed during build: {path}")


def _assert_payload_matches_manifest(
    manifest: dict,
    package_data: dict[str, bytes],
    notice_bytes: bytes,
) -> None:
    package_records = {
        record["path"]: record for record in manifest["package"]["files"]
    }
    if set(package_records) != set(package_data):
        raise ReleaseContractError(
            "package inventory changed between manifest validation and packaging"
        )
    for path, record in package_records.items():
        _assert_record_matches_bytes(record, package_data[path], "package file")
    runtime = manifest["runtime"]
    runtime_records = {record["path"]: record for record in runtime["files"]}
    packaged_runtime = {
        path for path in package_data if path.startswith("runtime/")
    }
    if set(runtime_records) != packaged_runtime:
        raise ReleaseContractError(
            "runtime inventory changed between manifest validation and packaging"
        )
    for path, record in runtime_records.items():
        _assert_record_matches_bytes(record, package_data[path], "runtime file")
    for record in runtime["artifacts"]:
        path = record["path"]
        data = package_data.get(path)
        if data is None:
            raise ReleaseContractError(f"runtime artifact is not packaged: {path}")
        _assert_record_matches_bytes(record, data, "runtime artifact")
    notice_record = next(
        (
            record for record in manifest["source_inventory"]
            if record["path"] == THIRD_PARTY_NOTICES
        ),
        None,
    )
    if notice_record is None:
        raise ReleaseContractError(
            f"source inventory does not cover {THIRD_PARTY_NOTICES}"
        )
    _assert_record_matches_bytes(
        notice_record, notice_bytes, "third-party notice"
    )


def build_release(
    repo: Path = BASE,
    *,
    version: str = VERSION,
    expected_tag: str | None = None,
    output_dir: Path | None = None,
    mandatory_files: Sequence[str] = MANDATORY_FILES,
    required_runtime_artifacts: Sequence[str] = REQUIRED_RUNTIME_ARTIFACTS,
    required_architectures: Sequence[int] = (75, 86, 89, 120),
) -> Path:
    repo = Path(repo).resolve()
    output_dir = Path(output_dir or repo).resolve()
    tag = expected_tag or f"v{version}"

    assert_clean_tracked_tree(repo)
    commit = assert_release_tag(repo, tag, version)
    tree = git_tree(repo, tag)
    mandatory = _assert_mandatory_inputs(
        repo,
        tree,
        mandatory_files,
        required_runtime_artifacts,
        ref=tag,
    )
    manifest, manifest_bytes = validate_runtime_manifest(
        repo, version=version, expected_tag=tag,
        git_ref=tag,
        mandatory_files=mandatory,
        required_runtime_artifacts=required_runtime_artifacts,
    )

    if required_architectures:
        architectures = dll_architectures(repo / "native/nvngx_dlssnr.dll")
        absent = [sm for sm in required_architectures if sm not in architectures]
        if absent:
            raise ReleaseContractError(
                "runtime architecture mismatch: missing "
                + ", ".join(f"sm_{sm}" for sm in absent)
            )
    else:
        architectures = set()

    package_files = [record["path"] for record in manifest["package"]["files"]]

    notice_entry = tree.get(THIRD_PARTY_NOTICES)
    if notice_entry is None:
        raise ReleaseContractError(
            f"{THIRD_PARTY_NOTICES} is not tracked by release tag {tag}"
        )
    notice_bytes = _git_blob_bytes(repo, notice_entry["oid"])
    version_bytes = (
        f"NeuralScreen {version}\n"
        f"commit: {commit}\n"
        f"tag: {tag}\n"
        f"runtime manifest: {RUNTIME_MANIFEST} sha256 {_sha256_bytes(manifest_bytes)}\n"
        f"kernel archs: {', '.join(SM_NAMES.get(sm, f'sm_{sm}') for sm in sorted(architectures)) or 'fixture'}\n"
        f"targets: {TARGET_ARCHS}\n"
    ).encode("utf-8")

    package_data = {
        rel: _release_input_bytes(repo, rel, tree, required_runtime_artifacts)
        for rel in package_files
    }
    _assert_payload_matches_manifest(manifest, package_data, notice_bytes)

    internal_hashes = [
        ("VERSION.txt", _sha256_bytes(version_bytes)),
        (RUNTIME_MANIFEST, _sha256_bytes(manifest_bytes)),
        (THIRD_PARTY_NOTICES, _sha256_bytes(notice_bytes)),
    ]
    internal_hashes.extend(
        (rel, _sha256_bytes(package_data[rel])) for rel in package_files
    )
    internal_checksums = _checksum_text(internal_hashes)

    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"neuralscreen-v{version}-full.zip"
    fd, temp_name = tempfile.mkstemp(
        prefix=archive.name + ".", suffix=".tmp", dir=output_dir
    )
    os.close(fd)
    temp_archive = Path(temp_name)
    try:
        with zipfile.ZipFile(
            temp_archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6
        ) as bundle:
            _write_zip_member(bundle, "VERSION.txt", version_bytes)
            _write_zip_member(bundle, RUNTIME_MANIFEST, manifest_bytes)
            _write_zip_member(bundle, THIRD_PARTY_NOTICES, notice_bytes)
            _write_zip_member(bundle, CHECKSUMS, internal_checksums)
            for rel in package_files:
                _write_zip_member(bundle, rel, package_data[rel])
        os.replace(temp_archive, archive)
    finally:
        temp_archive.unlink(missing_ok=True)

    sidecar_manifest = output_dir / RUNTIME_MANIFEST
    sidecar_notice = output_dir / THIRD_PARTY_NOTICES
    if sidecar_manifest.resolve() != (repo / RUNTIME_MANIFEST).resolve():
        sidecar_manifest.write_bytes(manifest_bytes)
    if sidecar_notice.resolve() != (repo / THIRD_PARTY_NOTICES).resolve():
        sidecar_notice.write_bytes(notice_bytes)
    sidecar_hashes = _checksum_text([
        (archive.name, _sha256_file(archive)),
        (RUNTIME_MANIFEST, _sha256_bytes(manifest_bytes)),
        (THIRD_PARTY_NOTICES, _sha256_bytes(notice_bytes)),
    ])
    checksums_path = output_dir / CHECKSUMS
    fd, temp_name = tempfile.mkstemp(
        prefix=CHECKSUMS + ".", suffix=".tmp", dir=output_dir
    )
    os.close(fd)
    temp_checksums = Path(temp_name)
    try:
        temp_checksums.write_bytes(sidecar_hashes)
        os.replace(temp_checksums, checksums_path)
    finally:
        temp_checksums.unlink(missing_ok=True)
    return archive


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", nargs="?", default=EXPECTED_TAG)
    parser.add_argument(
        "--write-runtime-manifest", action="store_true",
        help="regenerate the pinned manifest for review; does not build a release",
    )
    args = parser.parse_args(argv)
    try:
        if args.write_runtime_manifest:
            path = write_runtime_manifest(BASE, expected_tag=args.tag)
            print(f"wrote {path}")
            return 0
        archive = build_release(BASE, expected_tag=args.tag)
        print(f"built {archive.name} ({archive.stat().st_size} bytes)")
        print(f"sidecars: {CHECKSUMS}, {RUNTIME_MANIFEST}, {THIRD_PARTY_NOTICES}")
        # The release command, with the complete asset list. Assembling it by
        # hand is how the four documents were left off a published release once:
        # the builder never named them as assets, and nothing failed until the
        # verifier ran.
        assets = " ".join(
            [archive.name, CHECKSUMS, RUNTIME_MANIFEST, THIRD_PARTY_NOTICES,
             *RELEASE_DOCUMENTS]
        )
        print("next: upload all eight assets -")
        print(f"  gh release create {args.tag} \\")
        print("    -R perseval-BLR/NeuralScreen \\")
        print('    --title "<one line>" \\')
        print("    --notes-file <draft> \\")
        print(f"    {assets}")
        return 0
    except ReleaseContractError as exc:
        print(f"RELEASE CONTRACT FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
