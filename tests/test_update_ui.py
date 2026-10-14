"""Behavior and rendered evidence for the manual About update flow."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))
sys.path.insert(0, str(BASE / "tests"))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame  # noqa: E402

import commands  # noqa: E402
import overlay_ui  # noqa: E402
from i18n import STRINGS  # noqa: E402
from settings_io import menu_payload  # noqa: E402
from test_menu_state_keys import _state as _payload_state  # noqa: E402
from updates import UpdateState  # noqa: E402


def _font(size: int, **_kwargs):
    return pygame.font.Font(None, size)


def _menu(*, width=1280, height=900, scale=1.0, return_surface=False,
          **updates):
    menu = overlay_ui.OverlayMenu(scale, _font)
    menu.page = "settings"
    menu.settings_tab = "app"
    state = {
        "theme": "light",
        "lang": "en",
        "about": {"version": "2.4.0", "gpu": "RTX test", "driver": "test",
                  "windows": "Windows test"},
        "compatibility_status": "pass",
        "compatibility_score": "N/N",
        "update_phase": "idle",
        "update_latest_version": None,
        "update_received_bytes": 0,
        "update_total_bytes": 0,
        "update_error_code": None,
        "update_can_install": False,
        "update_busy": False,
        "update_confirming": False,
    }
    state.update(updates)
    menu.set_state(state)
    menu.visible = True
    menu.layout(width, height)
    surface = pygame.Surface((width, height))
    menu.draw(surface)
    return (menu, surface) if return_surface else menu


def _find(menu, kind: str, key: str):
    return next((item for item in menu.items
                 if item.kind == kind and item.key == key), None)


def _click(menu, item):
    pos = item.rect.center
    down = menu.handle_event(pygame.event.Event(
        pygame.MOUSEBUTTONDOWN, {"pos": pos, "button": 1}))
    menu.handle_event(pygame.event.Event(
        pygame.MOUSEBUTTONUP, {"pos": pos, "button": 1}))
    return down


def capture_update_demos(destination: Path) -> list[dict]:
    """Render real OverlayMenu states with simulated release snapshots."""
    pygame.init()
    pygame.display.set_mode((64, 64))
    destination.mkdir(parents=True, exist_ok=True)
    cases = (
        ("current-light", "light", 1280, 900, 1.0,
         {"update_phase": "current"}),
        ("checking-dark", "dark", 1280, 900, 1.0,
         {"update_phase": "checking"}),
        ("available-light", "light", 1280, 900, 1.0,
         {"update_phase": "available", "update_latest_version": "2.5.0"}),
        ("downloading-progress-dark", "dark", 1280, 900, 1.0,
         {"update_phase": "downloading", "update_latest_version": "2.5.0",
          "update_received_bytes": 25, "update_total_bytes": 100}),
        ("ready-light", "light", 1280, 900, 1.0,
         {"update_phase": "ready", "update_latest_version": "2.5.0",
          "update_can_install": True}),
        ("busy-blocked-dark", "dark", 1280, 900, 1.0,
         {"update_phase": "ready", "update_latest_version": "2.5.0",
          "update_can_install": True, "update_busy": True}),
        ("confirmation-light", "light", 1280, 900, 1.0,
         {"update_phase": "ready", "update_latest_version": "2.5.0",
          "update_can_install": True, "update_confirming": True}),
        ("confirmation-busy-dark", "dark", 1280, 900, 1.0,
         {"update_phase": "ready", "update_latest_version": "2.5.0",
          "update_can_install": True, "update_busy": True,
          "update_confirming": True}),
        ("error-dark", "dark", 1280, 900, 1.0,
         {"update_phase": "error", "update_error_code": "restart_required"}),
        ("confirmation-narrow", "light", 1024, 900, 1.0,
         {"update_phase": "ready", "update_latest_version": "2.5.0",
          "update_can_install": True, "update_confirming": True}),
        ("error-narrow", "dark", 1024, 900, 1.0,
         {"update_phase": "error", "update_error_code": "network"}),
    )
    inventory = []
    region_inventory = []
    region_names = {
        "available-light", "downloading-progress-dark", "ready-light",
        "busy-blocked-dark", "confirmation-light", "error-dark",
    }
    for name, theme, width, height, scale, state in cases:
        menu, surface = _menu(width=width, height=height, scale=scale,
                              theme=theme, return_surface=True, **state)
        path = destination / f"{name}.png"
        pygame.image.save(surface, str(path))
        inventory.append({
            "file": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "viewport": [width, height],
            "ui_scale": scale,
            "theme": theme,
            "update_phase": state["update_phase"],
            "state": state,
            "simulated_release_snapshot": True,
            "controls": [item.key for item in menu.items
                         if item.kind == "button"
                         and item.key.startswith("update_")],
        })
        if name in region_names:
            region_rect = menu.panel_rect.clip(surface.get_rect())
            region_path = destination / f"menu-{name}.png"
            pygame.image.save(surface.subsurface(region_rect).copy(),
                              str(region_path))
            region_inventory.append({
                "file": region_path.name,
                "sha256": hashlib.sha256(region_path.read_bytes()).hexdigest(),
                "viewport": [width, height],
                "panel_rect": [region_rect.x, region_rect.y,
                               region_rect.width, region_rect.height],
                "theme": theme,
                "update_phase": state["update_phase"],
                "state": state,
                "simulated_release_snapshot": True,
            })
    metadata = {
        "source": "OverlayMenu layout, event controls, and draw path",
        "release_data": "simulated fixture only; no update request or app launch",
        "captures": inventory,
        "menu_region_captures": region_inventory,
    }
    (destination / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return inventory


class _Controller:
    def __init__(self, state=None, *, armed=False, proceed=True):
        self.check_calls = 0
        self.download_calls = 0
        self.state = state or types.SimpleNamespace(
            phase="idle", latest_version=None, received_bytes=0,
            total_bytes=0, error_code=None, can_install=False)
        self.arm_calls = []
        self.armed = armed
        self.proceed_result = proceed
        self.authorized = False
        self.proceed_calls = 0
        self.cancel_calls = 0

    def check(self):
        self.check_calls += 1
        return True

    def download(self):
        self.download_calls += 1
        return True

    def poll(self):
        return self.state

    def poll_armed(self):
        return self.armed

    def arm_install(self, **kwargs):
        self.arm_calls.append(kwargs)
        return True

    def proceed_install(self):
        self.proceed_calls += 1
        if self.proceed_result:
            self.authorized = True
        else:
            self.state = types.SimpleNamespace(
                phase="error", latest_version=self.state.latest_version,
                received_bytes=0, total_bytes=0,
                error_code="prepare_failed", can_install=False)
        return self.proceed_result

    def cancel_install(self):
        self.cancel_calls += 1
        if self.authorized:
            return False
        self.state = types.SimpleNamespace(
            phase="error", latest_version=self.state.latest_version,
            received_bytes=0, total_bytes=0,
            error_code="prepare_failed", can_install=False)
        return True


class UpdateMenuBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.init()
        pygame.display.set_mode((64, 64))

    @classmethod
    def tearDownClass(cls):
        pygame.quit()

    def test_about_update_button_starts_one_manual_check(self):
        menu = _menu()
        button = _find(menu, "button", "update_check")
        self.assertIsNotNone(button, "About must show the update control")
        self.assertEqual(button.extra.get("label"),
                         STRINGS["en"]["update_check"])
        action = _click(menu, button)
        self.assertEqual(action, [("button", "update_check")])

        busy_fields = ("recorder", "recording_finalizer", "convert_queue")
        for field in busy_fields:
            with self.subTest(busy_field=field):
                controller = _Controller()
                st = types.SimpleNamespace(
                    update_controller=controller, recorder=None,
                    recording_finalizer=None,
                    convert_queue=types.SimpleNamespace(busy=False))
                if field == "convert_queue":
                    st.convert_queue.busy = True
                else:
                    setattr(st, field, object())
                commands.apply_menu_action(st, action[0])
                self.assertEqual(controller.check_calls, 1)

    def test_available_release_shows_version_and_download_action(self):
        menu = _menu(update_phase="available",
                     update_latest_version="2.5.0")
        status = _find(menu, "note", "update_status")
        self.assertIsNotNone(status)
        self.assertEqual(status.extra.get("text"),
                         STRINGS["en"]["update_available"].format(
                             version="2.5.0"))
        button = _find(menu, "button", "update_download")
        self.assertIsNotNone(button)
        self.assertEqual(button.extra.get("label"),
                         STRINGS["en"]["update_download"])
        action = _click(menu, button)
        self.assertEqual(action, [("button", "update_download")])
        # The rendered control remains usable while capture/conversion work is
        # active because downloading does not alter the running installation.
        for field in ("recorder", "recording_finalizer", "convert_queue"):
            with self.subTest(busy_field=field):
                busy_menu = _menu(update_phase="available",
                                  update_latest_version="2.5.0")
                busy_button = _find(busy_menu, "button", "update_download")
                self.assertEqual(_click(busy_menu, busy_button), action)
                busy_controller = _Controller(types.SimpleNamespace(
                    phase="available", latest_version="2.5.0",
                    received_bytes=0, total_bytes=0, error_code=None,
                    can_install=False))
                st = types.SimpleNamespace(
                    update_controller=busy_controller, recorder=None,
                    recording_finalizer=None,
                    convert_queue=types.SimpleNamespace(busy=False))
                if field == "convert_queue":
                    st.convert_queue.busy = True
                else:
                    setattr(st, field, object())
                commands.apply_menu_action(st, action[0])
                self.assertEqual(busy_controller.download_calls, 1)

    def test_restart_required_explains_recovery_and_disables_retry(self):
        for lang, strings in STRINGS.items():
            with self.subTest(lang=lang):
                state = _payload_state()
                state.lang = lang
                state.update_state = UpdateState(phase="error", error_code="restart_required")
                payload = menu_payload(state)
                self.assertEqual(payload["update_error_code"], "restart_required")
                menu = _menu(**payload)
                retry = _find(menu, "button", "update_retry")
                self.assertTrue(retry.extra.get("disabled"))
                self.assertEqual(_click(menu, retry), [])
                self.assertEqual(_find(menu, "note", "update_status").extra["text"],
                                 strings["update_error_restart_required"])
                self.assertIn("NeuralScreen", strings["update_error_restart_required"])
                self.assertIsNotNone(_find(menu, "button", "update_official_release"))

    def test_update_snapshots_render_status_and_gated_actions(self):
        cases = (
            ({"update_phase": "current"}, "update_current", "update_check"),
            ({"update_phase": "checking"}, "update_checking", "update_check"),
            ({"update_phase": "downloading", "update_latest_version": "2.5.0",
              "update_received_bytes": 25, "update_total_bytes": 100},
             "update_downloading", "update_download"),
            ({"update_phase": "ready", "update_latest_version": "2.5.0",
              "update_can_install": True}, "update_ready", "update_install"),
            ({"update_phase": "ready", "update_latest_version": "2.5.0",
              "update_can_install": True, "update_busy": True},
             "update_busy", "update_install"),
            ({"update_phase": "error", "update_error_code": "network"},
             "update_error_network", "update_retry"),
        )
        for state, key, button_key in cases:
            with self.subTest(state=state):
                menu = _menu(**state)
                status = _find(menu, "note", "update_status")
                self.assertIsNotNone(status)
                if key == "update_downloading":
                    expected = STRINGS["en"][key].format(percent=25)
                elif key == "update_ready":
                    expected = STRINGS["en"][key].format(version="2.5.0")
                else:
                    expected = STRINGS["en"][key]
                self.assertEqual(status.extra.get("text"), expected)
                button = _find(menu, "button", button_key)
                self.assertIsNotNone(button)
                if state.get("update_phase") == "checking":
                    self.assertTrue(button.extra.get("disabled"))
                if state.get("update_busy"):
                    self.assertTrue(button.extra.get("disabled"))
                    self.assertEqual(_click(menu, button), [])
                    menu._set_focus(button)
                    out = menu.handle_event(pygame.event.Event(
                        pygame.KEYDOWN, {"key": pygame.K_RETURN,
                                         "mod": 0, "unicode": ""}))
                    self.assertEqual(out, [])

        confirming = _menu(update_phase="ready",
                           update_latest_version="2.5.0",
                           update_can_install=True,
                           update_confirming=True)
        confirmation = _find(confirming, "note", "update_status")
        self.assertIsNotNone(confirmation)
        self.assertEqual(confirmation.extra.get("text"),
                         STRINGS["en"]["update_confirmation"].format(
                             version="2.5.0"))
        self.assertIsNotNone(_find(confirming, "button", "update_confirm"))
        self.assertIsNotNone(_find(confirming, "button", "update_cancel"))

        confirming_busy = _menu(update_phase="ready",
                                update_latest_version="2.5.0",
                                update_can_install=True,
                                update_busy=True,
                                update_confirming=True)
        busy_status = _find(confirming_busy, "note", "update_status")
        confirm = _find(confirming_busy, "button", "update_confirm")
        cancel = _find(confirming_busy, "button", "update_cancel")
        self.assertEqual(busy_status.extra.get("text"), STRINGS["en"]["update_busy"])
        self.assertTrue(confirm.extra.get("disabled"))
        self.assertEqual(_click(confirming_busy, confirm), [])
        self.assertFalse(cancel.extra.get("disabled"))

        failed = _menu(update_phase="error",
                       update_error_code="network")
        self.assertIsNotNone(_find(failed, "button", "update_official_release"))

    def test_install_confirmation_uses_a_fresh_ready_snapshot(self):
        ready = types.SimpleNamespace(
            phase="ready", latest_version="2.5.0", received_bytes=10,
            total_bytes=10, error_code=None, can_install=True)
        controller = _Controller(ready)
        st = types.SimpleNamespace(
            update_controller=controller, update_state=ready,
            update_confirming=False, update_frozen=False,
            recorder=None, recording_finalizer=None,
            convert_queue=types.SimpleNamespace(busy=False), lang="en",
            cfg_path=Path("settings.json"), original_cwd=Path.cwd())

        commands.apply_menu_action(st, ("button", "update_install"))
        self.assertTrue(st.update_confirming)
        self.assertEqual(controller.arm_calls, [])

        commands.apply_menu_action(st, ("button", "update_confirm"))
        self.assertFalse(st.update_confirming)
        self.assertEqual(len(controller.arm_calls), 1)
        self.assertEqual(controller.arm_calls[0]["config_path"], st.cfg_path)
        self.assertEqual(controller.arm_calls[0]["original_cwd"], st.original_cwd)
        self.assertEqual(set(controller.arm_calls[0]["messages"]), {
            "shutdown_timeout", "apply_failed", "relaunch_failed",
            "recovery_required"})

    def test_install_confirmation_rechecks_work_before_arm(self):
        ready = types.SimpleNamespace(
            phase="ready", latest_version="2.5.0", received_bytes=10,
            total_bytes=10, error_code=None, can_install=True)
        controller = _Controller(ready)
        st = types.SimpleNamespace(
            update_controller=controller, update_state=ready,
            update_confirming=True, update_frozen=False,
            recorder=None, recording_finalizer=None,
            convert_queue=types.SimpleNamespace(busy=True), lang="en",
            display=types.SimpleNamespace(alert=mock.Mock()),
            cfg_path=Path("settings.json"), original_cwd=Path.cwd())

        commands.apply_menu_action(st, ("button", "update_confirm"))
        self.assertFalse(st.update_confirming)
        self.assertEqual(controller.arm_calls, [])
        st.display.alert.assert_called_once()

    def test_armed_update_cancels_if_work_became_busy(self):
        controller = _Controller(
            types.SimpleNamespace(phase="installing", latest_version="2.5.0",
                                  received_bytes=0, total_bytes=0,
                                  error_code=None, can_install=False),
            armed=True)
        st = types.SimpleNamespace(
            update_controller=controller, update_frozen=True, running=True,
            recorder=None, recording_finalizer=object(),
            convert_queue=types.SimpleNamespace(busy=False))

        commands.service_updates(st)
        self.assertEqual(controller.cancel_calls, 1)
        self.assertEqual(controller.proceed_calls, 0)
        self.assertFalse(st.update_frozen)
        self.assertTrue(st.running)
        self.assertEqual(st.update_state.phase, "error")
        self.assertEqual(st.update_state.error_code, "prepare_failed")
        self.assertIsNotNone(_find(_menu(update_phase=st.update_state.phase,
                                         update_error_code=st.update_state.error_code),
                                   "button", "update_retry"))

    def test_armed_update_freezes_only_after_idle_recheck_and_proceeds(self):
        controller = _Controller(
            types.SimpleNamespace(phase="installing", latest_version="2.5.0",
                                  received_bytes=0, total_bytes=0,
                                  error_code=None, can_install=False),
            armed=True)
        st = types.SimpleNamespace(
            update_controller=controller, update_frozen=False, running=True,
            recorder=None, recording_finalizer=None,
            convert_queue=types.SimpleNamespace(busy=False))

        commands.service_updates(st)
        self.assertEqual(controller.cancel_calls, 0)
        self.assertEqual(controller.proceed_calls, 1)
        self.assertTrue(st.update_frozen)
        self.assertFalse(st.running)
        commands.apply_menu_action(st, ("button", "update_cancel"))
        self.assertTrue(st.update_frozen)
        self.assertFalse(st.running)
        self.assertEqual(controller.cancel_calls, 1)

    def test_failed_proceed_unfreezes_and_keeps_parent_running(self):
        controller = _Controller(
            types.SimpleNamespace(phase="installing", latest_version="2.5.0",
                                  received_bytes=0, total_bytes=0,
                                  error_code=None, can_install=False),
            armed=True, proceed=False)
        st = types.SimpleNamespace(
            update_controller=controller, update_frozen=False, running=True,
            recorder=None, recording_finalizer=None,
            convert_queue=types.SimpleNamespace(busy=False))

        commands.service_updates(st)

        self.assertEqual(controller.proceed_calls, 1)
        self.assertFalse(st.update_frozen)
        self.assertTrue(st.running)
        self.assertEqual(st.update_state.phase, "error")
        self.assertEqual(st.update_state.error_code, "prepare_failed")
        error_menu = _menu(update_phase=st.update_state.phase,
                           update_error_code=st.update_state.error_code)
        self.assertIsNotNone(_find(error_menu, "button", "update_retry"))

    def test_cancel_menu_event_cancels_preparation(self):
        menu = _menu(update_phase="installing",
                     update_latest_version="2.5.0")
        button = _find(menu, "button", "update_cancel")
        self.assertIsNotNone(button)
        action = _click(menu, button)
        self.assertEqual(action, [("button", "update_cancel")])

        controller = _Controller(types.SimpleNamespace(
            phase="installing", latest_version="2.5.0", received_bytes=0,
            total_bytes=0, error_code=None, can_install=False))
        st = types.SimpleNamespace(
            update_controller=controller, update_confirming=False,
            update_frozen=False, running=True)
        commands.apply_menu_action(st, action[0])

        self.assertEqual(controller.cancel_calls, 1)
        self.assertEqual(st.update_state.phase, "error")
        self.assertTrue(st.running)

    def test_retry_menu_event_starts_a_new_check(self):
        menu = _menu(update_phase="error", update_error_code="network")
        button = _find(menu, "button", "update_retry")
        self.assertIsNotNone(button)
        action = _click(menu, button)
        self.assertEqual(action, [("button", "update_retry")])

        controller = _Controller(types.SimpleNamespace(
            phase="error", latest_version=None, received_bytes=0,
            total_bytes=0, error_code="network", can_install=False))
        commands.apply_menu_action(
            types.SimpleNamespace(update_controller=controller), action[0])

        self.assertEqual(controller.check_calls, 1)

    def test_official_release_menu_event_opens_controller_fallback(self):
        menu = _menu(update_phase="error", update_error_code="network")
        button = _find(menu, "button", "update_official_release")
        self.assertIsNotNone(button)
        action = _click(menu, button)
        self.assertEqual(action, [("button", "update_official_release")])

        controller = _Controller(types.SimpleNamespace(
            phase="error", latest_version=None, received_bytes=0,
            total_bytes=0, error_code="network", can_install=False))
        controller.official_release_url = "https://example.invalid/releases"
        st = types.SimpleNamespace(update_controller=controller)
        with mock.patch.object(commands.webbrowser, "open") as open_url:
            commands.apply_menu_action(st, action[0])

        open_url.assert_called_once_with(controller.official_release_url)

    def test_frozen_update_rejects_record_and_conversion_starts(self):
        queue = mock.Mock()
        st = types.SimpleNamespace(
            update_frozen=True, lang="en", display=types.SimpleNamespace(
                alert=mock.Mock(), menu=types.SimpleNamespace(page="convert")),
            tray_commands=mock.Mock(), convert_queue=queue, running=True)

        commands.apply_menu_action(st, ("button", "record"))
        commands.add_conversions(st, [Path("clip.mp4")])
        commands.convert_row_action(st, "convert_job:3:retry")
        self.assertFalse(st.tray_commands.put.called)
        queue.add.assert_not_called()
        queue.act.assert_not_called()

        st.tray_commands.get_nowait = mock.Mock(side_effect=["record", __import__(
            "queue").Empty])
        st.recorder = None
        st.recording_finalizer = None
        with mock.patch.object(commands, "start_recorder") as start_recorder:
            commands.drain_commands(st)
        start_recorder.assert_not_called()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo-dir", type=Path)
    options, test_names = parser.parse_known_args()
    if options.demo_dir is not None:
        captures = capture_update_demos(options.demo_dir)
        print(f"Wrote {len(captures)} full-frame captures and six menu-region captures to {options.demo_dir}")
    unittest.main(argv=[sys.argv[0], *test_names])
