# Releasing NeuralScreen

Everything below was read out of the code that performs it:
`build_release_zip.py` (the builder), `verify_github.py` (the offline verifier),
`tests/autocheck.py` (the local gate), `tests/run_tests.py` (the suite runner),
`tests/test_release_contract.py` (the release contract tests), `app/settings_io.py`,
`native/launcher.rc` and `_render_docs.py`. Where a step is a convention rather
than something a program enforces, the text says so. English only, like the rest
of the maintainer documentation.

All commands are run from the repository root with the bundled interpreter:

    runtime\python.exe <script> ...

The example version throughout is `1.15.2`; replace it with the real one.

---

## 0. What a release is

| # | Asset | Who produces it |
|---|-------|-----------------|
| 1 | `neuralscreen-vX.Y.Z-full.zip` | `build_release_zip.py` |
| 2 | `SHA256SUMS` | `build_release_zip.py` (sidecar beside the ZIP) |
| 3 | `runtime-manifest.json` | tracked file, validated and pinned by the builder (sidecar) |
| 4 | `THIRD-PARTY-NOTICES.md` | tracked file, validated and pinned by the builder (sidecar) |
| 5 | `README.md` | **you upload it separately** |
| 6 | `README.ru.md` | **you upload it separately** |
| 7 | `TECHNICAL.md` | **you upload it separately** |
| 8 | `TECHNICAL.ru.md` | **you upload it separately** |

Eight assets, no more and no less. `verify_github.py` holds exactly this set in
`required_assets`; a missing one is reported as `release vX is missing asset
README.md`, and anything extra is reported as `release vX carries N asset(s)
beyond the release set: ...`. The set is built from `RELEASE_DOCUMENTS`, the
constant `build_release_zip.py` uses as well, and
`tests/test_release_documents_agree.py` fails if the two lists ever diverge -
the failure mode that matters, because the verifier only reports a missing
document after the release is already published. This is not a formality:
documentation images were
uploaded as assets once and the "required are present" check did not notice,
which is why the extra-asset check exists.

The four documents are inside the ZIP as payload as well (they are part of the
package inventory), but the release page still needs them as separate assets.

`docs/*.png` are **not** release assets. They live in the repository and are
read by the verifier from the tagged Git blobs
(`https://raw.githubusercontent.com/perseval-BLR/NeuralScreen/<tag>/docs/...`).
They must be committed and the tag must be pushed, or the verifier reports
`<name>: fetch failed`.

### What the builder writes, in one line

With the default output directory (the repository root) the builder writes
`neuralscreen-vX.Y.Z-full.zip` and `SHA256SUMS`; `runtime-manifest.json` and
`THIRD-PARTY-NOTICES.md` are the tracked files themselves (they are only
rewritten when the output directory is somewhere else). It prints
`built <name> (<bytes> bytes)` and
`sidecars: SHA256SUMS, runtime-manifest.json, THIRD-PARTY-NOTICES.md`.

---

## 1. The version lives in four places (six literal lines)

| File | Form | For 1.15.2 |
|------|------|------------|
| `build_release_zip.py` (line 35) | `VERSION = "X.Y.Z"` | `VERSION = "1.15.2"` |
| `app/settings_io.py` (line 192) | `APP_VERSION = "X.Y.Z"` | `APP_VERSION = "1.15.2"` |
| `native/launcher.rc` | the numeric pair, comma-separated, **four** fields, `FILEVERSION 1,15,2,0` | `FILEVERSION 1,15,2,0` |
| `native/launcher.rc` | the numeric pair, second field of the same pair | `PRODUCTVERSION 1,15,2,0` |
| `native/launcher.rc` | the string pair, **four** fields, `VALUE "FileVersion", "1.15.2.0"` | `VALUE "FileVersion", "1.15.2.0"` |
| `native/launcher.rc` | the string pair, **three** fields, `VALUE "ProductVersion", "1.15.2"` | `VALUE "ProductVersion", "1.15.2"` |

