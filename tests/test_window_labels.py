r"""Window titles are labels; HWND is separate selection/hover identity.

Run: runtime\python.exe tests\test_window_labels.py
"""
import os
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402

import commands  # noqa: E402
import overlay_ui  # noqa: E402
import settings_io  # noqa: E402


WINDOWS = [
    (0x10101, "Editor: project: alpha"),
    (0x20202, "Editor: project: alpha"),
    (0x30303, "ABCD: a title that looks like an old prefix"),
]


def _font(size, **_kwargs):
    return pygame.font.Font(None, size)


def _click(menu, item):
    out = menu.handle_event(pygame.event.Event(
        pygame.MOUSEBUTTONDOWN, {"pos": item.rect.center, "button": 1}))
    menu.handle_event(pygame.event.Event(
        pygame.MOUSEBUTTONUP, {"pos": item.rect.center, "button": 1}))
    return out


def _press(menu, key):
    return menu.handle_event(pygame.event.Event(
        pygame.KEYDOWN, {"key": key, "mod": 0, "unicode": ""}))


def main() -> int:
    pygame.init()
    pygame.display.set_mode((64, 64))
    failures = []
    try:
        entries, current = settings_io._window_menu_state(WINDOWS, 0x20202)
        if [entry["title"] for entry in entries] != [title for _, title in WINDOWS]:
            failures.append("settings payload changed a window title")
        # Compared by FIELD, not by the whole dict: the record carries a size
        # as well now (the windows page shows it), and a strict equality here
        # would fail on any field added for the UI - which is not what this
        # test is about. What it locks is that hwnd stays an integer and the
        # title is not folded into the identity.
        if current.get("hwnd") != 0x20202 or current.get("title") != WINDOWS[1][1]:
            failures.append(f"wrong current structured window: {current!r}")
        if not isinstance(current.get("hwnd"), int):
            failures.append(f"hwnd is not an integer: {current!r}")
        if any(f"{entry['hwnd']:X}:" in entry["title"] for entry in entries):
            failures.append("technical HWND leaked into a payload label")

        menu = overlay_ui.OverlayMenu(1.0, _font)
        menu.set_state({"windows": entries, "window_current": current,
                        "window_mode": True, "work_size": "1280x720"})
        menu.page = "windows"
        menu.visible = True
        surface = pygame.Surface((1920, 1080))
        menu.draw(surface)
        rows = [item for item in menu.items
                if item.kind == "option" and item.key == "window"]
        if len(rows) != len(WINDOWS):
            failures.append(f"expected {len(WINDOWS)} rows, got {len(rows)}")
        else:
            labels = [row.extra.get("label") for row in rows]
            identities = [row.payload for row in rows]
            if labels != [title for _, title in WINDOWS]:
                failures.append(f"visible labels are not clean titles: {labels!r}")
            if identities != [hwnd for hwnd, _ in WINDOWS]:
                failures.append(f"rows lost HWND identity: {identities!r}")
            selected = [row.payload for row in rows
                        if row.extra.get("selected")]
            if selected != [0x20202]:
                failures.append(f"duplicate titles confused selection: {selected!r}")

            second = rows[1]
            menu.handle_event(pygame.event.Event(
                pygame.MOUSEMOTION,
                {"pos": second.rect.center, "rel": (0, 0),
                 "buttons": (0, 0, 0)}))
            if menu.hover_window != 0x20202:
                failures.append(f"hover carried {menu.hover_window!r}, not HWND")
            if _click(menu, second) != [("window", 0x20202)]:
                failures.append("mouse selection did not emit the integer HWND")
            menu._set_focus(rows[0])
            if _press(menu, pygame.K_RETURN) != [("window", 0x10101)]:
                failures.append("keyboard selection did not emit the integer HWND")

        # The main-page current-source line shows the full title, including
        # its colons, and never prepends the handle.
        menu.page = "main"
        menu.open_choice = None
        menu.folds = {"source"}     # the source lives in its fold-out (v2.6.0)
        menu.draw(surface)
        source = next((item for item in menu.items
                       if item.kind == "info" and item.key == "source_now"), None)
        if source is None or source.extra.get("label") != WINDOWS[1][1]:
            failures.append(f"current source label is {getattr(source, 'extra', None)!r}")

        # Even the old combined token is sanitised before drawing. It remains
        # accepted as identity only for in-process upgrade compatibility.
        menu.page = "windows"
        menu.set_state({"windows": ["1A2B: Legacy: title"],
                        "window_current": "1A2B: Legacy: title"})
        menu.layout(1920, 1080)
        legacy = next(item for item in menu.items if item.kind == "option")
        if legacy.extra.get("label") != "Legacy: title":
            failures.append(f"legacy prefix is still visible: {legacy.extra!r}")

        # The command layer receives the HWND directly; a colon-rich title is
        # not available there and cannot affect the selected window.
        calls = []

        class _User32:
            def IsWindow(self, hwnd):
                calls.append(("is_window", hwnd.value))
                return 1

            def BringWindowToTop(self, hwnd):
                calls.append(("bring", hwnd.value))
                return 1

            def SetForegroundWindow(self, hwnd):
                calls.append(("foreground", hwnd.value))
                return 1

        old_windll = commands.ctypes.windll
        old_switch = commands.pipeline.switch_window
        try:
            commands.ctypes.windll = types.SimpleNamespace(user32=_User32())
            commands.pipeline.switch_window = (
                lambda state, hwnd: calls.append(("switch", hwnd)))
            state = types.SimpleNamespace(
                lang="en",
                display=types.SimpleNamespace(alert=lambda *_args: None),
            )
            commands.apply_menu_action(state, ("window", 0x30303))
        finally:
            commands.ctypes.windll = old_windll
            commands.pipeline.switch_window = old_switch
        if ("switch", 0x30303) not in calls:
            failures.append(f"command layer switched the wrong identity: {calls!r}")
    finally:
        pygame.quit()

    for failure in failures:
        print("FAIL:", failure)
    if failures:
        return 1
    print("OK: clean duplicate/colon titles retain exact HWND identity")
    return 0


if __name__ == "__main__":
    sys.exit(main())
