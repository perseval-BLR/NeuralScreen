"""Native file dialogs and the screenshot writer.

Leaf Windows code: it takes a window handle, shows a system dialog and hands
back a path. Nothing here knows about the pipeline, which is why it could
leave main() - and why the screenshot writer belongs here too rather than
being copied into a test.

Both dialogs are the classic Win32 ones (GetSaveFileNameW,
SHBrowseForFolderW). They block the thread that calls them, so the caller
runs them off the main loop - the overlay must keep drawing while a dialog
is open.
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path


class _OPENFILENAME(ctypes.Structure):
    _fields_ = [
        ("lStructSize", wintypes.DWORD),
        ("hwndOwner", wintypes.HWND),
        ("hInstance", wintypes.HINSTANCE),
        ("lpstrFilter", wintypes.LPCWSTR),
        ("lpstrCustomFilter", wintypes.LPWSTR),
        ("nMaxCustFilter", wintypes.DWORD),
        ("nFilterIndex", wintypes.DWORD),
        ("lpstrFile", wintypes.LPWSTR),
        ("nMaxFile", wintypes.DWORD),
        ("lpstrFileTitle", wintypes.LPWSTR),
        ("nMaxFileTitle", wintypes.DWORD),
        ("lpstrInitialDir", wintypes.LPCWSTR),
        ("lpstrTitle", wintypes.LPCWSTR),
        ("Flags", wintypes.DWORD),
        ("nFileOffset", wintypes.WORD),
        ("nFileExtension", wintypes.WORD),
        ("lpstrDefExt", wintypes.LPCWSTR),
        ("lCustData", wintypes.LPARAM),
        ("lpfnHook", wintypes.LPVOID),
        ("lpTemplateName", wintypes.LPCWSTR),
        ("pvReserved", wintypes.LPVOID),
        ("dwReserved", wintypes.DWORD),
        ("FlagsEx", wintypes.DWORD),
    ]


class _BROWSEINFO(ctypes.Structure):
    _fields_ = [
        ("hwndOwner", wintypes.HWND),
        ("pidlRoot", wintypes.LPVOID),
        ("pszDisplayName", wintypes.LPWSTR),
        ("lpszTitle", wintypes.LPCWSTR),
        ("ulFlags", wintypes.UINT),
        ("lpfn", wintypes.LPVOID),
        ("lParam", wintypes.LPARAM),
        ("iImage", ctypes.c_int),
    ]


def _save_dialog_struct(parent_hwnd: int, default_name: str,
                        initial_dir: str | None):
    """The OPENFILENAME for "Save as", and the buffer the path comes back in.

    Separate from the call because this is the part that was broken and
    nobody saw it: `ofn.lpstrFile = buf` assigns a c_wchar array to an
    LPWSTR field, and ctypes refuses - "incompatible types,
    c_wchar_Array_1024 instance instead of c_wchar_p instance". The
    TypeError was caught by the wrapper below, which quietly fell back to
    the screenshots folder, so the dialog never opened for anyone and the
    only trace was one line in the log. The array has to be cast to the
    pointer type. As a function it can be tested without a modal dialog
    on screen (tests/test_save_dialog.py).
    """
    buf = ctypes.create_unicode_buffer(1024)
    buf.value = default_name
    ofn = _OPENFILENAME()
    ofn.lStructSize = ctypes.sizeof(_OPENFILENAME)
    ofn.hwndOwner = parent_hwnd or None
    ofn.lpstrFilter = ("JPEG image (*.jpg)\0*.jpg\0PNG image (*.png)\0"
                       "*.png\0All files (*.*)\0*.*\0")
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = 1024
    png_default = Path(default_name).suffix.lower() == ".png"
    ofn.nFilterIndex = 2 if png_default else 1
    ofn.lpstrDefExt = "png" if png_default else "jpg"
    ofn.lpstrInitialDir = initial_dir or None
    # OFN_OVERWRITEPROMPT | OFN_PATHMUSTEXIST
    ofn.Flags = 0x00000002 | 0x00000008
    return ofn, buf


def ask_save_path(parent_hwnd: int, default_name: str,
                  initial_dir: str | None = None,
                  fallback_dir: Path | None = None) -> Path | None:
    """The native "Save as" dialog. The chosen path, or None on cancel.

    The JPEG filter is the default and the extension is appended when the
    user leaves it out. initial_dir is the folder the dialog opens in (the
    configured screenshot folder, if any).

    If the dialog itself cannot be shown, a screenshot must not be lost: the
    answer is then a timestamped name inside fallback_dir.
    """
    try:
        ofn, buf = _save_dialog_struct(parent_hwnd, default_name, initial_dir)
        if not ctypes.windll.comdlg32.GetSaveFileNameW(ctypes.byref(ofn)):
            return None
        path = Path(buf.value.strip())
        if path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            # APPENDED, not replaced: a name with a dot in it ("shot
            # 06.10.2026") has the "suffix" .2026 - the dialog adds its
            # default extension only to a name with none, with_suffix()
            # would cut the date to "shot 06.10.jpg", and the writer
            # refused .2026 after the frozen frame was already let go.
            path = path.with_name(path.name + (Path(default_name).suffix
                                               or ".jpg"))
        return path
    except Exception as exc:
        print(f"[dialogs] save dialog unavailable ({exc}) - "
              f"the screenshot goes to the fallback folder", file=sys.stderr)
        if fallback_dir is None:
            return None
        fallback_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        stamp = f"{stamp}-{time.time() % 1 * 1000:03.0f}"
        suffix = Path(default_name).suffix.lower()
        if suffix not in (".jpg", ".jpeg", ".png"):
            suffix = ".jpg"
        return fallback_dir / f"neuralscreen-{stamp}{suffix}"


def media_filter() -> str:
    """The Win32 filter for the convert picker, as its own value.

    A function rather than a literal inside the struct builder because this
    is the part worth testing and the struct is not: `ofn.lpstrFilter` reads
    back through ctypes as a plain str truncated at the first NUL, so a test
    that asked the STRUCT what it offers could only ever see the heading.

    The shape is Win32's: label, NUL, pattern, NUL, ... terminated by two.
    The patterns come from media_convert, so the picker cannot offer a file
    the converter would then refuse.
    """
    from media_convert import IMAGE_SUFFIXES, VIDEO_SUFFIXES
    nul = chr(0)
    images = ";".join(f"*{x}" for x in IMAGE_SUFFIXES)
    videos = ";".join(f"*{x}" for x in VIDEO_SUFFIXES)
    return (f"Images and video{nul}{images};{videos}{nul}"
            f"Images{nul}{images}{nul}"
            f"Video{nul}{videos}{nul}"
            f"All files (*.*){nul}*.*{nul}")


#: OFN_ALLOWMULTISELECT | OFN_EXPLORER: several files in one pick, answered
#: in the Explorer-style shape (folder, NUL, name, NUL, ..., NUL NUL).
OFN_MULTI = 0x00000200 | 0x00080000

#: Room for the names of a multi-file pick. Every chosen name is written into
#: this one buffer; at 1024 characters - the single-file size - a pick of
#: twenty long names in a deep folder fails with FNERR_BUFFERTOOSMALL, which
#: looks exactly like a cancel.
OPEN_BUFFER_CHARS = 65536


def _open_dialog_struct(parent_hwnd: int, initial_dir: str | None,
                        title: str | None = None, multi: bool = False):
    """The OPENFILENAME for "Open", and the buffer the path comes back in.

    Split from the call for the same reason as the save one: a modal dialog
    cannot be driven from a test, but the struct it is handed can be, and
    the struct is where the bug was last time - `lpstrFile = buf` raises
    inside ctypes unless the array is cast to the pointer type, and the
    wrapper's own except would have swallowed it.

    The filter lists the formats media_convert will actually open, so the
    picker cannot offer a file the converter then refuses.
    """
    buf = ctypes.create_unicode_buffer(OPEN_BUFFER_CHARS if multi else 1024)
    ofn = _OPENFILENAME()
    ofn.lStructSize = ctypes.sizeof(_OPENFILENAME)
    ofn.hwndOwner = parent_hwnd or None
    # The separator is chr(0), not a "\0" escape: this filter is built
    # by a patch step and a literal NUL in the source file is a
    # SyntaxError that only shows up at import time.
    ofn.lpstrFilter = media_filter()
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = len(buf)
    ofn.nFilterIndex = 1
    ofn.lpstrInitialDir = initial_dir or None
    ofn.lpstrTitle = title or None
    # OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR. The last one
    # matters here and not in the save dialog: the worker is started with
    # cwd=native/, and a dialog that quietly changes the process directory
    # would break the next worker launch rather than this conversion.
    ofn.Flags = (0x00001000 | 0x00000800 | 0x00000008
                 | (OFN_MULTI if multi else 0))
    return ofn, buf


def split_multiselect(raw: str) -> list[Path]:
    """The files in a multi-select answer, as full paths.

    Two shapes come back and both are ordinary: ONE file is its full path
    alone; SEVERAL are the folder first and then each bare name, NUL between
    them and two NULs at the end. Reading up to the first NUL - which is what
    `buf.value` does - turns a pick of five files into the folder they were
    in.
    """
    end = raw.find("\0\0")
    body = raw if end < 0 else raw[:end]
    parts = [part for part in body.split("\0") if part]
    if not parts:
        return []
    if len(parts) == 1:
        return [Path(parts[0])]
    folder = Path(parts[0])
    return [folder / name for name in parts[1:]]


def ask_open_paths(parent_hwnd: int, initial_dir: str | None = None,
                   title: str | None = None) -> list[Path]:
    """The native "Open" dialog with several files allowed. [] on cancel.

    The dialog remembers where the user last picked from by itself: Windows
    returns to the most recent folder whenever it is handed the same initial
    folder as the first time, so the caller passes a constant one and the
    second visit opens where the first one ended.
    """
    try:
        ofn, buf = _open_dialog_struct(parent_hwnd, initial_dir, title,
                                       multi=True)
        if not ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
            error = ctypes.windll.comdlg32.CommDlgExtendedError()
            if error:
                # 0 is a plain cancel. Anything else would look exactly like
                # one - FNERR_BUFFERTOOSMALL (0x3003) above all.
                print(f"[dialogs] open dialog failed (CommDlgExtendedError "
                      f"0x{error:04X})", file=sys.stderr)
            return []
        return split_multiselect(buf[:])
    except Exception as exc:
        print(f"[dialogs] open dialog unavailable ({exc})", file=sys.stderr)
        return []


def pick_directory(parent_hwnd: int, title: str) -> Path | None:
    """The classic folder picker. The chosen folder, or None on cancel.

    SHBrowseForFolder needs COM on the calling thread, and the caller runs
    this on a thread of its own (the dialog blocks), so the apartment is
    initialised and torn down here rather than assumed.
    """
    try:
        ole32 = ctypes.WinDLL("ole32")
        ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        try:
            shell32 = ctypes.WinDLL("shell32")
            shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(_BROWSEINFO)]
            shell32.SHBrowseForFolderW.restype = wintypes.LPVOID
            shell32.SHGetPathFromIDListW.argtypes = [wintypes.LPVOID,
                                                     wintypes.LPWSTR]
            shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
            buf = ctypes.create_unicode_buffer(260)
            bi = _BROWSEINFO()
            bi.hwndOwner = parent_hwnd or None
            bi.lpszTitle = title
            bi.ulFlags = 0x0001  # BIF_RETURNONLYFSDIRS
            pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
            if pidl and shell32.SHGetPathFromIDListW(pidl, buf):
                return Path(buf.value.strip())
            return None
        finally:
            ole32.CoUninitialize()
    except Exception as exc:
        print(f"[dialogs] folder picker failed: {exc}", file=sys.stderr)
        return None


def save_image(path: Path, rgba) -> bool:
    """Write PNG/JPEG bytes matching the requested extension.

    imencode + write_bytes, NOT cv2.imwrite: OpenCV opens the file through
    the C runtime with the ANSI codepage, so a path with any non-ASCII
    character writes NOTHING - and imwrite still answers True. Measured: a
    Cyrillic folder gave "True" and no file, and the program told the user
    the screenshot had been saved. Encoding to memory and writing the bytes
    through Python leaves the path to Python, which handles it in UTF-16.

    The result is checked on disk before the answer: "the encoder said yes"
    is not the same as "the file is there".

    THE FOURTH CHANNEL IS FORCED OPAQUE. A screenshot is pixels of a desktop
    capture, which is opaque by construction - the alpha byte carries no
    information about the picture. It is not zero either: the frame surface is
    built with pygame.image.frombuffer(..., "RGBX"), where the X byte is
    "unused" as far as pygame's renderer is concerned, and the panel is drawn
    onto it through an SRCALPHA scratch. Whatever that compositing leaves in
    the byte is what the file gets, because frombuffer does not copy and the
    PNG path keeps the channel (COLOR_RGBA2BGRA). Measured on a reporter's
    v2.0.1 screenshot (#107): alpha 255 over the desktop, alpha 0 over the
    whole panel rectangle - colours present, transparency zeroed - so any
    viewer that honours alpha showed a hole where the panel was, and he
    reported it as "the transparent section".
    """
    import cv2
    import numpy as np

    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        extension = ".jpg"
        pixels = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)
        options = [cv2.IMWRITE_JPEG_QUALITY, 100]
    elif suffix == ".png":
        extension = ".png"
        # Opaque before the conversion: the byte above is not a fact about
        # the capture, and writing it out invents transparency that was
        # never on screen. JPEG already drops the channel, which is why the
        # fault only ever showed in a PNG - the default format.
        #
        # np.array(..., copy=True), NOT ascontiguousarray: the latter returns
        # the SAME buffer when it is already contiguous, and the write below
        # would then reach back into the caller's frame - a screenshot that
        # silently edits the picture it was taken from.
        opaque = np.array(rgba, copy=True)
        if opaque.ndim == 3 and opaque.shape[2] == 4:
            opaque[..., 3] = 255
        pixels = cv2.cvtColor(opaque, cv2.COLOR_RGBA2BGRA)
        options = [cv2.IMWRITE_PNG_COMPRESSION, 3]
    else:
        raise ValueError(f"unsupported screenshot extension: {path.suffix or '<none>'}")

    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(extension, pixels, options)
    if not ok:
        return False
    # Through a temporary name and os.replace: a write cut short (a full
    # disk) leaves no truncated picture under the name the user chose - and
    # a file that was already there is replaced only by a whole one.
    staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        staging.write_bytes(buf.tobytes())
        os.replace(staging, path)
    finally:
        try:
            staging.unlink()
        except FileNotFoundError:
            pass
    return path.is_file() and path.stat().st_size > 0


def save_jpeg(path: Path, rgba) -> bool:
    """Compatibility wrapper for callers that explicitly request JPEG."""
    return save_image(path.with_suffix(".jpg") if not path.suffix else path,
                      rgba)