So: three files, and the launcher `.rc` holds the version twice over (once as
comma numbers, once as strings) - four places, six lines. The comma form is
`X,Y,Z,0`; the `FileVersion` string is `X.Y.Z.0`; the `ProductVersion` string is
`X.Y.Z` with no fourth field. Getting that shape wrong is a failure, not a
warning: the builder validates all four `.rc` values by regex and compares them
to the expected dictionary.

Exactly **one** match of each pattern may exist in the file. A second (even
commented-out) `FILEVERSION` line fails with
`native/launcher.rc must declare exactly one FILEVERSION; found 2`.

Version drift is caught in three independent places:

* the builder, `assert_version_coherence`:
  `version drift: builder, APP_VERSION and launcher.rc disagree; expected {...}, got {...}` - exit code 2, nothing is written;
* the verifier: `version source differs from manifest version: <path>`,
  `tagged version values differ from manifest: <path>`, and
  `requested tag 'vX.Y.Z' does not match builder vY.W.Z`;
* `tests/test_release_contract.py` (`version drift`), which covers the `.rc`
  string fields as well as the other sources;
* the local gate inherits it: `zip: integrity and contents` fails with
  `release manifest: version drift: ...`.

The `VERSION` constant is the source of truth. The tag argument only has to
agree with it - `v1.15.2` while `VERSION = "1.15.1"` fails with
`expected tag must be v1.15.1, got 'v1.15.2'`. Release candidates are refused by
the same check.

---

## 2. The order of operations

Each step says why it is where it is. The order is not a preference - the
builder refuses a dirty tracked tree and an untagged HEAD, and the manifest pins
the tracked inventory, so any commit after the re-pin invalidates it.

### Step 1 - bump the version

Change all six lines of section 1. Nothing else needs a version: `VERSION.txt`
is generated inside the archive from `VERSION` and the commit, and the product
defaults (`config.default.json`) carry no version.

### Step 2 - rebuild the native binaries if their sources changed

    native\build-host.bat        Rebuilds native/nvngx.dll_ns-forwarder.dll first, then native/nvngx.dll (in place; a compile error exits non-zero)
    native\build-launcher.bat    Compiles native/launcher.rc to launcher.res and writes NeuralScreen.exe to the repository root (the icon and the .rc version come from here)

Why: the static gate `worker: fresh, with the hook` fails with
`worker is older than <source> - rerun build-host.bat` when a native input is
newer than the worker, and all three binaries are `REQUIRED_RUNTIME_ARTIFACTS`
and `generated` (not tracked) package members, so the builder refuses with
`required runtime artifact is missing: <path>` if they are absent.
`native\launcher.rc` changed in step 1, so the launcher exe has to be rebuilt
for the change to reach the shipped binary.

### Step 3 - regenerate the documentation screenshots

    runtime\python.exe _render_docs.py

Writes the four tracked images: `docs/screenshot-main-light.png`,
`docs/screenshot-main-dark.png`, `docs/screenshot-settings.png`,
`docs/screenshot-windows.png`. The renderer takes the version from
`main.APP_VERSION`, so it has to run **after** the bump, or the shipped pictures
show yesterday's version. `_render_docs.py` itself is gitignored
(`_render_*.py`), the PNGs are tracked.

Why before the manifest: the re-pin in step 4 requires a clean tracked tree, and
any tracked byte that changes later invalidates the pinned inventory.

### Step 4 - re-pin the runtime manifest

    runtime\python.exe build_release_zip.py vX.Y.Z --write-runtime-manifest

Commit steps 1-3 first: the writer calls `assert_clean_tracked_tree` and fails
with `tracked working tree is dirty; commit or restore these paths: ...`.

