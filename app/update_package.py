"""Standard-library validation and staging for official NeuralScreen packages."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath


MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_DOWNLOAD_CHUNK = 1024 * 1024
MIN_FREE_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_CHECKSUM_BYTES = 32 * 1024 * 1024
MAX_VERSION_BYTES = 1024 * 1024
MAX_NOTICES_BYTES = 64 * 1024 * 1024
METADATA_FILES = (
    "VERSION.txt", "runtime-manifest.json", "THIRD-PARTY-NOTICES.md",
    "SHA256SUMS",
)
REQUIRED_PACKAGE_FILES = {
    "NeuralScreen.exe", "native/libraries/README.md", "runtime/python.exe",
    "runtime/pythonw.exe", "runtime/python313.dll", "runtime/python313.zip",
    "runtime/python313._pth",
}
PROTECTED_NAMES = {
    "config.json", "compatibility-cache.json", ".neuralscreen-update.json",
}
PROTECTED_ROOTS = {
    "logs", "screenshots", "recordings", "conversions", "converted",
    "exports", "output", "media",
}


class UpdateError(RuntimeError):
    """A fixed, user-displayable updater failure code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ManagedFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class StagedRelease:
    transaction_dir: Path
    candidate_dir: Path
    archive_path: Path
    version: str
    tag: str
    new_inventory: tuple[ManagedFile, ...]
    old_inventory: tuple[ManagedFile, ...] = ()
    installed_version: str | None = None
    can_install: bool = False
    refusal_code: str | None = None

    @property
    def managed_union(self) -> tuple[str, ...]:
        names = {item.path.casefold(): item.path for item in self.old_inventory}
        names.update({item.path.casefold(): item.path for item in self.new_inventory})
        return tuple(sorted(names.values(), key=str.casefold))


def parse_version(value: object) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise UpdateError("invalid_release")
    match = re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value)
    if match is None:
        raise UpdateError("invalid_release")
    try:
        return tuple(int(part) for part in match.groups())  # type: ignore[return-value]
    except ValueError as exc:
        raise UpdateError("invalid_release") from exc


def safe_package_path(value: object) -> str:
    if (not isinstance(value, str) or not value or "\\" in value
            or any(ord(char) < 32 or char in '<>:"|?*' for char in value)):
        raise UpdateError("invalid_package")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (value.startswith("/") or posix.is_absolute()
            or windows.is_absolute() or windows.drive
            or any(part in ("", ".", "..") for part in value.split("/"))
            or posix.as_posix() != value):
        raise UpdateError("invalid_package")
    for part in value.split("/"):
        if part.endswith((".", " ")):
            raise UpdateError("invalid_package")
        stem = part.split(".", 1)[0].rstrip(" .").upper()
        if stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}:
            raise UpdateError("invalid_package")
        if re.fullmatch(r"(?:COM|LPT)(?:[1-9]|[¹²³])", stem):
            raise UpdateError("invalid_package")
    return value


