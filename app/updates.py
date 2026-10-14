"""Official-release discovery and the UI-facing updater controller."""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import subprocess
import time
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

try:
    from . import update_apply
    from .update_package import UpdateError, parse_version, stage_release
except ImportError:  # Modules are also imported from the embedded app path.
    import update_apply
    from update_package import UpdateError, parse_version, stage_release


REPOSITORY = "perseval-BLR/NeuralScreen"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
MAX_RELEASE_JSON = 1024 * 1024
_API_HOSTS = {"api.github.com"}


@dataclass(frozen=True)
class ReleaseInfo:
    version: tuple[int, int, int]
    tag: str
    release_url: str
    asset_name: str
    asset_id: int
    download_url: str
    size: int
    sha256: str

    @property
    def version_text(self) -> str:
        return ".".join(str(part) for part in self.version)


@dataclass(frozen=True)
class UpdateState:
    phase: str = "idle"
    latest_version: str | None = None
    received_bytes: int = 0
    total_bytes: int = 0
    error_code: str | None = None
    can_install: bool = False


def install_id_for(install_dir: str | Path) -> str:
    """Return the full Windows-normalized identity for an absolute install root."""
    install_id = hashlib.sha256(
        os.path.normcase(str(Path(install_dir).resolve())).encode("utf-8")
    ).hexdigest()
    return install_id


class _RedirectPolicy(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: set[str]):
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, request, fp, code, message, headers, new_url):
        target = urllib.parse.urljoin(request.full_url, new_url)
        parsed = urllib.parse.urlsplit(target)
        if (parsed.scheme != "https" or parsed.hostname not in self.allowed_hosts
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443)):
            raise UpdateError("invalid_release")
        return super().redirect_request(request, fp, code, message, headers, target)


def _open_url(url: str, *, allowed_hosts: set[str], accept: str):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in allowed_hosts
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443)):
        raise UpdateError("invalid_release")
    request = urllib.request.Request(url, headers={
        "User-Agent": "NeuralScreen-Updater/1.0",
        "Accept": accept,
    })
    opener = urllib.request.build_opener(_RedirectPolicy(allowed_hosts))
    try:
        return opener.open(request, timeout=15)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise UpdateError("rate_limit") from exc
        if exc.code == 404 and url == API_URL:
            raise UpdateError("invalid_release") from exc
        raise UpdateError("network") from exc
    except UpdateError:
        raise
    except (OSError, TimeoutError, ValueError) as exc:
        raise UpdateError("network") from exc


def _decode_release_json(data: bytes) -> dict:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (UnicodeDecodeError, ValueError) as exc:
        raise UpdateError("invalid_release") from exc
    if not isinstance(payload, dict):
        raise UpdateError("invalid_release")
    return payload


