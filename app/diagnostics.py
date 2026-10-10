"""Privacy-safe, deterministic diagnostic bundles for NeuralScreen.

The module deliberately has no dependency on the UI or the processing
pipeline, so it can still produce a report after either of them fails.  The
future UI integration only needs to construct :class:`DiagnosticBundleRequest`
and call :func:`create_diagnostic_bundle`.

The resulting ZIP always contains exactly two files:

``diagnostics.json``
    Application/runtime identity, graphics environment and failure details.

``log_tail.txt``
    A bounded, scrubbed excerpt of ``NeuralScreen.log``: the head of the
    current session and its tail, and before them, when there is room, the
    tail of what came earlier (``NeuralScreen.log.1`` included).

No environment dump is collected, and of the configuration only an
allow-list of product settings (_SETTINGS_KEYS), each value bounded - no
paths, no presets, no hotkeys. Other programs' window titles are cut out of
the log tail. ZIP metadata and JSON ordering are fixed, making identical
inputs produce identical bytes.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
from typing import Any, Mapping, Sequence
import zipfile


# The install folder (this module lives in app/, one level down). No import of
# paths.py on purpose: this module must still work when the rest cannot load.
BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LOG_BYTES = 64 * 1024
MAX_LOG_BYTES = 256 * 1024
SCHEMA = "neuralscreen.diagnostics/v1"
_REDACTED = "<REDACTED>"
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


@dataclass(frozen=True)
class DiagnosticBundleRequest:
    """Inputs for one diagnostic bundle.

    ``failure_stage`` is required so reports cannot silently lose the most
    useful routing fact.  ``failure_details`` may contain HRESULT, SEH and DRED
    fields.  The module recursively scrubs every supplied string and replaces
    values under secret-looking keys.

    ``system_snapshot`` and ``runtime_signature`` are optional injection
    points for callers that already have authoritative data.  When omitted,
    this module performs best-effort, read-only Windows probes.

    ``sensitive_values`` is for application-specific opaque values which have
    no recognisable secret prefix.  They are removed in addition to usernames,
    home/temp paths, absolute paths and common credential formats.
    """

    failure_stage: str
    failure_details: Mapping[str, Any] = field(default_factory=dict)
    app_version: str | None = None
    commit: str | None = None
    runtime_path: str | os.PathLike[str] | None = None
    log_path: str | os.PathLike[str] | None = None
    system_snapshot: Mapping[str, Any] | None = None
    runtime_signature: Mapping[str, Any] | None = None
    sensitive_values: Sequence[str] = field(default_factory=tuple)
    max_log_bytes: int = DEFAULT_LOG_BYTES
    # The product settings the report was made with. Four support packages
    # (19.09) could not answer "what was the switch set to" - frame generation,
    # the multiplier, the motion backend, the profiles - because the bundle
    # carried no configuration at all, and the evidence table for each of them
    # ends in a list of settings that had to be asked for by hand. Only the
    # product keys go in; every value is scrubbed like the log and the payload
    # is bounded, so this stays a product snapshot rather than a user dump.
    settings: Mapping[str, Any] = field(default_factory=dict)
    # NeuralScreen.log.1, the log a start moved aside. Read only when the
    # current log leaves room: after a crash and a relaunch it is where the
    # session that crashed went (see _log_excerpt).
    previous_log_path: str | os.PathLike[str] | None = None


_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?"
    r"-----END [^-\r\n]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_AUTH_RE = re.compile(
    r"(?i)\b(?P<key>proxy-authorization|authorization)"
    r"(?P<sep>\s*[:=]\s*)(?:bearer|basic)?\s*[^\s,;]+"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<key>[a-z0-9_.-]*"
    r"(?:api[_-]?key|access[_-]?key|client[_-]?secret|secret|token|"
    r"password|passwd|pwd|cookie|session[_-]?id)"
    r"[a-z0-9_.-]*)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
)
_URI_CREDENTIAL_RE = re.compile(
    r"(?i)\b(?P<scheme>[a-z][a-z0-9+.-]*://)"
    r"[^/@\s:]+:[^/@\s]+@"
)
_KNOWN_TOKEN_RES = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
        r"[A-Za-z0-9_-]{8,}\b"
    ),
)
_SECRET_KEY_PARTS = (
    "apikey",
    "accesskey",
    "clientsecret",
    "secret",
    "token",
    "password",
    "passwd",
    "credential",
    "authorization",
    "cookie",
    "sessionid",
    "privatekey",
    "connectionstring",
)

# Quoted paths are removed precisely.  For an unquoted path the rest of that
# line is removed as well: Windows paths may legally contain spaces, so a more
# optimistic boundary can leave the private half of a path in the report.
_QUOTED_PATH_RE = re.compile(
    r"(?P<quote>[\"'])(?P<path>"
    r"(?:[A-Za-z]:[\\/]|\\\\(?!\.\\)|/(?!/))"
    r"[^\"'\r\n]*)(?P=quote)"
)
_WINDOWS_PATH_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:[A-Z]:[\\/]|\\\\(?!\.\\))[^\r\n]*"
)
_POSIX_PATH_RE = re.compile(
    r"(?<![:/A-Za-z0-9_.-])/(?!/)"
    r"(?:[^/\s<>\"'|]+/)+[^\r\n<>\"'|]*"
)
_DISPLAY_DEVICE_RE = re.compile(r"\\\\\.\\DISPLAY\d+", re.IGNORECASE)


def _secret_key(key: object) -> bool:
    normal = re.sub(r"[^a-z0-9]", "", str(key).casefold())
    return any(part in normal for part in _SECRET_KEY_PARTS)


def _privacy_literals(extra: Sequence[str]) -> tuple[list[str], list[str], list[str]]:
    """Return known private paths, usernames and caller-supplied literals."""
    paths: set[str] = set()
    users: set[str] = set()
    custom: set[str] = {str(value) for value in extra if str(value)}

    for name in ("USERPROFILE", "HOME", "TEMP", "TMP", "TMPDIR"):
        value = os.environ.get(name)
        if value:
            paths.add(value)
    try:
        paths.add(str(Path.home()))
    except Exception:
        pass
    try:
        paths.add(tempfile.gettempdir())
    except Exception:
        pass
    for name in ("USERNAME", "USER"):
        value = os.environ.get(name)
        if value:
            users.add(value)
    try:
        users.add(getpass.getuser())
    except Exception:
        pass

    # Match either separator style.  Longest first prevents a parent path
    # from exposing the private suffix of a more specific temp directory.
    path_variants = set()
    for value in paths:
        stripped = value.rstrip("\\/")
        if stripped:
            path_variants.update(
                (stripped, stripped.replace("\\", "/"), stripped.replace("/", "\\"))
            )
    return (
        sorted(path_variants, key=len, reverse=True),
        sorted((u for u in users if u), key=len, reverse=True),
        sorted(custom, key=len, reverse=True),
    )


def _replace_literal(text: str, value: str, replacement: str, *, token: bool) -> str:
    if not value:
        return text
    pattern = re.escape(value)
    if token:
        pattern = rf"(?<![A-Za-z0-9_]){pattern}(?![A-Za-z0-9_])"
    return re.sub(pattern, lambda _match: replacement, text, flags=re.IGNORECASE)


def _redact_absolute_paths(text: str) -> str:
    # Win32 display identifiers look like UNC paths but are useful, stable
    # hardware identities.  Shield and restore them around generic path rules.
    devices: list[str] = []

    def shield(match: re.Match[str]) -> str:
        devices.append(match.group(0))
        return f"__NS_DISPLAY_{len(devices) - 1}__"

    text = _DISPLAY_DEVICE_RE.sub(shield, text)
    text = _QUOTED_PATH_RE.sub(lambda m: m.group("quote") + "<PATH>" + m.group("quote"), text)
    text = _WINDOWS_PATH_RE.sub("<PATH>", text)
    text = _POSIX_PATH_RE.sub("<PATH>", text)
    for index, device in enumerate(devices):
        text = text.replace(f"__NS_DISPLAY_{index}__", device)
    return text


# `title='...'` as display.describe_window writes it (a repr, either quote).
# The z-order guard names every window that took the top, and on a user's
# desktop that is their chat, their mail, their Task Manager - the title of
# another program is theirs, not ours. Only our own window's title stays.
_WINDOW_TITLE_RE = re.compile(
    r"""title=(?P<repr>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")""")
