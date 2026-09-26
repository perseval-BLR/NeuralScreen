// Experimental desktop DLSS-G. Capture has no engine depth: use a flat plane
// and the estimated motion field. Keep this opt-in; UI/occlusions can distort.
static PFN_NR_Evaluate g_fg_evaluate = nullptr;
static NVSDK_NGX_Result FgEvaluateBridge(ID3D12GraphicsCommandList *list,
    NVSDK_NGX_Handle *handle, NVSDK_NGX_Parameter *params, PFN_NVSDK_NGX_ProgressCallback cb)
{ return g_fg_evaluate(list, handle, params, cb); }
#define NVSDK_NGX_D3D12_EvaluateFeature_C FgEvaluateBridge
#include "include/nvsdk_ngx_helpers_dlssg.h"
#undef NVSDK_NGX_D3D12_EvaluateFeature_C

struct FgSlot {
    winrt::com_ptr<ID3D12Resource> real, interpolated[3];
    unsigned count = 1;
    int state = 0; // free, writing, ready, presenting; protected by mutex
    UINT64 sequence = 0;
    bool interpolate = false;
    double interval = 0.016;
};
static struct FgState {
    HMODULE module = nullptr;
    PFN_NR_Create create = nullptr;
    PFN_NR_Release release = nullptr;
    NVSDK_NGX_Parameter *params = nullptr;
    NVSDK_NGX_Handle *feature = nullptr;
    VideoTex depth;
    winrt::com_ptr<ID3D12Resource> output[3];
    winrt::com_ptr<ID3D12Resource> disable, disable_readback;
    UINT w = 0, height = 0, mw = 0, mh = 0;
    DXGI_FORMAT format = DXGI_FORMAT_UNKNOWN;
    FgSlot slots[3];
    std::mutex mutex;
    std::condition_variable wake;
    std::thread thread;
    std::atomic<bool> stop{false}, failed{false};
    UINT64 sequence = 0;
    std::chrono::steady_clock::time_point last;
    bool history = false;
    // The store goes under the mutex (see StopFgPresentation).
    ~FgState()
    {
        { std::lock_guard<std::mutex> lock(mutex); stop = true; }
        wake.notify_all();
        if (thread.joinable()) thread.join();
    }
} g_fg;
// g_fg_present_fence lives in dlss5-feed-host64.cpp near the FgPresent
// forward declaration: PresentFrame consumes it for the defer-tail token,
// and the include of this file sits below that call site.

static int g_fg_ui_enabled = -1;
static unsigned g_fg_count = 1;
// What we actually ask the runtime for, 0-based: 1 = 2x, 3 = 4x.
//
// It starts at the DLSS 4 ceiling and drops only when a create or evaluation
// is actually refused - the runtime itself answered "not that step". Two
// refusals on a card that stops at 2x (4x, then 3x) land it on 2x and Frame
// Generation keeps running; before this, the first refusal killed the feature
// outright. Switching FG off and on re-arms the user's choice, so a newer
// stock provider or driver update gets a fair try without a restart.
static unsigned g_fg_count_limit = 3;
// Set when a refusal was answered by stepping the multiplier down: that frame
// is a retry, not a failure. FgPresent reads and clears it.
static bool g_fg_retry = false;

// Report-only: what the runtime says its own ceiling is, through
// DLSSG.MultiFrameCountMax (3 = 4x, 1 = 2x only). It is advisory and never
// clamps the request - the step-down below is what adapts, because it is
// driven by an answer this run actually got. The value is logged so that a
// log from a card which stops at 2x names the reason on the first attempt.
static unsigned g_fg_cap_reported = 0;

// Step the multiplier down after a refusal. True when a lower step remains to
// try; false when 2x itself was refused, which is the real "cannot run here".
static bool FgStepDown(unsigned refused)
{
    if (g_fg_count <= 1) return false;
    Log("[fg] %ux refused 0x%08X; stepping down to %ux",
        g_fg_count + 1, refused, g_fg_count);
    g_fg_count_limit = g_fg_count - 1;
    return true;
}

static bool FgRequested()
{
    static const bool requested = [] {
        char value[8] = {};
        return GetEnvironmentVariableA("NS_FRAMEGEN", value, sizeof(value)) == 1 && value[0] == '1';
    }();
    return (g_fg_ui_enabled < 0 ? requested : g_fg_ui_enabled != 0) && !g_fg.failed;
}

static void StopFgPresentation()
{
    // Stored under the mutex. The presenter checks `stop` in its wait
    // predicate and then sleeps with no timeout; a store and notify that land
    // between the check and the sleep were lost, and the join below blocked
    // the worker for good (a stop on every FG toggle, resize, format change
    // and ClosePresent - rare, but a hang the client could only time out).
    { std::lock_guard<std::mutex> lock(g_fg.mutex); g_fg.stop = true; }
    g_fg.wake.notify_all();
    if (g_fg.thread.joinable()) g_fg.thread.join();
    // The presenter has stopped: nothing waits on the handle any more.
    CloseFgWaitable();
    // A failed fence means the queue may still own every resource below.
    // Keep them alive until process teardown instead of releasing them from
    // the recovery path.
    if (g_submission_failed) { g_fg.history = false; return; }
    // Hand the swapchain back to the ordinary present path: default latency.
    if (g_present_swap != nullptr)
    {
        IDXGISwapChain2 *sc2 = nullptr;
        if (SUCCEEDED(g_present_swap->QueryInterface(
                __uuidof(IDXGISwapChain2), reinterpret_cast<void **>(&sc2)))
            && sc2 != nullptr)
        {
            sc2->SetMaximumFrameLatency(3);
            sc2->Release();
        }
    }
    for (auto &slot : g_fg.slots) { slot.real = nullptr; for (auto &image : slot.interpolated) image = nullptr; slot.state = 0; }
    g_fg.history = false;
}

