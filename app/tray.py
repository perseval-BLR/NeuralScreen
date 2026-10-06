"""TrayController - system tray icon for DLSS 5 Desktop NR.

Right-click menu: NR ON/OFF (with a checkmark), Scale (state), Settings,
Scale +0.05 / -0.05, Exit. Left click on the icon is the default action =
open the menu (Windows convention: right click for the menu, left for the
default). Commands go into a queue.Queue that the main loop drains.

The icon is the channel avatar (a black turbine fan with a gold "P"): the
same file the launcher and the taskbar button use, native/neuralscreen.ico.
The .ico rather than a single image because it carries a frame drawn for
every size - the tray asks for 16 or 24 px, and at that size the white rim
that separates the logo from a dark taskbar is a matter of one pixel, which
survives being drawn for 16 and does not survive 256 being squeezed into it.

Menu labels come from the caller: they are user-visible text, so they live
in i18n like the rest of the interface, not in this module. tray_labels()
picks them out of one language's strings, and set_labels() swaps them when
the user changes the language.
"""

from __future__ import annotations

import queue
import threading

import pystray
from PIL import Image, ImageDraw

from paths import BASE_DIR

#: Fallback labels, used when the caller passes none.
DEFAULT_LABELS = {"settings": "Settings", "quit": "Exit",
                  "nr_on": "NR: ON", "nr_off": "NR: OFF", "scale": "Scale",
                  "scale_up": "Scale +0.05", "scale_down": "Scale -0.05"}


def tray_labels(strings: dict) -> dict:
    """The tray's labels out of one language's i18n strings.

    The same words the panel uses for the same things: the NR switch's state,
    the processing resolution (the tray's "scale" is the work scale the
    panel's resolution slider sets) and the hotkey names of the two steps.
    A missing key keeps the English fallback.
    """
    pick = {"settings": "settings_title", "quit": "exit", "nr_on": "nr_on",
            "nr_off": "nr_off", "scale": "nr_res", "scale_up": "hk_scale_up",
            "scale_down": "hk_scale_down"}
    return {name: strings.get(key, DEFAULT_LABELS[name])
            for name, key in pick.items()}


def _make_icon(size: int = 64) -> Image.Image:
    """The tray image: the icon's own frame for this size.

    The .ico holds 16/32/48/64/128/256. The nearest frame at or above the
    requested size is taken and shrunk if it has to be - never blown up from
    a smaller one, which is what turns the rim into a grey halo.
    """
    ico = BASE_DIR / "native" / "neuralscreen.ico"
    if ico.is_file():
        try:
            img = Image.open(ico)
            have = sorted(w for w, _h in img.ico.sizes())
            pick = next((s for s in have if s >= size), have[-1])
            img.size = (pick, pick)
            img.load()
            img = img.convert("RGBA")
            return img if pick == size else img.resize((size, size),
                                                       Image.LANCZOS)
        except Exception:
            pass  # a broken .ico must not stop the program from starting
    # Fallback (the file is missing - a dev tree): the old placeholder, a
    # dark square with an amber accent.
    img = Image.new("RGBA", (size, size), (0x0D, 0x11, 0x17, 255))
    d = size // 8
    ImageDraw.Draw(img).rectangle((d, d, size - d, size - d),
                                  fill=(0xFF, 0xBF, 0x00, 255))
    return img


class TrayController:
    """Tray icon: commands into a queue, state for the menu to show."""

    def __init__(self, commands: queue.Queue, labels: dict | None = None):
        self._commands = commands
        self._labels = dict(DEFAULT_LABELS, **(labels or {}))
        self._state = {"nr": True, "scale": 0.5}
        self._icon = None
        self._thread = None

    def _set_state(self, **kw) -> None:
        self._state.update(kw)
        if self._icon is not None:
            try:
                nr = self._labels["nr_on" if self._state["nr"] else "nr_off"]
                self._icon.title = (f"NeuralScreen — {nr}"
                                    f" | {self._state['scale']:.2f}")
                self._icon.update_menu()
            except Exception:
                pass

    def set_labels(self, labels: dict) -> None:
        """New labels - the user changed the interface language.

        Every label is read through a callable on each update_menu(), so a
        swap here reaches the open icon; built from strings, the menu kept the
        launch language for the whole session.
        """
        self._labels = dict(DEFAULT_LABELS, **(labels or {}))
        self._set_state()

    def _cmd(self, name: str) -> None:
        self._commands.put(name)

    def _toggle_nr(self, icon, item) -> None:
        self._set_state(nr=not self._state["nr"])
        self._cmd("toggle")

    def _open_settings(self, icon, item) -> None:
        self._cmd("settings")

    # Scale is NOT changed optimistically: only main knows the bounds and the
    # step (WORK_SCALE_MIN/MAX), and it may also defer applying because of the
    # cooldown. The actual value comes back through _set_state(scale=...).
    def _scale_up(self, icon, item) -> None:
        self._cmd("scale_up")

    def _scale_down(self, icon, item) -> None:
        self._cmd("scale_down")

    def _quit(self, icon, item) -> None:
        self._cmd("quit")

    def _build_menu(self):
        return pystray.Menu(
            pystray.MenuItem(lambda item: self._labels["nr_on"], self._toggle_nr,
                             checked=lambda item: self._state["nr"]),
            pystray.MenuItem(lambda item: self._labels["nr_off"], self._toggle_nr,
                             checked=lambda item: not self._state["nr"]),
            pystray.Menu.SEPARATOR,
            # A callable, read on every update_menu(): the f-string was
            # evaluated once, when the menu was built, and the line kept the
            # launch value for the whole session.
            pystray.MenuItem(lambda item: f"{self._labels['scale']}: "
                                          f"{self._state['scale']:.2f}",
                             None, enabled=False),
            pystray.MenuItem(lambda item: self._labels["scale_up"], self._scale_up),
            pystray.MenuItem(lambda item: self._labels["scale_down"], self._scale_down),
            pystray.Menu.SEPARATOR,
            # default=True: left click on the icon triggers this item
            pystray.MenuItem(lambda item: self._labels["settings"],
                             self._open_settings, default=True),
            pystray.MenuItem(lambda item: self._labels["quit"], self._quit),
        )

    def start(self) -> None:
        """Start the tray in its own thread (does not block main)."""
        self._icon = pystray.Icon("neuralscreen", _make_icon(),
                                  "NeuralScreen", self._build_menu())
        self._set_state()
        self._thread = threading.Thread(target=self._icon.run, daemon=True)
        self._thread.start()

    def alive(self) -> bool:
        """Whether the icon is really in the tray.

        Asked before the taskbar button is hidden (#93): the icon runs in a
        daemon thread and pystray reports its failures there, where nothing
        sees them. Hiding the button on the strength of a thread that may have
        died leaves the program with no visible way back at all - the user's
        only exit would be Task Manager.
        """
        return (self._icon is not None and self._thread is not None
                and self._thread.is_alive())

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
