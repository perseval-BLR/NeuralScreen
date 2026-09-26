"""Isolated, fail-closed compatibility preflight for NeuralScreen.

The module intentionally knows nothing about desktop capture or presentation.
It gives a runner deterministic synthetic RGBA frames and accepts compatibility
only when every Evaluate call returns a complete output frame.

Integration points are deliberately small:

* :class:`CompatibilityKey` identifies the app/runtime/GPU/display tuple;
* :class:`CompatibilityPreflight` owns cache lookup and Create/Evaluate policy;
* a platform runner implements :class:`SelfTestRunner`;
* cache I/O and time are injectable for deterministic tests.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Callable, Mapping, Protocol, Sequence


CACHE_SCHEMA = "neuralscreen.compatibility/v1"
DEFAULT_COOLDOWN_SECONDS = 30 * 60
_SHA256_HEX_LENGTH = 64


def _stable_value(value: Any) -> Any:
    """Return a deterministic JSON value without retaining object reprs."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("non-finite values are not valid key components")
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _stable_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_stable_value(item) for item in value]
    raise TypeError(f"unsupported compatibility key value: {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _stable_value(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


class FileSystem(Protocol):
    """Minimal filesystem surface used by key hashing and cache storage."""

    def read_bytes(self, path: str | os.PathLike[str]) -> bytes: ...

    def read_text(self, path: str | os.PathLike[str]) -> str: ...

    def atomic_write_text(self, path: str | os.PathLike[str], text: str) -> None: ...


class LocalFileSystem:
    """Production filesystem implementation with durable atomic replacement."""

    def read_bytes(self, path: str | os.PathLike[str]) -> bytes:
        return Path(path).read_bytes()

    def read_text(self, path: str | os.PathLike[str]) -> str:
        return Path(path).read_text(encoding="utf-8")

    def atomic_write_text(self, path: str | os.PathLike[str], text: str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", delete=False,
                dir=target.parent, prefix=f".{target.name}.", suffix=".tmp",
            ) as stream:
                temporary = stream.name
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            temporary = None
            # Best effort: Windows cannot fsync a directory handle opened in
            # the ordinary way, while POSIX can make the rename durable.
            try:
                directory_fd = os.open(target.parent, os.O_RDONLY)
            except (AttributeError, OSError):
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass


def sha256_file(
    path: str | os.PathLike[str], *, filesystem: FileSystem | None = None,
) -> str:
    """Hash a runtime artifact without exposing its path in the result.

    The production path caches the digest per (path, size, mtime): the runtime
    is 165 MB and the compatibility guard hashes it before EVERY worker start -
    every monitor switch, window switch, Spout/HDR toggle, motion-backend
    change, GPU switch, auto-revive and NR revive - on a single-threaded main
    loop, where it read as a 0.2-0.4 s hitch (audit H2). The stat is what makes
    the cache safe: a file that changed cannot keep its old digest, which is
    the one thing this function has to get right.

    A caller-supplied filesystem is a test seam and is never cached: a test
    has to be able to simulate a changed file.
    """
    if filesystem is not None:
        return hashlib.sha256(filesystem.read_bytes(path)).hexdigest()
    key = _cache_key(path)
    if key is not None:
        with _HASH_CACHE_LOCK:
            cached = _HASH_CACHE.get(key)
        if cached is not None:
            return cached
    digest = hashlib.sha256(LocalFileSystem().read_bytes(path)).hexdigest()
    if key is not None:
        _remember(key, digest)
    return digest


#: path -> digest, keyed by (path, size, mtime_ns). Bounded: the program hashes
#: a handful of artifacts, and a bound keeps a long test run from growing it.
_HASH_CACHE: dict = {}
_HASH_CACHE_LOCK = threading.Lock()
_HASH_CACHE_MAX = 64


def _cache_key(path):
    """(realpath, size, mtime_ns) for a real file, or None when it is not one."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    try:
        return (os.path.realpath(path), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return None


def _remember(key, digest: str) -> None:
    with _HASH_CACHE_LOCK:
        if len(_HASH_CACHE) >= _HASH_CACHE_MAX:
            _HASH_CACHE.clear()
        _HASH_CACHE[key] = digest


@dataclass(frozen=True)
class CompatibilityKey:
    """Everything that can change the result of Create/Evaluate."""

    app_version: str
    runtime_sha256: str
    worker_sha256: str
    selected_gpu: Any
    driver_version: str
    display_mode: Any

    @classmethod
    def from_files(
        cls,
        *,
        app_version: str,
        runtime_path: str | os.PathLike[str],
        worker_path: str | os.PathLike[str],
        selected_gpu: Any,
        driver_version: str,
        display_mode: Any,
        filesystem: FileSystem | None = None,
    ) -> "CompatibilityKey":
        # Pass the seam through as-is: `filesystem or LocalFileSystem()` would
        # hand a filesystem to sha256_file even when the caller passed none,
        # which routes around its digest cache and re-reads the 165 MB runtime
        # on every call (audit H2). None means "the real filesystem", and that
        # is the path that caches.
        return cls(
            app_version=str(app_version),
            runtime_sha256=sha256_file(runtime_path, filesystem=filesystem),
            worker_sha256=sha256_file(worker_path, filesystem=filesystem),
            selected_gpu=_stable_value(selected_gpu),
            driver_version=str(driver_version),
            display_mode=_stable_value(display_mode),
        )

    def payload(self) -> dict[str, Any]:
        runtime_hash = str(self.runtime_sha256).casefold()
        worker_hash = str(self.worker_sha256).casefold()
        for label, value in (
            ("runtime_sha256", runtime_hash), ("worker_sha256", worker_hash),
        ):
            if len(value) != _SHA256_HEX_LENGTH or any(
                ch not in "0123456789abcdef" for ch in value
            ):
                raise ValueError(f"{label} must be a SHA256 hex digest")
        return {
            "app_version": str(self.app_version),
            "runtime_sha256": runtime_hash,
            "worker_sha256": worker_hash,
            "selected_gpu": _stable_value(self.selected_gpu),
            "driver_version": str(self.driver_version),
            "display_mode": _stable_value(self.display_mode),
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical_json(self.payload()).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SyntheticFrame:
    """A deterministic input frame; no capture source or window is involved."""

    index: int
    width: int
    height: int
    pixel_format: str
    pixels: bytes


def default_synthetic_frames() -> tuple[SyntheticFrame, ...]:
    """Three small, non-uniform frames that exercise distinct input values."""
    frames = []
    width, height = 16, 12
    for index in range(3):
        data = bytearray(width * height * 4)
        for y in range(height):
            for x in range(width):
                offset = (y * width + x) * 4
                data[offset:offset + 4] = bytes((
                    (x * 17 + index * 53) & 0xFF,
                    (y * 23 + index * 71) & 0xFF,
                    ((x ^ y) * 29 + index * 37) & 0xFF,
                    255,
                ))
        frames.append(SyntheticFrame(index, width, height, "RGBA8", bytes(data)))
    return tuple(frames)


@dataclass(frozen=True)
class CreateRequest:
    key: CompatibilityKey
    width: int
    height: int
    pixel_format: str = "RGBA8"
    input_source: str = "synthetic"
    desktop_capture: bool = False
    presentation_window: bool = False


@dataclass(frozen=True)
class EvaluateRequest:
    key: CompatibilityKey
    frame: SyntheticFrame
    input_source: str = "synthetic"
    desktop_capture: bool = False
    presentation_window: bool = False


class StageStatus(str, Enum):
    SUCCESS = "success"
    UNSUPPORTED = "unsupported"
    SKIP = "skip"  # kept: the compatibility verdict uses it
    NOT_RUN = "not_run"
    UNKNOWN = "unknown"
    ERROR = "error"
    TIMEOUT = "timeout"
    CRASH = "crash"
    DEVICE_LOST = "device_lost"
    TDR = "tdr"


@dataclass(frozen=True)
class StageOutcome:
    """One runner response. Only Evaluate uses ``output_pixels``."""

    status: StageStatus | str
    output_pixels: bytes | bytearray | memoryview | None = None
    output_width: int = 0
    output_height: int = 0
    pixel_format: str = "RGBA8"

    @classmethod
    def success(cls) -> "StageOutcome":
        return cls(StageStatus.SUCCESS)

    @classmethod
    def frame(cls, frame: SyntheticFrame, pixels: bytes) -> "StageOutcome":
        return cls(
            StageStatus.SUCCESS, pixels, frame.width, frame.height,
            frame.pixel_format,
        )


class SelfTestRunner(Protocol):
    """Backend adapter. It must not capture or create a presentation window."""

    def create(self, request: CreateRequest) -> StageOutcome: ...

    def evaluate(self, request: EvaluateRequest) -> StageOutcome: ...

    def close(self) -> None: ...


class UnsupportedError(RuntimeError):
    """The runtime deterministically rejects this compatibility key."""


class WorkerCrashError(RuntimeError):
    pass


class DeviceLostError(RuntimeError):
    pass


class TdrError(RuntimeError):
    pass


class CompatibilityStatus(str, Enum):
    PASS = "pass"
    UNSUPPORTED = "unsupported"
    QUARANTINED = "quarantined"
    FAILED = "failed"


@dataclass(frozen=True)
class CompatibilityResult:
    key_digest: str
    status: CompatibilityStatus
    stage: str
    passed: int
    attempted: int
    expected: int
    reason: str
    cached: bool = False
    quarantine_until: float | None = None

    @property
    def score(self) -> str:
        return f"{self.passed}/{self.attempted}"

    @property
    def is_pass(self) -> bool:
        return (
            self.status is CompatibilityStatus.PASS
            and self.expected > 0
            and self.attempted == self.expected
            and self.passed == self.attempted
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["score"] = self.score
        return data


class CompatibilityCache:
    """Versioned per-key cache, tolerant of missing or malformed JSON."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        filesystem: FileSystem | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = str(path)
        self.filesystem = filesystem or LocalFileSystem()
        self.clock = clock
        self._lock = threading.RLock()

    def _empty(self) -> dict[str, Any]:
        return {"schema": CACHE_SCHEMA, "entries": {}}

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.filesystem.read_text(self.path))
            if raw.get("schema") != CACHE_SCHEMA or not isinstance(raw.get("entries"), dict):
                return self._empty()
            return raw
        except (OSError, ValueError, TypeError, AttributeError):
            return self._empty()

    def _save(self, document: Mapping[str, Any]) -> None:
        text = json.dumps(
            document, ensure_ascii=False, sort_keys=True, indent=2,
            allow_nan=False,
        ) + "\n"
        self.filesystem.atomic_write_text(self.path, text)

    def get(self, key: CompatibilityKey) -> CompatibilityResult | None:
        digest = key.digest
        with self._lock:
            document = self._load()
            entry = document["entries"].get(digest)
            if not isinstance(entry, dict) or entry.get("key") != key.payload():
                return None
            try:
                result = CompatibilityResult(
                    key_digest=digest,
                    status=CompatibilityStatus(entry["status"]),
                    stage=str(entry["stage"]),
                    passed=int(entry["passed"]),
                    attempted=int(entry["attempted"]),
                    expected=int(entry["expected"]),
                    reason=str(entry["reason"]),
                    cached=True,
                    quarantine_until=(
                        None if entry.get("quarantine_until") is None
                        else float(entry["quarantine_until"])
                    ),
                )
            except (KeyError, TypeError, ValueError):
                return None
            if (
                result.status is CompatibilityStatus.QUARANTINED
                and result.quarantine_until is not None
                and self.clock() >= result.quarantine_until
            ):
                del document["entries"][digest]
                self._save(document)
                return None
            if result.status not in (
                CompatibilityStatus.PASS,
                CompatibilityStatus.UNSUPPORTED,
                CompatibilityStatus.QUARANTINED,
            ):
                return None
            return result

    def put(self, key: CompatibilityKey, result: CompatibilityResult) -> None:
        if result.status not in (
            CompatibilityStatus.PASS,
            CompatibilityStatus.UNSUPPORTED,
            CompatibilityStatus.QUARANTINED,
        ):
            return
        with self._lock:
            document = self._load()
            document["entries"][key.digest] = {
                "key": key.payload(),
                "status": result.status.value,
                "stage": result.stage,
                "passed": result.passed,
                "attempted": result.attempted,
                "expected": result.expected,
                "reason": result.reason,
                "quarantine_until": result.quarantine_until,
                "created_at": float(self.clock()),
            }
            self._save(document)

    def reset(self, key: CompatibilityKey) -> bool:
        """Clear one cached verdict for an explicit manual retry."""
        with self._lock:
            document = self._load()
            removed = document["entries"].pop(key.digest, None) is not None
            # Also repairs a malformed cache when the user explicitly retries.
            self._save(document)
            return removed


