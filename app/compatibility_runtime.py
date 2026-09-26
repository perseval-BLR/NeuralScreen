"""Windows integration for the isolated NeuralScreen compatibility preflight.

The policy and cache live in :mod:`compatibility`.  This module is the small
production adapter: it starts the native worker with synthetic pixels, never
opens desktop capture or a presentation window, and turns the result into a
startup decision and a privacy-safe support bundle.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

import numpy as np

import compatibility
from compatibility import (
    CompatibilityCache,
    CompatibilityKey,
    CompatibilityPreflight,
    CompatibilityResult,
    CreateRequest,
    EvaluateRequest,
    StageOutcome,
    StageStatus,
    SyntheticFrame,
)
from diagnostics import DiagnosticBundleRequest, create_diagnostic_bundle
from gpuinfo import probe as gpu_probe
from paths import BASE_DIR, NATIVE_DIR, WORKER_EXE
from pipeline import start_worker
from protocol import (
    CREATE_CATEGORY_NONE, CREATE_CATEGORY_UNSUPPORTED, send_frame,
)
from settings_io import APP_VERSION


CACHE_PATH = BASE_DIR / "compatibility-cache.json"
SUPPORT_DIR = BASE_DIR / "support-bundles"
PREFLIGHT_WIDTH = 640
PREFLIGHT_HEIGHT = 360
CREATE_TIMEOUT_SECONDS = 20.0
EVALUATE_TIMEOUT_SECONDS = 10.0
PROBE_CONTRACT = "canonical-rgba8-rg16f-640x360-3frames-v2-cack"
_FEATURE_NOT_SUPPORTED = 0xBAD00001


def _native_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else NATIVE_DIR / path


def runtime_path() -> Path:
    """The first runtime candidate used by the native worker's resolver."""
    configured = os.environ.get("NS_NR_DLL")
    if configured:
        return _native_path(configured)
    byo = NATIVE_DIR / "libraries" / "nvngx_dlssnr.dll"
    return byo if byo.is_file() else NATIVE_DIR / "nvngx_dlssnr.dll"


def _fingerprint(path: Path) -> dict[str, Any]:
    """Fingerprint an optional route component without persisting its path.

    Goes through the module, not the name imported here: this is the file that
    sha256_file's digest cache is keyed on, and a bound local name would route
    around it and re-read the 165 MB runtime on every key build (audit H2).

    The existence check comes first so an absent optional component (the BYO
    candidate is absent unless the user dropped a DLL there) costs a stat
    instead of a raised FileNotFoundError on every call.
    """
    try:
        if not path.is_file():
            return {"present": False, "sha256": ""}
        return {"present": True, "sha256": compatibility.sha256_file(path)}
    except OSError:
        return {"present": False, "sha256": ""}


def _runtime_route() -> dict[str, Any]:
    """Fingerprint every binary/mode that can redirect native NGX calls."""
    configured = bool(os.environ.get("NS_NR_DLL"))
    byo = NATIVE_DIR / "libraries" / "nvngx_dlssnr.dll"
    bundled = NATIVE_DIR / "nvngx_dlssnr.dll"
    forwarder_value = os.environ.get("NS_FORWARDER")
    forwarder = (
        NATIVE_DIR / "nvngx.dll_ns-forwarder.dll"
        if forwarder_value == "1"
        else _native_path(forwarder_value) if forwarder_value else None
    )
    core_value = os.environ.get("NS_NGX_CORE")
    core = _native_path(core_value) if core_value else None
    return {
        "probe_contract": PROBE_CONTRACT,
        "runtime_source": (
            "configured" if configured else "byo-candidate" if byo.is_file()
            else "bundled"
        ),
        # A BYO file can fail the native NVIDIA-signature/product gate.  The
        # key therefore fingerprints both candidate and fallback; a changed
        # file can never inherit PASS from a different route.
        "byo_candidate": _fingerprint(byo),
        "bundled_fallback": _fingerprint(bundled),
        "forwarder_enabled": forwarder is not None,
        "forwarder": _fingerprint(forwarder) if forwarder is not None else None,
        "via_core": os.environ.get("NS_NGX_VIA_CORE", "").startswith("1"),
        "core_preload": _fingerprint(core) if core is not None else None,
        "windows_build": int(getattr(sys.getwindowsversion(), "build", 0)),
    }