What it does: writes `runtime-manifest.json` (schema 3) in canonical JSON -
`indent=2, ensure_ascii=False, sort_keys=True` plus a trailing newline; any other
formatting is rejected later with
`runtime-manifest.json is not canonical UTF-8 JSON`. The manifest pins

* `version_sources` - the three version-source files with their extracted values;
* `runtime.*` - every file under `runtime/` that ships (count, tree digest,
  per-file size and sha256) plus the nine required runtime artifacts. What does
  not ship: `runtime/Scripts/` (pip launchers carry the builder's absolute
  `python.exe` path in their shebang), `runtime/Include/`, Tk, bytecode caches,
  test folders and the `DROP_PACKAGES` site-packages - a dropped name matches
  its folder, `name.*` (`name.py`, `name.libs`, `name.cp313-win_amd64.pyd`)
  and `name-*` (`name-1.0.dist-info`);
* `package.*` - the complete payload inventory, each record marked `origin: git`
  (with its Git blob OID) or `origin: generated`. Size and sha256 are those of
  the bytes in the ZIP. For a tracked file whose `.gitattributes` at the ref say
  `eol=crlf` (today only `NeuralScreen.bat`; cmd.exe needs CRLF) the builder
  ships the CRLF form of the LF blob, as a checkout would write it, and the
  record carries `eol: crlf` - its size and sha256 are the CRLF bytes, its
  `git_blob` is the tagged blob;
* `source_inventory` - every tracked native source/shader, version source and
  shipped resource with its Git blob OID, size and sha256 of the blob itself
  (for an `eol: crlf` file these differ from its `package` record by design).

It fails closed if a mandatory input is untracked
(`mandatory release input is untracked at HEAD: <path>`), if a required runtime
artifact is missing, or on version drift. It prints `wrote <path>`.

Then commit it:

    git add runtime-manifest.json && git commit -m "chore: repin the runtime manifest for vX.Y.Z"

**Re-pin again after every later commit that changes a tracked shipped file.**
The manifest records the Git blob of each of those files, so one commit after the
re-pin makes it stale for the tag. The failures are specific:

* builder: `runtime-manifest.json does not match tagged runtime/source inventory; regenerate and review it before the release commit`;
* local gate (`tests\autocheck.py`, check `zip: integrity and contents`):
  `release manifest: runtime-manifest.json does not match tagged runtime/source inventory; ...` - this is the zip: integrity error;
* verifier (if it got past the builder): `tagged Git blob differs from manifest: <path>`, `package Git blob differs from tag: <path>`.

### Step 5 - run the full suite

    runtime\python.exe tests\run_tests.py --gui

The screen is taken over several times; leave the machine alone and make sure no
NeuralScreen instance is running (the smoke and GUI stages refuse to start if
one is: `NeuralScreen is already running (...) - stop it first`).

Two rules the runner enforces:

* **Every tracked test must be assigned to exactly one group.** A new
  `tests/test_*.py` that is not in `TEST_GROUPS` stops the whole run with
  `RESULT: ERROR - unrouted test: <name>` (exit 1). Being in two groups is
  `test has multiple groups: <name> -> [...]`.
* **Group order decides execution order:** `unit/static`, `WARP`, `GPU`,
  `GUI-E2E`. The static stage (`autocheck.py`) runs first, the smoke test is a
  `GPU` job, the GUI cycle is the last `GUI-E2E` job. `--only <fragment>` runs one
  test and skips the static and smoke stages.

Green looks like `RESULT: PASS - <N> checks, <M> skipped, <seconds>`, with the
per-group counters above it. Anything FAIL/ERROR/TIMEOUT exits non-zero.

Also check the archive situation before this run: the static gate validates
`neuralscreen-v<APP_VERSION>-full.zip` **when that file exists in the root**. For
a new version it does not exist yet and the check reports
`ZIP waits for vX.Y.Z` (a pass). A stale archive of the *same* version left over
from an earlier build is validated and can fail
(`release ZIP: ...`, `a release ZIP exists but tag vX.Y.Z does not ...`).

