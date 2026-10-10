r"""The saved fisheye lens reaches the real program's worker at startup.

A worker starts without the lens - the stream header has no field for it, as
it has none for the pass count - so the client tells each new worker once,
keyed on its pid (main loop, commands.send_lens_state). This launches the
real program the way a user does, with the lens saved on in its config, and
checks that the worker reports the lens on and the pipeline keeps producing
frames with it. Ctrl+Alt+Q leaves nothing running.

Run:  runtime\python.exe tests\test_lens_app.py
"""
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import autocheck as ac  # noqa: E402
from paths import WORKER_EXE  # noqa: E402


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    busy = ac.running_instances()
    if busy:
        print(f"FAIL: NeuralScreen is already running ({busy}) - stop it first")
        return 1
    failures = []
    offset = ac.launch({"lens": True, "lens_fov": 120.0, "open_menu_on_start": False})
    try:
        text = ac.wait_for(offset, "[lens] on", 60.0)
        if text is None:
            failures.append("the worker never said the lens is on")
        else:
            line = next(l for l in text.splitlines() if "[lens] on" in l)
            print(f"    {line.strip()}")
            if "120-degree" not in line:
                failures.append(f"the worker runs another angle: {line.strip()}")
        time.sleep(4.0)
        stats = ac.nr_stats(ac.log_since(offset))
        if not stats or stats[-1][1] < 30:
            failures.append(f"too few frames with the lens on: {stats[-3:]}")
        else:
            print(f"    NR {stats[-1][0]:.1f} fps after {stats[-1][1]} frames with the lens on")
        for bad in ("[lens] target", "[lens] shader failed", "[lens] pipeline failed"):
            if bad in ac.log_since(offset):
                failures.append(f"the log says {bad!r}")
    finally:
        left = ac.quit_app()
        if left:
            failures.append(f"still running after Ctrl+Alt+Q: {left}")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the saved lens reaches the worker at startup and the pipeline runs with it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
