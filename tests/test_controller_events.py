r"""A game controller that comes and goes does not stop the program (#152).

`pygame.event.get()` raised `SystemError ... KeyError: 0` and the main loop
ended the program at startup. pygame.init() also starts SDL's joystick
subsystem, and when a controller (or a virtual gamepad, Steam Input, a
wireless pad waking up) is added and removed again before pygame has mapped
it, pygame fails on the "device removed" event for an id it never recorded.
NeuralScreen does not use controllers at all.

Checked with the real Display (dummy SDL driver) and an SDL virtual joystick
attached and detached straight away - the same sequence as a flapping
device. Reading the event queue afterwards must not raise, both through
Display.poll_events() and through a plain pygame.event.get() as the main
loop does.

Run:  runtime\python.exe tests\test_controller_events.py
"""
import ctypes
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402

SDL_JOYSTICK_TYPE_GAMECONTROLLER = 1


def _flap(sdl) -> bool:
    """Attach and detach a virtual controller; False when SDL cannot."""
    sdl.SDL_JoystickAttachVirtual.restype = ctypes.c_int
    index = sdl.SDL_JoystickAttachVirtual(SDL_JOYSTICK_TYPE_GAMECONTROLLER, 2, 2, 0)
    if index < 0:
        return False
    sdl.SDL_JoystickDetachVirtual(index)
    return True


def main() -> int:
    import display as display_mod

    sdl_path = Path(pygame.__file__).with_name("SDL2.dll")
    if not sdl_path.is_file():
        print("SKIP: pygame's SDL2.dll is not where this test expects it")
        return 0
    sdl = ctypes.CDLL(str(sdl_path))
    failures = []
    disp = display_mod.Display(640, 360, click_through=False)
    try:
        for reader, read in (("Display.poll_events()", disp.poll_events),
                             ("pygame.event.get()", pygame.event.get)):
            if not _flap(sdl):
                print("SKIP: SDL could not attach a virtual joystick")
                return 0
            try:
                read()
            except Exception as exc:  # the reported crash is a SystemError
                cause = exc.__cause__ or exc.__context__
                failures.append(f"{reader} raised {type(exc).__name__}: {exc}"
                                f" (cause: {cause!r})")
    finally:
        pygame.quit()
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a controller that connects and disconnects does not stop the program")
    return 0


if __name__ == "__main__":
    sys.exit(main())
