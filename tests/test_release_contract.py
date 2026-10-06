r"""Offline tests for the fail-closed release contract.

Run: runtime\python.exe tests\test_release_contract.py
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))  # the modules live in app/

import build_release_zip as builder
import verify_github as verifier


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    )
    return result.stdout.strip()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rewrite_release_archive(
    archive: Path,
    dist: Path,
    mutate,
) -> None:
    """Rewrite a release coherently so only the manifest contract can reject it."""
    with zipfile.ZipFile(archive) as source:
        members = {name: source.read(name) for name in source.namelist()}
    mutate(members)
    members[builder.CHECKSUMS] = builder._checksum_text(
        (name, hashlib.sha256(data).hexdigest())
        for name, data in members.items()
        if name != builder.CHECKSUMS
    )
    metadata = [
        "VERSION.txt",
        builder.RUNTIME_MANIFEST,
        builder.THIRD_PARTY_NOTICES,
        builder.CHECKSUMS,
    ]
    ordered = metadata + sorted(name for name in members if name not in metadata)
    replacement = archive.with_suffix(".rewritten")
    with zipfile.ZipFile(
        replacement, "w", zipfile.ZIP_DEFLATED, compresslevel=6
    ) as target:
        for name in ordered:
            builder._write_zip_member(target, name, members[name])
    replacement.replace(archive)

    outer = verifier.parse_sha256sums((dist / builder.CHECKSUMS).read_bytes())
    outer[archive.name] = sha(archive)
    (dist / builder.CHECKSUMS).write_bytes(
        builder._checksum_text(outer.items())
    )


class ReleaseFixture:
    version = "9.9.0"
    tag = "v9.9.0"
    runtime_artifacts = ("runtime/runtime.bin",)
    launcher_lf = b"@echo off\nrem fixture launcher\nexit /b 0\n"
    exe: bytes | None = None   # an untracked NeuralScreen.exe, when set
    mandatory = (
        "app.py",
        "native/libraries/README.md",
        builder.RUNTIME_MANIFEST,
        builder.THIRD_PARTY_NOTICES,
    )

    def __init__(self, root: Path):
        self.root = root
        self.dist = root / "dist"
        (root / "native" / "libraries").mkdir(parents=True)
        (root / "runtime").mkdir()
        (root / "app").mkdir()   # the modules' folder, as in the real tree
        (root / ".gitignore").write_text(
            "runtime/\ndist*/\nignored.local\n", encoding="utf-8"
        )
        (root / "app.py").write_text("print('fixture')\n", encoding="utf-8")
        # As in the real tree: the launcher is attributed CRLF, stored LF.
        (root / ".gitattributes").write_bytes(b"*.bat text eol=crlf\n")
        (root / "launch.bat").write_bytes(self.launcher_lf)
        (root / "build_release_zip.py").write_text(
            f'VERSION = "{self.version}"\n', encoding="utf-8"
        )
        (root / "pacing.py").write_text("TARGET_FPS = 60\n", encoding="utf-8")
        (root / "app" / "settings_io.py").write_text(
            f'APP_VERSION = "{self.version}"\n', encoding="utf-8"
        )
        self.write_launcher_version(self.version)
        (root / "native" / "all.cpp").write_text("// cpp\n", encoding="utf-8")
        (root / "native" / "all.h").write_text("// h\n", encoding="utf-8")
        (root / "native" / "all.inl").write_text("// inl\n", encoding="utf-8")
        (root / "native" / "all.hlsl").write_text("// shader\n", encoding="utf-8")
        (root / "native" / "neuralscreen.ico").write_bytes(b"icon")
        (root / "native" / "libraries" / "README.md").write_text(
            "# Runtime libraries\n", encoding="utf-8"
        )
        (root / "runtime" / "runtime.bin").write_bytes(b"runtime-v1")
        (root / "runtime" / "nested.bin").write_bytes(b"runtime-v2")
        (root / builder.THIRD_PARTY_NOTICES).write_text(
            "# notices\n", encoding="utf-8"
        )
        if self.exe is not None:
            (root / "NeuralScreen.exe").write_bytes(self.exe)
            (root / ".gitignore").write_text(
                "runtime/\ndist*/\nignored.local\nNeuralScreen.exe\n",
                encoding="utf-8",
            )
        run_git(root, "init", "-q")
        run_git(root, "config", "user.email", "release-test@example.invalid")
        run_git(root, "config", "user.name", "Release Test")
        run_git(root, "add", ".")
        run_git(root, "commit", "-qm", "fixture sources")
        builder.write_runtime_manifest(
            root,
            version=self.version,
            expected_tag=self.tag,
            mandatory_files=self.mandatory,
            required_runtime_artifacts=self.runtime_artifacts,
        )
        run_git(root, "add", builder.RUNTIME_MANIFEST)
        run_git(root, "commit", "-qm", "pin release manifest")
        run_git(root, "tag", self.tag)

    def write_launcher_version(self, version: str) -> None:
        numeric = version.replace(".", ",") + ",0"
        (self.root / "native" / "launcher.rc").write_text(
            "VS_VERSION_INFO VERSIONINFO\n"
            f" FILEVERSION {numeric}\n"
            f" PRODUCTVERSION {numeric}\n"
            "BEGIN\n"
            f' VALUE "FileVersion", "{version}.0"\n'
            f' VALUE "ProductVersion", "{version}"\n'
            "END\n",
            encoding="utf-8",
        )

    @property
    def commit(self) -> str:
        return run_git(self.root, "rev-parse", "HEAD")

    def repin(self) -> None:
        """Re-pin the manifest after a runtime change and move the tag."""
        builder.write_runtime_manifest(
            self.root,
            version=self.version,
            expected_tag=self.tag,
            mandatory_files=self.mandatory,
            required_runtime_artifacts=self.runtime_artifacts,
        )
        run_git(self.root, "add", builder.RUNTIME_MANIFEST)
        run_git(self.root, "commit", "-qm", "repin release manifest")
        run_git(self.root, "tag", "-f", self.tag)

    def build(self) -> Path:
        return builder.build_release(
            self.root,
            version=self.version,
            expected_tag=self.tag,
            output_dir=self.dist,
            mandatory_files=self.mandatory,
            required_runtime_artifacts=self.runtime_artifacts,
            required_architectures=(),
        )


class ReleaseContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ns-release-contract-")
        self.repo = Path(self.temp.name)
        self.fixture = ReleaseFixture(self.repo)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_build_creates_strict_release_set_and_complete_inventory(self) -> None:
        # An ignored local file is intentionally allowed by the tracked-tree gate.
        (self.repo / "ignored.local").write_text("developer state", encoding="utf-8")
        archive = self.fixture.build()
        self.assertTrue(archive.is_file())
        self.assertTrue((self.fixture.dist / builder.CHECKSUMS).is_file())
        self.assertTrue((self.fixture.dist / builder.RUNTIME_MANIFEST).is_file())
        self.assertTrue((self.fixture.dist / builder.THIRD_PARTY_NOTICES).is_file())

        manifest = json.loads(
            (self.repo / builder.RUNTIME_MANIFEST).read_text(encoding="utf-8")
        )
        inventory = {item["path"] for item in manifest["source_inventory"]}
        for name in (
            "native/all.cpp", "native/all.h", "native/all.inl",
            "native/all.hlsl", "native/neuralscreen.ico", "pacing.py",
            "native/libraries/README.md", builder.THIRD_PARTY_NOTICES,
            "build_release_zip.py",
        ):
            self.assertIn(name, inventory)
        records = {
            item["path"]: item for item in manifest["source_inventory"]
        }
        self.assertRegex(records["pacing.py"]["git_blob"], r"^[0-9a-f]{40,64}$")
        runtime_files = {
            item["path"] for item in manifest["runtime"]["files"]
        }
        self.assertEqual(
            {"runtime/nested.bin", "runtime/runtime.bin"}, runtime_files
        )
        self.assertEqual(
            set(builder.VERSION_SOURCE_PATHS),
            {item["path"] for item in manifest["version_sources"]},
        )
        self.assertEqual(3, manifest["schema_version"])
        package_records = manifest["package"]["files"]
        package_paths = {item["path"] for item in package_records}
        self.assertEqual(len(package_records), manifest["package"]["file_count"])
        self.assertTrue({
            "app.py", "native/libraries/README.md",
            "runtime/nested.bin", "runtime/runtime.bin",
        } <= package_paths)
        self.assertTrue({item["origin"] for item in package_records} <= {
            "git", "generated",
        })
        self.assertFalse({
            "VERSION.txt", builder.RUNTIME_MANIFEST,
            builder.THIRD_PARTY_NOTICES, builder.CHECKSUMS,
        } & package_paths)

        failures = verifier.validate_release_set(
            self.fixture.dist, tag=self.fixture.tag,
            tag_commit=self.fixture.commit, repo=self.repo,
        )
        self.assertEqual([], failures)
        with zipfile.ZipFile(archive) as bundle:
            names = set(bundle.namelist())
            self.assertTrue({
                "VERSION.txt", builder.RUNTIME_MANIFEST,
                builder.THIRD_PARTY_NOTICES, builder.CHECKSUMS,
                "native/libraries/README.md",
            } <= names)

    def test_crlf_attributed_launcher_ships_with_crlf(self) -> None:
        # cmd.exe needs CRLF, .gitattributes says *.bat eol=crlf, and a clone
        # gets CRLF - but the archive is built from the LF blob, and v2.1.9
        # shipped NeuralScreen.bat with bare LF.
        blob = subprocess.run(
            ["git", "show", f"{self.fixture.tag}:launch.bat"], cwd=self.repo,
            capture_output=True, check=True,
        ).stdout
        self.assertEqual(self.fixture.launcher_lf, blob)
        crlf = self.fixture.launcher_lf.replace(b"\n", b"\r\n")
        archive = self.fixture.build()
        with zipfile.ZipFile(archive) as bundle:
            self.assertEqual(crlf, bundle.read("launch.bat"))
            # Files not attributed CRLF keep the blob's LF.
            self.assertNotIn(b"\r", bundle.read("app.py"))
        manifest = json.loads(
            (self.fixture.dist / builder.RUNTIME_MANIFEST).read_bytes()
        )
        package = {
            item["path"]: item for item in manifest["package"]["files"]
        }
        source = {
            item["path"]: item for item in manifest["source_inventory"]
        }
        # The package record pins the shipped bytes, the source record the blob.
        self.assertEqual(
            hashlib.sha256(crlf).hexdigest(), package["launch.bat"]["sha256"]
        )
        self.assertEqual("crlf", package["launch.bat"]["eol"])
        self.assertEqual(
            hashlib.sha256(blob).hexdigest(), source["launch.bat"]["sha256"]
        )
        self.assertNotIn("eol", package["app.py"])
        self.assertEqual([], verifier.validate_release_set(
            self.fixture.dist, tag=self.fixture.tag,
            tag_commit=self.fixture.commit, repo=self.repo,
        ))

    def test_verifier_rejects_a_crlf_launcher_shipped_with_lf(self) -> None:
        archive = self.fixture.build()
        rewrite_release_archive(
            archive,
            self.fixture.dist,
            lambda members: members.__setitem__(
                "launch.bat", self.fixture.launcher_lf
            ),
        )
        failures = verifier.validate_release_set(
            self.fixture.dist, tag=self.fixture.tag,
            tag_commit=self.fixture.commit, repo=self.repo,
        )
        self.assertIn("package file checksum mismatch: launch.bat", failures)
        self.assertIn(
            "packaged CRLF file has stray line endings: launch.bat", failures
        )

    def test_dirty_tracked_tree_fails_but_ignored_files_do_not(self) -> None:
        (self.repo / "ignored.local").write_text("allowed", encoding="utf-8")
        builder.assert_clean_tracked_tree(self.repo)
        (self.repo / "app.py").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(builder.ReleaseContractError, "dirty"):
            self.fixture.build()

    def test_wrong_missing_and_stale_tags_fail(self) -> None:
        with self.assertRaisesRegex(builder.ReleaseContractError, "must be"):
            builder.assert_release_tag(self.repo, "v9.9.1", self.fixture.version)
        run_git(self.repo, "tag", "-d", self.fixture.tag)
        with self.assertRaisesRegex(builder.ReleaseContractError, "does not exist"):
            builder.assert_release_tag(
                self.repo, self.fixture.tag, self.fixture.version
            )
        run_git(self.repo, "tag", self.fixture.tag)
        (self.repo / "next.txt").write_text("next\n", encoding="utf-8")
        run_git(self.repo, "add", "next.txt")
        run_git(self.repo, "commit", "-qm", "move head")
        with self.assertRaisesRegex(builder.ReleaseContractError, "HEAD"):
            builder.assert_release_tag(
                self.repo, self.fixture.tag, self.fixture.version
            )

    def test_missing_mandatory_file_fails(self) -> None:
        with self.assertRaisesRegex(builder.ReleaseContractError, "mandatory"):
            builder.build_release(
                self.repo,
                version=self.fixture.version,
                expected_tag=self.fixture.tag,
                output_dir=self.fixture.dist,
                mandatory_files=(*self.fixture.mandatory, "missing.bin"),
                required_runtime_artifacts=self.fixture.runtime_artifacts,
                required_architectures=(),
            )

    def test_runtime_manifest_mismatch_fails_closed(self) -> None:
        # runtime/ is ignored, so this specifically exercises the manifest gate.
        (self.repo / "runtime" / "runtime.bin").write_bytes(b"runtime-tampered")
        builder.assert_clean_tracked_tree(self.repo)
        with self.assertRaisesRegex(
            builder.ReleaseContractError, "does not match"
        ):
            self.fixture.build()

    def test_runtime_change_during_build_is_rejected(self) -> None:
        original = builder.validate_runtime_manifest

        def validate_then_change(*args, **kwargs):
            result = original(*args, **kwargs)
            (self.repo / "runtime" / "nested.bin").write_bytes(b"changed mid-build")
            return result

        with mock.patch.object(
            builder, "validate_runtime_manifest", side_effect=validate_then_change
        ):
            with self.assertRaisesRegex(
                builder.ReleaseContractError, "changed during build"
            ):
                self.fixture.build()

    def test_new_tracked_native_source_invalidates_manifest(self) -> None:
        (self.repo / "native" / "new.cpp").write_text("// new\n", encoding="utf-8")
        run_git(self.repo, "add", "native/new.cpp")
        run_git(self.repo, "commit", "-qm", "new source")
        run_git(self.repo, "tag", "-f", self.fixture.tag)
        with self.assertRaisesRegex(
            builder.ReleaseContractError, "does not match"
        ):
            self.fixture.build()

    def test_published_zip_tamper_is_detected_without_network(self) -> None:
        archive = self.fixture.build()
        unpacked = self.repo / "unpacked"
        with zipfile.ZipFile(archive) as source:
            source.extractall(unpacked)
        (unpacked / "app.py").write_text("tampered\n", encoding="utf-8")
        tampered = archive.with_suffix(".tampered")
        with zipfile.ZipFile(tampered, "w", zipfile.ZIP_DEFLATED) as target:
            for path in sorted(unpacked.rglob("*")):
                if path.is_file():
                    target.write(path, path.relative_to(unpacked).as_posix())
        shutil.move(tampered, archive)
        # Make the outer checksum truthful: the verifier must still reject the
        # bad member against the independent checksum carried inside the ZIP.
        outer = verifier.parse_sha256sums(
            (self.fixture.dist / builder.CHECKSUMS).read_bytes()
        )
        outer[archive.name] = sha(archive)
        (self.fixture.dist / builder.CHECKSUMS).write_bytes(
            builder._checksum_text(outer.items())
        )
        failures = verifier.validate_release_set(
            self.fixture.dist, tag=self.fixture.tag,
            tag_commit=self.fixture.commit,
        )
        self.assertTrue(
            any("ZIP checksum mismatch: app.py" in item for item in failures),
            failures,
        )

    def test_coherent_extra_zip_member_is_rejected(self) -> None:
        archive = self.fixture.build()
        rewrite_release_archive(
            archive,
            self.fixture.dist,
            lambda members: members.__setitem__("extra.bin", b"coherent extra"),
        )
        failures = verifier.validate_release_set(
            self.fixture.dist,
            tag=self.fixture.tag,
            tag_commit=self.fixture.commit,
            repo=self.repo,
        )
        self.assertIn(
            "ZIP has undeclared payload members: ['extra.bin']", failures
        )
        self.assertFalse(
            any("checksum mismatch" in item.lower() for item in failures), failures
        )

    def test_coherent_missing_zip_member_is_rejected(self) -> None:
        archive = self.fixture.build()
        rewrite_release_archive(
            archive,
            self.fixture.dist,
            lambda members: members.pop("app.py"),
        )
        failures = verifier.validate_release_set(
            self.fixture.dist,
            tag=self.fixture.tag,
            tag_commit=self.fixture.commit,
            repo=self.repo,
        )
        self.assertIn(
            "ZIP is missing declared payload members: ['app.py']", failures
        )
        self.assertFalse(
            any("checksum mismatch" in item.lower() for item in failures), failures
        )

    def test_coherent_runtime_tamper_still_breaks_manifest(self) -> None:
        archive = self.fixture.build()
        unpacked = self.repo / "runtime-tamper"
        with zipfile.ZipFile(archive) as source:
            source.extractall(unpacked)
        runtime_path = unpacked / "runtime" / "runtime.bin"
        runtime_path.write_bytes(b"forged-runtime")
        checksums = verifier.parse_sha256sums(
            (unpacked / builder.CHECKSUMS).read_bytes()
        )
        checksums["runtime/runtime.bin"] = sha(runtime_path)
        (unpacked / builder.CHECKSUMS).write_bytes(
            builder._checksum_text(checksums.items())
        )
        replacement = archive.with_suffix(".forged")
        with zipfile.ZipFile(replacement, "w", zipfile.ZIP_DEFLATED) as target:
            for path in sorted(unpacked.rglob("*")):
                if path.is_file():
                    target.write(path, path.relative_to(unpacked).as_posix())
        shutil.move(replacement, archive)
        outer = verifier.parse_sha256sums(
            (self.fixture.dist / builder.CHECKSUMS).read_bytes()
        )
        outer[archive.name] = sha(archive)
        (self.fixture.dist / builder.CHECKSUMS).write_bytes(
            builder._checksum_text(outer.items())
        )
        failures = verifier.validate_release_set(
            self.fixture.dist, tag=self.fixture.tag,
            tag_commit=self.fixture.commit,
        )
        self.assertIn("runtime manifest tree digest differs from ZIP", failures)
        self.assertTrue(
            any("runtime artifact" in item for item in failures), failures
        )

    def test_build_is_byte_for_byte_deterministic(self) -> None:
        first = self.fixture.build()
        first_zip = first.read_bytes()
        first_sums = (self.fixture.dist / builder.CHECKSUMS).read_bytes()
        # Source mtimes and wall-clock time are deliberately irrelevant.
        for path in (self.repo / "app.py", self.repo / "runtime" / "runtime.bin"):
            path.touch()
            path.chmod(path.stat().st_mode)
        time.sleep(0.02)
        second = self.fixture.build()
        self.assertEqual(first_zip, second.read_bytes())
        self.assertEqual(first_sums, (self.fixture.dist / builder.CHECKSUMS).read_bytes())
        with zipfile.ZipFile(second) as archive:
            self.assertTrue(archive.infolist())
            for info in archive.infolist():
                self.assertEqual(builder.ZIP_TIMESTAMP, info.date_time)
                self.assertEqual(3, info.create_system)
                self.assertEqual(0o100644, info.external_attr >> 16)
            self.assertNotIn("built:", archive.read("VERSION.txt").decode("utf-8"))

    def test_version_sources_must_match_builder_version(self) -> None:
        (self.repo / "app" / "settings_io.py").write_text(
            'APP_VERSION = "9.8.0"\n', encoding="utf-8"
        )
        run_git(self.repo, "add", "app/settings_io.py")
        run_git(self.repo, "commit", "-qm", "introduce app version drift")
        run_git(self.repo, "tag", "-f", self.fixture.tag)
        with self.assertRaisesRegex(builder.ReleaseContractError, "version drift"):
            self.fixture.build()

    def test_tagged_builder_version_is_checked(self) -> None:
        (self.repo / "build_release_zip.py").write_text(
            'VERSION = "9.8.0"\n', encoding="utf-8"
        )
        run_git(self.repo, "add", "build_release_zip.py")
        run_git(self.repo, "commit", "-qm", "introduce builder version drift")
        run_git(self.repo, "tag", "-f", self.fixture.tag)
        with self.assertRaisesRegex(builder.ReleaseContractError, "version drift"):
            self.fixture.build()

    def test_launcher_version_fields_are_all_checked(self) -> None:
        manifest = builder.create_runtime_manifest(
            self.repo,
            version=self.fixture.version,
            expected_tag=self.fixture.tag,
            mandatory_files=self.fixture.mandatory,
            required_runtime_artifacts=self.fixture.runtime_artifacts,
        )
        launcher = next(
            item for item in manifest["version_sources"]
            if item["path"] == "native/launcher.rc"
        )
        launcher["values"]["FileVersion"] = "9.8.0.0"
        with self.assertRaisesRegex(builder.ReleaseContractError, "version drift"):
            builder.assert_version_coherence(
                self.fixture.version, manifest["version_sources"]
            )

    def test_untracked_release_input_is_rejected(self) -> None:
        (self.repo / "untracked.bin").write_bytes(b"not from the tag")
        with self.assertRaisesRegex(builder.ReleaseContractError, "untracked"):
            builder.build_release(
                self.repo,
                version=self.fixture.version,
                expected_tag=self.fixture.tag,
                output_dir=self.fixture.dist,
                mandatory_files=(*self.fixture.mandatory, "untracked.bin"),
                required_runtime_artifacts=self.fixture.runtime_artifacts,
                required_architectures=(),
            )

    def test_manifest_generator_rejects_untracked_mandatory_input(self) -> None:
        run_git(self.repo, "rm", "-q", builder.THIRD_PARTY_NOTICES)
        run_git(self.repo, "commit", "-qm", "remove tracked notices")
        (self.repo / builder.THIRD_PARTY_NOTICES).write_text(
            "# untracked notices\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(builder.ReleaseContractError, "untracked"):
            builder.write_runtime_manifest(
                self.repo,
                version=self.fixture.version,
                expected_tag=self.fixture.tag,
                mandatory_files=self.fixture.mandatory,
                required_runtime_artifacts=self.fixture.runtime_artifacts,
            )

    def test_manifest_is_bound_to_tagged_git_blobs(self) -> None:
        self.fixture.build()
        manifest = json.loads(
            (self.fixture.dist / builder.RUNTIME_MANIFEST).read_text(encoding="utf-8")
        )
        app = next(
            item for item in manifest["source_inventory"]
            if item["path"] == "app.py"
        )
        app["git_blob"] = "0" * len(app["git_blob"])
        failures = verifier.validate_manifest_git_binding(
            self.repo, self.fixture.tag, manifest
        )
        self.assertTrue(
            any("tagged Git blob differs" in item for item in failures), failures
        )

    def test_windows_and_traversal_paths_are_rejected(self) -> None:
        unsafe = (
            "../escape.bin", "/absolute.bin", "C:/absolute.bin",
            "C:drive-relative.bin", "\\\\server\\share\\file.bin",
            "safe.txt:alternate-stream",
        )
        for name in unsafe:
            with self.subTest(builder=name):
                with self.assertRaisesRegex(builder.ReleaseContractError, "unsafe"):
                    builder._safe_relpath(name)
            with self.subTest(checksums=name):
                line = f"{'0' * 64} *{name}\n".encode("utf-8")
                with self.assertRaisesRegex(ValueError, "unsafe path"):
                    verifier.parse_sha256sums(line)
            with self.subTest(zip=name):
                self.assertFalse(verifier._safe_archive_path(name))

    def test_verifier_rejects_noncanonical_version_inventory(self) -> None:
        self.fixture.build()
        manifest = json.loads(
            (self.fixture.dist / builder.RUNTIME_MANIFEST).read_text(encoding="utf-8")
        )
        settings = next(
            item for item in manifest["version_sources"]
            if item["path"] == "app/settings_io.py"
        )
        settings["values"]["APP_VERSION"] = "0.0.0"
        failures = verifier._validate_version_sources(
            manifest, self.fixture.version
        )
        self.assertTrue(any("differs" in item for item in failures), failures)

    def test_verifier_rejects_path_like_version_before_archive_lookup(self) -> None:
        self.fixture.build()
        manifest_path = self.fixture.dist / builder.RUNTIME_MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = "../../outside"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        failures = verifier.validate_release_set(
            self.fixture.dist,
            tag=self.fixture.tag,
            tag_commit=self.fixture.commit,
            repo=self.repo,
        )
        self.assertEqual(
            [f"{builder.RUNTIME_MANIFEST} version is not X.Y.Z"], failures
        )

    def test_parallel_release_body_warning_change_is_preserved(self) -> None:
        self.assertIn("latest NVIDIA driver", verifier.RELEASE_DRIVER_WARNING)
        self.assertIn("unsupported/non-standard", verifier.RELEASE_DRIVER_WARNING)

    def test_verifier_rejects_asset_outside_the_release_set(self) -> None:
        # The release set is exactly what a downloader needs. Documentation
        # images were uploaded as assets once and the "required are present"
        # check did not notice, because it never looked at what else was there.
        required = {
            "neuralscreen-v9.9.9-full.zip", verifier.CHECKSUMS,
            verifier.RUNTIME_MANIFEST, verifier.THIRD_PARTY_NOTICES,
            "README.md", "README.ru.md", "TECHNICAL.md", "TECHNICAL.ru.md",
        }
        assets = {name: {"name": name} for name in required}
        self.assertEqual(
            verifier.asset_set_failures(required, assets, "v9.9.9"), [],
        )
        for extra in ("screenshot-main-dark.png", "screenshot-settings.png"):
            assets[extra] = {"name": extra}
        failures = verifier.asset_set_failures(required, assets, "v9.9.9")
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("beyond the release set", failures[0])
        self.assertIn("screenshot-main-dark.png", failures[0])
        self.assertIn("screenshot-settings.png", failures[0])

    def test_verifier_still_reports_a_missing_asset(self) -> None:
        required = {
            "neuralscreen-v9.9.9-full.zip", verifier.CHECKSUMS,
            verifier.RUNTIME_MANIFEST, verifier.THIRD_PARTY_NOTICES,
            "README.md", "README.ru.md", "TECHNICAL.md", "TECHNICAL.ru.md",
        }
        assets = {
            name: {"name": name} for name in required if name != "TECHNICAL.md"
        }
        failures = verifier.asset_set_failures(required, assets, "v9.9.9")
        self.assertEqual(
            failures, ["release v9.9.9 is missing asset TECHNICAL.md"],
        )


class PublishedTrackedAssetTests(unittest.TestCase):
    """The hand-uploaded assets that are tracked files must be the tag's bytes.

    The verifier only checked that the four documents were present, and it
    never compared the downloaded runtime-manifest.json with the tagged blob,
    so a stale or edited upload passed.
    """

    def tagged(self) -> dict[str, bytes]:
        return {
            name: f"tagged {name}\n".encode("utf-8")
            for name in verifier.TRACKED_ASSETS
        }

    def test_the_tracked_assets_are_the_documents_manifest_and_notices(self) -> None:
        self.assertEqual(
            {
                verifier.RUNTIME_MANIFEST, verifier.THIRD_PARTY_NOTICES,
                *verifier.RELEASE_DOCUMENTS,
            },
            set(verifier.TRACKED_ASSETS),
        )

    def test_identical_assets_pass(self) -> None:
        tagged = self.tagged()
        self.assertEqual(
            [], verifier.tracked_asset_failures(dict(tagged), tagged, "v9.9.9")
        )

    def test_a_changed_document_or_manifest_is_reported(self) -> None:
        tagged = self.tagged()
        published = dict(tagged)
        published["README.ru.md"] = b"tagged README.ru.md\r\n"
        published[verifier.RUNTIME_MANIFEST] = b"{}\n"
        failures = verifier.tracked_asset_failures(published, tagged, "v9.9.9")
        self.assertEqual(2, len(failures), failures)
        self.assertTrue(failures[0].startswith(
            f"release asset {verifier.RUNTIME_MANIFEST} differs from tag v9.9.9"
        ), failures)
        self.assertTrue(failures[1].startswith(
            "release asset README.ru.md differs from tag v9.9.9"
        ), failures)

    def test_an_asset_absent_from_the_tag_is_reported(self) -> None:
        tagged = self.tagged()
        published = dict(tagged)
        tagged["TECHNICAL.md"] = None
        self.assertEqual(
            ["release asset TECHNICAL.md: missing from local tag v9.9.9"],
            verifier.tracked_asset_failures(published, tagged, "v9.9.9"),
        )

    def test_an_asset_not_downloaded_is_left_to_the_download_check(self) -> None:
        tagged = self.tagged()
        published = dict(tagged)
        del published["README.md"]
        self.assertEqual(
            [], verifier.tracked_asset_failures(published, tagged, "v9.9.9")
        )

    def test_main_compares_the_downloaded_assets_with_the_tag(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ns-release-verify-") as temp:
            repo = Path(temp)
            fixture = ReleaseFixture(repo)
            for name in verifier.RELEASE_DOCUMENTS:
                (repo / name).write_bytes(f"# {name}\n".encode("utf-8"))
            run_git(repo, "add", *verifier.RELEASE_DOCUMENTS)
            run_git(repo, "commit", "-qm", "documents")
            run_git(repo, "tag", "-f", fixture.tag)
            tag, commit = fixture.tag, fixture.commit
            tracked = (
                verifier.RUNTIME_MANIFEST, verifier.THIRD_PARTY_NOTICES,
                *verifier.RELEASE_DOCUMENTS,
            )
            names = [
                f"neuralscreen-v{fixture.version}-full.zip", verifier.CHECKSUMS,
                *tracked,
            ]
            payload = {
                name: subprocess.run(
                    ["git", "show", f"{tag}:{name}"], cwd=repo,
                    capture_output=True, check=True,
                ).stdout
                for name in tracked
            }
            payload[names[0]] = b"zip"
            payload[verifier.CHECKSUMS] = b"sums"
            # Uploaded from a stale checkout: a document and the manifest.
            payload["README.md"] = b"# an older README\n"
            payload[verifier.RUNTIME_MANIFEST] += b"\n"
            release = {
                "body": "",
                "assets": [
                    {"name": name, "id": index}
                    for index, name in enumerate(names)
                ],
            }

            def gh(args):
                if args[0] == "repo":
                    return "user presets, 12 languages"
                if args[1].endswith("/releases/latest"):
                    return tag
                if "/git/ref/tags/" in args[1]:
                    return json.dumps({"object": {"type": "commit", "sha": commit}})
                return json.dumps(release)

            def fetch_asset(asset_id, dest):
                dest.write_bytes(payload[names[asset_id]])
                return True

            output = io.StringIO()
            with mock.patch.object(verifier, "ROOT", repo), \
                    mock.patch.object(verifier, "_gh", side_effect=gh), \
                    mock.patch.object(verifier, "_fetch", return_value=False), \
                    mock.patch.object(verifier, "_fetch_asset", side_effect=fetch_asset), \
                    mock.patch.object(verifier, "validate_release_set", return_value=[]), \
                    contextlib.redirect_stdout(output):
                code = verifier.main([tag])
        text = output.getvalue()
        self.assertEqual(1, code, text)
        self.assertIn(f"release asset README.md differs from tag {tag}", text)
        self.assertIn(
            f"release asset {verifier.RUNTIME_MANIFEST} differs from tag {tag}", text
        )
        for name in ("README.ru.md", "TECHNICAL.md", verifier.THIRD_PARTY_NOTICES):
            self.assertNotIn(f"release asset {name} differs", text)


def version_resource(numeric: str, file_version: str, product_version: str) -> bytes:
    """A VS_VERSIONINFO resource as rc.exe lays it out, inside PE-like bytes.

    Written from the documented layout (wLength, wValueLength, wType, UTF-16
    key, 32-bit padding, value, padded children), independently of the
    builder's reader.
    """
    def block(key: str, value: bytes = b"", count: int | None = None,
              text: bool = False, children: tuple = ()) -> bytes:
        data = struct.pack(
            "<HHH", 0, len(value) if count is None else count, int(text)
        ) + key.encode("utf-16-le") + b"\0\0"
        data += b"\0" * (-len(data) % 4) + value
        for child in children:
            data += b"\0" * (-len(data) % 4) + child
        return struct.pack("<H", len(data)) + data[2:]

    def string(key: str, value: str) -> bytes:
        raw = value.encode("utf-16-le") + b"\0\0"
        return block(key, raw, count=len(raw) // 2, text=True)

    a, b, c, d = (int(part) for part in numeric.split("."))
    fixed = struct.pack(
        "<13I", 0xFEEF04BD, 0x10000, (a << 16) | b, (c << 16) | d,
        (a << 16) | b, (c << 16) | d, 0x3F, 0, 0x40004, 1, 0, 0, 0,
    )
    table = block("040904b0", text=True, children=(
        string("FileDescription", "NeuralScreen"),
        string("FileVersion", file_version),
        string("ProductName", "NeuralScreen"),
        string("ProductVersion", product_version),
    ))
    root = block("VS_VERSION_INFO", fixed, children=(
        block("StringFileInfo", text=True, children=(table,)),
        block("VarFileInfo", text=True, children=(
            block("Translation", struct.pack("<HH", 0x409, 1200)),
        )),
    ))
    return b"MZ" + b"\x90" * 0x1F2 + root + b"\0" * 0x40


class LauncherVersionFixture(ReleaseFixture):
    runtime_artifacts = ("runtime/runtime.bin", "NeuralScreen.exe")
    mandatory = (*ReleaseFixture.mandatory, "NeuralScreen.exe")


class LauncherVersionTests(unittest.TestCase):
    """The shipped NeuralScreen.exe must report the release version.

    The builder checked native/launcher.rc, but the exe is a generated binary:
    if build-launcher.bat was not rerun after the bump, the archive carried
    an exe whose Properties name the previous release.
    """

    def build_with(self, exe: bytes) -> Path:
        temp = tempfile.TemporaryDirectory(prefix="ns-release-launcher-")
        self.addCleanup(temp.cleanup)
        fixture = type("Fixture", (LauncherVersionFixture,), {"exe": exe})(
            Path(temp.name)
        )
        return fixture.build()

    def test_a_launcher_of_the_release_version_is_packaged(self) -> None:
        exe = version_resource("9.9.0.0", "9.9.0.0", "9.9.0")
        archive = self.build_with(exe)
        with zipfile.ZipFile(archive) as bundle:
            self.assertEqual(exe, bundle.read("NeuralScreen.exe"))

    def test_a_launcher_left_at_the_previous_version_is_refused(self) -> None:
        stale = {
            "all fields": ("9.8.0.0", "9.8.0.0", "9.8.0"),
            "numeric pair": ("9.8.0.0", "9.9.0.0", "9.9.0"),
            "ProductVersion string": ("9.9.0.0", "9.9.0.0", "9.9.0.0"),
        }
        for label, fields in stale.items():
            with self.subTest(label):
                with self.assertRaisesRegex(
                    builder.ReleaseContractError,
                    r"NeuralScreen\.exe version resource differs",
                ):
                    self.build_with(version_resource(*fields))

    def test_a_launcher_without_a_version_resource_is_refused(self) -> None:
        with self.assertRaisesRegex(
            builder.ReleaseContractError, r"NeuralScreen\.exe has no readable"
        ):
            self.build_with(b"MZ" + b"\0" * 512)

    @unittest.skipUnless(sys.platform == "win32", "reads a Windows executable")
    def test_the_reader_agrees_with_a_real_executable(self) -> None:
        # The interpreter running this test is a real MSVC-built PE whose
        # string version is its own sys.version_info.
        values = builder.launcher_version_values(Path(sys.executable).read_bytes())
        expected = "%d.%d.%d" % sys.version_info[:3]
        self.assertEqual(expected, values["ProductVersion"])
        self.assertTrue(values["FILEVERSION"].startswith(
            "%d.%d." % sys.version_info[:2]
        ), values)


class RuntimePayloadTests(unittest.TestCase):
    """What the local interpreter contributes to the archive (v2.1.9 audit).

    v2.1.9 shipped runtime/Scripts/mss.exe - a pip launcher whose shebang is
    the builder's own python.exe path - the 31 headers of runtime/Include/,
    and _brotli.cp313-win_amd64.pyd although "_brotli" is a dropped package.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ns-release-runtime-")
        self.repo = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, rel: str, data: bytes = b"x") -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_pip_launchers_headers_and_dropped_extensions_stay_out(self) -> None:
        sp = builder.SP
        shipped = {
            "runtime/python.exe",
            "runtime/python313.dll",
            sp + "numpy/__init__.py",
            sp + "numpy/_core/_multiarray_umath.cp313-win_amd64.pyd",
            # Share a prefix with a dropped package but are other packages.
            sp + "pipx/__init__.py",
            sp + "yamlloader/__init__.py",
            sp + "brotlicffi/__init__.py",
        }
        dropped = {
            "runtime/Scripts/mss.exe",
            "runtime/Scripts/pip.exe",
            "runtime/Include/Python.h",
            "runtime/Include/cpython/object.h",
            sp + "_brotli.cp313-win_amd64.pyd",
            sp + "brotli.py",
            sp + "_yaml.cp313-win_amd64.pyd",
            sp + "pip/__init__.py",
            sp + "pip-25.0.dist-info/METADATA",
            sp + "distutils-precedence.pth",
        }
        for rel in shipped | dropped:
            self.write(rel)
        self.assertEqual(sorted(shipped), builder.runtime_files(self.repo))

    def test_a_path_component_of_the_builder_is_found(self) -> None:
        # The temp folder lies inside the real home; a stand-in home keeps
        # the two needles apart so each is proven on its own.
        with mock.patch.object(Path, "home", return_value=Path("C:/Users/Tester")):
            needles = builder._builder_path_needles(self.repo)
        root = str(self.repo.resolve())
        leaks = {
            "launcher.exe": b"MZ#!" + root.encode() + b"\\runtime\\python.exe\n",
            "slashes.txt": root.replace("\\", "/").upper().encode() + b"/x",
            "wide.dll": ("\0" + root + "\\a.pdb").encode("utf-16-le"),
            "exact.txt": root.encode(),
            "home.py": b"cache = 'c:/users/tester/AppData'",
        }
        clean = {
            # A sibling folder that merely starts with the same name.
            "sibling.txt": (root + "-other\\file").encode(),
            "placeholder.txt": b"C:\\Users\\Testername\\Python\\Lib",
            "other.txt": b"C:\\Users\\runneradmin\\build",
        }
        with self.assertRaises(builder.ReleaseContractError) as caught:
            builder.assert_no_builder_paths({**leaks, **clean}, needles)
        message = str(caught.exception)
        for name in leaks:
            self.assertIn(name, message)
        for name in clean:
            self.assertNotIn(name, message)
        builder.assert_no_builder_paths(clean, needles)

    def test_the_build_refuses_a_payload_naming_the_builder(self) -> None:
        fixture = ReleaseFixture(self.repo)
        fixture.build()
        launcher = self.repo / "runtime" / "Lib" / "site-packages" / "tool.exe"
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(
            b"MZ#!" + str(self.repo.resolve() / "runtime" / "python.exe").encode()
        )
        fixture.repin()
        with self.assertRaisesRegex(
            builder.ReleaseContractError, r"builder machine's absolute path"
        ):
            fixture.build()


if __name__ == "__main__":
    unittest.main(verbosity=2)