### Step 6 - commit everything and confirm the tree is clean

    git status --short          # must be empty (config.json is ignored anyway)

The builder will refuse anything else. Ignored files never trip it: `runtime/`,
`config.json`, `*.zip`, `SHA256SUMS`, `_work/`, `_render_*.py` and the rest are
outside the gate.

### Step 7 - tag and push

    git tag vX.Y.Z
    git push origin main
    git push origin vX.Y.Z

The tag has to resolve exactly to HEAD:
`tag vX.Y.Z resolves to <sha>, HEAD is <sha>`. Tagging and then committing again
produces the same message, so tag last.

The verifier needs both sides: it resolves the local tag and the remote tag
through the GitHub API and compares them
(`local/remote tag mismatch: <a> != <b>`, `local tag vX.Y.Z is missing or invalid`).

### Step 8 - build the archive

    runtime\python.exe build_release_zip.py vX.Y.Z

This is the step that must **not** run earlier. The builder calls, in order:
`assert_clean_tracked_tree` -> `assert_release_tag` (tag exists and equals HEAD)
-> mandatory inputs present at the tag -> `validate_runtime_manifest` against the
tag (`git_ref=tag`, not HEAD) -> DLL architecture gate -> payload packaging ->
`assert_no_builder_paths` (no packaged file may name the repository root or the
builder's home folder, in either slash form, UTF-8 or UTF-16) -> the ZIP.

It refuses, with exit code 2 and `RELEASE CONTRACT FAILED: <message>`:

| Situation | Message |
|-----------|---------|
| dirty tracked tree | `tracked working tree is dirty; commit or restore these paths: ...` |
| tag argument not `v<version>` | `expected tag must be vX.Y.Z, got '...'` |
| tag missing | `required tag vX.Y.Z does not exist` |
| tag not at HEAD | `tag vX.Y.Z resolves to <sha>, HEAD is <sha>` |
| mandatory file absent | `mandatory release files are missing: <paths>` |
| mandatory file untracked at the tag | `mandatory release input is untracked at vX.Y.Z: <path>` |
| other input not in the tag | `release input is not tracked by the tag: <path>` |
| required runtime artifact absent | `required runtime artifact is missing: <path>` |
| manifest out of date | `runtime-manifest.json does not match tagged runtime/source inventory; regenerate and review it before the release commit` |
| manifest formatting | `runtime-manifest.json must use canonical UTF-8 JSON formatting` |
| version drift | `version drift: builder, APP_VERSION and launcher.rc disagree; expected {...}, got {...}` |
| missing kernel archs | `runtime architecture mismatch: missing sm_75, sm_89` |
| file changed during the build | `package file ... changed during build: <path>` |
| a file names the build machine | `package files contain the builder machine's absolute path: <paths>` |

The build is deterministic: ZIP timestamps are pinned to 1980-01-01, modes to
0644, `VERSION.txt` carries no wall-clock line, so two builds of the same commit
are byte-identical.

### Step 9 - create the GitHub release with all eight assets

Draft the notes in `_work/` (gitignored) - see section 3 - then:

    gh release create vX.Y.Z \
      -R perseval-BLR/NeuralScreen \
      --title "vX.Y.Z - <one line>" \
      --notes-file _work/release-XYZ-notes.md \
      neuralscreen-vX.Y.Z-full.zip SHA256SUMS runtime-manifest.json THIRD-PARTY-NOTICES.md \
      README.md README.ru.md TECHNICAL.md TECHNICAL.ru.md

The four documents are the ones the builder does not emit. `gh` infers the
repository from `origin`, so `-R` is optional; the tracked scripts use
`perseval-BLR/NeuralScreen`, which is the legacy name of the same repository
(GitHub serves it at `perseval-BLR/DLSS5-NeuralScreen`; the READMEs' raw URLs and
`verify_github.py` still use the legacy name, so the redirect is relied upon).

The release must become the **Latest** one - the verifier fails with
`vX.Y.Z is not the Latest release (GitHub says 'vY.W.Z')`.

### Step 10 - verify

    runtime\python.exe verify_github.py vX.Y.Z

It downloads the assets through `gh`, checks the documentation against the tagged
Git blobs over `raw.githubusercontent.com`, resolves the tag on GitHub, reads the
release body, and validates the whole set offline (see section 4). It needs
network access and an authenticated `gh`; it writes downloads into
`_work\verify-github\` (gitignored).

PASS prints one `[OK] <name> matches tag vX.Y.Z (<sha12>)` line per document, a
`[OK] repo description: ...` line, and closes with:

    ============================================================
    OK: vX.Y.Z on GitHub matches tag, manifest and checksums

---

## 3. Release-note rules

The notes are the release body, written once, English only. `_work/release-1151-notes.md`
is the draft that went out as v1.15.1 and the published body is byte-identical to
it - that file is the format to copy.

* **Language: English only.** The two-language body (EN, then `---`, then RU) was
  retired; the length check measures the whole body now, not the part before the
  separator.
* **Length: under 5000 characters**, measured by the static gate
  `tests\autocheck.py`, check `release notes: concise`. It resolves the *latest*
  release through `gh` and reports `the release body is <N> characters - too long`.
  Two things follow: the check can only judge a release **after** it is published,
  and it always judges the latest one - run before publishing, it measures the
  previous release. Recent bodies were 3934 (v1.15.0) and 4675 (v1.15.1); four
  older ones are already over the line - v1.4.0 9995, v1.4.1 5232, v1.7.0 5130,
  v1.11.0 5082 - and nothing caught them at the time. Stay well under.
* **Line endings: LF, no BOM.** The drafts are plain UTF-8 with LF; write the file
  with LF and publish it with `--notes-file` so the bytes on GitHub are the bytes
  you reviewed.
* **Two blocks are mandatory verbatim** (the verifier compares the exact strings,
  not prefixes):
  1. the notice line
     `> **Notice.** Not affiliated with NVIDIA; NVIDIA, DLSS and the NVIDIA logo are NVIDIA Corporation's trademarks. The bundled NVIDIA runtimes are NVIDIA's property, included unmodified as received, research/educational use only, no warranty, use at your own risk.`
     - a missing or reworded notice is `release vX is missing standard NVIDIA notice`;
  2. the driver warning, exactly:
     ```
     > [!WARNING]
     > NeuralScreen requires the latest NVIDIA driver. Operation with older drivers or unsupported/non-standard configurations is not guaranteed.
     ```
     - a missing or reworded warning is `release vX is missing driver warning alert`.
     Note the two lines are compared as one string, so both lines and the space
     after both `>` markers have to match.
* **The AMD tester call** goes in a `> [!IMPORTANT]` block: the standing call for
  Radeon testers, pointing at the separate repository `NeuralScreen-AMD`
  (`https://github.com/perseval-BLR/NeuralScreen-AMD`), telling them the build has
  not run on a real Radeon yet, to start from `native/AMD.md`, and to attach the
  diagnostic package (**Settings -> Program -> Create diagnostic package**) when
  it does not come up. Every release since v1.13.1 carries it. Nothing checks this
  block - it is a convention the owner keeps, not a gate.
* **Then**: one paragraph naming what the release is, `## Fixes` (one bolded
  symptom per paragraph, with the mechanism and the measurement), and a `##
  Tests` section with the suite count and `PASS / FAIL / SKIP` plus the GUI-E2E
  result.
* **Punctuation**: the owner's rule is plain hyphens, never em-dashes, in text
  written for them. (The bodies published so far are full of em-dashes; do not add
  to that.) No Russian twin of the notes - the body is English only.

---

## 4. What verification checks, and how it fails

`verify_github.py` first checks the tag against the builder's `VERSION`, then, in
order:

1. every document against the tagged Git blob (`git show <tag>:<path>` vs
   `raw.githubusercontent.com/<tag>/<path>`);
2. the release exists and resolves to a commit, and the local and remote tag
   commits agree;
3. the release body carries the two mandatory blocks;
4. the asset set is exactly the eight (missing and extra both reported);
5. all eight assets are downloaded, and the six that are tracked files (the four
   documents, `runtime-manifest.json`, `THIRD-PARTY-NOTICES.md`) must be
   byte-identical to `git show <tag>:<name>` - an asset uploaded from a
   checkout other than the tag is a failure, not merely "present";
6. the built set is validated offline:
   canonical manifest JSON, schema/product identity, tag/manifest agreement,
   version sources, package and runtime inventories, `source_inventory` vs the
   tagged blobs, outer `SHA256SUMS` (exactly the ZIP, manifest, notices) with
   matching digests, ZIP member order, deterministic timestamps and modes, member
   checksums against the ZIP's own `SHA256SUMS`, `VERSION.txt` (version, tag,
   commit, manifest digest), and - if the local archive is still in the root - the
   published ZIP against the local one;
