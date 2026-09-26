# NeuralScreen internals

How it works, what was measured, and why the decisions went the way
they did. For installing and using the program see
[README.md](README.md).

Every number here was measured on this machine - RTX 5070 Ti, driver
616.56, Windows 11 - and says so where it matters. Where an earlier
conclusion turned out to be wrong, the correction is kept rather than
quietly edited out: the mistakes are the useful part.

## One window: Windows Graphics Capture (WGCW)

`Num5` swaps the input from Desktop Duplication of the whole screen to
Windows Graphics Capture of one window. Both sources hand the worker the same
kind of `ID3D11Texture2D`, so everything downstream - the NT-shared texture,
the fence, the BGRA->RGBA swizzle, the luminance channel for the guides - is
the same code.

Why it matters, measured rather than assumed:

* A per-window capture is unaffected by what is drawn on top of the window. A
  fullscreen overlay covering the target contributes **0.0%** of the captured
  pixels while the window's own content is **100%**. No self-capture loop.
* So `WDA_EXCLUDEFROMCAPTURE` comes off in this mode, and that is the whole
  point: with it on, the NVIDIA App writes **no file at all** (0 out of 4
  attempts); in one-window mode it recorded 26.1 MB of the same desktop.
* The yellow "this window is being captured" border can be turned off.
  `GraphicsCaptureSession.IsBorderRequired = false` takes effect for an
  unpackaged process on Windows 11 26200 even though
  `GraphicsCaptureAccess.RequestAccessAsync(Borderless)` answers
  `UserPromptRequired`. Measured against a control run: 12 yellow pixels in
  the ring around the window with the capture on, 12 with it off.

Sizes are **physical pixels**. `WGCW` is acknowledged with the size the
capture really produces, and the pipeline is rebuilt for exactly that - a
window's logical size on a scaled display is a different number, and
`GetWindowRect` is a third one (it includes the invisible resize border). The
overlay is placed by `DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)`,
which is the rectangle the capture agrees with.

The capture is event-driven: a window that does not redraw produces one frame
and then nothing, exactly like `DXGI_ERROR_WAIT_TIMEOUT` on the duplication
path. The worker keeps the last frame.

A resize reconfigures the running worker over `RNSZ` - new capture size, new
shared memory, new textures, same process - and still waits half a second for
the size to settle first. It used to replace the worker, which cost 1.845 s and
a veil over the picture; in place it is 0.112 s and nothing goes dark. A move
only moves the windows.

## Recording (Num0)

`Num0` starts/stops recording of the **NR-processed frame** into
`recordings/neuralscreen-<timestamp>.mp4`. Two encoders can make it, and
**Record on the GPU** in the Media tab chooses between them (on by default).
Whichever records, stopping never blocks the UI and the file is published only
once it reads back (see *For both paths*).

### On the GPU (the default)

The worker records the frame the viewer sees without the frame ever leaving
the card (`native/gpu_recorder.cpp`):

- **One copy a frame, no submission of its own.** Wherever the frame is copied
  for Spout - the SDR, bypass, Frame Generation and HDR presents, and the pixel
  download - it is copied into a three-slot ring too, in the same command list.
  `EndCommands` hands that list's fence to the encoder thread.
- **The encoder thread** waits for the fence, converts RGBA to NV12 with the
  D3D11 video processor on the same adapter (matched by LUID, as the Spout
  bridge does) and hands the surface to a Media Foundation sink writer running
  NVENC: AV1, then HEVC, then H.264 - the first one the driver opens. An SDR
  file is tagged BT.709 studio range, which is what players assume for HD video.
- **HDR10 when HDR is on.** An HDR session is recorded as the display gets it:
  10-bit, BT.2020 primaries and matrix, the PQ curve, tagged so that a player
  tone-maps it. The worker hands the recorder its HDR composite as 10-bit PQ -
  with Frame Generation the composite already is that; without it, the
  composite runs a second time with the PQ encode on. The D3D11 video processor
  converts within BT.709 only on this driver (no BT.2020, no PQ, no FP16 input),
  so two pixel-shader draws write the P010 planes instead: 10-bit luma, then
  chroma at half size, left-sited. AV1 or HEVC Main10 encodes it (NVENC has no
  10-bit H.264); when neither opens, the recording is SDR. The mastering
  metadata gives a nominal 1000 nits. `NS_GREC_HDR=0` keeps recordings SDR.
- **Fragmented MP4** where the system offers it (for AV1 and H.264 here, not
  HEVC): a worker that dies mid-recording still leaves a file that plays up to
  its last fragment. "Auto" takes any codec in a fragmented file before the
  best codec in a plain one.
- **60 fps on the clock.** A frame goes into each 16.7 ms slot the pipeline
  fills; a still screen fills none, and the file is variable-rate rather than
  padded. On stop, the frame on screen is written once more, so the picture
  lasts as long as the sound.
- **A frame of another size** (a captured window resized mid-recording) is
  scaled into the recording and letterboxed instead of ending it.
- **Sound** comes from the client: the WASAPI loopback described below,
  resampled to 48 kHz 16-bit stereo and written into a shared PCM ring
  (`AUDIO_RING_FMT`). The encoder thread muxes it into the same file as AAC,
  timed by its position in the ring - so there is no remux at the end. The
  file's time 0 is the moment the encoder is ready: a slow setup delays the
  whole recording instead of opening it with a gap.
- **Failures end cleanly.** A write error (a full disk) ends the recording in
  the worker, which says so with an unasked `REAK`; a worker that dies is
  noticed from the client's side. Either way the client publishes what reached
  the disk and says the recording was **cut short**. If the GPU recorder cannot
  start at all, the CPU path records instead.
- **The open menu is not in the file:** it is a separate window the worker
  never sees. Switch Record on the GPU off to have it drawn in.