static void CloseFgResources()
{
    StopFgPresentation();
    if (g_submission_failed) return;
    if (g_fg.feature) { g_fg.release(g_fg.feature); g_fg.feature = nullptr; }
    if (g_fg.depth.tex) { g_fg.depth.tex->Release(); g_fg.depth.tex = nullptr; }
    if (g_fg.depth.upload) { g_fg.depth.upload->Release(); g_fg.depth.upload = nullptr; }
    for (auto &image : g_fg.output) image = nullptr;
    g_fg.disable = nullptr; g_fg.disable_readback = nullptr;
    g_fg.w = g_fg.height = 0;
}

// The presenter has its own command list/allocator/fence. The producer never
// waits for frame pacing. Slots being displayed cannot be recycled by it.
// It submits on the present queue - the swap chain's own - so a frame due on
// screen is copied and shown while the producer's next NR pass is still
// running, not after it.
static void FgPresenter()
{
    winrt::com_ptr<ID3D12CommandAllocator> alloc;
    winrt::com_ptr<ID3D12GraphicsCommandList> list;
    winrt::com_ptr<ID3D12Fence> fence;
    if (FAILED(h.dev->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(alloc.put()))) ||
        FAILED(h.dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, alloc.get(), nullptr, IID_PPV_ARGS(list.put()))) ||
        FAILED(h.dev->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(fence.put()))))
    { g_fg.failed = true; return; }
    list->Close();
    HANDLE event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    if (!event) { g_fg.failed = true; return; }
    UINT64 value = 0, previous = 0, shown = 0;
    auto report = std::chrono::steady_clock::now();
    // Where the presenter's time goes, for the phase profiler (NS_PHASE=1):
    // the copy wait runs from ExecuteCommandLists to the copy's fence, so it
    // holds whatever the queue had to finish first; the vblank wait is the
    // compositor's latency waitable. A generated frame is "late" when its
    // slot had passed before it could be shown, "superseded" when a newer
    // real frame arrived first. Counted always, logged only with the profiler.
    struct PresentStat { double sum = 0.0, max = 0.0; unsigned n = 0; };
    PresentStat copy_gen, copy_real, vblank;
    unsigned copy_bins[6] = {}, planned = 0, shown_gen = 0, late = 0,
             superseded = 0, flat = 0, skipped = 0;
    double last_copy_ms = 0.0;
    auto add = [](PresentStat &s, double ms) { s.sum += ms; if (ms > s.max) s.max = ms; ++s.n; };
    auto bin_of = [](double ms) {
        return ms < 1.0 ? 0 : ms < 2.0 ? 1 : ms < 4.0 ? 2 : ms < 8.0 ? 3 : ms < 16.0 ? 4 : 5;
    };
    auto wait_vblank = [&](DWORD ms) {
        const auto t = std::chrono::steady_clock::now();
        const DWORD r = WaitForSingleObject(g_fg_waitable, ms);
        add(vblank, std::chrono::duration<double, std::milli>(
                        std::chrono::steady_clock::now() - t).count());
        return r;
    };
    // R11: when the swapchain gave us a frame-latency waitable object, the
    // compositor paces us: waiting on it releases one back buffer one
    // vblank before the previous frame hits the screen. The first wait
    // returns immediately (documented), so it is consumed here - from then
    // on every loop iteration waits for the release before presenting,
    // and the wall-clock deadlines become a second-order hint rather than
    // the pacing source. Without the waitable (pre-8.1, blocked QI) the
    // old wall-clock deadlines stay.
    if (g_fg_waitable != nullptr)
        WaitForSingleObject(g_fg_waitable, 2000);
    auto present = [&](ID3D12Resource *source) {
        winrt::com_ptr<ID3D12Resource> bb;
        HRESULT hr = g_present_swap->GetBuffer(
            g_present_swap->GetCurrentBackBufferIndex(), IID_PPV_ARGS(bb.put()));
        if (FAILED(hr) || bb.get() == nullptr)
            return FailGpuWork("fg-present", "get-buffer-error",
                               FAILED(hr) ? hr : E_POINTER);
        hr = alloc->Reset();
        if (FAILED(hr)) return FailGpuWork("fg-present", "allocator-error", hr);
        hr = list->Reset(alloc.get(), nullptr);
        if (FAILED(hr)) return FailGpuWork("fg-present", "command-error", hr);
        auto pre = Transition(bb.get(), D3D12_RESOURCE_STATE_PRESENT, D3D12_RESOURCE_STATE_COPY_DEST);
        list->ResourceBarrier(1, &pre);
        list->CopyResource(bb.get(), source);
        auto post = Transition(bb.get(), D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_PRESENT);
        list->ResourceBarrier(1, &post);
        hr = list->Close();
        if (FAILED(hr)) return FailGpuWork("fg-present", "command-error", hr);
        ID3D12CommandList *commands[] = {list.get()};
        const auto t_copy = std::chrono::steady_clock::now();
        PresentQueue()->ExecuteCommandLists(1, commands);
        hr = PresentQueue()->Signal(fence.get(), ++value);
        if (FAILED(hr))
        {
            bb.detach();
            return FailGpuWork("fg-present-fence", "fence-error", hr);
        }
        hr = fence->SetEventOnCompletion(value, event);
        if (FAILED(hr))
        {
            bb.detach();
            return FailGpuWork("fg-present-fence", "fence-error", hr);
        }
        const DWORD waited = WaitForSingleObject(event, 2000);
        if (waited != WAIT_OBJECT_0)
        {
            bb.detach();
            return FailGpuWork("fg-present-fence",
                               waited == WAIT_TIMEOUT ? "fence-timeout" : "fence-error",
                               waited == WAIT_TIMEOUT ? HRESULT_FROM_WIN32(WAIT_TIMEOUT)
                                                      : HRESULT_FROM_WIN32(GetLastError()));
        }
        if (fence->GetCompletedValue() == UINT64_MAX)
        {
            bb.detach();
            return FailGpuWork("fg-present-fence", "fence-error",
                               DXGI_ERROR_DEVICE_REMOVED);
        }
        if (fence->GetCompletedValue() < value)
        {
            bb.detach();
            return FailGpuWork("fg-present-fence", "fence-error", E_FAIL);
        }
        last_copy_ms = std::chrono::duration<double, std::milli>(
            std::chrono::steady_clock::now() - t_copy).count();
        const HRESULT pr = g_present_swap->Present(0, 0);
        if (pr == DXGI_STATUS_MODE_CHANGED)
        {
            // The mode changed under us. The present did not happen; treat
            // it as a benign skip - the pipeline's resize path rebuilds the
            // swapchain when the size follows, and the next frame presents
            // normally. Presenting into the old surface until then is what
            // froze the overlay black (v1.10-review H2).
            Log("[fg] present reports a mode change - skipping a frame");
            return true;
        }
        if (pr == DXGI_STATUS_OCCLUDED)
        {
            // The window is hidden (minimised target): benign.
            return true;
        }
        if (FAILED(pr)) return FailGpuWork("present", "present-error", pr);
        RevealOnFirstPresent();
        ++shown;
        return true;
    };
    while (!g_fg.stop && !g_fg.failed)
    {
        FgSlot *chosen = nullptr;
        {
            std::unique_lock<std::mutex> lock(g_fg.mutex);
            g_fg.wake.wait(lock, [] {
                if (g_fg.stop) return true;
                for (auto &s : g_fg.slots) if (s.state == 2) return true;
                return false;
            });
            if (g_fg.stop) break;
            for (auto &s : g_fg.slots)
                if (s.state == 2 && (!chosen || s.sequence > chosen->sequence)) chosen = &s;
            for (auto &s : g_fg.slots) if (s.state == 2 && &s != chosen) { s.state = 0; ++skipped; }
            chosen->state = 3;
        }
        const auto start = std::chrono::steady_clock::now();
        // If a frame was dropped, do not interpolate against an unseen frame.
        if (chosen->interpolate && chosen->sequence == previous + 1)
        {
            planned += chosen->count;
            for (unsigned index = 0; index < chosen->count && !g_fg.stop && !g_fg.failed; ++index)
            {
                const auto deadline = start + std::chrono::duration<double>(
                    chosen->interval * (index + 1) / (chosen->count + 1));
                // A delayed GPU copy must not cause a burst of obsolete generated frames.
                if (std::chrono::steady_clock::now() >= deadline) { ++late; continue; }
                if (g_fg_waitable != nullptr)
                {
                    // The compositor's pacing: wait for the back buffer to
                    // be released instead of sleeping to a wall-clock
                    // deadline that drifts against the vblank.
                    if (wait_vblank(2000) != WAIT_OBJECT_0)
                        Log("[fg] waitable timeout - the compositor stalled");
                }
                if (!present(chosen->interpolated[index].get())) g_fg.failed = true;
                else { ++shown_gen; add(copy_gen, last_copy_ms); ++copy_bins[bin_of(last_copy_ms)]; }
                std::unique_lock<std::mutex> lock(g_fg.mutex);
                if (g_fg.wake.wait_until(lock, deadline, [&] {
                    if (g_fg.stop) return true;
                    for (auto &slot : g_fg.slots)
                        if (slot.state == 2 && slot.sequence > chosen->sequence) return true;
                    return false;
                })) { superseded += chosen->count - index - 1; break; }
            }
        }
        else ++flat;
        if (!g_fg.stop && !g_fg.failed)
        {
            if (g_fg_waitable != nullptr
                && wait_vblank(2000) != WAIT_OBJECT_0)
                Log("[fg] waitable timeout on the real frame");
            if (!present(chosen->real.get())) g_fg.failed = true;
            else add(copy_real, last_copy_ms);
        }
        previous = chosen->sequence;
        {
            std::lock_guard<std::mutex> lock(g_fg.mutex);
            chosen->state = 0;
        }
        const double elapsed = std::chrono::duration<double>(start - report).count();
        if (elapsed >= 2.0)
        {
            // The "[fg] displayed" prefix is a contract: settings_io parses it
            // for the HUD counter. The step is appended, not prefixed, so that
            // parser keeps working and the line still names the multiplier it
            // really ran at (a silent clamp used to claim a step that never
            // ran).
            Log("[fg] displayed %.1f FPS (real + generated, %ux); experimental flat-depth guides",
                shown / elapsed, chosen->count + 1);
            if (PhaseEnabled())
                Log("[phase] fg presenter: copy wait generated %.2f/%.2f real %.2f/%.2f ms | "
                    "generated by copy wait <1=%u 1-2=%u 2-4=%u 4-8=%u 8-16=%u 16+=%u | "
                    "vblank wait %.2f/%.2f ms | generated shown %u of %u (late %u, "
                    "superseded %u) | real skipped %u, without generated %u (mean/max)",
                    copy_gen.n ? copy_gen.sum / copy_gen.n : 0.0, copy_gen.max,
                    copy_real.n ? copy_real.sum / copy_real.n : 0.0, copy_real.max,
                    copy_bins[0], copy_bins[1], copy_bins[2], copy_bins[3], copy_bins[4],
                    copy_bins[5], vblank.n ? vblank.sum / vblank.n : 0.0, vblank.max,
                    shown_gen, planned, late, superseded, skipped, flat);
            copy_gen = PresentStat{}; copy_real = PresentStat{}; vblank = PresentStat{};
            for (auto &b : copy_bins) b = 0;
            planned = shown_gen = late = superseded = flat = skipped = 0;
            shown = 0; report = start;
        }
    }
    CloseHandle(event);
    if (g_fg.failed) Log("[fg] presenter failed; returning to ordinary output");
}

