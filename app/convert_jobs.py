"""The conversion queue: files waiting, one converting, the ones that finished.

media_convert turns ONE file into another; this module is everything a person
needs around that. They add several files at once (from the picker, or by
dropping them on the panel), watch each one's progress, speed and time left,
stop one or all of them, retry a failure, and open the folder a result landed
in. The panel draws the rows, commands.py feeds the queue, and neither of them
touches a thread.

The rules, and why:

* ONE file at a time, on ONE thread. Each conversion starts its own worker,
  and two of them next to the overlay's would split the card three ways for
  no gain: the queue finishes no sooner, every row crawls, and the picture on
  screen stutters. In order, one after another.
* A file is converted with the look it was ADDED with. The settings - the
  sliders, the profile, the output choices - are copied on the main thread
  when the file joins the queue, so a queue of twenty never ends up half in
  one look and half in another because a slider moved in between, and the
  runner thread never reads the live state the main loop is writing.
* The queue answers the panel through snapshots. Progress arrives on the
  runner thread many times a second; the panel reads a copy made under the
  lock, once per frame, and a row can never be half-updated when it is drawn.
* Nothing finished is announced from the runner. Finished jobs wait in a list
  the main loop drains (pop_finished), and the alert is shown there - the
  display is not thread-safe.
"""
from __future__ import annotations

import itertools
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

import media_convert

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
FINISHED = frozenset({DONE, FAILED, CANCELLED})

#: Where the output goes: beside each original, or one chosen folder.
DESTINATIONS = ("source", "folder")

#: How many finished rows the queue keeps before dropping the oldest. The
#: panel is not a history; it is the list of what is happening now.
MAX_FINISHED = 50


@dataclass(frozen=True)
class ConvertSettings:
    """Everything a conversion needs, copied when the file joins the queue."""
    params: dict
    work_scale: float = 0.65
    nr_small: bool = True
    nr_passes: int = 1
    flow_preset: str = "fast"
    #: None: beside the original. A path: that folder.
    out_dir: str | None = None
    codec: str = "auto"
    quality: str = "high"
    image_format: str = "keep"
    copy_audio: bool = True


@dataclass
class ConvertJob:
    """One file in the queue, and everything the panel says about it."""
    id: int
    source: Path
    kind: str
    settings: ConvertSettings
    size_bytes: int = 0
    status: str = QUEUED
    stage: str = ""
    done: int = 0
    total: int = 0
    #: Frames per second through the network, for a video.
    fps: float = 0.0
    #: Seconds left, when the total is known and the rate has settled.
    eta: float | None = None
    output: Path | None = None
    error: str = ""
    seconds: float = 0.0
    codec: str = ""
    audio: str = ""
    #: Frames the network did not process - written as they came in.
    skipped: int = 0
    notes: list = field(default_factory=list)
    started_at: float = 0.0
    #: When the first frame went through - the rate is measured from here,
    #: not from the start, or the worker's warm-up would read as a slow file.
    first_frame_at: float = 0.0
    frames_at_first: int = 0

    def row(self) -> dict:
        """The plain values the panel draws a row from."""
        fraction = 0.0
        if self.status == DONE:
            fraction = 1.0
        elif self.total > 0:
            fraction = min(1.0, self.done / self.total)
        return {
            "id": self.id,
            "name": self.source.name,
            "kind": self.kind,
            "status": self.status,
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "fraction": fraction,
            "fps": self.fps,
            "eta": self.eta,
            "size": self.size_bytes,
            "output": str(self.output) if self.output else "",
            "error": self.error,
            "seconds": self.seconds,
            "codec": self.codec,
            "audio": self.audio,
            "skipped": self.skipped,
            "notes": list(self.notes),
        }


def friendly_error(exc: BaseException) -> str:
    """One short line for the row; the whole story goes to the log.

    The panel has one line under a file name, and "encode: [Errno 13]
    Permission denied: 'D:\\...\\clip-nr.mp4.partial'" does not fit it - nor
    does it say what to do. The stage decides the sentence.

    The stage alone is not enough for "process": a frame that fails while the
    worker is running and a write the muxer refuses are the same stage, and
    they need different sentences - the first sends the user to restart the
    worker, the second would have them chase a problem that is in the file.
    The cause is asked: libav errors (this project's bundled ffmpeg) are the
    file's, and anything else at that stage is the worker's.
    """
    stage = getattr(exc, "stage", "")
    cause = getattr(exc, "cause", exc)
    if isinstance(cause, PermissionError) or "Permission denied" in str(cause):
        return "denied"
    if stage == "hdr":
        return "hdr"
    if stage == "too_large":
        return "too_large"
    if stage == "decode":
        return "unreadable"
    if stage in ("process", "worker"):
        module = type(cause).__module__ or ""
        if module.split(".")[0] == "av":       # av.error.* - libav refused the write
            return "encode"
        return "worker"
    if stage == "encode":
        return "encode"
    return "unknown"


