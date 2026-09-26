"""A card that refused the neural pass is marked in the picker, not hidden.

DXGI reports some cards twice. One user has a single RTX 5080 listed as
adapters 0 and 2, and only one of the two can bring the neural pass up - the
other kills the worker. From outside the two entries are the same string, so
the menu was offering a choice between two identical lines, one of which
breaks the program (issue #33).

Merging them by name is not available: on a machine with two identical cards
that would remove a real choice. What IS known is what was tried, so that is
what the picker says - and it says it as a note, not a lock:

* an adapter whose switch was rolled back is remembered and marked;
* it stays selectable, and the mark is dropped the moment it does work
  (a driver update is the usual reason);
* the mark survives a restart, because it goes into the config;
* the index in the label is untouched, because that is what the action is
  parsed from.

The label expression itself is EXECUTED here, taken from `menu_payload`'s own
line in settings_io.py. The previous version rebuilt the string with its own
copy of the expression, so deleting `_no_nr(st)` from the product - or changing
its wording - still passed: it was testing its own concatenation.

Run:  runtime\\python.exe tests\\test_gpu_mark.py
"""
import ast
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import capture  # noqa: E402
import settings_io  # noqa: E402

FAKE = [(0, "NVIDIA GeForce RTX 5080"), (2, "NVIDIA GeForce RTX 5080")]


def _state(marked):
    return types.SimpleNamespace(cfg={"gpu_no_nr": list(marked)}, lang="en")


def picker_labels(st, adapters):
    """Run the product's own 'gpus' list comprehension.

    Taken from menu_payload's AST rather than re-typed: the node is located by
    the key it builds, so the wording, the key name and the _no_nr() call all
    come from the product. A change there changes what this returns.
    """
    tree = ast.parse((BASE / "app" / "settings_io.py").read_text(encoding="utf-8"))
    target = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "gpus":
                target = value
                break
        if target is not None:
            break
    if target is None:
        raise RuntimeError("menu_payload no longer builds a 'gpus' entry")

    expression = ast.Expression(body=target)
    ast.fix_missing_locations(expression)
    scope = {
        "list_adapters": lambda: adapters,
        "_no_nr": settings_io._no_nr,
        "UI_STRINGS": settings_io.UI_STRINGS,
        "st": st,
        "i": None, "name": None,
    }
    code = compile(expression, "<gpus-entry>", "eval")
    return eval(code, scope)  # noqa: S307 - the product's own expression


def main() -> int:
    failures = []
    real_capture = capture.list_adapters
    capture.list_adapters = lambda: FAKE
    settings_io.list_adapters = lambda: FAKE
    try:
        # 1. Nothing marked: two plain entries, and they are the two the
        #    machine has.
        st = _state([])
        labels = picker_labels(st, FAKE)
        plain = [f"{i}: {n}" for i, n in FAKE]
        if labels != plain:
            failures.append(f"with nothing marked the list changed: {labels}")

        # 2. Adapter 2 marked: it is still there, still second, still
        #    parseable as index 2 - with the product's own note.
        st = _state([2])
        labels = picker_labels(st, FAKE)
        print(f"    marked list: {labels}")
        if labels[0] != "0: NVIDIA GeForce RTX 5080":
            failures.append(f"the working card was marked too: {labels[0]!r}")
        if len(labels) != 2:
            failures.append(f"the refused card was hidden, not marked: {labels}")
        elif labels[1] == "2: NVIDIA GeForce RTX 5080":
            failures.append("the refused card carries no note at all")
        elif int(labels[1].split(":")[0]) != 2:
            failures.append("the note broke the index the action is parsed from")
        else:
            note = labels[1].split(" - ", 1)[1] if " - " in labels[1] else ""
            if not note:
                failures.append(f"the mark has no text: {labels[1]!r}")
            else:
                print(f"    note text: {note!r}")

        # 3. A card that works again loses the mark: the note is driven by the
        #    config set, so clearing it clears the label.
        st = _state([])
        labels = picker_labels(st, FAKE)
        if any(" - " in label for label in labels):
            failures.append(f"an unmarked card still carries a note: {labels}")

        # 4. The mark survives a round trip through the config payload.
        payload = settings_io._menu_layout_payload(
            {"profile": "Natural", "gpu": 0, "spout": False,
             "rec_indicator": True, "screenshot_dir": "", "presets": {},
             "gpu_no_nr": [2]},
            {"intensity": 1.0, "local_tone": 1.0, "local_structure": 1.0,
             "skin_structure": -1.0},
            0, "en", 0.65, 0.0, True, False,
            type("M", (), {"user_scale": 1.0, "user_height": None,
                           "state": {"theme": "light"}, "offset": [0, 0]})())
        if payload.get("gpu_no_nr") != [2]:
            failures.append(f"the mark does not survive a save: "
                            f"{payload.get('gpu_no_nr')!r}")

        # 5. Junk in the config does not take the menu down - the config is
        #    user-editable and this key is new.
        for junk in (None, "2", ["x"], 5):
            st = _state([])
            st.cfg["gpu_no_nr"] = junk
            try:
                settings_io._no_nr(st)
            except Exception as exc:
                failures.append(f"gpu_no_nr={junk!r} raised {exc!r}")
    finally:
        capture.list_adapters = real_capture
        settings_io.list_adapters = real_capture

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the refused card is marked by the product's own label, "
          "selectable, and remembered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