Measured on an RTX 5080 (`tests/test_gpu_recorder.py`, which also runs the
recorder on its own through `native/gpu_recorder_check.cpp`): all three codecs
120/120 frames at 60 fps with none dropped; four colour bars within one code
value of BT.709 studio range; frame spacing 16.67 ms throughout; picture and
sound 0.0 ms apart (white flashes against tone bursts at the same instants).
HDR10: 10-bit PQ bars decode to their exact BT.2020 code values in AV1 and in
HEVC Main10, through a resize too; through a worker
(`tests/test_hdr_recording.py`), the file carries the desktop's own levels -
white at the PQ code of the SDR white - with Frame Generation off and on.
In a headless loop that pays the readback either way, GPU recording at 60 fps
cost no measurable frame rate and the CPU path at 30 fps cost 6%.

### On the CPU (Record on the GPU off, and the fallback)

The client asks the worker for the pixels of each recorded frame
(`FRAME_FLAG_WANT_PIXELS`), draws the open menu onto them and encodes them
itself:

- AV1 NVENC hardware encoding through PyAV (HEVC or H.264 on cards without an
  AV1 encoder) at your desktop resolution, **30 fps**,
  quality-targeted VBR (`cq 16`, ~64 Mbps in practice, ceiling 250 Mbps),
  preset p6 + tune hq, sRGB/BT.709 color tags (metadata written both on the
  stream and on every frame — players render colors identical to the screen).
- The bitrate is a **ceiling, not a target**: on fast motion the encoder is
  allowed to spend more instead of dropping quality to hit a fixed number.
  Raising quality costs no encoding time — that is dominated by the colour
  conversion, not by the preset (measured: 1.6–1.7 s per 3 s of video at
  every setting tried).
- **30 fps, not 60:** every recorded frame is a full trip of the picture from
  the worker, and at 60 fps that measured ~36% of the frame rate (101 -> 65
  FPS). Half the rate is half the cost, and each frame is the same picture. The
  timestamps come from the clock, so the file still plays at real speed.
- **Only the open menu is burned into the recording** — nothing else. Our
  own layer is hidden from external capture, so anything that must reach the
  file is drawn onto the frame before encoding. The same applies to
  screenshots. The HUD panel and the watermark used to be burned in as well;
  they are gone from the screen, and in a file they read as someone else's
  caption.

### For both paths

- Recording works in both NR ON and NR OFF (bypass) modes; the file duration
  matches real time (PTS is built from the wall clock).
- **External recorders see the picture through Spout2** (off by default,
  toggled in the settings): the worker publishes its output as a Spout2
  shared texture, so OBS with the Spout2 Capture plugin records the
  processed picture in full-screen mode too — where `WDA_EXCLUDEFROMCAPTURE`
  hides the overlay from a screen capture. The NVIDIA App has no Spout
  input; its path is one-window mode, which drops the WDA flag. The bridge
  is initialised once per worker process (`NS_SPOUT`), so toggling it
  restarts the worker.
- **System audio is recorded as a second track**: WASAPI loopback ("what you
  hear") from the default playback device, resampled to AAC 48 kHz / 192
  kbit/s stereo. No virtual cable or microphone. Turn it off with
  `"record_audio": false` in `config.json`. A missing endpoint or AAC encoder
  falls back to video-only instead of aborting the MP4.

  While nothing is playing at all, WASAPI loopback hands back no data rather
  than silence, so quiet stretches are padded from the same clock the video
  uses. Without that the audio track would simply be shorter than the video
  and everything after a pause would be out of sync.

Stopping is a state transition, not a blocking `join()` on the UI thread.
The encoder drains every accepted frame into `<name>.mp4.partial`, flushes and
closes both streams, reopens the container and decodes a video frame, then
publishes the final path with `os.replace`. Until that verification succeeds,
the UI says *finalizing* and never reports the recording as saved. A failure
keeps the recoverable `.partial` and reports the exact lifecycle stage.

### What the CPU path costs, and why it is not the bitrate

These are the costs the GPU path does away with; they still apply when Record
on the GPU is off. Recording used to halve the frame rate. Measured at 4K with
`NS_PHASE=1`, per frame:

```
                    idle    recording   after both fixes
frame rate          55.7      20.6           30.1 FPS
recv                17.5      31.6           24.5 ms
encode (Python)      0.4      19.9            3.1 ms
worker frame        17.4      24.1           21.3 ms
```

Two costs, neither of them the bitrate:

- **19.9 ms of RGBA→yuv420p on the CPU** plus the nvenc submit, inside the
  capture loop. Encoding now runs in its own thread behind a 4-slot queue;
  the loop only computes the PTS and hands the frame over. On a full queue
  the frame is dropped rather than stalling the loop — the user is looking at
  the screen, not at the file, and a gap does not shift timing because the
  PTS comes from the clock.
- **~7 ms of pushing 33 MB down the pipe.** The `OUTS` channel hands the
  worker a named section to write pixels into instead. The copy out of the
  section happens on the reader thread; a copy is unavoidable because the
  section has one slot and the worker overwrites it next frame, while a
  recorded frame outlives that.
- **~11 ms of ALLOCATING the 33 MB to copy into.** This is the part the
  `OUTS` channel did not fix and the table above still shows: transport was
  removed, allocation was not. `read_out()` called `.copy()`, so every
  recorded frame mapped a fresh 33 MB array and paid ~8000 first-touch page
  faults on it - measured at 4K with four frames held alive (the encoder's
  queue depth), **13.2 ms per frame at 2.5 GB/s against 2.4 ms at 13.7 GB/s**
  into a pre-allocated buffer. The memcpy was never the cost.

  It now copies into a slot of a reused ring (`OUT_RING_SLOTS`). The reason
  that is safe, and the reason the ring is scanned rather than simply
  advanced: a returned frame travels by REFERENCE into the encoder queue and
  lives until the encoder has written it, so a slot is only handed out when
  its refcount shows nobody else holds it. When every slot is in flight the
  answer is a fresh array - slower for that frame, and always correct.

Lowering the bitrate does nothing for any of this: the time goes into the
colour conversion, not into the encoder. Which is why there is no bitrate
slider in the menu — it would be a knob that looks like it helps and does
not.

## Converting files (the Media tab)

The Media tab's conversion page runs images and videos from disk through the
same network, with the profile and sliders on screen. Each file runs on its
own worker in the shape the compatibility gate uses - synthetic frames, no
capture, no window - so the overlay keeps running beside it (`media_convert.py`,
the queue in `convert_jobs.py`):

- **Stills** are one frame with `reset` and zero motion: a still has no history
  to correlate against.
- **Videos** run the live optical-flow guides at the live work size and are
  re-encoded through NVENC (AV1, HEVC or H.264; H.264 on the CPU for sizes NVENC
  refuses). The original audio is copied, or re-encoded to AAC when the
  container needs it.
- **The queue** takes several files at once (the picker, or drag and drop onto
  the panel), converts them in order and shows progress, speed and time left
  for each, with Stop, Remove, Retry and Show. The output choices - where to
  save, codec, quality, image format, keep audio - apply to files added after
  the change.
- Output goes to a `.partial` name first and is published when complete, as a
  recording is; the format is named explicitly because neither Pillow nor PyAV
  can guess it from that suffix.
- **A converted file is seen the way its source is.** A still is turned by its
  EXIF orientation before it goes in - a phone stores a portrait photo as
  landscape pixels and a "turn me" tag - and keeps its colour profile and EXIF;
  a 16-bit grey image is scaled to 8 bits, where Pillow's own conversion clips
  it to white. A video keeps its display matrix: the frames go through as they
  are stored, and the file says how to turn them, as the source did. The
  worker takes frames up to 7680x4320, a landscape shape, so a portrait still
  taller than 4320 goes through on its side and comes back upright; a file too
  large both ways is refused with a sentence that says so.
- **A frame does not travel through a pipe, and the stages overlap.** The
  conversion worker takes the same three channels the live pipeline does: the
  frame through shared memory (`SHMI`), the result back through a second
  section (`OUTS`), and the motion field at flow size for the GPU to upscale
  (`MOTS`) - so a converted frame takes the same vectors into the network as
  the desktop does, where it used to build them on the CPU. Decode with the
  guides, the network and the encoder then run on three threads, handing
  frames over in the source's own order (`NS_CONVERT_PIPELINE=0` puts them
  back on one thread). Measured on an RTX 5080 over 150 frames with Boost at
  0.65, one pass, AV1: **22.35 -> 8.08 ms a frame at 720p** and **36.47 ->
  12.15 ms at 1080p**; the whole call, which carries the worker's ~1.2 s
  start either way, 5.11 -> 2.98 s and 7.40 -> 3.79 s.