def format_eta(seconds: float | None) -> str:
    """0:48, 12:05, 1:02:09 - the clock the rows print."""
    if seconds is None or seconds != seconds or seconds < 0:
        return ""
    whole = int(round(seconds))
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_size(size: int) -> str:
    """1.4 MB, 820 KB - enough to tell a clip from a film at a glance."""
    size = max(0, int(size or 0))
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.1f} GB"
    if size >= 1024 ** 2:
        return f"{size / 1024 ** 2:.1f} MB"
    return f"{max(1, size // 1024)} KB"


#: What a row calls the encoder that produced the file. The CPU one says so:
#: it is ten times slower, and a user wondering why a file took so long
#: deserves the reason on the row rather than in the log.
CODEC_LABELS = {"av1_nvenc": "AV1", "hevc_nvenc": "HEVC",
                "h264_nvenc": "H.264", "libx264": "H.264 (CPU)"}


def codec_label(codec: str) -> str:
    codec = str(codec or "")
    return CODEC_LABELS.get(codec, codec.upper())


def status_line(row: dict, s: dict) -> tuple[str, str]:
    """What the second line of a row says, and in which tone.

    Returned as (text, tone) with tone one of "muted", "text", "ok",
    "danger" - the panel owns the colours. `s` is the language's strings.
    A finished file is "ok" (the panel's green), not the accent: in the
    light theme the accent and the danger red are the same clay within a
    few shades, and "done" and "failed" rows read alike.
    Everything shown comes from `s`: the converter's own notes are English
    sentences for the log, and a row carries what they mean instead.
    """
    status = row.get("status")
    kind = row.get("kind")
    if status == QUEUED:
        kind_text = s.get(f"convert_kind_{kind}", str(kind or ""))
        return (f"{s.get('convert_queued', 'waiting')} · {kind_text} · "
                f"{format_size(row.get('size', 0))}", "muted")
    if status == RUNNING:
        stage = row.get("stage") or ""
        if stage in ("", "decoding", "starting"):
            return (s.get("convert_starting", "starting the network..."),
                    "text")
        if stage == "writing":
            return s.get("convert_writing", "writing the file..."), "text"
        if kind != "video":
            return s.get("convert_processing", "processing..."), "text"
        parts = []
        if row.get("total", 0) > 0:
            parts.append(f"{int(row.get('fraction', 0.0) * 100)}%")
        else:
            parts.append(s.get("convert_frames", "{n} frames").format(
                n=int(row.get("done", 0))))
        fps = float(row.get("fps") or 0.0)
        if fps > 0:
            parts.append(f"{fps:.1f} fps")
        eta = format_eta(row.get("eta"))
        if eta:
            parts.append(s.get("convert_left", "{time} left").format(time=eta))
        return " · ".join(parts), "text"
    if status == DONE:
        parts = [s.get("convert_done_row", "done in {time}").format(
            time=format_eta(row.get("seconds", 0.0)) or "0:00")]
        label = codec_label(row.get("codec", ""))
        if label:
            parts.append(label)
        audio = row.get("audio")
        if audio == "aac":
            parts.append(s.get("convert_audio_aac", "audio as AAC"))
        elif audio == "dropped":
            parts.append(s.get("convert_audio_dropped", "no audio"))
        skipped = int(row.get("skipped") or 0)
        if skipped:
            parts.append(s.get("convert_unprocessed",
                               "{n} frames not processed").format(n=skipped))
        return " · ".join(parts), "ok"
    if status == FAILED:
        reason = s.get(f"convert_err_{row.get('error') or 'unknown'}",
                       s.get("convert_err_unknown", "failed"))
        return reason, "danger"
    if status == CANCELLED:
        return s.get("convert_cancelled", "stopped"), "muted"
    return "", "muted"


def row_action(row: dict) -> str:
    """The one action a row offers, by its state: stop / remove / show / retry."""
    status = row.get("status")
    if status == RUNNING:
        return "stop"
    if status == QUEUED:
        return "remove"
    if status == DONE:
        return "show"
    return "retry"


def reveal(path: Path) -> bool:
    """Open Explorer with the file selected. False if there is nothing to show."""
    path = Path(path)
    try:
        if path.is_file():
            # One string, not a list: Explorer parses its own command line,
            # and the form it documents is /select,"<path>" - a list would be
            # quoted whole by Python for a path with a space in it.
            subprocess.Popen(f'explorer /select,"{path}"')
            return True
        if path.parent.is_dir():
            os.startfile(str(path.parent))            # noqa: S606 - a folder
            return True
    except OSError as exc:
        print(f"[convert] could not open Explorer: {exc}", file=sys.stderr)
    return False