static bool EnsureFg(VideoState &v, DXGI_FORMAT format)
{
    const UINT w = v.upscale ? v.full_w : v.w, height = v.upscale ? v.full_h : v.hgt;
    if (g_fg.feature && g_fg.w == w && g_fg.height == height && g_fg.mw == v.w &&
        g_fg.mh == v.hgt && g_fg.format == format && g_fg.thread.joinable()) return true;
    CloseFgResources();
    if (!g_fg.module)
    {
        // The BYO library folder next to the worker, then the worker's own
        // directory: users drop nvngx_dlssg.dll into native\libraries\ to
        // pick the build they want, and native\ stays the bundled fallback.
        wchar_t path[MAX_PATH] = {};
        GetModuleFileNameW(nullptr, path, MAX_PATH);
        if (auto slash = wcsrchr(path, L'\\')) *(slash + 1) = 0;
        wchar_t directory[MAX_PATH]; wcscpy_s(directory, path);
        wchar_t libraries[MAX_PATH]; wcscpy_s(libraries, directory);
        wcscat_s(libraries, L"libraries\\");
        wchar_t lib_path[MAX_PATH]; wcscpy_s(lib_path, libraries);
        wcscat_s(lib_path, L"nvngx_dlssg.dll");
        if (GetFileAttributesW(lib_path) != INVALID_FILE_ATTRIBUTES)
        {
            // The BYO file is verified (NVIDIA signature, machine-root
            // chain, product name) before it is mapped - a writable folder
            // next to the executable is otherwise the easiest DLL plant.
            if (NsGateByoDll(lib_path, "nvngx_dlssg.dll"))
            {
                g_fg.module = LoadLibraryW(lib_path);
                if (g_fg.module)
                    Log("[fg] FG runtime from native\\libraries\\ (BYO, "
                        "verified NVIDIA signature)");
            }
            else
                Log("[fg] BYO refused: nvngx_dlssg.dll is not a "
                    "NVIDIA-signed runtime - falling back to the bundled "
                    "copy (%ls)", lib_path);
        }
        if (!g_fg.module)
        {
            wcscpy_s(path, directory);
            wcscat_s(path, L"nvngx_dlssg.dll");
            g_fg.module = LoadLibraryW(path);
        }
        if (!g_fg.module) { Log("[fg] nvngx_dlssg.dll load failed: %lu", GetLastError()); return false; }
        auto init = reinterpret_cast<PFN_NR_InitExt>(GetProcAddress(g_fg.module, "NVSDK_NGX_D3D12_Init_Ext"));
        g_fg.create = reinterpret_cast<PFN_NR_Create>(GetProcAddress(g_fg.module, "NVSDK_NGX_D3D12_CreateFeature"));
        g_fg.release = reinterpret_cast<PFN_NR_Release>(GetProcAddress(g_fg.module, "NVSDK_NGX_D3D12_ReleaseFeature"));
        g_fg_evaluate = reinterpret_cast<PFN_NR_Evaluate>(GetProcAddress(g_fg.module, "NVSDK_NGX_D3D12_EvaluateFeature"));
        if (!init || !g_fg.create || !g_fg.release || !g_fg_evaluate ||
            NVSDK_NGX_FAILED(NVSDK_NGX_D3D12_AllocateParameters(&g_fg.params))) return false;
        const auto result = init(0x1000000ULL, directory, h.dev, NVSDK_NGX_Version_API, g_fg.params);
        Log("[fg] Init_Ext -> 0x%08X", result);
        if (NVSDK_NGX_FAILED(result)) return false;
        // Ask the runtime what it says its own multiplier ceiling is, and log
        // it. DLSSG.MultiFrameCountMax is 3 for 4x and 1 for a card that stops
        // at 2x. This is REPORT ONLY: the request is not clamped by it, since
        // an unverified capability answer must not silently cost a working
        // step. What adapts is FgStepDown, driven by a refusal this run really
        // got. A log from a 2x card then names the reason on the first attempt
        // instead of leaving the two refused steps unexplained.
        NVSDK_NGX_Parameter *caps = nullptr;
        if (!NVSDK_NGX_FAILED(NVSDK_NGX_D3D12_GetCapabilityParameters(&caps)) && caps != nullptr)
        {
            unsigned reported = 0;
            if (!NVSDK_NGX_FAILED(caps->Get(NVSDK_NGX_DLSSG_Parameter_MultiFrameCountMax, &reported)) &&
                reported >= 1u)
            {
                g_fg_cap_reported = std::min(3u, reported);
                Log("[fg] the runtime reports a %ux multiplier ceiling",
                    g_fg_cap_reported + 1);
            }
            NVSDK_NGX_D3D12_DestroyParameters(caps);
        }
    }
    auto p = g_fg.params;
    p->Reset();
    p->Set("CreationNodeMask", 1u); p->Set("VisibilityNodeMask", 1u);
    p->Set("Width", w); p->Set("Height", height);
    p->Set(NVSDK_NGX_DLSSG_Parameter_BackbufferFormat, (unsigned)format);
    p->Set(NVSDK_NGX_DLSSG_Parameter_InternalWidth, v.w);
    p->Set(NVSDK_NGX_DLSSG_Parameter_InternalHeight, v.hgt);
    p->Set(NVSDK_NGX_DLSSG_Parameter_DynamicResolution, 0u);
    if (!BeginCommands()) return false;
    const auto result = g_fg.create(h.list, NVSDK_NGX_Feature_FrameGeneration, p, &g_fg.feature);
    if (!WaitFenceValue(h.fence, EndCommands(), 30000) || NVSDK_NGX_FAILED(result) || !g_fg.feature)
    {
        Log("[fg] CreateFeature failed 0x%08X", result);
        // The frame that met the refusal is a retry, not a dead feature: mark
        // it so the caller lets the next header rebuild at the lower step
        // instead of turning Frame Generation off for this GPU.
        g_fg_retry = FgStepDown(result);
        return false;
    }
    if (!CreateVideoTex(g_fg.depth, v.w, v.hgt, DXGI_FORMAT_R32_FLOAT, v.w * 4)) return false;
    std::vector<float> depth(static_cast<size_t>(v.w) * v.hgt, 0.5f);
    if (!FillUpload(g_fg.depth, reinterpret_cast<BYTE *>(depth.data()), v.w * 4, v.hgt) || !BeginCommands()) return false;
    CopyUpload(h.list, g_fg.depth);
    auto b = Transition(g_fg.depth.tex, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    h.list->ResourceBarrier(1, &b);
    if (!WaitFenceValue(h.fence, EndCommands(), 30000)) return false;
    for (unsigned i = 0; i < g_fg_count; ++i)
    {
        g_fg.output[i].attach(MakeTex(w, height, format, true));
        if (!g_fg.output[i]) return false;
    }
    for (auto &slot : g_fg.slots)
    {
        slot.real.attach(MakeTex(w, height, format, false));
        if (!slot.real) return false;
        for (unsigned i = 0; i < g_fg_count; ++i)
        {
            slot.interpolated[i].attach(MakeTex(w, height, format, true));
            if (!slot.interpolated[i]) return false;
        }
    }
    if (!BeginCommands()) return false;
    for (unsigned i = 0; i < g_fg_count; ++i)
    {
        auto to_copy = Transition(g_fg.output[i].get(), D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_SOURCE);
        h.list->ResourceBarrier(1, &to_copy);
    }
    for (auto &slot : g_fg.slots) for (unsigned i = 0; i <= g_fg_count; ++i)
    {
        auto texture = i ? slot.interpolated[i - 1].get() : slot.real.get();
        auto to_copy = Transition(texture, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_SOURCE);
        h.list->ResourceBarrier(1, &to_copy);
    }
    if (!WaitFenceValue(h.fence, EndCommands(), 30000)) return false;
    D3D12_RESOURCE_DESC buffer = {};
    buffer.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    buffer.Width = 12; buffer.Height = 1; buffer.DepthOrArraySize = 1;
    buffer.MipLevels = 1; buffer.SampleDesc.Count = 1;
    buffer.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    buffer.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
    D3D12_HEAP_PROPERTIES heap = {}; heap.Type = D3D12_HEAP_TYPE_DEFAULT;
    if (FAILED(h.dev->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &buffer,
        D3D12_RESOURCE_STATE_UNORDERED_ACCESS, nullptr, IID_PPV_ARGS(g_fg.disable.put())))) return false;
    heap.Type = D3D12_HEAP_TYPE_READBACK; buffer.Flags = D3D12_RESOURCE_FLAG_NONE;
    if (FAILED(h.dev->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &buffer,
        D3D12_RESOURCE_STATE_COPY_DEST, nullptr, IID_PPV_ARGS(g_fg.disable_readback.put())))) return false;
    g_fg.w = w; g_fg.height = height; g_fg.mw = v.w; g_fg.mh = v.hgt; g_fg.format = format;
    g_fg.stop = false;
    // R11 refined: latency 1 belongs to the FG presenter while it owns the
    // present loop. The ordinary NR path presents Present(0,0) per frame and
    // never consumes the waitable - with latency 1 the swapchain would hold
    // a single queued present and every ordinary present would block or drop
    // against the compositor (the fullscreen flicker). The waitable handle
    // is taken here, from the same thread that will wait on it.
    if (g_present_swap != nullptr)
    {
        IDXGISwapChain2 *sc2 = nullptr;
        if (SUCCEEDED(g_present_swap->QueryInterface(
                __uuidof(IDXGISwapChain2), reinterpret_cast<void **>(&sc2)))
            && sc2 != nullptr)
        {
            // Both calls need a swap chain created with
            // DXGI_SWAP_CHAIN_FLAG_FRAME_LATENCY_WAITABLE_OBJECT; without it
            // they refuse and there is no handle. OpenPresent creates the
            // chain with it (and puts the ordinary path back on latency 3);
            // the log still says which pacing actually runs, in case a driver
            // refuses anyway.
            CloseFgWaitable();
            const HRESULT latency = sc2->SetMaximumFrameLatency(1);
            if (SUCCEEDED(latency)) g_fg_waitable = sc2->GetFrameLatencyWaitableObject();
            sc2->Release();
            if (g_fg_waitable != nullptr)
                Log("[fg] the presenter is paced by the compositor (latency 1)");
            else
                Log("[fg] the presenter is paced by the clock: the swap chain has no "
                    "latency waitable (0x%08X)", static_cast<unsigned>(latency));
        }
    }
    g_fg.thread = std::thread(FgPresenter);
    Log("[fg] %ux enabled at %ux%u, format=%u; flat depth and estimated motion (experimental)", g_fg_count + 1, w, height, format);
    return true;
}

