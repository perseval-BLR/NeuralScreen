from __future__ import annotations

import sys
import time
from dataclasses import dataclass

import cv2
import numpy as np

from scene_cut import SceneCutDetector


@dataclass(slots=True)
class GuideFrame:
    motion: np.ndarray
    reset: bool
    scene_score: float


class TemporalGuideGenerator:
    """Estimate the guide buffers an encoded video does not contain."""

    #: The DIS configuration, by name: config.json's "flow_preset".
    #:
    #: "fast" is what shipped and stays the default. "ultrafast" is the
    #: measured alternative - 0.34 ms against 2.85 ms for the same 320x180
    #: pair, an 8.4x cut of the one per-frame cost Python still carries once
    #: the capture, the motion upscale and the presentation have all moved to
    #: the GPU.
    #:
    #: Why it is not the default. Scored against synthetic ground truth (a
    #: known pixel shift), ultrafast's MEAN endpoint error stays under 0.08
    #: flow px for displacements from 1 to 16 px - comfortably inside the
    #: _flow_noise_floor this class already zeroes, so on average it cannot
    #: even reach the motion field. Its TAILS are the worry: p99 0.49 px at a
    #: 1 px shift, and a maximum of 31.6 px against fast's 8.5 px at a 32 px
    #: shift. Tails are what a temporal network shows as smearing, and
    #: nothing here can judge that without the GPU pipeline and real moving
    #: content in front of a person. So it ships as a switch with its numbers
    #: attached - the way nr_small did before it became the default - and the
    #: before/after wipe is how to judge it.
    PRESETS = {
        "ultrafast": cv2.DISOPTICAL_FLOW_PRESET_ULTRAFAST,
        "fast": cv2.DISOPTICAL_FLOW_PRESET_FAST,
        "medium": cv2.DISOPTICAL_FLOW_PRESET_MEDIUM,
    }

    @classmethod
    def _preset_name(cls, name: str) -> str:
        """The validated preset key for a config value, tolerant of a typo.

        An unknown name falls back to the shipped default and says so in the
        log, the way `lang` does in the config loader: config.json is
        hand-editable, and a typo in it must not become a crash on the first
        pipeline build.

        Returns the NAME rather than the cv2 id so that self.preset can be
        what is actually running - an attribute that reported the typo back
        while DIS quietly ran something else would be a readout that lies.
        """
        key = str(name).lower()
        if key not in cls.PRESETS:
            print(f"[main] unknown flow_preset {name!r} - using 'fast'",
                  file=sys.stderr)
            return "fast"
        return key

    def __init__(self, width: int, height: int, flow_width: int = 320,
                 emit_small: bool = False, preset: str = "fast") -> None:
        """width/height is the NGX work resolution.

        emit_small=True: hand back the motion field at the optical-flow
        resolution (~320x180) instead of the work resolution. The worker then
        upscales it on the GPU and the CPU is spared ~8 ms per frame — a
        resize and a conversion of 6 million values. The vectors are in the
        same units either way (work-resolution pixels); only the grid changes.
        """
        self.width = width
        self.height = height
        scale = min(1.0, flow_width / width)
        self.flow_width = max(64, int(round(width * scale / 2) * 2))
        self.flow_height = max(64, int(round(height * scale / 2) * 2))
        self.emit_small = emit_small
        self.previous_gray: np.ndarray | None = None
        # The cut decision (app/scene_cut.py); the mean difference below
        # still gates the static screen and feeds the log.
        self._cuts = SceneCutDetector()
        # The last frame went out with the scene cut left to the worker
        # (handoff); the first one after anything else resets.
        self._handed_off = False
        self._zero_motion = np.zeros((self.height, self.width, 2), dtype=np.float16)
        self._zero_small = np.zeros((self.flow_height, self.flow_width, 2), dtype=np.float16)
        self._flow_f16 = np.empty((self.flow_height, self.flow_width, 2), dtype=np.float16)
        # Buffers for upscaling the motion field. Each frame is 3M pixels by
        # 2 channels: without reuse it cost ~11.6 ms in allocation, multiplying
        # the already-upscaled field and astype (measured, _work/bench_guides.py).
        self._flow_scaled = np.empty((self.flow_height, self.flow_width, 2), dtype=np.float32)
        self._motion_f32 = np.empty((self.height, self.width, 2), dtype=np.float32)
        self._motion_f16 = np.empty((self.height, self.width, 2), dtype=np.float16)
        self.preset = self._preset_name(preset)
        self.dis = cv2.DISOpticalFlow_create(self.PRESETS[self.preset])
        self.dis.setUseSpatialPropagation(True)
        self.dis.setFinestScale(1)
        # MV validation (Feeder 0.14 static-hypothesis pattern): DIS on a
        # static desktop produces small noise vectors (capture noise,
        # cursor jitter, UI shimmer). Vectors below the noise floor are
        # zeroed - NGX would otherwise treat them as real motion and smear
        # text/UI. The floor is in work-resolution pixels. Applying it on
        # the small flow grid erased real 1–6 pixel scrolling at high resolution.
        self._flow_noise_floor = 0.5
        # A2, the other half of MV validation: the noise floor is a test of
        # LENGTH, and a wrong vector can be long. When a window slides across
        # static text, DIS finds motion on the text as well - the text near a
        # moving edge looks explainable by a shift - and NGX smears it.
        #
        # The test that catches this is the static hypothesis: warp the
        # previous frame by the vector, and keep the vector only if it
        # explains the pixel better than standing still does. Measured here
        # against the alternative (a backward DIS pass and a
        # forward/backward consistency check) on three motion cases built
        # from real frames, at this grid:
        #
        #   cost added to the one DIS pass that already runs
        #     consistency (a second DIS + remap)   +2.1 .. +2.4 ms
        #     static hypothesis                    +0.67 .. +0.72 ms
        #   false vectors where nothing moved, before -> after
        #     a window over static text   0.6% -> 0.3% (consistency)
        #                                 0.6% -> 0.01% (static)
        #     video inside a window       3.6% -> 3.6% (consistency)
        #                                 3.6% -> 0.37% (static)
        #
        # Consistency costs three times as much and catches almost nothing:
        # on repetitive structure both directions agree on the same wrong
        # answer, so the vector comes home and passes. The static hypothesis
        # is the cheaper test AND the better one, so there is no backward
        # flow here.
        #
        # The two numbers are ours, swept on those cases, not taken from
        # anyone: window 7x7 and margin 0.5 keep 96.6% / 97.7% of the real
        # vectors where something moved. A uniform scroll keeps 79.2%, and
        # the fifth it drops sits on flat content - texture 0.7 against 8.1
        # for what it keeps - where a zero vector carries away nothing.
        #
        # On a real desktop the share is much higher than any of those
        # cases: 18-45% of the surviving vectors go, and briefly up to 94%.
        # That is not the guard misfiring. Over 29 samples of a live
        # session the texture under the dropped cells was 4.5-15.3 against
        # 10.1-33.3 under the kept ones - lower every single time, usually
        # by a factor of two and a half. A desktop is mostly flat, and a
        # vector on flat content is a guess either way.
        self._trust_window = 7
        self._trust_margin = 0.5
        # The memory the hysteresis in _moved needs: last frame's raw
        # verdict, and the verdict actually in force.
        self._trust_last = None
        self._trust_held = None
        # Reused buffers, same reason as the motion field below: this runs
        # on every moving frame.
        gy, gx = np.mgrid[0:self.flow_height, 0:self.flow_width]
        self._grid_x = gx.astype(np.float32)
        self._grid_y = gy.astype(np.float32)
        self._map_x = np.empty_like(self._grid_x)
        self._map_y = np.empty_like(self._grid_y)
        self._warped = np.empty((self.flow_height, self.flow_width), dtype=np.uint8)
        self._err_flow = np.empty((self.flow_height, self.flow_width), dtype=np.float32)
        self._err_zero = np.empty((self.flow_height, self.flow_width), dtype=np.float32)
        # What the guard is actually doing, for a live check and for a log
        # someone attaches to a ticket. One line per half minute, and only
        # while something is moving. Costs 0.116 ms per moving frame with
        # the texture half sampled - 0.7% of a 17 ms frame, and it is what
        # answered the question below.
        self._trust_frames = 0
        self._trust_alive = 0
        self._trust_dropped = 0
        self._trust_kept = 0
        self._trust_tex_gone_n = 0
        self._trust_tex_gone = 0.0
        self._trust_tex_kept = 0.0
        self._trust_said = 0.0

    @property
    def motion_width(self) -> int:
        """Width of the motion field process() hands back."""
        return self.flow_width if self.emit_small else self.width

    @property
    def motion_height(self) -> int:
        return self.flow_height if self.emit_small else self.height

    def _small_gray(self, rgba: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(rgba, cv2.COLOR_RGBA2GRAY)
        return cv2.resize(gray, (self.flow_width, self.flow_height), interpolation=cv2.INTER_AREA)

    def _moved(self, current: np.ndarray, previous: np.ndarray,
               flow: np.ndarray) -> np.ndarray:
        """True where the vector explains the pixel better than standing still.

        Both residuals are averaged over a window: a single pixel decides
        nothing on flat content, where every vector fits equally well.
        """
        np.add(self._grid_x, flow[..., 0], out=self._map_x)
        np.add(self._grid_y, flow[..., 1], out=self._map_y)
        cv2.remap(previous, self._map_x, self._map_y, cv2.INTER_LINEAR,
                  dst=self._warped, borderMode=cv2.BORDER_REPLICATE)
        win = (self._trust_window, self._trust_window)
        cv2.boxFilter(cv2.absdiff(current, self._warped), cv2.CV_32F, win,
                      dst=self._err_flow)
        cv2.boxFilter(cv2.absdiff(current, previous), cv2.CV_32F, win,
                      dst=self._err_zero)
        raw = self._err_flow < self._err_zero - self._trust_margin
        # A verdict has to win twice before it takes effect. Without this the
        # test is re-decided from scratch every frame, and a cell sitting on
        # the threshold alternates trusted/dropped - the motion field
        # alternates with it, and the guard becomes a source of the shimmer
        # it exists to remove. Measured on this implementation before the
        # memory was added: on a game scene in a window 8.10% of the live
        # cells changed verdict between consecutive frames and 49.2% of
        # those flipped straight back on the next one.
        #
        # The returned array is a reused buffer - read it, do not keep it.
        if (self._trust_last is None
                or self._trust_last.shape != raw.shape):
            self._trust_last = raw.copy()
            self._trust_held = raw.copy()
            return self._trust_held
        np.copyto(self._trust_held, raw, where=(raw == self._trust_last))
        np.copyto(self._trust_last, raw)
        return self._trust_held

    def _report_trust(self, current: np.ndarray, long_enough: np.ndarray,
                      dropped: np.ndarray) -> None:
        """Say how much the guard is throwing away, every few seconds.

        The share on its own does not say whether that is bad: on a desktop
        most of the screen is flat, and a vector dropped on flat content
        carries nothing away. So the texture under the dropped cells is
        reported next to it - local standard deviation over the same window
        the test uses. Low against the kept cells means the guard is
        clearing out exactly the cells where any vector is a guess.
        """
        alive = int(long_enough.sum())
        if alive == 0:
            return
        gone = long_enough & dropped
        self._trust_frames += 1
        self._trust_alive += alive
        self._trust_dropped += int(gone.sum())
        # The texture half is sampled, not counted every frame: measured at
        # 0.474 ms it costs almost three times the guard it is reporting on,
        # and one moving frame in eight is plenty for a number that is read
        # once every half minute.
        if self._trust_frames % 8 == 0:
            kept = long_enough & ~dropped
            f = current.astype(np.float32)
            win = (self._trust_window, self._trust_window)
            mean = cv2.boxFilter(f, cv2.CV_32F, win)
            sd = cv2.sqrt(np.maximum(cv2.boxFilter(f * f, cv2.CV_32F, win)
                                     - mean * mean, 0.0))
            self._trust_tex_gone += float(sd[gone].sum())
            self._trust_tex_kept += float(sd[kept].sum())
            self._trust_tex_gone_n += int(gone.sum())
            self._trust_kept += int(kept.sum())
        now = time.monotonic()
        if self._trust_said == 0.0:
            self._trust_said = now
            return
        # Half a minute, not five seconds: this is a health line, not a
        # trace, and on a moving desktop the shorter period filled the log
        # with twelve lines a minute.
        if now - self._trust_said < 30.0:
            return
        share = 100.0 * self._trust_dropped / max(1, self._trust_alive)
        tex_gone = self._trust_tex_gone / max(1, self._trust_tex_gone_n)
        tex_kept = self._trust_tex_kept / max(1, self._trust_kept)
        print(f"[guides] motion trust: {share:.1f}% of the vectors dropped as "
              f"'did not move' over {self._trust_frames} moving frames; "
              f"texture under them {tex_gone:.1f} against {tex_kept:.1f} "
              f"under the ones kept")
        self._trust_said = now
        self._trust_frames = self._trust_alive = self._trust_dropped = 0
        self._trust_kept = self._trust_tex_gone_n = 0
        self._trust_tex_gone = self._trust_tex_kept = 0.0

    def zero_guide(self) -> GuideFrame:
        """Fallback for a persistent process() failure: zero motion, reset=True.

        The frame keeps going to the worker (the picture does not freeze); NGX
        gets a zero motion field instead of a fresh one.
        """
        motion = self._zero_small if self.emit_small else self._zero_motion
        return GuideFrame(motion=motion, reset=True, scene_score=1.0)

    def forget(self) -> None:
        """Drop the scene history: the next frame starts fresh and resets.

        NR off (bypass) calls it on every frame. Keeping the last pre-bypass
        gray as history would compare against a screen minutes old the moment
        NR comes back on; forgotten, the first NR frame is a reset - which is
        what a resumed pipeline is.
        """
        self.previous_gray = None
        self._cuts.reset()
        self._handed_off = False

    def handoff(self) -> GuideFrame:
        """The frame's guides when the worker decides the scene cut.

        FRAME_FLAG_WORKER_SCENE: the worker scores the gray it has just
        captured - the same rule as process() (native/scene_cut.h, the twin
        of app/scene_cut.py) - and resets on a cut itself. What is left here is zero motion (NVOFA
        makes the field in the worker) and a reset only for what the client
        knows and the worker does not: a first frame, after a restart or a
        stretch of NR off (forget).

        The client's own history is dropped on the way: were the loop to fall
        back to computing guides itself (NVOFA failing mid-session), its first
        frame then compares against nothing and resets, not against a gray
        from before the handoff.
        """
        reset = not self._handed_off
        self._handed_off = True
        self.previous_gray = None
        self._cuts.reset()
        motion = self._zero_small if self.emit_small else self._zero_motion
        return GuideFrame(motion=motion, reset=reset, scene_score=0.0)

    def process(self, rgba: np.ndarray | None = None,
                gray: np.ndarray | None = None, compute_motion: bool = True) -> GuideFrame:
        """Compute the guides: motion/reset/scene_score.

        Either rgba (full-res BGR/RGBA — downsampled here) or a ready gray
        frame (flow-sized, uint8 2D) coming from the worker's reverse channel
        (GRAY/DDA). Gray wins: it is already the right size.
        """
        if gray is not None:
            current = gray.reshape(self.flow_height, self.flow_width).astype(np.uint8)
            if current.shape != (self.flow_height, self.flow_width):
                raise ValueError(
                    f"gray {current.shape} != expected {(self.flow_height, self.flow_width)}")
        else:
            current = self._small_gray(rgba)
        pixels = self.width * self.height
        self._handed_off = False
        cut = self._cuts.step(current)
        if self.previous_gray is None:
            motion = self._zero_small if self.emit_small else self._zero_motion
            reset = True
            scene_score = 1.0
        else:
            scene_score = float(np.mean(cv2.absdiff(current, self.previous_gray))) / 255.0
            # Not "scene_score > 0.24": that fired on every frame of a fast
            # cel-shaded pan and missed dark cuts (app/scene_cut.py).
            reset = cut
            if not compute_motion or reset or scene_score < 0.001:
                # Reset (scene cut) or static screen (desktop/text): no flow needed.
                # 0.001: above capture noise (~0.0002 @ +-2 LSB) and static 0.0,
                # below real motion: 2px scroll 0.03, 2px shift 0.002, fast cursor 0.0013.
                motion = self._zero_small if self.emit_small else self._zero_motion
            else:
                # NGX consumes current-to-previous motion in pixel units.
                # Only the forward flow is computed: the backward one was
                # measured and rejected - see _trust_window above.
                cur_to_prev = self.dis.calc(current, self.previous_gray, None)
                # MV validation, two tests, both on the flow grid (115k
                # elements, not 3M):
                #   1. the noise floor - DIS reports small vectors even on a
                #      static screen (capture noise, cursor jitter), and NGX
                #      would smear text and UI on them;
                #   2. the static hypothesis - a vector of any length is
                #      dropped unless it explains its pixel better than no
                #      motion at all.
                mag = np.hypot(cur_to_prev[..., 0], cur_to_prev[..., 1])
                drop = mag < self._flow_noise_floor
                np.logical_or(drop,
                              ~self._moved(current, self.previous_gray,
                                           cur_to_prev),
                              out=drop)
                self._report_trust(current, mag >= self._flow_noise_floor, drop)
                cur_to_prev[drop] = 0.0
                # Scale BEFORE the upscale: 115k elements instead of 3M, and
                # exactly equivalent because resize is linear (verified: the
                # two orders differ by 0.002, i.e. float16 rounding).
                np.multiply(cur_to_prev[..., 0], self.width / self.flow_width,
                            out=self._flow_scaled[..., 0])
                np.multiply(cur_to_prev[..., 1], self.height / self.flow_height,
                            out=self._flow_scaled[..., 1])
                mag = np.hypot(self._flow_scaled[..., 0], self._flow_scaled[..., 1])
                self._flow_scaled[mag < self._flow_noise_floor] = 0.0
                if self.emit_small:
                    # The worker upscales it on the GPU — all that is left
                    # here is converting 115k values to float16.
                    np.copyto(self._flow_f16, self._flow_scaled, casting="same_kind")
                    motion = self._flow_f16
                else:
                    cv2.resize(self._flow_scaled, (self.width, self.height),
                               dst=self._motion_f32, interpolation=cv2.INTER_LINEAR)
                    np.copyto(self._motion_f16, self._motion_f32, casting="same_kind")
                    motion = self._motion_f16
        self.previous_gray = current
        expected = (self.flow_width * self.flow_height if self.emit_small else pixels)
        assert motion.size == expected * 2
        assert motion.dtype == np.float16 and motion.flags["C_CONTIGUOUS"]
        # INVARIANT: motion is a reused buffer (either _motion_f16 or a
        # cached zero field). The next process() call overwrites it, so the
        # consumer must copy the data before then. The main loop does exactly
        # that: send_frame copies the frame into shared memory immediately.
        return GuideFrame(
            motion=motion,
            reset=reset,
            scene_score=scene_score,
        )