class ConvertQueue:
    """The queue. Every method is safe from the main thread; see the module
    docstring for what runs where."""

    def __init__(self, convert: Callable | None = None):
        self._lock = threading.Lock()
        self._jobs: list[ConvertJob] = []
        self._ids = itertools.count(1)
        self._runner: threading.Thread | None = None
        self._cancel: threading.Event | None = None
        self._finished: list[ConvertJob] = []
        self._closing = False
        # Injectable so the queue's own rules can be tested without a GPU.
        self._convert = convert or media_convert.convert

    # -- what the main thread asks ---------------------------------------

    def add(self, paths, settings: ConvertSettings
            ) -> tuple[list[int], list[tuple[str, str]]]:
        """Queue files. Returns (ids added, [(name, reason)] refused).

        Refused: a path that is not a file ("missing"), a format the
        converter will not open ("format"), a file already waiting or
        converting ("queued"). A folder dropped on the panel adds the media
        directly inside it - one level, the way a person means it.
        """
        added: list[int] = []
        refused: list[tuple[str, str]] = []
        expanded: list[Path] = []
        for raw in paths or []:
            path = Path(str(raw))
            if path.is_dir():
                expanded.extend(sorted(
                    (p for p in path.iterdir()
                     if p.is_file() and media_convert.classify(p)),
                    key=lambda p: p.name.casefold()))
            else:
                expanded.append(path)
        with self._lock:
            if self._closing:
                return added, refused
            active = {os.path.normcase(str(j.source.resolve()))
                      for j in self._jobs if j.status in (QUEUED, RUNNING)}
            for path in expanded:
                if not path.is_file():
                    refused.append((path.name, "missing"))
                    continue
                kind = media_convert.classify(path)
                if not kind:
                    refused.append((path.name, "format"))
                    continue
                key = os.path.normcase(str(path.resolve()))
                if key in active:
                    refused.append((path.name, "queued"))
                    continue
                active.add(key)
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                job = ConvertJob(id=next(self._ids), source=path, kind=kind,
                                 settings=settings, size_bytes=size)
                self._jobs.append(job)
                added.append(job.id)
            self._trim_finished()
        self._start_runner()
        return added, refused

    def act(self, job_id: int, action: str, settings: ConvertSettings | None = None
            ) -> bool:
        """A row's button: stop / remove / show / retry. True if it did something.

        Retry takes the settings of NOW when they are given: a file that
        failed is usually retried after changing what made it fail (the
        folder, the codec), and repeating the old choice would fail again.
        """
        with self._lock:
            job = next((j for j in self._jobs if j.id == job_id), None)
            if job is None:
                return False
            if action == "stop" and job.status == RUNNING:
                if self._cancel is not None:
                    self._cancel.set()
                return True
            if action == "remove" and job.status != RUNNING:
                self._jobs.remove(job)
                return True
            if action == "retry" and job.status in (FAILED, CANCELLED):
                self._jobs.remove(job)
                fresh = ConvertJob(id=next(self._ids), source=job.source,
                                   kind=job.kind,
                                   settings=settings or job.settings,
                                   size_bytes=job.size_bytes)
                self._jobs.append(fresh)
            elif action == "show" and job.status == DONE and job.output:
                output = job.output
            else:
                return False
        if action == "retry":
            self._start_runner()
            return True
        return reveal(output)

    def stop_all(self) -> int:
        """Stop the running file and everything waiting. Returns how many."""
        stopped = 0
        with self._lock:
            for job in self._jobs:
                if job.status == QUEUED:
                    job.status = CANCELLED
                    stopped += 1
                elif job.status == RUNNING:
                    stopped += 1
            if self._cancel is not None:
                self._cancel.set()
        return stopped

    def clear_finished(self) -> int:
        """Drop every finished row. Returns how many went."""
        with self._lock:
            before = len(self._jobs)
            self._jobs = [j for j in self._jobs if j.status not in FINISHED]
            return before - len(self._jobs)

    def rows(self) -> list[dict]:
        """A copy of every row, for the panel."""
        with self._lock:
            return [j.row() for j in self._jobs]

    def summary(self) -> dict:
        """The one-line view: how far the whole queue is, for the main page."""
        with self._lock:
            active = [j for j in self._jobs if j.status in (QUEUED, RUNNING)]
            running = next((j for j in self._jobs if j.status == RUNNING), None)
            finished = sum(1 for j in self._jobs if j.status in FINISHED)
            return {
                "busy": running is not None or bool(active),
                "waiting": sum(1 for j in active if j.status == QUEUED),
                "running": running.source.name if running else "",
                "fraction": running.row()["fraction"] if running else 0.0,
                "finished": finished,
                "total": len(self._jobs),
            }

    def pop_finished(self) -> list[ConvertJob]:
        """Jobs that finished since the last call, oldest first."""
        with self._lock:
            out, self._finished = self._finished, []
            return out

    @property
    def busy(self) -> bool:
        with self._lock:
            return any(j.status in (QUEUED, RUNNING) for j in self._jobs)

    def shutdown(self, timeout: float = 10.0) -> None:
        """Stop everything and wait for the runner, bounded. For program exit.

        The runner's worker is reaped by media_convert on the way out; and if
        this process dies before that, the job object in pipeline takes the
        worker with it.
        """
        with self._lock:
            self._closing = True
        self.stop_all()
        runner = self._runner
        if runner is not None:
            runner.join(timeout)

    # -- the runner thread -----------------------------------------------

    def _trim_finished(self) -> None:
        finished = [j for j in self._jobs if j.status in FINISHED]
        for job in finished[:max(0, len(finished) - MAX_FINISHED)]:
            self._jobs.remove(job)

    def _start_runner(self) -> None:
        with self._lock:
            if self._runner is not None or self._closing:
                return
            if not any(j.status == QUEUED for j in self._jobs):
                return
            self._runner = threading.Thread(target=self._run,
                                            name="convert-queue", daemon=True)
            runner = self._runner
        runner.start()

    def _next(self) -> tuple[ConvertJob | None, threading.Event | None]:
        with self._lock:
            job = None if self._closing else next(
                (j for j in self._jobs if j.status == QUEUED), None)
            if job is None:
                self._runner = None
                self._cancel = None
                return None, None
            job.status = RUNNING
            job.stage = "decoding"
            job.started_at = time.monotonic()
            self._cancel = threading.Event()
            return job, self._cancel

    def _progress(self, job: ConvertJob, progress) -> None:
        now = time.monotonic()
        with self._lock:
            job.stage = progress.stage
            job.done = int(progress.done)
            job.total = int(progress.total)
            if progress.stage != "processing" or job.kind != "video":
                return
            if not job.first_frame_at:
                job.first_frame_at = now
                job.frames_at_first = job.done
                return
            elapsed = now - job.first_frame_at
            frames = job.done - job.frames_at_first
            if elapsed >= 0.5 and frames > 0:
                job.fps = frames / elapsed
                if job.total > job.done:
                    job.eta = (job.total - job.done) / job.fps
                else:
                    job.eta = None

    def _run(self) -> None:
        while True:
            job, cancel = self._next()
            if job is None:
                return
            settings = job.settings
            print(f"[convert] {job.source.name}: started "
                  f"({job.kind}, codec {settings.codec}, quality "
                  f"{settings.quality}, "
                  f"{'beside the source' if not settings.out_dir else settings.out_dir})")
            status, error, result = FAILED, "", None
            try:
                result = self._convert(
                    job.source, None, dict(settings.params),
                    work_scale=settings.work_scale,
                    nr_small=settings.nr_small,
                    nr_passes=settings.nr_passes,
                    flow_preset=settings.flow_preset,
                    out_dir=settings.out_dir,
                    image_format=settings.image_format,
                    codec=settings.codec,
                    quality=settings.quality,
                    copy_audio=settings.copy_audio,
                    progress=lambda p, job=job: self._progress(job, p),
                    cancel=cancel)
                status = DONE
            except media_convert.ConversionCancelled:
                status = CANCELLED
            except Exception as exc:                  # noqa: BLE001
                error = friendly_error(exc)
                print(f"[convert] {job.source.name}: failed - "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
            with self._lock:
                job.status = status
                job.error = error
                job.seconds = time.monotonic() - job.started_at
                job.eta = None
                if result is not None:
                    job.output = Path(result.output)
                    job.codec = str(result.codec or "")
                    job.audio = str(result.audio or "")
                    job.notes = list(result.notes or [])
                    job.skipped = int(getattr(result, "skipped", 0) or 0)
                    job.done = max(job.done, int(result.frames or 0))
                    if job.kind == "video" and job.seconds > 0 and result.frames:
                        job.fps = result.frames / job.seconds
                self._finished.append(replace(job, notes=list(job.notes)))
                self._trim_finished()
            if status == DONE:
                print(f"[convert] {job.source.name}: done -> {job.output} "
                      f"({job.done} frame(s), {job.seconds:.1f}s, {job.codec}, "
                      f"audio {job.audio})"
                      + (f"; {'; '.join(job.notes)}" if job.notes else ""))
            elif status == CANCELLED:
                print(f"[convert] {job.source.name}: stopped")
