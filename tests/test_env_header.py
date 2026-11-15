"""The environment header in the log: version, OS, HDR, driver.

Users paste NeuralScreen.log into issues; the [env] header answers the
questions we would otherwise have to ask (which version, which Windows,
is HDR on, which driver). Every probe is wrapped - a missing API or a
stripped system must not crash the startup, the line is simply skipped.

Checked: the header prints the version and the OS; the HDR line is
always present (on/off/unknown); a broken probe (monkeypatched to
raise) does not crash the caller.
"""
import importlib.util
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "app"))  # the modules live in app/
# main.py is a script with an entry point - import it under an explicit
# module name, otherwise `import main` binds the function main().
_spec = importlib.util.spec_from_file_location("ns_main", os.path.join(BASE, "main.py"))
ns_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ns_main)


def main() -> int:
    failures = []

    # 1. The header prints the version and the OS.
    import io
    buf = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = buf
    try:
        ns_main._log_environment({"lang": "en", "profile": "Natural",
                                  "work_scale": 0.65})
    finally:
        sys.stdout = real_stdout
    text = buf.getvalue()
    print(text.strip())
    if f"NeuralScreen {ns_main.APP_VERSION}" not in text:
        failures.append("the version is missing from the header")
    if "Windows" not in text:
        failures.append("the OS is missing from the header")
    if "HDR:" not in text:
        failures.append("the HDR line is missing")
    if "Num Lock:" not in text:
        failures.append("the Num Lock line is missing")

    # 1b. The switches line says what NR was set to. It read `_paused`, a key
    #     nothing writes any more, so `not cfg.get('_paused', False)` was
    #     always true and every session logged "NR on" - including the one in
    #     a reporter's package (issue #194) whose settings said nr_on: false
    #     while the next line said "NR starts off, as it was left".
    for nr_on, want in ((False, "NR off"), (True, "NR on")):
        buf = io.StringIO()
        sys.stdout = buf
        try:
            ns_main._log_environment({"lang": "en", "nr_on": nr_on,
                                      "frame_generation": False,
                                      "frame_multiplier": 2,
                                      "motion_backend": "nvofa"})
        finally:
            sys.stdout = real_stdout
        line = next((l for l in buf.getvalue().splitlines()
                     if "[env] switches:" in l), "")
        print(line.strip())
        if want not in line:
            failures.append(f"with nr_on={nr_on} the switches line is "
                            f"{line.strip()!r}, expected {want!r} - a package "
                            f"cannot say whether NR was on")

    # 2. A broken probe must not crash the caller: the probes after it still
    #    run. Two things were wrong with the old check (audit: WEAK):
    #    - it looked for "NeuralScreen", and that banner is printed BEFORE any
    #      probe, so it passed even if everything after it died;
    #    - it raised OSError, which the probe's OWN inner `except OSError`
    #      already catches - so the outer wrapper was never exercised at all.
    #    Raise something only the outer `except Exception` can catch, and read
    #    the LAST line, which only prints if every probe survived.
    class _ProbeBoom(RuntimeError):
        pass

    real_winreg = None
    try:
        import winreg
        real_winreg = winreg.OpenKey
        winreg.OpenKey = lambda *a, **k: (_ for _ in ()).throw(_ProbeBoom("boom"))
    except ImportError:
        pass
    buf2 = io.StringIO()
    sys.stdout = buf2
    raised = None
    try:
        ns_main._log_environment({})
    except Exception as exc:
        raised = exc
    finally:
        sys.stdout = real_stdout
        if real_winreg is not None:
            import winreg
            winreg.OpenKey = real_winreg
    text2 = buf2.getvalue()
    if raised is not None:
        failures.append(f"a raising winreg probe escaped the header "
                        f"({type(raised).__name__}: {raised}) - a user's log "
                        f"then stops mid-header")
    if "[env] Num Lock:" not in text2:
        failures.append("a raising winreg probe stopped the header - the "
                        "lines after it never printed")
    if "[env] NeuralScreen" not in text2:
        failures.append("the version banner itself was lost")

    # 3. Same guarantee for a different call: the point is the wrapper, not
    #    winreg.
    real_platform = None
    try:
        import platform as _plat
        real_platform = _plat.platform
        _plat.platform = lambda *a, **k: (_ for _ in ()).throw(_ProbeBoom("boom"))
    except Exception:
        pass
    buf3 = io.StringIO()
    sys.stdout = buf3
    raised = None
    try:
        ns_main._log_environment({"lang": "en"})
    except Exception as exc:
        raised = exc
    finally:
        sys.stdout = real_stdout
        if real_platform is not None:
            import platform as _plat
            _plat.platform = real_platform
    text3 = buf3.getvalue()
    if raised is not None:
        failures.append(f"a raising platform probe escaped the header "
                        f"({type(raised).__name__}: {raised})")
    if "[env] Num Lock:" not in text3:
        failures.append("a raising platform probe stopped the header")

    print("=" * 60)
    if failures:
        print(f"FAIL: {len(failures)} - {failures}")
        return 1
    print("OK: the environment header carries version/OS/HDR and survives broken probes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