## config.json

`config.default.json` is the tracked product default and declares
`schema_version`; the local `config.json` is generated/migrated per user and is
ignored by Git. Loading merges validated user values over a fresh default, so
a maintainer's GPU, paths or experimental switches cannot leak into a release.

| Field | Meaning |
|---|---|
| `schema_version` | config contract version; older supported versions are migrated before validation |
| `monitor` | stable display name saved from the menu; its flat UI index resolves to the owning adapter/output pair |
| `width`, `height` | output resolution (**actual monitor resolution is used automatically when config is stale**) |
| `fullscreen` | borderless fullscreen window |
| `warmup` | NGX warmup frames at start |
| `work_scale` | 0.1–1.0, the resolution the network runs at, relative to the screen. Only has an effect with `nr_small` on |
| `nr_small` | process at a reduced resolution and compose the result onto the native frame: faster, sharp (the residual composite). Default `true` - **Boost is on** |
| `profile` | `Faithful`, `Natural`, `Strong / Cinematic`, `Extreme / Overdrive` |
| `intensity`, `local_tone`, `local_structure`, `skin_structure` | `null` = take from profile |
| `lang` | 12 languages: `en` `ru` `fr` `de` `es` `it` `pt` `pl` `uk` `zh` `ja` `ko` |
| `worker_present` | worker shows the frame in its own window (`false` — pygame output) |
| `motion_on_gpu` | worker upscales the motion field (`false` — CPU) |
| `motion_backend` | which estimator builds the motion field: `nvofa` (driver optical flow, the shipped default) or `cpu` (DIS). NVOFA falls back to CPU DIS by itself when the driver refuses |
| `capture_in_worker` | worker captures the desktop itself (DDA, `false` — dxcam in Python) |
| `pixels_in_shm` | result pixels come back through a shared section instead of the pipe (`false` — pipe, as before) |
| `flow_preset` | which DIS configuration estimates the motion field: `fast` (default, as shipped), `ultrafast`, `medium`. See "What the guides cost" |
| `frame_limit_mode`, `frame_limit_custom` | active-pipeline cap: 30, 60, custom 15–240, or unlimited |
| `frame_generation`, `frame_multiplier` | opt-in FG and ×2/×3/×4 requested multiplier; refusal turns it back off |
| `recording_dir`, `screenshot_dir`, `screenshot_mode`, `screenshot_format` | persistent media destinations; screenshots use Save As or a quiet unique name, PNG or JPEG |
| `spout`, `hdr` | explicit opt-in output/capture optimisations; both default off |
| `split` | 0–1, share of the frame left unprocessed for the before/after wipe; 0 — off |
| `theme` | `light` / `dark` |
| `open_menu_on_start` | open the menu on launch; `false` — a short alert instead |
| `hotkeys` | `{"toggle": "Num1", ...}` — see README, "Using it". Names: `Num0`-`Num9`, `Numdot`, `Numplus`, `Numminus`, `Nummul`, `Numdiv`, `F1`-`F12`, `Insert`, `Home`, letters, digits, with `Ctrl+`/`Alt+`/`Shift+` |
| `nr_passes` | 1-4, how many passes the network makes over one frame (Boost only). An experiment; the second pass costs about a third of the frame rate and every extra pass carries its own network - about 640 MB at a 2560x1440 work size, and it scales with that size |
| `fps_overlay` | `off` / `tl` / `tr` / `bl` / `br` - the on-screen frame counter and the corner it sits in |
| `tray_on_minimise`, `tray_on_close` | what the taskbar button's minimise and close do; both off by default, and neither stops the neural pass |
| `menu_scale_auto` | the panel size is still the automatic fit; cleared for good the moment a scale step is chosen by hand |
| `menu_offset`, `menu_scale`, `menu_height` | where the menu sits, its scale and height. Written by the app, not meant to be edited by hand (`menu_height: null` — fit the content) |

