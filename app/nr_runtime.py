"""Which NR runtime the worker will load, and whether it has model presets (#179).

The NR preset switch (Auto / A / B) only means something on a runtime with two
models: 310.9.16 carries A (the previous model) and B (the new, smaller one);
the bundled 310.8 has one and ignores the hint. Shown regardless, the switch
read as broken - "the same fps with model A and model B" (#179). The version
comes from the DLL's version resource, read without loading the DLL.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path

#: The first runtime with two models (A and B) behind Hint.Render.Preset.
PRESETS_SINCE = (310, 9, 16)

_cache: dict = {}


class _FixedFileInfo(ctypes.Structure):
    _fields_ = [(name, wintypes.DWORD) for name in (
        "signature", "struc_version", "file_ms", "file_ls", "product_ms", "product_ls",
        "flags_mask", "flags", "os", "type", "subtype", "date_ms", "date_ls")]


def file_version(path: Path) -> tuple | None:
    """The file version (a, b, c, d) of a DLL, or None when it has none."""
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key in _cache:
        return _cache[key]
    version = None
    try:
        api = ctypes.WinDLL("version")
        api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        api.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                            ctypes.c_void_p]
        api.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                       ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(wintypes.UINT)]
        size = api.GetFileVersionInfoSizeW(str(path), None)
        if size:
            buf = ctypes.create_string_buffer(size)
            if api.GetFileVersionInfoW(str(path), 0, size, buf):
                ptr, length = ctypes.c_void_p(), wintypes.UINT()
                if api.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)) \
                        and ptr.value:
                    fi = ctypes.cast(ptr, ctypes.POINTER(_FixedFileInfo)).contents
                    version = (fi.file_ms >> 16, fi.file_ms & 0xFFFF,
                               fi.file_ls >> 16, fi.file_ls & 0xFFFF)
    except (OSError, AttributeError):
        version = None
    _cache[key] = version
    return version


def has_presets(version: tuple | None) -> bool:
    """Whether a runtime of this version answers the A / B preset hint."""
    return version is not None and tuple(version[:3]) >= PRESETS_SINCE


def current() -> tuple[str, bool]:
    """The runtime the worker will load: its version as text ("" when
    unknown) and whether it has presets."""
    try:
        from compatibility_runtime import runtime_path
        version = file_version(runtime_path())
    except Exception:
        version = None
    text = ".".join(str(v) for v in version) if version else ""
    return text, has_presets(version)