_OWN_TITLES = {"'NeuralScreen'", "''"}
# A file the conversion queue refused is named by the log line; the name is
# the user's (the folder is already cut out as a path).
# The reason is the LAST parenthesis on the line (one word: missing, format,
# queued); a name may hold parentheses of its own - "Anna (wedding).mp4" kept
# "(wedding).mp4" in the bundle while the match stopped at the first one.
_REFUSED_FILE_RE = re.compile(
    r"(not queued for conversion: )(.+)( \([^()\n]*\))[ \t\r]*$", re.MULTILINE)
# The conversion queue names the file on every line of a job's life
# (convert_jobs: "[convert] <name>: started|failed|done|stopped ..."). Only
# the folder used to go, as a path; the name - "Holiday with Anna.mp4" -
# reached the bundle. A Windows file name cannot hold a colon, so the name is
# everything up to the first one. The local log keeps it: it is the user's
# own file, and the line is how they find which job failed.
_CONVERT_FILE_RE = re.compile(
    r"(?P<head>\[convert\] )(?P<name>[^:\r\n]+)"
    r"(?P<verb>: (?:started|failed|done|stopped)\b)(?P<rest>[^\r\n]*)")


def _redact_convert_name(match: "re.Match[str]") -> str:
    # A failure message may repeat the name (a decoder quoting the file it
    # could not open), and the output keeps its stem; both go as well. A
    # name of one or two characters is left in the rest of the line - it
    # would cut letters out of the error text and hide nothing.
    name = match.group("name")
    rest = match.group("rest")
    for part in (name, Path(name).stem):
        if len(part) >= 3:
            rest = rest.replace(part, "<FILE>")
    return match.group("head") + "<FILE>" + match.group("verb") + rest