def _selected_gpu(cfg: dict) -> dict[str, Any]:
    """Resolve the configured DXGI adapter using the worker's fallback rule."""
    from capture import list_adapters

    adapters = list_adapters()
    try:
        wanted = int(cfg.get("gpu"))
    except (TypeError, ValueError):
        wanted = None
    selected_index = None
    selected_name = ""
    for index, name in adapters:
        if index == wanted:
            selected_index, selected_name = int(index), str(name)
            break
    if selected_index is None and adapters:
        selected_index, selected_name = int(adapters[0][0]), str(adapters[0][1])
    info = gpu_probe(selected_name or None)
    return {
        "configured_index": wanted,
        "resolved_index": selected_index,
        "name": info.get("name") or selected_name or "unknown",
        "architecture_group": int(info.get("arch_group") or 0),
    }


def build_key(st) -> CompatibilityKey:
    """Build the cache key without opening a capture or creating a window."""
    selected = _selected_gpu(st.cfg)
    # Compatibility is a canonical hardware/runtime probe, not a claim that
    # the user's current 4K/HDR presentation path was exercised.  Display
    # details belong in diagnostics and the support table, not in this key.
    probe_mode = {
        "pixel_format": "RGBA8",
        "motion_format": "RG16F",
        "width": PREFLIGHT_WIDTH,
        "height": PREFLIGHT_HEIGHT,
        "route": _runtime_route(),
    }
    return CompatibilityKey.from_files(
        app_version=APP_VERSION,
        runtime_path=runtime_path(),
        worker_path=WORKER_EXE,
        selected_gpu=selected,
        driver_version=str(getattr(st, "environment", {}).get("driver", "unknown")),
        display_mode=probe_mode,
    )


def production_synthetic_frames(
    width: int = PREFLIGHT_WIDTH,
    height: int = PREFLIGHT_HEIGHT,
) -> tuple[SyntheticFrame, ...]:
    """Three deterministic, non-uniform RGBA frames accepted by the worker."""
    if width < 64 or height < 64:
        raise ValueError("the native worker requires synthetic frames of at least 64x64")
    yy, xx = np.indices((height, width), dtype=np.uint32)
    frames: list[SyntheticFrame] = []
    for index in range(3):
        rgba = np.empty((height, width, 4), dtype=np.uint8)
        rgba[:, :, 0] = (xx * 17 + index * 53) & 0xFF
        rgba[:, :, 1] = (yy * 23 + index * 71) & 0xFF
        rgba[:, :, 2] = ((xx ^ yy) * 29 + index * 37) & 0xFF
        rgba[:, :, 3] = 255
        frames.append(SyntheticFrame(
            index=index,
            width=width,
            height=height,
            pixel_format="RGBA8",
            pixels=rgba.tobytes(),
        ))
    return tuple(frames)


def _failure_from_logs(lines: list[str]) -> StageStatus | None:
    """Classify transient GPU evidence; Create support comes only from CACK."""
    recent = "\n".join(lines[-200:]).casefold()
    if any(token in recent for token in (
        "tdr detected", "dxgi_error_device_hung", "0x887a0006",
    )):
        return StageStatus.TDR
    if any(token in recent for token in (
        "device removed reason", "dxgi_error_device_removed", "0x887a0005",
        "dxgi_error_device_reset", "0x887a0007",
    )):
        return StageStatus.DEVICE_LOST
    return None


