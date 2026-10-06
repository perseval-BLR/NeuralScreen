"""Audit F4: a user revive must cancel the armed auto-revive.

Worker fails transiently -> worker_failed=True, next_auto_revive armed
(+30 s), overlay hidden. The user presses Num1 within the window: the
toggle path revives the worker itself, but next_auto_revive stays armed.
If the revived worker's first frame does not arrive inside 30 s, the
top-of-loop auto-revive fires AGAIN on top of the live worker - the
double-init hazard restart_worker's docstring warns about - with a
second "NR ON" alert. In its failure branch, paused stays as the user
set it (False), so the HUD/tray then claim NR ON with no worker.

Expected: at most one revive per failure; a successful user revive
disarms the pending auto-revive. [audit F4]

Both halves are parsed, not sliced by a character count. The old version took
a 1200-character window after a log string, and `st.worker_failed = True` in
the handler it was looking at sits 1396 characters past that anchor - so the
first conjunct was always False, the whole condition was dead, and deleting
`st.paused = True` (the desync the test exists for) still passed.

Run:  runtime\\python.exe tests\\test_revive_race.py
"""
import ast
import sys
from pathlib import Path


def _repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "main.py").is_file():
            return p
    return start


BASE = _repo_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/


def _handler_after_try(tree):
    """The handler of the INNERMOST try that restarts the worker.

    The first match is not enough: main() wraps its whole loop in a try, and
    that outer handler was picked instead of the auto-revive one. The block we
    want starts with require_compatibility and contains restart_worker followed
    by the forget_* resets, so the innermost such try is selected by depth.
    """
    candidates = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try) or not node.handlers:
            continue
        calls = [ast.unparse(n.func) for n in ast.walk(node)
                 if isinstance(n, ast.Call)]
        if not any("restart_worker" in name for name in calls):
            continue
        if not any("forget_verdict" in name for name in calls):
            continue
        first = ast.unparse(node.body[0]) if node.body else ""
        if "require_compatibility" not in first:
            continue
        # The two restart branches have the same shape now (their failures
        # are contained too); the auto-revive's handler is the one that says
        # so in its own log line.
        if "auto-revive" not in ast.unparse(node.handlers[0]):
            continue
        candidates.append(node)
    if not candidates:
        return None, ""
    # Innermost = the one with the largest line number for its `try`.
    node = max(candidates, key=lambda n: n.lineno)
    handler = node.handlers[0]
    return handler, "\n".join(ast.unparse(s) for s in handler.body)


def main() -> int:
    failures = []

    # 1. Source-level pin: the toggle's revive path must touch next_auto_revive.
    src = (BASE / "app" / "commands.py").read_text(encoding="utf-8")
    start = src.find('elif cmd == "toggle":')
    end = src.find('elif cmd ==', start + 1)
    block = src[start:end if end > 0 else start + 4000]
    if start < 0:
        failures.append("the toggle command is gone from commands.py - "
                        "re-check this test's premise")
    elif "next_auto_revive" not in block:
        failures.append(
            "F4: the toggle revive block never touches st.next_auto_revive - "
            "a user revive leaves the auto-revive armed; 30 s later it "
            "restarts the (possibly already healthy) worker again")

    # 2. The auto-revive's own failure branch must not leave NR state lying.
    #    Parsed: find the try whose body restarts the worker and forgets the
    #    verdict, then read its handler's body.
    main_src = (BASE / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(main_src)
    handler, body = _handler_after_try(tree)
    if handler is None:
        failures.append("could not locate the auto-revive try/handler in "
                        "main.py - re-check this test's premise")
    else:
        print(f"    auto-revive handler at main.py:{handler.lineno}")
        if "worker_failed" not in body:
            failures.append(
                "F4: the auto-revive failure branch no longer re-arms "
                "worker_failed - a failed revive would be forgotten")
        needs_state_fix = ("paused" not in body
                           and "next_auto_revive" not in body)
        if needs_state_fix:
            failures.append(
                "F4: the auto-revive failure branch sets worker_failed but "
                "leaves paused=False - the HUD shows NR ON with no worker")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the revive paths cannot double-fire or desync NR state")
    return 0


if __name__ == "__main__":
    sys.exit(main())
