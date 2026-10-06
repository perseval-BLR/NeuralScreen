r"""An RTSS hook in our processes is named in the log (#143).

The report (Inconsumable, #143): NR and FG both pinned at 30 FPS on 2.1.9,
the panel's frame limit doing nothing - and the cap gone once MSI Afterburner
/ RivaTuner were closed. RTSS injects RTSSHooks64.dll into a Direct3D process
and limits it inside Present; the worker presents everything through one
swap chain, so the cap hits NR and FG alike and our own pacing never sees it.

What this locks, against real processes and the real module walk: a process
that has a module named RTSSHooks64.dll loaded is reported, once, with what it
does to us; a process without it is not; and the worker side is walked too.

The "hook" is a harmless DLL from this interpreter copied under that name -
the detection is by module name, which is what RTSS's hook is known by.

Run:  runtime\python.exe tests\test_foreign_hooks.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import foreign_hooks  # noqa: E402

CHILD = r"""
import ctypes, sys, time
if len(sys.argv) > 1:
    ctypes.WinDLL(sys.argv[1])
sys.stdout.write("ready\n"); sys.stdout.flush()
time.sleep(30)
"""


def _child(dll: str | None) -> subprocess.Popen:
    args = [sys.executable, "-c", CHILD] + ([dll] if dll else [])
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, text=True)
    proc.stdout.readline()
    return proc


def _stop(proc: subprocess.Popen) -> None:
    proc.kill()
    proc.wait(timeout=10)


def main() -> int:
    failures = []
    tmp = Path(tempfile.mkdtemp(prefix="ns-hooks-"))
    donor = Path(sys.executable).with_name("python3.dll")
    fake = tmp / "RTSSHooks64.dll"
    shutil.copyfile(donor, fake)

    hooked = _child(str(fake))
    plain = _child(None)
    try:
        if "rtsshooks64.dll" not in foreign_hooks.find(hooked.pid):
            failures.append("a process with RTSSHooks64.dll loaded was not "
                            "reported")
        if foreign_hooks.find(plain.pid):
            failures.append(f"a clean process was reported as hooked: "
                            f"{foreign_hooks.find(plain.pid)}")

        # Through the call the frame loop makes: the worker is walked, the
        # line names the hook and what it does, and it is said once.
        # The frame loop's own state object: __slots__ turns a field it does
        # not declare into an AttributeError, which took the app down at its
        # first housekeeping tick before this test used it.
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        import main as main_mod
        lines = []
        st = main_mod._Pipeline()
        st.worker = hooked
        foreign_hooks.check(st, log=lines.append)
        st._foreign_hooks_due = 0.0          # the next housekeeping tick
        foreign_hooks.check(st, log=lines.append)
        named = [line for line in lines if "rtsshooks64.dll" in line
                 and "worker" in line]
        if len(named) != 1:
            failures.append(f"the worker's hook was named {len(named)} times, "
                            f"not once: {lines}")
        elif "frame limit" not in named[0]:
            failures.append(f"the line does not say what the hook does: "
                            f"{named[0]!r}")

        lines.clear()
        st = main_mod._Pipeline()
        st.worker = plain
        foreign_hooks.check(st, log=lines.append)
        if any("worker" in line for line in lines):
            failures.append(f"a clean worker was named: {lines}")
    finally:
        _stop(hooked)
        _stop(plain)
        for _ in range(20):
            try:
                shutil.rmtree(tmp)
                break
            except OSError:
                time.sleep(0.1)

    # The frame loop calls this twice a second for the whole session: a clean
    # worker must stop being walked, and a walk that raises must not escape
    # into the loop (main.py has no try around its housekeeping).
    import main as main_mod
    calls = []
    real_find = foreign_hooks.find
    foreign_hooks.find = lambda pid: calls.append(pid) or []
    try:
        st = main_mod._Pipeline()
        st.worker = SimpleNamespace(pid=424242, poll=lambda: None)
        for _ in range(40):
            st._foreign_hooks_due = 0.0
            foreign_hooks.check(st, log=lambda line: None)
        walked = calls.count(424242)
        if walked > foreign_hooks.MAX_WALKS:
            failures.append(f"a clean worker was walked {walked} times in 40 "
                            f"ticks - the walk never stops on a machine "
                            f"without RTSS")
        foreign_hooks.find = lambda pid: 1 / 0
        st = main_mod._Pipeline()
        st.worker = SimpleNamespace(pid=434343, poll=lambda: None)
        try:
            foreign_hooks.check(st, log=lambda line: None)
        except Exception as exc:
            failures.append(f"a failing walk escaped into the frame loop: "
                            f"{type(exc).__name__}")
    finally:
        foreign_hooks.find = real_find

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: an RTSS hook in the worker is named once, with what it does (#143)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
