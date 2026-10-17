"""Every interactive control on every page fires the right command.

The menu is the only surface the user touches, so a control that silently
does nothing is a broken release. Walks every page (main, settings,
windows), clicks every item, and checks the emitted action against the
expected command table:

  * header icons: help -> github, gear -> settings page, min -> close,
    close (settings) -> back to main;
  * main page: the DLSS 5 toggle -> ("nr",), profile choice -> ("profile",),
    sliders -> ("param", ...) / ("split", ...) / ("nr_res", ...), the
    Actions buttons -> windows page / window_mode / screenshot / record;
  * settings page: language/theme segments -> ("lang",) / ("theme",),
    the Spout2 and recording-indicator toggles -> ("toggle", ...),
    hotkey rows -> capture, back -> main;
  * windows page: a window row -> ("window",), back -> main;
  * the footer exit -> ("button", "exit").
"""
import sys
from pathlib import Path

import pygame

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, display.py, ...)
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
import overlay_ui  # noqa: E402

STATE = {
    "nr": True,
    "profile": "Natural",
    "profiles": ["Natural", "Strong / Cinematic", "Vivid"],
    "params": {"intensity": 1.0, "local_tone": 1.0,
               "local_structure": 1.0, "skin_structure": -1.0},
    "split": 0.0,
    "work_scale": 0.65, "work_scale_cap": 1.0, "nr_small": False,
    "screen_size": "3840x2160",
    "theme": "light", "lang": "en",
    "gpu_text": "RTX 5070 Ti · Blackwell", "gpu_ok": True,
    "window_mode": False,
    "rec_indicator": True, "spout": False, "hdr": False,
    "windows": ["1A2B3C: Notepad", "4D5E6F: Chrome - YouTube"],
    "window_current": "1A2B3C: Notepad",
}


def font_loader(size):
    try:
        return pygame.font.SysFont("consolas", size)
    except Exception:
        return pygame.font.Font(None, size)


def build():
    menu = overlay_ui.OverlayMenu(1.0, font_loader)
    menu.set_state(dict(STATE))
    menu.set_stats({"fps": 55.0, "status": "NR ON", "resolution": "3840x2160",
                    "frames": 100})
    menu.visible = True
    # Every fold-out open (redesign B, v2.6.0): the page with all of its
    # controls, which is what the tests built on this one click through.
    menu.folds = {"net", "tune", "camera", "source"}
    return menu


def paint(menu):
    """Draw once so the geometry extras (slider tracks, segment cells) exist."""
    surf = pygame.Surface((3840, 2160))
    menu.draw(surf)


def click(menu, item):
    """Click the CONTROL's own zone, not the row (user rule 16.09).

    A toggle/slider/choice row spans the panel width and only the control
    inside it reacts - the switch pill, the slider track, the select
    field, the hotkey field. item.extra["hit"] is that zone; the tests
    click it so they exercise the same target a user does.
    """
    zone = item.extra.get("hit") or item.rect
    if item.extra.get("hit", True) is None:
        raise AssertionError(
            f"{item.kind} {item.key!r} has no hit zone of its own - its cells "
            f"carry the target, click one of those")
    pos = zone.center
    out = menu.handle_event(pygame.event.Event(
        pygame.MOUSEBUTTONDOWN, {"pos": pos, "button": 1}))
    menu.handle_event(pygame.event.Event(
        pygame.MOUSEBUTTONUP, {"pos": pos, "button": 1}))
    return out


def find(menu, kind, key):
    return next((i for i in menu.items
                 if i.kind == kind and i.key == key), None)