def _redact_titles(text: str) -> str:
    def shield(match: "re.Match[str]") -> str:
        quoted = match.group("repr")
        if quoted in _OWN_TITLES:
            return match.group(0)
        return f"title=<{max(0, len(quoted) - 2)} chars>"
    text = _WINDOW_TITLE_RE.sub(shield, text)
    text = _CONVERT_FILE_RE.sub(_redact_convert_name, text)
    return _REFUSED_FILE_RE.sub(lambda m: m.group(1) + "<FILE>" + m.group(3), text)


def sanitize_text(text: object, *, sensitive_values: Sequence[str] = ()) -> str:
    """Return text safe to place in a support bundle.

    The scrubber is intentionally conservative: an unquoted absolute path
    consumes the remainder of its line rather than guessing where a path with
    spaces ends.  URLs are preserved, while drive, UNC and POSIX paths are not.
    """
    result = str(text)
    paths, users, custom = _privacy_literals(sensitive_values)

    for value in paths:
        result = _replace_literal(result, value, "<HOME_OR_TEMP>", token=False)
    for value in users:
        # Do not use <USER>: on an account literally named "User" that
        # placeholder redacts itself again and breaks the fail-closed
        # idempotence check.
        result = _replace_literal(result, value, "<ACCOUNT>", token=True)
    for value in custom:
        result = _replace_literal(result, value, _REDACTED, token=False)

    # Titles before the secret rules: a title is a quoted repr, and the
    # assignment rule's unquoted value runs to the next space - it ate the
    # closing quote of "title='reset?token=abc'", the title rule then found
    # no title, and the whole window title went out. "password: x" in a
    # chat's name did the same.
    result = _redact_titles(result)
    result = _PRIVATE_KEY_RE.sub(_REDACTED, result)
    result = _AUTH_RE.sub(
        lambda m: f"{m.group('key')}{m.group('sep')}{_REDACTED}", result
    )
    result = _SECRET_ASSIGNMENT_RE.sub(
        lambda m: f"{m.group('key')}{m.group('sep')}{_REDACTED}", result
    )
    result = _URI_CREDENTIAL_RE.sub(
        lambda m: f"{m.group('scheme')}{_REDACTED}@", result
    )
    for pattern in _KNOWN_TOKEN_RES:
        result = pattern.sub(_REDACTED, result)
    return _redact_absolute_paths(result)


def _sanitize_value(value: Any, sensitive_values: Sequence[str]) -> Any:
    if isinstance(value, Mapping):
        clean = {}
        for key, item in value.items():
            clean_key = sanitize_text(key, sensitive_values=sensitive_values)
            clean[clean_key] = (
                _REDACTED
                if _secret_key(key)
                else _sanitize_value(item, sensitive_values)
            )
        return clean
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item, sensitive_values) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_sanitize_value(item, sensitive_values) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, default=str))
    if isinstance(value, Path):
        return sanitize_text(value, sensitive_values=sensitive_values)
    if isinstance(value, bytes):
        return f"<BYTES:{len(value)}>"
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize_text(value, sensitive_values=sensitive_values)


def _assert_scrubbed(text: str, sensitive_values: Sequence[str]) -> None:
    """Fail closed if another scrub pass can still remove private material."""
    if sanitize_text(text, sensitive_values=sensitive_values) != text:
        raise ValueError("diagnostic privacy check found unsanitized data")


def _assert_report_scrubbed(report: Mapping[str, Any], sensitive_values: Sequence[str]) -> None:
    """Check values before JSON escaping changes Win32 display identifiers."""
    if _sanitize_value(report, sensitive_values) != report:
        raise ValueError("diagnostic privacy check found unsanitized data")