## Architecture

Two processes. Python drives settings, optical-flow guides and the menu
layer; the C++ worker owns the D3D12 device, the capture, NGX and the
overlay window.
They talk over stdin/stdout with a binary protocol:

| Message | Purpose |
|---|---|
| `D5V3` | stream header: sizes, profile, NR parameters |
| `CACK` | explicit result/category of the initial `CreateFeature`; SAFE PASSTHROUGH pixels are not evidence of support |
| `SHMI` / `SACK` | shared-memory section name for the input frame |
| `WNDO` / `WACK` | raise/close the worker's output window |
| `MOTS` / `MACK` | motion arrives at reduced size, worker upscales it on GPU |
| `DDA1` / `DACK` | worker takes over capture (Desktop Duplication), colour never touches the CPU |
| `GRAY` / `GAK` | worker writes AREA-downsampled luminance (320×180) into a back-mapping for the guides |
| `FRM1` | frame: header, then either payload (RGBA8 + motion) or "in shared memory" flag |
| `OUT1` | result: RGBA8 full-res, or `bytes=0` — the worker already presented it |
| `RNSZ` / `RACK` | change work resolution on the fly, no process restart |
| `OUTS` / `OAK2` | named section the worker writes result pixels into; the reply then carries `bytes = 0xFFFFFFFF` instead of a payload |
| `RECS` / `RSAK` | record the frame the viewer sees into an MP4, on the GPU; the answer names the codec, the size and the file's time 0 |
| `RECE` / `REAK` | stop and close the recording; `REAK` also comes unasked when the encoder fails |

**Capture.** On `DDA1` the worker opens Desktop Duplication on the GPU: each
frame is copied into a cross-device shared texture and swizzled to RGBA.
Python stops capturing entirely — `grab` and `guides` drop to 0.1 ms.

With HDR compatibility off, DDA asks for a single format through
`DuplicateOutput1` so it cannot flap. The v1.12 attempt to request BGRA8
alone proved insufficient on the drivers reported in #86 and #89: acquired
frames still alternated between FP16 and BGRA8 and rebuilt the bridge
repeatedly - 1,955 rebuilds in 133 seconds. v1.13 moved SDR onto the
original `IDXGIOutput1::DuplicateOutput`, whose desktop image is converted
to BGRA8, and another reporter's log showed that call succeeding with the
same alternation. v1.14 therefore pins the format instead of offering a
choice: a display that can produce a high-colour surface - HDR-capable, or
a scan-out deeper than 8 bits per colour - is asked for FP16 through
`DuplicateOutput1`, and the plain `DuplicateOutput` path remains for an
ordinary 8-bit display, where it works and its behaviour is preserved
exactly.