class NativeSelfTestRunner:
    """One short-lived native worker fed only deterministic inline frames."""

    def __init__(self, params: dict) -> None:
        self.params = dict(params)
        self.worker = None
        self.logs: list[str] = []
        self.reader = None
        self.stop = None

    def create(self, request: CreateRequest) -> StageOutcome:
        if request.desktop_capture or request.presentation_window:
            return StageOutcome(StageStatus.ERROR)
        try:
            self.worker, self.logs, self.reader, self.stop = start_worker(
                self.params, request.width, request.height, 0, 0, 0, None,
            )
        except (OSError, RuntimeError):
            return StageOutcome(StageStatus.CRASH)

        try:
            ok, ngx_result, category = self.reader.wait_create_ack(
                CREATE_TIMEOUT_SECONDS)
        except TimeoutError:
            return StageOutcome(StageStatus.TIMEOUT)
        except (EOFError, OSError, RuntimeError):
            return StageOutcome(_failure_from_logs(self.logs) or StageStatus.CRASH)
        failure = _failure_from_logs(self.logs)
        if failure is not None:
            return StageOutcome(failure)
        if ok and category == CREATE_CATEGORY_NONE:
            return StageOutcome.success()
        if (not ok and category == CREATE_CATEGORY_UNSUPPORTED
                and ngx_result == _FEATURE_NOT_SUPPORTED):
            return StageOutcome(StageStatus.UNSUPPORTED)
        return StageOutcome(StageStatus.ERROR)

    def evaluate(self, request: EvaluateRequest) -> StageOutcome:
        if request.desktop_capture or request.presentation_window:
            return StageOutcome(StageStatus.ERROR)
        if self.worker is None or self.reader is None or self.worker.poll() is not None:
            return StageOutcome(_failure_from_logs(self.logs) or StageStatus.CRASH)
        frame = request.frame
        rgba = np.frombuffer(frame.pixels, dtype=np.uint8).reshape(
            frame.height, frame.width, 4)
        motion = np.zeros((frame.height, frame.width, 2), dtype=np.float16)
        try:
            send_frame(
                self.worker, frame.index, rgba, motion, frame.index == 0,
                frame.index,
                want_pixels=True,
            )
            pixels = self.reader.recv(frame.index, timeout=EVALUATE_TIMEOUT_SECONDS)
        except TimeoutError:
            return StageOutcome(StageStatus.TIMEOUT)
        except (BrokenPipeError, EOFError, OSError, RuntimeError):
            return StageOutcome(_failure_from_logs(self.logs) or StageStatus.CRASH)

        failure = _failure_from_logs(self.logs)
        if failure is not None:
            return StageOutcome(failure)
        if pixels is None:
            return StageOutcome(StageStatus.UNKNOWN)
        output = np.ascontiguousarray(pixels, dtype=np.uint8)
        return StageOutcome(
            StageStatus.SUCCESS,
            output.tobytes(),
            output.shape[1],
            output.shape[0],
            "RGBA8",
        )

    def close(self) -> None:
        if self.worker is not None:
            try:
                _reap_worker(self.worker, self.stop)
            finally:
                self.worker = None


def _reap_worker(worker, stop) -> None:
    """Close, terminate or kill the probe and always reap its process handle."""
    if stop is not None:
        stop.set()
    try:
        if worker.stdin is not None and not worker.stdin.closed:
            worker.stdin.close()
    except OSError:
        pass
    try:
        worker.wait(timeout=2.0)
        return
    except subprocess.TimeoutExpired:
        worker.terminate()
    try:
        worker.wait(timeout=2.0)
        return
    except subprocess.TimeoutExpired:
        worker.kill()
    worker.wait(timeout=5.0)


def require_pass(st) -> None:
    """Refuse a production worker when the current canonical key has no PASS."""
    result = getattr(st, "compatibility_result", None)
    passed_key = getattr(st, "compatibility_key", None)
    current_key = build_key(st)
    if result is None or not result.is_pass or passed_key is None:
        raise RuntimeError("production worker blocked: compatibility PASS is missing")
    if current_key.digest != passed_key.digest:
        raise RuntimeError("production worker blocked: compatibility key changed")


def run_preflight(st, *, force: bool = False) -> CompatibilityResult:
    """Run or reuse the preflight and retain its exact verdict on ``st``."""
    key = build_key(st)
    service = CompatibilityPreflight(
        CompatibilityCache(CACHE_PATH),
        lambda: NativeSelfTestRunner(st.params),
        frames=production_synthetic_frames(),
    )
    result = service.run(key, force=force)
    st.compatibility_key = key
    st.compatibility_result = result
    print(
        f"[compat] {result.status.value}: {result.passed}/{result.attempted} "
        f"(expected {result.expected}), stage={result.stage}, "
        f"reason={result.reason}, cached={'yes' if result.cached else 'no'}"
    )
    return result