def main() -> int:
    pygame.init()
    pygame.display.set_mode((64, 64))
    failures = []

    menu = build()
    menu.layout(3840, 2160)
    paint(menu)

    # 1. Header icons on the main page.
    for key, want in (("help", [("button", "github")]),
                      ("min", [("button", "close")])):
        icon = find(menu, "icon", key)
        if icon is None:
            failures.append(f"no {key} icon on the main page")
            continue
        out = click(menu, icon)
        if out != want:
            failures.append(f"icon {key}: expected {want}, got {out}")
    gear = find(menu, "icon", "gear")
    if gear is None:
        failures.append("no gear icon on the main page")
    else:
        out = click(menu, gear)
        if menu.page != "settings" or out != [("capture", None)]:
            failures.append(f"gear: expected settings page, got {out}")

    # 2. The settings page: four tabs, and each control on the one it
    #    belongs to. The page used to lay every control out at once.
    menu.settings_tab = "app"      # the language and the theme
    menu.layout(3840, 2160)
    paint(menu)
    lang_choice = find(menu, "choice", "lang")
    if lang_choice is None:
        failures.append("no lang drop-down on the settings page")
    else:
        out = click(menu, lang_choice)
        if out != []:
            failures.append(f"lang: expected open (no action), got {out}")
        menu.layout(3840, 2160)
        paint(menu)
        paint(menu)
        opts = [i for i in menu.options if i.kind == "option"]
        if len(opts) < 2:
            failures.append("the lang list did not open")
        else:
            out = click(menu, opts[1])
            if out != [("lang", "ru")]:
                failures.append(f"lang pick: expected ru, got {out}")
        # The stale option rows are cleared only by the next layout - rebuild
        # before clicking anything below the list.
        menu.layout(3840, 2160)
        paint(menu)
    menu.settings_tab = "app"      # the theme segment lives here now
    menu.layout(3840, 2160)
    paint(menu)
    for key, want in (("theme", [("theme", "dark")]),):
        seg = find(menu, "segmented", key)
        if seg is None:
            failures.append(f"no {key} segment on the settings page")
            continue
        cells = seg.extra.get("cells") or []
        if not cells:
            failures.append(f"the {key} segment has no cells")
            continue
        out = menu.handle_event(pygame.event.Event(
            pygame.MOUSEBUTTONDOWN, {"pos": cells[1].center, "button": 1}))
        if out != want:
            failures.append(f"segment {key}: expected {want}, got {out}")
    # The key rows have their own tab now.
    menu.settings_tab = "keys"
    menu.layout(3840, 2160)
    for hk_cmd in ("toggle", "settings", "screenshot_menu", "record",
                   "window_mode", "scale_up", "scale_down", "quit"):
        hk = find(menu, "hotkey", hk_cmd)
        if hk is None:
            failures.append(f"no hotkey row {hk_cmd} on the settings page")
            continue
        out = click(menu, hk)
        if out != [("capture", hk_cmd)]:
            failures.append(f"hotkey row {hk_cmd}: expected capture, got {out}")
    # The switches on the settings page: each reports its own key. Two
    # of the three restart the worker when they fire (spout, hdr).
    # The toggles are spread over the tabs now: capture holds hdr and the
    # static skip, recording holds spout and the indicator.
    for tg_key, tg_tab in (("spout", "rec"), ("rec_indicator", "rec"),
                           ("hdr", "capture")):
        menu.settings_tab = tg_tab
        menu.layout(3840, 2160)
        tg = find(menu, "toggle", tg_key)
        if tg is None:
            failures.append(f"no {tg_key} toggle on the settings page")
            continue
        # The control's own zone (the switch pill), not the row: only the
        # switch reacts now (user rule 16.09).
        pos = (tg.extra.get("hit") or tg.rect).center
        out = menu.handle_event(pygame.event.Event(
            pygame.MOUSEBUTTONDOWN, {"pos": pos, "button": 1}))
        menu.handle_event(pygame.event.Event(
            pygame.MOUSEBUTTONUP, {"pos": pos, "button": 1}))
        if out != [("toggle", tg_key)]:
            failures.append(f"toggle {tg_key}: expected [('toggle', {tg_key!r})], "
                            f"got {out}")
    # The label end of the row is a caption, not a hit target (user rule
    # 16.09: only explicit switches and choices react). A click on the empty
    # left half of a toggle row must emit nothing - it used to flip the
    # switch.
    menu.settings_tab = "rec"
    menu.layout(3840, 2160)
    paint(menu)
    label_tg = find(menu, "toggle", "spout")
    if label_tg is None:
        failures.append("no spout toggle for the label hit-test")
    else:
        label_pos = (label_tg.rect.x + menu._u(8),
                     label_tg.rect.y + menu._u(overlay_ui.CTRL_H) // 2)
        out = menu.handle_event(pygame.event.Event(
            pygame.MOUSEBUTTONDOWN, {"pos": label_pos, "button": 1}))
        if out:
            failures.append(f"a click on the row label must not emit, got {out}")
    # The hint under a toggle is a caption, not a hit target: a click on
    # the explanation must emit nothing (the Spout2 toggle restarts the
    # worker, so a stray click there would freeze the screen for seconds).
    menu.settings_tab = "rec"
    menu.layout(3840, 2160)
    paint(menu)
    spout_tg = find(menu, "toggle", "spout")
    if spout_tg is None:
        failures.append("no spout toggle for the hint hit-test")
    elif not spout_tg.extra.get("hint"):
        failures.append("the spout toggle lost its hint")
    else:
        hint_pos = (spout_tg.rect.centerx,
                    spout_tg.rect.y + menu._u(overlay_ui.CTRL_H)
                    + menu._u(10))
        out = menu.handle_event(pygame.event.Event(
            pygame.MOUSEBUTTONDOWN, {"pos": hint_pos, "button": 1}))
        if out:
            failures.append(f"a click on the hint must not emit, got {out}")
    close_icon = find(menu, "icon", "close")
    if close_icon is None:
        failures.append("no close icon on the settings page")
    else:
        out = click(menu, close_icon)
        if menu.page != "main" or out != [("capture", None)]:
            failures.append(f"close icon: expected back to main, got {out}")

    # 3. The main page: toggle, profile, sliders, Actions buttons.
    menu.layout(3840, 2160)
    paint(menu)
    toggle = find(menu, "tile", "nr")
    if toggle is None:
        failures.append("no DLSS 5 tile on the main page")
    else:
        out = click(menu, toggle)
        if out != [("nr",)]:
            failures.append(f"toggle: expected [('nr',)], got {out}")
    prof = find(menu, "choice", "profile")
    if prof is None:
        failures.append("no profile choice on the main page")
    else:
        out = click(menu, prof)
        if out != []:
            failures.append(f"profile: expected open (no action), got {out}")
        menu.layout(3840, 2160)
        paint(menu)
        paint(menu)
        opts = [i for i in menu.options if i.kind == "option"]
        if len(opts) < 2:
            failures.append("the profile list did not open")
        else:
            out = click(menu, opts[1])
            if out != [("profile", "Strong / Cinematic")]:
                failures.append(f"profile pick: expected Strong, got {out}")
    for key, want in (("screenshot", [("button", "screenshot")]),
                      ("record", [("button", "record")])):
        btn = find(menu, "button", key)
        if btn is None:
            failures.append(f"no {key} button in the Actions section")
            continue
        out = click(menu, btn)
        if out != want:
            failures.append(f"button {key}: expected {want}, got {out}")

    # 3a2. Style is its own control now - the measurement says it is the
    #      strongest lever there is, and it used to be reachable only by
    #      picking a whole profile. Three cells, the middle one is Natural.
    # The profile list above was expanded and collapsed; its rows move
    # everything below, and a segment's cells are geometry the DRAWER
    # fills. Lay out and paint again or the clicks land on the old rects.
    menu.layout(3840, 2160)
    paint(menu)
    seg = find(menu, "segmented", "style")
    if seg is None:
        failures.append("no style segment on the main page")
    else:
        cells = seg.extra.get("cells") or []
        if len(cells) != 3:
            failures.append(f"the style segment has {len(cells)} cells, not 3")
        else:
            out = menu.handle_event(pygame.event.Event(
                pygame.MOUSEBUTTONDOWN, {"pos": cells[0].center, "button": 1}))
            if out != [("style", "0")]:
                failures.append(f"style Default: expected [('style', '0')], "
                                f"got {out}")
            out = menu.handle_event(pygame.event.Event(
                pygame.MOUSEBUTTONDOWN, {"pos": cells[2].center, "button": 1}))
            if out != [("style", "2")]:
                failures.append(f"style Cinematic: expected [('style', '2')], "
                                f"got {out}")

    # 3a3. "modified - revert" appears only when the live values have left
    #      the profile they came from, and reverting is picking the same
    #      profile again - which is what replaces every parameter with the
    #      profile's own.
    menu.page = "main"
    menu.set_state({"profile": "Natural",
                    "param_defaults": {"intensity": 1.0, "local_tone": 0.5,
                                       "local_structure": 1.0,
                                       "skin_structure": -1.0, "style": 1},
                    "params": {"intensity": 1.0, "local_tone": 0.5,
                               "local_structure": 1.0, "skin_structure": -1.0},
                    "style": 1})
    menu.layout(3840, 2160)
    if find(menu, "button", "revert_profile") is not None:
        failures.append("revert is offered while the profile is untouched")
    menu.set_state({"params": {"intensity": 1.0, "local_tone": 1.2,
                               "local_structure": 1.0, "skin_structure": -1.0}})
    menu.layout(3840, 2160)
    paint(menu)
    rev = find(menu, "button", "revert_profile")
    if rev is None:
        failures.append("a moved slider must offer revert")
    else:
        out = click(menu, rev)
        if out != [("profile", "Natural")]:
            failures.append(f"revert: expected [('profile', 'Natural')], "
                            f"got {out}")
    # The model is NOT part of the profile any more (user rule 15.09): it
    # is its own control, and a model change must not offer "revert" -
    # reverting the profile restores the four sliders and leaves the model
    # where the user put it.
    menu.set_state({"params": {"intensity": 1.0, "local_tone": 0.5,
                               "local_structure": 1.0, "skin_structure": -1.0},
                    "style": 2})
    menu.layout(3840, 2160)
    if find(menu, "button", "revert_profile") is not None:
        failures.append("a changed model must NOT offer revert any more")
    menu.set_state({"style": 1})
    # Back to a painted layout: the source segment below reads the cells the
    # drawer fills, and the state changes above invalidated them.
    menu.layout(3840, 2160)
    paint(menu)

    # 3a4. The row that names the captured window opens the picker. It was
    #      the one line on the page that looked like a control and was not.
    menu.page = "main"
    menu.state["window_mode"] = True
    menu.set_state({"window_current": "1A2B3C: Firefox"})
    menu.layout(3840, 2160)
    row = next((i for i in menu.items
                if i.kind == "info" and i.key == "source_now"), None)
    if row is None:
        failures.append("no captured-window row in window mode")
    else:
        out = menu.handle_event(pygame.event.Event(
            pygame.MOUSEBUTTONDOWN, {"pos": row.rect.center, "button": 1}))
        if menu.page != "windows" or out != [("capture", None)]:
            failures.append(f"the captured-window row must open the picker, "
                            f"got page={menu.page!r} out={out}")
    menu.page = "main"
    menu.state["window_mode"] = False
    menu.layout(3840, 2160)
    paint(menu)

    # 3b. The source segment carries what the Actions buttons used to: the
    #     left cell returns to the whole screen (nothing to do when it is
    #     already there), the right cell opens the window list.
    seg = find(menu, "segmented", "source")
    if seg is None:
        failures.append("no source segment on the main page")
    else:
        cells = seg.extra.get("cells") or []
        if len(cells) != 2:
            failures.append(f"the source segment has {len(cells)} cells")
        else:
            out = menu.handle_event(pygame.event.Event(
                pygame.MOUSEBUTTONDOWN,
                {"pos": cells[0].center, "button": 1}))
            menu.handle_event(pygame.event.Event(
                pygame.MOUSEBUTTONUP, {"pos": cells[0].center, "button": 1}))
            if out != []:
                failures.append(f"whole screen while already there: {out}")
            out = menu.handle_event(pygame.event.Event(
                pygame.MOUSEBUTTONDOWN,
                {"pos": cells[1].center, "button": 1}))
            menu.handle_event(pygame.event.Event(
                pygame.MOUSEBUTTONUP, {"pos": cells[1].center, "button": 1}))
            if out != [("capture", None)] or menu.page != "windows":
                failures.append(f"one window: expected the window page, got {out}")
        # And from window mode the left cell is the way back out.
        menu.page = "main"
        menu.state["window_mode"] = True
        menu.layout(3840, 2160)
        paint(menu)
        seg = find(menu, "segmented", "source")
        cells = (seg.extra.get("cells") or []) if seg else []
        if cells:
            out = menu.handle_event(pygame.event.Event(
                pygame.MOUSEBUTTONDOWN, {"pos": cells[0].center, "button": 1}))
            menu.handle_event(pygame.event.Event(
                pygame.MOUSEBUTTONUP, {"pos": cells[0].center, "button": 1}))
            if out != [("button", "window_mode")]:
                failures.append(f"leaving window mode: got {out}")
        menu.state["window_mode"] = False
        menu.page = "windows"

    # 4. The windows page: a window row and the back button.
    menu.layout(3840, 2160)
    paint(menu)
    row = find(menu, "option", "window")
    if row is None:
        failures.append("no window rows on the windows page")
    else:
        out = click(menu, row)
        if out != [("window", "1A2B3C: Notepad")]:
            failures.append(f"window row: expected pick, got {out}")
    back = find(menu, "action", "back")
    if back is None:
        failures.append("no back button on the windows page")
    else:
        out = click(menu, back)
        if menu.page != "main" or out != [("capture", None)]:
            failures.append(f"back: expected main page, got {out}")

    # 5. The footer exit on the main page.
    menu.layout(3840, 2160)
    paint(menu)
    exit_btn = find(menu, "action", "exit")
    if exit_btn is None:
        failures.append("no exit button in the footer")
    else:
        out = click(menu, exit_btn)
        if out != [("button", "exit")]:
            failures.append(f"exit: expected [('button', 'exit')], got {out}")

    # 6. The sliders emit their commands.
    #
    # The resolution slider only exists while Boost is on: with Boost off the
    # network runs at the full frame size whatever the slider says, and the
    # output frames come back bit-identical at every position of it
    # (measured, 12.09). A control that answers and does nothing is worse
    # than no control, so it is simply not laid out then.
    menu.layout(3840, 2160)
    paint(menu)
    if find(menu, "slider", "nr_res") is not None:
        failures.append("the resolution slider is laid out with Boost off, "
                        "where every position of it does the same thing")
    boost = find(menu, "tile", "boost")
    if boost is None:
        failures.append("no Boost tile on the main page")
    else:
        out = click(menu, boost)
        if out != [("toggle", "boost")]:
            failures.append(f"boost: expected [('toggle', 'boost')], got {out}")
    menu.set_state({"nr_small": True, "work_size": "2496x1404"})
    menu.layout(3840, 2160)
    paint(menu)
    for key, want_prefix in (("intensity", "param"), ("split", "split"),
                             ("nr_res", "nr_res")):
        sl = find(menu, "slider", key)
        if sl is None:
            failures.append(f"no {key} slider on the main page")
            continue
        track = sl.extra.get("track")
        if track is None:
            failures.append(f"the {key} slider has no track")
            continue
        out = menu.handle_event(pygame.event.Event(
            pygame.MOUSEBUTTONDOWN,
            {"pos": (track.x + track.w // 2, track.centery), "button": 1}))
        if not out or out[0][0] != want_prefix:
            failures.append(f"slider {key}: expected {want_prefix}..., got {out}")

    # 7. The preset buttons: Save emits, Delete is disabled without an
    #    active user preset and emits nothing.
    menu.layout(3840, 2160)
    paint(menu)
    save_btn = find(menu, "button", "save_preset")
    del_btn = find(menu, "button", "delete_preset")
    if save_btn is None or del_btn is None:
        failures.append("the preset buttons are missing on the main page")
    else:
        out = click(menu, save_btn)
        if out != [("button", "save_preset")]:
            failures.append(f"save_preset: expected [('button', 'save_preset')], "
                            f"got {out}")
        if not del_btn.extra.get("disabled"):
            failures.append("delete_preset must be disabled without an "
                            "active user preset")
        out = click(menu, del_btn)
        if out:
            failures.append(f"a disabled button must not emit, got {out}")
        # With an active user preset the delete button becomes live.
        menu.set_state({"preset_active": True})
        menu.layout(3840, 2160)
        paint(menu)
        del_btn = find(menu, "button", "delete_preset")
        if del_btn is None or del_btn.extra.get("disabled"):
            failures.append("delete_preset must be enabled with an active "
                            "user preset")
        else:
            out = click(menu, del_btn)
            if out != [("button", "delete_preset")]:
                failures.append(f"delete_preset: expected "
                                f"[('button', 'delete_preset')], got {out}")

    # 8. The conversion page. The main page's Convert cell opens it; each
    #    row's button carries its job and its action in the key; the footer
    #    adds files; the output choices send their values; and a file dropped
    #    on the panel opens the page and joins the queue.
    menu.page = "main"
    menu.layout(3840, 2160)
    paint(menu)
    cell = find(menu, "button", "convert")
    if cell is None:
        failures.append("no Convert cell in the main page's actions")
    else:
        out = click(menu, cell)
        if menu.page != "convert" or out != [("capture", None)]:
            failures.append(f"Convert: expected the conversion page, got "
                            f"page={menu.page!r} out={out}")
    menu.page = "convert"
    menu.set_state({"convert_dest": "source", "convert_jobs": [
        {"id": 7, "name": "clip.mp4", "kind": "video", "status": "running",
         "fraction": 0.4, "line": "40%", "tone": "text", "action": "stop"},
        {"id": 8, "name": "still.png", "kind": "image", "status": "done",
         "fraction": 1.0, "line": "done", "tone": "ok", "action": "show"}]})
    menu.layout(3840, 2160)
    paint(menu)
    for key in ("convert_job:7:stop", "convert_job:8:show",
                "convert_clear", "convert_stop_all"):
        btn = find(menu, "button", key)
        if btn is None:
            failures.append(f"conversion page: no {key!r} button")
            continue
        out = click(menu, btn)
        if out != [("button", key)]:
            failures.append(f"{key}: expected [('button', {key!r})], got {out}")
    add = find(menu, "action", "convert_add")
    if add is None:
        failures.append("no Add files in the conversion page's footer")
    else:
        out = click(menu, add)
        if out != [("button", "convert_add")]:
            failures.append(f"Add files: expected [('button', 'convert_add')], "
                            f"got {out}")
    for key, value in (("convert_dest", "folder"), ("convert_codec", "h264"),
                       ("convert_quality", "small"),
                       ("convert_image_format", "png")):
        menu.layout(3840, 2160)
        paint(menu)
        seg = find(menu, "segmented", key)
        cells = (seg.extra.get("cells") or []) if seg else []
        if not cells:
            failures.append(f"conversion page: no {key!r} segment")
            continue
        at = cells[list(seg.payload).index(value)].center
        out = menu.handle_event(pygame.event.Event(
            pygame.MOUSEBUTTONDOWN, {"pos": at, "button": 1}))
        menu.handle_event(pygame.event.Event(
            pygame.MOUSEBUTTONUP, {"pos": at, "button": 1}))
        if out != [(key, value)]:
            failures.append(f"{key}: expected [({key!r}, {value!r})], got {out}")
    menu.layout(3840, 2160)
    paint(menu)
    if find(menu, "button", "convert_dir") is None:
        failures.append("One folder shows no Change folder button")
    audio = find(menu, "toggle", "convert_audio")
    if audio is None:
        failures.append("no Keep audio switch on the conversion page")
    else:
        out = click(menu, audio)
        if out != [("toggle", "convert_audio")]:
            failures.append(f"Keep audio: expected [('toggle', "
                            f"'convert_audio')], got {out}")
    menu.page = "main"
    menu.layout(3840, 2160)
    out = menu.handle_event(pygame.event.Event(
        pygame.DROPFILE, {"file": "C:/clips/drop.mp4"}))
    if menu.page != "convert" or out != [("capture", None),
                                         ("convert_files", ["C:/clips/drop.mp4"])]:
        failures.append(f"a dropped file: expected the conversion page and "
                        f"the file queued, got page={menu.page!r} out={out}")
    menu.layout(3840, 2160)
    paint(menu)
    back = find(menu, "action", "back")
    if back is None or click(menu, back) != [("capture", None)] \
            or menu.page != "main":
        failures.append("Back on the conversion page did not return to main")

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: every control on every page fires the right command")
    return 0


if __name__ == "__main__":
    sys.exit(main())
