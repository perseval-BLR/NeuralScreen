"""GPU model and architecture, through nvapi, with no external processes.

The interface needs it: to show what we are running on and whether Neural
Rendering is available. The architecture is read the same way NVIDIA's own
library reads it (nvapi_QueryInterface -> NvAPI_GPU_GetArchInfo), so the
value matches the one it makes its decision on.

Everything is wrapped in try: without nvapi (a non-NVIDIA machine, a
stripped driver) the module returns empty fields instead of taking the
program down.
"""
from __future__ import annotations

import ctypes

# nvapi function ids are hashes of their names
_ID_INITIALIZE = 0x0150E828
_ID_ENUM_GPUS = 0xE5AC921F
_ID_GET_ARCH = 0xD8265D24
_ID_GET_NAME = 0xCEEE8E9F

# NV_GPU_ARCHITECTURE_ID: the group lives in the high bits. Neural Rendering
# (feature 18) officially requires Blackwell — see NGXGpuArchitecture inside
# nvngx_dlssnr.dll itself. Verified against NVIDIA's nvapi.h (TU100=0x160,
# GA100=0x170, AD100=0x190, GB200=0x1B0) and open-gpu-kernel-modules
# nv_arch.h (Turing=0x160, Ampere=0x170, Hopper=0x180, Ada=0x190,
# Blackwell GB1XX=0x1A0, GB2XX=0x1B0). Real-user logs confirm: RTX 2070
# reports 0x160, RTX 3060 Ti reports 0x170.
ARCH_NAMES = {
    0x160: ("Turing", "20xx"),
    0x170: ("Ampere", "30xx"),
    0x180: ("Hopper", ""),
    0x190: ("Ada", "40xx"),
    0x1A0: ("Blackwell", "50xx"),
    0x1B0: ("Blackwell", "50xx"),
    0x1C0: ("Blackwell", "50xx"),
}
ARCH_BLACKWELL = 0x1A0

# DLSS Frame Generation needs Ada (RTX 40) or newer. Turing (RTX 20) and
# Ampere (RTX 30) have no DLSS-G, so they run FSR Frame Generation instead -
# the interpolation from the AMD FSR SDK, which runs on any DX12 GPU. (FSR 4
# frame generation is the ML variant for RDNA 4 only; FSR 3.1.x is the newest
# generation that exists for NVIDIA hardware.)
FSR_FG_ARCH_GROUPS = (0x160, 0x170)
FG_BACKENDS = ("auto", "fsr", "dlss")


def fg_backend(info: dict, choice: str | None = "auto") -> str:
    """\"fsr\" or \"dlss\": the frame generation backend for this card.

    ``choice`` is the ``fg_backend`` config key. \"auto\" (and anything that is
    not a known value) picks by architecture; an unknown architecture keeps
    DLSS-G, which is what every earlier version did.
    """
    if choice in ("fsr", "dlss"):
        return choice
    group = int((info or {}).get("arch_group", 0) or 0)
    return "fsr" if group in FSR_FG_ARCH_GROUPS else "dlss"


class _ArchInfo(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32),
                ("architecture", ctypes.c_uint32),
                ("implementation", ctypes.c_uint32),
                ("revision", ctypes.c_uint32)]


def _choose_index(names: list, hint) -> int:
    """Which card the hint names, or 0 - the first one.

    The hint is the DXGI name of the adapter the pipeline will really run
    on. NVAPI enumerates in its own order and the two disagree on
    multi-GPU machines - the first NVAPI card can be the one that does no
    work at all (issue #81: the menu said RTX 3050 while the 4070 Super
    did everything). No hint, no cards, or nothing matching keeps the
    first card, which is what a single-card machine wants.
    """
    if hint:
        wanted = str(hint).strip().casefold()
        for i, nm in enumerate(names):
            if str(nm).strip().casefold() == wanted:
                return i
    return 0


def probe(name_hint: str | None = None) -> dict:
    """{name, arch, arch_group, family, official} — empty fields on failure.

    name_hint says WHICH card to describe, by its DXGI name: the caller
    passes the adapter the worker will really run on, so the line on
    screen and the log name the card that does the work (issue #81).
    Without a hint the first card is described.
    """
    out = {"name": "", "arch": "", "arch_group": 0, "family": "",
           "official": False}
    try:
        nvapi = ctypes.WinDLL("nvapi64.dll")
        qi = nvapi.nvapi_QueryInterface
        qi.restype = ctypes.c_void_p
        qi.argtypes = [ctypes.c_uint32]

        p_init, p_enum = qi(_ID_INITIALIZE), qi(_ID_ENUM_GPUS)
        if not p_init or not p_enum:
            return out
        if ctypes.CFUNCTYPE(ctypes.c_int)(p_init)() != 0:
            return out

        handles = (ctypes.c_void_p * 64)()
        count = ctypes.c_uint32(0)
        enum = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_void_p),
                                ctypes.POINTER(ctypes.c_uint32))(p_enum)
        if enum(handles, ctypes.byref(count)) != 0 or count.value == 0:
            return out

        p_name = qi(_ID_GET_NAME)
        # Every card's name first: the hint can only be matched against a
        # name, and reading the names is the only way to know which handle
        # is which - NVAPI's order is not DXGI's (see _choose_index).
        names = []
        name_fn = (ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                                    ctypes.c_char_p)(p_name)
                   if p_name else None)
        for h in handles[:count.value]:
            nm = ""
            if name_fn is not None:
                buf = ctypes.create_string_buffer(64)
                if name_fn(h, buf) == 0:
                    nm = buf.value.decode("ascii", "replace").strip()
            names.append(nm)
        chosen = _choose_index(names, name_hint)
        gpu = handles[chosen]

        name = names[chosen]
        if name:
            # "NVIDIA GeForce RTX 5070 Ti" -> "RTX 5070 Ti": the full name
            # does not fit the menu line, and the vendor adds nothing there.
            for prefix in ("NVIDIA GeForce ", "NVIDIA "):
                if name.startswith(prefix):
                    name = name[len(prefix):]
                    break
            out["name"] = name

        p_arch = qi(_ID_GET_ARCH)
        if p_arch:
            fn = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                                  ctypes.POINTER(_ArchInfo))(p_arch)
            for ver in (2, 1):
                info = _ArchInfo()
                info.version = ctypes.sizeof(_ArchInfo) | (ver << 16)
                if fn(gpu, ctypes.byref(info)) == 0:
                    group = info.architecture & 0xFFFFFFF0
                    arch, family = ARCH_NAMES.get(group, ("", ""))
                    out["arch_group"] = group
                    out["arch"] = arch
                    out["family"] = family
                    out["official"] = group >= ARCH_BLACKWELL
                    break
    except Exception:
        return out
    return out


def describe(info: dict) -> str:
    """Menu line: "RTX 5070 Ti · Blackwell". Empty when the GPU is unknown —
    the menu shows its own placeholder rather than an English string in a
    localised interface."""
    name = info.get("name")
    if not name:
        return ""
    arch = info.get("arch")
    return f"{name} · {arch}" if arch else name


if __name__ == "__main__":
    got = probe()
    print(describe(got) or "unknown GPU")
    print(f"group 0x{got['arch_group']:X}, officially supported: "
          f"{'yes' if got['official'] else 'no'}")