_TRANSIENT = {
    StageStatus.TIMEOUT,
    StageStatus.CRASH,
    StageStatus.DEVICE_LOST,
    StageStatus.TDR,
}


def _normalise_status(value: StageStatus | str) -> StageStatus:
    try:
        return StageStatus(str(value.value if isinstance(value, StageStatus) else value))
    except ValueError:
        return StageStatus.UNKNOWN


def _exception_status(exc: BaseException) -> StageStatus:
    if isinstance(exc, UnsupportedError):
        return StageStatus.UNSUPPORTED
    if isinstance(exc, TimeoutError):
        return StageStatus.TIMEOUT
    if isinstance(exc, DeviceLostError):
        return StageStatus.DEVICE_LOST
    if isinstance(exc, TdrError):
        return StageStatus.TDR
    if isinstance(exc, WorkerCrashError):
        return StageStatus.CRASH
    # A runner exception means the isolated worker did not complete its stage.
    # Its text is intentionally discarded: it can contain private paths.
    return StageStatus.CRASH


def _call(call: Callable[[], StageOutcome]) -> StageOutcome:
    try:
        result = call()
        if not isinstance(result, StageOutcome):
            return StageOutcome(StageStatus.UNKNOWN)
        return replace(result, status=_normalise_status(result.status))
    except BaseException as exc:  # runner/process adapters may wrap SEH failures
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return StageOutcome(_exception_status(exc))