// Opt-in pixel regression capture, never enabled in ordinary runs.
static void FgDump(ID3D12Resource *source, D3D12_RESOURCE_STATES state, unsigned index)
{
    char dir[MAX_PATH] = {};
    if (!GetEnvironmentVariableA("NS_FG_DUMP", dir, sizeof(dir)) ||
        (g_fg.sequence != 30 && g_fg.sequence != 150 && g_fg.sequence != 300)) return;
    auto desc = source->GetDesc();
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT fp = {};
    UINT rows; UINT64 rowbytes, bytes;
    h.dev->GetCopyableFootprints(&desc, 0, 1, 0, &fp, &rows, &rowbytes, &bytes);
    D3D12_HEAP_PROPERTIES heap = {}; heap.Type = D3D12_HEAP_TYPE_READBACK;
    D3D12_RESOURCE_DESC bd = {}; bd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    bd.Width = bytes; bd.Height = 1; bd.DepthOrArraySize = bd.MipLevels = 1;
    bd.SampleDesc.Count = 1; bd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    winrt::com_ptr<ID3D12Resource> rb;
    if (FAILED(h.dev->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &bd,
        D3D12_RESOURCE_STATE_COPY_DEST, nullptr, IID_PPV_ARGS(rb.put()))) || !BeginCommands()) return;
    auto pre = Transition(source, state, D3D12_RESOURCE_STATE_COPY_SOURCE);
    if (state != D3D12_RESOURCE_STATE_COPY_SOURCE) h.list->ResourceBarrier(1, &pre);
    D3D12_TEXTURE_COPY_LOCATION from = {}, to = {};
    from.pResource = source; from.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    to.pResource = rb.get(); to.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT; to.PlacedFootprint = fp;
    h.list->CopyTextureRegion(&to, 0, 0, 0, &from, nullptr);
    auto post = Transition(source, D3D12_RESOURCE_STATE_COPY_SOURCE, state);
    if (state != D3D12_RESOURCE_STATE_COPY_SOURCE) h.list->ResourceBarrier(1, &post);
    if (!WaitFenceValue(h.fence, EndCommands(), 30000, "fg-dump"))
    {
        if (g_submission_failed) rb.detach();
        return;
    }
    BYTE *data = nullptr; D3D12_RANGE read = {0, static_cast<SIZE_T>(bytes)}, written = {0, 0};
    if (FAILED(rb->Map(0, &read, reinterpret_cast<void **>(&data)))) return;
    char path[MAX_PATH]; sprintf_s(path, "%s/fg-%llu-%u.raw", dir, g_fg.sequence, index);
    FILE *file = nullptr;
    if (!fopen_s(&file, path, "wb") && file) {
        for (UINT y = 0; y < rows; ++y) fwrite(data + size_t(y) * fp.Footprint.RowPitch, 1, size_t(rowbytes), file);
        fclose(file);
    }
    rb->Unmap(0, &written);
}

