# Changelog

Release history of NeuralScreen, reconstructed from the repository itself: the
Git tags of `perseval-BLR/NeuralScreen`, the published release bodies
(`gh release view <tag> --json body`) and the assets each release carries
(`gh release view <tag> --json assets`). Dates are the GitHub **publish** dates
in UTC, not the tag timestamps.

Scope and honesty notes:

* The list starts at `v1.0.0` and ends at `v2.1.5`; every tag in that range has a
  published GitHub release. GitHub returns more releases than the tag count
  because the count includes the tags listed as out of scope below.
  The tag `v0.1.0-alpha` (2026-09-06) is **not** part of this history: it is a tag
  from the other project line that shares this repository's history (the AMD
  fork), not a NeuralScreen version. It is excluded rather than guessed at. The tag
  `pre-state-refactor` (2026-09-11) is an internal marker commit with no release
  and is not listed either.
* `v1.3.1-win10-diag` is a diagnostic-only tag, not a version. It is listed at
  the bottom for completeness.
* Each entry is a summary of what the published release notes said. Where the
  notes were written for a single audience or a single machine, the entry keeps
  that focus instead of inventing broader claims.
* v1.5.1's body also documents v1.5.2's contents (the UX package was first
  attached to that tag and then given its own release); v1.5.2's own entry is
  taken from its own body.
* No entry is invented: every bullet is traceable to the release body of that
  tag, and the asset list per release is the same eight-file set unless noted
  (the early releases carried a different set - see the individual entries).

---

## Unreleased

* **Fisheye: a wide-lens look with the game's own field of view, and a little
  webcam noise.** A new switch on the main page, under the effect sliders,
  bends the processed picture the way a real wide lens does. Set **Field of
  view** to the game's FOV (110-120 is typical): the edges a wide game frame
  stretches are compressed instead, the corners stay where they are, nothing
  goes black, and a larger angle is a stronger look. **Webcam noise** adds a
  small sensor's grain - soft, stronger in the shadows, renewed 30 times a
  second. It is applied after the network and after Frame Generation, so the
  network's input and FG's interpolation never see it, and it lands in GPU
  recordings, Spout and screenshots. Measured: no frame-rate cost at 4K
  (100-101 fps with and without). It is a game mode: away from the centre a
  click lands somewhere else than it looks.

* **A still screen is held and the edit steadied on the CPU motion path too.**
  An unchanged capture is evaluated a few times and then keeps its result, and
  the stabilizer carries the edit over time - both start over on any command.
  On the CPU motion path (motion backend CPU, or NVOFA falling back to it) the
  client latches the capture with a small command before every frame, and that
  command counted: a still desktop was re-evaluated on every frame and
  shimmered, and the stabilizer reset its history every time and did nothing.
  Measured on the worker, a still window sent that way: 66 of 66 frames
  evaluated before, 0 of 70 after.

* **A stabilizer that does not fit in video memory is tried once, not on every
  frame.** When its textures could not be created (a large Boost work size,
  several NR passes holding the rest of the memory) the stabilizer stayed off
  by trying again on every frame: allocate, free, wait for the GPU, and one
  more log line, all session long. It now stays off until the next resize and
  says so once.

* **A Frame Generation fault with NR off no longer leaves two presenters on
  one window.** When DLSS-G faulted while NR was off, the frame fell back to
  the ordinary present while FG's own presenter thread was still running, so
  both could present into the same window for a moment - a torn frame or a
  present error. It is now stopped first, as the NR-on path already did.

* **Frames sent from the client after a worker capture use the right texture
  state.** The upload of a frame sent through the pipe judged the colour
  texture's state by whether motion had been uploaded yet; after a capture in
  the worker (a prepared capture, a screenshot retry) followed by a fall back
  to client frames, that guess was wrong in either direction. It now follows
  the colour texture's own state, as the capture path already did.

