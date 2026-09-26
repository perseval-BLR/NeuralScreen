# NeuralScreen

**NVIDIA's DLSS 5 neural renderer, applied to your whole Windows desktop in
real time.** Everything on screen — games, video, photos — goes through the
same neural network that DLSS 5 games use, and comes back sharper.

> The guide below gets you running. How it works and what was measured:
> **[TECHNICAL.md](TECHNICAL.md)**. Русская версия:
> **[README.ru.md](README.ru.md)** / **[TECHNICAL.ru.md](TECHNICAL.ru.md)**.

> **Notice.** Not affiliated with NVIDIA; NVIDIA, DLSS and the NVIDIA logo
> are NVIDIA Corporation's trademarks. The bundled NVIDIA runtimes
> (`nvngx_dlssnr.dll`, `nvngx_dlssg.dll`) are NVIDIA's property, included
> unmodified, research/educational use only, no warranty, use at your own
> risk. Rights holders: say the word and the next build ships without them.

> [!IMPORTANT]
> **AMD Radeon build - an active call for testers.** A separate repository, **[NeuralScreen-AMD](https://github.com/perseval-BLR/NeuralScreen-AMD)** - the same overlay with the neural pass aimed at RX 7000 / 9000 (RDNA3/RDNA4). **It has not run on a real Radeon yet**, and that is what that release is for: start with `native/AMD.md`, and if it does not come up, press **Settings -> Program -> Create diagnostic package** and open an issue with that one file - it carries the log, your card, the driver and the stage it stopped at, with your paths already scrubbed.

## How it looks

<table>
<tr>
<td><img src="https://raw.githubusercontent.com/perseval-BLR/NeuralScreen/main/docs/screenshot-main-light.png" alt="Menu, light theme" width="400"></td>
<td><img src="https://raw.githubusercontent.com/perseval-BLR/NeuralScreen/main/docs/screenshot-main-dark.png" alt="Menu, dark theme" width="400"></td>
</tr>
<tr>
<td><img src="https://raw.githubusercontent.com/perseval-BLR/NeuralScreen/main/docs/screenshot-settings.png" alt="Settings" width="400"></td>
<td><img src="https://raw.githubusercontent.com/perseval-BLR/NeuralScreen/main/docs/screenshot-windows.png" alt="Window list" width="400"></td>
</tr>
</table>

*One menu inside the overlay, in light and dark themes; the settings page; the
window list - and the **Before / after wipe** slider that splits the screen.*

## What you need

- **Windows 11**, or Windows 10 — reported working.
- **An NVIDIA RTX card.** **Validated** = reproduced locally; **reported** = user evidence;
  **unverified** = no successful run captured; **known failure** = the shipped runtime refuses or fails.

  | Family | Neural Rendering | Frame Generation |
  |---|---|---|
  | **RTX 50** | **validated** (5070 Ti) | **validated** (5070 Ti) |
  | **RTX 40** | **reported**, unresolved 4060 reports | **reported** (4080 Super ×2; 4060 needs retest) |
  | **RTX 30** | **unverified** | **known failure** (below Ada) |
  | **RTX 20** | **known failure** | **known failure** (below Ada) |

  Hybrid laptops and multi-GPU systems remain experimental; capture may use a slower fallback when the display is attached to the iGPU.

- **The latest NVIDIA driver, and Windows up to date.** Not a formality: the
  neural runtime talks to the driver directly; an old driver is the commonest
  reason it refuses to start or the picture never appears.
- **Nothing installed.** The release archive brings its own Python.

## Install

1. Download the archive from [Releases](https://github.com/perseval-BLR/NeuralScreen/releases)
   and unpack it anywhere. Everything is inside, including NVIDIA's runtime.
2. Run **`NeuralScreen.exe`**.

Windows will probably warn you about an unknown publisher — the program is not
signed with a paid certificate. Click *More info* → *Run anyway*, or use
`NeuralScreen.vbs` next to it.

There is no installer: to remove the program, delete the folder. Autostart is
the one thing written outside it — turn it off before you move or delete it.

> **Do not use it in competitive online games.** A fullscreen overlay over a
> game is what anti-cheat systems look for.

## Using it

The program sits in the tray and draws over your desktop. Press **Num2** for
the menu. The hotkeys are on the numpad, so **Num Lock has to be on**.

| Key | What it does |
|---|---|
| **Num2** | open / close the menu |
| **Num1** | neural rendering on / off |
| **Num7** | frame generation on / off |
| **Num3** | screenshot |
| **Num0** | start / stop recording, with sound |
| **Num4** / **Num6** | processing resolution down / up |
| **Num5** | capture the window under the cursor |
| **Ctrl+Alt+Q** | quit |

Every key can be reassigned in the menu, under the sliders icon. While the
menu is open it takes the mouse and keyboard, so it works on top of a game;
closed, clicks go straight through it.

### Whole screen or one window

The whole screen is the default. **Source**, second in the menu, switches
between **Fullscreen** and **Window mode**; choosing the second opens the list
of windows, and hovering a row highlights that window. **Num5** is the
shortcut when the window is already in front of you: point at it and press.
The overlay follows the window as it moves, and resizing it — a video going
fullscreen, a different player size — reconfigures the worker in place, with
no black moment. Minimising the window pauses processing.

## The menu

The dot next to your graphics card is green when neural rendering really runs
on it, red when it is not.

- **Source** — the whole screen or one window, and which window.
- **Profile** — how strong the effect is, from *Faithful* to *Extreme*;
  *Natural* by default. The four sliders underneath are the same thing in
  detail. **Save preset** stores the current values under a name and puts it
  in the Profile list; **Delete preset** removes it. Dark scenes are
  brightened automatically so shadows keep their detail. A profile moves the
  sliders only — the model below stays where you put it.
- **Model** — *which* network produces the picture, as opposed to how
  strongly. Three of them, and they are three different outputs rather than
  three strengths: **Default** suits a desktop, **Natural** and **Cinematic**
  are tuned for games and soften photographs and small text (measured in
  TECHNICAL.md). A saved preset keeps the model it was saved with.
- **Before / after wipe** — leaves the left part of the screen unprocessed so
  you can see what the effect is doing. Back to 0 when done.
- **Boost** — on by default. The network runs at a reduced resolution and a
  slider under the switch chooses which: measured on a 5070 Ti at 4K,
  **45.7 → 72.6 frames** at the default step and **83.4** at the lowest. The
  picture stays sharp — the result is composed onto your original frame, so
  text and edges keep full resolution. Turn it off to compare. **NR passes**
  under the slider repeats the pass: an experiment, priced in TECHNICAL.md.
- **DLSS 4.5 FG** — Frame Generation, off by default, with a ×2 / ×3 / ×4
  multiplier beside the switch (and on **Num7**). DLSS-G's own desktop
  build: the depth is flat and the motion is estimated, there is no engine
  cooperation, so UI and text can distort — the known cost of the approach.
  The header pairs the two honest rates when they differ: "47 / 111 fps" is
  the network's output, then what the presenter shows. DLSS-G has a hardware
  floor of its own: on a card below Ada the runtime refuses and **the switch
  flips back off with a short notice** — no silent ON. Validated on RTX
  50-series; adapters beyond it are unconfirmed.

Everything else is behind the sliders icon: monitor/GPU, a 30/60/custom/off
frame limiter, HDR, media folders, file conversion, Spout2, the recording
indicator, an on-screen frame counter, minimise/close to tray, key
assignments, keyboard navigation, theme and **12** languages,
and **Scale** above Quit, which sizes the panel.

## Swapping a runtime

Everything ships in the archive. To run your own runtime build (a newer
DLSS-G, say), drop the DLL into **`native/libraries/`** — it wins over the
bundled copy; `nr_dll` / `NS_NR_DLL` remain the NR override.

## Recording and screenshots

**Num0** records to the configured folder, on the GPU by default (NVENC, 60 fps,
sound, no frame-rate cost; a recording an error cuts short is kept). The open
menu is not in it: turn **Record on the GPU** off for the older 30 fps path,
which draws it in. **Num3** freezes the frame, menu included, before any
dialog; choose **Save As** or quiet saving, plus PNG or JPEG, in settings.

**Recording externally:**

- **OBS (recommended):** turn on **Spout2 output (OBS)** in the settings, then
  add a **Spout2 Capture** source in OBS. Works in any mode.
- **NVIDIA App:** it has no Spout input, so use one-window mode. In full-screen
  mode the overlay intentionally hides from Windows/OBS capture to prevent a
  feedback loop; use Spout or the built-in screenshot for the processed frame.

## If something is not working

**Nothing appears after launch.** The program first runs three synthetic
frames without desktop capture. A failed, unsupported or quarantined result
blocks the overlay instead of entering a restart loop. Use **Create diagnostic
package** on the Program tab (or the path in the failure dialog), then check
`NeuralScreen.log`.

**The overlay is invisible in a game.** True fullscreen cannot have anything
drawn over it — a Windows rule. Switch the game to *borderless*.

**The menu pointer is missing or frozen.** A fullscreen game hides the system
cursor, and the overlay only shows that one. Borderless fixes it.

**Everything is too bright and the sliders do nothing.** HDR is on for that
display. Turn it off (Win+Alt+B), or try **HDR compatibility** in the
settings — it is experimental; see [HDR setup](https://github.com/perseval-BLR/NeuralScreen/blob/main/docs/HDR.md).

**A key does nothing.** Something else claimed it; reassign it in the menu.

## Known limitations

- **True fullscreen games** cannot have an overlay drawn over them — borderless or windowed only.
- **HDR displays:** experimental, and off until you turn on **HDR compatibility** (settings, CAPTURE). Recording and Spout exports stay SDR. See [HDR setup and limitations](https://github.com/perseval-BLR/NeuralScreen/blob/main/docs/HDR.md).
- **Windows 10 and multi-GPU systems are experimental** — adapter/output selection is covered by regression tests, but not by local multi-GPU hardware.
- **A rotated display:** 180° is turned back over on capture; 90° and 270° are not handled yet and come out with the sides swapped.
- **Pipeline latency** is 25–30 ms (10-15ms with Boost Mode) — fine interactively, not competitively; **processing resolution is capped at 2560×1440**, output is always your full native resolution.
- **Window/menu recovery:** the menu is re-shown, raised and redrawn after taskbar, monitor or GPU reactivation; a full driver reset remains hardware-dependent.
- **Lossless Scaling:** there is no supported direct hand-off; one-window mode uses a separate presenter, so LS can still select the source HWND and show two windows.
- **Frame generation on RTX 20/30:** there is no FSR FG backend; it is a research candidate, not a promised compatibility mode.

## License

The code here is source-available under the PolyForm Strict License 1.0.0: noncommercial use is free; distributing, modifying or copying it needs the licensor's permission - see [LICENSE](LICENSE). NVIDIA's runtimes
ship unmodified and remain NVIDIA's property: `nvngx_dlssnr.dll` is the leaked 310.8.0 build (sm_75/86/89/120 kernels, RTX 20-50), `nvngx_dlssg.dll` is the
public 310.9.1.0 redistributable — both as received, no guarantees, research-only. Interface faces: IBM Plex (OFL-1.1, `fonts/OFL.txt`).
