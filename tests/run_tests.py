"""Run everything: the static checks, the regression tests, the smoke test.

There is one reason this file exists. The tests were run by hand, one at a
time, and that is how a regression shipped: audit #3 broke the shared-memory
pixel channel, `test_out_shm.py` caught it, and nobody ran `test_out_shm.py`.
One command, one verdict, a non-zero exit on any failure.

    runtime\\python.exe tests\\run_tests.py            static + tests + smoke
    runtime\\python.exe tests\\run_tests.py --gui      ... and the full GUI cycle
    runtime\\python.exe tests\\run_tests.py --only out_shm      one test by name
    runtime\\python.exe tests\\run_tests.py --no-smoke          skip the smoke test

Two of the stages take over the screen for a few seconds each (the overlay is
raised for real) and the machine should be left alone while they run. Nothing
here needs the network except the release-notes check inside autocheck.

Only tests tracked by git are run: the working copy also holds throwaway
probes, and those are nobody's regression suite.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Console encodings, both directions: the children are told to print utf-8 so
# their output decodes here, and our own stdout replaces anything the console
# codepage cannot represent instead of dying on it.
CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8")
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent  # the project root (tests/ lives inside it)
PY = ROOT / "runtime" / "python.exe"
TIMEOUT = 600
# How long to wait for the previous test's processes to die before
# starting the next one. See settle().
SETTLE_LIMIT = 10.0
#: (label, seconds) per test - reported in the summary so a run where
#: nothing ever waited is visible as evidence, not as silence.
SETTLES: list = []

GROUP_UNIT_STATIC = "unit/static"
GROUP_WARP = "WARP"
GROUP_GPU = "GPU"
GROUP_GUI_E2E = "GUI-E2E"
GROUP_ORDER = (GROUP_UNIT_STATIC, GROUP_WARP, GROUP_GPU, GROUP_GUI_E2E)

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_SKIP = "SKIP"
STATUS_ERROR = "ERROR"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_ORDER = (STATUS_PASS, STATUS_FAIL, STATUS_SKIP,
                STATUS_ERROR, STATUS_TIMEOUT)
BLOCKING_STATUSES = frozenset({STATUS_FAIL, STATUS_ERROR, STATUS_TIMEOUT})

# A child may explain that one optional sub-check was skipped in prose.  Only
# a dedicated, canonical result line turns the whole test into SKIP.
CANONICAL_SKIP_RE = re.compile(
    r"^\s*(?:SKIP(?:\s*:|\s*$)|\[SKIP\](?:\s|$))", re.MULTILINE)

# What each test is for, in one line - a failing name should not send anyone
# digging through the file to find out what broke.
ABOUT = {
    "test_apply_debounce.py": "the settings apply is debounced: one rebuild at the end of a drag (#115)",
    "test_wire_msg_numbers.py": "every wire command has its own message number and keeps the prepared frame",
    "test_pointer_and_monitor_target.py": "the pointer stays put and a monitor change drops the window target (#96)",
    "test_frame_pacing.py": "30/60/custom/unlimited pacing and real-work NR rate accounting",
    "test_media_output_flow.py": "nonblocking recording publication and dialog-free frozen screenshots",
    "test_compatibility_preflight.py": "CompatibilityKey cache, fail-closed N/N verdicts and quarantine policy",
    "test_compatibility_runtime.py": "native synthetic preflight runs before capture and rejects passthrough/skip",
    "test_diagnostic_bundle.py": "support bundle is complete, atomic, bounded and privacy-scrubbed",
    "test_low_cost_off.py": "NR OFF closes capture/presentation and produces no background frames",
    "test_nvofa_controls.py": "motion backend selection, CPU fallback and scene tracking without NVIDIA hardware",
    "test_build_scripts_portable.py": "build scripts resolve the compiler instead of hardcoding a path",
    "test_bypass.py": "explicit OFF consumers receive raw frames and the pipeline survives",
    "test_out_ring.py": "read_out reuses its buffers and never overwrites one in use",
    "test_verdict_forget.py": "the feature-18 verdict dies with the worker that gave it",
    "test_audio_limiter.py": "the soft limiter folds only the peaks - a packet with one keeps its quiet samples",
    "test_worker_zorder_and_safety.py": "the worker keeps the panel above the picture; HDR/FG, presenter, descriptor, DLL-gate and recorder fixes",
    "test_reader_resilience.py": "a dead worker reaches every wait at once, frames met during a probe are kept, late acks stay with their command",
    "test_config_resilience.py": "BOM and broken configs, gpu: null saves, hostile values, another copy's autostart, a release over another",
    "test_conversion_colour_and_hdr.py": "converted streams carry their colour tags, the timescale is capped, HDR sources are refused",
    "test_ui_containment.py": "failures stay local, taken keys are refused, a failed probe restores the source, no focus theft",
    "test_audio_pack.py": "the audio format structs are byte-packed, truncated formats rejected",
    "test_bake_menu.py": "an open menu is baked into the file, a closed one is not",
    "test_bake_menu_position.py": "the baked menu lands where the user saw it, not at the frame centre",
    "test_fg_row_fits.py": "the FG row's name fits every locale and its cells take the hover",
    "test_ui_scale_fit.py": "a first launch fits the panel to the screen, and never grows it",
    "test_ngx_params_exist.py": "every DLSSNR parameter the worker sets exists in the runtime",
    "test_nr_passes_wire.py": "the NR cascade reaches the worker, and its last pass lands in nr_out",
    "test_per_pass_params.py": "passes 2+ can carry their own parameters, and a missing set means the main one",
    "test_nr_pass_resources.py": "unused neural passes retire safely without discarding active history",
    "test_reveal_below_panel.py": "the picture window is revealed under the panel, not over it and back",
    "test_hud_handle_before_worker.py": "the panel publishes its handle before the worker reads it",
    "test_screenshot_opaque.py": "a PNG screenshot carries no transparency over the panel",
    "test_foreign_window_rule.py": "shell chrome and our own windows never count as covering us",
    "test_i18n_not_mixed.py": "no language's interface shows another language's words",
    "test_zorder_walk_depth.py": "the z-order walk reaches our own windows past any helper stack",
    "test_convert_queue.py": "the conversion queue: order, per-file settings, progress, stop, retry, row text",
    "test_convert_settings.py": "conversion settings agree from config to converter and survive a restart",
    "test_convert_page.py": "the conversion page and the Convert cell fit in twelve languages",
    "test_convert_pipeline.py": "the converter's overlapped stages write the file the serial loop writes",
    "test_convert_orientation.py": "photos and videos keep their turn, profile and 16-bit range; too large is named",
    "test_mini_mode.py": "mini mode keeps the chosen rows, and the dot, label and control all choose",
    "test_media_convert.py": "a file goes through the network: formats, naming, and a real still on the GPU",
    "test_vfr_timestamps.py": "a variable-rate file is written on its own grid, not on its average rate",
    "test_gpu_recorder.py": "recording on the GPU: every codec, colour, resize, A/V sync, and the worker's stop, failure and loss",
    "test_early_reply.py": "the worker answers a frame once it is queued: one answer per frame, early only where safe, and the wait cut",
    "test_hdr_resize.py": "with HDR on, a captured window grows and shrinks without ending the worker (runs only on an HDR display)",
    "test_hdr_recording.py": "with HDR on, a GPU recording is HDR10: 10-bit, tagged BT.2020/PQ, the desktop's levels, FG off and on (HDR display only)",
    "test_worker_scene.py": "the worker decides the scene cut (no capture round trip per frame): the client's score, one reset per cut",
    "test_cascade_work_size.py": "multipass at native steps the work size 2 px aside so the cascade runs (#110), on every sizing path",
    "test_cascade_native.py": "multipass at native on the worker: 4 of 4 passes build and run; 1:1 builds one and says why",
    "test_worker_orphan.py": "a dead parent cannot leave a worker running (the Job Object tie)",
    "test_diag_settings_complete.py": "every shipped setting is reported in a diagnostic package or withheld on purpose",
    "test_worker_log_prefixes.py": "every prefix the worker logs under reaches the shared log",
    "test_fps_overlay.py": "the on-screen counter says the same as the status line, in the corner it names",
    "test_to_tray.py": "minimise and close to tray, without stopping the pass",
    "test_adaptive_exposure.py": "adaptive exposure brightens dark scenes, lit scenes untouched",
    "test_capture_visibility.py": "outside capture sees the overlay only without WDA",
    "test_config.py": "the config loader validates, clamps and resolves",
    "test_theme_rebuild.py": "the chosen theme survives a pipeline rebuild",
    "test_gpu_select.py": "the GPU picker drives config, environment and restart",
    "test_hdr_switch.py": "HDR compatibility is off by default and switchable in 12 languages",
    "test_settings_hints.py": "every hint is one line and fits its row, in 12 languages",
    "test_rebuild_warmup.py": "a rebuild warms up briefly; only a cold launch pays 120 frames",
    "test_param_apply.py": "a parameter change applies without rebuilding the feature",
    "test_param_effect.py": "every menu parameter changes the picture; the dead ones are named",
    "test_direct_reconstruction.py": "the two Boost composites are a live switch, and they differ",
    "test_motion_trust.py": "vectors where nothing moved are dropped before NGX sees them",
    "test_live_resize.py": "a window resize reconfigures the worker instead of replacing it",
    "test_revive_not_disarmed.py": "a transient failure still gets its one automatic revive",
    "test_hotkeys_resume_on_close.py": "every route out of the menu gives the keyboard back",
    "test_shm_close_releases_out.py": "closing the frame buffer releases every section",
    "test_teardown_always_completes.py": "every step of the teardown is guarded",
    "test_config_hostile_values.py": "a hand-edited config never aborts the launch",
    "test_drag_not_stuck_on_close.py": "a drag never outlives the menu",
    "test_dropdown_inside_viewport.py": "an up-opening list stays out of the title bar",
    "test_key_hash_not_repeated.py": "the runtime is hashed once, not on every call",
    "test_release_lists_pinned.py": "the release contract lists cannot change in silence",
    "test_text_fits_control.py": "every drawn string fits the control that holds it",
    "test_shot_dir_exists_check.py": "the save dialog opens in a folder that exists",
    "test_status_line_card_name.py": "the status line always says which card runs",
    "test_nr_truthful_state.py": "the state cell does not claim processing while the worker is not processing",
    "test_every_control_clickable.py": "every control on every page survives a real click on the real state object",
    "test_i18n_translated.py": "every user-facing string is really translated",
    "test_capture_deep_scanout.py": "a 10-bit display cannot flip the capture format per frame",
    "test_dda_output5_released.py": "the DXGI output reference is released on every path",
    "test_rnsz_failure_stops.py": "a failed RNSZ stops instead of answering ok",
    "test_fg_multiplier_stepdown.py": "a refused FG multiplier steps down, FG keeps running",
    "test_fg_bypass_present.py": "FG runs while NR is off: the bypass path asks the presenter instead of stopping it",
    "test_release_documents_agree.py": "the builder and the verifier name the same four release documents",
    "test_self_test_uses_runtime.py": "--test creates and evaluates through the same runtime as the live path",
    "test_log_timestamps.py": "every untimed log line gets a stamp; the close button logs the close",
    "test_log_rotation.py": "an oversized NeuralScreen.log is moved aside at startup; an open one is kept",
    "test_diagnostic_settings.py": "the bundle carries the product settings; the header carries the date and the switches",
    "test_command_recording_reuse.py": "command recording is a fixed ring reused behind its fence, not a per-frame cost",
    "test_taskbar_foreign_minimize.py": "a minimised foreign window must not open our menu",
    "test_taskbar_first_click.py": "the FIRST click on the taskbar button opens the menu",
    "test_top_window_virtual_desktop.py": "the z-order guard sees windows on every monitor",
    "test_veil_never_sticks.py": "the mode-switch veil always comes down, fade or not",
    "test_spout_adapter.py": "the Spout bridge lives on the card the worker runs on",
    "test_present_window_leak.py": "closing the picture window destroys it, six cycles",
    "test_pixels_after_resize.py": "a resize does not put a hole in the recording",
    "test_alert_position.py": "alerts sit at the top centre of the screen, not the overlay",
    "test_stdout_noise.py": "a stray printf cannot corrupt the worker protocol",
    "test_hdr_shaders.py": "the HDR capture and composite shaders, run on WARP",
    "test_hdr_capture.py": "the whole HDR path on real hardware (runs only on an HDR display)",
    "test_capture_format_stability.py": "SDR DDA pins BGRA8 across unstable 10-bit scan-out",
    "test_gpu_rollback.py": "a card that cannot run the network reverts itself",
    "test_monitor_resize.py": "a desktop resolution change rebuilds once, after it settles",
    "test_save_dialog.py": "the Save As struct is valid; the fallback still catches",
    "test_window_follow_size.py": "capture size vs frame size: no rebuild loop",
    "test_minimised_window_menu.py": "the menu survives a minimised window; the HUD still hides without it",
    "test_module_layers.py": "no module imports main, no dangling names, all import clean",
    "test_config_atomic.py": "the config write is atomic and persists profile/params/monitor",
    "test_dred_diag.py": "the worker logs DRED/device-removed diagnostics at startup",
    "test_failure_stages.py": "native failures are staged; fence/device loss stops GPU reuse",
    "test_env_header.py": "the log header carries version/OS/HDR and survives broken probes",
    "test_hotkey_once.py": "one press of a hotkey fires exactly one command",
    "test_hotkey_rebind.py": "hotkeys can be reassigned from the config",
    "test_hotkey_bindings.py": "parsing, aliases and defaults survive binding changes",
    "test_hotkey_held.py": "a key held at startup is the baseline, not an event",
    "test_hotkey_remap_no_fire.py": "a key pressed during a remap is the baseline, not an event",
    "test_header_footer.py": "the header collapse icon and the one-window footer button",
    "test_i18n.py": "every language has the same keys, none empty",
    "test_menu_position.py": "the menu position is fixed - saved offset honoured, clamped",
    "test_monitor_identity.py": "monitors are identified by DXGI devicename, not by position",
    "test_multi_gpu_monitor.py": "a monitor keeps its dxcam adapter/output pair across GPU switches",
    "test_monitor_switch.py": "a monitor switch cannot crash the capture - stale indices fall back",
    "test_mss_fallback.py": "the GDI fallback (mss) opens when dxcam cannot (Optimus)",
    "test_menu_scroll.py": "the menu scrolls and the wheel lands where it should",
    "test_menu_state_keys.py": "every key menu_payload produces reaches the menu",
    "test_silent_spots.py": "the silent spots from issue #29 speak: log, alert, split warning",
    "test_single_instance_message.py": "a second copy's show-yourself message is registered, the old number only posted",
    "test_monitor_origin.py": "the chosen monitor's origin reaches overlay and worker",
    "test_monitor_windows.py": "both output windows really follow the chosen monitor",
    "test_overlay_toolwindow.py": "the overlay is a tool window - one taskbar button only",
    "test_motion_small.py": "the downscaled motion field is upscaled on the GPU",
    "test_mv_validation.py": "noise-floor motion vectors are zeroed, real motion survives",
    "test_ngx_result_names.py": "every NVSDK_NGX failure code the header defines is named in the log",
    "test_nr_small.py": "the reduced-resolution mode produces a real picture",
    "test_out_shm.py": "the pixel channel through shared memory",
    "test_out_status.py": "0x00000000 is not an error, only 0xBAD00000 raises",
    "test_odd_frame_size.py": "a frame whose row pitch needs padding survives",
    "test_recorder_audio.py": "the audio track keeps up with the video",
    "test_recorder_high_rate_audio.py": "a 192 kHz loopback is resampled to AAC without aborting video",
    "test_recorder_fallback.py": "the NVENC codec chain falls back AV1->HEVC->H.264",
    "test_recorder_lifecycle.py": "recordings drain, verify and publish atomically from .partial",
    "test_rec_indicator.py": "the recording indicator draws only while recording, never in the file",
    "test_shot_dir.py": "the screenshot folder is configured, persisted and shown on the button",
    "test_screenshot_before_dialog.py": "Save As opens only after a screenshot frame is frozen",
    "test_spout_toggle.py": "the Spout2 toggle drives the config, the environment and the restart",
    "test_swappable_runtime.py": "the swappable runtime is driven by the config",
    "test_presets.py": "user presets load, apply, and survive a broken config",
    "test_recovery.py": "only 0xBAD00001 is a hard failure, everything else auto-revives",
    "test_recorder_thread.py": "the encoder thread and a clean close",
    "test_residual.py": "the matched residual composite keeps 1:1 detail at reduced work",
    "test_residual_split.py": "residual and the wipe compose in the same frame",
    "test_resize.py": "the on-the-fly resize reconfigures the feature without a restart",
    "test_reveal.py": "the present window stays hidden until the first Present",
    "test_focus_z_order.py": "focused target stays below the worker picture and HUD",
    "test_split.py": "the before/after wipe leaves the left side untouched",
    "test_taskbar_window.py": "the taskbar button exists, opens the menu, closes cleanly",
    "test_taskbar_show.py": "duplicate taskbar activation shows the menu without closing it",
    "test_vdesk_follow.py": "the overlay follows the taskbar window across virtual desktops",
    "test_ui_buttons.py": "every control on every page fires the right command",
    "test_controls_visual.py": "the switch, the slider ticks and the Quit edge draw",
    "test_keyboard_navigation.py": "Tab, arrows, activation, Esc and focus auto-scroll",
    "test_light_theme_contrast.py": "light/dark text, controls and focus meet WCAG contrast",
    "test_wgc_capture.py": "the worker captures one window (the single-window input)",
    "test_zorder_decision_log.py": "the z-order guard names what it saw and what it decided",
    "test_window_filter.py": "the window list holds only real taskbar windows",
    "test_window_labels.py": "window titles stay clean while HWND remains the identity",
    "test_worker_reply.py": "raw native tests consume explicit CACK before command replies",
    "test_windows_page.py": "the windows page lists, highlights and switches",
    "test_window_mode.py": "the one-window hotkey switches the pipeline and back",
    "test_window_surround.py": "a window-sized frame's surround is keyed, the layer is really keyed",
    "test_window_mode_menu.py": "the menu stays fully visible across the window-mode switch",
    "test_switch_veil.py": "the mode-switch veil eases in/out and owns the layer",
    "test_runner_reporting.py": "pure runner routing, SKIP classification and exact summaries",
}


# Every test belongs to exactly one execution class.  This inventory is
# deliberately explicit: adding a tracked test without choosing its class is
# a runner error.  Tests that render UI into dummy/in-memory surfaces stay in
# unit/static; GUI-E2E is reserved for real desktop/app-shell scenarios.
TEST_GROUPS = {
    GROUP_UNIT_STATIC: frozenset({
        "test_config_resilience.py",
        "test_conversion_colour_and_hdr.py",
        "test_reader_resilience.py",
        "test_ui_containment.py",
        "test_worker_zorder_and_safety.py",
        "test_alert_font_size.py",
        "test_alert_position.py",
        "test_apply_debounce.py",
        "test_wire_msg_numbers.py",
        "test_audio_limiter.py",
        "test_audio_pack.py",
        "test_bake_menu.py",
        "test_bake_menu_position.py",
        "test_boost_toggle.py",
        "test_build_scripts_portable.py",
        "test_capture_format_stability.py",
        "test_cascade_work_size.py",
        "test_channel_flags.py",
        "test_choice_hint_hit.py",
        "test_compatibility_preflight.py",
        "test_compatibility_runtime.py",
        "test_config.py",
        "test_config_atomic.py",
        "test_config_schema.py",
        "test_controls_visual.py",
        "test_crash_report.py",
        "test_diagnostic_bundle.py",
        "test_env_header.py",
        "test_every_control_clickable.py",
        "test_esc_order.py",
        "test_fg_row_fits.py",
        "test_follow_monitor_init.py",
        "test_fps_overlay.py",
        "test_frame_pacing.py",
        "test_framegen_controls.py",
        "test_gpu_alert_path.py",
        "test_gpu_identity.py",
        "test_gpu_mark.py",
        "test_gpu_pending_wgc.py",
        "test_gpu_rollback.py",
        "test_gpu_select.py",
        "test_hard_failure_window.py",
        "test_hdr_notice.py",
        "test_hdr_switch.py",
        "test_header_footer.py",
        "test_highlight_origin.py",
        "test_hotkey_bindings.py",
        "test_hotkey_held.py",
        "test_hotkey_once.py",
        "test_i18n_not_mixed.py",
        "test_zorder_walk_depth.py",
        "test_convert_queue.py",
        "test_convert_settings.py",
        "test_convert_page.py",
        "test_convert_pipeline.py",
        "test_convert_orientation.py",
        "test_mini_mode.py",
        "test_vfr_timestamps.py",
        "test_worker_orphan.py",
        "test_diag_settings_complete.py",
        "test_worker_log_prefixes.py",
        "test_hotkey_rebind.py",
        "test_hotkey_remap_no_fire.py",
        "test_i18n.py",
        "test_i18n_direct_keys.py",
        "test_keyboard_navigation.py",
        "test_leak_list.py",
        "test_light_theme_contrast.py",
        "test_live_resize.py",
        "test_ngx_params_exist.py",
        "test_nr_passes_wire.py",
        "test_nr_pass_resources.py",
        "test_per_pass_params.py",
        "test_reveal_below_panel.py",
        "test_hud_handle_before_worker.py",
        "test_screenshot_opaque.py",
        "test_foreign_window_rule.py",
        "test_to_tray.py",
        "test_ui_scale_fit.py",
        "test_veil_never_sticks.py",
        "test_revive_not_disarmed.py",
        "test_hotkeys_resume_on_close.py",
        "test_shm_close_releases_out.py",
        "test_teardown_always_completes.py",
        "test_config_hostile_values.py",
        "test_drag_not_stuck_on_close.py",
        "test_dropdown_inside_viewport.py",
        "test_key_hash_not_repeated.py",
        "test_release_lists_pinned.py",
        "test_text_fits_control.py",
        "test_shot_dir_exists_check.py",
        "test_status_line_card_name.py",
        "test_i18n_translated.py",
        "test_capture_deep_scanout.py",
        "test_dda_output5_released.py",
        "test_rnsz_failure_stops.py",
        "test_fg_multiplier_stepdown.py",
        "test_fg_bypass_present.py",
        "test_release_documents_agree.py",
        "test_self_test_uses_runtime.py",
        "test_log_timestamps.py",
        "test_log_rotation.py",
        "test_diagnostic_settings.py",
        "test_command_recording_reuse.py",
        "test_taskbar_foreign_minimize.py",
        "test_taskbar_first_click.py",
        "test_capture_output_log.py",
        "test_top_window_virtual_desktop.py",
        "test_low_cost_off.py",
        "test_media_output_flow.py",
        "test_menu_position.py",
        "test_menu_scroll.py",
        "test_menu_state_keys.py",
        "test_minimised_window_menu.py",
        "test_module_layers.py",
        "test_monitor_hint.py",
        "test_monitor_identity.py",
        "test_monitor_origin.py",
        "test_monitor_resize.py",
        "test_monitor_switch.py",
        "test_motion_capture_order.py",
        "test_motion_floor.py",
        "test_motion_trust.py",
        "test_mss_fallback.py",
        "test_mss_identity.py",
        "test_multi_gpu_monitor.py",
        "test_mv_validation.py",
        "test_ngx_result_names.py",
        "test_nvofa_confidence_metrics.py",
        "test_nvofa_controls.py",
        "test_out_ring.py",
        "test_out_status.py",
        "test_preset_roundtrip.py",
        "test_presets.py",
        "test_pointer_and_monitor_target.py",
        "test_protocol_sizes.py",
        "test_rebuild_warmup.py",
        "test_rec_indicator.py",
        "test_recorder_lifecycle.py",
        "test_recovery.py",
        "test_release_contract.py",
        "test_resolution_limits.py",
        "test_revive_race.py",
        "test_revive_warmup.py",
        "test_runner_reporting.py",
        "test_save_dialog.py",
        "test_screenshot_before_dialog.py",
        "test_settings_hints.py",
        "test_silent_spots.py",
        "test_shot_dir.py",
        "test_shot_unicode.py",
        "test_single_instance_message.py",
        "test_status_fg_off.py",
        "test_spout_shutdown.py",
        "test_spout_toggle.py",
        "test_state_contract.py",
        "test_swappable_runtime.py",
        "test_switch_veil.py",
        "test_taskbar_show.py",
        "test_theme_rebuild.py",
        "test_ui_buttons.py",
        "test_vdesk_follow.py",
        "test_verdict_classifier.py",
        "test_verdict_forget.py",
        "test_wait_rack_eof.py",
        "test_window_filter.py",
        "test_window_follow_size.py",
        "test_window_labels.py",
        "test_window_surround.py",
        "test_windows_page.py",
        "test_worker_reply.py",
        "test_zorder_decision_log.py",
    }),
    GROUP_WARP: frozenset({
        "test_hdr_shaders.py",
        "test_quality_gpu.py",
    }),
    GROUP_GPU: frozenset({
        # Starts the real program with its worker twice (and the log it
        # writes is the program's own) - it sat among the unit tests, so a
        # "static only" run took the GPU and cleared NeuralScreen.log.
        "test_adaptive_exposure.py",
        "test_media_convert.py",
        "test_gpu_recorder.py",
        "test_early_reply.py",
        "test_hdr_resize.py",
        "test_hdr_recording.py",
        "test_worker_scene.py",
        "test_cascade_native.py",
        "test_adapter_agreement.py",
        "test_bypass.py",
        "test_direct_reconstruction.py",
        "test_dred_diag.py",
        "test_failure_stages.py",
        "test_feature_leak.py",
        "test_fence_waits.py",
        "test_frame_generation.py",
        "test_frame_retirement.py",
        "test_hdr_capture.py",
        "test_motion_small.py",
        "test_ngx_forwarder.py",
        "test_nr_truthful_state.py",
        "test_nr_small.py",
        "test_odd_frame_size.py",
        "test_out_shm.py",
        "test_param_apply.py",
        "test_param_effect.py",
        "test_pixels_after_resize.py",
        "test_prepared_capture.py",
        "test_present_window_leak.py",
        "test_recorder_audio.py",
        "test_recorder_fallback.py",
        "test_recorder_high_rate_audio.py",
        "test_recorder_thread.py",
        "test_residual.py",
        "test_residual_split.py",
        "test_resize.py",
        "test_spout_adapter.py",
        "test_spout_library.py",
        "test_split.py",
        "test_stdout_noise.py",
        "test_wgc_capture.py",
    }),
    GROUP_GUI_E2E: frozenset({
        "test_capture_visibility.py",
        "test_focus_z_order.py",
        "test_monitor_windows.py",
        "test_overlay_toolwindow.py",
        "test_reveal.py",
        "test_taskbar_window.py",
        "test_window_mode.py",
        "test_window_mode_menu.py",
    }),
}


def group_for_test(path: str) -> str:
    """Return the single explicit group for a test, or fail closed."""
    name = Path(path).name
    matches = [group for group, names in TEST_GROUPS.items() if name in names]
    if not matches:
        raise ValueError(f"unrouted test: {name}")
    if len(matches) != 1:
        raise ValueError(f"test has multiple groups: {name} -> {matches}")
    return matches[0]


def tracked_tests() -> list:
    try:
        out = subprocess.check_output(["git", "ls-files", "tests/test_*.py"], cwd=ROOT,
                                      text=True, encoding="utf-8", errors="replace")
        names = [Path(n.strip()).name for n in out.splitlines() if n.strip()]
        if names:
            return sorted(names)
    except Exception as exc:
        print(f"(git ls-files failed: {exc!r} - falling back to a glob)")
    return sorted(p.name for p in ROOT.glob("tests/test_*.py"))


def classify_result(returncode: int | None, stdout: str = "", stderr: str = "",
                    *, timed_out: bool = False, error: bool = False) -> str:
    """Classify one child result without touching hardware or the filesystem."""
    if timed_out:
        return STATUS_TIMEOUT
    if error or returncode is None:
        return STATUS_ERROR
    if returncode != 0:
        return STATUS_FAIL
    if CANONICAL_SKIP_RE.search(f"{stdout}\n{stderr}"):
        return STATUS_SKIP
    return STATUS_PASS


def aggregate_results(results: list[dict]) -> dict:
    """Return exact per-group and global counters for runner results."""
    groups = {
        group: {status: 0 for status in STATUS_ORDER}
        for group in GROUP_ORDER
    }
    total = {status: 0 for status in STATUS_ORDER}
    for result in results:
        group = result.get("group")
        status = result.get("status")
        if group not in groups:
            raise ValueError(f"unknown result group: {group!r}")
        if status not in total:
            raise ValueError(f"unknown result status: {status!r}")
        groups[group][status] += 1
        total[status] += 1
    return {"groups": groups, "global": total}


def exit_code_for_results(results: list[dict]) -> int:
    """FAIL/ERROR/TIMEOUT and an empty run are always non-zero."""
    if not results:
        return 1
    return int(any(r.get("status") in BLOCKING_STATUSES for r in results))


def build_jobs(test_names: list[str], argv: list[str]) -> list[dict]:
    """Build the run plan while preserving --only/--no-smoke/--gui."""
    only = None
    if "--only" in argv:
        index = argv.index("--only")
        if index + 1 >= len(argv):
            raise ValueError("--only requires a test-name fragment")
        only = argv[index + 1]

    jobs: list[dict] = []
    if only is None:
        jobs.append({
            "label": "static checks",
            "args": ["autocheck.py"],
            "note": "build, archive, docs, git",
            "group": GROUP_UNIT_STATIC,
        })
    for raw_name in sorted(test_names):
        name = Path(raw_name).name
        if only is not None and only not in name:
            continue
        jobs.append({
            "label": name,
            "args": [name],
            "note": ABOUT.get(name, ""),
            "group": group_for_test(name),
        })
    if only is None and "--no-smoke" not in argv:
        jobs.append({
            "label": "smoke",
            "args": ["autocheck.py", "--smoke"],
            "note": "launch -> processing -> exit",
            "group": GROUP_GPU,
        })
    if "--gui" in argv:
        jobs.append({
            "label": "GUI cycle",
            "args": ["autocheck.py", "--gui"],
            "note": "launch -> record -> exit",
            "group": GROUP_GUI_E2E,
        })

    # Stable sort keeps alphabetical test order inside each execution class.
    rank = {group: index for index, group in enumerate(GROUP_ORDER)}
    return sorted(jobs, key=lambda job: rank[job["group"]])


def settle(limit: float = SETTLE_LIMIT) -> float:
    """Wait until none of our processes are left, and say how long it took.

    The tests run back to back and each GUI test starts a worker that holds
    the NGX feature on the card. When the previous one has not finished
    dying, the next test waits for its first NR frame while the old worker
    is still on the GPU - and test_window_mode, which has the longest wait
    in the suite, is the one that runs out of patience. The suite was
    measuring process teardown rather than the code under test.

    Returns the seconds spent waiting (0.0 when the field was already
    clear). It never fails a run: past the limit it returns what it waited
    and the caller says so out loud, because a process that will not die is
    itself worth knowing about.
    """
    started = time.monotonic()
    while time.monotonic() - started < limit:
        out = subprocess.run(["tasklist"], capture_output=True).stdout
        text = out.decode("cp1251", errors="replace")
        if not [l for l in text.splitlines()
                if "pythonw.exe" in l or "nvngx.dll" in l]:
            break
        time.sleep(0.25)
    return time.monotonic() - started


def run(label: str, args: list, note: str = "",
        group: str = GROUP_UNIT_STATIC) -> dict:
    print(f"\n>>> [{group}] {label}" + (f"  ({note})" if note else ""))
    started = None
    try:
        waited = settle()
        SETTLES.append((label, waited))
        if waited > 0.5:
            print(f"    (waited {waited:.1f}s for the previous test's "
                  f"processes to exit)")
        started = time.monotonic()
        # Only file arguments get the tests/ prefix - flags (--smoke,
        # --gui) must pass through untouched, otherwise autocheck.py runs
        # without its flag and fails on the stale zip instead of doing
        # the smoke/GUI cycle.
        args = [a if a.startswith("tests/") or a.startswith("-")
                else f"tests/{a}" for a in args]
        r = subprocess.run([str(PY)] + args, cwd=ROOT, timeout=TIMEOUT,
                           capture_output=True, text=True, env=CHILD_ENV,
                           encoding="utf-8", errors="replace")
        took = time.monotonic() - started
        status = classify_result(r.returncode, r.stdout or "", r.stderr or "")
        lines = [line for stream in (r.stdout or "", r.stderr or "")
                 for line in stream.splitlines() if line.strip()]
        tail_size = 8 if status in BLOCKING_STATUSES else 3
        tail = lines[-tail_size:]
        if status == STATUS_SKIP:
            skip_lines = [line for line in lines
                          if CANONICAL_SKIP_RE.match(line)]
            if skip_lines and skip_lines[-1] not in tail:
                tail = [skip_lines[-1]] + tail[-2:]
        for l in tail:
            print(f"    {l}")
        print(f"    [{status}] {took:.1f}s")
        return {"label": label, "group": group,
                "status": status, "took": took}
    except subprocess.TimeoutExpired:
        took = 0.0 if started is None else time.monotonic() - started
        print(f"    [TIMEOUT] after {took:.0f}s")
        return {"label": label, "group": group,
                "status": STATUS_TIMEOUT, "took": took}
    except Exception as exc:
        took = 0.0 if started is None else time.monotonic() - started
        print(f"    [ERROR] {exc!r}")
        return {"label": label, "group": group,
                "status": STATUS_ERROR, "took": took}


def _format_counts(counts: dict) -> str:
    return " ".join(f"{status}={counts[status]}" for status in STATUS_ORDER)


def print_summary(results: list[dict]) -> None:
    summary = aggregate_results(results)
    print("\n" + "=" * 70)
    width = max(len(r["label"]) for r in results)
    for result in results:
        print(f"{result['status']:<7} [{result['group']:<11}] "
              f"{result['label']:<{width}}  {result['took']:6.1f}s")

    print("-" * 70)
    for group in GROUP_ORDER:
        print(f"{group:<11} {_format_counts(summary['groups'][group])}")
    print(f"{'GLOBAL':<11} {_format_counts(summary['global'])}")

    total_time = sum(result["took"] for result in results)
    if SETTLES:
        worst_label, worst = max(SETTLES, key=lambda item: item[1])
        print(f"settle: {sum(seconds for _, seconds in SETTLES):.1f}s total, "
              f"longest {worst:.1f}s before {worst_label}")
    print("=" * 70)

    blocking = [r for r in results if r["status"] in BLOCKING_STATUSES]
    if blocking:
        labels = ", ".join(f"{r['label']} ({r['status']})" for r in blocking)
        print(f"RESULT: FAIL - {len(blocking)} blocking result(s): {labels}")
    else:
        skipped = summary["global"][STATUS_SKIP]
        print(f"RESULT: PASS - {len(results)} checks, {skipped} skipped, "
              f"{total_time:.0f}s")


def main() -> int:
    if not PY.exists():
        print(f"no {PY} - run this with the bundled runtime")
        return 1

    try:
        jobs = build_jobs(tracked_tests(), sys.argv[1:])
    except ValueError as exc:
        print(f"RESULT: ERROR - {exc}")
        return 1
    if not jobs:
        print("RESULT: ERROR - no tests selected")
        return 1

    print(f"NeuralScreen test run - {ROOT}")
    print("The screen is taken over a few times; leave the machine alone.")
    print("=" * 70)
    SETTLES.clear()
    results = [run(job["label"], job["args"], job["note"], job["group"])
               for job in jobs]
    print_summary(results)
    return exit_code_for_results(results)


if __name__ == "__main__":
    sys.exit(main())