* **Quick changes no longer undo each other.** A setting is applied 0.3 s after
  the last change (#115), and each change used to start from the settings still
  running rather than from the ones already waiting - so a second change inside
  that moment threw the first away. Two presses of the work-scale key moved one
  step instead of two (holding the key walked a single step), moving intensity
  and then tone lost the intensity, a profile followed by a slider lost the
  profile, and a preset saved right after a slider move stored the old value.

* **Turning the second parameter set (NR passes 2+) off is remembered.** The
  switch cleared it for the running session but left it in config.json, so with
  two or more passes the next launch brought the set back on.

* **A hand-edited second parameter set with NaN or Infinity is ignored.** The
  main sliders and presets already refused such numbers; the set for NR passes
  2+ let NaN through to the worker.

* **A recording folder that is gone no longer locks every dialog.** "Add files"
  on the conversion page starts in the recording folder; when that folder could
  not be created (a disconnected drive, an offline share) the picker failed in
  a way that left screenshots, the folder pickers, the diagnostic package and
  the picker itself refusing silently until a restart. The picker now opens
  where Windows last left it.

* **A wipe slider a hair above zero no longer counts as on.** A position too
  small to draw still switched the wipe on: with HDR the divider appeared at
  the left edge, and Frame Generation restarted its history on every frame.

* **A preset with the strength at 0 no longer gets the card blocked as
  unsupported.** The startup check added in v2.2.0 refuses a runtime that
  answers success but changes nothing - and it ran with the user's own
  settings. At intensity 0 the network returns the picture exactly as it was
  (measured: not one value changed), so after the next program or driver update
  the check said "no effect" and blocked the start on a card that works, until
  config.json was edited by hand. The check now always runs with the shipped
  profile.

* **The diagnostic package no longer keeps part of a refused file's name.** A
  file the conversion queue refused is named in the log and cut out of the
  package - but only up to its first parenthesis, so "Anna (wedding).mp4" left
  "(wedding).mp4" behind.

* **A start while Windows lists no display at all no longer fails (#158).** At
  logon the program can start before the monitors are up, and for a moment
  Windows may report none. The capture's last fallback raised in that case and
  the start ended in "NeuralScreen failed to start"; it now opens the slower
  GDI capture instead, as it already did when the fast capture refuses.

* **Autostart waits for the desktop before reading the monitors and the cards
  (#158).** Started from the Run key while Windows was still logging on, the
  program saw one wrong-sized display instead of three, was refused the screen
  capture, and found the graphics cards listed in a different order - and built
  everything for that moment. It now waits until the user's desktop is up and
  the monitor layout stops changing (at most three minutes), then starts as
  before. A start on the desktop - every ordinary launch - does not wait.
  Thanks to the reporter, whose own fix this follows.

* **A monitor that is late or unplugged keeps its place in the settings
  (#158).** When the saved monitor was not there at startup - a TV, a dock, a
  monitor still waking up - the program used the first one for the session,
  which is right, but the next save wrote that one over the user's choice for
  good. The same happened when the captured monitor vanished mid-session. The
  stand-in is now only for the session; the saved monitor changes when the user
  picks another in the menu.

* **A screen capture refused "for now" is asked for again (#158).** At logon,
  under a UAC prompt or on the lock screen, Windows refuses the fast desktop
  capture because the input is not the user's yet. The program asked once per
  worker, so one refusal at the wrong moment left the whole session on the slow
  capture path. It now asks again every few seconds for about two minutes. A
  refusal for any other reason - a card that drives no display - is final, as
  before.

## v2.2.0 - 2026-10-08 - Steadier NR: the edit stabilizer, NVOFA without jelly on still backgrounds, real scene cuts, and the tracker bugs

* **The neural edit is steadied over time: less shimmer and "jelly" in Boost.**
  The network's output moves a little on every frame even where the picture
  does not, and on moving content the motion field's error comes on top. The
  network's edit - and only the edit; the picture underneath is never delayed
  - is now carried along the motion and blended with the current one where a
  per-pixel test trusts it. Measured on the worker: frame-to-frame instability
  of the edit about halved on a pan (-43%), a moving object (-45%) and still
  content (-49%), the shimmer around a moving object with NVOFA-like vectors
  -57%, the strength of the effect kept within 2%, no ghosting when the
  vectors are wrong. Costs about 0.1-0.3 ms a frame. `NS_STAB=0` turns it off. Its log lines (`[stab]`)
  reach NeuralScreen.log and the diagnostic package.

* **Scene cuts are judged on the picture, not on how much it moved.** On a cut
  the network's history is thrown away and the picture pops; a missed cut
  smears one picture into the next. The old rule - the average change between
  two frames above a fixed 0.24 - fired on every frame of a fast pan in
  cel-shaded animation (another project counted 42 resets in 120 frames of an
  anime clip run through NeuralScreen) and missed cuts between two dark or two
  text-heavy pictures. The new rule first lines the frames up (a global shift
  of up to 48 px at the 320x180 analysis size, and the same brightness and
  contrast), then asks whether what is left is still a different picture. On
  the evaluation set: false resets 4.8% -> 0.6%, missed cuts 39% -> 1.3%; the
  anime-style test clip resets on its five cuts and nowhere else. The client
  and the worker run the same rule and agree frame for frame.

* **Two more GPU-priority experiments for #142, off by default.** The
  reporter's A/B showed `NS_GPU_PRIORITY=high` changes nothing while hardware
  GPU scheduling (HAGS) is on - the practical answer there is HAGS off. For
  the next measurement `NS_GPU_PRIORITY=realtime` (needs NeuralScreen started
  as administrator) and `NS_GPU_QUEUE_PRIORITY=high|realtime` (the priority of
  the queue the network runs on) are available; each says in the log what it
  got, and a queue Windows refuses falls back to a normal one.

* **Building the worker no longer destroys the working one when the build
  fails** (developer builds only; nothing shipped changes). build-host.bat
  linked straight to native\nvngx.dll, and the MSVC linker deletes its output
  when a link fails - one unresolved symbol and the worker was gone. It now
  links to a temporary name and moves it into place after a successful link,
  as build-clang.bat already did.

* **A neural runtime that "works" without changing anything is now caught at
  startup.** The compatibility check counted an evaluation as passed when it
  answered success and returned a frame of the right size - so a card or a
  runtime that accepts the work and hands the picture back untouched (an
  unsupported card on a patched runtime "starts without processing the
  picture") passed, and NR then ran doing nothing. The real runtime changes
  96-99% of the check's synthetic pixels; a check where no frame changes (or
  the output is black) now ends with a clear message, in the user's language,
  that the renderer does not run on this card.

* **NVOFA "jelly": still parts of the picture no longer get motion (#151, #141,
  #95).** NVOFA measures one vector per 4x4 block of a small grey copy of the
  frame, and the field is stretched over the picture - so the vectors of
  anything moving spread onto the still background around it, and the driver
  also answers small vectors on still pixels by itself. The network then
  warped its history by them. Each vector is now kept only where it explains
  its neighbourhood better than no motion at all. Measured on a moving
  textured square over a still background: the background rows around it
  carried motion on 91.7% of their pixels before and 0% after, while the
  square kept its motion (56% -> 64% of it pointing the right way).

* **A still desktop or window no longer shimmers or keeps the GPU busy.** The
  network was run on every frame, also when the capture had not changed - and
  on identical input its output never settles: it moves by about a quarter of
  a level on average and up to 3 levels every frame. An unchanged capture is
  now evaluated a few times, until the network's history has it, and the last
  result stays on screen. New content, a parameter, pass or size change, the
  comparison wipe and a screenshot all bring the evaluation back at once.

* **Extra NR passes no longer darken the picture pass after pass.** Passes 2+
  repeated the whole profile, local tone included, and Natural's local tone
  darkens a little every time it runs: against the input, the picture's mean
  brightness was 0.988 with one pass, 0.978 with two and 0.970 with three.
  Without a per-pass set of their own, passes 2+ now run with tone 0 - three
  passes keep 0.988 and still work on detail - and switching a per-pass set
  on starts it from those same numbers.

* **The "adaptive exposure" is gone, because the network never used it.** It
  mapped the picture's average brightness into DLSS.Exposure.Scale to lift dark
  scenes, and the log announced it in every desktop session. The neural
  runtime does not read that parameter at all: its library carries no exposure
  name, and the output with the value forced to 0.3, 1.0 and 3.0 is the same to
  the byte. Nothing in the picture changes; the false log line and the wasted
  work are gone. A dark-scene lift that works has to act before the network.

* **One-window mode is no longer held at 60 frames a second (#155).** Window
  capture was opened without a minimum update interval, and current Windows 11
  then delivers a window at most ~60 times a second whatever it draws - the
  reporter's game lost 40% of its NR frame rate in a window against fullscreen.
  The interval is now 4 ms: measured here 60 -> 120 fresh frames a second on a
  120 Hz monitor, and 49 -> 63 on the reporter's machine.

* **A desktop capture that keeps changing format is pinned after the first
  change (#151).** An SDR display is captured through the older duplication
  call because it hands the desktop over as 8-bit - but one driver alternated
  8-bit and 16-bit frames through it anyway, on monitors that report 8 bits
  and no HDR: 307 capture rebuilds in two minutes, each one losing a frame.
  The format was already pinned for displays that report HDR or more than 8
  bits (#89); now the first change on any display pins it for the session.

* **A game controller that connects and drops again no longer closes the
  program (#152).** The interface library starts the joystick subsystem with
  everything else, and it fails on the "device removed" event of a controller
  it never saw arrive - a virtual gamepad, Steam Input or a wireless pad waking
  up. The main loop ended on it at startup. NeuralScreen does not read
  controllers, so their events are no longer queued at all.

## v2.1.10 - 2026-10-06 - A full audit: the open tracker reports, HDR early reply, and a capture that survives the secure desktop

* **Taking every hotkey off the keyboard now survives a restart (#134).** With
  all commands unbound, `build_bindings` correctly returns an empty set - but
  the hotkey controller, the Num Lock alert and the startup log treated `{}` as
  "no argument" and fell back to the defaults. The live session was right; at
  the next launch all eleven default keys were registered again while the panel
  showed every row as "none". `None` alone now means "the defaults".

* **Turning "Keep full speed while hidden" off now gives the speed back to
  Windows (#137).** Off only changed the status line: the opt-out set earlier
  stayed on both processes until they exited, while the log and the support
  bundle reported "the OS decides". Off now makes the documented "let the
  system manage" call on both processes, the status is read back from the OS,
  and a refusing system is logged once instead of every 30 frames.

* **One late answer from the worker no longer breaks every later command of
  its kind (#148).** When a wait gave up (a WNDO during a slow NGX start, for
  example), its late acknowledgement is remembered as owed. It usually arrives
  while the main thread waits for something else - another acknowledgement or
  a frame - and was dropped there without settling the debt. The next prompt
  acknowledgement of that kind was then taken for the late one, its wait sat
  out the full 15 s and failed, and the debt was re-armed: after NR off/on the
  picture fell back to the slow path on every resume for the worker's life.

* **A screenshot no longer carries our menu (#140).** The open menu was baked
  into every screenshot on purpose - but the Screenshot button lives in that
  menu, so a screenshot taken from it always had the panel over the picture.
  Screenshots now hold the processed frame only; CPU recordings still draw an
  open menu in, as before.

* **A slow worker start is no longer read as "no shared memory" (#148).** The
  worker answers the shared-memory handshake only from its command loop, after
  NGX initialisation - 5-19 s on the reporter's machine - but the client
  started its 10 s clock when it sent the request. Every rebuild on such a
  machine logged "shared memory unavailable" and began degraded. The client
  now waits for the worker's startup verdict first (on its own 45 s budget; a
  worker that dies still ends the wait at once), and the 10 s clock starts
  when the worker can answer. The commands that follow - the output window
  among them - are then answered in milliseconds instead of timing out too.

* **A switch written as `0`, `1` or `null` in config.json means one thing
  everywhere.** Numbers were left as they were, and the readers disagreed:
  `bool(0)` is off, while the checks written as `is not False` (the hotkey
  master switch, "keep full speed while hidden") read it as on - the panel
  showed one state and the program ran the other. Numbers are now stored as
  the boolean they mean, and `null` falls back to the shipped default.

* **Ctrl+Delete (and Delete with any modifier) can be bound again (#134).** The
  "clear this key" gesture caught Delete whatever modifiers were held, so
  pressing Ctrl+Delete to assign it silently removed the command's key
  instead. Only the bare Backspace or Delete clears now.

* **An RTSS frame limit inside NeuralScreen is named in the log (#143).**
  MSI Afterburner / RivaTuner Statistics Server injects `RTSSHooks64.dll` into
  Direct3D processes and limits them inside Present. The worker presents NR
  and Frame Generation through one swap chain, so a global 30 FPS profile
  pinned both at 30 while the panel's frame limit changed nothing. The module
  lists of both processes are now checked every few seconds and a hook is
  named once, with what to do about it - in the log a diagnostic package
  carries. Nothing about RTSS is changed.

* **HDR no longer loses a fifth of the frame rate to waiting (#149).** With
  Windows HDR and HDR compatibility on, NR ran at 68-70 FPS where SDR ran at
  85, with the network's GPU time unchanged. The early reply and the deferred
  frame tail (one GPU wait per frame instead of three, and the client's work
  overlapped with the GPU's) were reserved for SDR - not by decision: the
  merge that joined them with the HDR path left HDR out because the HDR
  present took no fence token. It takes one now and answers early like the
  SDR present. Found and traced to the line by the reporter. Not measured on
  an HDR display here (the bench has none); `NS_EARLY_REPLY=0` turns the early
  answer off for an A/B.

* **An NVIDIA driver too old for the neural renderer is named as the reason
  (#145).** On 576.x drivers NVIDIA's runtime answers the requirements query
  with "out of date" and then faults inside the create instead of refusing
  (#51 and #83 had the same sequence). The check caught the fault, but the
  dialog said only "failed; stage create; passed 0/0". The worker now reports
  that combination as its own verdict, the check files it as unsupported for
  this driver - the verdict is keyed by the driver version, so an update is
  checked afresh - and the dialog leads with "the NVIDIA driver (576.88) is
  too old", in all twelve languages.

* **No black strip down the right and bottom of a captured window on Windows
  10 (#140).** Window capture opened at the window's GetWindowRect size, which
  on Windows 10 includes the invisible resize border, while the frames it
  delivers carry only the visible frame - 813x1017 against 799x1010 in the
  report. Every buffer was built 14x7 px too large, and the strip no frame
  reached stayed black: under the network, on screen past the window's edge
  (the "shadow"), and in every screenshot. The capture now opens at the
  visible frame's size where that is smaller; on Windows 11 the two agree and
  nothing changes.

* **`NS_GPU_PRIORITY` - an opt-in GPU priority for the worker (#142,
  experimental).** With an uncapped GPU-heavy game in focus, NR fell to ~1 FPS:
  the network's GPU time grew to hundreds of milliseconds while its CPU side
  stayed under 1 ms, and Alt-Tab alone brought it back. A command queue's
  priority - the reporter's suspicion - ranks queues inside one process and
  is ignored under hardware scheduling, so it cannot help; the process's GPU
  scheduling class can. `NS_GPU_PRIORITY=above_normal|high` sets the worker's
  class at start and logs what Windows reads back. Off by default until it is
  measured on such a machine.

* **With Frame Generation on, a display mode change rebuilds the picture
  window.** Every ordinary present path rebuilds the window when Windows says
  the mode changed under it (#58: switching the output to 10 bits per colour
  went black). The Frame Generation presenter only logged it, on every
  present, and kept presenting into the old chain - a colour-depth change
  brings no resize, so the picture stayed frozen or black until FG was turned
  off and on. It now marks the chain for the same rebuild and says it once.

* **No "jelly" on a still picture with NVOFA motion (#141, #95).** NR runs on
  every frame, also when the capture brought nothing new - a window that did
  not redraw, a desktop that did not change. For those frames the CPU motion
  path answers zero; NVOFA ran optical flow on two identical frames with the
  last real flow as its hint and could answer large vectors anyway - up to 22
  px measured here on an unchanged window - which NR warped its history by.
  On a static photo that reads as the picture jiggling in patches, more in
  fullscreen. A frame without new content now gets zero motion and no optical
  flow, and the next real frame is measured against the last fresh one.
  `Motion: CPU` was the workaround; it is no longer needed for this.

* **The motion map covers the whole picture at every resolution.** The
  worker's gray downscale - what NVOFA, the CPU optical flow, the scene-cut
  score and the adaptive exposure all read - left black columns and rows at
  the right and bottom whenever the picture size did not divide by the map
  size: 46 columns and 26 rows at 1366x768, 53 and 67 for #140's 799-wide
  window, a few at 3440x1440. Motion there was measured against a black wall
  (artefacts along those edges), scene cuts were diluted and exposure read
  darker. Window capture almost always hit it; 1920x1080, 2560x1440 and 4K
  did not.

* **A Frame Generation start that fails is undone completely.** When the FG
  runtime loaded but its initialisation failed (an older driver, a card below
  Ada, a BYO runtime missing an entry point), it stayed loaded half-way, and
  switching FG on again skipped the initialisation and went straight to
  creating the feature on a runtime that was never set up - a refusal at
  best, a crash of the worker at worst. The runtime is now unloaded on any
  such failure and the next switch-on starts from scratch.

* **A slow NVIDIA start no longer blocks the program for half an hour
  (#148).** The startup compatibility check gave the worker 20 s to reach its
  first verdict - less than NGX initialisation alone took on a reporter's
  machine (5-19 s). A timeout there quarantines startup for 30 minutes, and
  the check runs exactly on the launches that start coldest: the first after
  a driver or program update. It now has the same 45 s budget as the live
  pipeline.

* **After a display mode change in window mode, the picture stays on the
  window.** The picture window is rebuilt when the display mode changes under
  it, and a new window opens at the monitor's corner; the follower places it
  on the captured window only when that window moves, and it remembered the
  old position across the rebuild. So the processed picture sat in the
  corner, with the real window unprocessed under the panel, until the window
  was moved.

* **A busy panel can no longer stall the picture.** When another program's
  window covers the picture (a borderless game), the worker raises our panel
  and puts the picture under it. That call waits for the panel's thread, and
  the panel's thread is not answering while it waits for the worker - so the
  worker could stand still until a 15-20 s timeout declared it dead and
  restarted it. The raise now runs off the frame thread, in the same order.

* **A UAC prompt, the lock screen or Ctrl+Alt+Del no longer restarts the
  picture.** They put up the secure desktop, and screen capture is refused
  for as long as it is up (a fullscreen game's mode switch does the same for
  a moment). The capture was reopened once, at once, and when that was
  refused the worker ended; the program restarted it - seconds of NGX start
  each time - and three such restarts turned NR off. The capture source is
  now kept: the last picture stays, the worker retries with a growing pause
  (up to 2 s, quietly in the log), and picks up as soon as the desktop is
  back. The same holds for a captured window recreated while minimised.

* **A crash inside NVIDIA's Frame Generation runtime no longer takes the
  picture down.** The neural renderer has always been protected against an
  old driver that crashes inside NVIDIA's code instead of refusing (#145);
  Frame Generation was not, and with FG on the same crash ended the worker,
  which restarted and crashed again. It is now caught, said in the log, and
  Frame Generation is switched off for the session.

* **The support bundle carries less of you and more of the session.**
  Converted file names no longer go into it (they were on every
  "[convert] <name>: ..." line; your own NeuralScreen.log keeps them); its log
  no longer starts in the middle of a line, which let the rest of a window
  title or a path through unscrubbed; and a window title containing "token="
  or "password:" is cut out like any other - the secret filter used to eat
  its closing quote and let the title through. A bundle now also keeps how
  the session started ([env], driver, [compat], adapter) and a tail where
  profiler lines cannot crowd everything else out, and after a crash and a
  relaunch it includes the end of NeuralScreen.log.1.
* **The driver version shown and used for the compatibility check is the
  card in use (#145).** It was the first NVIDIA entry in the registry - which
  can be a card removed years ago - and cards past the tenth entry were never
  read.
* **A too-large width, height or warmup in config.json no longer stops the
  program from starting.** They are reduced to what the worker accepts
  (7680x4320, 240 warm-up frames), and the log says so.
* **compatibility-cache.json and the support-bundles folder no longer grow
  forever.** The newest 32 verdicts and the newest 10 bundles are kept.

* **The release archive is cleaner.** v2.1.9 carried a pip launcher
  (runtime/Scripts/mss.exe) pointing at the maintainer's own python.exe -
  broken on every other PC - plus 31 C header files and a dropped Brotli
  module; they are left out now, and the build refuses to finish if any
  packaged file still names the build machine's folders. NeuralScreen.bat
  ships with the Windows line endings cmd.exe expects. The release checks
  now compare the uploaded documents and manifest byte for byte with the
  tagged files and refuse a launcher whose embedded version is stale.

* **The neural runtime is loaded only from the program's own folder.** The
  bundled nvngx_dlssnr.dll was loaded by bare name, so a native folder
  without it (a partial copy, an antivirus quarantine) let Windows search the
  working directory, the system folders and PATH and load whichever file of
  that name it found first. It is now loaded by its full path; your own
  runtime still goes through native\libraries\ and its signature check.

* **Recording and conversion fixes.**
  * A CPU recording no longer plays in slow motion: without the separate
    picture window, pixels came back on every frame and each extra one was
    given the next slot - three seconds of screen became a six-second file.
  * Sound no longer runs ahead of the picture after a quiet stretch (it was
    100-200 ms early); each sound packet goes where the playback device's own
    timestamp puts it, in both recording paths. The recording also follows a
    switch of the default playback device instead of capturing the old,
    silent one.
  * A CPU recording whose encoder fails ends at once and says so; what was
    encoded is saved and reported as cut short. A GPU recording that started
    too late leaves no stray file behind, and one the worker closed on its
    own is reported as cut short.
  * Converted anamorphic video keeps its picture shape (a 720x480 DVD file
    came out squeezed to 3:2). Converted images keep their transparency
    (JPEG gets a white background), a lossless WebP stays lossless, and a
    CMYK photo's colour profile is no longer attached to the RGB result.
  * A conversion the network did not actually process is no longer reported
    as converted: the job fails, or its row says how many frames were missed.
  * A screenshot named with a dot ("скрин 06.10.2026") is saved instead of
    lost, and a failed write no longer leaves a truncated picture.

* **Panel, hotkey and tray fixes.**
  * Keys typed after closing the menu no longer land in NeuralScreen and
    replay on the next open (a Space toggling NR, an Esc closing the menu
    again); the window you were in gets the keyboard back.
  * A letter, digit, arrow or navigation key without Ctrl/Alt can no longer
    be bound as a global hotkey - it stopped typing in every program.
    Shift+numpad digits, which could never fire, are refused too. A config
    that stored such a key falls back to that command's default.
  * A hotkey another program holds no longer fires in NeuralScreen as well,
    one press can no longer be delivered twice, and two hotkeys on the same
    key with different modifiers (Num1 and Ctrl+Num1) both work in games
    that swallow hotkeys.
  * The panel, HUD and alerts resize when the resolution changes or the
    program moves to another monitor; on a narrow portrait screen the panel
    no longer runs off the edge. Alerts that show a file path are shortened
    in the middle so they fit and keep the file name.
  * The tray menu is fully translated and follows a language change.
  * A second launch no longer starts next to a copy running as
    administrator, and that copy can still be asked to show its menu.
  * Window and monitor lists no longer come back short on systems that hand
    out large window or monitor handles.

* **The program survives a failed or hung worker start.**
  * A worker that fails to start while the program runs (for example after a
    DLL is dropped into native/libraries) turns NR off and arms the automatic
    revive instead of closing NeuralScreen. A failed monitor, window, GPU or
    setting switch leaves NR off and ready to revive, not a half-built
    pipeline.
  * A worker that hangs or dies while starting is reported as a failed start
    and cleaned up, instead of freezing the program on its first frame.
  * The window no longer goes "Not Responding" while a worker starts,
    restarts or shuts down (the #135 kind of stall).
  * Starting a recording during a heavy frame no longer makes the program
    restart a healthy worker.
* **Switching and capture fixes.**
  * After a monitor or window switch in the middle of a frame, the new
    pipeline no longer receives a full-screen grab, and the switch veil no
    longer drops early.
  * A screenshot whose Save As dialog is open survives a window resize or a
    monitor change.
  * Picking a window that cannot be captured while NR is off no longer
    restarts desktop capture in the background.
  * In one-window mode, if the worker refuses the gray channel, motion is no
    longer computed from the whole monitor.
  * With capture in Python (capture in worker off, or a split-GPU setup), an
    idle desktop no longer keeps a CPU core busy.

* **With Windows HDR on and HDR compatibility off, whites are white again.**
  The captured HDR desktop was tone-mapped for the HDR composite - which
  inverts that mapping - but with HDR compatibility off (its default) no
  composite runs, and the mapped picture went to the screen as it was: SDR
  white at about 187 of 255, every white grey next to the desktop around it.
  Shown as SDR, SDR white now stays where it is (about 250) and only what is
  brighter rolls off. With HDR compatibility on nothing changes.

* **In window mode the picture keeps up with a window dragged without
  redrawing** (it moved ten times a second and trailed behind), and Frame
  Generation does no work while the captured window is minimised.

* **A GPU recording of a still picture is smooth again.** Since v2.1.7 the
  capture waits up to a tenth of a second for a new frame, and a window or
  desktop that did not change was recorded at about ten frames a second.
  While a recording runs the wait is a few milliseconds.

## v2.1.9 - 2026-09-29 - Three tracker reports: full speed when hidden, the Windows 10 window mode, and a panel that opened itself

* **Full speed while the window is hidden (#137).** The report: the picture is
  smooth while the program is open and collapses the moment it is minimised or
  sent to the background. Windows 11 does that deliberately - Quality of
  Service classifies a window-owning process by its window's state (High in
  focus, Medium visible, Low minimised or fully occluded) and the timer
  resolution page states that a process whose window is invisible "does not get
  a guaranteed higher resolution than the default system resolution". Neither
  rule is aimed at us; a real-time overlay is simply the case where they are
  wrong, and the documented opt-out is per process:
  `SetProcessInformation(ProcessPowerThrottling)` with the EXECUTION_SPEED bit
  selected and cleared. The call takes a process HANDLE, so the same opt-out
  reaches the worker by pid - both processes own a window and each needs its
  own answer, which is why nothing here required a native change or a rebuild.
  The option is a real switch in the Behaviour section, **on by default**,
  because a user who minimises the program is asking it to keep working; the
  hint states the price (power and heat). `power.status()` reports the mask
  read back from the OS for both processes, so a support bundle can say
  whether the opt-out held rather than only that it was requested.

* **Window mode on Windows 10: the effect disappeared after the switch because
  the overlay was hidden (#139).** The report: fullscreen works, one-window mode
  draws a frame around the window but the effect is gone, and switching back to
  fullscreen brings it back. The frame is the capture border Windows 10 will not
  let us remove (`IsBorderRequired` is a Windows 11 API; the call is already
  guarded). The missing effect was ours. The overlay the worker presents into
  keeps the size of the buffer it was built for, and every follow step hides it
  when that buffer will not fit inside the frame of the window being captured.
  On Windows 10 the two rectangles of ONE window differ: the capture comes back
  at the `GetWindowRect` size, invisible resize border included, while the frame
  is `DWMWA_EXTENDED_FRAME_BOUNDS` without it - a log in the older #30 measured
  1354x853 captured against 1340x846 framed. The comparison was `buffer >
  frame`, which is true for every window on Windows 10, so the overlay was
  hidden on the first follow step and stayed hidden for as long as that window
  was captured. Fullscreen has no follower, which is why it stayed clean. The
  border is now measured in the same step and the buffer is compared against
  the surface it is really presented on; on Windows 11 the measurement is zero
  and the rule is the one it always was. The decision lives in
  `native/present_follow.h` so its harness drives it with #30's own numbers
  instead of reading it as source.

* **The panel no longer opens by itself at launch when the cursor is over the
  taskbar.** The taskbar button is a 1x1 APPWINDOW window, and it used to be
  shown with `SW_SHOW` - which makes it the FOREGROUND window. Windows then
  sends the `WM_NCACTIVATE(WA_ACTIVE)` pair, and that pair is exactly what a
  click on our own taskbar button looks like to the guard that separates a real
  click from a system activation (#96). So the menu opened on its own at launch
  whenever the cursor happened to rest over the taskbar, and stayed shut
  anywhere else - measured on the bench before the fix: 4 of 4 launches with
  the cursor parked over the taskbar, 0 of 4 with it anywhere else. The same
  rule was already spelled out on `set_visible()`, which shows and hides the
  button with `SW_SHOWNA` precisely so the button "comes back without taking
  the focus"; the startup path was the one place it had been missed. It is now
  `SW_SHOWNA` too, and the button still appears - covered by
  `tests/test_taskbar_startup_menu.py`, which parks the cursor over the real
  taskbar, launches with the menu switched off, and checks both that nothing
  opened and that the button is still there and visible.

## v2.1.8 - 2026-09-27 - Both tracker tickets: the screenshot dialog, and a hotkey master switch

Fixes for the two open tickets (#135, #134), the last native item from the
v2.1.6 review, and the bench's own C++ toolchain, which had stopped finding
Visual Studio at all.

* **The screenshot dialog opens while NR is off (#135).** With NR off and the
  panel closed the program hides its window and sleeps, and the only call that
  took messages off that window's queue lived inside the menu branch - so
  nothing pumped it. Windows marked the window Not Responding and stopped
  answering it, and the native Save As dialog is OWNED by that very window, so
  opening it stalled: the reporter's own log opened the dialog at 18:36:23.545
  and recorded the answer at 18:37:22.643, 59193 ms later, the only event in
  between being `overlay menu opened` - which is exactly what started pumping
  again. Measured on the bench, four states of the real program: NR ON with the
  menu open answered a `SendMessageTimeoutW` in 2 ms and the dialog appeared at
  once; NR OFF with the menu closed did not answer at all, `IsHungAppWindow`
  turned True, and no dialog appeared in 100 s. The idle branch pumps the
  window now. This also explains the second half of the report - the panel
  appearing in the saved file "though it did not before": the file is written
  when the dialog is ANSWERED, so it carried the menu as it looked 59 seconds
  later, not as it looked when the screenshot was taken.

* **A master switch for the global hotkeys (#134).** The numpad keys
  NeuralScreen binds belong to it while it runs - that is what RegisterHotKey
  does - and people lose them in games that use the numpad, in Blender, in a
  calculator. The menu could already give the keyboard back for a moment
  (the suspend used while a field waits for a rebind key) but never for good,
  and nothing survived a restart. `hotkeys_enabled` is a real setting: off
  releases every binding, stays off across restarts, and is not undone by a
  resume() - the menu closing after a rebind used to put the keys straight
  back on. The row sits at the top of the hotkeys page, above the individual
  keys it governs, and the menu stays reachable from the tray and the taskbar
  either way. The shipped default is on, so an existing config.json keeps its
  hotkeys.

* **A single command can be taken off the keyboard (#134).** The other half
  of the same request: "keep only the hotkeys you actually need". A key could
  be REPLACED but never removed - an empty or unparsable value was ignored and
  the default stayed, so "I do not want this key" had no answer: a rebound key
  is still a key. A field now clears with Backspace or Delete (`none` in
  config.json), the row reads "none" in words rather than an em dash that
  looks like a broken caption, and the freed combination goes back to every
  other program. An unparsable value still means "leave it alone" and an empty
  string is still not an unbind, so a hand-written config cannot mute a hotkey
  by accident.

* **The picture comes back after a resize with the frame that was just
  presented, not the one from before it.** When the output's size stops
  matching the overlay's, the overlay is hidden - right, it would otherwise
  show a stale-sized picture. It used to be shown again from inside the size
  check itself, which runs BEFORE that frame is presented: for one refresh the
  screen held the frame from before the mismatch, at the new size, with the
  sizes already matching. The decision stays where the sizes are known; the
  show now happens after that frame's Present, where the reveal-on-first-frame
  already lived. The log line is also per episode now instead of one per run,
  so a second resize is not silent when someone is reading the log - which is
  exactly when it matters.

  Found while checking the review's other native item, the two-second waits
  while the overlay is hidden: **it did not reproduce.** With FG on at 4x and
  the overlay really hidden - a captured window shrunk below the picture, the
  one path that hides it while frames keep flowing; a size mismatch does not
  count, PresentModeActive hands that frame to the client and the presenter
  idles - the presenter's own two-second report, taken entirely inside the
  hidden state, showed **184 generated frames shown, no waitable timeout, and
  a mean vblank wait of 0.02 ms** (1.98 ms worst). The compositor keeps
  retiring presents for a hidden flip-model window, so the wait returns at
  once. The item is closed as measured, with no change made against it, and
  the 2000 ms waits are left as they are.

## v2.1.7 - 2026-09-27 - Frame Generation takes only new pictures and is paced by the display

Fixes from the review of v2.1.6 against the tracker (#132, #123).

* **Hotkeys for the NR passes (#126).** Num+ and Num− step the cascade one
  pass up or down, 1 to 4, through the same path as the panel's control -
  no restart - and are rebindable in Settings -> Keys like the rest. The alert
  names the count and, where it differs, the count that really runs: the
  cascade needs Boost, and a frame too small to step its work size aside runs
  one pass. On the numpad on purpose: a global hotkey takes its key from every
  program, and the main row's minus is typed far too often for that.
* **Moving off a vanished monitor works when it was output 0 (#128).** The
  switch compared positional indices: once the monitor list was re-read, the
  live monitor was output 0 - the index the unplugged one had - and the switch
  returned without doing anything, while the log said it was switching and
  the absence was never retried. It compares the monitor's identity now.
* **Frame Generation is really paced by the compositor (#123).** The frame
  latency waitable is a semaphore that every retired present raises, and the
  ordinary present path never waits on it. After a few seconds of NR-only
  presenting it held ~40 counts; the FG presenter consumed one, so none of its
  waits ever blocked, for the whole session, while the log said `paced by the
  compositor (latency 1)`. The presenter now drains every count at start.
  Measured at 4x on a 144 Hz display: the wait before each present went from
  0.00 ms to ~6.4 ms (one vblank), and the generated frames dropped as late
  from ~95 to ~5 per two seconds.
* **A still screen no longer resets NR and FG when it moves again.** Since
  #130 made the stall reset real, any second without a new frame counted as a
  capture pause - and on a desktop that is simply not changing, that is every
  second of it. The first scroll, keystroke or video frame after a pause then
  reset the NR history and showed one frame without generated frames: a hitch
  exactly where motion starts. The history was never stale there: NR and FG
  run on every one of those frames. The reset now needs a capture that really
  missed something - closed, reopened, or a captured window minimised, hidden
  or cloaked - and still fires in those cases.
* **A reopened capture keeps the frame it was opened for.** The #128 fix
  consumed the first frame of every Desktop Duplication session to avoid
  showing its empty surface. It did that unconditionally, and the no-colour
  fallback reopens the capture precisely for that first frame - "a fresh
  session hands over the current content as its first frame". Measured on the
  bench with a still desktop: the reopen's first frame carried the picture,
  the rule consumed it, and all eleven retries that followed saw an empty
  surface, so a screenshot or a recording slot in that window got no pixels.
  Only a genuinely empty first frame is consumed now: `AccumulatedFrames == 0`
  and `LastPresentTime == 0`, which is what the empty surface reports, while a
  frame carrying the desktop reports `AccumulatedFrames >= 1` and a real
  present time.
* **Frame Generation takes only new pictures (#132).** Every frame the loop
  produced went into FG as a new real frame: a pointer-only Desktop
  Duplication update (the same pixels - the cursor is not in the capture) and
  a WGC window that had not redrawn. Each cost a full set of DLSS-G evaluates,
  and its slot, a few ms after the real one, superseded the generated frames
  of the real step - the video stuttered while the mouse moved, the counter
  went up and so did the GPU load. Such a frame is now held: no slot, no
  evaluate, the export and the reply as before. FG spaces its frames by the
  source's own timestamps (DDA `LastPresentTime`, WGC `SystemRelativeTime`),
  not by when the loop got to them.
* **Window capture waits for the window (#132).** WGC's `TryGetNextFrame`
  returns at once, so a window that was not redrawing spun the whole loop on
  the same picture - the "FG does nothing in window mode" of the report. The
  capture now waits for the window's next frame, up to 100 ms, like the
  Desktop Duplication acquire.

## v2.1.5 - 2026-09-25 - the fixes from the tracker, and presentation on its own queue

Patch release on the v2.1 line: the three open reports (#128, #129, #130) plus
the frame generation work of PR #127, which ships here for the first time.

* **A capture pause really resets NR and FG.** The stall detector cleared its own
  flag on the first fresh frame, three hundred lines above the one place that
  reads it, so the reset it announced never happened: NR kept its temporal
  accumulation and FG kept its interpolation slots pointed at a picture that no
  longer existed while the log printed `capture resumed ... history reset` on
  every pause. That is the jerk a window drag showed.
* **A reopened capture never shows its empty first frame.** The first
  `AcquireNextFrame` of a fresh Desktop Duplication session publishes an empty
  surface - the desktop is not composited into it yet - and swizzling it
  overwrote the last good frame with black, which is what a screenshot taken
  right after NR OFF woke the capture was made of. The empty frame is consumed
  instead; Desktop Duplication only, because a WGC frame pool carries frames the
  window has already produced.
* **The pipeline moves off a monitor that is gone.** An unplugged or
  switched-off captured display left the program running against a devicename
  DXGI no longer exposes, while the worker refused the output on every reopen.
  It switches to a live monitor by devicename, not by index, debounced like a
  size change.
* **DRED breadcrumbs can be switched off** with `NS_DRED=0` (they are on by
  default since v2.1.4; Microsoft measures 2-5% on a typical engine). The log
  says which state it is in either way.
* **Presentation has its own queue (PR #127).** With FG 2x and NR on, every real
  frame waited about 13 ms for the next NR pass to finish on the shared queue -
  two frames close together, then a long gap, while `[fg] displayed` still read
  a clean 2x. Waits drop under 1 ms, nothing is dropped, and the source rate
  with FG on goes up 5-19%.

## v2.1.4 - 2026-09-24 - the fixes from PR #124

Patch release on the v2.1 line, built from a contribution (PR #124): fixes across
the worker, the converter, the panel and the log, including the frame generation
pacing that silently never ran.

* **Frame generation is paced by the compositor now.** FG was to wait for the
  compositor to release the previous back buffer instead of presenting on
  wall-clock deadlines, and it never did: `SetMaximumFrameLatency(1)` and
  `GetFrameLatencyWaitableObject` work only on a swap chain created with
  `DXGI_SWAP_CHAIN_FLAG_FRAME_LATENCY_WAITABLE_OBJECT`, and the chain was created
  without it. v2.1.2 only made the log admit the fallback. The chain starts at
  latency 3 and only an FG session takes it to 1, restoring 3 when it stops - a
  chain created with the flag starts at 1, which is what flickered the ordinary
  NR path in the R13 attempt. `ResizeBuffers` passes the chain's own flags back,
  so the flag survives an HDR switch. Confirmed live:
  `[fg] the presenter is paced by the compositor (latency 1)`.
* **Six defects in the native worker:** the BYO runtime was loaded through a
  pointer into a buffer that was dead by the `LoadLibraryW` call; the DRED
  settings were asked of the created device, which no Windows answers, so every
  log said `unavailable (0x80004002)` and a removed device had nothing behind its
  reason code (they come from `D3D12GetDebugInterface` and were moved before
  `D3D12CreateDevice`); in window mode an oversized picture hung past the window's
  right and bottom edges because the clamp moved it left and straight back; a size
  mismatch left the last frame frozen on screen and in window mode came back every
  frame; the HDR composite on a "Landscape (flipped)" display (#47) read the
  native frame the right way up while the capture shader turned it over, giving an
  upside-down double image; and the DLL signature gate built its chain at the
  current time and without the signature's own certificates, so a timestamped
  NVIDIA DLL whose certificate has expired was refused as not NVIDIA-signed.
* **A converted photo or video is seen the way its source is.** EXIF orientation
  is applied and removed from the EXIF written back; a photo's colour profile
  carries into the output; a 16-bit grey image is scaled instead of clipped
  (Pillow's `I;16` to RGBA took everything above 255 as white - a ramp measured
  99.6% white); a video's display matrix is read from the first frame and set on
  the output stream. A portrait still that fits on its side now goes through
  turned and comes back upright instead of failing as "the network stopped" (the
  worker's 7680x4320 limit is a landscape shape and the converter never asked);
  a file too large both ways is refused up front, in all twelve languages.
* **The panel:** the window recreated when a fullscreen game changes the display
  mode kept a third copy of the theme restore, accepted light and dark only, and
  reverted contrast on the next save; it also took the panel height from the dying
  window and never gave it back. The show-yourself message was `0x8000 + 0x4E53`,
  called `WM_APP` - which ends at `0xBFFF`, while `0xC000-0xFFFF` is where
  `RegisterWindowMessage` hands numbers to every program in the session; it is a
  registered name now.
* **The log stops growing without a bound.** A user's log reached 7.8 MB and
  81,768 lines in two and a half days, almost all of it heartbeat lines; a start
  now moves a log past 8 MB to `NeuralScreen.log.1`.
* New or extended tests, each failing on v2.1.3: `test_convert_orientation`,
  `test_log_rotation`, `test_single_instance_message`, `test_theme_rebuild`,
  `test_worker_zorder_and_safety`.

## v2.1.3 - 2026-09-23 - conversion speed, a contrast theme, and mini mode

Patch release on the v2.1 line, built from a contribution (PR #122): the
converter stops copying every frame through a pipe, the panel gets a third theme
and a shortened mode, and the theme list gets one home after it was found split
across three.

* **Conversion is faster.** A frame used to be written into the worker's pipe and
  read back out of it - the same 8 MB at 1080p, twice per frame. Both directions
  use the shared mapping the live pipeline already uses, and the motion field
  travels at ~320x180 and is upscaled on the GPU instead of being built at the
  working size on the CPU. Measured here on a real recording (1478 frames of
  640x360, the product's own converter): 13.2 s serial against 9.0 s overlapped at
  the default work scale, 14.3 s against 10.0 s at 1:1 - 1.4x, identical output
  bytes. Per-stage CPU costs at 1080p from the contribution: 13.7 ms of guides to
  6.0, 2.7 ms of `tobytes` and 6.2 ms of pipe to 0.9 ms of copy, returned pixels
  0.4 ms instead of about 3.
* **The three conversion stages overlap** (decode / network / encode on their own
  threads, exact order and interleaving preserved). Each channel is asked for
  separately and each can be refused - a worker that does not take one keeps the
  old path for it and says so. `NS_CONVERT_PIPELINE=0` restores the serial loop.
* **A contrast theme**: near-black background, phosphor-green text, one
  monospaced face for every role.
* **Fixed: the contrast theme reverted to light after a restart or monitor
  switch.** The theme list existed in three places and only two were updated, so
  the third theme was accepted, applied, and then silently dropped by both restore
  paths. The list now lives once (`settings_io.THEME_NAMES`) and the validator,
  both restores, the control and the action handler read it;
  `test_theme_rebuild` checks every offered theme against the real rebuild and
  fails if a whitelist is duplicated again.
* **Mini mode**: an icon in the panel header cuts it down to the rows you use,
  and a second icon chooses which rows those are. The choice is saved; the
  choosing state is not. 1213 px of panel becomes 372 at the default scale.
* The converter's colour tags land on the frame as well as the stream. New tests
  cover the overlapped pipeline (same frames, order and timestamps as the serial
  loop; a cancel stops all three stages) and the mini-mode header.

## v2.1.2 - 2026-09-22 - fixes from a code audit, and a tidier root

Patch release on the v2.1 line: the defects a full audit of the code found, and
the program's modules moved out of the repository root.

* **The settings panel no longer goes under the picture (#96).** In window mode
  the worker re-inserted its picture at the top on every move or resize of the
  captured window; in fullscreen a periodic check read helper windows (a 1x1 DWM
  helper, hidden Start/Search hosts, the NVIDIA overlay) as covering it and raised
  it over the panel - the flicker. A follow step only moves the picture now, the
  check skips hidden, cloaked, tiny and off-picture windows, and the panel goes up
  first with the picture placed directly under it. The client guard does the same
  in one step; `[z]` lines carry `cloaked=` and the picture's own rectangle.
* **Recorded sound no longer turns into a buzz on loud moments.** The limiter bent
  every sample of a 10 ms block once one was loud (quiet sound came out as a
  square wave at 0.8); only the peaks are bent now. A playback-device change
  reopens the capture instead of silence to the end.
* **A dying worker no longer freezes the window for up to a minute**; a hotkey
  during a frame no longer loses that frame's answer; a failed window probe puts
  the capture back as it was instead of showing a desktop corner.
* **HDR:** Frame Generation at a refused 3x/4x no longer retries forever on the
  HDR path; an HDR10 recording that leaves HDR is closed, not frozen.
* **Updating over an old folder no longer runs old code**: the first start of a
  release drops the compiled cache (a same-size module kept the old `.pyc` -
  2.1.1 over 2.1.0 still said 2.1.0).
* Smaller: `"gpu": null` broke every settings save; BOM/`"false"`-string configs
  broke the launch; a second start now brings the running copy's menu up;
  autostart set from another folder reads as off; a failed menu action is reported
  instead of closing the program; showing the panel no longer takes the keyboard;
  a hotkey onto a taken key is refused; converted videos carry their colour range
  and HDR sources are refused; the compatibility dialog speaks the user's language;
  support packages drop other programs' window titles.
* **Modules moved to `app/`** (the root lists 23 entries instead of 52). Unpacking
  over an old folder leaves the old root `*.py` files; nothing loads them.

## v2.1.1 - 2026-09-22 - cheaper and smarter

Patch release on the v2.1.0 line: the file-conversion defect reported against
the published build, and three items from a contributor on top of it.

* **Conversion no longer fails on a variable-rate file.** The Media tab's
  converter wrote every frame on a grid derived from the file's *average* rate
  (`1 / average_rate`). A recording whose rate varies - and a GPU recording does,
  because the recorder drops the frames the pipeline never handed over - has
  frames arriving faster than that grid, so two neighbours landed on one tick
  and the multiplexer refused the file with `Invalid argument: ... returned 22`
  (libav's `non monotonically increasing dts to muxer`). Frames are now written
  on the source's own time base; the same grid goes to the muxer, so the
  timescale stays inside the 32-bit range (`60000`, 19.9 hours) instead of the
  `19 620 000` the old shape produced (6 minutes). `MAX_TIMESCALE` keeps a
  nanosecond container from pushing the mp4 past that limit. On the bench two of
  three recordings failed before the fix and convert cleanly after it.
* **A refused write no longer reads as a stopped worker.** A muxing failure at
  the process stage was reported with the sentence meant for a dead worker
  ("the network stopped - try again"), which sent the reader after the wrong
  cause. A libav error at that stage is now its own case - "the file could not
  be written" - in all twelve languages.
* **From a contributor (PR #119), on top of the above:** a captured window
  changing size no longer ends the worker with HDR on; with NVOFA the worker
  decides the scene cut itself, so a frame no longer pays a capture round trip
  (+3.3% full-resolution NR, +3.6% with two Boost passes, measured on an RTX
  5080 at 2560x1440); and an HDR session's GPU recording now holds what the
  display gets - 10-bit, BT.2020, PQ, tagged - where it used to be the SDR
  proxy. `NS_WORKER_SCENE=0` and `NS_GREC_HDR=0` turn the last two off.

## v2.1.0 - 2026-09-22 - cheaper and smarter

* **The second pass can run its own settings.** The cascade repeated the same
  neural pass up to four times since v2.0.0, so pass 2 was pass 1 again: it cost
  about a third of the frame rate and looked the same. Passes 2 and later now
  take their own set of parameters - style and the four values - from a switch
  that appears under **NR passes** once there are two of them, and the set
  survives a launch, a resize and a restart. Pass 1 keeps following the profile,
  so turning it on does not change the picture you already tuned.
* **Recording moved to the GPU.** **Num0** encodes on the graphics card (NVENC)
  at 60 fps with sound by default, so a recording no longer costs frame rate. It
  records the frame the worker presents and nothing else: the panel, its menu and
  the desktop around the picture stay out of the file, and a recording an error
  cuts short is still kept. **Record on the GPU** off returns to the older CPU
  path, which is also the automatic fallback.
* **Files can be converted.** A new **Media** tab converts a video or an image
  with the settings the sliders are set to - the same look the desktop gets - as
  a queue with progress, stop and retry, drag and drop, and a choice of output
  folder, codec, quality and audio. It runs without the overlay being on.
* **The client is off the critical path.** The worker used to answer a frame only
  after presenting it; it now answers as soon as the frame is queued, and the
  client's own work runs in that window. Measured by a contributor off-screen at
  2560x1440 on an RTX 5080, with the client's per-frame work simulated at the
  1.6 ms a user's log shows: 147.7 FPS against 119.6 lockstep, +23.6%, picture
  unchanged (Boost workloads gain 3.9%, already at the refresh).
  `NS_EARLY_REPLY=0` turns it off.
* **Fixes:** the Boost switch no longer drops presses while an apply is queued; a
  monitor change in window mode clears the window being captured instead of
  keeping a stale handle, and the pointer stopped jumping; GPU recordings no
  longer run ahead of their own sound (each frame lasts until the next); a
  parameter change at 1:1 with Boost on no longer rebuilds a working feature;
  passes no longer size the work with Boost off, and the worker names Boost
  instead of saying something is missing.

## v2.0.2 - 2026-09-21 - the fixes from the tracker

The reported defects, and one of them was a bug in an earlier fix. Verified on
the bench with the full suite before tagging.

* **The v2.0.0 reveal fix never ran on an ordinary launch.** The panel publishes
  its handle so the worker can reveal the picture *under* it rather than over it,
  but the worker was started before the panel existed: it read the variable once,
  found nothing, and kept that answer forever. A reporter's log still showed the
  old fallback line on a build that was supposed to have fixed it. The panel is
  built first now.
* **The taskbar, our Save As dialog and our own windows were all read as
  "something covered us".** The worker re-checks the topmost slot every 300
  frames and raises the picture when anything has taken it. The shell's taskbar
  is a topmost window, so interacting with it lifted the picture over the panel;
  our own dialog did the same for as long as it was open, which is the flicker
  filmed while taking a screenshot. Both sides now decide by **process** - the
  shell's windows and our own program's windows are not occlusion - and a real
  application window still lifts the picture, which is what this exists for.
* **A saved PNG carried a transparent hole where the panel was.** Measured on a
  reporter's screenshot: alpha 255 over the desktop, alpha 0 over the whole panel
  rectangle, with the panel's colours still in the file. The PNG path kept the
  frame's fourth byte as the alpha channel and JPEG dropped it, which is why it
  showed only in the default format. A capture is opaque by construction, so that
  byte is forced opaque.
* **Changing monitor in window mode kept the old window target.** A monitor
  switch rebuilds the pipeline for the new monitor's size but never cleared the
  handle of the captured window - only leaving window mode did. The stale handle
  was still found alive, and the worker was asked to capture that window on top
  of a pipeline rebuilt for the whole monitor: two sources in one session.
* **Two fixes from a contributor.** HDR tone-mapping followed the presentation
  setting instead of the captured monitor's own HDR state, so the two could
  disagree; and the worker now waits for the GPU before freeing a neural pass,
  refusing safely rather than releasing from under it, and releases the passes it
  is not using - which a reporter's log had been showing all along
  (`asked=4 have=3 live=3`, then down and never released).
* **The taskbar test was passing two of its steps vacuously.** It gave a foreign
  window the foreground with a call Windows refuses, so the steps never reached
  the bug they existed for, and an assertion blamed the test's own setup for a
  message the click path had correctly sent. One message branch had no negative
  coverage at all. Five mutations now caught.

## v2.0.1 - 2026-09-20 - the log tells the truth, and the guard can see

A maintenance release. **Nothing changes on screen** - every control, default and
measurement from v2.0.0 is unchanged. What changes is the program's ability to
explain itself when something goes wrong, because an open report spent a week
unanswerable while three diagnostic packages looked perfectly healthy.

* **The guard that keeps the panel above the picture was walking sixteen windows
  and giving up.** Helper windows (invisible input-method entries, a 1x1 system
  thumbnail helper, an off-screen accessibility window) are skipped but still
  spend steps, so on a busy desktop the walk ended with "found nothing" and did
  nothing at all. In a reporter's two packages the healthy verdict appears **zero**
  times, against 63 on a machine where the same code works.
* **"I gave up looking" and "nothing is above us" were printed as the same
  line** - two different facts, and printing them as one is why those packages
  read as healthy.
* **The guard said nothing about our own panel.** Every decision now records the
  panel's position, its visibility, whether it is still topmost, and its actual
  transparency read back from the window rather than remembered.
* **Nine kinds of worker diagnostic never reached the log.** The failure report
  itself, every shader and pipeline setup failure, and the line that says Boost
  quietly fell back to full resolution were written and then dropped one layer
  later. A line that is never printed looks exactly like a line that was never
  reached.
* **The NR cascade was told to the first worker and no other.** The pass count
  travels with a resize command and was sent once at startup, so every restart
  brought the worker back at one pass while the panel still showed four.
* **A number in the v2.0.0 notes was wrong.** "About 440 MB" per extra pass was
  an estimate printed as a measurement; measured at a 2560x1440 work size it is
  about 640 MB per pass, and it scales with the work resolution.
* **The diagnostic package had stopped saying how the program was configured** -
  it could not say whether the cascade was running, where the counter was, or how
  large the panel had been made.

## v2.0.0 - 2026-09-20 - Multipass, the redesign, and the tray

* **Boost** - the network runs at a reduced resolution and only its DELTA is
  composed onto the native frame, so text and edges keep full resolution. On by
  default.
* **DLSS 4.5 Frame Generation, x2 / x3 / x4**, with or without the neural pass.
  Ada or newer.
* **Whole screen or one window**, picked from a list or by pointing at it and
  pressing Num5; the overlay follows it as it moves and resizes.
* **NR passes** - the network over the same frame more than once, 1 to 4, an
  experiment rather than a finished feature. Every pass gets its OWN network
  instance: calling one instance twice inside a frame hands it two evaluations
  with no motion between them, which is a lie to its temporal history. Off by
  default and only available with Boost on.
* **The panel fits the screen** and carries a scale control, 80 to 130%. At 100%
  the main page does not fit a 1080p desktop.
* **Minimise to tray and Close to tray** (#93), both off by default; neither
  stops the neural pass.
* **The frame rate on screen** (#109) with the panel closed, in the corner you
  choose - `NR 55.1`, or `FG 167 (55.1)`.
* **Fixes:** the panel no longer flashes while the picture is on (#107) - it was
  never a foreign window but our own, shown above the panel instead of below it;
  eight NGX parameters the runtime does not have were being set on every create;
  the English interface said a Russian word in the theme label.

## v1.17.0 - 2026-09-20 - the panel redesign, and fixes from the tracker

This is a **pre-release for testing**: the panel was rebuilt to the new mockup and
this package carries the first reports from the tracker. It is published as a
normal release so everyone can reach it - read the known limits below before
filing anything.

* **The panel was redesigned** to the approved mockup, section by section:
  numbered sections, the value plate on every slider, the tabbed Settings page,
  the ACTIONS icons, and a tighter vertical rhythm. The Frame Generation row is
  now one control - `off / x2 / x3 / x4` - with no separate switch, so one click
  on a multiplier turns it on.
* **The generated rate is shown with the rate it is built on** (#109): the line
  reads `FG 178 (60.0)` instead of two separate readings. Two numbers side by
  side stated both values but not their relation, and the relation is what the
  pair means. With Frame Generation running alone the single `FG` reading is
  unchanged.
* **Frame limit now says what it counts** (#109): it caps the SOURCE frames the
  network processes, and Frame Generation rides on top of them - which is why a
  60 cap and a 170 counter can both be true. The row carries a hint saying so, in
  all twelve languages.
* **Hints wrap and fit their row.** The drop-down rendered a hint as one
  unclipped line while the slider already wrapped, so a real sentence was cut at
  the panel edge. Both now share one implementation, and the layout reserves the
  height the wrapped text needs.
* **The first click on the taskbar button opens the menu again.** A click on a
  not-yet-active button arrives as `WM_NCACTIVATE(1)`, which the guard rejected as
  a duplicate; only the second click worked, by a different route. The previous
  window's state now separates a real click (its window is alive) from the
  fallback activation that follows another window being minimised, so the #96
  behaviour is kept while the first click works.
* **The menu no longer disappears with the captured window** (one-window mode):
  the layer was hidden whenever the source window was minimised, taking the menu
  with it - the taskbar button then looked dead. The picture correctly stops when
  there is nothing to capture; the panel stays visible.
* **The menu in a saved screenshot is placed where it is on screen** (#107): in
  one-window mode the panel was composited at the frame's coordinates and landed
  up to 524 px away from where the user sees it.
* **Clicking Screenshot crashed the program** - `_Pipeline` uses `__slots__` and
  two diagnostic fields were never declared. A test now presses every control on
  every page (94 actions) against the real pipeline, so an undeclared field fails
  in the suite instead of on the user's first click.

Known limits in this build (unchanged): the network works up to 2560x1440, the
overlay is invisible to external recorders, and exclusive-fullscreen games are not
covered - see the README section "Limits".

---

## v1.16.1 - 2026-09-19 - the status line told the truth about Frame Generation

* Frame Generation was reported as "not processing" while Neural Rendering was
  off, although it was running (#107, reported against v1.16.0 the same day).
  The status line answered from the NR switch before looking at Frame
  Generation - correct before v1.16.0, a lie after the bypass path started
  presenting. The line now follows the work: NR off with a reported rate says
  "frame generation", NR off before the first rate says "frame generation
  starting", and NR on keeps "processing". Both strings ship in all twelve
  languages.
* The capture log names every output of the adapter it uses, with the device
  name and rectangle, and reports the total. A diagnostic package lists display
  drivers from the registry, including display-only adapters (Parsec, Cherry)
  that DXGI never reports as adapters, so "is it capturing the real monitor or
  the virtual one?" had no answer. The lines are written before the output is
  matched, so they survive a mismatched NS_OUTPUT.
* The taskbar check no longer flakes: the cursor is re-parked immediately
  before each synthetic activation instead of once at startup, and a park that
  cannot be made is reported. Mutations exposed two steps that passed without
  checking what they named - one relied on a foreground change Windows can
  refuse, the other had no coverage of the cursor condition at all.

## v1.16.0 - 2026-09-19 - Frame Generation with NR off, and reports that explain themselves

* Frame Generation now runs while Neural Rendering is off (#104). The bypass
  present path stopped the presenter on every frame and the reset flag kept it
  non-interpolating even when left running, so the switch reached the worker,
  the frame counter climbed, and nothing generated. Measured with NR off from
  the first frame: `Init_Ext`, `2x enabled at`, 118.7 FPS real + generated.
* `--test` reported 0/300 and now reports 300/300: it created the feature
  through the DLSSNR runtime and evaluated it through the NGX core, which knows
  nothing about that handle.
* Half of every log had no timestamp (499 of 916 lines in one package, 76 of
  153 in another). Every line carries a time now, the header carries the date,
  and the menu close is logged with how long the menu had been open.
* The diagnostic package says how the program was configured: a settings
  section built from the live config, allow-listed to product keys, with
  hotkeys, directories and presets dropped.
* The first hotkey press in a game did nothing: the polling fallback took its
  baseline from the first sample, so an early press read as "already down".
* The status line stays one row; the resolution, the skip count and the frame
  counter left it by decision.
* The release procedure (`RELEASING.md`) and the release history
  (`CHANGELOG.md`) are written down instead of living in memory.

## v1.15.1 - 2026-09-19 - the readings on the status line, and the guard that tells us why

* The status line dropped the numbers people watch: it laid values out from the
  right edge in reverse and silently discarded whatever ran out of room, so on 4K
  with a real card name it showed `SKIP 0 3840x2160` and no rates at all. One row
  again - state and card left, readings anchored right, FG at the edge. The
  resolution, the skip count and the frame counter left the line by decision.
* The first hotkey press in a game did nothing: the polling fallback took its
  baseline on the poller's first sample, so the first press after launch was
  recorded as "already down" and swallowed (6 of 6 runs). The baseline now comes
  from registration.
* The z-order guard now logs what it saw - class, title, pid and rect of the
  window that took the top, and the branch it took (`hud-on-top`,
  `picture-above-hud`, `foreign-above-hud`, `nothing-covers`), including the
  healthy case - which is what made #96 and #89 undiagnosable.
* Build scripts no longer hardcode the author's `BuildTools` path: six scripts
  resolve the toolchain through `vswhere`, in one shared `vcvars.bat` that checks
  its result (contributed by @HyperRamzey, with a clang-cl path as well).
* Suite: 173 checks, 170 PASS / 0 FAIL / 3 SKIP, GUI-E2E 9/9.

## v1.15.0 - 2026-09-18 - washed-out colours, the taskbar menu, and the FG readout

* Washed-out colours with HDR off on a 10-bit display (#99): the FP16 capture
  format was treated as scRGB by itself, so white landed at 187 instead of 255.
  `isFloat` and `hdr` are separate facts now; only a real scRGB capture is
  tone-mapped. Covered on WARP: 255 with the flag, ~187 without.
* The panel showed a Frame Generation multiplier that was not running (#100):
  after a refused x4 the worker steps down to x2 while the buttons kept showing
  the pick. The panel now reports the step the presenter really runs.
* A minimised foreign window opened the menu (#96): Windows activates the 1x1
  taskbar window as a fallback and it arrives as the same message as a click.
  Now distinguished by the state before the event plus the minimised window.
* The z-order guard saw no window on a left-hand monitor (#89): the rect was
  tested against the primary screen, where a window on a left monitor has a
  negative x. The rule intersects the virtual desktop now.
* Every NGX failure code is named in the log - `0xBAD0000C` is `FAIL_OutOfDate`
  (an older driver), which had been reaching user logs as `?`.
* Suite: 171 checks, 168 PASS / 0 FAIL / 3 SKIP, GUI-E2E 9/9.

## v1.14.0 - 2026-09-17 - sixteen post-release audit fixes

* The capture stops rebuilding itself on a high-colour display: a v1.13.1 log
  showed the duplicated desktop alternating FP16/BGRA8 1955 times in 133 s
  (`grab 43.0ms` against `NR 18.3 fps`). A high-colour display now asks for FP16
  first through `DuplicateOutput1`, BGRA8 stays the fallback, and it drops to
  `grab 3.0ms` / `NR 48.6 fps`.
* A failed resize no longer answers "ok": the handler fell through into
  `CreateFeature` and wrote a success reply after a failure, leaving a half-built
  state the client believed was applied.
* The mode-switch veil always comes down now (#89, #96 - reported twice as "the
  window is invisible").
* Four ways the panel fought the user: the keyboard stayed captured after the
  menu closed, a drag outlived the menu, the drop-down opened out of the
  viewport, and a click on a hint hit the row.
* A hand-edited `config.json` no longer aborts the launch (26 hostile values are
  normalised or refused by field name); the runtime is hashed once (184 ms -> 1 ms);
  the save dialog opens in an existing folder; eleven strings were translated.
* Suite: 166 checks, 163 PASS / 0 FAIL / 3 SKIP, GUI-E2E 9/9. This release also
  found checks that could not fail - four tests that always printed SKIP.

## v1.13.1 - 2026-09-16 - the hotfix release

* Four fixes reported in the first hours of v1.13.0.
* The menu no longer hides behind the picture (#94): a restarted worker re-asserts
  its picture window as topmost, and the recovery path used `SWP_NOZORDER`, a flag
  that made `SetWindowPos` ignore the position. The menu is re-asserted as topmost
  itself, every frame while it is open.
* Only the control reacts, not the whole row: every toggle, slider and choice row
  reacted anywhere on the panel, and clicking a caption restarted the worker. Each
  row carries an explicit hit zone that is the control.
* The focus outline follows the keyboard rather than the mouse, and the taskbar
  activation path stopped hiding an already visible menu.
* NVOFA became the default motion backend.

## v1.13.0 - 2026-09-16 - a real OFF, the compatibility gate, and a verified release

* A real OFF: with NR and FG off and nothing recording, capture and presentation
  close, no frames are sent and the overlay hides (#75). NR back on rearms every
  channel from a fresh frame; FG, recording and screenshots keep their own bypass.
* A frame cap on the active loop: 30 / 60 / custom 15-240 / unlimited, on a
  monotonic deadline, and the panel stops conflating the NR rate, the FG presenter
  cadence and the skip counter (#89).
* The compatibility gate runs first: one short-lived worker, three 640x360 frames,
  and only a clean pass unlocks the overlay - a failure blocks it with a named
  reason and a timed quarantine instead of a restart loop (#71, #83). The verdict
  is cached per exact configuration; `unknown`, `not run` and an unexpected SKIP
  can never become a PASS.
* A verified release: `config.default.json` is tracked and the user's
  `config.json` is generated and ignored; `build_release_zip.py` refuses a dirty
  tree, a wrong tag or a mismatched manifest and builds byte-identical archives
  per tag; the set ships `SHA256SUMS`, `runtime-manifest.json` and
  `THIRD-PARTY-NOTICES.md`. The manifest pinned 1352 payload and 1299 runtime files.
* Full keyboard navigation, and the README labels each GPU family validated /
  reported / unverified / known failure. 148 checks on HEAD, 0 FAIL, GUI-E2E 9/9.

## v1.12.0 - 2026-09-15 - capture reliability and safe recovery

* SDR flicker on 10-bit displays fixed: Desktop Duplication requests one stable
  BGRA8 format instead of alternating BGRA8/FP16 (#86).
* Multi-GPU monitor routing fixed: every menu display resolves to its real
  `(adapter, output)` pair, and an incompatible pair falls back to Python frame
  transfer instead of silently showing adapter output 0 (#88).
* Bounded capture recovery: three consecutive failures, then a 30-second cooldown
  instead of an endless restart loop. WGC initialises WinRT before its support
  query and keeps resize recovery on the frame-pool path.
* GPU failures are distinct diagnostic stages with one terminal state, and a
  resize honours fence failures instead of freeing feature resources after a
  failed wait.
* Recording and screenshots: 192 kHz WASAPI is resampled to AAC 48 kHz, AAC is
  probed before the MP4 is opened, and screenshots freeze the frame before Save As
  so the dialog cannot appear in the image (#89). Taskbar activation is idempotent
  (#87).
* 132 of 133 checks passed; the remaining one exposed a WGC resize race that was
  fixed before the tag.

## v1.11.1 - 2026-09-15 - the shipped config default, and the multiplier unlocks

* The 1.11.0 archive carried a maintainer's working config: `frame_multiplier` 3
  (an RTX 40 card caps at x2), SR-merge leftovers in the sliders and two dead
  `dlss_sr` keys. The defaults are the product defaults again - multiplier 2, FG
  off, CPU motion, Natural's four sliders.
* A new static check ("config: the shipped defaults") compares the committed
  `config.default.json` against the profile-derived defaults, which the
  zip-vs-HEAD comparison can never catch; it flagged the old values immediately.
* The multiplier group is selectable while Frame Generation is off, so the way out
  of a refused multiplier is no longer locked behind the switch that caused it.
* 128 checks green on the release commit.

## v1.11.0 - 2026-09-15 - FG truth, wider sliders

* The blink over the picture in window mode is fixed: the z-order guard took the
  top of the stack at face value and re-asserted the pair for invisible IME /
  MSCTFIME / ForegroundStaging / DWM helper windows - 10,339 pairs in one session.
  The guard walks to the first visible window that can cover the layer, and
  "foreign" is decided by HWND, not class.
* The Frame Generation switch follows reality: when FG cannot start it now flips
  back off with an 8-second notice instead of staying ON with nothing
  interpolating; Num7 toggles FG from anywhere.
* Sliders reach further (tone and structure to 2.0, skin structure to 2.5 -
  measured on the live runtime first); intensity stays 1.0 because the runtime
  clamps above it. A built-in profile moves the four sliders only.
* The drag stutter ends: the `dragging` gate now covers the title-bar drag, edge
  scale and grip resize.

## v1.10.0 - 2026-09-14 - DLSS 4.5 FG, NVOFA, BYO libraries, perf series

* DLSS 4.5 Frame Generation after neural rendering, opt-in, with an x2/x3/x4
  multiplier. The depth is flat and the motion is estimated, so UI and text can
  distort - stated openly as the cost of the approach.
* NVIDIA Optical Flow (NVOFA) as an opt-in motion backend for the driver's
  optical flow instead of CPU DIS, with an automatic fallback to CPU and one alert.
* The HUD pairs both rates ("network / presenter") instead of showing the
  network-only counter that read as broken with FG on.
* The library auto-updater is removed and the app no longer downloads anything:
  both NVIDIA runtimes ship in the archive, and a `native/libraries/` DLL wins over
  the bundled copy. DLSS Super Resolution from the 1.9.x cycle was removed after
  the live test (it reads as smear on desktop captures).
* Performance: the recorded-frame ring ends a 33 MB-per-frame allocation
  (13.2 -> 2.4 ms), bypass no longer computes an unread motion field (2.9 ms/frame).

## v1.9.0 - 2026-09-14 - models switch, the overlay reworked, a week of window-mode bugs closed

* Model switch: three networks with three different outputs rather than three
  strengths. Measured fine detail against the untouched frame on a desktop
  capture: Default +18.7%, Natural -11.4%, Cinematic -23.4%.
* The menu redesigned: four tabs instead of one long scroll, one status line
  instead of a readings grid, every parameter slider shows its scale, and About
  says what an issue report has to carry to be solvable.
* The parameter ranges are measured, not guessed: intensity used to be offered to
  2.5 while the DLL clamps at 1.0, and a test now sweeps every knob and names the
  dead ones.
* The one-window overlay, five bugs closed as one series: a magenta fallback
  screen, the desktop cut-out dropped twice a second while the menu was open, a
  stale surround, a trail of copies when the window shrank, and a shimmer whenever
  the program's UI sat above the picture.
* A colour-format flip no longer tears the capture down; an Infinity in a preset
  is reported as a broken file instead of being silently clamped.

## v1.8.2 - 2026-09-13 - Boost on by default, and three silent failures named

* Boost is on out of the box: on a 5070 Ti at 4K it is 45.7 -> 72.6 frames for a
  picture indistinguishable at 1:1, because the network's result is composited
  onto the native frame.
* Every profile's Local tone is half a point lower - the slider was lifting
  shadows more than the picture wanted, most of all on dark scenes.
* The overlay invisible with the effect firing for a second at a time (#61):
  something else in the worker's process printed into its stdout, which carries
  the binary protocol. The protocol now has a private handle.
* A black screen at 10-bit colour depth (#58): one FP16 frame was read as "HDR"
  off the format alone, putting the picture on the scRGB path with HDR off.
* Also: alerts appear at the top of the screen instead of inside the captured
  window, "the window cannot be captured" stopped repeating every half second, a
  180° flipped display is turned back over (#47), the Spout bridge builds its
  device on the card the worker runs on, and a window resize no longer leaves a
  hole in a recording.
* A window resize now reconfigures the worker instead of replacing it:
  1.845 s -> 0.112 s, with no veil, because nothing dies.

## v1.8.1 - 2026-09-13 - faster pipeline + fixes

* The screen flickered on every cursor move (#58): 1.8.0 put a `SetColorSpace1`
  call into the ordinary present path; it runs only with HDR compatibility on now.
  Reported on an ASUS ROG STRIX G16 with an external monitor.
* The processed picture could sit behind a focused fullscreen game while the HUD
  stayed on top - the picture is raised first, the HUD last (thanks to @tzachbon, #52).
* Dead links in `TECHNICAL.md` / `TECHNICAL.ru.md` and the HDR links in both
  READMEs, broken in every unpacked copy, fixed.
* Moving a slider no longer rebuilds the neural feature (113-148 ms of frozen
  picture, measured at 0 ms after), a mode switch is half as long (1.845 -> 0.972 s),
  and there is one fence wait per frame instead of three.

## v1.8.0 - 2026-09-12 - experimental HDR support, off until you turn it on

* HDR compatibility, off by default (Num2 -> the gear -> CAPTURE): the screen is
  duplicated in FP16 scRGB, the network is handed a tone-mapped SDR copy, and its
  edit returns as a bounded linear residual, so highlights above white survive.
* This is not native HDR inference - the runtime still works on an SDR proxy, and
  a zero edit reproduces the original exactly. The arithmetic and limits are in
  `docs/HDR.md`.
* Contributed by @saffd96 (#36): capture, shaders, presentation and a WARP test
  for all of it.
* Known limits: screenshots, recording and Spout output stay tone-mapped 8-bit
  SDR, and Windows HDR has to be on as well.

## v1.7.1 - 2026-09-12 - hotfix: a video-memory leak and two ways the picture could never appear

* The card filled up: every slider step released the neural feature against the
  NGX core while it had been created by `nvngx_dlssnr.dll`, leaking video memory -
  on a 16 GB card, half a minute of sliding, and it took other applications down
  with it (#48). The worker now reports its own video memory after every build.
* The network and the capture could run on different cards: `NS_GPU` is a DXGI
  adapter index, the neural side treated it as a wish and the capture took it
  literally - on a hybrid laptop adapter 0 is the iGPU, and nothing appeared.
* A second monitor could stay blank until you switched away and back: one
  auto-reset event served every GPU wait, so a wait could be woken by somebody
  else's completion.
* Also: a hand-edited config can no longer take the program down silently, a crash
  brings its traceback to the log, HDR is reported once per session, a card that
  cannot run the pass is marked in the picker, autostart's registry entry is
  documented, and the README came down to 158 lines.

## v1.7.0 - 2026-09-12 - Boost: up to 80% more frames for the same picture

* Boost: the network is capped at 2560x1440 and until now it ran at the full frame
  size wherever the slider stood - the output was bit-identical at every position.
  The network now runs at the work resolution and its edit is composited onto the
  untouched native frame, so text and edges stay 1:1. Off by default at that point.
* The neural calls move to a module of their own, `nvngx.dll_ns-forwarder.dll`:
  `nvngx_dlssnr.dll` decides by looking at the module a call returns to, so the
  worker no longer has to *be* an executable named `nvngx.dll`.
* Fixed: saved presets never reached the disk; a card that cannot run the pass
  stopped being retried forever; a revive brought the long warm-up back; a manual
  revive left the automatic one armed; the GPU alert only fired with the menu open.
* The test suite grew from 67 checks to 92.

## v1.6.1 - 2026-09-11 - the second monitor fixes, idle screens stop burning the GPU

* The overlay was created at (0, 0) of the virtual desktop while the capture
  followed the chosen monitor, so on a second monitor the program ran, logged
  frames, answered hotkeys and showed nothing (#28, #33, #35).
* An idle screen no longer costs a card: when nothing changed, the network ran
  over the previous frame anyway; it now answers "nothing changed" and the menu
  shows `idle`.
* Fixed: the Spout2 switch looked off while working; a GPU that cannot run the
  network could lock the program on it; the resolution slider promised 3840x2160
  at its top step though the network caps at 2560x1440; the theme fell back to
  light after a monitor switch; minimised windows are listed again.
* Typography split by role (IBM Plex Sans for language, IBM Plex Mono for
  readings), with the faces shipped so it looks the same on any Windows install.

## v1.6.0 - 2026-09-11 - Win 10 confirmed, OBS output, 1 or 2 GPU picker, Save As fixed

* OBS output as a switch (settings, RECORDING): Spout2 on or off, no more
  `NS_SPOUT=1` before launch; toggling restarts the worker.
* An experimental GPU picker for two-NVIDIA-card machines, moving the network and
  the capture together (#29); `NS_GPU=<index>` does the same without the menu.
* Windows 10 confirmed by a user on an RTX 4060 (#30), with the one-window rebuild
  loop they hit fixed.
* "Save as" never opened for anyone - an error was caught and swallowed and the
  dialog fell back to the screenshots folder.
* Changing the desktop resolution left the program on the old one; the archive
  shipped without its icons in 1.5.6/1.5.7 - both fixed.

## v1.5.7 - 2026-09-11 - the icons ship, screenshots on any path

* Screenshots were silently lost on non-ASCII paths: OpenCV's writer returns True
  and writes nothing outside ASCII, so Python writes the file now.
* The archive shipped without its icons (issue #28) - both are back and the
  release check fails the build if they go missing again.
* A redrawn icon: the logo alone, round, with more contrast and a white rim, every
  size rendered on its own.
* Adaptive exposure could produce NaN when `NS_PW_DARK` and `NS_PW_LIT` were
  equal, returning a black frame - guarded.
* `main.py` was taken apart: 3788 lines to 940, the state went into one object
  with `__slots__`, and a test now forbids importing `main` back. 54 checks green.

## v1.5.6 - 2026-09-10 - adaptive exposure, GDI fallback, recording fix

* Adaptive exposure: dark scenes are brightened for the network automatically (the
  PaperWhite principle), on by default, `NS_PW=0` disables.
* GDI capture fallback on Optimus laptops where the display is wired to the iGPU
  (`DXGI_ERROR_UNSUPPORTED`), confirmed on an Acer Nitro with an RTX 4050 (#26).
* Recording dropped every second frame - the slot is reserved by `needs_frame()`
  now, taking a 5-second recording from ~75 to ~150 frames.
* A crash on a monitor switch (issues #24/#26): a stale dxcam output index raised
  `IndexError`; the capture validates and falls back to output 0 with an alert.
* The smoke and GUI cycle checks never ran in the full suite; 52 tests green.

## v1.5.5 - 2026-09-10 - user presets, recording indicator, screenshot folder

* User presets: save a snapshot of the four sliders under a name, delete it, and
  it applies like a built-in profile - with broken entries dropped and a config
  pointing at a deleted preset falling back to Natural.
* A recording indicator: a red dot with a timer in the screen corner, hidden from
  the recorded file, toggleable in settings.
* A remembered screenshot folder, so Save As opens there every time (#20).
* UI: the resolution slider got its own section, expanded lists became a pop-up
  layer with hover highlights, and silent failure points gained alerts.
* Under the hood: a swappable runtime (`nr_dll` / `NS_NR_DLL`), noise-floor motion
  vectors zeroed, and an environment header in the log. 49 tests green.

## v1.5.4 - 2026-09-10 - stability: 8 code-review fixes, remap fixes, auto-recovery

* Remapping no longer auto-executes the key you press, and Num4/Num6 became
  remappable - all eight hotkeys are listed, in all 12 languages.
* NVENC codec fallback AV1 -> HEVC -> H.264, probed at open time, so recording
  works on RTX 30 (no AV1 encoder); frame demand is gated to one frame per 30 fps.
* Atomic config writes (temp + fsync + atomic replace), and the menu layout save
  also persists the profile, the NR parameters and the monitor.
* The saved monitor is remembered by DXGI DeviceName instead of a positional
  index, so a cable unplug or dock does not move the capture to another screen.
* NGX result 0x00000000 ("no frame this call") is no longer treated as a crash -
  it used to restart the worker three times and turn NR off (issue #11). Fence
  hardening, and one automatic revive after a 30 s backoff for transient failures.

## v1.5.3 - 2026-09-10 - UI hotfix: text clipping, language list scroll, CJK names

* Long localised strings clip with an ellipsis instead of painting over the
  neighbours; the French UI was the worst offender. French wording was shortened
  and the record button shows a short "Stop" while recording (all 12 languages).
* The language list scrolls, is bounded by the panel and opens upward when there
  is no room below - the last languages were unreachable before.
* CJK names (中文, 日本語, 한국어) render with per-script fonts instead of boxes.
* Menu position is fixed: the saved offset is honoured across mode switches, and
  the restore applies only to a recreated window.

## v1.5.2 - 2026-09-10 - UX package, 12 languages

* 12 languages (EN, RU, FR, DE, ES, IT, PT, PL, UK, ZH, JA, KO) behind a drop-down
  switcher, with CJK fonts shipped so they do not render as tofu boxes.
* A recording audio limiter: the system mix can peak above 0 dBFS (measured up to
  +7.9 dB) and AAC clipped it - a soft tanh limiter above 0.9 folds peaks toward
  1.0, verified as +7.9 dBFS -> 0.0 dBFS and 2103 clipped samples -> 0.
* Menu rework: live indicators on the main page only, WORK became MODE, the View
  section became Actions, and the footer holds only Quit.
* Default profile is Natural, and new tests cover every control, the limiter and
  i18n parity.

## v1.5.1 - 2026-09-09 - Hotfix: RTX 30/40 restored (and the UX package, now v1.5.2)

* The v1.4.1+ builds regressed every non-Blackwell card: the architecture spoof
  target moved from `0x1B0` to `0x1A0`, and the leaked runtimes refuse feature 18
  below `0x1B0`. Back to the v1.3.0 stack - universal runtime (310.8.0,
  sm_75/86/89/120) with spoof `0x1B0` - so RTX 30 and RTX 40 work again.
* `[arch]` lines now always reach the log and print the real spoof value, and the
  archive carries a `VERSION.txt` with the commit, the runtime SHA-256 and the
  kernel architectures.
* v1.5.0 was released twice under the same tag (first a broken community patch,
  then a Blackwell-only runtime): this release is a single verifiable archive,
  stated in the notes.
* The tag also carried the v1.5.2 UX package while it was being tested, which then
  got its own release.

## v1.5.0 - 2026-09-09 - RTX 40 support

* RTX 40-series support: the bundled `nvngx_dlssnr.dll` is a community
  re-targeted 310.8.0 build carrying sm_89 and sm_120 kernels, so it runs on Ada
  and Blackwell. Previous builds died on Ada with `0xBAD00001` on frame 0
  (issues #8, #10, #11, #12).
* A window picker page that outlines the real window in amber on hover and lists
  only real taskbar windows.
* A visible window-mode exit in the footer, and the capture mode shown under the
  GPU line.
* Spout2 bridge behind `NS_SPOUT=1` (OBS records the NR picture through the Spout2
  Capture plugin), off by default; DRED diagnostics for device removal; recording
  at 30 fps instead of 60; safe passthrough when feature 18 cannot be created.
* Known limitations: no HDR, no hybrid-graphics laptops without a MUX switch.

## v1.4.2 - 2026-09-09 - Window picker page (testing build)

* Marked a pre-release for testing only: experimental changes needing real-world
  validation.
* The window picker page: hover outlines the real window on screen, clicking
  switches the capture; only real taskbar windows are listed.
* A visible window-mode exit from the footer ("already active" alert in fullscreen
  mode), and the capture mode shown under the GPU line.
* Screenshots in the README: light/dark main page, the window list, the settings
  page.

## v1.4.1 - 2026-09-09 - Spout2 bridge (testing build)

* Marked a pre-release for testing only.
* The Spout2 bridge behind `NS_SPOUT=1`, so OBS with the Spout2 Capture plugin can
  record the NR picture directly (off by default).
* DRED device-removed diagnostics in the log instead of a bare "code 6".
* Recording at 30 fps instead of 60 (about 36% -> 18% of the frame cost).
* Correct GPU architecture IDs (Turing 0x160, Ampere 0x170), a pre-Blackwell warm-up
  clamp, a hardened hotkey parser, safe passthrough, and picking a window raises it.

## v1.4.0 - 2026-09-09 - No flash, mode-switch overlay

* No flash on startup or mode switches: both overlay windows are created hidden and
  revealed with the first real frame.
* A mode-switch overlay: a brief semi-transparent blur with a spinner covers a
  Num5 or monitor change, and the picture comes back sharp.
* The menu is no longer clipped across a one-window-mode switch.
* Release-archive hygiene: the zip carries exactly what the program needs, with no
  tests or dev tools, and includes NVIDIA's runtime.
* Worker fixes (residual descriptor cache on resize, a GRAY resource leak, a stale
  sized picture, a 4 s DWM flicker from a topmost re-assert on IME windows), and a
  22-scenario suite. The body also lists four post-release fixes added under the
  same tag, ending with DRED diagnostics.

## v1.3.1-win10-diag - 2026-09-08 - diagnostic-only tag (not a version)

* Not a NeuralScreen version: a diagnostic build for issue #1 (the Windows 10
  "code 6" death). The worker logs every upload sub-step with a `[pure]` prefix,
  and this build exists to be run, reproduce the crash and have
  `NeuralScreen.log` attached. Release body is 594 characters, the shortest of any
  release.

## v1.3.0 - 2026-09-08 - One-window mode, WGC capture, numpad keys

* One-window mode (Num5): the network runs on the focused window and the overlay
  sits on it, following moves and resizes.
* The NVIDIA App and OBS can record the result: in one-window mode the overlay
  stops hiding from capture; full-screen mode keeps hiding it.
* Hotkeys moved to the numpad (Num1 NR, Num2 menu, Num3 screenshot, Num0 record,
  Num5 one-window, Num4/Num6 resolution, Ctrl+Alt+Q quit), Num Lock on.
* Odd-height and fast windows (140+ FPS) record correctly and the capture bridge
  cleans up on failure.
* Post-release updates under the same tag: the matched residual composite
  (47.9 -> 71.9 FPS at work_scale 0.65 with the native anchor intact), a sharp
  resolution slider, the menu re-placed across a window-mode switch, the version in
  the header, and the ~4 s flicker removed.

## v1.2.1 - 2026-09-08 - Fixes: resolution slider, hotkey polling

* One control for the processing resolution instead of a button and a slider that
  could disagree; its top step is the whole screen, and it still switches live.
* Hotkeys work in games that take the keyboard: a polling fallback runs alongside
  `RegisterHotKey`, and defaults moved off keys games use (F10 NR, F11 menu, Home
  screenshot).
* Recording went back onto shared memory - a size check was rejecting 4K frames and
  they quietly went down the pipe; a 5-second recording went 105 -> 118 frames.
* Stability: a monitor resolution change rebuilds the capture chain, the
  architecture hook no longer answers for a second card or an iGPU, and stopping a
  recording is clean.

## v1.2.0 - 2026-09-07 - Sound in recordings, reduced resolution

* Sound in recordings: `Insert` writes system audio as a second AAC track (WASAPI
  loopback, 192 kbit/s stereo, no virtual cable), padded from the same clock as the
  video so pauses do not shorten the track.
* Processing at reduced resolution: a slider whose top step is the full screen
  (default, best picture) and whose lower steps feed the network a smaller frame -
  42.9 -> 65.3 FPS on a 4K desktop, `Evaluate` 16.05 -> 7.25 ms. Off by default.
* Correction to v1.1.0: the network is same-resolution, and its cost tracks the
  pixels it is handed - `eval = 1.50 ms + 1.51 ms/MPix`.
* `NeuralScreen.exe`: an unsigned launcher with the program's icon (Windows warns
  about an unknown publisher once); `NeuralScreen.vbs` still works.

## v1.1.0 - 2026-09-07 - GPU status, wipe slider, arch hook

* A GPU status line in the menu: green means feature 18 was actually created, by
  the worker's answer rather than the architecture.
* A before/after wipe slider (raw vs NR, composed on the GPU, lands in recordings).
* The architecture hook is on by default (`NS_ARCH_SPOOF=0` disables): feature 18
  on 20/30/40-series, confirmed working on a 40-series card by a user, with a
  likely conflict with NVIDIA's licence terms stated openly.
* NGX evaluation scales with the screen and not with `work_scale` - 15.7 ms on a
  4K desktop, 47 FPS at 4K with NR on (RTX 5070 Ti).
* Native Save As for screenshots, monitor selection, configurable hotkeys, autostart
  with Windows, and a reworked menu.

## v1.0.0 - 2026-09-06 - First release

* Silent launch: `NeuralScreen.vbs` plus a bundled portable Python, no console
  windows.
* Built-in recording (Insert): MP4 AV1 NVENC, 60 fps, quality-targeted VBR, with a
  HUD and the @perseval_BLR watermark burned in and sRGB/BT.709 colour tags.
* A full GPU pipeline (DDA capture + NGX + the worker's window), 102 FPS on
  1440p-class desktops.
* Automatic monitor resolution detection and automatic recovery after display mode
  switches.
* Assets differ from later releases: the archive plus `nvngx.dll`,
  `nvngx_dlssnr.dll`, `README.md` and `README.ru.md`.
