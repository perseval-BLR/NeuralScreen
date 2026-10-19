"""The recording codec is a choice: Auto, AV1, HEVC or H.264 (#174).

Recording always took the first NVENC encoder that opened - AV1 on RTX
40/50 - and Premiere Pro does not import AV1, so every recording had to be
transcoded before editing. Media gains "Recording codec".

Checked without a GPU:

1. the setting is validated (case folded, anything else is auto) and saved;
2. the Media tab has the four-way switch and a pick sends it;
3. the GPU recorder is asked for the codec by the worker's number, and
   H.264 with HDR on records SDR (NVENC has no 10-bit H.264);
4. the CPU recorder tries the named encoder first and H.264 after it; auto
   keeps the whole chain.

Fails before the change: no setting, and both recorders always ran auto.

Run:  runtime\\python.exe tests\\test_record_codec.py
"""
import contextlib
import io
import os
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "app"))


def main() -> int:
    failures = []
    import settings_io
    import protocol

    # 1. Validation.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        for raw, want in (('"H264"', "h264"), ('"av1"', "av1"), ('"vp9"', "auto"), ("3", "auto")):
            path.write_text('{"record_codec": %s}' % raw, encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                got = settings_io.load_config(path).get("record_codec")
            if got != want:
                failures.append(f"record_codec {raw} loaded as {got!r}, not {want!r}")

    # 2. The menu and the command.
    import pygame
    pygame.init()
    pygame.display.set_mode((64, 64))
    import fonts
    import overlay_ui as ui
    import commands
    menu = ui.OverlayMenu(1.0, lambda size, mono=False, bold=False: fonts.load(size, mono=mono))
    menu.visible = True
    menu.page, menu.settings_tab = "settings", "rec"
    menu.set_state({"record_codec": "auto"})
    menu.layout(1920, 1080)
    if not any(i.kind == "segmented" and i.key == "record_codec" for i in menu.items):
        failures.append("the Media tab has no recording codec switch")
    if menu._pick("record_codec", "h264") != [("record_codec", "h264")]:
        failures.append("picking H.264 does not send it")
    pygame.quit()
    st = types.SimpleNamespace(cfg={})
    real_save = settings_io.save_menu_layout
    settings_io.save_menu_layout = lambda st: True
    try:
        commands.apply_menu_action(st, ("record_codec", "hevc"))
        commands.apply_menu_action(st, ("record_codec", "mpeg2"))
    finally:
        settings_io.save_menu_layout = real_save
    if st.cfg.get("record_codec") != "hevc":
        failures.append(f"the command stored {st.cfg}")

    # 3. The GPU recorder gets the codec; H.264 with HDR records SDR.
    asked = []

    class FakeGpu:
        def __init__(self, worker, reader, path, **kw):
            asked.append(kw)

    class Worker:
        def poll(self):
            return None

    real_gpu = commands.GpuRecorder
    commands.GpuRecorder = FakeGpu
    try:
        for codec, hdr in (("h264", True), ("av1", False), ("auto", True)):
            st = types.SimpleNamespace(cfg={"gpu_record": True, "hdr": hdr, "record_codec": codec},
                                       worker=Worker(), reader=object(), record_audio=False)
            with contextlib.redirect_stdout(io.StringIO()):
                commands.start_recorder(st, "x.mp4")
    finally:
        commands.GpuRecorder = real_gpu
    want = [(protocol.REC_CODEC_H264, False), (protocol.REC_CODEC_AV1, False),
            (protocol.REC_CODEC_AUTO, True)]
    got = [(kw.get("codec"), kw.get("hdr")) for kw in asked]
    if got != want:
        failures.append(f"the GPU recorder was asked {got}, not {want}")

    # 4. The CPU recorder's encoder order.
    from recorder import VideoRecorder
    for codec, want in (("auto", VideoRecorder.CODEC_CHAIN),
                        ("hevc", ("hevc_nvenc", "h264_nvenc")),
                        ("h264", ("h264_nvenc",)), ("bogus", VideoRecorder.CODEC_CHAIN)):
        if VideoRecorder.codec_chain(codec) != want:
            failures.append(f"CPU recorder chain for {codec}: {VideoRecorder.codec_chain(codec)}")

    for f in failures:
        print("  FAIL:", f)
    print("PASS" if not failures else "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