7. the release is the Latest one;
8. the repository description still contains `user presets` and `12 languages`.

Exit code 0 means PASS. Exit code 1 means `FAIL: <N>` followed by one line per
failure. The messages worth recognising:

* assets: `release vX is missing asset README.md`,
  `release vX carries 2 asset(s) beyond the release set: docs/screenshot-main-dark.png, ...`
* documents: `<name>: missing from local tag vX`, `<name>: fetch failed`,
  `<name>: tagged blob mismatch local=<sha12> github=<sha12>`
* uploaded tracked assets: `release asset <name> differs from tag vX: asset=<sha12> tag=<sha12>`,
  `release asset <name>: missing from local tag vX`,
  `could not download release asset <name>`
* body: `release vX is missing standard NVIDIA notice`,
  `release vX is missing driver warning alert`
* release/tag: `release vX does not exist`, `local tag vX is missing or invalid`,
  `requested tag 'vX' does not match builder vY`, `could not resolve remote tag vX to a commit`,
  `local/remote tag mismatch: <a> != <b>`, `vX is not the Latest release (GitHub says 'vY')`
* set integrity: `release is missing asset <file>`,
  `invalid runtime-manifest.json: ...`, `runtime-manifest.json version is not X.Y.Z`,
  `tag/manifest mismatch: requested=..., manifest=..., version=...`,
  `runtime-manifest.json is not canonical UTF-8 JSON`,
  `outer SHA256SUMS inventory mismatch: expected [...], got [...]`,
  `outer checksum mismatch: <name>`, `ZIP member order is not canonical`,
  `ZIP member timestamp is not deterministic: <name>`,
  `ZIP has undeclared payload members: [...]`,
  `ZIP is missing declared payload members: [...]`,
  `package file checksum mismatch: <name>`,
  `runtime manifest file inventory differs from ZIP`,
  `runtime manifest tree digest differs from ZIP`, `ZIP checksum mismatch: <name>`,
  `published ZIP differs from the local release ZIP`,
  `tagged Git blob differs from manifest: <path>`,
  `package Git blob differs from tag: <path>`,
  `package Git file checksum differs from tag: <path>` (for an `eol: crlf`
  record the tag's blob is converted to CRLF first),
  `packaged CRLF file has stray line endings: <name>`,
  `package record has an unknown eol conversion: <path>`
* repository: `repo description lost the marker 'user presets'`,
  `could not read repository description`

---

## 5. Common mistakes, and the failure each one produces

| Mistake | What you see |
|---------|--------------|
| The four documents were not uploaded as assets | `release vX is missing asset README.md` (one line per missing name) |
| Documents uploaded but not committed to the tag (or the tag not pushed) | `<name>: missing from local tag vX`, `<name>: fetch failed`, `<name>: tagged blob mismatch ...` |
| A document, the manifest or the notices uploaded from a checkout other than the tag | `release asset README.md differs from tag vX: asset=<sha12> tag=<sha12>` |
| Documentation PNGs uploaded as assets | `release vX carries N asset(s) beyond the release set: docs/screenshot-...` |
| Manifest not re-pinned after a later commit | builder: `runtime-manifest.json does not match tagged runtime/source inventory; regenerate and review it before the release commit`; local gate: `release manifest: runtime-manifest.json does not match ...` (the zip: integrity error); verifier: `tagged Git blob differs from manifest: <path>` |
| Manifest re-pinned but not committed | `tracked working tree is dirty; commit or restore these paths:` on the next builder run |
| Version drift (usually the `.rc` pair forgotten) | `version drift: builder, APP_VERSION and launcher.rc disagree; expected {...}, got {...}`; in the local gate the same text under `release manifest:`; in `tests\test_release_contract.py`, `version drift` |
| An old `FILEVERSION` line left in the `.rc` | `native/launcher.rc must declare exactly one FILEVERSION; found 2` |
| Dirty tracked tree | `tracked working tree is dirty; commit or restore these paths:` - nothing is written, exit 2 |
| Builder run before tagging | `required tag vX.Y.Z does not exist` |
| Anything committed after the tag | `tag vX.Y.Z resolves to <a>, HEAD is <b>` |
| A mandatory file not committed | `mandatory release input is untracked at vX.Y.Z: <path>` / `release input is not tracked by the tag: <path>` |
| A native binary or the runtime missing | `required runtime artifact is missing: native/nvngx_dlssg.dll` |
| Wrong DLL build | `runtime architecture mismatch: missing sm_86` (the DLL must carry sm_75/86/89/120 fatbins) |
| Notes too long | `release notes: concise: the release body is 5180 characters - too long` (only after publishing) |
| Notes missing a mandatory block | `release vX is missing driver warning alert` |
| The release was not promoted to Latest | `vX is not the Latest release (GitHub says 'vB')` |
| A stale archive of the same version left in the root | `zip: integrity and contents` fails on it: `release ZIP: ...` / `a release ZIP exists but tag vX.Y.Z does not - the archive cannot be matched to a release` |
| A new test file with no group | `RESULT: ERROR - unrouted test: test_new_thing.py` from `tests\run_tests.py` |
| `MANDATORY_FILES` / `REQUIRED_RUNTIME_ARTIFACTS` edited by hand | `tests\test_release_lists_pinned.py` fails: `MANDATORY_FILES lost <path> - a file the contract ...` |
| Repository description edited | `repo description lost the marker 'user presets'` |

---

## 6. Things this document could not derive from code

* **There is no release script.** `git ls-files` contains no `.github/` and no
  workflow; the whole procedure is manual (`build_release_zip.py` -> `gh release`).
  Step 9's command line is reconstructed from the asset set the verifier demands;
  the exact flags the owner uses are not recorded anywhere in the repository.
* **Nothing enforces screenshot freshness.** `_render_docs.py` is gitignored and
  no check compares the PNGs with the version or the commit; the verifier only
  requires that the tagged blob equals what GitHub serves. Regenerating them every
  release is the correct reading of "the screenshots must show the real version",
  which the script itself states - but it is not machine-enforced.
* **Step order for the screenshots** (before the manifest re-pin) is what the code
  allows, because the re-pin requires a clean tracked tree; it is not written down
  as a procedure anywhere.
* **The `> [!IMPORTANT]` AMD block** is verified only in the sense that a
  human reads the release; `verify_github.py` checks the notice and the driver
  warning and nothing else in the body.
* The notes-length check reads the **latest** release, so it cannot gate a draft.
  There is no pre-publish length gate in the code.
