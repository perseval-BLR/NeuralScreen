r"""NS_GPU_PRIORITY sets the worker's GPU scheduling class, and says so (#142).

#142 (OnErrorResumeNextAndBurn, RTX 4060 Ti): with an uncapped GPU-heavy game
in the foreground, NR fell to ~1 FPS - the network's GPU time grew to 200-650
ms while its CPU side stayed under 1 ms, and Alt-Tab alone brought it back.
The reporter suspected the command queue's priority; that ranks queues inside
one process and is ignored under hardware scheduling. The process's GPU
scheduling class is the lever across processes, and it is shipped as an
opt-in experiment until it is measured on such a machine.

What this locks, on the real worker: with NS_GPU_PRIORITY=high the worker
sets its own class and logs what the OS reads back; an unknown value is named
and ignored; and without the variable nothing is touched.

The reporter's A/B (06.10) showed `high` changes nothing under hardware GPU
scheduling (HAGS), where evaluation waited hundreds of ms. Two more levers
are offered for the next run, both opt-in: NS_GPU_PRIORITY=realtime (the
realtime class, which needs NeuralScreen started as administrator) and
NS_GPU_QUEUE_PRIORITY=high|realtime (the priority of the D3D12 queue the
network runs on; realtime needs the same rights). Each says what it got, and
a queue the system refuses falls back to a normal one rather than failing.

Run:  runtime\python.exe tests\test_gpu_priority_env.py
"""
import os
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

from paths import WORKER_EXE  # noqa: E402

PARAMS = {"style": 1, "auto_mask": 0, "intensity": 1.0, "local_tone": 0.5,
          "local_structure": 1.0, "skin_structure": -1.0}


def _run(value: str | None, name: str = "NS_GPU_PRIORITY") -> list[str]:
    from pipeline import shutdown_worker, start_worker
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    try:
        worker, logs, reader, stop = start_worker(PARAMS, 640, 360, 0, 0, 0, None)
        try:
            ok, _ngx, _category = reader.wait_create_ack(60)
            if not ok:
                return ["create failed"] + [ln for ln in logs if "[gpu]" in ln]
        finally:
            shutdown_worker(worker, stop)
    finally:
        os.environ.pop(name, None)
    return [ln for ln in logs if "[gpu]" in ln]


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    failures = []

    lines = _run("high")
    said = [ln for ln in lines if "GPU scheduling priority high" in ln]
    if not said:
        failures.append(f"NS_GPU_PRIORITY=high left no line saying what "
                        f"happened: {lines}")
    else:
        m = re.search(r"reads back (-?\d+)", said[0])
        if not m:
            failures.append(f"the line does not carry the read-back: {said[0]!r}")
        elif m.group(1) != "4" and "not applied" not in said[0]:
            failures.append(f"a class that did not take was not called out: "
                            f"{said[0]!r}")
        else:
            print(f"    {said[0].strip()}")

    lines = _run("realtime")
    said = [ln for ln in lines if "GPU scheduling priority realtime" in ln]
    m = re.search(r"reads back (-?\d+)", said[0]) if said else None
    if not m:
        failures.append(f"NS_GPU_PRIORITY=realtime was not applied and reported: {lines}")
    elif m.group(1) != "5" and "not applied" not in said[0]:
        failures.append(f"a realtime class that did not take was not called out: {said[0]!r}")
    else:
        print(f"    {said[0].strip()}")

    for level in ("high", "realtime"):
        lines = _run(level, "NS_GPU_QUEUE_PRIORITY")
        if "create failed" in lines:
            failures.append(f"NS_GPU_QUEUE_PRIORITY={level} broke the worker: {lines}")
        said = [ln for ln in lines if "command queue priority" in ln]
        if not said:
            failures.append(f"NS_GPU_QUEUE_PRIORITY={level} left no line saying what "
                            f"the queue got: {lines}")
        else:
            print(f"    {said[0].strip()}")

    lines = _run("ludicrous")
    if not any("ludicrous" in ln and "ignored" in ln for ln in lines):
        failures.append(f"an unknown value was not named and ignored: {lines}")

    lines = _run(None)
    if lines:
        failures.append(f"without NS_GPU_PRIORITY the worker still touched "
                        f"its class: {lines}")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: NS_GPU_PRIORITY sets the worker's GPU class and says what the "
          "OS reads back (#142)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
