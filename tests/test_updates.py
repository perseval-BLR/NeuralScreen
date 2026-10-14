"""Offline behavior tests for the portable update flow.

Run from the repository root with ``python tests/test_updates.py``.
"""
from __future__ import annotations

import json
import hashlib
import ast
import os
import shutil
import stat
import sys
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from io import BytesIO
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from unittest import mock


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from updates import ReleaseInfo, UpdateController, UpdateError, _RedirectPolicy, check_latest, install_id_for  # noqa: E402
from update_package import _AssetRedirect, stage_release  # noqa: E402


REPOSITORY = "perseval-BLR/NeuralScreen"


class Response:
    def __init__(self, body: bytes):
        self.body = body

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            body, self.body = self.body, b""
            return body
        body, self.body = self.body[:size], self.body[size:]
        return body

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Opener:
    def __init__(self, response: Response):
        self.response = response
        self.calls: list[tuple[object, int | None]] = []

    def open(self, request, timeout=None):
        self.calls.append((request, timeout))
        return self.response


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def package_zip_bytes(members: dict[str, bytes], *, duplicate: tuple[str, bytes] | None = None,
                      symlink_name: str | None = None) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (0o120777 if name == symlink_name else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
        if duplicate is not None:
            name, data = duplicate
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return buffer.getvalue()


def git_blob(data: bytes) -> str:
    header = b"blob " + str(len(data)).encode("ascii") + b"\0"
    return hashlib.sha1(header + data).hexdigest()


def record_tree(records: list[dict]) -> str:
    digest = hashlib.sha256()
    for record in sorted(records, key=lambda item: item["path"]):
        digest.update(record["path"].encode("utf-8"))
        digest.update(b"\0" + record["sha256"].encode("ascii"))
        digest.update(b"\0" + str(record["size"]).encode("ascii") + b"\n")
    return digest.hexdigest()


class BundleFixture:
    """A tiny official-format schema-3 ZIP, assembled independently of the updater."""

    def __init__(self, root: Path, version: str, extra_payload: dict[str, bytes] | None = None):
        self.root = root
        self.version = version
        self.tag = f"v{version}"
        self.name = f"neuralscreen-{self.tag}-full.zip"
        self.release_url = f"https://github.com/{REPOSITORY}/releases/tag/{self.tag}"
        self.download_url = (
            f"https://github.com/{REPOSITORY}/releases/download/{self.tag}/{self.name}"
        )
        source_bytes = {
            "app/main.py": b"# entry\n",
            "app/settings_io.py": f'APP_VERSION = "{version}"\n'.encode(),
            "native/libraries/README.md": b"# runtime libraries\n",
            "build_release_zip.py": f'VERSION = "{version}"\n'.encode(),
            "native/launcher.rc": f"PRODUCTVERSION {version}\n".encode(),
        }
        self.payload = {
            "app/main.py": source_bytes["app/main.py"],
            "app/settings_io.py": source_bytes["app/settings_io.py"],
            "native/libraries/README.md": source_bytes["native/libraries/README.md"],
            "NeuralScreen.exe": b"fixture launcher bytes",
            "runtime/python.exe": b"fixture python.exe",
            "runtime/pythonw.exe": b"fixture pythonw.exe",
            "runtime/python313.dll": b"fixture python313.dll",
            "runtime/python313.zip": b"fixture standard library",
            "runtime/python313._pth": b"python313.zip\n.\n",
            "runtime/_ctypes.pyd": b"fixture ctypes extension",
        }
        self.payload.update(extra_payload or {})
        source_records = [
            {"path": path, "size": len(data), "sha256": sha(data), "git_blob": git_blob(data)}
            for path, data in source_bytes.items()
        ]
        source_by_path = {item["path"]: item for item in source_records}
        package_records = []
        for path, data in self.payload.items():
            record = {"path": path, "size": len(data), "sha256": sha(data)}
            if path in source_by_path:
                record.update(origin="git", git_blob=source_by_path[path]["git_blob"])
            else:
                record["origin"] = "generated"
            package_records.append(record)
        package_records.sort(key=lambda item: item["path"])
        runtime_records = [item for item in package_records if item["path"].startswith("runtime/")]
        artifact = next(item for item in package_records if item["path"] == "NeuralScreen.exe")
        parts = version.split(".")
        rc_version = ".".join((*parts, "0"))
        self.manifest = {
            "expected_tag": self.tag,
            "package": {
                "file_count": len(package_records),
                "files": package_records,
                "tree_sha256": record_tree(package_records),
            },
            "product": "NeuralScreen",
            "runtime": {
                "artifacts": [artifact],
                "file_count": len(runtime_records),
                "files": runtime_records,
                "tree_sha256": record_tree(runtime_records),
            },
            "schema_version": 3,
            "source_inventory": sorted(source_records, key=lambda item: item["path"]),
            "version": version,
            "version_sources": [
                dict(source_by_path["build_release_zip.py"], values={"VERSION": version}),
                dict(source_by_path["app/settings_io.py"], values={"APP_VERSION": version}),
                dict(source_by_path["native/launcher.rc"], values={
                    "FILEVERSION": rc_version, "PRODUCTVERSION": rc_version,
                    "FileVersion": rc_version, "ProductVersion": version,
                }),
            ],
        }
        self.manifest_bytes = (
            json.dumps(self.manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        notice = b"fixture third-party notices\n"
        version_text = (
            f"NeuralScreen {version}\n"
            f"commit: {'1' * 40}\n"
            f"tag: {self.tag}\n"
            "runtime manifest: runtime-manifest.json sha256 "
            f"{sha(self.manifest_bytes)}\n"
        ).encode("utf-8")
        self.members = {
            "VERSION.txt": version_text,
            "runtime-manifest.json": self.manifest_bytes,
            "THIRD-PARTY-NOTICES.md": notice,
            **self.payload,
        }
        sums = "".join(
            f"{sha(data)} *{name}\n"
            for name, data in sorted(self.members.items())
        ).encode("utf-8")
        self.members["SHA256SUMS"] = sums
        self.path = root / self.name
        with zipfile.ZipFile(self.path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            order = ["VERSION.txt", "runtime-manifest.json", "THIRD-PARTY-NOTICES.md", "SHA256SUMS"]
            order.extend(sorted(set(self.members) - set(order)))
            for name in order:
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, self.members[name])
        archive_bytes = self.path.read_bytes()
        self.release = ReleaseInfo(
            version=tuple(int(part) for part in parts),
            tag=self.tag,
            release_url=self.release_url,
            asset_name=self.name,
            asset_id=99,
            download_url=self.download_url,
            size=len(archive_bytes),
            sha256=sha(archive_bytes),
        )

    def install_to(self, install_dir: Path) -> None:
        install_dir.mkdir(parents=True)
        for name, data in self.members.items():
            path = install_dir.joinpath(*name.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)


class ArchiveOpener(Opener):
    def __init__(self, archive_bytes: bytes):
        super().__init__(Response(archive_bytes))


class GatedResponse(Response):
    def __init__(self, body: bytes):
        super().__init__(body)
        self.first_read = threading.Event()
        self.allow_finish = threading.Event()
        self.read_count = 0

    def read(self, size: int = -1) -> bytes:
        self.read_count += 1
        if self.read_count == 1:
            part, self.body = self.body[:16], self.body[16:]
            self.first_read.set()
            return part
        if self.read_count == 2:
            self.allow_finish.wait(timeout=5)
        return super().read(size)

    def close(self) -> None:
        self.allow_finish.set()


class ReadTimeoutResponse(Response):
    def __init__(self, body: bytes):
        super().__init__(body)
        self.read_count = 0

    def read(self, size: int = -1) -> bytes:
        self.read_count += 1
        if self.read_count == 1:
            return super().read(16)
        raise TimeoutError("simulated stalled socket read")


class ElapsedChunkResponse(Response):
    def __init__(self, body: bytes):
        super().__init__(body)
        self.elapsed_seconds = 0

    def read(self, size: int = -1) -> bytes:
        body, self.body = self.body[:min(size, 512)], self.body[min(size, 512):]
        if body:
            self.elapsed_seconds += 8
        return body


class RoutingOpener:
    def __init__(self, api_body: bytes, zip_body: bytes):
        self.api = Response(api_body)
        self.asset = GatedResponse(zip_body)
        self.calls: list[tuple[str, int | None]] = []

    def open(self, request, timeout=None):
        self.calls.append((request.full_url, timeout))
        return self.api if request.full_url.endswith("/releases/latest") else self.asset


class RetryOpener:
    def __init__(self, api_body: bytes, first_asset: Response, second_asset: Response):
        self.api = Response(api_body)
        self.assets = [first_asset, second_asset]
        self.calls: list[str] = []

    def open(self, request, timeout=None):
        self.calls.append(request.full_url)
        if request.full_url.endswith("/releases/latest"):
            return self.api
        return self.assets.pop(0)


def release_payload(
    tag: str = "v2.10.0", *, draft: bool = False, prerelease: bool = False,
    assets: list[dict] | None = None,
) -> dict:
    name = f"neuralscreen-{tag}-full.zip"
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{tag}",
        "assets": assets if assets is not None else [{
            "name": name,
            "id": 42,
            "state": "uploaded",
            "size": 1234,
            "digest": "sha256:" + "a" * 64,
            "browser_download_url": (
                f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"
            ),
        }],
    }


def api_payload(release: ReleaseInfo) -> dict:
    return {
        "tag_name": release.tag,
        "draft": False,
        "prerelease": False,
        "html_url": release.release_url,
        "assets": [{
            "name": release.asset_name,
            "id": release.asset_id,
            "state": "uploaded",
            "size": release.size,
            "digest": "sha256:" + release.sha256,
            "browser_download_url": release.download_url,
        }],
    }


def wait_for_phase(controller: UpdateController, phase: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = controller.poll()
        if state.phase == phase:
            return state
        if state.phase == "error":
            raise AssertionError(f"controller failed with {state.error_code}")
        threading.Event().wait(0.002)
    raise AssertionError(f"controller did not reach {phase}")


class ReleaseDiscoveryTests(unittest.TestCase):
    def test_install_identity_uses_the_full_normalized_resolved_path_hash(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ns identity ") as temp:
            install_dir = Path(temp) / "Neural Screen Ω"
            install_dir.mkdir()
            install_id = install_id_for(install_dir)
            expected = hashlib.sha256(
                os.path.normcase(str(Path(install_dir).resolve())).encode("utf-8")
            ).hexdigest()
            self.assertEqual(expected, install_id)
            self.assertRegex(install_id, r"^[0-9a-f]{64}$")
            drive, tail = os.path.splitdrive(str(install_dir))
            if drive:
                self.assertEqual(install_id, install_id_for(Path(drive.swapcase() + tail)))

    def check(self, current: str, payload: dict):
        opener = Opener(Response(json.dumps(payload).encode("utf-8")))
        with mock.patch("updates.urllib.request.build_opener", return_value=opener):
            result = check_latest(current)
        self.assertEqual(15, opener.calls[0][1])
        request = opener.calls[0][0]
        self.assertEqual(
            f"https://api.github.com/repos/{REPOSITORY}/releases/latest",
            request.full_url,
        )
        self.assertIn("NeuralScreen", request.get_header("User-agent"))
        return result

    def test_newer_release_is_frozen_from_the_official_asset(self) -> None:
        release = self.check("2.9.0", release_payload())
        self.assertIsInstance(release, ReleaseInfo)
        self.assertEqual((2, 10, 0), release.version)
        self.assertEqual("v2.10.0", release.tag)
        self.assertEqual(
            f"https://github.com/{REPOSITORY}/releases/tag/v2.10.0",
            release.release_url,
        )
        self.assertEqual("neuralscreen-v2.10.0-full.zip", release.asset_name)
        self.assertEqual(42, release.asset_id)
        self.assertEqual(1234, release.size)
        self.assertEqual("a" * 64, release.sha256)
        self.assertEqual(
            f"https://github.com/{REPOSITORY}/releases/download/"
            "v2.10.0/neuralscreen-v2.10.0-full.zip",
            release.download_url,
        )

    def test_same_and_older_versions_are_current_by_integer_order(self) -> None:
        self.assertIsNone(self.check("2.10.0", release_payload("v2.9.9")))
        self.assertIsNone(self.check("2.10.0", release_payload("v2.10.0")))

    def test_rejects_unstable_malformed_and_nonofficial_releases(self) -> None:
        cases = {
            "draft": release_payload(draft=True),
            "prerelease": release_payload(prerelease=True),
            "leading zero": release_payload("v02.10.0"),
            "missing component": release_payload("v2.10"),
        }
        wrong_page = release_payload()
        wrong_page["html_url"] = "https://github.com/elsewhere/NeuralScreen/releases/tag/v2.10.0"
        cases["wrong repository"] = wrong_page
        for name, payload in cases.items():
            with self.subTest(name=name), self.assertRaises(UpdateError):
                self.check("2.9.0", payload)

    def test_requires_one_uploaded_zip_with_exact_digest_and_url(self) -> None:
        good = release_payload()["assets"][0]
        cases = {
            "missing zip": [],
            "duplicate zip": [good, dict(good, id=43)],
            "not uploaded": [dict(good, state="new")],
            "missing digest": [dict(good, digest=None)],
            "short digest": [dict(good, digest="sha256:1234")],
            "wrong name": [dict(good, name="neuralscreen-v2.10.0.zip")],
            "wrong size": [dict(good, size=True)],
            "cross repository": [dict(good, browser_download_url="https://example.com/file.zip")],
        }
        for name, assets in cases.items():
            with self.subTest(name=name), self.assertRaises(UpdateError):
                self.check("2.9.0", release_payload(assets=assets))

    def test_classifies_http_failures_timeout_and_invalid_json(self) -> None:
        for status, expected in ((404, "invalid_release"), (403, "rate_limit"),
                                 (429, "rate_limit"), (500, "network")):
            opener = Opener(Response(b""))
            opener.open = mock.Mock(side_effect=urllib.error.HTTPError(
                "https://api.github.com/releases/latest", status, "error", {}, None
            ))
            with self.subTest(status=status), \
                    mock.patch("updates.urllib.request.build_opener", return_value=opener), \
                    self.assertRaises(UpdateError) as caught:
                check_latest("2.9.0")
            self.assertEqual(expected, caught.exception.code)

        for exception, expected in ((TimeoutError("socket timeout"), "network"),
                                    (ValueError("invalid JSON"), "network")):
            opener = Opener(Response(b""))
            opener.open = mock.Mock(side_effect=exception)
            with mock.patch("updates.urllib.request.build_opener", return_value=opener), \
                    self.assertRaises(UpdateError) as caught:
                check_latest("2.9.0")
            self.assertEqual(expected, caught.exception.code)

        opener = Opener(Response(b"{"))
        with mock.patch("updates.urllib.request.build_opener", return_value=opener), \
                self.assertRaises(UpdateError) as caught:
            check_latest("2.9.0")
        self.assertEqual("invalid_release", caught.exception.code)

        response = Response(b"")
        response.read = mock.Mock(side_effect=TimeoutError("socket read timeout"))
        opener = Opener(response)
        with mock.patch("updates.urllib.request.build_opener", return_value=opener), \
                self.assertRaises(UpdateError) as caught:
            check_latest("2.9.0")
        self.assertEqual("network", caught.exception.code)

    def test_release_json_is_bounded_and_redirects_stay_on_github_hosts(self) -> None:
        opener = Opener(Response(b"x" * (1024 * 1024 + 1)))
        with mock.patch("updates.urllib.request.build_opener", return_value=opener), \
                self.assertRaises(UpdateError) as caught:
            check_latest("2.9.0")
        self.assertEqual("invalid_release", caught.exception.code)

        policy = _RedirectPolicy({"github.com", "release-assets.githubusercontent.com"})
        request = urllib.request.Request("https://github.com/release.zip")
        allowed = policy.redirect_request(
            request, None, 302, "Found", {},
            "https://release-assets.githubusercontent.com/release.zip",
        )
        self.assertIsNotNone(allowed)
        for target in (
            "http://github.com/release.zip",
            "https://example.com/release.zip",
            "https://user@github.com/release.zip",
            "https://github.com:444/release.zip",
        ):
            with self.subTest(target=target), self.assertRaises(UpdateError):
                policy.redirect_request(request, None, 302, "Found", {}, target)

        asset_policy = _AssetRedirect()
        asset_request = urllib.request.Request(
            f"https://github.com/{REPOSITORY}/releases/download/v2.10.0/file.zip"
        )
        for host in ("release-assets.githubusercontent.com", "objects.githubusercontent.com"):
            self.assertIsNotNone(asset_policy.redirect_request(
                asset_request, None, 302, "Found", {}, f"https://{host}/file.zip"
            ))
        for target in (
            "http://release-assets.githubusercontent.com/file.zip",
            "https://example.com/file.zip",
            "https://user@release-assets.githubusercontent.com/file.zip",
            "https://objects.githubusercontent.com:444/file.zip",
        ):
            with self.subTest(asset_target=target), self.assertRaises(UpdateError):
                asset_policy.redirect_request(asset_request, None, 302, "Found", {}, target)


class PackageStagingTests(unittest.TestCase):
    def test_package_bootstrap_module_imports_only_standard_library_modules(self) -> None:
        package_path = ROOT / "app" / "update_package.py"
        tree = ast.parse(package_path.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(0, node.level, "bootstrap module must not use relative imports")
                if node.module:
                    imports.append(node.module.split(".", 1)[0])
        self.assertTrue(imports)
        self.assertTrue(set(imports) <= sys.stdlib_module_names, sorted(set(imports)))

    def test_official_schema_three_package_stages_and_preserves_user_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ns-update-stage-") as temp:
            root = Path(temp)
            old_root = root / "old"
            old_root.mkdir()
            old = BundleFixture(old_root, "2.4.0")
            install_dir = root / "install"
            old.install_to(install_dir)
            user_files = {
                "config.json": b'{"preset":"personal"}\n',
                "compatibility-cache.json": b'{"cache":"personal"}\n',
                "recordings/session.mkv": b"recording bytes",
                "native/libraries/custom.dll": b"user library",
                "unmanaged/keep.txt": b"keep me",
            }
            for name, data in user_files.items():
                path = install_dir.joinpath(*name.split("/"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            before = {
                name: sha(install_dir.joinpath(*name.split("/")).read_bytes())
                for name in user_files
            }

            new_root = root / "new"
            new_root.mkdir()
            new = BundleFixture(new_root, "2.4.1")
            transaction = root / "transaction"
            transaction.mkdir()
            opener = ArchiveOpener(new.path.read_bytes())
            with mock.patch("update_package.urllib.request.build_opener", return_value=opener):
                staged = stage_release(new.release, transaction, install_dir=install_dir)

            self.assertTrue(staged.can_install, staged.refusal_code)
            self.assertIsNone(staged.refusal_code)
            self.assertEqual("2.4.1", staged.version)
            self.assertEqual("v2.4.1", staged.tag)
            self.assertTrue(staged.candidate_dir.is_dir())
            self.assertEqual(new.release.size, staged.archive_path.stat().st_size)
            self.assertEqual(sha(new.path.read_bytes()), sha(staged.archive_path.read_bytes()))
            self.assertEqual(
                set(new.members), {item.path for item in staged.new_inventory}
            )
            self.assertTrue({item.path for item in staged.old_inventory} >= {
                "VERSION.txt", "runtime-manifest.json", "SHA256SUMS",
                "THIRD-PARTY-NOTICES.md",
            })
            self.assertEqual(set(staged.managed_union), {
                item.path for item in staged.old_inventory + staged.new_inventory
            })
            for name, expected in before.items():
                self.assertEqual(expected, sha(install_dir.joinpath(*name.split("/")).read_bytes()))
            for name, data in new.payload.items():
                self.assertEqual(data, staged.candidate_dir.joinpath(*name.split("/")).read_bytes())

    def test_refuses_package_file_that_collides_with_an_unmanaged_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ns-update-dir-collision-") as temp:
            root = Path(temp)
            old_root = root / "old"
            old_root.mkdir()
            old = BundleFixture(old_root, "2.4.0")
            install_dir = root / "install"
            old.install_to(install_dir)
            collision = install_dir / "personal-data"
            collision.mkdir()
            (collision / "keep.txt").write_bytes(b"personal data")
            user_file = install_dir / "unmanaged.txt"
            user_file.write_bytes(b"user file")

            new_root = root / "new"
            new_root.mkdir()
            new = BundleFixture(new_root, "2.4.1", {
                "personal-data": b"package file", "unmanaged.txt": b"replacement",
            })
            transaction = root / "transaction"
            transaction.mkdir()
            opener = ArchiveOpener(new.path.read_bytes())
            with mock.patch("update_package.urllib.request.build_opener", return_value=opener):
                staged = stage_release(new.release, transaction, install_dir=install_dir)

            self.assertFalse(staged.can_install)
            self.assertEqual("modified_install", staged.refusal_code)
            self.assertEqual(b"personal data", (collision / "keep.txt").read_bytes())
            self.assertEqual(b"user file", user_file.read_bytes())

    def test_source_checkout_can_stage_but_is_never_install_eligible(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ns-update-source-") as temp:
            root = Path(temp)
            install_dir = root / "checkout"
            install_dir.mkdir()
            (install_dir / ".git").mkdir()
            new_root = root / "new"
            new_root.mkdir()
            new = BundleFixture(new_root, "2.4.1")
            transaction = root / "transaction"
            transaction.mkdir()
            opener = RoutingOpener(json.dumps(api_payload(new.release)).encode(),
                                   new.path.read_bytes())
            controller = UpdateController("2.4.0", install_dir)
            try:
                with mock.patch("updates.urllib.request.build_opener", return_value=opener), \
                        mock.patch("update_package.urllib.request.build_opener", return_value=opener):
                    self.assertTrue(controller.check())
                    self.assertEqual("available", wait_for_phase(controller, "available").phase)
                    self.assertTrue(controller.download(transaction))
                    self.assertTrue(opener.asset.first_read.wait(timeout=3))
                    opener.asset.allow_finish.set()
                    ready = wait_for_phase(controller, "ready")
                self.assertFalse(ready.can_install)
                self.assertEqual("source_checkout", ready.error_code)
                self.assertTrue(controller.staged_release.candidate_dir.is_dir())
                self.assertEqual(2, len(opener.calls))
            finally:
                controller.close()

    def test_modified_managed_install_is_refused_without_touching_modified_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ns-update-modified-") as temp:
            root = Path(temp)
            old_root = root / "old"
            old_root.mkdir()
            old = BundleFixture(old_root, "2.4.0")
            install_dir = root / "install"
            old.install_to(install_dir)
            managed = install_dir / "app" / "main.py"
            managed.write_bytes(b"local edit")
            new_root = root / "new"
            new_root.mkdir()
            new = BundleFixture(new_root, "2.4.1")
            transaction = root / "transaction"
            transaction.mkdir()
            opener = ArchiveOpener(new.path.read_bytes())
            with mock.patch("update_package.urllib.request.build_opener", return_value=opener):
                staged = stage_release(new.release, transaction, install_dir=install_dir)

            self.assertFalse(staged.can_install)
            self.assertEqual("modified_install", staged.refusal_code)
            self.assertEqual(b"local edit", managed.read_bytes())

    def test_rejects_windows_alias_traversal_and_protected_archive_paths(self) -> None:
        invalid_paths = ("../escape", "C:/outside", "CON.txt", "COM¹.txt",
                         "folder/bad?.dll", "trailing./name", "config.json",
                         "logs/trace.txt", "native/libraries/custom.dll")
        with tempfile.TemporaryDirectory(prefix="ns-update-paths-") as temp:
            root = Path(temp)
            for index, name in enumerate(invalid_paths):
                with self.subTest(name=name):
                    source = root / f"source-{index}"
                    source.mkdir()
                    bundle = BundleFixture(source, "2.4.1", {name: b"untrusted"})
                    transaction = root / f"transaction-{index}"
                    transaction.mkdir()
                    opener = ArchiveOpener(bundle.path.read_bytes())
                    with mock.patch("update_package.urllib.request.build_opener", return_value=opener), \
                            self.assertRaises(UpdateError) as caught:
                        stage_release(bundle.release, transaction)
                    self.assertEqual("invalid_package", caught.exception.code)
                    self.assertEqual([], list(transaction.iterdir()))

    def test_candidate_reparse_destination_is_refused_before_download(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ns-update-reparse-") as temp:
            root = Path(temp)
            new_root = root / "new"
            new_root.mkdir()
            bundle = BundleFixture(new_root, "2.4.1")
            transaction = root / "transaction"
            transaction.mkdir()
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "keep.txt"
            sentinel.write_bytes(b"do not follow reparse destination")
            try:
                (transaction / "candidate").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlink unavailable in this fixture: {exc}")
            opener = ArchiveOpener(bundle.path.read_bytes())
            with mock.patch("update_package.urllib.request.build_opener", return_value=opener), \
                    self.assertRaises(UpdateError) as caught:
                stage_release(bundle.release, transaction)
            self.assertEqual("invalid_package", caught.exception.code)
            self.assertEqual([], opener.calls)
            self.assertEqual(b"do not follow reparse destination", sentinel.read_bytes())

    @unittest.skipUnless(os.name == "nt", "Windows directory junction behavior")
    def test_real_directory_junction_transaction_is_refused_before_download(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if powershell is None:
            self.skipTest("PowerShell is unavailable for junction fixture creation")
        with tempfile.TemporaryDirectory(prefix="ns-update-junction-") as temp:
            root = Path(temp)
            target = root / "target"
            target.mkdir()
            sentinel = target / "keep.txt"
            sentinel.write_bytes(b"junction target remains untouched")
            junction = root / "transaction"
            env = os.environ.copy()
            env["NS_UPDATE_JUNCTION_PATH"] = str(junction)
            env["NS_UPDATE_JUNCTION_TARGET"] = str(target)
            created = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-Command",
                 "New-Item -ItemType Junction -Path $env:NS_UPDATE_JUNCTION_PATH "
                 "-Target $env:NS_UPDATE_JUNCTION_TARGET | Out-Null"],
                env=env, capture_output=True, text=True, timeout=15,
            )
            if created.returncode:
                self.skipTest(f"PowerShell could not create junction: {created.stderr.strip()}")
            try:
                self.assertTrue(junction.lstat().st_file_attributes & 0x400)
                source = root / "source"
                source.mkdir()
                bundle = BundleFixture(source, "2.4.1")
                opener = ArchiveOpener(bundle.path.read_bytes())
                with mock.patch("update_package.urllib.request.build_opener", return_value=opener), \
                        self.assertRaises(UpdateError) as caught:
                    stage_release(bundle.release, junction)
                self.assertEqual("invalid_package", caught.exception.code)
                self.assertEqual([], opener.calls)
                self.assertEqual(b"junction target remains untouched", sentinel.read_bytes())
            finally:
                # Remove only the junction itself. The target stays in this fixture root.
                if junction.exists() and junction.lstat().st_file_attributes & 0x400:
                    junction.rmdir()

    def test_reparse_attribute_refuses_candidate_before_download(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ns-update-reparse-attribute-") as temp:
            root = Path(temp)
            new_root = root / "new"
            new_root.mkdir()
            bundle = BundleFixture(new_root, "2.4.1")
            transaction = root / "transaction"
            transaction.mkdir()
            candidate = transaction / "candidate"
            real_lstat = Path.lstat

            def lstat_with_reparse_attribute(path):
                if path == candidate:
                    return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
                return real_lstat(path)

            opener = ArchiveOpener(bundle.path.read_bytes())
            with mock.patch.object(Path, "lstat", lstat_with_reparse_attribute), \
                    mock.patch("update_package.urllib.request.build_opener", return_value=opener), \
                    self.assertRaises(UpdateError) as caught:
                stage_release(bundle.release, transaction)
            self.assertEqual("invalid_package", caught.exception.code)
            self.assertEqual([], opener.calls)
            self.assertEqual([], list(transaction.iterdir()))

    def test_rejects_symlink_case_alias_hash_version_and_expansion_attacks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ns-update-archive-attacks-") as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            bundle = BundleFixture(source, "2.4.1")
            cases: list[tuple[str, bytes, ReleaseInfo, str]] = []

            symlink = package_zip_bytes(bundle.members, symlink_name="runtime/python.exe")
            cases.append(("symlink", symlink,
                          replace(bundle.release, size=len(symlink), sha256=sha(symlink)),
                          "invalid_package"))

            duplicate = package_zip_bytes(
                bundle.members, duplicate=("APP/MAIN.PY", bundle.members["app/main.py"])
            )
            cases.append(("case alias", duplicate,
                          replace(bundle.release, size=len(duplicate), sha256=sha(duplicate)),
                          "invalid_package"))

            altered_members = dict(bundle.members)
            altered_members["app/main.py"] = b"altered code"
            altered = package_zip_bytes(altered_members)
            cases.append(("payload hash", altered,
                          replace(bundle.release, size=len(altered), sha256=sha(altered)),
                          "checksum"))

            mismatch_members = dict(bundle.members)
            mismatch_members["runtime-manifest.json"] = bundle.manifest_bytes.replace(
                b'"version": "2.4.1"', b'"version": "2.4.2"'
            )
            mismatched = package_zip_bytes(mismatch_members)
            cases.append(("manifest version", mismatched,
                          replace(bundle.release, size=len(mismatched), sha256=sha(mismatched)),
                          "invalid_package"))

            oversized = package_zip_bytes(bundle.members)
            cases.append(("expansion cap", oversized,
                          replace(bundle.release, size=len(oversized), sha256=sha(oversized)),
                          "invalid_package"))

            for index, (name, body, release, expected_code) in enumerate(cases):
                with self.subTest(case=name):
                    transaction = root / f"transaction-{index}"
                    transaction.mkdir()
                    opener = ArchiveOpener(body)
                    cap = mock.patch("update_package.MAX_ARCHIVE_BYTES", 32) if name == "expansion cap" else mock.patch(
                        "update_package.MAX_ARCHIVE_BYTES", 2 * 1024 * 1024 * 1024
                    )
                    with cap, mock.patch("update_package.urllib.request.build_opener", return_value=opener), \
                            self.assertRaises(UpdateError) as caught:
                        stage_release(release, transaction)
                    self.assertEqual(expected_code, caught.exception.code)
                    self.assertEqual([], list(transaction.iterdir()))

    def test_truncated_download_and_both_disk_preflights_leave_no_staging_debris(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ns-update-preflight-") as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            bundle = BundleFixture(source, "2.4.1")
            body = bundle.path.read_bytes()

            truncated_transaction = root / "truncated"
            truncated_transaction.mkdir()
            truncated_opener = ArchiveOpener(body[:-17])
            with mock.patch("update_package.urllib.request.build_opener", return_value=truncated_opener), \
                    self.assertRaises(UpdateError) as caught:
                stage_release(bundle.release, truncated_transaction)
            self.assertEqual("checksum", caught.exception.code)
            self.assertEqual([], list(truncated_transaction.iterdir()))

            low_transaction = root / "low-space"
            low_transaction.mkdir()
            low_opener = ArchiveOpener(body)
            with mock.patch("update_package.shutil.disk_usage", return_value=mock.Mock(
                    free=bundle.release.size + (128 * 1024 * 1024) - 1)), \
                    mock.patch("update_package.urllib.request.build_opener", return_value=low_opener), \
                    self.assertRaises(UpdateError) as caught:
                stage_release(bundle.release, low_transaction)
            self.assertEqual("disk_space", caught.exception.code)
            self.assertEqual([], low_opener.calls)
            self.assertEqual([], list(low_transaction.iterdir()))

            expanded_transaction = root / "expanded-low-space"
            expanded_transaction.mkdir()
            expanded_opener = ArchiveOpener(body)
            enough = mock.Mock(free=bundle.release.size + (128 * 1024 * 1024))
            insufficient = mock.Mock(free=0)
            with mock.patch("update_package.shutil.disk_usage", side_effect=(enough, insufficient)), \
                    mock.patch("update_package.urllib.request.build_opener", return_value=expanded_opener), \
                    self.assertRaises(UpdateError) as caught:
                stage_release(bundle.release, expanded_transaction)
            self.assertEqual("disk_space", caught.exception.code)
            self.assertEqual([], list(expanded_transaction.iterdir()))

    def test_unwritable_install_is_refused_after_download_without_touching_payload(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ns-update-readonly-") as temp:
            root = Path(temp)
            old_root = root / "old"
            old_root.mkdir()
            old = BundleFixture(old_root, "2.4.0")
            install_dir = root / "install"
            old.install_to(install_dir)
            preserved = install_dir / "config.json"
            preserved.write_bytes(b"personal settings")
            new_root = root / "new"
            new_root.mkdir()
            new = BundleFixture(new_root, "2.4.1")
            transaction = root / "transaction"
            transaction.mkdir()
            opener = ArchiveOpener(new.path.read_bytes())
            real_mkstemp = tempfile.mkstemp

            def deny_install_probe(*args, **kwargs):
                if kwargs.get("dir") == install_dir:
                    raise PermissionError("simulated read-only install")
                return real_mkstemp(*args, **kwargs)

            with mock.patch("update_package.urllib.request.build_opener", return_value=opener), \
                    mock.patch("update_package.tempfile.mkstemp", side_effect=deny_install_probe):
                staged = stage_release(new.release, transaction, install_dir=install_dir)

            self.assertFalse(staged.can_install)
            self.assertEqual("install_readonly", staged.refusal_code)
            self.assertEqual(b"personal settings", preserved.read_bytes())

    def test_transaction_inside_install_tree_is_not_install_eligible(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ns-update-nested-txn-") as temp:
            root = Path(temp)
            old_root = root / "old"
            old_root.mkdir()
            old = BundleFixture(old_root, "2.4.0")
            install_dir = root / "install"
            old.install_to(install_dir)
            new_root = root / "new"
            new_root.mkdir()
            new = BundleFixture(new_root, "2.4.1")
            transaction = install_dir / ".neuralscreen-update-test" / "txn-test"
            transaction.mkdir(parents=True)
            opener = ArchiveOpener(new.path.read_bytes())
            with mock.patch("update_package.urllib.request.build_opener", return_value=opener), \
                    self.assertRaises(UpdateError) as caught:
                stage_release(new.release, transaction, install_dir=install_dir)

            self.assertEqual("install_readonly", caught.exception.code)
            self.assertEqual([], opener.calls)
            self.assertEqual([], list(transaction.iterdir()))

    def test_invalid_archive_cleans_only_staging_artifacts_created_by_the_call(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ns-update-cleanup-") as temp:
            root = Path(temp)
            transaction = root / "transaction"
            transaction.mkdir()
            unrelated = transaction / "keep.txt"
            unrelated.write_bytes(b"transaction owner data")
            corrupt = b"not a zip archive"
            release = replace(
                ReleaseInfo((2, 4, 1), "v2.4.1",
                            f"https://github.com/{REPOSITORY}/releases/tag/v2.4.1",
                            "neuralscreen-v2.4.1-full.zip", 99,
                            f"https://github.com/{REPOSITORY}/releases/download/v2.4.1/neuralscreen-v2.4.1-full.zip",
                            len(corrupt), sha(corrupt)),
            )
            opener = ArchiveOpener(corrupt)
            with mock.patch("update_package.shutil.disk_usage", return_value=shutil.disk_usage(root)), \
                    mock.patch("update_package.urllib.request.build_opener", return_value=opener), \
                    self.assertRaises(UpdateError) as caught:
                stage_release(release, transaction)

            self.assertEqual("invalid_package", caught.exception.code)
            self.assertEqual(b"transaction owner data", unrelated.read_bytes())
            self.assertEqual(["keep.txt"], sorted(path.name for path in transaction.iterdir()))

    def test_asset_http_errors_are_classified_without_creating_download_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ns-update-asset-errors-") as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            bundle = BundleFixture(source, "2.4.1")
            for index, (status, expected) in enumerate(((403, "rate_limit"),
                                                        (429, "rate_limit"),
                                                        (500, "network"))):
                transaction = root / f"transaction-{index}"
                transaction.mkdir()
                opener = Opener(Response(b""))
                opener.open = mock.Mock(side_effect=urllib.error.HTTPError(
                    bundle.release.download_url, status, "asset error", {}, None
                ))
                with self.subTest(status=status), \
                        mock.patch("update_package.urllib.request.build_opener", return_value=opener), \
                        self.assertRaises(UpdateError) as caught:
                    stage_release(bundle.release, transaction)
                self.assertEqual(expected, caught.exception.code)
                self.assertEqual([], list(transaction.iterdir()))

    def test_controller_serializes_jobs_and_delivers_progress_from_poll(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ns-update-controller-") as temp:
            root = Path(temp)
            old_root = root / "old"
            old_root.mkdir()
            old = BundleFixture(old_root, "2.4.0")
            install_dir = root / "install"
            old.install_to(install_dir)
            new_root = root / "new"
            new_root.mkdir()
            new = BundleFixture(new_root, "2.4.1")
            api_body = json.dumps(api_payload(new.release)).encode("utf-8")
            opener = RoutingOpener(api_body, new.path.read_bytes())
            transaction = root / "transaction"
            transaction.mkdir()
            controller = UpdateController("2.4.0", install_dir)
            try:
                with mock.patch("updates.urllib.request.build_opener", return_value=opener):
                    self.assertTrue(controller.check())
                    self.assertFalse(controller.check())
                    available = wait_for_phase(controller, "available")
                    self.assertEqual("2.4.1", available.latest_version)

                    self.assertTrue(controller.download(transaction))
                    self.assertFalse(controller.download(transaction))
                    self.assertTrue(opener.asset.first_read.wait(timeout=3))
                    progress = controller.poll()
                    self.assertEqual("downloading", progress.phase)
                    self.assertEqual(16, progress.received_bytes)
                    opener.asset.allow_finish.set()
                    ready = wait_for_phase(controller, "ready")
                self.assertTrue(ready.can_install)
                self.assertEqual(2, len(opener.calls))
                self.assertTrue(all(timeout == 15 for _, timeout in opener.calls))
                self.assertIsNotNone(controller.staged_release)
            finally:
                controller.close()

    def test_stalled_download_requires_explicit_retry_and_succeeds(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ns-update-retry-") as temp:
            root = Path(temp)
            old_root = root / "old"
            old_root.mkdir()
            old = BundleFixture(old_root, "2.4.0")
            install_dir = root / "install"
            old.install_to(install_dir)
            new_root = root / "new"
            new_root.mkdir()
            new = BundleFixture(new_root, "2.4.1")
            opener = RetryOpener(
                json.dumps(api_payload(new.release)).encode("utf-8"),
                ReadTimeoutResponse(new.path.read_bytes()),
                Response(new.path.read_bytes()),
            )
            transaction = root / "transaction"
            transaction.mkdir()
            controller = UpdateController("2.4.0", install_dir)
            try:
                with mock.patch("updates.urllib.request.build_opener", return_value=opener), \
                        mock.patch("update_package.urllib.request.build_opener", return_value=opener):
                    self.assertTrue(controller.check())
                    wait_for_phase(controller, "available")
                    self.assertTrue(controller.download(transaction))
                    failed = wait_for_phase(controller, "error")
                    self.assertEqual("network", failed.error_code)
                    self.assertEqual(2, len(opener.calls))
                    self.assertEqual([], list(transaction.iterdir()))

                    self.assertTrue(controller.download(transaction))
                    ready = wait_for_phase(controller, "ready")
                self.assertTrue(ready.can_install)
                self.assertEqual(3, len(opener.calls))
                self.assertIsNotNone(controller.staged_release)
            finally:
                controller.close()

    def test_slow_multichunk_download_can_exceed_timeout_overall(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ns-update-slow-") as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            new = BundleFixture(source, "2.4.1")
            transaction = root / "transaction"
            transaction.mkdir()
            response = ElapsedChunkResponse(new.path.read_bytes())
            opener = Opener(response)
            progress = []
            with mock.patch("update_package.urllib.request.build_opener", return_value=opener):
                staged = stage_release(new.release, transaction,
                                        progress=lambda received, total: progress.append((received, total)))

            self.assertGreater(response.elapsed_seconds, 15)
            self.assertEqual(15, opener.calls[0][1])
            self.assertGreater(len(progress), 1)
            self.assertEqual((new.release.size, new.release.size), progress[-1])
            self.assertTrue(staged.candidate_dir.is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