def _reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    attrs = getattr(info, "st_file_attributes", 0)
    return path.is_symlink() or bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _ensure_no_reparse(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if _reparse(current):
            raise UpdateError("invalid_package")


def _record_tree_digest(records: list[dict]) -> str:
    digest = hashlib.sha256()
    for record in sorted(records, key=lambda item: item["path"]):
        digest.update(record["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(record["sha256"].encode("ascii"))
        digest.update(b"\0")
        digest.update(str(record["size"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _sha256_stream(stream) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    while True:
        block = stream.read(MAX_DOWNLOAD_CHUNK)
        if not block:
            break
        total += len(block)
        digest.update(block)
    return total, digest.hexdigest()


def _sha256_file(path: Path) -> tuple[int, str]:
    with path.open("rb") as stream:
        return _sha256_stream(stream)


def _strict_json(data: bytes) -> dict:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (UnicodeDecodeError, ValueError) as exc:
        raise UpdateError("invalid_package") from exc
    if not isinstance(value, dict):
        raise UpdateError("invalid_package")
    return value


def _valid_record(record: object, *, git: bool = False) -> bool:
    if not isinstance(record, dict):
        return False
    try:
        safe_package_path(record.get("path"))
    except UpdateError:
        return False
    size, checksum = record.get("size"), record.get("sha256")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return False
    if not isinstance(checksum, str) or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
        return False
    return (not git or isinstance(record.get("git_blob"), str)
            and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", record["git_blob"]) is not None)


def _validate_manifest(manifest_bytes: bytes, tag: str, version: str) -> tuple[dict, dict[str, dict]]:
    manifest = _strict_json(manifest_bytes)
    if manifest_bytes != (json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"):
        raise UpdateError("invalid_package")
    if (manifest.get("schema_version") != 3 or manifest.get("product") != "NeuralScreen"
            or manifest.get("version") != version or manifest.get("expected_tag") != tag
            or tag != f"v{version}"):
        raise UpdateError("invalid_package")
    parse_version(version)

    package = manifest.get("package")
    records = package.get("files") if isinstance(package, dict) else None
    if (not isinstance(records, list) or isinstance(package.get("file_count"), bool)
            or package.get("file_count") != len(records)):
        raise UpdateError("invalid_package")
    by_path: dict[str, dict] = {}
    keys: set[str] = set()
    for record in records:
        if not _valid_record(record):
            raise UpdateError("invalid_package")
        path = record["path"]
        key = path.casefold()
        if key in keys or key in {item.casefold() for item in METADATA_FILES}:
            raise UpdateError("invalid_package")
        if _protected(path) and path.casefold() != "native/libraries/readme.md":
            raise UpdateError("invalid_package")
        keys.add(key)
        by_path[path] = record
        origin = record.get("origin")
        if origin not in {"git", "generated"}:
            raise UpdateError("invalid_package")
        if origin == "git" and not _valid_record(record, git=True):
            raise UpdateError("invalid_package")
        if origin == "generated" and "git_blob" in record:
            raise UpdateError("invalid_package")
        if "eol" in record and (origin != "git" or record["eol"] != "crlf"):
            raise UpdateError("invalid_package")
    if [record["path"] for record in records] != sorted(by_path):
        raise UpdateError("invalid_package")
    if package.get("tree_sha256") != _record_tree_digest(records):
        raise UpdateError("invalid_package")
    if not REQUIRED_PACKAGE_FILES <= set(by_path):
        raise UpdateError("invalid_package")
    for path in by_path:
        components = path.casefold().split("/")
        if any("/".join(components[:index]) in keys for index in range(1, len(components))):
            raise UpdateError("invalid_package")

    sources = manifest.get("source_inventory")
    if not isinstance(sources, list):
        raise UpdateError("invalid_package")
    source_by_path: dict[str, dict] = {}
    for record in sources:
        if (not _valid_record(record, git=True)
                or record["path"].casefold() in {path.casefold() for path in source_by_path}):
            raise UpdateError("invalid_package")
        source_by_path[record["path"]] = record
    for path, record in by_path.items():
        if record["origin"] != "git":
            continue
        source = source_by_path.get(path)
        if source is None or source.get("git_blob") != record.get("git_blob"):
            raise UpdateError("invalid_package")
        if "eol" not in record and any(source.get(key) != record.get(key) for key in ("size", "sha256")):
            raise UpdateError("invalid_package")

    version_sources = manifest.get("version_sources")
    required_sources = {"build_release_zip.py", "app/settings_io.py", "native/launcher.rc"}
    if not isinstance(version_sources, list) or len(version_sources) != 3:
        raise UpdateError("invalid_package")
    version_by_path = {}
    for record in version_sources:
        if not _valid_record(record, git=True) or record["path"] in version_by_path:
            raise UpdateError("invalid_package")
        version_by_path[record["path"]] = record
    if set(version_by_path) != required_sources:
        raise UpdateError("invalid_package")
    parts = version.split(".")
    rc_version = ".".join((*parts, "0"))
    expected_values = {
        "build_release_zip.py": {"VERSION": version},
        "app/settings_io.py": {"APP_VERSION": version},
        "native/launcher.rc": {
            "FILEVERSION": rc_version, "PRODUCTVERSION": rc_version,
            "FileVersion": rc_version, "ProductVersion": version,
        },
    }
    for path, values in expected_values.items():
        record = version_by_path[path]
        source = source_by_path.get(path)
        if record.get("values") != values or source is None or any(
            source.get(key) != record.get(key) for key in ("size", "sha256", "git_blob")
        ):
            raise UpdateError("invalid_package")

    runtime = manifest.get("runtime")
    runtime_files = runtime.get("files") if isinstance(runtime, dict) else None
    artifacts = runtime.get("artifacts") if isinstance(runtime, dict) else None
    if not isinstance(runtime_files, list) or not isinstance(artifacts, list):
        raise UpdateError("invalid_package")
    runtime_by_path: dict[str, dict] = {}
    for record in runtime_files:
        if not _valid_record(record) or not record["path"].startswith("runtime/") or record["path"] in runtime_by_path:
            raise UpdateError("invalid_package")
        runtime_by_path[record["path"]] = record
        packed = by_path.get(record["path"])
        if packed is None or any(packed.get(key) != record.get(key) for key in ("size", "sha256")):
            raise UpdateError("invalid_package")
    expected_runtime = {path for path in by_path if path.startswith("runtime/")}
    if (set(runtime_by_path) != expected_runtime
            or isinstance(runtime.get("file_count"), bool)
            or runtime.get("file_count") != len(runtime_by_path)
            or runtime.get("tree_sha256") != _runtime_digest(runtime_by_path.values())):
        raise UpdateError("invalid_package")
    for record in artifacts:
        if not _valid_record(record):
            raise UpdateError("invalid_package")
        packed = by_path.get(record["path"])
        if packed is None or any(packed.get(key) != record.get(key) for key in ("size", "sha256")):
            raise UpdateError("invalid_package")
    return manifest, by_path


def _runtime_digest(records) -> str:
    digest = hashlib.sha256()
    ordered = sorted(records, key=lambda item: item["path"])
    for record in ordered:
        digest.update(record["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(record["sha256"].encode("ascii"))
        digest.update(b"\0")
        digest.update(str(record["size"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _parse_checksums(data: bytes) -> dict[str, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UpdateError("invalid_package") from exc
    result = {}
    keys = set()
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line)
        if match is None:
            raise UpdateError("invalid_package")
        digest, name = match.groups()
        safe_package_path(name)
        key = name.casefold()
        if key in keys:
            raise UpdateError("invalid_package")
        keys.add(key)
        result[name] = digest
    if not result:
        raise UpdateError("invalid_package")
    return result


class _AssetRedirect(urllib.request.HTTPRedirectHandler):
    allowed_hosts = {
        "github.com", "release-assets.githubusercontent.com",
        "objects.githubusercontent.com",
    }

    def redirect_request(self, request, fp, code, message, headers, new_url):
        target = urllib.parse.urljoin(request.full_url, new_url)
        parsed = urllib.parse.urlsplit(target)
        if (parsed.scheme != "https" or parsed.hostname not in self.allowed_hosts
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443)):
            raise UpdateError("invalid_release")
        return super().redirect_request(request, fp, code, message, headers, target)


def _open_asset(url: str):
    expected_prefix = "https://github.com/perseval-BLR/NeuralScreen/releases/download/"
    parsed = urllib.parse.urlsplit(url)
    if (not url.startswith(expected_prefix) or parsed.scheme != "https"
            or parsed.hostname != "github.com" or parsed.username is not None
            or parsed.password is not None or parsed.port not in (None, 443)):
        raise UpdateError("invalid_release")
    request = urllib.request.Request(url, headers={
        "User-Agent": "NeuralScreen-Updater/1.0",
        "Accept": "application/octet-stream",
    })
    opener = urllib.request.build_opener(_AssetRedirect())
    try:
        return opener.open(request, timeout=15)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise UpdateError("rate_limit") from exc
        raise UpdateError("network") from exc
    except UpdateError:
        raise
    except (OSError, TimeoutError, ValueError) as exc:
        raise UpdateError("network") from exc


def _version_text_valid(data: bytes, version: str, tag: str, manifest_hash: str) -> bool:
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return False
    values = {}
    for line in lines:
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()
    return (lines and lines[0] == f"NeuralScreen {version}"
            and values.get("tag") == tag
            and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", values.get("commit", "")) is not None
            and values.get("runtime manifest") == f"runtime-manifest.json sha256 {manifest_hash}")


def _protected(path: str) -> bool:
    parts = path.casefold().split("/")
    return (path.casefold() in PROTECTED_NAMES or parts[0] in PROTECTED_ROOTS
            or path.casefold().startswith("native/libraries/"))


def _zip_mode_is_regular(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    return not info.is_dir() and kind in (0, stat.S_IFREG)


def _read_small_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    if info.file_size > limit:
        raise UpdateError("invalid_package")
    data = archive.read(info)
    if len(data) != info.file_size:
        raise UpdateError("invalid_package")
    return data


def _validate_archive(archive_path: Path, release) -> list[tuple[zipfile.ZipInfo, ManagedFile]]:
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise UpdateError("invalid_package") from exc
    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if not names:
            raise UpdateError("invalid_package")
        seen = set()
        for info in infos:
            name = safe_package_path(info.filename)
            key = name.casefold()
            if key in seen or not _zip_mode_is_regular(info):
                raise UpdateError("invalid_package")
            seen.add(key)
        if "runtime-manifest.json" not in names:
            raise UpdateError("invalid_package")
        if sum(info.file_size for info in infos) > MAX_ARCHIVE_BYTES:
            raise UpdateError("invalid_package")
        by_name = {info.filename: info for info in infos}
        manifest_bytes = _read_small_member(
            archive, by_name["runtime-manifest.json"], MAX_MANIFEST_BYTES
        )
        manifest, package_by_path = _validate_manifest(manifest_bytes, release.tag, release.version_text)
        expected = set(package_by_path) | set(METADATA_FILES)
        if set(names) != expected:
            raise UpdateError("invalid_package")
        if by_name["SHA256SUMS"].file_size > MAX_CHECKSUM_BYTES:
            raise UpdateError("invalid_package")
        checksums = _parse_checksums(_read_small_member(
            archive, by_name["SHA256SUMS"], MAX_CHECKSUM_BYTES
        ))
        if set(checksums) != expected - {"SHA256SUMS"}:
            raise UpdateError("invalid_package")
        version_data = _read_small_member(
            archive, by_name["VERSION.txt"], MAX_VERSION_BYTES
        )
        if by_name["THIRD-PARTY-NOTICES.md"].file_size > MAX_NOTICES_BYTES:
            raise UpdateError("invalid_package")
        if not _version_text_valid(version_data, release.version_text, release.tag,
                                   hashlib.sha256(manifest_bytes).hexdigest()):
            raise UpdateError("invalid_package")

        records: list[tuple[zipfile.ZipInfo, ManagedFile]] = []
        for name in sorted(expected - set(METADATA_FILES)):
            info = by_name[name]
            record = package_by_path[name]
            actual_size, actual_hash = _hash_zip_member(archive, info)
            if actual_size != record["size"] or actual_hash != record["sha256"]:
                raise UpdateError("checksum")
            if name in checksums and checksums[name] != actual_hash:
                raise UpdateError("checksum")
            records.append((info, ManagedFile(name, actual_size, actual_hash)))
        metadata_records = {}
        for name in METADATA_FILES:
            if name == "SHA256SUMS":
                continue
            info = by_name[name]
            size, digest = _hash_zip_member(archive, info)
            if checksums.get(name) != digest:
                raise UpdateError("checksum")
            metadata_records[name] = ManagedFile(name, size, digest)
        checksums_info = by_name["SHA256SUMS"]
        sum_size, sum_hash = _hash_zip_member(archive, checksums_info)
        metadata_records["SHA256SUMS"] = ManagedFile("SHA256SUMS", sum_size, sum_hash)
        records.extend((by_name[name], record)
                       for name, record in metadata_records.items())
        return records


def _hash_zip_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with archive.open(info) as stream:
        while True:
            block = stream.read(MAX_DOWNLOAD_CHUNK)
            if not block:
                break
            size += len(block)
            if size > info.file_size:
                raise UpdateError("invalid_package")
            digest.update(block)
    if size != info.file_size:
        raise UpdateError("invalid_package")
    return size, digest.hexdigest()


def _all_install_files(root: Path) -> dict[str, Path]:
    result = {}

    def record(path: Path) -> None:
        key = path.relative_to(root).as_posix().casefold()
        if key in result and result[key] != path:
            raise UpdateError("modified_install")
        result[key] = path

    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in list(dirs):
            path = current_path / name
            record(path)
            if _reparse(path):
                dirs.remove(name)
        for name in files:
            record(current_path / name)
    return result


def _read_installed_inventory(install_dir: Path) -> tuple[tuple[ManagedFile, ...], str]:
    if (install_dir / ".git").exists():
        raise UpdateError("source_checkout")
    manifest_path = install_dir / "runtime-manifest.json"
    metadata_paths = [install_dir / name for name in METADATA_FILES]
    for path in metadata_paths:
        _ensure_no_reparse(path)
    if not manifest_path.is_file() or not (install_dir / "VERSION.txt").is_file():
        raise UpdateError("source_checkout")
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise UpdateError("modified_install")
    manifest_bytes = manifest_path.read_bytes()
    manifest = _strict_json(manifest_bytes)
    if manifest.get("schema_version") != 3 or manifest.get("product") != "NeuralScreen":
        raise UpdateError("install_readonly")
    version = manifest.get("version")
    tag = manifest.get("expected_tag")
    parse_version(version)
    if tag != f"v{version}":
        raise UpdateError("install_readonly")
    # Reuse the same strict shape validation with a tiny version view.
    _validate_manifest(manifest_bytes, tag, version)
    package = manifest["package"]["files"]
    sums_path = install_dir / "SHA256SUMS"
    if sums_path.stat().st_size > MAX_CHECKSUM_BYTES:
        raise UpdateError("modified_install")
    checksums = _parse_checksums(sums_path.read_bytes())
    expected_paths = set(METADATA_FILES) - {"SHA256SUMS"}
    expected_paths.update(record["path"] for record in package)
    if set(checksums) != expected_paths:
        raise UpdateError("modified_install")
    version_path = install_dir / "VERSION.txt"
    if version_path.stat().st_size > MAX_VERSION_BYTES:
        raise UpdateError("modified_install")
    version_bytes = version_path.read_bytes()
    if not _version_text_valid(version_bytes, version, tag, hashlib.sha256(manifest_bytes).hexdigest()):
        raise UpdateError("modified_install")
    inventory = []
    entries = [ManagedFile(record["path"], record["size"], record["sha256"])
               for record in package]
    entries.extend((
        ManagedFile("VERSION.txt", len(version_bytes), hashlib.sha256(version_bytes).hexdigest()),
        ManagedFile("runtime-manifest.json", len(manifest_bytes), hashlib.sha256(manifest_bytes).hexdigest()),
    ))
    for record in entries:
        _ensure_no_reparse(install_dir.joinpath(*record.path.split("/")))
        path = install_dir.joinpath(*record.path.split("/"))
        if _reparse(path) or not path.is_file():
            raise UpdateError("modified_install")
        size, digest = _sha256_file(path)
        expected = checksums.get(record.path)
        if record.path == "SHA256SUMS":
            continue
        if size != record.size or digest != record.sha256 or expected != digest:
            raise UpdateError("modified_install")
        inventory.append(ManagedFile(record.path, size, digest))
    notice_path = install_dir / "THIRD-PARTY-NOTICES.md"
    if notice_path.stat().st_size > MAX_NOTICES_BYTES:
        raise UpdateError("modified_install")
    notice_size, notice_hash = _sha256_file(notice_path)
    if checksums.get("THIRD-PARTY-NOTICES.md") != notice_hash:
        raise UpdateError("modified_install")
    inventory.append(ManagedFile("THIRD-PARTY-NOTICES.md", notice_size, notice_hash))
    sums_path = install_dir / "SHA256SUMS"
    sums_size, sums_hash = _sha256_file(sums_path)
    inventory.append(ManagedFile("SHA256SUMS", sums_size, sums_hash))
    return tuple(sorted(inventory, key=lambda item: item.path)), version


def _paths_overlap(first: Path, second: Path) -> bool:
    first_path = os.path.normcase(os.path.abspath(first))
    second_path = os.path.normcase(os.path.abspath(second))
    try:
        common = os.path.commonpath((first_path, second_path))
    except ValueError:
        return False
    return common in (first_path, second_path)


def _check_install(install_dir: Path, transaction_dir: Path,
                   new_inventory: tuple[ManagedFile, ...],
                   target_version: str,
                   expected_current_version: str | None) -> tuple[tuple[ManagedFile, ...], str | None, str | None]:
    try:
        _ensure_no_reparse(install_dir)
        _ensure_no_reparse(transaction_dir)
        if not install_dir.is_dir() or not transaction_dir.is_dir():
            raise UpdateError("install_readonly")
        if _paths_overlap(install_dir, transaction_dir):
            raise UpdateError("install_readonly")
        if install_dir.stat().st_dev != transaction_dir.stat().st_dev:
            raise UpdateError("install_readonly")
        old_inventory, old_version = _read_installed_inventory(install_dir)
        if parse_version(target_version) <= parse_version(old_version):
            raise UpdateError("invalid_release")
        if (expected_current_version is not None
                and parse_version(expected_current_version) != parse_version(old_version)):
            raise UpdateError("modified_install")
        old_keys = {item.path.casefold() for item in old_inventory}
        writable_dirs = {install_dir}
        for item in new_inventory:
            if _protected(item.path) and item.path != "native/libraries/README.md":
                raise UpdateError("invalid_package")
        existing = _all_install_files(install_dir)
        for item in new_inventory:
            key = item.path.casefold()
            prior = existing.get(key)
            if prior is not None and key not in old_keys:
                raise UpdateError("modified_install")
            path = install_dir.joinpath(*item.path.split("/"))
            parent = path.parent
            while parent != install_dir:
                if parent.exists() and not parent.is_dir():
                    raise UpdateError("modified_install")
                if _reparse(parent):
                    raise UpdateError("modified_install")
                parent = parent.parent
            writable_parent = path.parent
            while not writable_parent.exists():
                writable_parent = writable_parent.parent
            if writable_parent != install_dir and not writable_parent.is_dir():
                raise UpdateError("modified_install")
            writable_dirs.add(writable_parent)
        for item in old_inventory:
            path = install_dir.joinpath(*item.path.split("/"))
            if path.exists() and not (path.stat().st_mode & stat.S_IWRITE):
                raise UpdateError("install_readonly")
            writable_dirs.add(path.parent)
        # Probe each existing directory that the helper may need to change.
        for directory in sorted(writable_dirs, key=lambda path: (len(path.parts), str(path).casefold())):
            fd = None
            probe = None
            try:
                fd, probe = tempfile.mkstemp(
                    prefix=".neuralscreen-update-probe-", dir=directory
                )
                os.close(fd)
                fd = None
                os.unlink(probe)
                probe = None
            finally:
                if fd is not None:
                    os.close(fd)
                if probe is not None:
                    try:
                        os.unlink(probe)
                    except FileNotFoundError:
                        pass
        return old_inventory, old_version, None
    except UpdateError as exc:
        return (), None, exc.code
    except (OSError, ValueError):
        return (), None, "install_readonly"


def stage_release(release, transaction_dir: str | os.PathLike[str], *,
                  install_dir: str | os.PathLike[str] | None = None,
                  current_version: str | None = None,
                  progress=None, response_callback=None) -> StagedRelease:
    """Download, authenticate, validate, and stage a frozen official release.

    ``transaction_dir`` must already be a fresh updater-owned directory.
    ``install_dir`` adds read-only ownership and replacement eligibility checks.
    """
    version_text = getattr(release, "version_text", None)
    tag = getattr(release, "tag", None)
    name = getattr(release, "asset_name", None)
    release_url = getattr(release, "release_url", None)
    asset_id = getattr(release, "asset_id", None)
    download_url = getattr(release, "download_url", None)
    size_value = getattr(release, "size", None)
    digest_value = getattr(release, "sha256", None)
    version_tuple = getattr(release, "version", None)
    if (not isinstance(tag, str) or not isinstance(name, str)
            or not isinstance(release_url, str)
            or isinstance(asset_id, bool) or not isinstance(asset_id, int) or asset_id <= 0
            or not isinstance(version_tuple, tuple) or len(version_tuple) != 3
            or any(isinstance(part, bool) or not isinstance(part, int) or part < 0
                   for part in version_tuple)
            or not isinstance(version_text, str) or not isinstance(download_url, str)
            or isinstance(size_value, bool) or not isinstance(size_value, int)
            or size_value <= 0 or not isinstance(digest_value, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest_value) is None):
        raise UpdateError("invalid_release")
    version = parse_version(version_text)
    if (version_tuple != version or tag != f"v{version_text}"
            or release_url != f"https://github.com/perseval-BLR/NeuralScreen/releases/tag/{tag}"
            or name != f"neuralscreen-{tag}-full.zip"
            or download_url != f"https://github.com/perseval-BLR/NeuralScreen/releases/download/{tag}/{name}"):
        raise UpdateError("invalid_release")

    transaction = Path(os.path.abspath(transaction_dir))
    _ensure_no_reparse(transaction)
    if not transaction.is_dir():
        raise UpdateError("invalid_package")
    if install_dir is not None and _paths_overlap(Path(install_dir), transaction):
        raise UpdateError("install_readonly")
    if _reparse(transaction / "candidate") or (transaction / "candidate").exists():
        raise UpdateError("invalid_package")
    part = transaction / "release.zip.part"
    archive_path = transaction / "release.zip"
    if (_reparse(part) or part.exists() or _reparse(archive_path) or archive_path.exists()):
        raise UpdateError("invalid_package")

    created_files: list[Path] = []
    created_dirs: list[Path] = []
    succeeded = False

    def cleanup() -> None:
        for path in reversed(created_files):
            try:
                if (path.exists() or path.is_symlink()) and not _reparse(path):
                    path.unlink()
            except OSError:
                pass
        for path in reversed(created_dirs):
            try:
                if path.is_dir() and not _reparse(path):
                    path.rmdir()
            except OSError:
                pass

    try:
        if shutil.disk_usage(transaction).free < size_value + MIN_FREE_BYTES:
            raise UpdateError("disk_space")
        with _open_asset(download_url) as response:
            if response_callback is not None:
                response_callback(response)
            digest = hashlib.sha256()
            received = 0
            with part.open("xb") as output:
                created_files.append(part)
                while True:
                    try:
                        block = response.read(MAX_DOWNLOAD_CHUNK)
                    except (OSError, ValueError) as exc:
                        raise UpdateError("network") from exc
                    if not block:
                        break
                    received += len(block)
                    if received > size_value:
                        raise UpdateError("checksum")
                    digest.update(block)
                    output.write(block)
                    if progress is not None:
                        progress(received, size_value)
                output.flush()
                os.fsync(output.fileno())
        if received != size_value or digest.hexdigest() != digest_value:
            raise UpdateError("checksum")
        part.replace(archive_path)
        created_files.remove(part)
        created_files.append(archive_path)
        try:
            records = _validate_archive(archive_path, release)
        except UpdateError:
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise UpdateError("invalid_package") from exc
        expanded = sum(record.size for _, record in records)
        if shutil.disk_usage(transaction).free < expanded + MIN_FREE_BYTES:
            raise UpdateError("disk_space")

        candidate = transaction / "candidate"
        candidate.mkdir()
        created_dirs.append(candidate)
        _ensure_no_reparse(candidate)
        new_inventory = []
        with zipfile.ZipFile(archive_path) as archive:
            for info, record in records:
                destination = candidate.joinpath(*record.path.split("/"))
                parent = destination.parent
                missing_dirs = []
                cursor = parent
                while not cursor.exists():
                    missing_dirs.append(cursor)
                    cursor = cursor.parent
                parent.mkdir(parents=True, exist_ok=True)
                created_dirs.extend(reversed(missing_dirs))
                _ensure_no_reparse(parent)
                with archive.open(info) as source, destination.open("xb") as output:
                    created_files.append(destination)
                    shutil.copyfileobj(source, output, MAX_DOWNLOAD_CHUNK)
                    output.flush()
                    os.fsync(output.fileno())
                size, digest = _sha256_file(destination)
                if size != record.size or digest != record.sha256:
                    raise UpdateError("checksum")
                new_inventory.append(record)
        installable = False
        refusal = "install_readonly"
        old_inventory: tuple[ManagedFile, ...] = ()
        if install_dir is not None:
            old_inventory, installed_version, refusal = _check_install(
                Path(os.path.abspath(install_dir)), transaction, tuple(new_inventory),
                version_text, current_version,
            )
            installable = refusal is None
        else:
            installed_version = None
        staged = StagedRelease(
            transaction_dir=transaction,
            candidate_dir=candidate,
            archive_path=archive_path,
            version=version_text,
            tag=tag,
            new_inventory=tuple(sorted(new_inventory, key=lambda item: item.path)),
            old_inventory=old_inventory,
            installed_version=installed_version,
            can_install=installable,
            refusal_code=refusal,
        )
        succeeded = True
        return staged
    except UpdateError:
        raise
    except OSError as exc:
        raise UpdateError("disk_space") from exc
    finally:
        if not succeeded:
            cleanup()
