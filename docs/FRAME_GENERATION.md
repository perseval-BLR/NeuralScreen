# Experimental desktop frame generation

Off by default. Enable Frame Generation in the processing menu and select
an output multiplier from 2x to 4x. Requires `native/nvngx_dlssg.dll`, supplied
separately from the source repository.

Two backends, chosen by the GPU (`fg_backend` config key: `auto`, `fsr`, `dlss`):

- RTX 40/50 (Ada, Blackwell): DLSS Frame Generation, as before.
- RTX 20/30 (Turing, Ampere): FSR Frame Generation from the AMD FSR SDK 2.3.0
  (frame generation 3.1.x provider). It runs on any DX12 GPU, makes one
  generated frame per real one (2x, whatever multiplier is picked) and needs
  `amd_fidelityfx_loader_dx12.dll` and `amd_fidelityfx_framegeneration_dx12.dll`
  (from the SDK's `Kits/FidelityFX/signedbin`) next to the worker in `native/`.
  FSR 4 frame generation is the ML variant for RDNA 4 and cannot run on NVIDIA
  cards; 3.1.x is the newest FSR generation available for RTX 20/30.

Generated frames follow neural rendering. A separate presenter
paces bounded frame slots and drops outdated generated frames. It still shares
the GPU command queue with neural rendering; this does not guarantee lower latency
or higher displayed FPS under GPU saturation.

HDR capture stays FP16 scRGB. The edited result is converted to HDR10 PQ for FG,
which does not accept scRGB. Ordinary HDR presentation continues to use scRGB.
Recording and Spout export real SDR frames, not generated frames.

This is experimental: depth is flat and motion is estimated from capture.
Occlusion and UI artifacts remain possible. HUD detection is a separate proposal.
FG runs on both present paths: with NR off it presents the raw capture rather
than the neural frame, so the doubled rate survives turning the neural pass off.
An FG failure returns that frame to ordinary presentation.

Validation:
- `runtime/python.exe tests/test_framegen_controls.py`
- `runtime/python.exe tests/test_frame_generation.py --run`
- `runtime/python.exe tests/test_frame_generation.py --hdr --dynamic --check-pixels`
- `native/test-hdr.bat`

Native tests require an NVIDIA GPU; HDR tests require an HDR-enabled display.
