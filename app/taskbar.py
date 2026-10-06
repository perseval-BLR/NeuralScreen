"""TaskbarWindow - a taskbar button for the program.

The overlay is a borderless click-through window and the worker window is
a tool window, so neither shows in the taskbar - the program lived only
in the tray. A taskbar button is what users expect from a desktop app
(user rule 2026-09-09: "всегда отображалась в панели задач а не только
в трее").

The button is a real top-level window with WS_EX_APPWINDOW: a 1x1 visible
window parked at the corner of the screen. Clicking its taskbar button
activates it; the window procedure turns that into an idempotent menu-show
command. The tray/hotkey retain their useful toggle semantics, but duplicate
taskbar activation must never close a visible menu. The window itself never
shows anything.

The icon comes from native/neuralscreen.ico (the same one the launcher
uses), so the taskbar button looks like the program.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import queue
import threading
import time

from paths import BASE_DIR

# Private library instances, not ctypes.windll.*. The function objects of
# windll.user32 are shared by every module in the process, and pystray declares
# its own prototypes on them when the tray starts: CreateWindowExW with the
# class name as an ATOM, DefWindowProcW returning a DWORD (an LRESULT cut to
# 32 bits), GetModuleHandleW with an errcheck. Whichever module declared last
# decided how the calls below were marshalled - and the handles came back as a
# C int without a restype at all: GetModuleHandleW's 64-bit image base was
# truncated before it reached RegisterClassW and CreateWindowExW. Every
# prototype this module relies on is stated here, on its own instances.
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
LRESULT = ctypes.c_ssize_t
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE,
                                   wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.DestroyWindow.argtypes = [wt.HWND]
user32.DestroyWindow.restype = wt.BOOL
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.FindWindowW.restype = wt.HWND
user32.FindWindowExW.argtypes = [wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR]
user32.FindWindowExW.restype = wt.HWND
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wt.HWND
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.GetCursorPos.restype = wt.BOOL
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.IsIconic.argtypes = [wt.HWND]
user32.IsIconic.restype = wt.BOOL
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindow.restype = wt.BOOL
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL
user32.LoadCursorW.argtypes = [wt.HINSTANCE, wt.LPVOID]   # a MAKEINTRESOURCE id
user32.LoadCursorW.restype = wt.HANDLE
user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int,
                              ctypes.c_int, wt.UINT]
user32.LoadImageW.restype = wt.HANDLE
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendMessageW.restype = LRESULT
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostQuitMessage.restype = None
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.GetMessageW.restype = wt.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.TranslateMessage.restype = wt.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
user32.RegisterWindowMessageW.restype = wt.UINT
user32.ChangeWindowMessageFilterEx.argtypes = [wt.HWND, wt.UINT, wt.DWORD, wt.LPVOID]
user32.ChangeWindowMessageFilterEx.restype = wt.BOOL
kernel32.CreateMutexW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]
kernel32.CreateMutexW.restype = wt.HANDLE

WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_EX_APPWINDOW = 0x00040000
WM_ACTIVATE = 0x0006
WM_NCACTIVATE = 0x0086
WA_CLICKACTIVE = 0x2
WA_ACTIVE = 0x1
WM_SYSCOMMAND = 0x0112
#: What copies of the program before this one listen for. It is not in the
#: WM_APP range its comment claimed - 0x8000 + 0x4E53 = 0xCE53, and WM_APP
#: ends at 0xBFFF - but in the range RegisterWindowMessage hands out, where
#: another program's broadcast message could carry the same number and open
#: the panel on its own. Only posted now, for an older copy still running.
WM_NS_SHOW_LEGACY = 0x8000 + 0x4E53


def _registered_message(name: str) -> int:
    """The number every process in the session gets for `name`."""
    try:
        return int(user32.RegisterWindowMessageW(name))
    except Exception:
        return 0


#: Posted by a second copy of the program that was just started: "you are
#: already running - show yourself". A registered message: every process gets
#: the same number for the same name, and no other program's can have it.
#: The WM_APP number stands in if registration ever fails.
WM_NS_SHOW = _registered_message("NeuralScreen.ShowSettings") or 0x8000 + 0x3E53
ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183
MSGFLT_ALLOW = 1
SINGLE_INSTANCE_MUTEX = "NeuralScreen_SingleInstance"


def _last_error() -> int:
    """The error the last call on our private kernel32/user32 left."""
    return ctypes.get_last_error()


def claim_single_instance(name: str = SINGLE_INSTANCE_MUTEX):
    """Create the single-instance mutex: (handle, another copy is running).

    The handle has to be kept for the life of the process - the mutex dies
    with the last handle, which is what lets a crashed copy not block the
    next launch.

    ERROR_ALREADY_EXISTS is the usual answer of a second copy. A copy
    started as administrator creates the mutex with an elevated security
    descriptor, and then a non-elevated launch cannot open it at all:
    CreateMutexW returns NULL with ERROR_ACCESS_DENIED - and that copy used
    to take the NULL as "first" and start next to the running one. Both
    answers mean another copy holds the name.
    """
    handle = kernel32.CreateMutexW(None, False, name)
    error = _last_error()
    return handle, error in (ERROR_ALREADY_EXISTS, ERROR_ACCESS_DENIED)


def allow_show_message(hwnd) -> bool:
    """Let WM_NS_SHOW through UIPI to `hwnd`, whatever the sender's level.

    A running copy started as administrator is a high-integrity window, and
    Windows drops messages posted to it from a normal-integrity process - the
    second copy's "show yourself" never arrived, and the user saw nothing.
    Only the current number: the legacy one is not accepted by this window
    anyway (see WM_NS_SHOW_LEGACY).
    """
    try:
        return bool(user32.ChangeWindowMessageFilterEx(hwnd, WM_NS_SHOW,
                                                       MSGFLT_ALLOW, None))
    except Exception:
        return False


SC_MINIMIZE = 0xF020
SC_RESTORE = 0xF120
SC_CLOSE = 0xF060
WM_QUIT = 0x0012
WM_SETICON = 0x0080
ICON_SMALL = 0
ICON_BIG = 1
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    """WNDCLASSW - not in ctypes.wintypes, defined here."""
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class TaskbarWindow:
    """The taskbar button: a 1x1 APPWINDOW window that asks to show the menu.

    It has a command separate from the tray/hotkey toggle: Windows can deliver
    more than one activation message for a click, and turning a visible menu
    into a hidden one was indistinguishable from the overlay disappearing by
    itself (issue #87).
    """

    #: Policy, set by main from the config. The window procedure runs on its
    #: own thread and must not reach into the config, so the two answers are
    #: pushed to it instead of pulled.
    to_tray_on_minimise = False
    to_tray_on_close = False

    def __init__(self, commands: queue.Queue, title: str = "NeuralScreen"):
        self._commands = commands
        self._title = title
        self._hwnd = None
        self._thread: threading.Thread | None = None
        self._proc = WNDPROC(self._wnd_proc)
        self._last_cmd = 0.0
        # Did WE hold the foreground just before this message? A click on our
        # already-active button and the fallback activation that follows a
        # foreign window being minimised arrive as the same WM_NCACTIVATE(1) -
        # measured on the bench - so the state around the message is what tells
        # them apart (#96).
        self._was_active = False
        # The previous foreground window - the discriminator #96 relies on.
        # It cannot come from lParam: WM_NCACTIVATE carries 0 there, so the
        # state has to be sampled while the message is NOT arriving. The same
        # probe that solved #96 (`_work/probe_taskbar_activation.py`) samples it
        # the same way, at 50 Hz.
        #
        # Why it is needed at all: on Win11 a click on our NOT-yet-active
        # button arrives as WM_NCACTIVATE(1) with `_was_active` still False -
        # measured on the bench, `was=False cursorTb=True fgOurs=True` - and the
        # old guard required `_was_active`, so THE FIRST CLICK WAS DROPPED and
        # only the second (SC_MINIMIZE, unconditional) worked. Reported as
        # "the first click does nothing, the second expands it".
        #
        # The minimised previous window is what separates a real click from the
        # fallback activation Windows sends when another window is minimised
        # (#96: the menu opened by itself). A real click leaves that window
        # alive; the fallback leaves it minimised.
        self._prev_hwnd = 0
        self._prev_iconic = False
        self._prev_stop = threading.Event()
        self._prev_thread: threading.Thread | None = None

    def _wnd_proc(self, hwnd, msg, wparam, lparam) -> int:
        if msg == WM_NS_SHOW:
            self._emit("show_settings")
            return 0
        if msg == WM_ACTIVATE:
            # A click on the taskbar button arrives as WA_ACTIVE here (not
            # WA_CLICKACTIVE - measured on Win11 26200). System activations
            # (another window minimized/closed, Alt+Tab, Win+D) arrive the
            # same way, so wparam alone cannot tell them apart - and neither
            # can the cursor position alone, because switching to another
            # app also happens with the cursor over the taskbar (#93). The
            # full test lives in _is_user_click.
            if self._is_user_click(wparam, lparam or 0):
                self._emit("show_settings")
            self._was_active = wparam in (WA_ACTIVE, WA_CLICKACTIVE)
            return 0
        if msg == WM_NCACTIVATE:
            # The click on an ALREADY-active taskbar button arrives as
            # WM_NCACTIVATE(WA_ACTIVE), not WM_ACTIVATE (measured on Win11
            # 26200: the button click delivered 0x86 wp=1 with the cursor
            # over the taskbar and nothing else). Without handling it the
            # second click was dead (user: "залипает"). wparam=0 is a
            # deactivation - never a user click, ignore it.
            #
            # Our window must still be the foreground one: when the user
            # clicks another app's icon, this message arrives for that
            # activation too, and showing our menu then is the reported
            # bug (#93).
            #
            # AND the previous window must not be MINIMISED: when the previous
            # foreground window is minimised, Windows activates us as a
            # fallback and this same message arrives - the menu opening by
            # itself while the user only touched another program (#96).
            #
            # `_was_active` used to be the whole guard here, and it dropped the
            # FIRST click on a not-yet-active button: measured on the bench,
            # `msg=0x0086 wp=1 cursorTb=True fgOurs=True was=False` for the
            # first click, with the menu staying shut, and `SC_MINIMIZE` only on
            # the second. The previous window's state separates the two cases
            # where `_was_active` could not: a real click leaves it alive, the
            # fallback leaves it minimised.
            if wparam == 1 and not self._prev_minimised() and \
                    self._cursor_over_taskbar() and self._is_foreground_ours():
                self._emit("show_settings")
            self._was_active = bool(wparam)
            return 0
        if msg == WM_SYSCOMMAND and (wparam & 0xFFF0) == SC_MINIMIZE \
                and self.to_tray_on_minimise:
            # "Minimise to tray" (#93): the button goes away and the program
            # lives in the tray until it is asked back. The 1x1 window still
            # must not be minimised by the system - a minimised window keeps
            # its taskbar button, which is the thing being removed - so it is
            # HIDDEN instead, and the tray icon becomes the way back.
            self._emit("to_tray")
            return 0
        if msg == WM_SYSCOMMAND and (wparam & 0xFFF0) in (SC_MINIMIZE, SC_RESTORE):
            # The taskbar button sends these when the window is already
            # minimized (restore) or when the user asks to minimize it. The
            # 1x1 window must NEVER actually minimize: the taskbar button
            # disappears with it. So SC_MINIMIZE/SC_RESTORE are turned into
            # the same idempotent menu show instead of letting the system
            # minimize the window (user: the button stopped responding on the
            # second click).
            self._emit("show_settings")
            return 0
        if msg == WM_SYSCOMMAND and (wparam & 0xFFF0) == SC_CLOSE:
            # 'Close window' in the thumbnail's right-click menu destroys
            # the 1x1 window and the taskbar button is gone for the session.
            # The user must quit through the tray (Exit) - ignore SC_CLOSE
            # (audit 10.09 F3).
            #
            # With "close to tray" on, the same click means something the user
            # asked for: put the program in the tray. It still does not destroy
            # the window - hiding it keeps the button recoverable, which
            # destroying never was (#93).
            if self.to_tray_on_close:
                self._emit("to_tray")
            return 0
        if msg == WM_QUIT:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def set_visible(self, visible: bool) -> None:
        """Show or hide the taskbar button, without destroying the window.

        SW_SHOWNA rather than SW_SHOW: the button comes back without taking
        the focus, which is the whole point of a program that draws over
        somebody else's full-screen game.
        """
        hwnd = self._hwnd
        if not hwnd:
            return
        SW_HIDE, SW_SHOWNA = 0, 8
        try:
            user32.ShowWindow(hwnd, SW_SHOWNA if visible else SW_HIDE)
        except Exception as exc:
            print("[main] taskbar button: cannot %s it (%s)"
                  % ("show" if visible else "hide", exc))

    def _cursor_over_taskbar(self) -> bool:
        """Whether the cursor is inside a taskbar rectangle.

        The primary taskbar is Shell_TrayWnd; on Windows 11 a secondary
        monitor's taskbar is a separate top-level window, Shell_SecondaryTrayWnd
        - both are checked (audit 10.09 F1: multi-monitor users could not use
        the button on the second screen).
        """
        try:
            pt = wt.POINT()
            if not user32.GetCursorPos(ctypes.byref(pt)):
                return False
            # Every taskbar, not the first of each class: with three monitors
            # or more there is one Shell_SecondaryTrayWnd per extra screen,
            # and FindWindowW only ever returned one of them - a click on our
            # button on the others failed this test.
            for cls in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
                tb = user32.FindWindowExW(None, None, cls, None)
                while tb:
                    rect = wt.RECT()
                    if (user32.GetWindowRect(tb, ctypes.byref(rect))
                            and rect.left <= pt.x < rect.right
                            and rect.top <= pt.y < rect.bottom):
                        return True
                    tb = user32.FindWindowExW(None, tb, cls, None)
            return False
        except Exception:
            return False

    def _is_foreground_ours(self) -> bool:
        """Whether our own 1x1 window currently owns the foreground.

        Used together with the cursor test: a click on another application's
        taskbar icon also leaves the cursor over the taskbar, so the
        foreground window is what tells the two apart (#93).
        """
        try:
            fg = user32.GetForegroundWindow()
            return bool(fg) and self._hwnd is not None and fg == self._hwnd
        except Exception:
            return False

    def _sample_previous(self) -> None:
        """Remember the last foreground window that was NOT ours.

        Sampled while no message is being handled, because WM_NCACTIVATE's
        lParam is 0 and the state around the message is the only thing that
        separates a click on our button from the fallback activation Windows
        hands us after another window is minimised (#96). A real click leaves
        the previous window alive; the fallback leaves it minimised.
        """
        while not self._prev_stop.is_set():
            try:
                fg = user32.GetForegroundWindow() or 0
                if fg and fg != self._hwnd:
                    self._prev_hwnd = fg
                    self._prev_iconic = bool(user32.IsIconic(fg))
            except Exception:
                pass
            self._prev_stop.wait(0.02)          # 50 Hz, as in the probe

    def _prev_minimised(self) -> bool:
        """Is the previous foreground window minimised RIGHT NOW?

        Read live, not from the sampler's cache. The cache is sampled at 50 Hz,
        so for the #96 fallback it holds the state from BEFORE the minimise:
        the user minimises a window, Windows activates us, and a cached reading
        still says "alive" - the guard then lets the fallback through and the
        menu opens by itself (caught by test_taskbar_foreign_minimize, step 1).
        The window is still valid at message time, so its state is readable
        then, and that is the moment the decision is about.
        """
        try:
            hwnd = self._prev_hwnd
            if not hwnd or not user32.IsWindow(hwnd):
                return False
            return bool(user32.IsIconic(hwnd))
        except Exception:
            return False

    def _is_user_click(self, wparam: int, deactivated: int = 0) -> bool:
        """Whether this activation is a click on OUR taskbar button.

        wparam alone cannot say it: Windows reports a click on our button and
        a system activation (another window minimized, Alt+Tab, Win+D) the
        same way (WA_ACTIVE). The cursor-over-taskbar test separates those,
        but it is not enough on its own: clicking ANOTHER app's taskbar icon
        also happens with the cursor over the taskbar, and the menu used to
        pop up when the user was just switching programs (issue #93, user
        Saymoin: "the mouse is on the taskbar, expanding any minimized
        application").

        The distinguishing fact is WHICH window ends up in the foreground:
        a click on our button activates our own 1x1 window, while a click on
        a foreign icon activates that application. So a WA_ACTIVE activation
        counts only when our window is still the foreground one AND the
        cursor is over the taskbar. WA_CLICKACTIVE stays an unconditional
        click (Windows sends it only for a real click on this window).

        The window that LOST the activation arrives in lParam, and a
        MINIMISED one means this is the fallback activation Windows hands us
        after another window is minimised - not a click (#96, measured: the
        real fallback carries a minimised window, a real click does not).
        """
        if wparam == WA_CLICKACTIVE:
            return True
        if wparam != WA_ACTIVE or not self._cursor_over_taskbar():
            return False
        if deactivated and user32.IsWindow(deactivated) and \
                user32.IsIconic(deactivated):
            return False                       # the previous window went away
        # A different application took the foreground: this was a click on
        # ITS icon, not on ours.
        return self._is_foreground_ours()

    def _emit(self, command: str) -> None:
        """Queue a command, deduped: one click can deliver both
        WA_CLICKACTIVE and WA_ACTIVE, and SC_RESTORE may follow a click."""
        now = time.monotonic()
        if now - self._last_cmd > 0.5:
            self._last_cmd = now
            try:
                self._commands.put(command)
            except Exception:
                pass

    def start(self) -> None:
        """Create the window in its own thread (the message loop blocks)."""
        if self._thread is not None:
            return
        # The previous-foreground sampler: WM_NCACTIVATE carries no lParam, so
        # the state that separates a click from the #96 fallback has to be
        # sampled continuously, not read from the message.
        if self._prev_thread is None:
            self._prev_stop.clear()
            self._prev_thread = threading.Thread(
                target=self._sample_previous, daemon=True,
                name="taskbar-prev")
            self._prev_thread.start()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="taskbar")
        self._thread.start()

    def _run(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        cls = "NeuralScreenTaskbar"
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = cls
        wc.hCursor = user32.LoadCursorW(None, 32512)  # IDC_ARROW
        if not user32.RegisterClassW(ctypes.byref(wc)):
            # Already registered (a second instance in the same process).
            pass
        # A 1x1 window at the corner: visible to the system (so the
        # taskbar button exists) but nothing the eye can catch. The caption
        # style bits matter: without WS_CAPTION/WS_SYSMENU/WS_MINIMIZEBOX
        # the taskbar button has no minimize behaviour at all - clicking an
        # ALREADY-active button sends nothing (no WM_ACTIVATE, no
        # SC_MINIMIZE), which made the second click dead (user: "залипает").
        # With the styles the system sends SC_MINIMIZE on the active button,
        # which the window procedure converts into the menu toggle.
        self._hwnd = user32.CreateWindowExW(
            WS_EX_APPWINDOW, cls, self._title,
            WS_POPUP | WS_VISIBLE | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX,
            0, 0, 1, 1, None, None, hinst, None)
        if not self._hwnd:
            return
        # CreateWindowExW may drop WS_VISIBLE for a popup with caption styles
        # until the first ShowWindow - force it, or the taskbar button never
        # appears (measured: window came up hidden without it).
        #
        # SW_SHOWNA, not SW_SHOW: showing it must NOT activate it. With
        # SW_SHOW this 1x1 window became the FOREGROUND window at startup,
        # and the WM_NCACTIVATE/WA_ACTIVE pair that follows is exactly what a
        # click on our own taskbar button looks like - so the guards let it
        # through whenever the cursor happened to rest over the taskbar, and
        # the menu opened by itself on launch (same family as #96, measured
        # on the bench: 4/4 launches with the cursor over the taskbar, 0/4
        # with it anywhere else). The same reasoning already applies to
        # set_visible() below; it was simply missed here.
        user32.ShowWindow(self._hwnd, 8)  # SW_SHOWNA - show, do not activate
        self._set_icon(hinst)
        allow_show_message(self._hwnd)
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.DestroyWindow(self._hwnd)
        self._hwnd = None

    def _set_icon(self, hinst) -> None:
        """The launcher's icon, so the taskbar button looks like the app."""
        ico = BASE_DIR / "native" / "neuralscreen.ico"
        if not ico.is_file():
            return
        # No module handle with LR_LOADFROMFILE: the image is a file, and
        # an instance handle there asks for a resource inside that module.
        hicon = user32.LoadImageW(None, str(ico), IMAGE_ICON, 32, 32,
                                  LR_LOADFROMFILE)
        if hicon:
            user32.SendMessageW(self._hwnd, WM_SETICON, ICON_SMALL, hicon)
            user32.SendMessageW(self._hwnd, WM_SETICON, ICON_BIG, hicon)

    def stop(self) -> None:
        """Close the window and join the thread."""
        self._prev_stop.set()
        if self._prev_thread is not None:
            self._prev_thread.join(timeout=1.0)
            self._prev_thread = None
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    @property
    def hwnd(self):
        return self._hwnd