Two facts about a captured frame are easy to conflate and must not be: the
format it ARRIVED in, and the encoding it CARRIES. A display with a
high-colour surface - HDR-capable, or a 10-bit scan-out - has its format
pinned to FP16 so it cannot flap (that is the #86 fix), but with HDR off
that FP16 frame holds an ordinary SDR desktop whose 1.0 is white. The
capture shader therefore takes `isFloat` and `hdr` as separate constants:
only a real scRGB frame is tone-mapped, FP16-without-HDR gets the display
encode alone. Running the HDR tone map on an SDR frame put white at
`ToSrgb(1/(1+1)) = 0.735` - 187 instead of 255, the washed-out picture
reported as #99. `tests/hdr_gpu.cpp` runs that case on WARP.

That `guides` figure is a STATIC screen, and it is worth saying so: with
nothing moving, `process()` sees a scene score under 0.001 and returns a
cached zero field without ever calling DIS (measured 0.03 ms). On moving
content — a game, a video, a page being scrolled, i.e. the whole point of
the program — the DIS call runs and the stage costs **3.9 ms**, of which
2.9 ms is `dis.calc` itself. It is the one per-frame cost left in Python
once the capture, the motion upscale and the presentation are all on the
GPU, and it is serial ahead of `send`, so it is frame time rather than
background work.

Two things follow from that, both done:

* **In bypass (NR OFF) it is not computed at all while FG is off too.** The
  worker skips the NGX evaluate, so nothing ever reads the field; filling it
  cost ~2.9 ms on the mode that runs fastest (121-133 FPS), about half a core
  spent on a buffer that gets thrown away. `previous_gray` is cleared along
  with it, so the first frame after NR comes back reports a scene cut instead
  of correlating against a screen that may be minutes old.
* **With FG on, that frame is read after all.** FG interpolates between the
  frames it is handed, so the field stops being disposable the moment bypass
  frames are presented by the FG presenter (#104). The hardware backend
  (`motion_backend: nvofa`) already fills it for every captured frame, so the
  DIS cost is only paid when that path is unavailable; the Python-side
  `previous_gray` is still cleared, because the two sources must not mix.
* **The preset is a setting now** (`flow_preset`), because it is worth a
  measurement rather than an assumption.

### What the guides cost

`dis.calc` on the same 320×180 pair, and the endpoint error against
synthetic ground truth (a known pixel shift, in flow-grid pixels):

```
                     ms      EPE mean over 1..16 px    p99 @1px   max @32px
FAST / finest 1    2.85          0.001 - 0.029           0.10       8.5
ULTRAFAST / 1      0.34          0.0003 - 0.076          0.49      31.6
FAST / finest 2    1.45          (not scored against GT)
```

`ultrafast` is **8.4× faster**, and its mean error stays under 0.08 flow px
— inside the 0.5 px noise floor `guides.py` already zeroes, so on average it
cannot even reach the motion field. Its tails are the reason it is not the
default: p99 0.49 px at a 1 px shift and a 31.6 px maximum at a 32 px shift,
against 8.5 px for `fast`. Tails are what a temporal network shows as
smearing, and nothing here can judge that without the GPU pipeline and real
moving content in front of a person — so it ships as a switch with its
numbers attached, the way `nr_small` did before it became the default. The
before/after wipe is how to judge it.
Fallback (dxcam + full-frame send) stays intact.

**Guides.** The optical flow needs a small gray frame. On `GRAY` the worker
computes it with an honest block average (12×12 per cell at 4K → 320×180,
matching `cv2.INTER_AREA`; bilinear would alias text and break the flow) and
writes it into a named mapping. No 4K frame ever crosses the CPU.

**Output.** On `WNDO` the worker raises its own borderless D3D12-swapchain
window across the screen and presents the NGX result itself: pixels never
return to Python. The pygame window stays as the menu layer — its background
is filled with a chroma key and made transparent (`LWA_COLORKEY`). While the
menu is open the window's global alpha (`LWA_ALPHA`) goes to 255, otherwise
the bright frame underneath bleeds through the panel.

Logical menu state and physical HWND visibility are handled separately. Every
open/show request reveals a hidden layer, reapplies visibility, raises it above
the presenter and redraws it. This makes taskbar activation idempotent while
recovering the invisible-menu state reported after monitor/GPU changes (#87,
#88).

**NR OFF.** With FG off and no recording or pending screenshot, the lifecycle
closes DDA/WGC and presentation, sends no `FRM1` packets and hides the output;
the worker stays warm but capture, motion, Evaluate and Present counters stop.
Opening the menu needs only the transparent HUD layer. Turning NR back on — or
starting FG/recording/a screenshot — rearms channels from a fresh source frame
and cleared temporal history. Those explicit consumers use bypass frames while
NR itself remains off.

**Recording path.** Frames are requested from the worker with
`FRAME_FLAG_WANT_PIXELS` (the same mechanism as screenshots), the open menu
is drawn onto the frame with `draw_capture_overlay()`, then PyAV encodes
AV1 NVENC. A screenshot copies this processed frame before opening Save As,
so the dialog can never become the next captured frame.

Two constraints that look like quirks but are mandatory:

- **The worker binary must be named `nvngx.dll`.** NGX Core returns
  `FAIL_PlatformError` on `Init_Ext` for any other process name. Verified
  experimentally.
- **Work resolution is capped at 2560×1440.** At 4K the feature 18 goes
  silent: the worker hangs on frame zero in both legacy and upscale modes.

Scale changes go through `RNSZ` (~60 ms, the worker recreates the NGX
feature in-process). If `RNSZ` fails — fall back to a full worker restart.

### Compatibility gate and diagnostics

Before desktop capture or any presentation window exists, v1.13 runs one
short-lived worker on three deterministic 640×360 RGBA8 frames with zero RG16F
motion. The native worker sends `CACK` immediately after `CreateFeature`.
Only category *unsupported* with the exact result `0xBAD00001` is durable
UNSUPPORTED; stderr text, SAFE PASSTHROUGH output, `unknown`, `not run` and
unexpected `SKIP` can never become PASS. All three Evaluate replies must have
the expected dimensions, format and byte count.

The cache key is for that canonical probe, not a claim that the current 4K/HDR
desktop was exercised. It includes app version, worker hash, selected/resolved
GPU, driver, Windows build and hashes/modes of every possible NR route: explicit
`NS_NR_DLL`, BYO candidate plus bundled fallback, forwarder, core preload and
via-core mode. A changed key reruns the probe. PASS and exact UNSUPPORTED are
cached; timeout, crash, TDR or device loss receive a timed quarantine. Manual
Retry clears the current record. Every production worker start/restart checks
that its freshly reconstructed key still matches a PASS.

The Program tab creates a deterministic support ZIP containing a bounded,
scrubbed log tail and structured version/GPU/driver/display/runtime-signature,
stage and HRESULT/SEH/DRED markers, plus an allow-list of product settings
(each value bounded; no paths, presets or hotkeys). It excludes the environment
dump; usernames, secrets, absolute user paths, other programs' window titles
and the names of files the conversion queue refused are redacted.
`NeuralScreen.log` itself is moved aside to `NeuralScreen.log.1` at startup
once it passes 8 MB, so it keeps the latest sessions and does not grow without
end.

## Performance

Measured on RTX 5070 Ti, 4K desktop, `work_scale` 0.5 (1920×1080), pipeline
fully on the GPU (DDA + GRAY + WNDO + MOTS). Worker-side phase breakdown
(enable with `NS_PHASE=1`):

```
                 acq   dda  upload   eval  present   frame     FPS
NR ON            0.0   0.7     0.1   16.6      0.5    17.9      55
NR OFF + consumer
       (bypass)  ~3    ~4      0.1      -      ~3      7.3  121-133
```

**NGX evaluation is 16.6 ms — 93% of an NR frame.** Everything else together
costs 1.3 ms, so the practical ceiling on this hardware is set by NGX, not by
the plumbing. Bypass is used only while FG, recording or a screenshot explicitly
needs frames with NR off; ordinary NR OFF is idle and therefore has no FPS.

The active loop can be capped at 30, 60 or a custom 15–240 FPS on a monotonic
deadline; a slow frame resets the schedule instead of causing a catch-up burst.
The displayed NR rate counts only completed neural evaluations, the optional
second rate is the FG presenter cadence, and the static-skip counter is separate.
None of these labels claims to be the physical monitor refresh rate.

An earlier revision of this section claimed "NGX itself is ~1 ms". That was a
measurement error: the figure came from regressing round-trip time against
work resolution, and such a regression only sees the resolution-dependent part
(0.46 ms/MPix). NGX's large constant cost was invisible to it and got
attributed to transport.

The 1440p figures previously published here (64 FPS) predate the DDA fence
fix and understate current performance; they have not been re-measured.

### The client off the critical path: the early reply

The loop used to run in lockstep: send the frame, wait while the worker
evaluated **and presented** it, then do the client's own work - the HUD, the
commands, the next capture request - while the GPU sat idle. With
`FRAME_FLAG_EARLY_REPLY` (bit 13 of the frame flags) the worker answers as soon
as the frame's work is on the GPU queue, then waits for it and presents as
before; the client's work runs in that time. Nothing is reordered: the worker
reads the next message only once this frame is on screen, and a dead worker
still shows as a missing answer. It is honoured only on the plain present path
(a processed frame, worker capture, no pixels, wipe or HDR); Frame Generation,
HDR, screenshots and CPU recording keep the old timing. `NS_EARLY_REPLY=0`
turns it off.

Measured off-screen on an RTX 5080 at 2560×1440 - window capture, worker
present, NVOFA - with the client's per-frame work simulated at the 1.6 ms a
user's log shows:

```
                             lockstep   early reply
full-resolution NR  FPS        119.6       147.7     (+23.6%)
  client waits per frame       6.70        0.65 ms
Boost (work 1664x936) FPS      170.6       177.2     (+3.9%, at the display's refresh)
```

What remains between two frames is the capture round trip: in the Boost run
the GPU idled 2.07 ms a frame between presenting one frame and starting the
network on the next (`gap` in the `NS_PHASE` boundary report). With NVOFA the
client needs the capture before `FRM1` for one thing only - the scene-cut test,
the mean difference of two 320×180 grays against 0.24. Done in the worker, that
round trip could go too.

### work_scale costs nothing (in upscale mode)

In the legacy upscale mode (nr_small off) NGX evaluation time does not depend
on the work resolution at all. Measured across the whole slider range on a 4K
desktop:

```
work          MPix   eval ms    FPS
960x540       0.52    16.13    53.8
1344x756      1.02    15.90    55.4
1728x972      1.68    15.93    56.8
2112x1188     2.51    15.96    55.7
2496x1404     3.50    15.98    55.3
```

Fit: `eval = 15.97 ms + (-0.01) ms/MPix`, R² = 0.011 — i.e. noise, not a
trend. Seven times more input pixels cost 0.2 ms, which is within the
measurement spread.

**So in upscale mode the slider is a quality control, not a speed control.**
Lowering `work_scale` buys no performance and only costs sharpness. The work
size is clamped to the 2560×1440 NGX cap anyway, so the slider always lands
exactly at the cap: 2560×1440 from a 4K desktop, 2304×1440 from 2560×1600.

Re-confirmed with D3D12 timestamps on the queue, i.e. GPU time inside
`Evaluate` rather than time around the submit, on two desktop resolutions:

```
desktop      work_scale range   work MPix      eval GPU
2560x1600    0.30 .. 1.00       0.37 .. 3.32   7.9 - 8.6 ms
3840x2160    0.30 .. 1.00       0.75 .. 3.69   15.70 - 15.73 ms
```

Five to nine times the work pixels for the same time, at either resolution.

### ...but the screen resolution does

The two rows above differ by 2.02× in screen pixels and by 1.96× in eval
time. That is the whole story: in upscale mode NGX is handed the **full-res**
frame and returns a full-res frame, downsampling to the work size internally.
So the cost is set by the desktop, not by the slider.

An earlier revision of this section concluded "the model works at its own
fixed internal resolution". That was wrong, and wrong in an instructive way:
it came from varying only `work_scale` at a single desktop resolution, which
by construction cannot see a dependence on the frame size.

Consequences: in this mode a 4K desktop pays a floor of 15.7 ms of NGX per
frame, about 47 FPS end to end, and no amount of plumbing gets near a 144 Hz
panel. On 2560×1600 the same floor is 8.0 ms.

### Processing at a reduced resolution

That floor is not a law, though. The network is **same-resolution — it
enhances, it does not upscale**, so its cost tracks the pixel count it is
handed, and "upscaling" mode hands it the whole screen. Measured in isolation
by feeding the worker different frame sizes directly:

```
work          MPix   eval GPU
1280x720      0.92    2.90 ms
1920x1080     2.07    4.60 ms
2560x1440     3.69    7.10 ms
```

Fit: `eval = 1.50 ms + 1.51 ms/MPix`, which also predicts the two numbers
above (14.0 ms at 4K, 7.7 ms at 2560×1600) and matches what the
[neural-upstream](https://github.com/matiasLombo/neural-upstream) add-on
measures for the same network in games.

So **Process at reduced resolution** (menu → processing, `"nr_small"` in
`config.json`) scales the frame down to the work resolution, runs the network
there, and scales the result back up. On a 4K desktop, work at the 2560×1440
cap:

```
                 eval GPU    FPS
full screen       16.05     42.9
reduced            7.25     65.3
```

**Off by default** (set it with the resolution slider in the menu), and the
softness that used to come with a reduced work size is gone: the result is
composed onto the pristine 1:1 native frame by the **matched residual
composite** (see below), so text and edges keep full resolution while the
cheap low-res network does the relighting.
Measured end to end on the 4K desktop: 47.9 FPS at full screen vs 71.9 FPS at
work_scale 0.65 with the composite — a 50% gain with the native anchor intact.

With it on, **Work scale** finally does something — it is the resolution the
network actually sees. With it off the slider is inert, which is exactly what
the measurements at the top of this section were showing all along.

### Matched residual composite

The composite is the DLSSNR-Cost-Scaler principle applied to the desktop:
instead of stretching the low-res network result up, the worker writes

```
result = native + (nr_out - nr_in) * strength
```

at full resolution — the neural delta (what the network changed) lands on the
pristine 1:1 native frame. The native frame stays the anchor, so text, edges
and UI keep full sharpness while the network runs at the cheap work
resolution. Measured on a synthetic detail pattern (test_residual.py):
Laplacian detail 983 with the composite vs 88 with the plain bilinear
upscale (input 951), and a hard edge 87.8 vs 59.1. `strength` is fixed at
1.0; `NS_NR_RESIDUAL=0` forces the plain upscale path for the tests.

The delta is added in **display space**, not in linear light: the textures are
`R8G8B8A8_UNORM` and the network itself works on those values, so the
composite is consistent with its input. It is worth knowing where to look if
shadows ever misbehave - a delta that is linear in code is not linear in
light, and the error is largest in the darkest pixels. Moving the composite to
linear would change the look of every scene, so it is a deliberate choice, not
an oversight.


### The NR cascade: more than one pass over a frame

An experiment, reachable only in Boost mode, and off (one pass) by default.
`nr_passes` 1-4 runs the network over the same frame that many times before
anything is composed.

Each pass gets its **own NGX feature**. Calling one feature twice inside a
frame hands it two evaluations with no motion in between, which is a lie to
its temporal history; separate features each keep their own. That is also
what makes it expensive. Measured at a work resolution of 2560x1440, the
worker's video memory after each create:

```
pass 1   845 MB
pass 2  1738 MB   (+893)
pass 3  2375 MB   (+637)
pass 4  3012 MB   (+637)
```

So roughly **640 MB per extra pass** here, and four passes take the worker
from 845 MB to 3 GB. The figure scales with the work resolution - it is the
network's own working set, not a constant - so treat it as the shape of the
cost rather than a number to quote. An earlier draft of this document said
"about 440 MB", which came from the same estimate that said two passes would
cost half the frame rate; both were guesses, and both were wrong.

The passes ping-pong between two work-resolution scratch buffers, `nr_out`
and `nr_alt`, and `nr_in` - the composite's anchor - is never written. Which
buffer pass 0 starts on is chosen by the **parity** of the pass count, so the
last pass always lands in `nr_out`, which is the name everything downstream
reads. The alternative, letting it land anywhere and swapping the two
pointers afterwards, costs the same nothing per frame and is wrong: it
changes which resource `nr_out` is from one frame to the next, and both
composites cache their descriptors on that pointer. An even pass count would
then rewrite a shader-visible descriptor heap every frame while up to two
earlier frames are still reading it.

Those buffers exist only when the network runs below the frame's own size: at
1:1 it writes the full-size output directly, and there is nothing to cascade
through. So with Boost on and more than one pass, a work size equal to the
frame - the slider at the top, on 1440p and below - steps 2 px under it on
each axis (`settings_io._work_size`). That brings the matched residual
composite in, and the cascade runs. It also keeps more detail than 1:1 did,
because the composite anchors on the native frame. Without Boost the count is
hidden and sizes nothing (`settings_io.cascade_passes`). A cascade that still
runs short says why at the end of the worker's `NR cascade built` line.

Measured on the bench at 2496x1404, Boost on:

```
passes   NR fps
1        58.3 / 64.7 / 63.3   (~62)
2        42.9 / 43.1 / 43.3   (~43)
```

**The second pass costs about a third of the frame rate.** Whether it buys
anything visible is not settled, and the control exists to answer that.

The count rides in bits 2-4 of the RNSZ flags word, so changing it costs no
teardown once the features exist. The stream header has no field for it - the
worker always starts at one pass, and the saved value is sent as soon as the
stream is running.

### Outside the panel: the counter, the tray and the scale

`fps_overlay` draws the same reading the panel's header shows - `NR 55.1`, or
`FG 167 (55.1)` with Frame Generation on - in one of the four corners, with
the panel closed. One implementation produces both (`status_readings`),
because a second copy of "which numbers appear when" would drift. It is drawn
into recordings and screenshots like the rest of the overlay, which is why it
is off by default.

`tray_on_minimise` and `tray_on_close` decide what the taskbar button's two
buttons do. Neither stops the neural pass: this program's output is the
picture on the screen, so a minimise that also stopped it would change what
the user sees without saying so. The 1x1 window that carries the button is
hidden, never minimised and never destroyed - either would lose the button
for the rest of the session (#93). Going to the tray is refused, with a
notice, when the tray icon is not actually running: it is the only way back.

`menu_scale` is one of five steps - 80, 90, 100, 115, 130% - and
`menu_scale_auto` says whether the user has picked one yet. While it is set,
a launch lays the main page out at each step and takes the largest that needs
no scrolling on the work area (the desktop minus the taskbar). At 100% that
page is 1165 px of content, which does not fit a 1080p desktop: it came up
scrolled, with its last controls behind the taskbar. The fit never grows the
panel above 100%, and it stops running for good the moment a step is chosen
by hand.

### Which model changes what

Measured on a desktop capture, fine detail against the untouched frame:
Default **+18.7%**, Natural **-11.4%**, Cinematic **-23.4%**. Three different
outputs rather than three strengths, which is why a saved preset keeps the
model it was saved with.


## Before / after wipe

The **Before / after wipe** slider leaves the left share of the frame
unprocessed, so the raw capture and the NGX result sit side by side with an
accent-coloured divider between them. 0 turns it off.

It happens in the worker, on the GPU: one `CopyTextureRegion` of the left
strip of the input over the output, then the divider through
`ClearUnorderedAccessViewFloat` with a rect. Both run before Present and
before pixels are handed back, so the wipe lands in recordings and
screenshots by itself. The position rides in the top 16 bits of the frame
header's flag field, so moving the slider does not recreate the worker.

## Older GPUs (20/30/40-series)

`nvngx_dlssnr.dll` refuses to create the feature on anything below Blackwell.
Its own version resource says `NGXGpuArchitecture = NVSDK_NGX_GPU_Arch_Blackwell2`,
and it carries the message

```
DLSSNR: Unsupported GPU architecture 0x%x, minimum required 0x%x
```

The bundled build is the leaked **310.8.0** runtime (from the
RankFTW/rhi-repo mirror): parsing its fatbin headers shows `sm_75/86/89/120`
kernels - the universal build. The architecture hook (below) makes the
feature create succeed on non-Blackwell cards; the refusal on cards the
kernels cannot run on (Turing) is a policy check, not missing code.

The library learns the architecture through nvapi — it loads `nvapi64.dll`,
takes its single export `nvapi_QueryInterface` and asks for
`NvAPI_GPU_GetArchInfo` by id. The worker patches that one function in **its
own process memory** at startup: the prologue is saved, replaced with a jump
to our handler, and restored around every real call, so any GPU handle is
still served by NVIDIA's own code — only the returned architecture is
rewritten to Blackwell. Nothing in NVIDIA's files is modified, and on a
50-series card the hook disables itself and does nothing.

**On by default** (set `NS_ARCH_SPOOF=0` to disable). **Confirmed working on
a 40-series card** by a user who ran it; 20- and 30-series are still
unverified. Nothing here can test any of them — the only card on this machine
is a 5070 Ti, where the hook disables itself by design. The menu's GPU dot
tells you the truth either way: it goes green only when the worker actually
created feature 18, not when the architecture merely looks right.

This likely conflicts with the license terms of NVIDIA's redistributable. It
defeats no copy protection and modifies no files, but enabling it is your
call.

## Building the worker

```
native\build-host.bat
```

Requires MSVC 2022 Build Tools at
`C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools`. The script
builds `dlss5-feed-host64.cpp` into `native/nvngx.dll`, linking
`native/lib/Windows_x86_64/x64/nvsdk_ngx_d.lib`. NGX headers are in
`native/include/`.

The artifact `native/nvngx.dll` is not committed to the repo.

## Frame Generation on the desktop (opt-in)

DLSS-G's desktop build runs after the neural pass: the presenter asks the FG
feature to interpolate between consecutive output frames. The depth fed to it
is flat and the motion is estimated per frame - there is no engine
cooperation - so UI and text can distort where motion estimation guesses
wrong; that is inherent to the screen-space approach, not a tuning issue.
The multiplier is x2/x3/x4 (the DLSS-G contract caps there), the switch is
opt-in, and the header reports both rates - the network's and the presenter's
- whenever they differ.

FG is independent of NR: with the neural pass off, the presenter interpolates
the raw capture instead of the neural frame, so the doubled rate survives
turning NR off. It used to be stopped by the bypass present path on every
frame, which is why the switch appeared to do nothing there (#104).

**Frames go to the screen on their own queue.** The swap chain lives on a
high-priority queue of its own, and every copy into a back buffer runs there -
the FG presenter's and the ordinary SDR, bypass and HDR presents'. The network,
the capture and FG's own evaluation stay on the worker's queue. While the two
shared one queue, a frame due on screen waited for whatever had been submitted
before it, usually the next frame's NR pass. Measured at 4K (work 2496×1404,
four passes, HDR, RTX 5080): every real frame of FG x2 waited ~13 ms for its
copy, so the output left in uneven pairs while the counter read a clean 2x; at
x4 one generated frame in nine missed its slot and was dropped. On its own
queue the copy waits under 1 ms and none are dropped, the worker's source rate
with FG on is 5-19% higher (x2-x4), and the compositor's back-buffer waits no
longer stall the network. The ordinary present with FG off pays for the second
submission: +0.3 ms a frame. With `NS_PHASE=1` the presenter logs its copy and
vblank waits and how many generated frames were shown, late or superseded
(`[phase] fg presenter`).

The FG runtime (`nvngx_dlssg.dll`) ships in the archive - the public
310.9.1.0 redistributable, NVIDIA-signed, included unmodified. The licensing
position is stated in the README notice: research use, takedown on request.
Absent the DLL, the switch refuses politely and nothing breaks; a different
build drops into `native/libraries/`.

## Runtimes and the libraries folder

The NR and FG runtimes ship in the archive. A user-supplied build in
`native/libraries/` takes priority over `native/`; an NR BYO file is checked for
an NVIDIA signature and product identity before it is mapped, otherwise the
bundled copy remains the fallback. The signature is judged as of its
timestamp, as Windows judges it, so a DLL whose certificate has expired since
it was signed still passes. There is no network updater or downloader.

## Reproducible release contract

`build_release_zip.py v1.15.0` accepts only a clean checkout whose `HEAD` is the
requested tag and whose version sources agree. The allowlist covers every
shipped Python/C++/header/shader/resource, while `runtime-manifest.json` binds
the package paths and hashes. Archive ordering, timestamps and metadata are
fixed, so the same tagged inputs produce identical bytes. The package carries
`THIRD-PARTY-NOTICES.md`, the manifest, `VERSION.txt` and `SHA256SUMS`.

`verify_github.py` downloads no source-of-truth from the worktree: it checks the
published asset, manifest and checksums against the tagged Git blobs and rejects
missing, extra, absolute, drive-qualified or traversal paths. The test runner
reports unit/static, WARP, GPU and GUI-E2E separately; a canonical `SKIP` with
exit code 0 is counted as SKIP, never silently promoted to PASS.

The menu has a cyclic Tab/Shift+Tab order, arrow-key editing, Enter/Space
activation, focus auto-scroll and a contrast-tested focus ring. Window rows keep
their HWND as hidden identity; duplicate titles and colons remain display text.