def _valid_frame(outcome: StageOutcome, frame: SyntheticFrame) -> bool:
    if _normalise_status(outcome.status) is not StageStatus.SUCCESS:
        return False
    if outcome.pixel_format != frame.pixel_format:
        return False
    if outcome.output_width != frame.width or outcome.output_height != frame.height:
        return False
    if not isinstance(outcome.output_pixels, (bytes, bytearray, memoryview)):
        return False
    return len(outcome.output_pixels) == frame.width * frame.height * 4


class CompatibilityPreflight:
    """Run or reuse an isolated Create/Evaluate compatibility verdict."""

    def __init__(
        self,
        cache: CompatibilityCache,
        runner_factory: Callable[[], SelfTestRunner],
        *,
        clock: Callable[[], float] = time.time,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        frames: Sequence[SyntheticFrame] | None = None,
    ) -> None:
        self.cache = cache
        self.runner_factory = runner_factory
        self.clock = clock
        self.cooldown_seconds = float(cooldown_seconds)
        self.frames = tuple(frames or default_synthetic_frames())
        if not self.frames:
            raise ValueError("at least one synthetic frame is required")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be non-negative")

    def _failure(
        self,
        key: CompatibilityKey,
        status: StageStatus,
        stage: str,
        passed: int,
        attempted: int,
    ) -> CompatibilityResult:
        if status is StageStatus.UNSUPPORTED:
            verdict = CompatibilityStatus.UNSUPPORTED
            until = None
        elif status in _TRANSIENT:
            verdict = CompatibilityStatus.QUARANTINED
            until = float(self.clock()) + self.cooldown_seconds
        else:
            verdict = CompatibilityStatus.FAILED
            until = None
        result = CompatibilityResult(
            key.digest, verdict, stage, passed, attempted, len(self.frames),
            status.value, False, until,
        )
        return result

    def run(self, key: CompatibilityKey, *, force: bool = False) -> CompatibilityResult:
        """Return a cached verdict or execute the isolated self-test."""
        if force:
            self.cache.reset(key)
        else:
            cached = self.cache.get(key)
            if cached is not None:
                return cached

        passed = attempted = 0
        first = self.frames[0]
        try:
            runner = self.runner_factory()
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            result = self._failure(
                key, _exception_status(exc), "create", passed, attempted,
            )
            self.cache.put(key, result)
            return result
        result: CompatibilityResult | None = None
        created = _call(lambda: runner.create(CreateRequest(
            key, first.width, first.height, first.pixel_format,
        )))
        create_status = _normalise_status(created.status)
        if create_status is not StageStatus.SUCCESS:
            result = self._failure(key, create_status, "create", passed, attempted)
        else:
            for frame in self.frames:
                attempted += 1
                outcome = _call(lambda frame=frame: runner.evaluate(
                    EvaluateRequest(key, frame)
                ))
                status = _normalise_status(outcome.status)
                if status is StageStatus.SUCCESS and _valid_frame(outcome, frame):
                    passed += 1
                    continue
                if status is StageStatus.SUCCESS:
                    status = StageStatus.ERROR
                result = self._failure(
                    key, status, f"evaluate[{frame.index}]", passed, attempted,
                )
                break

        if result is None:
            result = CompatibilityResult(
                key.digest, CompatibilityStatus.PASS, "complete",
                passed, attempted, len(self.frames), StageStatus.SUCCESS.value,
            )
            # This constructor path is intentionally guarded even though the
            # loop above already enforces it: future changes cannot turn a
            # partial/empty test into PASS accidentally.
            if not result.is_pass:
                result = self._failure(
                    key, StageStatus.ERROR, "complete", passed, attempted,
                )

        # Never publish PASS/UNSUPPORTED while the isolated process may still
        # be alive.  A close failure is itself a crash at the cleanup stage.
        try:
            runner.close()
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            result = self._failure(
                key, _exception_status(exc), "close", passed, attempted,
            )

        self.cache.put(key, result)
        return result

    def manual_retry(self, key: CompatibilityKey) -> CompatibilityResult:
        """Clear this key's PASS/unsupported/quarantine record and rerun it."""
        self.cache.reset(key)
        return self.run(key)

    def clear_for_retry(self, key: CompatibilityKey) -> bool:
        """Clear this key now; useful when the UI schedules retry separately."""
        return self.cache.reset(key)


__all__ = [
    "CACHE_SCHEMA", "DEFAULT_COOLDOWN_SECONDS", "CompatibilityCache",
    "CompatibilityKey", "CompatibilityPreflight", "CompatibilityResult",
    "CompatibilityStatus", "CreateRequest", "DeviceLostError",
    "EvaluateRequest", "FileSystem", "LocalFileSystem", "SelfTestRunner",
    "StageOutcome", "StageStatus", "SyntheticFrame", "TdrError",
    "UnsupportedError", "WorkerCrashError", "default_synthetic_frames",
    "sha256_file",
]