def _release_info(payload: dict, current: tuple[int, int, int]) -> ReleaseInfo | None:
    tag = payload.get("tag_name")
    match = re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag) if isinstance(tag, str) else None
    if match is None or payload.get("draft") is not False or payload.get("prerelease") is not False:
        raise UpdateError("invalid_release")
    version = parse_version(".".join(match.groups()))
    release_url = f"https://github.com/{REPOSITORY}/releases/tag/{tag}"
    if payload.get("html_url") != release_url:
        raise UpdateError("invalid_release")
    if version <= current:
        return None

    name = f"neuralscreen-{tag}-full.zip"
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("invalid_release")
    named = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name]
    if len(named) != 1:
        raise UpdateError("invalid_release")
    asset = named[0]
    asset_id, size, digest = asset.get("id"), asset.get("size"), asset.get("digest")
    expected_url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"
    if (asset.get("state") != "uploaded"
            or isinstance(asset_id, bool) or not isinstance(asset_id, int) or asset_id <= 0
            or isinstance(size, bool) or not isinstance(size, int) or size <= 0
            or not isinstance(digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
            or asset.get("browser_download_url") != expected_url):
        raise UpdateError("invalid_release")
    return ReleaseInfo(
        version=version,
        tag=tag,
        release_url=release_url,
        asset_name=name,
        asset_id=asset_id,
        download_url=expected_url,
        size=size,
        sha256=digest.removeprefix("sha256:"),
    )


def check_latest(current_version: str) -> ReleaseInfo | None:
    """Return one newer stable official release, or ``None`` when current."""
    current = parse_version(current_version)
    with _open_url(
        API_URL, allowed_hosts=_API_HOSTS, accept="application/vnd.github+json"
    ) as response:
        try:
            payload_bytes = response.read(MAX_RELEASE_JSON + 1)
        except (OSError, ValueError) as exc:
            raise UpdateError("network") from exc
    if len(payload_bytes) > MAX_RELEASE_JSON:
        raise UpdateError("invalid_release")
    return _release_info(_decode_release_json(payload_bytes), current)


class UpdateController:
    """Serialize background discovery/staging and publish snapshots through poll."""

    def __init__(self, current_version: str, install_dir: str | Path):
        parse_version(current_version)
        self.current_version = current_version
        self.install_dir = Path(install_dir)
        self._state = UpdateState()
        self._events: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._active = False
        self._worker_running = False
        self._closed = False
        self._thread: threading.Thread | None = None
        self._release: ReleaseInfo | None = None
        self._staged = None
        self._response = None
        self._apply_plan = None
        self._helper = None
        self._authorized = False
        self._arm_acknowledged = False
        self._cancel_install = threading.Event()

    @property
    def staged_release(self):
        """The validated candidate and ownership inventory for the apply layer."""
        with self._lock:
            return self._staged

    @property
    def official_release_url(self) -> str:
        return RELEASES_URL

    def _start(self, phase: str, latest_version: str | None,
               total_bytes: int, work) -> bool:
        with self._lock:
            if self._closed or self._active:
                return False
            self._active = True
            self._worker_running = True
            self._state = UpdateState(
                phase=phase,
                latest_version=latest_version,
                received_bytes=0,
                total_bytes=total_bytes,
                error_code=None,
                can_install=False,
            )

            def run() -> None:
                event = None
                try:
                    event = work()
                except UpdateError as exc:
                    event = ("error", exc.code)
                except OSError:
                    event = ("error", "prepare_failed" if phase == "installing" else "network")
                except Exception:
                    event = ("error", "prepare_failed" if phase == "installing" else "invalid_package")
                finally:
                    with self._lock:
                        self._worker_running = False
                        if event is not None:
                            self._events.put(event)
                        self._events.put(("done", None))

            thread = threading.Thread(target=run, name="NeuralScreen updater", daemon=True)
            self._thread = thread
            try:
                thread.start()
            except RuntimeError:
                self._active = False
                self._worker_running = False
                self._state = UpdateState(phase="error", error_code="network")
                return False
            return True

    def check(self) -> bool:
        """Start one official latest-release query on a daemon worker."""
        with self._lock:
            if self._closed or self._active or not self._settle_install():
                return False
            self._release = None

        def work():
            self._discard_staged()
            return ("checked", check_latest(self.current_version))

        return self._start("checking", self.current_version, 0, work)

    def create_download_transaction(self) -> Path:
        """Create a new owned sibling transaction, never a reused authorization."""
        return update_apply.create_transaction(self.install_dir)

    def _discard_staged(self) -> None:
        with self._lock:
            staged = self._staged
        if staged is not None:
            update_apply.discard_staged(staged, self.install_dir)
            with self._lock:
                if self._staged is staged:
                    self._staged = None

    def _settle_install(self) -> bool:
        """Called under the controller lock before starting another operation."""
        plan = self._apply_plan
        if plan is None:
            return True
        if self._authorized or self._helper is not None and self._helper.poll() is None:
            return False
        try:
            transaction = Path(plan["bootstrap_dir"]).parent
            marker = Path(plan["install_dir"]) / update_apply.MARKER
            if marker.exists() or marker.is_symlink():
                return False
            if (transaction / "journal.jsonl").exists():
                records = update_apply.read_journal(plan)
                if any(record["event"] == "intent" for record in records) or not any(record["event"] == "abandoned" for record in records):
                    return False
            elif self._helper is not None:
                return False
            for name in ("armed", "proceed", "cancel", "cancelled"):
                update_apply.protocol(plan, name)
            update_apply.clear_uncommitted(plan)
            self._apply_plan = None
            self._helper = None
            self._arm_acknowledged = False
            self._cancel_install.clear()
            return True
        except (OSError, UpdateError):
            return False

    def download(self, transaction_dir: str | Path | None = None) -> bool:
        """Start one verified ZIP download and candidate staging operation."""
        with self._lock:
            if (self._closed or self._active
                    or not self._settle_install()
                    or self._state.phase not in {"available", "error"}
                    or self._release is None):
                return False
            release = self._release

        def track_response(response) -> None:
            with self._lock:
                if self._closed:
                    response.close()
                else:
                    self._response = response

        def progress(received: int, total: int) -> None:
            self._events.put(("progress", (received, total)))

        def work():
            self._discard_staged()
            destination = transaction_dir if transaction_dir is not None else self.create_download_transaction()
            staged = stage_release(
                release, destination, install_dir=self.install_dir,
                progress=progress, response_callback=track_response,
            )
            return ("ready", staged)

        return self._start("downloading", release.version_text, release.size, work)

    def arm_install(self, *, config_path, original_cwd, messages) -> bool:
        """Prepare/arm asynchronously. Caller must separately authorize on its main thread."""
        with self._lock:
            if self._closed or self._active or self._apply_plan is not None or self._staged is None or not self._staged.can_install:
                return False
            staged = self._staged
        self._cancel_install.clear()

        def prepare_and_arm():
            helper_env = update_apply.helper_environment(os.environ)
            path = update_apply.prepare_plan(staged, config_path=config_path,
                                             original_cwd=original_cwd, messages=messages)
            plan = update_apply.load_plan(path)
            with self._lock:
                self._apply_plan = plan
            if self._cancel_install.is_set():
                update_apply.write_protocol(plan, "cancel")
            bootstrap = Path(plan["bootstrap_dir"])
            process = subprocess.Popen(
                [str(bootstrap / "runtime/pythonw.exe"), "-I", "-S", str(bootstrap / "update_apply.py"),
                 "--plan", str(path), "--parent-pid", str(os.getpid())],
                cwd=bootstrap, env=helper_env, shell=False, close_fds=True,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
            with self._lock:
                self._helper = process
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self._cancel_install.is_set():
                    update_apply.write_protocol(plan, "cancel")
                    raise UpdateError("prepare_failed")
                if update_apply.protocol(plan, "armed"):
                    return ("armed", None)
                if process.poll() is not None:
                    if not (path.parent / "journal.jsonl").exists():
                        raise UpdateError("restart_required")
                    break
                time.sleep(0.025)
            update_apply.write_protocol(plan, "cancel")
            raise UpdateError("prepare_failed")

        def work():
            try:
                return prepare_and_arm()
            except Exception:
                with self._lock:
                    plan = self._apply_plan
                if plan is not None:
                    try:
                        update_apply.write_protocol(plan, "cancel")
                    except (OSError, UpdateError):
                        pass
                else:
                    try:
                        self._discard_staged()
                    except (OSError, UpdateError):
                        pass
                raise

        return self._start("installing", staged.version, 0, work)

    def poll_armed(self) -> bool:
        """Return matching ACK readiness for the caller's busy recheck/freeze."""
        self.poll()
        with self._lock:
            return self._arm_acknowledged and not self._authorized and not self._cancel_install.is_set()

    def proceed_install(self) -> bool:
        """Explicit main-thread authorization after its final busy check and freeze."""
        with self._lock:
            plan = self._apply_plan
            if self._closed or not self._arm_acknowledged or self._authorized or self._cancel_install.is_set() or plan is None:
                return False
            try:
                if (not update_apply.protocol(plan, "armed") or self._helper is None
                        or self._helper.poll() is not None):
                    raise UpdateError("prepare_failed")
                update_apply.atomic_json(self.install_dir / update_apply.MARKER, update_apply.identity(plan))
                update_apply.write_protocol(plan, "proceed")
                self._authorized = True
                return True
            except (OSError, UpdateError):
                self._cancel_install.set()
                self._arm_acknowledged = False
                try:
                    update_apply.write_protocol(plan, "cancel")
                except (OSError, UpdateError):
                    pass
                self._state = UpdateState(phase="error", latest_version=self._state.latest_version,
                                          error_code="prepare_failed")
                return False

    def cancel_install(self) -> bool:
        """Cancel before explicit proceed. Ordinary close cannot revoke authorized teardown."""
        with self._lock:
            if self._authorized:
                return False
            self._cancel_install.set()
            self._arm_acknowledged = False
            plan = self._apply_plan
            if self._state.phase == "installing":
                self._state = UpdateState(phase="error", latest_version=self._state.latest_version,
                                          error_code="prepare_failed")
        if plan is not None:
            try:
                update_apply.write_protocol(plan, "cancel")
            except (OSError, UpdateError):
                return False
        return True

    def poll(self) -> UpdateState:
        """Apply queued worker results and return a display-only state snapshot."""
        with self._lock:
            while True:
                try:
                    event, value = self._events.get_nowait()
                except queue.Empty:
                    break
                if event == "checked":
                    if value is None:
                        self._state = UpdateState(phase="current", latest_version=self.current_version)
                    else:
                        self._release = value
                        self._state = UpdateState(phase="available", latest_version=value.version_text)
                elif event == "progress":
                    received, total = value
                    self._state = UpdateState(
                        phase="downloading", latest_version=self._state.latest_version,
                        received_bytes=received, total_bytes=total,
                    )
                elif event == "ready":
                    self._staged = value
                    self._state = UpdateState(
                        phase="ready", latest_version=value.version,
                        received_bytes=self._state.total_bytes,
                        total_bytes=self._state.total_bytes,
                        error_code=value.refusal_code,
                        can_install=value.can_install,
                    )
                elif event == "error":
                    self._state = UpdateState(
                        phase="error", latest_version=self._state.latest_version,
                        error_code=value,
                    )
                elif event == "armed":
                    if not self._cancel_install.is_set():
                        self._arm_acknowledged = True
                elif event == "done":
                    self._active = False
                    self._response = None
            if (self._apply_plan is not None and not self._authorized
                    and not self._worker_running
                    and self._helper is not None and self._helper.poll() is not None
                    and self._state.phase == "installing"):
                self._arm_acknowledged = False
                journal = Path(self._apply_plan["bootstrap_dir"]).parent / "journal.jsonl"
                self._state = UpdateState(phase="error", latest_version=self._state.latest_version,
                                          error_code="prepare_failed" if journal.exists() else "restart_required")
            return self._state

    def _cleanup_closed(self) -> None:
        self.poll()
        with self._lock:
            if self._authorized or not self._settle_install():
                return
        try:
            self._discard_staged()
        except (OSError, UpdateError):
            pass

    def close(self) -> None:
        """Cancel streamed work and reclaim safe staging during ordinary teardown."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            response = self._response
            self._response = None
            worker = self._thread if self._worker_running else None
        self.cancel_install()
        if response is not None:
            try:
                response.close()
            except OSError:
                pass
        if worker is None:
            self._cleanup_closed()
        else:
            def finish():
                worker.join()
                self._cleanup_closed()
            # Normal teardown continues, but Python waits for staging reclamation.
            cleanup = threading.Thread(target=finish, name="NeuralScreen update cleanup", daemon=False)
            try:
                cleanup.start()
            except RuntimeError:
                finish()
