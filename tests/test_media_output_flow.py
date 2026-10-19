"""Media output stays responsive and reports only verified final results."""
from __future__ import annotations

import queue
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import commands  # noqa: E402
from recorder import RecordingError, RecordingResult, RecordingStatus  # noqa: E402


class FakeRecorder:
    FINISH_TIMEOUT_S = 30.0

    def __init__(self, path: str, width: int = 8, height: int = 4,
                 fps: float = 30.0, audio: bool = True, codec: str = "auto"):
        # codec: the recording codec the user picked (#174).
        self.codec = codec
        self.path = path
        self.partial_path = f"{path}.partial"
        self.width = width
        self.height = height
        self.fps = fps
        self.codec = "fake_nvenc"
        self.audio_enabled = audio
        self.written = 7
        self.dropped = 1
        self.duration_ms = 1250.0
        self.status = RecordingStatus.RECORDING
        self.result = None

    def finish(self) -> bool:
        if self.status is not RecordingStatus.RECORDING:
            return False
        self.status = RecordingStatus.FINALIZING
        return True

    def wait(self, _timeout=None):
        return self.result

    def close(self, timeout=None):  # noqa: ARG002
        if self.result is None:
            raise AssertionError("the fake finalizer was forced before its deadline")
        return self.result


def state(cfg: dict | None = None):
    alerts: list[str] = []
    return SimpleNamespace(
        cfg=dict(cfg or {}), recorder=None, recording_finalizer=None,
        recording_finalize_deadline=0.0, last_recording={},
        tray_commands=queue.Queue(), running=True, width=8, height=4,
        record_audio=True, lang="en", shot_dialog_open=False,
        pending_shot=None, shot_rgba=None, shot_paths=queue.Queue(),
        display=SimpleNamespace(
            alert=lambda message, *args: alerts.append(str(message)),
            get_hwnd=lambda: 0,
            menu=SimpleNamespace(set_state=lambda _payload: None)),
        alerts=alerts,
    )


def check_recording_flow(tmp: Path) -> None:
    st = state({"recording_dir": str(tmp)})
    made: list[FakeRecorder] = []
    real = commands.VideoRecorder

    def factory(*args, **kwargs):
        rec = FakeRecorder(*args, **kwargs)
        made.append(rec)
        return rec

    commands.VideoRecorder = factory
    try:
        st.tray_commands.put("record")
        assert commands.drain_commands(st)
        assert st.recorder is made[0]
        assert Path(st.recorder.path).parent == tmp
        assert Path(st.recorder.path).suffix == ".mp4"

        started = time.perf_counter()
        st.tray_commands.put("record")
        assert commands.drain_commands(st)
        assert time.perf_counter() - started < 0.1
        assert st.recorder is None
        assert st.recording_finalizer is made[0]
        assert made[0].status is RecordingStatus.FINALIZING

        final_path = str(tmp / "verified.mp4")
        made[0].result = RecordingResult(
            RecordingStatus.PUBLISHED, final_path, None)
        commands.poll_recording_finalizer(st)
        assert st.recording_finalizer is None
        assert st.last_recording == {
            "container": "MP4", "codec": "fake_nvenc", "fps": 30.0,
            "audio": True, "path": final_path, "status": "published"}
        assert any(final_path in alert for alert in st.alerts)

        failed = FakeRecorder(str(tmp / "broken.mp4"), audio=False)
        failed.finish()
        error = RecordingError("verify", ValueError("bad moov"))
        failed.result = RecordingResult(
            RecordingStatus.FAILED, failed.partial_path, error)
        st.recording_finalizer = failed
        st.recording_finalize_deadline = time.monotonic() + 30
        commands.poll_recording_finalizer(st)
        assert st.last_recording["status"] == "failed"
        assert st.last_recording["path"] == failed.partial_path
        assert any("verify" in alert and failed.partial_path in alert
                   for alert in st.alerts)
    finally:
        commands.VideoRecorder = real


def check_automatic_screenshot(tmp: Path) -> None:
    st = state({"screenshot_mode": "auto", "screenshot_format": "png",
                "screenshot_dir": str(tmp)})
    saved = []
    dialogs_opened = []
    real_save = commands.save_screenshot
    real_open = commands.open_save_dialog
    commands.save_screenshot = lambda _st, path, rgba: saved.append(
        (path, rgba.copy()))
    commands.open_save_dialog = lambda _st: dialogs_opened.append(True)
    try:
        commands.request_screenshot(st)
        source = np.zeros((3, 4, 4), dtype=np.uint8)
        source[..., 0] = 37
        assert commands.freeze_screenshot_frame(st, source)
        source[..., 0] = 99
        assert not dialogs_opened
        assert len(saved) == 1 and saved[0][0].suffix == ".png"
        assert saved[0][0].parent == tmp
        assert int(saved[0][1][..., 0].max()) == 37
        assert st.shot_rgba is None
    finally:
        commands.save_screenshot = real_save
        commands.open_save_dialog = real_open


def main() -> int:
    with tempfile.TemporaryDirectory() as temp:
        tmp = Path(temp)
        check_recording_flow(tmp)
        check_automatic_screenshot(tmp)
    print("OK: recording finalizes off-thread and auto screenshots keep frozen pixels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