def _diagnostic_markers(st) -> list[str]:
    markers = []
    for line in list(getattr(st, "worker_logs", None) or [])[-200:]:
        lower = line.casefold()
        if any(token in lower for token in (
            "0x", "hresult", "seh", "dred", "device removed", "create failed",
        )):
            markers.append(line)
    return markers[-40:]


def create_support_bundle(st, *, stage: str | None = None) -> Path:
    """Create one sanitized bundle and return its exact published path."""
    result = getattr(st, "compatibility_result", None)
    details: dict[str, Any] = {
        "compatibility": result.to_dict() if result is not None else {"status": "not_run"},
        "worker_running": bool(
            getattr(st, "worker", None) is not None
            and getattr(st.worker, "poll", lambda: 1)() is None
        ),
        "markers": _diagnostic_markers(st),
    }
    failure_stage = stage or (
        f"compatibility.{result.stage}" if result is not None and not result.is_pass
        else "manual"
    )
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = SUPPORT_DIR / f"NeuralScreen-diagnostics-{stamp}.zip"
    serial = 1
    while candidate.exists():
        candidate = SUPPORT_DIR / f"NeuralScreen-diagnostics-{stamp}-{serial}.zip"
        serial += 1
    return create_diagnostic_bundle(candidate, DiagnosticBundleRequest(
        failure_stage=failure_stage,
        failure_details=details,
        app_version=APP_VERSION,
        runtime_path=runtime_path(),
        log_path=BASE_DIR / "NeuralScreen.log",
        settings=settings_snapshot(st),
    ))


def settings_snapshot(st) -> dict:
    """The product settings this session is running with.

    Read from the live config rather than from the file on disk: the file is
    written on a menu change, and a user who changed a setting a second ago
    would otherwise send yesterday's value - which is exactly the question the
    section exists to answer.
    """
    try:
        cfg = getattr(st, "cfg", None)
        return dict(cfg) if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def startup_gate(st) -> bool:
    """Block capture startup until PASS; Retry reruns without app restart."""
    force = False
    while True:
        try:
            result = run_preflight(st, force=force)
        except Exception as exc:
            # Key construction (missing runtime/worker) is itself a closed
            # failure.  Do not open capture just because the test could not run.
            result = None
            st.compatibility_result = None
            print(f"[compat] preflight could not run: {type(exc).__name__}: {exc}")
        if result is not None and result.is_pass:
            return True

        # In the user's language: this is the one message a user sees when
        # the program refuses to start, and it was Russian in every language.
        from i18n import STRINGS
        lang = getattr(st, "lang", None) or (getattr(st, "cfg", None) or {}).get("lang")
        text = STRINGS.get(lang) or STRINGS["en"]
        try:
            bundle = create_support_bundle(st, stage=(
                f"compatibility.{result.stage}" if result is not None
                else "compatibility.setup"
            ))
            bundle_text = str(bundle)
        except Exception as exc:
            print(f"[compat] diagnostic bundle failed: {type(exc).__name__}: {exc}")
            bundle_text = text["compat_bundle_failed"]

        if result is None:
            verdict = text["compat_not_run"]
        else:
            verdict = text["compat_verdict"].format(
                status=result.status.value, stage=result.stage,
                passed=result.passed, attempted=result.attempted,
                expected=result.expected)
        message = text["compat_blocked"].format(verdict=verdict, bundle=bundle_text)
        retry = 4  # IDRETRY
        try:
            answer = ctypes.windll.user32.MessageBoxW(
                None,
                message,
                text["compat_title"],
                0x00000005 | 0x00000010 | 0x00040000,  # RETRY/CANCEL, error, foreground
            )
        except Exception:
            answer = 2  # IDCANCEL
        if answer != retry:
            return False
        force = True


__all__ = [
    "CACHE_PATH", "NativeSelfTestRunner", "SUPPORT_DIR", "build_key",
    "create_support_bundle", "production_synthetic_frames", "run_preflight",
    "require_pass", "runtime_path", "startup_gate",
]
