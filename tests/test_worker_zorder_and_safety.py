"""The worker's z-order rules and the safety fixes of the 22.09 audit, as source.

Read from the C++ rather than run, because each of these only shows itself on
a machine in a particular state (a busy desktop, a card that refuses 3x, a
swapped DLL). Each check names the behaviour it protects:

* the retry after a failed FG present on the HDR path never re-enters FG
  (it recursed until the stack ran out on a card that refuses the multiplier);
* a follow step of a captured window moves the picture, never raises it
  (it re-inserted it over the open panel on every step - #96);
* the periodic re-assert walks past helper, cloaked and off-picture windows,
  and raises the panel first so the picture lands directly below it (#96);
* a stale panel handle is looked up again by its owner;
* the FG presenter's stop flag is stored under its mutex (a lost wake-up);
* the split-view UAV cache is dropped with the textures it pointed at;
* the DLL gate holds the file BEFORE it verifies it, fails closed, and the
  configured NS_NR_DLL goes through it;
* the GPU recorder frees its device on a failed start, detaches late sample
  returns, and bounds the audio ring by its mapping;
* an HDR10 recording that loses its HDR picture is closed, not frozen;
* a frame due on screen is copied and presented on the swap chain's own
  queue, never on the worker's behind the next NR pass (with FG 2x at 4K
  every real frame waited ~13 ms there).

Run:  runtime\\python.exe tests\\test_worker_zorder_and_safety.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
NATIVE = BASE / "native"


def _read(name: str) -> str:
    return (NATIVE / name).read_text(encoding="utf-8", errors="surrogateescape")


def _body(src: str, signature: str) -> str:
    """The function whose definition starts with `signature`, to its closing brace."""
    # `{` right after the signature: a forward declaration is not the body.
    m = re.search(re.escape(signature) + r"\s*\{.*?\n\}", src, re.S)
    return m.group(0) if m else ""


def _code(text: str) -> str:
    """Comments stripped: a check a comment can satisfy is not a check."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def main() -> int:
    failures = []
    cpp = _read("dlss5-feed-host64.cpp")
    hdr = _read("hdr_present.inl")
    fg = _read("frame_generation.inl")
    trust = _read("dll_trust.h")
    grec = _read("gpu_recorder.cpp")

    # 1. HDR + FG retry
    present_hdr = _code(_body(hdr, "static bool PresentHdr(VideoState &v, bool bypass, bool allow_fg, UINT64 *submitted)"))
    if not present_hdr:
        failures.append("PresentHdr no longer takes allow_fg")
    elif "allow_fg && FgRequested()" not in present_hdr:
        failures.append("PresentHdr decides FG without allow_fg")
    if re.search(r"return\s+PresentHdr\(v,\s*bypass\)\s*;", hdr):
        failures.append("the FG-failure retry re-enters PresentHdr with FG allowed "
                        "- it recurses at a refused multiplier")
    if "return PresentHdr(v, bypass, false, submitted);" not in hdr:
        failures.append("the FG-failure retry is not an ordinary (no FG) present")

    # 2. follow step: a move, never a raise
    follow = _code(_body(cpp, "static void FollowCapturedWindow()"))
    if not follow:
        failures.append("FollowCapturedWindow is gone")
    else:
        if "HWND_TOPMOST" in follow:
            failures.append("a follow step can still re-insert the picture at "
                            "HWND_TOPMOST - over the open panel (#96)")
        if "SWP_NOZORDER" not in follow:
            failures.append("the follow SetWindowPos does not keep the z-order")
        if "ShowPresentBelowPanel()" not in follow:
            failures.append("the window-back re-show does not go below the panel")

    # 3. the re-assert walk
    reassert = _code(_body(cpp, "static void ReassertPresentTopmost()"))
    for token, why in (
            ("GW_HWNDNEXT", "it reads only the top window again"),
            ("DWMWA_CLOAKED", "cloaked Start/Search hosts count as covering"),
            ("< 16", "1x1 and 20x20 helpers count as covering"),
            ("IntersectRect", "windows on another monitor count as covering"),
            ("PanelTopmost(hud)", "the raise does not put the panel first"),
            ("RaisePanelAndPicture(hud, g_present_hwnd)",
             "the picture is not inserted below the panel")):
        if token not in reassert:
            failures.append(f"ReassertPresentTopmost lost {token!r}: {why}")
    # The raise itself, off the frame thread (test_panel_raise_async drives
    # it): panel first, the picture inserted after the panel.
    raise_both = _code(_body(cpp, "static void RaisePanelAndPicture(HWND hud, HWND picture)"))
    if not raise_both or "SetWindowPos(pic, panel" not in raise_both:
        failures.append("RaisePanelAndPicture no longer inserts the picture below the panel")

    # 4. stale panel handle
    hud = _code(_body(cpp, "static HWND HudWindow()"))
    if "FindClientPanel(ParentProcessId())" not in hud:
        failures.append("HudWindow never looks the panel up again once the "
                        "published handle is stale")

    # 5. FG stop under the mutex
    stop = _code(_body(fg, "static void StopFgPresentation()"))
    if not re.search(r"lock_guard<std::mutex>\s+\w+\(g_fg\.mutex\);\s*g_fg\.stop\s*=\s*true",
                     stop):
        failures.append("StopFgPresentation stores `stop` outside the mutex - "
                        "the presenter can miss it and join() blocks for good")
    if re.search(r"~FgState\(\)\s*\{\s*stop\s*=\s*true", fg):
        failures.append("~FgState stores `stop` outside the mutex")
    # ...and the chain can hand the presenter its latency waitable: created
    # with the flag, and put back on the default latency for the ordinary path
    present = _code(_body(cpp, "static bool OpenPresent(UINT width, UINT height, uint32_t flags)"))
    if "DXGI_SWAP_CHAIN_FLAG_FRAME_LATENCY_WAITABLE_OBJECT" not in present:
        failures.append("the overlay chain is created without the latency "
                        "waitable - FgStart's SetMaximumFrameLatency(1) is "
                        "refused and FG pacing always falls back to the clock")
    elif "SetMaximumFrameLatency(3)" not in present:
        failures.append("the chain created with the waitable is left at "
                        "latency 1 - the ordinary present path flickers (R13)")

    # 6. split UAV cache
    release = _code(_body(cpp, "static void ReleaseVideoTextures(VideoState &v)"))
    if "g_split_uav_for = nullptr" not in release:
        failures.append("ReleaseVideoTextures keeps the split-view UAV cache - "
                        "a new output at the old address uses a freed descriptor")

    # 7. DLL trust
    gate = _code(_body(trust, "static bool NsTrustedDll(const wchar_t *path)"))
    if "CreateFileW" not in gate or "NsTrustedDllHeld(path, hold)" not in gate:
        failures.append("NsTrustedDll does not take the hold before verifying")
    if re.search(r"return\s+true;\s*\}\s*$", gate) and "INVALID_HANDLE_VALUE) return false" not in gate:
        failures.append("the DLL gate may still pass without a hold")
    held = _code(_body(trust, "static bool NsTrustedDllHeld(const wchar_t *path, HANDLE hold)"))
    if "file.hFile = hold" not in held:
        failures.append("the signature is not verified through the held handle")
    nr = cpp.split('GetEnvironmentVariableW(L"NS_NR_DLL"', 1)[-1][:2000]
    if "NsGateByoDll(full" not in nr:
        failures.append("the configured NS_NR_DLL is loaded without the signature gate")

    # 8. GPU recorder
    start_fail = grec.split("if (FAILED(hr))\n    {\n        r->thread.join();", 1)[-1][:600]
    if "SafeRelease(r->d11)" not in start_fail or "SafeRelease(r->ctx)" not in start_fail:
        failures.append("a failed GPU recording start still leaks its D3D11 device")
    if "Detach()" not in _code(_body(grec, "void Teardown(Recorder *r)")):
        failures.append("Teardown frees the Recorder with late sample returns attached")
    if "r->audio_capacity = r->ring->capacity" not in grec or "VirtualQuery" not in grec:
        failures.append("the audio ring's capacity is not checked against its mapping")
    if "const int64_t capacity = r->ring->capacity;" in grec:
        failures.append("PumpAudio re-reads the client's capacity on every pump")

    # 9. HDR10 recording without its HDR picture
    if "hdr_feed_lost_at" not in cpp or "the HDR10 recording lost its HDR picture" not in cpp:
        failures.append("an HDR10 recording that leaves the HDR path is not closed")

    # 10. The BYO runtime is loaded through a buffer that outlives its block
    byo = _code(cpp.split('L"libraries\\\\nvngx_dlssnr.dll"', 1)[-1][:1500])
    if "dll_name = candidate" in byo:
        failures.append("the BYO runtime's path points into a block-scoped "
                        "buffer that is dead by the LoadLibraryW")

    # 11. DRED: the settings come from the runtime, before the device
    init = _code(_body(cpp, "static bool InitDisguise()"))
    if "EnableDred(d3d12)" not in init or "create_device(" not in init:
        failures.append("InitDisguise does not enable DRED")
    elif init.index("EnableDred(d3d12)") > init.index("create_device("):
        failures.append("DRED is enabled after the device exists - it applies "
                        "only to devices created after it")
    if re.search(r"h\.dev->QueryInterface\(__uuidof\(ID3D12DeviceRemovedExtendedDataSettings", cpp):
        failures.append("the DRED settings are asked of the device again - no "
                        "Windows answers that")
    if "D3D12GetDebugInterface" not in _code(_body(cpp, "static void EnableDred(HMODULE d3d12)")):
        failures.append("EnableDred does not use D3D12GetDebugInterface")

    # 12. The overlay never hangs past a shrunken window, and a size mismatch
    #     really hides it
    follow = _code(_body(cpp, "static void FollowCapturedWindow()"))
    # The comparison must go through the shared decision, which measures the
    # capture's own border (present_follow.h, #139): the literal `buffer >
    # frame` was true for every window on Windows 10, where the capture
    # carries the invisible resize border and the frame does not, so the
    # overlay was hidden on the first follow step and the effect never came
    # back. The decision itself is driven by tests/test_present_follow.py.
    if "ns_present_follow::DecideForStep(" not in follow:
        failures.append("the follower does not use the shared step decision "
                        "(present_follow.h) - a raw buffer/frame comparison "
                        "hides the overlay on every Windows 10 window (#139)")
    if "ns_present_follow::CaptureSlack(" not in follow:
        failures.append("the follower compares the buffer against the frame "
                        "without measuring the capture's resize border (#139)")
    if re.search(r"\bbw\s*>\s*rw\b|\bbh\s*>\s*rh\b", follow):
        failures.append("a buffer larger than the followed window is still "
                        "placed over it - it hangs past the right/bottom edge")
    if "!g_present_mismatch" not in follow:
        failures.append("the window-back re-show ignores a size mismatch")
    active = _code(_body(cpp, "static bool PresentModeActive(const VideoState &v)"))
    # A 1400-character window, not 900: the original fix (the SW_HIDE) and the
    # F9 re-show request sit ~950 characters behind the first `if`, and a
    # window that ends between them silently drops the second check.
    mismatch = active.split("if (ow != g_present_w || oh != g_present_h)", 1)[-1][:1400]
    if "ShowWindow(g_present_hwnd, SW_HIDE)" not in mismatch:
        failures.append("a size mismatch only clears the flag - the stale "
                        "overlay stays on screen")

    # 12b. The re-show after a mismatch happens AFTER a Present (F9), never in
    #      PresentModeActive itself.
    #
    # PresentModeActive runs BEFORE the frame is presented. Showing the window
    # there put it back on screen while the compositor still held the frame
    # from before the mismatch, so one refresh showed a picture that no longer
    # matched the output - the "one frame early" of the brief. The decision is
    # taken in PresentModeActive (it is the function that knows the sizes) and
    # the show is performed by RevealOnFirstPresent, which every present path
    # calls with the frame of that call already presented.
    if "g_present_reshow = true" not in mismatch:
        failures.append("the size mismatch no longer asks for the overlay back "
                        "(F9) - desktop mode would leave it hidden forever")
    # Hiding is this region's own job (SW_HIDE, checked above); SHOWING is not.
    if "SW_SHOW" in mismatch or "ShowPresentBelowPanel" in mismatch:
        failures.append("the overlay is shown while the sizes are checked - the "
                        "frame from before the mismatch goes on screen for one "
                        "refresh (F9)")
    reveal = _code(_body(cpp, "static void RevealOnFirstPresent()"))
    if "g_present_reshow" not in reveal:
        failures.append("nothing consumes the post-mismatch show request - the "
                        "overlay never comes back after a resize (F9)")
    elif reveal.find("g_present_reshow") > reveal.find("g_present_revealed) return"):
        failures.append("the re-show is handled after the already-revealed "
                        "early return - it is dead code (F9)")
    # Every path that presents has to offer the show; one that does not leaves
    # a desktop-mode mismatch hidden until the next mode switch.
    for name, signature, source in (
            ("PresentFrame", "static bool PresentFrame(VideoState &v, UINT64 *submitted = nullptr)", cpp),
            ("PresentBypass", "static bool PresentBypass(VideoState &v)", cpp),
            ("PresentHdr", None, present_hdr)):
        body = source if signature is None else _code(_body(source, signature))
        if body and "RevealOnFirstPresent()" not in body:
            failures.append(f"{name} presents without offering the reveal - a "
                            "re-show requested by PresentModeActive is dropped (F9)")
    # The one-shot log line is per episode: a session that resizes twice must
    # not go silent about the second one (F9).
    if "warned = false" not in mismatch:
        failures.append("the mismatch warning is one-shot per process, not per "
                        "episode - the second resize is silent in the log (F9)")

    # 13. The HDR composite reads a flipped display's native frame turned over
    shaders = _read("hdr_shaders.h")
    composite = shaders.split("kHdrCompositeHlsl[]", 1)[-1][:3000]
    if "rotate180" not in composite:
        failures.append("the HDR composite ignores rotate180 - upside down on a "
                        "Landscape (flipped) display")
    if "SetComputeRoot32BitConstants(1, 4," in hdr:
        failures.append("the HDR composite is handed 4 constants, not 5 "
                        "(rotate180 missing)")

    # 14. The DLL gate judges the chain at the signature's time, with its
    #     own certificates
    chain = _code(trust.split("static bool NsChainMachineRootsOnly(", 1)[-1][:2500])
    if "CertGetCertificateChain(engine, leaf, &at, signature_store" not in chain:
        failures.append("the machine-root chain is built at the current time "
                        "without the signature's certificates - a timestamped "
                        "DLL is refused once its certificate expires")
    held = _code(_body(trust, "static bool NsTrustedDllHeld(const wchar_t *path, HANDLE hold)"))
    if "sftVerifyAsOf" not in held:
        failures.append("the gate does not take the time WinVerifyTrust judged at")

    # 15. A frame due on screen does not queue behind the network: the swap
    #     chain lives on its own queue, and no back buffer is written on the
    #     worker's
    opened = _code(_body(cpp, "static bool OpenPresent(UINT width, UINT height, uint32_t flags)"))
    if "CreateSwapChainForHwnd(PresentQueue()," not in opened:
        failures.append("the swap chain is created on the worker's queue - every "
                        "frame due on screen waits behind the next NR pass")
    presenter = _code(_body(fg, "static void FgPresenter()"))
    if ("h.queue->ExecuteCommandLists" in presenter
            or "PresentQueue()->ExecuteCommandLists" not in presenter):
        failures.append("the FG presenter copies on the worker's queue")
    back_buffer_on_worker = re.compile(r"h\.list->CopyResource\(\s*bb")
    for name, signature, call in (
            ("PresentFrame", "static bool PresentFrame(VideoState &v, UINT64 *submitted = nullptr)",
             "CopyToBackBuffer(bb,"),
            ("PresentBypass", "static bool PresentBypass(VideoState &v)", "CopyToBackBuffer(bb,"),
            ("PresentHdr", None, "CopyToBackBuffer(bb.get(),")):
        body = present_hdr if signature is None else _code(_body(cpp, signature))
        if not body:
            failures.append(f"{name} not found")
        elif back_buffer_on_worker.search(body) or call not in body:
            failures.append(f"{name} writes the back buffer on the worker's queue")
    fmt = _code(_body(hdr, "static bool EnsurePresentFormat(bool hdr, bool pq)"))
    if not 0 <= fmt.find("FlushPresentQueue(") < fmt.find("ResizeBuffers("):
        failures.append("ResizeBuffers runs before the present queue is drained")
    closing = _code(_body(cpp, "static void ClosePresent()"))
    if not 0 <= closing.find("FlushPresentQueue(") < closing.find("g_present_swap->Release()"):
        failures.append("the swap chain is released before the present queue is drained")

    # 16. A capture pause is finally RESET, not just announced (#130)
    #
    # The stall detector used to clear its own flag on the first fresh frame,
    # three hundred lines above the one place that consumes it. The reset was
    # therefore dead: the log printed "capture resumed ... history reset" on
    # every pause while NR kept its temporal accumulation and FG kept its
    # interpolation slots pointed at a picture that no longer existed - the
    # jerk on a window drag in the report. The flag must survive until the
    # consumer, and the consumer must be the only one that clears it.
    detector = _code(cpp[cpp.index("R12: the pause detector"):])
    detector = detector[:detector.index("if (!got && !g_dda_ready)")]
    if "stall_pending = false" in detector:
        failures.append("the pause detector clears its own flag - the reset it "
                        "announces is dead before the consumer reads it (#130)")
    if "stall_pending = true" not in detector:
        failures.append("the pause detector no longer marks a long silence")
    consumer = _code(cpp[cpp.index("const bool stall_reset = "):])
    consumer = consumer[:consumer.index("if (!bypass)")]
    if "stall_pending = false" not in consumer or "fh.reset = 1" not in consumer:
        failures.append("the stall reset is not consumed where NR and FG read "
                        "it - nothing is reset after a capture pause (#130)")
    if "g_fg_reset" not in consumer or "stall_reset" not in consumer:
        failures.append("the stall reset does not reach g_fg_reset - FG keeps "
                        "interpolating across the pause (#130)")
    # The announcement must live with the reset, not with the detector.
    if "capture resumed after" not in consumer:
        failures.append("the resume line is printed away from the reset it "
                        "describes - the log claims a reset that may not happen")

    # 17. The first frame of a new Desktop Duplication session is consumed ONLY
    #     when it is really empty, not shown (#128: a screenshot after NR OFF
    #     woke the capture and came back black, because that empty surface was
    #     answered from; F3: consuming every first frame threw away the picture
    #     the reopen fallback reopens the capture for).
    #
    # A fresh duplication session publishes an EMPTY surface on its first
    # AcquireNextFrame - the desktop has not been composited into it. Showing
    # it would overwrite the last good frame in v.color with black. But a first
    # frame that CARRIES the desktop is the current content, and the no-colour
    # fallback reopens the capture precisely to get it: on a still screen it is
    # the only frame the reopen will ever hand over. The flag has to be raised
    # where the session opens, the decision has to be made on the frame info,
    # and an empty frame must leave g_dda_ready false so a consumer asking for
    # pixels gets "no colour yet", not a black picture.
    #
    # Behavioural coverage lives in test_dda_reopen_pixels.py, which drives the
    # worker: it fails on the code that consumed every first frame and passes on
    # the conditional one. These source checks are the cheap guard that the
    # condition cannot be deleted without a test failing somewhere.
    open_dda = _code(_body(cpp, "static bool OpenDda(UINT w, UINT hgt)"))
    if "g_dda_first_frame = true" not in open_dda:
        failures.append("a new duplication session does not raise the "
                        "first-frame flag - a reopened capture can publish its "
                        "empty first frame (#128a)")
    grab = _code(_body(cpp, "static bool DdaGrab(VideoState &v)"))
    if "g_dda_first_frame" not in grab:
        failures.append("DdaGrab does not judge the first frame of a "
                        "duplication session (#128a)")
    else:
        consume = grab[grab.index("g_dda_first_frame"):]
        swizzle = consume.find("SwizzleCaptureIntoColor(")
        if swizzle < 0:
            failures.append("DdaGrab no longer swizzles the capture - cannot "
                            "tell where the first frame is handled (#128a)")
        else:
            guard = consume[:swizzle]
            if not 0 <= guard.find("g_dda_first_frame = false"):
                failures.append("the first frame reaches "
                                "SwizzleCaptureIntoColor without the flag being "
                                "cleared - it is never consumed (#128a)")
            elif guard.find("g_dda_first_frame = false") > guard.rfind("return false"):
                failures.append("the first-frame flag is cleared after the guard "
                                "returns - the frame is dropped, not consumed "
                                "(#128a)")
            if "return false" not in guard:
                failures.append("an empty first frame is not answered as "
                                "\"no frame\" - the consumer cannot tell it from "
                                "a real one (#128a)")
            # The consumption must be conditional on the frame info, or a first
            # frame carrying the desktop is thrown away again (F3).
            if not re.search(r"AccumulatedFrames\s*==\s*0", guard):
                failures.append("the first frame is consumed without checking "
                                "the frame info - a first frame carrying the "
                                "desktop is discarded, which is the frame the "
                                "reopen fallback came for (F3)")
    # The fix is for Desktop Duplication ONLY: discarding the first frame of a
    # WGC pool starves a static window of the only frame it will ever offer.
    wgc = re.search(r"static bool WgcGrab\(VideoState &v\)\s*\{.*?\n\}", cpp, re.S)
    if wgc and "g_dda_first_frame" in _code(wgc.group(0)):
        failures.append("the WGC path also discards its first frame - a static "
                        "captured window would never be shown (#128a)")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the worker's z-order keeps the panel on top, the HDR/FG, "
          "presenter, descriptor, DLL-gate and recorder fixes are in place, "
          "frames go to the screen on their own queue, and a reopened "
          "duplication session no longer shows its empty first frame")
    return 0


if __name__ == "__main__":
    sys.exit(main())