static bool FgPresent(VideoState &v, ID3D12Resource *color, D3D12_RESOURCE_STATES state,
                      bool bypass)
{
    if (!FgRequested()) { StopFgPresentation(); return false; }
    if (!EnsureFg(v, color->GetDesc().Format))
    {
        // A refused multiplier stepped the ceiling down: this frame is a
        // retry, not a dead feature. Release what the failed attempt built and
        // let the next header rebuild at the lower step.
        if (g_fg_retry) { g_fg_retry = false; CloseFgResources(); return false; }
        g_fg.failed = true; CloseFgResources(); return false;
    }
    const auto now = std::chrono::steady_clock::now();
    const double interval = g_fg.history ? std::chrono::duration<double>(now - g_fg.last).count() : 0.016;
    const bool interpolate = g_fg.history && !g_fg_reset && interval < 0.1;
    g_fg.last = now;
    auto p = g_fg.params;
    p->Reset();
    NVSDK_NGX_DLSSG_Opt_Eval_Params opt = {};
    for (int i = 0; i < 4; ++i)
        opt.cameraViewToClip[i][i] = opt.clipToCameraView[i][i] = opt.clipToLensClip[i][i] =
        opt.clipToPrevClip[i][i] = opt.prevClipToClip[i][i] = 1.0f;
    opt.mvecScale[0] = 1.f / v.w; opt.mvecScale[1] = 1.f / v.hgt;
    opt.cameraUp[1] = opt.cameraRight[0] = opt.cameraFwd[2] = 1;
    opt.cameraNear = 0.1f; opt.cameraFar = 1000; opt.cameraFOV = 1;
    opt.cameraAspectRatio = float(g_fg.w) / g_fg.height;
    opt.cameraMotionIncluded = opt.orthoProjection = opt.motionVectorsDilated = true;
    opt.colorBuffersHDR = g_fg.format == DXGI_FORMAT_R10G10B10A2_UNORM;
    opt.reset = !interpolate;
    opt.mvecsSubrectSize = opt.depthSubrectSize = {v.w, v.hgt};
    opt.backbufferSubrectSize = opt.outputInterpSubrectSize = {g_fg.w, g_fg.height};
    NVSDK_NGX_D3D12_DLSSG_Eval_Params ep = {};
    ep.pBackbuffer = color; ep.pMVecs = v.mv.tex; ep.pDepth = g_fg.depth.tex;
    if (!BeginCommands()) { g_fg.failed = true; CloseFgResources(); return false; }
    const bool profile_fg = ProfileGpuBegin(PS_FG);
    bool allow_interpolation = true;
    p->Set(NVSDK_NGX_DLSSG_Parameter_BackbufferFrameID, static_cast<unsigned long long>(g_fg.sequence + 1));
    opt.multiFrameCount = g_fg_count;
    for (unsigned index = 0; index < g_fg_count; ++index)
    {
        opt.multiFrameIndex = index + 1;
        ep.pOutputInterpFrame = g_fg.output[index].get();
        ep.pOutputDisableInterpolation = g_fg.disable.get();
        auto pre = Transition(color, state, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        if (state != D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE) h.list->ResourceBarrier(1, &pre);
        auto out_pre = Transition(g_fg.output[index].get(), D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        h.list->ResourceBarrier(1, &out_pre);
        const auto result = NGX_D3D12_EVALUATE_DLSSG(h.list, g_fg.feature, p, &ep, &opt);
        auto disable_pre = Transition(g_fg.disable.get(), D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE);
        h.list->ResourceBarrier(1, &disable_pre);
        h.list->CopyBufferRegion(g_fg.disable_readback.get(), index * 4, g_fg.disable.get(), 0, 4);
        auto disable_post = Transition(g_fg.disable.get(), D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        h.list->ResourceBarrier(1, &disable_post);
        auto out_post = Transition(g_fg.output[index].get(), D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE);
        h.list->ResourceBarrier(1, &out_post);
        auto post = Transition(color, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, state);
        if (state != D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE) h.list->ResourceBarrier(1, &post);
        if (NVSDK_NGX_FAILED(result))
        {
            if (profile_fg) ProfileGpuEnd(PS_FG);
            if (!ProfileWait(PS_FG, EndCommands(), 30000,
                                "fg-evaluate"))
            { g_fg.failed = true; return false; }
            // A refused multiplier is not a broken feature, and it used to be
            // treated as one: FG died at every count, including on a card that
            // runs 2x perfectly well. Step down; 2x is the floor, so a refusal
            // there does mean Frame Generation cannot run on this GPU. No
            // retry flag here: the create already succeeded, so the next frame
            // is an ordinary fresh build at the lower step and its own failure
            // must read as one.
            if (FgStepDown(result)) { CloseFgResources(); return false; }
            Log("[fg] Evaluate failed 0x%08X; falling back", result);
            g_fg.failed = true; CloseFgResources(); return false;
        }
    }
    if (profile_fg) ProfileGpuEnd(PS_FG);
    FgSlot *slot = nullptr;
    {
        std::lock_guard<std::mutex> lock(g_fg.mutex);
        for (auto &s : g_fg.slots) if (s.state == 0) { slot = &s; break; }
        if (!slot) for (auto &s : g_fg.slots) if (s.state == 2) { slot = &s; break; }
        if (!slot)
        {
            // Starvation: every slot is mid-flight. The submitted work still
            // ends here - record its token, or PresentFrame hands the defer
            // contract a STALE g_fg_present_fence from an earlier frame and
            // the token-order guard kills the worker (the blink-out class).
            const UINT64 fence = EndCommands();
            if (!ProfileWait(PS_FG, fence, 30000, "fg-starvation"))
            { g_fg.failed = true; return false; }
            g_fg_present_fence = fence;
            return true;
        }
        slot->state = 1;
    }
    // The reserved slot is invisible to the presenter until the fence completes.
    // Hand it the generated textures and reuse its old buffers next frame.
    for (unsigned i = 0; i < g_fg_count; ++i)
        std::swap(g_fg.output[i], slot->interpolated[i]);
    {
        auto source = color;
        auto destination = slot->real.get();
        const auto rest = state;
        D3D12_RESOURCE_BARRIER before[] = {
            Transition(source, rest, D3D12_RESOURCE_STATE_COPY_SOURCE),
            Transition(destination, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_COPY_DEST)};
        h.list->ResourceBarrier(2, before);
        h.list->CopyResource(destination, source);
        D3D12_RESOURCE_BARRIER after[] = {
            Transition(source, D3D12_RESOURCE_STATE_COPY_SOURCE, rest),
            Transition(destination, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_COPY_SOURCE)};
        h.list->ResourceBarrier(2, after);
    }
    // Export follows the source the viewer sees: in bypass that is the raw
    // capture, not the neural result, which on this path is a frame the screen
    // is not showing at all.
    ID3D12Resource *export_src = bypass ? v.color.tex : v.output;
    const auto export_rest = bypass ? D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE
                                    : D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
    auto spout_pre = Transition(export_src, export_rest, D3D12_RESOURCE_STATE_COPY_SOURCE);
    h.list->ResourceBarrier(1, &spout_pre);
    ExportCopy(h.list, export_src, g_fg.w, g_fg.height);
    auto spout_post = Transition(export_src, D3D12_RESOURCE_STATE_COPY_SOURCE, export_rest);
    h.list->ResourceBarrier(1, &spout_post);
    const UINT64 fg_fence = EndCommands();
    if (!ProfileWait(PS_FG, fg_fence, 30000, "fg-evaluate"))
    { g_fg.failed = true; CloseFgResources(); return false; }
    g_fg_present_fence = fg_fence;
    BYTE *disabled = nullptr;
    D3D12_RANGE read = {0, g_fg_count * 4}, written = {0, 0};
    if (SUCCEEDED(g_fg.disable_readback->Map(0, &read, reinterpret_cast<void **>(&disabled))))
    {
        for (unsigned i = 0; i < g_fg_count; ++i)
            allow_interpolation = allow_interpolation && disabled[i * 4] == 0;
        g_fg.disable_readback->Unmap(0, &written);
    }
    else allow_interpolation = false;
    FgDump(color, state, 0);
    for (unsigned i = 0; i < g_fg_count; ++i) FgDump(slot->interpolated[i].get(), D3D12_RESOURCE_STATE_COPY_SOURCE, i + 1);
    SpoutBridgeSend();
    {
        std::lock_guard<std::mutex> lock(g_fg.mutex);
        slot->sequence = ++g_fg.sequence;
        slot->count = g_fg_count;
        slot->interpolate = interpolate && allow_interpolation;
        slot->interval = std::clamp(interval, 0.004, 0.05);
        slot->state = 2;
    }
    g_fg.history = true;
    g_fg.wake.notify_one();
    return true;
}

// Explicit UI settings ride in each frame header, so toggling does not restart NR.
static void ConfigureFgFrame(uint32_t flags)
{
    if (!(flags & 0x800)) return; // older clients may still use NS_FRAMEGEN
    const int enabled = (flags & 0x100) != 0;
    const unsigned requested = std::min(3u, ((flags >> 9) & 3u) + 1u);
    // A step the runtime already refused is not offered again in this FG
    // session: the request is clamped to the last one that worked. Switching
    // FG off and on arms the user's choice again, so a newer stock provider
    // (BYO folder) or a driver update can lift the ceiling without a restart.
    const unsigned count = std::min(requested, g_fg_count_limit);
    if (g_fg_ui_enabled == enabled && g_fg_count == count) return;
    CloseFgResources();
    // A fresh switch-on is a fresh attempt: the ceiling goes back to the DLSS 4
    // maximum and the user's own choice is asked for again. A step this card
    // refused earlier is worth exactly one retry after a provider or driver
    // update, and those do not restart the program.
    if (enabled && g_fg_ui_enabled == 0) g_fg_count_limit = 3;
    g_fg_retry = false;
    g_fg_ui_enabled = enabled;
    g_fg_count = count;
    g_fg.failed = false;
    Log("[fg] UI: %s, %ux%s", enabled ? "on" : "off", count + 1,
        count < requested ? " (capped by the runtime ceiling)" : "");
}
