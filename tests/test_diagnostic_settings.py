"""The bundle must say how the program was configured, and the log must date it.

Four support packages (19.09) could not answer the questions they were sent to
answer:

* `diagnostics.json` carried no configuration at all - "No configuration or
  environment dump is collected" - so a report could not say whether frame
  generation was on, at which multiplier, or which motion backend was running.
  Every evidence table for those packages ends with a list of settings that had
  to be asked for by hand;
* the log header carried a time of day but no date, so a bundle could only be
  placed in time by reading a screenshot's file name;
* the header named the version, the GPU and the driver but not one switch, so
  "frame generation does not work" left no way to tell whether the feature had
  been on from the start or switched on halfway through.

Checked here:

1. the settings section exists in the report and carries the product keys;
2. it drops what must not travel: hotkeys (a user's own bindings) and the
   screenshot/recording directories (paths) - even though they are in the
   config the section is built from;
3. an unknown key is dropped rather than trusted (the allow-list is the
   contract, not the caller's dict);
4. a value that is not a scalar is dropped, and a long string is dropped - the
   section is a dozen scalars, not an unbounded part of the report;
5. the environment header prints a date, and the switches line names the
   product switches;
6. the bundle caller passes the live config, not the file on disk.

Provable by mutation: add `hotkeys` to the allow-list and check 2 fails; drop
the date from the header and check 5 fails.

Run:  runtime\\python.exe tests\\test_diagnostic_settings.py
"""
import json
import re
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))  # the modules live in app/

import diagnostics  # noqa: E402

# What a real config.json holds, including the fields that must not travel.
SAMPLE = {
    "profile": "Natural", "style": 1, "auto_mask": 1,
    "intensity": 1.0, "local_tone": 0.5, "local_structure": 1.0,
    "skin_structure": -1.0, "work_scale": 0.65, "nr_small": False,
    "motion_backend": "nvofa", "frame_generation": True, "frame_multiplier": 3,
    "frame_limit_mode": "off", "hdr": False, "spout": False,
    "monitor": 0, "theme": "dark", "lang": "ru",
    # Must not travel:
    "hotkeys": {"toggle": "Num1", "record": "Num0"},
    "screenshot_dir": "C:/Users/Somebody/Pictures",
    "recording_dir": "D:/private/captures",
    "presets": {"mine": {"intensity": 2.0}},
}


def build(settings):
    out = Path(tempfile.mkdtemp()) / "b.zip"
    diagnostics.create_diagnostic_bundle(out, diagnostics.DiagnosticBundleRequest(
        failure_stage="manual",
        settings=settings,
        system_snapshot={"os": {"system": "Windows"}, "gpus": [], "displays": []},
    ))
    with zipfile.ZipFile(out) as archive:
        return json.loads(archive.read("diagnostics.json"))


def main() -> int:
    failures = []
    report = build(SAMPLE)
    settings = report.get("settings")

    # --- 1. the section exists and carries the product keys ----------------
    if settings is None:
        failures.append("the report has no settings section: a bundle still "
                        "cannot say how the program was configured")
        settings = {}
    for key in ("frame_generation", "frame_multiplier", "motion_backend",
                "profile", "work_scale", "lang"):
        if key not in settings:
            failures.append(f"the settings section is missing {key}")

    # --- 2. what must not travel ------------------------------------------
    for private in ("hotkeys", "screenshot_dir", "recording_dir", "presets"):
        if private in settings:
            failures.append(f"`{private}` travelled into the bundle: it is a "
                            "user's own setting or a path, not a product fact")

    # --- 3. an unknown key is dropped -------------------------------------
    unknown = build({**SAMPLE, "something_new_and_private": "leak"})
    if "something_new_and_private" in unknown.get("settings", {}):
        failures.append("an unknown config key was copied into the bundle: the "
                        "section must be an allow-list, not a pass-through")

    # --- 4. non-scalars and long strings are dropped ----------------------
    odd = build({"lang": "ru", "profile": "N" * 500, "monitor": {"nested": 1}})
    odd_settings = odd.get("settings", {})
    if "monitor" in odd_settings:
        failures.append("a nested value was copied: the section would grow "
                        "with whatever a config happens to hold")
    if "profile" in odd_settings:
        failures.append("a 500-character string was copied unbounded")

    # --- 5. the log header has the date and the switches ------------------
    startup = (ROOT / "app" / "startup.py").read_text(encoding="utf-8", errors="replace")
    if not re.search(r"time\.strftime\('%Y-%m-%d %H:%M:%S'\)", startup):
        failures.append("the environment header carries no date: a bundle can "
                        "only be placed in time by reading a file name")
    if "[env] switches:" not in startup:
        failures.append("the header names no switches: a report cannot tell "
                        "whether FG was on when the session started")
    for switch in ("frame_generation", "frame_multiplier", "motion_backend"):
        if switch not in startup.split("[env] switches:")[-1][:600]:
            failures.append(f"the switches line does not name {switch}")

    # --- 6. the caller passes the live config -----------------------------
    runtime = (ROOT / "app" / "compatibility_runtime.py").read_text(encoding="utf-8",
                                                            errors="replace")
    if "settings=settings_snapshot(st)" not in runtime:
        failures.append("the bundle is not built from the live config: a "
                        "setting changed a second ago would be sent as its "
                        "previous value")
    elif "getattr(st, \"cfg\"" not in runtime:
        failures.append("settings_snapshot does not read the live config")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the bundle carries the product settings (and only them), and the "
          "log header carries the date and the switches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
