"""Every wire magic returns its OWN message number, and every one is handled.

Two independent lines of work both picked 11 for a new command. The dispatcher
is a chain of `if (msg == N)`, so only the FIRST branch on a number ever runs:
the second command was read, answered with the wrong ack, and the client that
sent it waited for a reply that could not arrive. It showed up as "the worker
did not answer within 8 s" in a test that looked like a recording bug.

The numbers are how the worker tells the two apart, so they are a contract of
the same kind as the magic values themselves - and a duplicate is invisible in
every other way: both commands parse their own payload correctly, both log
their own line, and the broken one simply never gets there.

Checked statically, on the source:
  1. no two `return N` inside ReadVideoMessage are equal (one number per magic);
  2. every number that a dispatcher branch handles is produced by
     ReadVideoMessage - a branch on a number nothing returns is dead code, so
     the command it belongs to can never run;
  3. the prepared-frame test names every non-frame command, so a new one does
     not silently invalidate the frame that is ready and waiting.

Run:  runtime\\python.exe tests\\test_wire_msg_numbers.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SOURCE = BASE / "native" / "dlss5-feed-host64.cpp"


def read_video_message_body(src: str) -> str:
    """The body of ReadVideoMessage, by brace matching from its definition."""
    start = src.index("static int ReadVideoMessage(")
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[brace:i + 1]
    raise AssertionError("ReadVideoMessage has no closing brace")


def main() -> int:
    failures = []
    src = SOURCE.read_text(encoding="utf-8", errors="replace")
    body = read_video_message_body(src)

    # 1. One number per magic: return 0 is the error path and repeats freely,
    #    every other number identifies a command and must be unique.
    returns = [int(n) for n in re.findall(r"return\s+(\d+)\s*;", body)]
    non_zero = [n for n in returns if n != 0]
    duplicates = sorted({n for n in non_zero if non_zero.count(n) > 1})
    if duplicates:
        failures.append(f"ReadVideoMessage returns the same number twice: "
                        f"{duplicates} - the later command can never run")
    print(f"    message numbers returned: {sorted(set(non_zero))}")
    print(f"    duplicates: {duplicates if duplicates else 'none'}")

    # 2. Every dispatcher branch number is produced by ReadVideoMessage, and
    #    no number has two branches. This is the check that would have caught
    #    the real bug: PPRM returned 11 and RECS handled 11, so the first
    #    branch swallowed the record command - both commands parsed their own
    #    payload correctly and only one of them could ever run.
    handled = [int(n) for n in re.findall(r"if\s*\(\s*msg\s*==\s*(\d+)\s*\)", src)]
    produced = set(non_zero)
    dead = sorted(set(handled) - produced - {0})
    if dead:
        failures.append(f"the dispatcher handles {dead} but ReadVideoMessage "
                        f"never returns them - those branches are dead code")
    twice = sorted({n for n in handled if n != 0 and handled.count(n) > 1})
    if twice:
        failures.append(f"two dispatcher branches handle the same number: "
                        f"{twice} - only the first one can ever run, and the "
                        f"command in the other one is silently unreachable")
    print(f"    dispatcher branches: {sorted(handled)}")
    print(f"    handled but never produced: {dead if dead else 'none'}")
    print(f"    numbers with two branches: {twice if twice else 'none'}")

    # 3. The prepared-frame test. A command that arrives on the hot path and
    #    changes only parameters must be named there, or the frame that is
    #    already prepared is thrown away on every one of them - which is a
    #    silent frame-rate loss, not an error.
    #
    #    The frames and the plumbing are the other way round and belong OUT of
    #    the list: 1 and 2 carry (or resize) the frame itself, and 3..9 are the
    #    capture/presentation setup commands, which legitimately invalidate
    #    what is ready. The parameter commands are pinned below, each with the
    #    reason it must stay - a new one that is not added to the code's list
    #    is exactly the bug this check exists for.
    PARAMETER_COMMANDS = {
        10: "CAPTURE: asks for pixels, does not touch the frame source",
        11: "RECS: starts a recording on the frame that is ready",
        12: "RECE: stops a recording",
        13: "PPRM: passes 2..N get their own parameters",
        14: "LENS: the fisheye lens over the output, applied after the network",
    }
    m = re.search(r"if \(msg != 1 && msg != 10[^)]*\) prepared = false;", src)
    if not m:
        failures.append("the prepared-frame test is gone or was rewritten - "
                        "it is what keeps a parameter command from dropping "
                        "the frame that is ready")
    else:
        named = {int(n) for n in re.findall(r"msg != (\d+)", m.group(0))}
        missing = sorted(set(PARAMETER_COMMANDS) - named)
        if missing:
            names = ", ".join(f"{n} ({PARAMETER_COMMANDS[n]})" for n in missing)
            failures.append(f"the prepared-frame test does not name {names} - "
                            f"the ready frame is wiped on every such command")
        print(f"    prepared-frame test names: {sorted(named)}")
        print(f"    parameter commands missing from it: "
              f"{missing if missing else 'none'}")

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nOK: every wire command has its own number, every branch has a "
          "producer, and every parameter command keeps the prepared frame")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
