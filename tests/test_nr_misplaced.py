"""An NR runtime put where the worker does not look is named, with the right place (#185).

The worker takes exactly native\\libraries\\nvngx_dlssnr.dll and says nothing
when it is not there. The same user put 310.9.16 somewhere else twice (#182,
#185): both logs show the bundled 310.8, with no line about why.

Checked without a GPU, in a scratch program folder:

1. the usual slips are found - a libraries\\ folder next to the program, the
   DLL in the program folder, a doubled extension, an extracted archive one
   folder down, the archive itself, a second DLL beside the bundled one -
   and only those; the bundled runtime is never reported;
2. with the runtime where it belongs nothing is reported, whatever else lies
   around;
3. the Fine tuning block names the stray file and where it has to go;
4. every language has the line, with the right path in it.

Fails before the change: nothing looked for them.

Run:  runtime\\python.exe tests\\test_nr_misplaced.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "app"))

SLIPS = [
    Path("libraries") / "nvngx_dlssnr.dll",
    Path("nvngx_dlssnr.dll"),
    Path("native") / "libraries" / "nvngx_dlssnr.dll.dll",
    Path("native") / "libraries" / "dlssnr_310.9.16" / "nvngx_dlssnr.dll",
    Path("native") / "libraries" / "nvngx_dlssnr_310.9.16.zip",
    Path("native") / "nvngx_dlssnr (1).dll",
]
PLACE = "native\\libraries\\nvngx_dlssnr.dll"


def scratch(root: Path, files) -> None:
    for rel in [Path("native") / "nvngx_dlssnr.dll", Path("native") / "nvngx.dll",
                Path("native") / "libraries" / "nvngx_dlssg.dll", Path("main.py")] + list(files):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"x")


def main() -> int:
    failures = []
    try:
        import nr_runtime
        find = nr_runtime.misplaced
    except (ImportError, AttributeError):
        find = None
    for slip in SLIPS:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scratch(root, [slip])
            got = find(root) if find else []
            if got != [str(slip)]:
                failures.append(f"{slip} gave {got}")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        scratch(root, [])
        if find and find(root):
            failures.append(f"a clean folder reported {find(root)}")
        scratch(root, [Path("native") / "libraries" / "nvngx_dlssnr.dll", Path("libraries") / "nvngx_dlssnr.dll"])
        if find and find(root):
            failures.append(f"with the runtime in place it still reported {find(root)}")

    import pygame
    pygame.init()
    pygame.display.set_mode((64, 64))
    import fonts
    import overlay_ui as ui
    from i18n import STRINGS
    menu = ui.OverlayMenu(1.0, lambda size, mono=False, bold=False: fonts.load(size, mono=mono))
    menu.visible = True
    menu.page = "main"
    menu.set_state({"nr": True, "gpu_ok": True, "profile": "Natural", "profiles": ["Natural"],
                    "nr_presets": False, "nr_runtime_version": "310.8.0.0",
                    "nr_runtime_misplaced": "libraries\\nvngx_dlssnr.dll"})
    menu.folds = {"tune"}
    menu.layout(1920, 1080)
    note = next((i for i in menu.items if i.key == "nr_misplaced"), None)
    text = str(note.extra.get("text")) if note else ""
    if "libraries\\nvngx_dlssnr.dll" not in text or PLACE not in text:
        failures.append(f"Fine tuning says {text!r}")
    menu.set_state({"nr_runtime_misplaced": ""})
    menu.layout(1920, 1080)
    if any(i.key == "nr_misplaced" for i in menu.items):
        failures.append("the line stays with nothing misplaced")
    pygame.quit()
    for lang, s in sorted(STRINGS.items()):
        line = s.get("nr_misplaced", "")
        if "{name}" not in line or PLACE not in line:
            failures.append(f"{lang}: the line is {ascii(line)}")

    for f in failures:
        print("  FAIL:", f)
    print("PASS" if not failures else "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
