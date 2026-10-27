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


#: Where the worker looks for a user's runtime, relative to the program folder.
BYO_RELATIVE = Path("native") / "libraries" / "nvngx_dlssnr.dll"


def misplaced(base: Path) -> list[str]:
    """NR runtimes a user put where the worker does not look (#182, #185).

    The worker takes exactly native\\libraries\\nvngx_dlssnr.dll and says
    nothing when it is not there, so a runtime one folder off, or under
    another name, ran the bundled 310.8 with no word about why - twice from
    the same user. Looked for: any file with "dlssnr" in its name in
    native\\libraries\\ (and one folder below it - an extracted archive),
    in a libraries\\ folder next to the program, and in the program folder
    itself; plus a second DLL beside the bundled one in native\\. Empty
    when the runtime is where it belongs. Paths relative to `base`.
    """
    base = Path(base)
    if (base / BYO_RELATIVE).is_file():
        return []
    found = []

    def look(folder: Path, depth: int = 0) -> None:
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.is_file() and "dlssnr" in entry.name.lower():
                if entry == base / "native" / "nvngx_dlssnr.dll":
                    continue                    # the bundled runtime
                found.append(str(entry.relative_to(base)))
            elif entry.is_dir() and depth > 0:
                look(entry, depth - 1)

    look(base / "native" / "libraries", depth=1)
    look(base / "libraries", depth=1)
    look(base)
    look(base / "native")
    return found


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