def _read_manifest(base_dir: Path) -> tuple[str | None, str | None]:
    path = base_dir / "VERSION.txt"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    version_match = re.search(r"(?m)^NeuralScreen\s+(\S+)\s*$", text)
    commit_match = re.search(r"(?mi)^commit:\s*([0-9a-f]{7,64})\s*$", text)
    return (
        version_match.group(1) if version_match else None,
        commit_match.group(1).lower() if commit_match else None,
    )


def _version_from_source(base_dir: Path) -> str | None:
    # app/ holds the modules; the root is where an install from before the
    # move kept them.
    source = base_dir / "app" / "settings_io.py"
    if not source.is_file():
        source = base_dir / "settings_io.py"
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return None
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "APP_VERSION" for target in targets):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return None


def discover_application_identity(base_dir: Path = BASE_DIR) -> dict[str, str]:
    """Discover app version and commit without importing application modules."""
    version, commit = _read_manifest(base_dir)
    version = version or _version_from_source(base_dir) or "unknown"
    if not commit:
        try:
            completed = subprocess.run(
                ["git", "-C", str(base_dir), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            candidate = completed.stdout.strip().lower()
            if completed.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", candidate):
                commit = candidate
        except (OSError, subprocess.SubprocessError):
            pass
    return {"name": "NeuralScreen", "version": version, "commit": commit or "unknown"}


def _runtime_candidate(explicit: str | os.PathLike[str] | None) -> tuple[Path, str]:
    if explicit is not None:
        return Path(explicit), "explicit"
    configured = os.environ.get("NS_NR_DLL")
    if configured:
        return Path(configured), "configured"
    byo = BASE_DIR / "native" / "libraries" / "nvngx_dlssnr.dll"
    if byo.is_file():
        return byo, "libraries-candidate"
    return BASE_DIR / "native" / "nvngx_dlssnr.dll", "bundled"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _authenticode_signature(path: Path) -> dict[str, str]:
    if os.name != "nt":
        return {"status": "unavailable"}
    # Prefer PowerShell 7 when present.  A parent PowerShell 7 session may
    # prepend its module folder to PSModulePath; Windows PowerShell then sees
    # an incompatible Microsoft.PowerShell.Security module first.
    powershell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    if not powershell:
        return {"status": "unavailable"}
    script = (
        "& { param([string]$p)"
        "$m=Join-Path $PSHOME 'Modules\\Microsoft.PowerShell.Security\\"
        "Microsoft.PowerShell.Security.psd1';"
        "if(Test-Path -LiteralPath $m){Import-Module $m -ErrorAction Stop};"
        "$s=Get-AuthenticodeSignature -LiteralPath $p;"
        "$c=$s.SignerCertificate;"
        "[ordered]@{status=[string]$s.Status;"
        "subject=$(if($c){[string]$c.Subject}else{''});"
        "issuer=$(if($c){[string]$c.Issuer}else{''});"
        "thumbprint=$(if($c){[string]$c.Thumbprint}else{''})}"
        "|ConvertTo-Json -Compress}"
    )
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script, str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8-sig",
            errors="replace",
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            return {"status": "unavailable"}
        data = json.loads(completed.stdout)
        return {
            "status": str(data.get("status") or "unknown"),
            "subject": str(data.get("subject") or ""),
            "issuer": str(data.get("issuer") or ""),
            "thumbprint": str(data.get("thumbprint") or ""),
        }
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"status": "unavailable"}


def inspect_runtime(
    runtime_path: str | os.PathLike[str] | None = None,
    *,
    signature: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the runtime filename, digest and best-effort signature identity."""
    path, source = _runtime_candidate(runtime_path)
    result: dict[str, Any] = {
        "name": path.name or "nvngx_dlssnr.dll",
        "source": source,
        "size_bytes": None,
        "sha256": "",
        "signature": {"status": "not-checked"},
    }
    try:
        result["size_bytes"] = path.stat().st_size
        result["sha256"] = _sha256(path)
    except OSError as exc:
        result["error"] = type(exc).__name__
        return result
    result["signature"] = dict(signature) if signature is not None else _authenticode_signature(path)
    return result


def _windows_gpus() -> list[dict[str, str]]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []
    base_name = (
        r"SYSTEM\CurrentControlSet\Control\Class"
        r"\{4d36e968-e325-11ce-bfc1-08002be10318}"
    )
    found: list[dict[str, str]] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base_name) as base:
            count = winreg.QueryInfoKey(base)[0]
            for index in range(count):
                sub_name = winreg.EnumKey(base, index)
                if not re.fullmatch(r"\d{4}", sub_name):
                    continue
                try:
                    with winreg.OpenKey(base, sub_name) as key:
                        name = str(winreg.QueryValueEx(key, "DriverDesc")[0]).strip()
                        driver = str(winreg.QueryValueEx(key, "DriverVersion")[0]).strip()
                        try:
                            provider = str(winreg.QueryValueEx(key, "ProviderName")[0]).strip()
                        except OSError:
                            provider = ""
                except OSError:
                    continue
                if name:
                    found.append(
                        {"name": name, "driver_version": driver, "provider": provider}
                    )
    except OSError:
        return []
    unique = {json.dumps(item, sort_keys=True): item for item in found}
    return sorted(unique.values(), key=lambda item: (item["name"], item["driver_version"]))


def _windows_displays() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        class MonitorInfoEx(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32),
            ]

        displays: list[dict[str, Any]] = []
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HANDLE,
            wintypes.HDC,
            ctypes.POINTER(wintypes.RECT),
            wintypes.LPARAM,
        )

        def callback(monitor, _dc, _rect, _data):
            info = MonitorInfoEx()
            info.cbSize = ctypes.sizeof(info)
            # c_void_p: a bare int handle at or above 2**31 raised
            # ArgumentError inside this callback and ended the enumeration.
            if ctypes.windll.user32.GetMonitorInfoW(ctypes.c_void_p(monitor),
                                                    ctypes.byref(info)):
                rect = info.rcMonitor
                displays.append(
                    {
                        "name": info.szDevice,
                        "left": int(rect.left),
                        "top": int(rect.top),
                        "width": int(rect.right - rect.left),
                        "height": int(rect.bottom - rect.top),
                        "primary": bool(info.dwFlags & 1),
                    }
                )
            return True

        callback_ref = callback_type(callback)
        ctypes.windll.user32.EnumDisplayMonitors(0, 0, callback_ref, 0)
        return sorted(displays, key=lambda item: (item["left"], item["top"], item["name"]))
    except Exception:
        return []


def collect_system_snapshot() -> dict[str, Any]:
    """Collect non-identifying OS, GPU/driver and display information."""
    return {
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        },
        "gpus": _windows_gpus(),
        "displays": _windows_displays(),
    }


# The product keys a report needs, in the order the panel shows them. Anything
# outside this list is dropped rather than trusted: `hotkeys` is a user's
# binding choices and `screenshot_dir` / `recording_dir` are paths - neither
# belongs in a bundle even scrubbed, and a product snapshot that quietly grew
# a personal field would be a privacy bug, not a feature.
_SETTINGS_KEYS = (
    "profile", "style", "auto_mask", "ui_correction",
    "intensity", "local_tone", "local_structure", "skin_structure",
    "work_scale", "nr_small", "motion_backend", "flow_preset",
    "frame_generation", "frame_multiplier", "frame_limit_mode",
    "frame_limit_custom", "hdr", "spout",
    "monitor", "gpu", "gpu_no_nr", "theme", "lang",
    "nr_dll",
    # Everything below shipped AFTER this list was written, and every one of
    # them was invisible in a report until 2.0.1: a package from a 2.0 user
    # could not say whether the cascade was running, where the on-screen
    # counter was, or how big the panel had been made. The section exists to
    # answer "how was the program configured" and had quietly stopped
    # answering it for anything new (found in a reporter's package, #96).
    "nr_direct", "nr_passes", "residual_strength", "fps_overlay",
    # What passes 2..N use, when they have their own set. Without it a report
    # from a two-pass session cannot say whether the cascade was running one
    # set or two - and that is exactly the difference between "the second pass
    # repeats the first" and "the second pass is doing something".
    "nr_pass_params",
    "tray_on_minimise", "tray_on_close",
    # #137: whether the app asked the OS for full speed while its window is
    # hidden. A report that says "it is slow when minimised" is unanswerable
    # without knowing this switch and what the read-back said.
    "keep_speed_when_hidden",
    "hotkeys_enabled",
    "menu_scale", "menu_scale_auto",
    # Mini mode and the rows it keeps: a report from a shortened
    # panel would otherwise show a page the reporter never saw.
    "menu_mini", "menu_mini_rows",
    # The four switches that decide WHICH pipeline ran. Without them a report
    # is ambiguous about the code path it came from, and `worker_present` in
    # particular decides whether the picture is the worker's own window or
    # ours - which is the difference between two entirely different classes
    # of overlay bug.
    "worker_present", "capture_in_worker", "pixels_in_shm", "motion_on_gpu",
    "fullscreen", "warmup", "split", "record_audio",
    "rec_indicator", "gpu_record", "open_menu_on_start",
    "screenshot_format", "screenshot_mode",
    # The conversion page's output choices: a report about a conversion
    # that came out silent or in the wrong codec starts here. The folder
    # itself is a path and stays out, like the other two.
    "convert_dest", "convert_codec", "convert_quality",
    "convert_image_format", "convert_audio",
)
_MAX_SETTINGS_VALUE = 120


#: The only keys whose value may be a list of numbers / a flat mapping.
_LIST_SETTINGS = frozenset({"gpu_no_nr"})
_MAPPING_SETTINGS = frozenset({"nr_pass_params"})


def _bounded_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    """The product settings, keyed by an allow-list and bounded per value.

    Scalars, and two bounded shapes: a short list of numbers (gpu_no_nr) and
    a small flat mapping of scalars (nr_pass_params). Both are allow-listed on
    purpose, and both used to be dropped by a scalar-only rule - the report
    could not say what passes 2..N ran with, which is the reason the key is on
    the list. Anything larger or nested is still left out: the section is a
    summary of how the program was configured, not a copy of the file.
    """
    def scalar(value) -> bool:
        return (isinstance(value, (bool, int, float))
                or (isinstance(value, str) and len(value) <= _MAX_SETTINGS_VALUE))

    result: dict[str, Any] = {}
    for key in _SETTINGS_KEYS:
        if key not in settings:
            continue
        value = settings[key]
        if scalar(value):
            result[key] = value
        elif (key in _LIST_SETTINGS and isinstance(value, (list, tuple))
              and len(value) <= 16
              and all(isinstance(item, (int, float)) and not isinstance(item, bool)
                      for item in value)):
            result[key] = list(value)
        elif (key in _MAPPING_SETTINGS and isinstance(value, Mapping)
              and len(value) <= 16
              and all(isinstance(k, str) and len(k) <= 40 and scalar(v)
                      for k, v in value.items())):
            result[key] = {k: value[k] for k in sorted(value)}
    return result


# --- The log excerpt -------------------------------------------------------
# The bundle used to carry the last max_log_bytes of NeuralScreen.log and
# nothing else, and that lost the lines a report is read for twice over:
#
# * NS_PHASE=1 writes a [phase]/[pw] line for every frame. A bundle from such
#   a session held about 1.3 s of profiler output and not one line of the
#   startup - no [env] header, no driver, no [compat] verdict, no adapter.
# * a crash and a relaunch: the relaunch moves an oversized log aside to
#   NeuralScreen.log.1, and the session that crashed is no longer in the file
#   the bundle read.
#
# So the excerpt is built from parts, each cut on whole lines and each
# scrubbed: the head of the current session (from its "[env] NeuralScreen"
# header), the session's tail with the profiler lines held to a share of it,
# and - in whatever room that leaves - what came before the session: the
# rest of this file, then the tail of NeuralScreen.log.1.
_SESSION_MARK = b"[env] NeuralScreen "
_PROFILER_TAGS = (b"[phase]", b"[pw]")
#: How far back the session header is looked for, and how much of the end of
#: a file a tail is chosen from. Both only bound the reading: what goes into
#: the bundle is bounded by max_log_bytes.
_SESSION_SCAN_BYTES = 64 * 1024 * 1024
_TAIL_SCAN_BYTES = 4 * 1024 * 1024
#: The session head takes at most this share of the budget; profiler lines
#: are held to this share of a tail when the other lines need the room.
_HEAD_SHARE = 4
_PROFILER_SHARE = 4


def _read_range(handle, start: int, end: int) -> bytes:
    handle.seek(start)
    return handle.read(max(0, end - start))


def _session_start(handle, size: int) -> int | None:
    """Offset of the line that opens the last session in the file, or None."""
    chunk = 1024 * 1024
    end = size
    while end > 0 and size - end < _SESSION_SCAN_BYTES:
        start = max(0, end - chunk)
        # Overlap into the chunk after this one, so a header split across
        # the boundary is still found.
        data = _read_range(handle, start, min(size, end + len(_SESSION_MARK)))
        found = data.rfind(_SESSION_MARK)
        if found >= 0:
            mark = start + found
            lead_from = max(0, mark - 256)
            newline = _read_range(handle, lead_from, mark).rfind(b"\n")
            if newline >= 0:
                return lead_from + newline + 1
            return 0 if lead_from == 0 else mark
        end = start
    return None


def _is_profiler(line: bytes) -> bool:
    return any(tag in line for tag in _PROFILER_TAGS)


def _select_tail(raw: bytes, budget: int, *, aligned: bool) -> tuple[bytes, bool, int]:
    """The newest whole lines of raw within budget bytes.

    ``aligned`` says whether raw starts on a line; when it does not, the
    partial first line is dropped - the scrubber's rules are anchored on the
    start of a title or a path, and the second half of one is not
    recognisable. When not everything fits, profiler lines get what the
    other lines leave of the budget, but never less than a share of it - so
    they cannot crowd the rest out, and do not leave the bundle half empty
    either. Returns the lines, whether anything was left out, and how many
    profiler lines were.
    """
    lines = raw.splitlines(keepends=True)
    if lines and not aligned:
        lines = lines[1:]
    if sum(len(line) for line in lines) <= budget:
        return b"".join(lines), False, 0
    others = sum(len(line) for line in lines if not _is_profiler(line))
    profiler_cap = max(budget // _PROFILER_SHARE, budget - others)
    keep: list[bytes] = []
    used = profiler = kept_profiler = 0
    for line in reversed(lines):
        if used + len(line) > budget:
            break
        if _is_profiler(line):
            if profiler + len(line) > profiler_cap:
                continue
            profiler += len(line)
            kept_profiler += 1
        keep.append(line)
        used += len(line)
    keep.reverse()
    profiler_out = sum(1 for line in lines if _is_profiler(line)) - kept_profiler
    return b"".join(keep), True, profiler_out


def _fit_lines(text: str, limit: int, *, newest: bool) -> str:
    """Whole lines of an already scrubbed text within limit bytes.

    Scrubbing can make a line longer (<HOME_OR_TEMP> for a short TEMP), so a
    part chosen to fit can come out over its budget; its oldest lines go
    then - or its newest, for the session head (``newest=False``).
    """
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    if limit <= 0:
        return ""
    if newest:
        cut = raw[-limit:]
        if raw[-limit - 1:-limit] != b"\n":
            newline = cut.find(b"\n")
            cut = cut[newline + 1:] if newline >= 0 else b""
    else:
        cut = raw[:limit]
        newline = cut.rfind(b"\n")
        cut = cut[:newline + 1] if newline >= 0 else b""
    return cut.decode("utf-8", errors="ignore")


def _note(text: str) -> str:
    return f"[bundle] --- {text} ---\n"


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def _log_excerpt(
    path: Path,
    previous: Path | None,
    limit: int,
    sensitive_values: Sequence[str],
) -> tuple[bytes, dict[str, Any]]:
    """The scrubbed log excerpt of a bundle, and its metadata (see above)."""
    metadata: dict[str, Any] = {
        "included": False, "truncated": False, "bytes": 0,
        "session_head": False, "previous_log": False,
    }

    def scrub(raw: bytes) -> str:
        return sanitize_text(raw.decode("utf-8", errors="replace"),
                             sensitive_values=sensitive_values)

    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            head_start = _session_start(handle, size)
            session_from = head_start if head_start is not None else 0
            head_raw = b""
            if head_start is not None:
                head_raw = _read_range(
                    handle, head_start,
                    min(size, head_start + limit // _HEAD_SHARE))
                if head_start + len(head_raw) < size:
                    newline = head_raw.rfind(b"\n")
                    head_raw = head_raw[:newline + 1] if newline >= 0 else b""
            rest_from = session_from + len(head_raw)
            tail_from = max(rest_from, size - _TAIL_SCAN_BYTES)
            tail_raw = _read_range(handle, tail_from, size)
            # This file before the session: only as much as a budget can use.
            older_from = max(0, session_from - limit)
            older_raw = _read_range(handle, older_from, session_from)
    except OSError:
        return b"", metadata
    metadata["included"] = True
    truncated = False

    # The session: its head, then its tail.
    scrubbed = scrub(head_raw)
    head = _fit_lines(scrubbed, limit // _HEAD_SHARE, newest=False)
    metadata["session_head"] = bool(head)
    truncated |= head != scrubbed
    gap_note = ""
    if tail_from > rest_from:
        truncated = True
        if head:
            gap_note = _note("the middle of this session is left out here")
    budget = limit - _size(head) - _size(gap_note)
    aligned = tail_from == rest_from
    chosen, left_out, profiler_out = _select_tail(tail_raw, budget, aligned=aligned)
    if profiler_out:
        # Again, with room for the line that says so.
        reserve = _size(_note("0000000 profiler lines ([phase]/[pw]) are "
                              "left out of what follows"))
        chosen, left_out, profiler_out = _select_tail(
            tail_raw, budget - reserve, aligned=aligned)
    scrubbed = scrub(chosen)
    if profiler_out:
        scrubbed = _note(f"{profiler_out} profiler lines ([phase]/[pw]) are "
                         f"left out of what follows") + scrubbed
    tail = _fit_lines(scrubbed, budget, newest=True)
    truncated |= left_out or tail != scrubbed
    session = head + gap_note + tail

    # Before the session, newest first and only in the room it left: the
    # rest of this file, then the previous one.
    room = limit - _size(session)
    older = ""
    if session_from > 0:
        chosen, left_out, _ = _select_tail(
            older_raw, room, aligned=(older_from == 0))
        scrubbed = scrub(chosen)
        older = _fit_lines(scrubbed, room, newest=True)
        truncated |= left_out or older_from > 0 or older != scrubbed
        room -= _size(older)
    if previous is not None and room >= limit // _HEAD_SHARE:
        before_note = _note("NeuralScreen.log.1, the log before this one")
        after_note = _note("NeuralScreen.log")
        space = room - _size(before_note) - _size(after_note)
        previous_from = 0
        try:
            # Usually absent: only a start that found an oversized log made it.
            previous_size = previous.stat().st_size
            previous_from = max(0, previous_size - _TAIL_SCAN_BYTES)
            with previous.open("rb") as handle:
                previous_raw = _read_range(handle, previous_from, previous_size)
        except OSError:
            previous_raw = b""
        chosen, _, _ = _select_tail(
            previous_raw, space, aligned=(previous_from == 0))
        part = _fit_lines(scrub(chosen), space, newest=True)
        if part:
            older = before_note + part + after_note + older
            metadata["previous_log"] = True

    text = older + session
    data = _fit_lines(text, limit, newest=True).encode("utf-8")
    metadata["truncated"] = truncated or len(data) < _size(text)
    metadata["bytes"] = len(data)
    return data, metadata


def _json_bytes(report: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _write_zip_entry(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 0
    info.external_attr = 0o600 << 16
    archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def _atomic_zip(destination: Path, report: bytes, log_tail: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w+b") as handle:
            with zipfile.ZipFile(handle, mode="w") as archive:
                _write_zip_entry(archive, "diagnostics.json", report)
                _write_zip_entry(archive, "log_tail.txt", log_tail)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        if os.name != "nt":
            try:
                directory_fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def create_diagnostic_bundle(
    destination: str | os.PathLike[str], request: DiagnosticBundleRequest
) -> Path:
    """Build and atomically publish a sanitized diagnostic ZIP.

    ``destination`` is the final ``.zip`` path.  The previous file, if any,
    remains intact unless a complete new archive has been flushed and closed.
    No UI or application state is mutated.
    """
    if not isinstance(request, DiagnosticBundleRequest):
        raise TypeError("request must be DiagnosticBundleRequest")
    if not isinstance(request.failure_stage, str) or not request.failure_stage.strip():
        raise ValueError("failure_stage must be a non-empty string")
    if not 1 <= request.max_log_bytes <= MAX_LOG_BYTES:
        raise ValueError(f"max_log_bytes must be between 1 and {MAX_LOG_BYTES}")

    target = Path(destination)
    if target.suffix.casefold() != ".zip":
        raise ValueError("diagnostic destination must end in .zip")

    identity = discover_application_identity()
    if request.app_version is not None:
        identity["version"] = request.app_version
    if request.commit is not None:
        identity["commit"] = request.commit

    runtime = inspect_runtime(request.runtime_path, signature=request.runtime_signature)
    system = (
        dict(request.system_snapshot)
        if request.system_snapshot is not None
        else collect_system_snapshot()
    )
    log_path = Path(request.log_path) if request.log_path is not None else BASE_DIR / "NeuralScreen.log"
    previous_log = (Path(request.previous_log_path)
                    if request.previous_log_path is not None else None)
    safe_log, log_metadata = _log_excerpt(
        log_path, previous_log, request.max_log_bytes, request.sensitive_values)

    report = _sanitize_value(
        {
            "schema": SCHEMA,
            "application": identity,
            "runtime": runtime,
            "graphics": {
                "gpus": system.get("gpus", []),
                "displays": system.get("displays", []),
            },
            "system": system.get("os", {}),
            "failure": {
                "stage": request.failure_stage.strip(),
                "details": dict(request.failure_details),
            },
            "settings": _bounded_settings(request.settings),
            "log": log_metadata,
        },
        request.sensitive_values,
    )
    report_bytes = _json_bytes(report)

    # A second pass is the publication gate: if sanitization is not
    # idempotent, no archive replaces the existing destination.
    _assert_report_scrubbed(report, request.sensitive_values)
    _assert_scrubbed(safe_log.decode("utf-8", errors="strict"), request.sensitive_values)
    _atomic_zip(target, report_bytes, safe_log)
    return target


__all__ = [
    "DEFAULT_LOG_BYTES",
    "MAX_LOG_BYTES",
    "DiagnosticBundleRequest",
    "collect_system_snapshot",
    "create_diagnostic_bundle",
    "discover_application_identity",
    "inspect_runtime",
    "sanitize_text",
]
