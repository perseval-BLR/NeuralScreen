"""HotkeyController - global hotkeys through RegisterHotKey + a polling fallback.

The difference from polling GetAsyncKeyState is fundamental: the system
delivers WM_HOTKEY only to us and does NOT pass the keypress to the active
application. Num1 inside a game toggles NR and the game never sees the key.
Polling cannot do that — it only peeks at the key state while the press still
reaches the game.

The flip side of the same property: while NeuralScreen runs, the numpad
digits it binds belong to it and other programs (games, a spreadsheet) will
not get them.

Quitting is on Ctrl+Alt deliberately: a single key for it is far too easy to
hit by accident. Bare arrows are never registered either - they would stop
working system-wide.

RegisterHotKey(NULL, ...) posts WM_HOTKEY to the message queue of the CALLING
thread, so no window is needed — only a message loop in our own thread.
Commands go into the same queue the tray uses: the command vocabulary is
shared ("quit", "settings", "toggle", "scale_up", "scale_down").
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time
from ctypes import wintypes

user32 = ctypes.windll.user32

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000  # holding the key does not spam repeats

VK_F7 = 0x76
VK_F8 = 0x77
VK_F9 = 0x78
VK_F10 = 0x79
VK_F11 = 0x7A
VK_UP = 0x26
VK_DOWN = 0x28
VK_Q = 0x51
VK_INSERT = 0x2D
VK_HOME = 0x24
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_SHIFT = 0x10
VK_NUMLOCK = 0x90
VK_F1 = 0x70
VK_F24 = 0x87
# The numpad block. These codes only arrive while Num Lock is ON: with it off
# the same physical keys send Insert/End/arrows/Home/PageUp, and nothing in
# RegisterHotKey or GetAsyncKeyState can tell them apart from the dedicated
# navigation keys - which is why numpad bindings need Num Lock (see
# numlock_needed below).
VK_NUMPAD = {n: 0x60 + n for n in range(10)}
VK_DECIMAL = 0x6E
VK_MULTIPLY = 0x6A
VK_SUBTRACT = 0x6D
VK_ADD = 0x6B
VK_DIVIDE = 0x6F
_NUMPAD_VKS = set(VK_NUMPAD.values()) | {
    VK_DECIMAL, VK_MULTIPLY, VK_SUBTRACT, VK_ADD, VK_DIVIDE}

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000
# Our own messages to the hotkey thread. RegisterHotKey/UnregisterHotKey with
# hWnd=None are bound to the CALLING thread, so they can only be removed and
# reinstalled from inside that same thread — never straight from main.
MSG_SUSPEND = 0x8000 + 1
MSG_RESUME = 0x8000 + 2
MSG_REBIND = 0x8000 + 3
# The master switch (#134). Its own message rather than a flag read straight
# from the main thread: enable and the menu's resume() can be posted in either
# order, and a flag written outside the queue would let a late resume()
# re-register hotkeys the user had just switched off. Every message here is
# handled in order by the one thread, so the last one wins as the user meant.
MSG_ENABLE = 0x8000 + 4

# id -> (modifiers, VK, command, human-readable name)
# The defaults live on the numpad. The reasoning, since it changed twice:
# function keys keep colliding with things - F9 is quickload in Bethesda
# titles, F8/F7 are screenshots in some engines, and F10 turned out to be the
# NVIDIA App's recording key on a real machine, so one press did two things.
# The numpad is a block of keys almost nothing competes for, and 0-1-2-3 in a
# row is easier to remember than three scattered F-keys.
#
# Two consequences to be honest about:
#   * Num Lock has to be ON, otherwise these keys send Insert/End/arrows
#     instead and the bindings are simply not there (main warns about it);
#   * while NeuralScreen runs these numpad digits belong to it and other
#     programs will not see them - that is what RegisterHotKey does, and it
#     was equally true of F10/F11 before.
# Quitting stays on Ctrl+Alt+Q: a single key for it is too easy to hit.
DEFAULT_BINDINGS = {
    1: (MOD_NOREPEAT, VK_NUMPAD[1], "toggle", "Num1"),
    2: (MOD_NOREPEAT, VK_NUMPAD[2], "settings", "Num2"),
    3: (MOD_NOREPEAT, VK_NUMPAD[6], "scale_up", "Num6"),
    4: (MOD_NOREPEAT, VK_NUMPAD[4], "scale_down", "Num4"),
    5: (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_Q, "quit", "Ctrl+Alt+Q"),
    # Num0 is the big key at the bottom of the block, and with Num Lock off it
    # is the very Insert that used to start a recording.
    6: (MOD_NOREPEAT, VK_NUMPAD[0], "record", "Num0"),
    7: (MOD_NOREPEAT, VK_NUMPAD[3], "screenshot_menu", "Num3"),
    # One-window mode: the window that had the focus becomes the only thing
    # processed. Num5 is the middle of the block and free of any habit.
    8: (MOD_NOREPEAT, VK_NUMPAD[5], "window_mode", "Num5"),
    # Frame Generation on/off, next to the NR toggle on the block (user
    # request 15.09: the FG switch lives two pages deep in the menu).
    9: (MOD_NOREPEAT, VK_NUMPAD[7], "framegen", "Num7"),
    # The NR cascade one pass up or down (#126). On the block's own + and -,
    # not the main row's: RegisterHotKey takes a key from every program, and
    # a hyphen is typed far too often to belong to NeuralScreen.
    10: (MOD_NOREPEAT, VK_ADD, "nr_passes_up", "Numplus"),
    11: (MOD_NOREPEAT, VK_SUBTRACT, "nr_passes_down", "Numminus"),
}

# Key name -> VK (for parsing the config)
_KEY_NAMES = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74,
    "F6": 0x75, "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79,
    "F11": 0x7A, "F12": 0x7B,
    "INSERT": VK_INSERT, "DELETE": 0x2E, "HOME": 0x24, "END": 0x23,
    "PGUP": 0x21, "PGDN": 0x22,
    "UP": VK_UP, "DOWN": VK_DOWN, "LEFT": 0x25, "RIGHT": 0x27,
    "Q": VK_Q, "W": 0x57, "E": 0x45, "R": 0x52, "T": 0x54, "Y": 0x59,
    "U": 0x55, "I": 0x49, "O": 0x4F, "P": 0x50, "A": 0x41, "S": 0x53,
    "D": 0x44, "F": 0x46, "G": 0x47, "H": 0x48, "J": 0x4A, "K": 0x4B,
    "L": 0x4C, "Z": 0x5A, "X": 0x58, "C": 0x43, "V": 0x56, "B": 0x42,
    "N": 0x4E, "M": 0x4D,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    # The numpad. "NUM+" is unspellable here - the parser splits on "+" - so
    # the arithmetic keys go by name.
    "NUM0": 0x60, "NUM1": 0x61, "NUM2": 0x62, "NUM3": 0x63, "NUM4": 0x64,
    "NUM5": 0x65, "NUM6": 0x66, "NUM7": 0x67, "NUM8": 0x68, "NUM9": 0x69,
    "NUMDOT": VK_DECIMAL, "NUMMUL": VK_MULTIPLY, "NUMMINUS": VK_SUBTRACT,
    "NUMPLUS": VK_ADD, "NUMDIV": VK_DIVIDE,
}


#: What the user may type into a field to take a command off the keyboard
#: (#134). Words rather than only an empty string, because the field is
#: filled by pressing a key and there is no obvious "clear" gesture - and
#: because a config written by hand reads better with a word in it.
UNBIND_WORDS = frozenset({"none", "off", "no", "disabled", "unbound"})


def parse_binding(text: str) -> tuple[int, int] | None:
    """Parse a string like 'F10', 'Ctrl+Alt+Q', 'Num1' -> (mods, vk).

    Returns None when the string is not recognised, in which case the binding
    is left alone - and when it names a key that cannot be a global hotkey:

    * a typing key (a letter, a digit, an arrow, Insert/Delete/Home/End/
      PgUp/PgDn) with no Ctrl or Alt. RegisterHotKey takes the key from
      every program, so a bare "E" or "Up" would stop working system-wide,
      and Shift alone is still typing (Shift+Insert pastes, Shift+arrows
      select). Bare keys are the F-keys and the numpad only;
    * Shift with a numpad digit or the numpad dot. With Num Lock on, Windows
      sends those as the navigation keys while Shift is held (Shift+Num1
      arrives as End), so the combination can never fire.
    """
    if not text:
        return None
    stripped = text.strip()
    # A leading or trailing '+' is malformed ("+Q", "Q+", "Ctrl+") - the
    # split below would silently drop the empty part and accept "+Q" as
    # "Q". Reject the whole string instead.
    if stripped.startswith("+") or stripped.endswith("+"):
        return None
    parts = [p.strip().upper() for p in stripped.split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    for p in parts[:-1]:
        if p == "CTRL" or p == "CONTROL":
            mods |= MOD_CONTROL
        elif p == "ALT":
            mods |= MOD_ALT
        elif p == "SHIFT":
            mods |= MOD_SHIFT
        else:
            return None
    vk = _KEY_NAMES.get(parts[-1])
    if vk is None:
        return None
    if (not mods & (MOD_CONTROL | MOD_ALT)
            and not VK_F1 <= vk <= VK_F24 and vk not in _NUMPAD_VKS):
        return None                 # a typing key: it belongs to every program
    if mods & MOD_SHIFT and (vk in VK_NUMPAD.values() or vk == VK_DECIMAL):
        return None                 # Shift turns it into a navigation key
    return mods | MOD_NOREPEAT, vk


def build_bindings(overrides: dict | None = None) -> dict:
    """Bindings with the user's overrides from the config applied.

    overrides: {"toggle": "F10", "record": "Insert", ...} — command -> string.
    Unknown or malformed strings are ignored and the default stays - and so
    are bindings parse_binding no longer accepts (a bare letter or arrow an
    older version let through): the command falls back to its default key,
    and the log says so.

    A command can also be taken OFF the keyboard entirely (#134): an empty
    string, or one of UNBIND_WORDS, removes its binding from the set. That is
    the difference between "leave this one alone" (no key in the overrides at
    all, or an unparsable one) and "I do not want this key" - the request is
    explicit, and rebinding cannot express it: a new key is still a key.
    """
    bindings = {hk_id: tuple(entry) for hk_id, entry in DEFAULT_BINDINGS.items()}
    if not overrides:
        return bindings
    for hk_id, (mods, vk, cmd, name) in list(bindings.items()):
        if cmd not in overrides:
            continue
        text = overrides.get(cmd)
        if isinstance(text, str) and text.strip().lower() in UNBIND_WORDS:
            del bindings[hk_id]
            continue
        if not text:
            continue
        parsed = parse_binding(text)
        if parsed is None:
            print(f"[hotkeys] {cmd}: {text!r} cannot be a global hotkey - "
                  f"the default {name} is used")
            continue
        new_mods, new_vk = parsed
        bindings[hk_id] = (new_mods, new_vk, cmd, text)
    return bindings


def describe(bindings: dict | None = None) -> str:
    """A line like 'Num1=toggle, Num2=settings, ...' for the startup log."""
    src = DEFAULT_BINDINGS if bindings is None else bindings
    return ", ".join(f"{name}={cmd}" for _, (_, _, cmd, name) in sorted(src.items()))


def numlock_on() -> bool:
    """Num Lock state. The low bit of GetKeyState is the toggle, not the press."""
    return bool(user32.GetKeyState(VK_NUMLOCK) & 1)


def numlock_needed(bindings: dict | None = None) -> list:
    """Binding names that do nothing while Num Lock is off.

    With Num Lock off the numpad sends Insert/End/arrows instead of the
    numeric codes, so those hotkeys are not merely inconvenient - they never
    fire at all. Worth saying out loud rather than letting the user conclude
    the program is broken.
    """
    src = DEFAULT_BINDINGS if bindings is None else bindings
    # The arithmetic keys send the same code with Num Lock off.
    return [name for _, (_, vk, _cmd, name) in sorted(src.items())
            if vk in _NUMPAD_VKS
            and vk not in (VK_ADD, VK_SUBTRACT, VK_MULTIPLY, VK_DIVIDE)]


# --- polling fallback -----------------------------------------------------
# Some engines (id Tech 5, UE3) grab the keyboard exclusively even in a
# window: the system does not deliver WM_HOTKEY while the game holds the
# device, so RegisterHotKey alone is dead under them. The poller below reads
# the raw key state (GetAsyncKeyState) and fires the command when the press
# never arrived as WM_HOTKEY. It runs ALWAYS - when RegisterHotKey works it
# fires first and the poller's duplicate is suppressed by the cooldown; when
# the game swallows the hotkey the poller is the only path. The key still
# reaches the game (unlike RegisterHotKey), which is fine for the numpad keys
# and the Ctrl+Alt combinations.

# 15 ms, not 30: under a game that swallows WM_HOTKEY the poller is the only
# path, and it can only see a press that is still down when it samples. A
# short press - and people do tap briefly - fell between two samples.
POLL_INTERVAL = 0.015
POLL_COOLDOWN = 0.25


def _pressed(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def _mods_down(mods: int) -> bool:
    if mods & MOD_CONTROL and not _pressed(VK_CONTROL):
        return False
    if mods & MOD_ALT and not _pressed(VK_MENU):
        return False
    if mods & MOD_SHIFT and not _pressed(VK_SHIFT):
        return False
    return True


def _mods_clear(mods: int) -> bool:
    """A bare key (no modifiers) must not fire while Ctrl/Alt/Shift is down."""
    if mods & (MOD_CONTROL | MOD_ALT | MOD_SHIFT):
        return True
    return not (_pressed(VK_CONTROL) or _pressed(VK_MENU) or _pressed(VK_SHIFT))


class HotkeyController:
    """Registers the global hotkeys; commands go into a queue."""

    def __init__(self, commands: queue.Queue, bindings: dict | None = None):
        self._commands = commands
        # {} is a real answer: every command was taken off the keyboard (#134).
        self._bindings = DEFAULT_BINDINGS if bindings is None else bindings
        self._thread: threading.Thread | None = None
        self._tid = 0
        self.registered: list[str] = []
        self.failed: list[str] = []
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._pending: dict | None = None   # bindings for MSG_REBIND
        # Set once a MSG_REBIND has been registered, so the caller can read
        # `failed` for the NEW bindings (see wait_rebound).
        self._rebound = threading.Event()
        self._active = False                # hotkeys are currently registered
        # The master switch (#134): off means the bindings are never
        # registered and the poller stays quiet, whatever suspend/resume say.
        # Kept apart from `_active` because the two answer different
        # questions - `_active` is "registered right now", this is "registered
        # at all" - and resume() must not undo it.
        self._enabled = True
        #: Set by the hotkey thread once a MSG_ENABLE has been acted on, so a
        #: caller can report the real state instead of the one it asked for.
        self._enabled_done = threading.Event()
        # Polling fallback state: (mods, vk) -> last time the command fired,
        # and vk -> was the key down on the previous tick. Both keyed so that
        # Num1 and Ctrl+Num1 are two bindings: the cooldown belongs to the
        # combination, the down-state to the physical key. The timestamp kills the
        # duplicate that would otherwise follow a delivered WM_HOTKEY (the
        # message loop stamps it too); the down-state makes the poller fire on
        # the press EDGE instead of every cooldown while a key is held.
        # Both threads check-and-stamp the timestamp table, so that pair is
        # done under self._lock: unlocked, a WM_HOTKEY and a poller sample of
        # the same press could both read "not yet" and fire it twice.
        self._poll_last: dict[tuple[int, int], float] = {}
        self._poll_down: dict[int, bool] = {}
        # The ids RegisterHotKey accepted. Only those are polled: a refused
        # combination belongs to another program, which gets the press -
        # firing it from the poller as well made one key do two things,
        # while the user had been told the key would not work.
        self._live_ids: set[int] = set()
        self._poll_stop = threading.Event()

    def start(self, timeout: float = 3.0) -> None:
        """Start the thread and wait for the registration result."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="hotkeys")
        self._thread.start()
        self._ready.wait(timeout)
        # The polling fallback is a separate daemon: it must keep running
        # even while the message loop is blocked inside GetMessageW.
        self._poll_stop.clear()
        threading.Thread(target=self._poll_loop, daemon=True,
                         name="hotkeys-poll").start()

    def _run(self) -> None:
        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        # A thread's message queue is created lazily — force it into
        # existence BEFORE RegisterHotKey, or the first WM_HOTKEY may vanish.
        user32.PeekMessageW(ctypes.byref(msg), None, WM_HOTKEY, WM_HOTKEY, PM_NOREMOVE)
        self._register()
        self._ready.set()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                binding = self._bindings.get(msg.wParam)
                if binding is not None:
                    # Tell the poller this press is already handled. Without
                    # it the poller delivered the same command again ~30 ms
                    # later and every hotkey fired twice: NR toggled on and
                    # straight back off, the menu opened and closed.
                    key = (binding[0], binding[1])
                    now = time.monotonic()
                    with self._lock:
                        fresh = now - self._poll_last.get(key, 0.0) >= POLL_COOLDOWN
                        if fresh:
                            self._poll_last[key] = now
                    if not fresh:
                        continue  # the poller already delivered this press
                    self._commands.put(binding[2])
            elif msg.message == MSG_SUSPEND:
                self._unregister()
            elif msg.message == MSG_RESUME:
                # A master switch that is off wins over a resume: the menu
                # closing gives the keyboard back to the USER, not to us, and
                # _register() would otherwise put the keys back on (#134).
                self._register()
            elif msg.message == MSG_ENABLE:
                self._enabled = bool(msg.wParam)
                if self._enabled:
                    self._register()
                else:
                    self._unregister()
                self._enabled_done.set()
            elif msg.message == MSG_REBIND:
                self._unregister()
                with self._lock:
                    if self._pending is not None:
                        self._bindings = self._pending
                        self._pending = None
                self._register()
                self._rebound.set()
        self._unregister()

    # Registration lives only in the hotkey thread — see MSG_* above.
    def _register(self) -> None:
        # The master switch wins over every route in: with hotkeys off a
        # rebind, a resume() or the startup call all land here and must do
        # nothing. Checked before `_active` so an off controller reports no
        # `registered` names and the caller is not told it holds keys it does
        # not (#134).
        if not self._enabled or self._active:
            return
        self.registered = []
        self.failed = []
        live = set()
        for hk_id, (mods, vk, _cmd, name) in self._bindings.items():
            if user32.RegisterHotKey(None, hk_id, mods, vk):
                self.registered.append(name)
                live.add(hk_id)
            else:
                # Someone else already holds the combination — not fatal,
                # the remaining hotkeys keep working.
                self.failed.append(name)
        # The poller's baseline is taken HERE, at the moment the hotkeys go
        # live, and not on the poller's first sample. Two rules meet here:
        # a key already held when we start must not fire (a stuck key, a game
        # holding Num1), and a key pressed AFTER we start must fire - even if
        # the poller's first sample happens to land while it is still down.
        # Defaulting the baseline to the first sample satisfied the first rule
        # and broke the second: in a game (where WM_HOTKEY never arrives and
        # the poller is the only path) the first press after launch was eaten
        # as the baseline and did nothing. Measured: 6 of 6 presses lost when
        # the first sample landed inside the press.
        with self._lock:
            self._poll_down = {vk: _pressed(vk)
                               for _mods, vk, _cmd, _n in self._bindings.values()}
            # A fresh baseline also clears the cooldowns: they belong to the
            # previous registration, and MSG_REBIND goes through here.
            self._poll_last = {}
            self._live_ids = live
        self._active = True

    def _poll_loop(self) -> None:
        """Raw-key fallback: fires commands RegisterHotKey cannot deliver.

        Runs every POLL_INTERVAL and fires on the press EDGE: a key that is
        held down must not repeat the command. On top of that a cooldown
        suppresses the duplicate of a press RegisterHotKey already delivered -
        the message loop stamps the same table when it hands over a WM_HOTKEY,
        which is what makes the fallback invisible while the normal path works.
        The poller also honours suspend: the message loop unregisters the
        hotkeys while the menu waits for a rebind key, and the poller must
        go quiet the same way - otherwise the key being remapped fires through
        the poller and breaks the capture (audit #4, HIGH).
        """
        while not self._poll_stop.wait(POLL_INTERVAL):
            if not self._active:
                continue                  # suspended (or not yet registered)
            self._poll_tick()

    def _poll_tick(self) -> None:
        """One poller sample: fire commands for fresh press edges.

        Split out of the loop so a test can drive it directly.
        """
        with self._lock:
            bindings = {hk_id: entry for hk_id, entry in self._bindings.items()
                        if hk_id in self._live_ids}
        now = time.monotonic()
        # One sample and one edge per physical key, before any binding is
        # looked at. Sampled per binding, the first of two bindings on the
        # same key (Num1 and Ctrl+Num1) stored "down" and the second then
        # never saw an edge - it could not fire through the poller at all.
        fresh: dict[int, bool] = {}
        for _mods, vk, _cmd, _name in bindings.values():
            if vk in fresh:
                continue
            down = _pressed(vk)
            # A key held down at startup (a stuck key, a game holding
            # Num1) must not fire: the poller only triggers on a fresh
            # press EDGE, and the first sample is the baseline, not an
            # event (user: "NR OFF (bypass NGX)" right after every start
            # while Num1 was physically held).
            was_down = self._poll_down.get(vk, down)
            self._poll_down[vk] = down
            fresh[vk] = down and not was_down
        for hk_id, (mods, vk, cmd, _name) in bindings.items():
            if not fresh[vk]:
                continue                      # not a fresh press
            if not _mods_down(mods) or not _mods_clear(mods):
                continue
            with self._lock:
                if now - self._poll_last.get((mods, vk), 0.0) < POLL_COOLDOWN:
                    continue                  # WM_HOTKEY already did it
                self._poll_last[(mods, vk)] = now
            self._commands.put(cmd)

    def _unregister(self) -> None:
        if not self._active:
            return
        for hk_id in self._bindings:
            user32.UnregisterHotKey(None, hk_id)
        self._active = False
        self._live_ids = set()
        # The names go with the keys. `registered` is REPORTED - startup prints
        # it, the master switch prints it - and an entry left behind says the
        # program still holds a key it has just given back. That is the one
        # question the switch exists to answer, so the list is emptied here,
        # in the only place the keys are actually released (#134).
        self.registered = []
        # The poller must treat the CURRENT key state as its baseline after
        # a suspend/rebind: the key the user just pressed to remap (or is
        # still holding) is not a fresh press. Without the reset the poller
        # sees it as a new edge and fires the command the user just
        # reassigned (issue: remapping Divide to Num2 auto-executed the
        # action). The next tick re-baselines from the live state.
        self._poll_down = {}
        self._poll_last = {}

    def suspend(self) -> None:
        """Suspend the hotkeys: while the menu waits for a key, F8 must land
        in the field instead of toggling the menu."""
        if self._tid:
            user32.PostThreadMessageW(self._tid, MSG_SUSPEND, 0, 0)

    def resume(self) -> None:
        if self._tid:
            user32.PostThreadMessageW(self._tid, MSG_RESUME, 0, 0)

    def set_enabled(self, on: bool) -> None:
        """The master switch (#134): off releases every binding for good.

        Unlike suspend(), which the menu uses for the length of a rebind and
        always pairs with a resume(), this is a setting: it survives a
        resume() and stays off until the user turns it back on. The keys go
        back to every other program, which is the entire point of the
        request - the numpad belongs to the game, Blender or the calculator.

        Posted as a message like the rest: RegisterHotKey with hWnd=None is
        bound to the hotkey thread, so a flag set from here could not release
        anything, and the thread has to see the messages in the order the user
        produced them.

        Before the thread exists there is no queue to post to, and no keys to
        release either - startup calls this ahead of start() so the very first
        registration already honours the switch. The flag is written directly
        in that case, which is safe for the same reason: nothing is running
        yet to race with.
        """
        if not self._tid:
            self._enabled = bool(on)
            return
        self._enabled_done.clear()
        user32.PostThreadMessageW(self._tid, MSG_ENABLE, 1 if on else 0, 0)

    def wait_enabled(self, timeout: float = 0.5) -> bool:
        """Wait for the last set_enabled to be acted on; False on a timeout.

        Same reason as wait_rebound: `registered` and `failed` describe the
        new state only after the hotkey thread has handled the message, and
        that is what a caller reporting the switch back has to read.
        """
        return self._enabled_done.wait(timeout)

    def rebind(self, bindings: dict) -> None:
        """Replace the assignments on the fly, without restarting."""
        if not self._tid:
            return
        with self._lock:
            self._pending = {k: tuple(v) for k, v in bindings.items()}
        self._rebound.clear()
        user32.PostThreadMessageW(self._tid, MSG_REBIND, 0, 0)

    def wait_rebound(self, timeout: float = 0.5) -> bool:
        """Wait for the last rebind to be registered; False on a timeout.

        The registration happens on the hotkey thread, so `registered` and
        `failed` describe the new bindings only after this returns True. A
        combination another program already holds used to report "settings
        applied" and then simply never fire.
        """
        return self._rebound.wait(timeout)

    def stop(self) -> None:
        self._poll_stop.set()
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._tid = 0
