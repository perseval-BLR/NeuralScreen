// Frame Generation on the FSR backend: RTX 20 (Turing) and RTX 30 (Ampere),
// which have no DLSS Frame Generation. Included by frame_generation.inl; it
// uses that file's FgState, slots, presenter and the host's D3D12 helpers.
//
// The interpolation is AMD's FSR Frame Generation from the FSR SDK. It runs
// on any DX12 GPU (optical flow + interpolation as compute shaders). The
// runtime is two DLLs loaded at run time, nothing is linked:
//   amd_fidelityfx_loader_dx12.dll         the API entry points
//   amd_fidelityfx_framegeneration_dx12.dll the providers (FSR 3.1.x here;
//                                           the FSR 4 ML provider inside it is
//                                           RDNA 4 only and is not selected)
//
// What differs from the DLSS-G path:
//   * One generated frame per real one (2x). The FSR interpolation makes a
//     single frame between two; the multiplier is held at 2x on this backend.
//   * The SDK drives its own swap chain wrapper normally. Here the host owns
//     the presenter, so the context runs with
//     FFX_FRAMEGENERATION_FLAG_NO_SWAPCHAIN_CONTEXT_NOTIFY: Configure, Prepare
//     and Dispatch are called per frame with the same frame id, and the
//     generated image lands in g_fg.output[0] like the DLSS-G one did.
//   * Capture has no engine depth or jitter: the flat depth plane and the
//     estimated motion field are given, the interpolator's own optical flow
//     does the rest. Same limits as the DLSS-G path - UI and occlusion
//     artifacts remain possible (the interface-protection copy still applies).

#include "include/ffx/api/include/ffx_api.h"
#include "include/ffx/api/include/dx12/ffx_api_dx12.h"
#include "include/ffx/framegeneration/include/ffx_framegeneration.h"

static struct FsrFgState {
    HMODULE provider = nullptr, loader = nullptr;
    PfnFfxCreateContext create = nullptr;
    PfnFfxDestroyContext destroy = nullptr;
    PfnFfxConfigure configure = nullptr;
    PfnFfxQuery query = nullptr;
    PfnFfxDispatch dispatch = nullptr;
    ffxContext context = nullptr;
    bool ready = false;
    uint64_t frame_id = 0;
    char version[32] = {};
} g_fsr;

// Guarded calls: a fault inside AMD's runtime (an old driver that faults
// instead of refusing) must end Frame Generation, not the worker. The same
// rule as the NGX calls above; kept free of C++ objects so __try is allowed.
static ffxReturnCode_t FsrCreateGuarded(ffxContext *context, ffxCreateContextDescHeader *desc)
{
    g_fg_fault = 0;
    __try
    {
        if (TestFailureOnce("fg-create-fault"))
            RaiseException(EXCEPTION_ACCESS_VIOLATION, 0, 0, nullptr);
        return g_fsr.create(context, desc, nullptr);
    }
    __except (EXCEPTION_EXECUTE_HANDLER) { g_fg_fault = GetExceptionCode(); }
    return FFX_API_RETURN_ERROR;
}

static ffxReturnCode_t FsrConfigureGuarded(const ffxConfigureDescHeader *desc)
{
    g_fg_fault = 0;
    __try { return g_fsr.configure(&g_fsr.context, desc); }
    __except (EXCEPTION_EXECUTE_HANDLER) { g_fg_fault = GetExceptionCode(); }
    return FFX_API_RETURN_ERROR;
}

static ffxReturnCode_t FsrDispatchGuarded(const ffxDispatchDescHeader *desc)
{
    g_fg_fault = 0;
    __try
    {
        if (TestFailureOnce("fg-evaluate-fault"))
            RaiseException(EXCEPTION_ACCESS_VIOLATION, 0, 0, nullptr);
        return g_fsr.dispatch(&g_fsr.context, desc);
    }
    __except (EXCEPTION_EXECUTE_HANDLER) { g_fg_fault = GetExceptionCode(); }
    return FFX_API_RETURN_ERROR;
}

static ffxReturnCode_t FsrQueryGuarded(ffxQueryDescHeader *desc)
{
    __try { return g_fsr.query(nullptr, desc); }
    __except (EXCEPTION_EXECUTE_HANDLER) { g_fg_fault = GetExceptionCode(); }
    return FFX_API_RETURN_ERROR;
}

static void FsrDestroy()
{
    if (g_fsr.context && g_fsr.destroy)
    {
        // A failed submission may leave the queue owning the context's
        // resources: keep them alive to process teardown, as the rest of the
        // Frame Generation resources are.
        if (!g_submission_failed) g_fsr.destroy(&g_fsr.context, nullptr);
        g_fsr.context = nullptr;
    }
    g_fsr.ready = false;
    g_fsr.frame_id = 0;
}

// The runtime comes from the folder next to the worker only. The bring-your-own
// folder of the NVIDIA runtimes is not used: its signature gate is pinned to
// NVIDIA's signer, and a writable folder is exactly where a planted DLL goes.
static bool FsrLoadRuntime()
{
    if (g_fsr.loader && g_fsr.create) return true;
    wchar_t directory[MAX_PATH] = {};
    GetModuleFileNameW(nullptr, directory, MAX_PATH);
    if (auto slash = wcsrchr(directory, L'\\')) *(slash + 1) = 0;
    wchar_t provider[MAX_PATH], loader[MAX_PATH];
    wcscpy_s(provider, directory); wcscat_s(provider, L"amd_fidelityfx_framegeneration_dx12.dll");
    wcscpy_s(loader, directory);   wcscat_s(loader,   L"amd_fidelityfx_loader_dx12.dll");
    if (GetFileAttributesW(loader) == INVALID_FILE_ATTRIBUTES ||
        GetFileAttributesW(provider) == INVALID_FILE_ATTRIBUTES)
    {
        Log("[fg] FSR runtime missing: amd_fidelityfx_loader_dx12.dll and "
            "amd_fidelityfx_framegeneration_dx12.dll must sit next to the worker");
        return false;
    }
    // The provider first, by full path: the loader then finds it already
    // mapped under its own name instead of searching for it.
    g_fsr.provider = LoadLibraryW(provider);
    if (!g_fsr.provider)
    {
        Log("[fg] FSR provider load failed: %lu", GetLastError());
        return false;
    }
    g_fsr.loader = LoadLibraryW(loader);
    if (!g_fsr.loader)
    {
        Log("[fg] FSR loader load failed: %lu", GetLastError());
        FreeLibrary(g_fsr.provider); g_fsr.provider = nullptr;
        return false;
    }
    g_fsr.create = reinterpret_cast<PfnFfxCreateContext>(GetProcAddress(g_fsr.loader, "ffxCreateContext"));
    g_fsr.destroy = reinterpret_cast<PfnFfxDestroyContext>(GetProcAddress(g_fsr.loader, "ffxDestroyContext"));
    g_fsr.configure = reinterpret_cast<PfnFfxConfigure>(GetProcAddress(g_fsr.loader, "ffxConfigure"));
    g_fsr.query = reinterpret_cast<PfnFfxQuery>(GetProcAddress(g_fsr.loader, "ffxQuery"));
    g_fsr.dispatch = reinterpret_cast<PfnFfxDispatch>(GetProcAddress(g_fsr.loader, "ffxDispatch"));
    if (!g_fsr.create || !g_fsr.destroy || !g_fsr.configure || !g_fsr.query || !g_fsr.dispatch)
    {
        Log("[fg] the FSR loader is missing an entry point");
        FreeLibrary(g_fsr.loader); g_fsr.loader = nullptr;
        FreeLibrary(g_fsr.provider); g_fsr.provider = nullptr;
        g_fsr.create = nullptr;
        return false;
    }
    return true;
}

// Creates the interpolation context at the display size. False leaves FG off
// for this session (the caller marks it failed), with the reason logged.
static bool FsrCreate(VideoState &v, UINT w, UINT height, DXGI_FORMAT format)
{
    if (!FsrLoadRuntime()) return false;

    // The FSR 3.x provider, by name. The runtime also carries the FSR 4 ML
    // provider, which needs RDNA 4 and would be picked first on a card that
    // could run it; this backend is for NVIDIA cards, so the choice is
    // explicit instead of left to the loader's ordering.
    uint64_t version_id = 0;
    bool have_version = false;
    {
        uint64_t count = 0;
        ffxQueryDescGetVersions query = {};
        query.header.type = FFX_API_QUERY_DESC_TYPE_GET_VERSIONS;
        query.createDescType = FFX_API_CREATE_CONTEXT_DESC_TYPE_FRAMEGENERATION;
        query.device = h.dev;
        query.outputCount = &count;
        if (FsrQueryGuarded(&query.header) == FFX_API_RETURN_OK && count > 0)
        {
            std::vector<uint64_t> ids(static_cast<size_t>(count));
            std::vector<const char *> names(static_cast<size_t>(count));
            uint64_t capacity = count;
            query.outputCount = &capacity;
            query.versionIds = ids.data();
            query.versionNames = names.data();
            if (FsrQueryGuarded(&query.header) == FFX_API_RETURN_OK)
            {
                for (uint64_t i = 0; i < capacity && i < ids.size(); ++i)
                {
                    const char *name = names[i] ? names[i] : "?";
                    Log("[fg] FSR provider available: %s", name);
                    if (!have_version && name[0] == '3')
                    {
                        version_id = ids[i];
                        have_version = true;
                        strncpy_s(g_fsr.version, name, _TRUNCATE);
                    }
                }
            }
        }
    }
    if (!have_version)
    {
        Log("[fg] the FSR runtime has no 3.x frame generation provider");
        return false;
    }

    ffxCreateContextDescFrameGeneration create = {};
    create.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_FRAMEGENERATION;
    create.flags = (format == DXGI_FORMAT_R10G10B10A2_UNORM) ? FFX_FRAMEGENERATION_ENABLE_HIGH_DYNAMIC_RANGE : 0u;
    create.displaySize = {w, height};
    create.maxRenderSize = {v.w, v.hgt};
    create.backBufferFormat = ffxApiGetSurfaceFormatDX12(format);

    ffxCreateContextDescFrameGenerationVersion api_version = {};
    api_version.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_FRAMEGENERATION_VERSION;
    api_version.version = FFX_FRAMEGENERATION_VERSION;

    ffxOverrideVersion override_version = {};
    override_version.header.type = FFX_API_DESC_TYPE_OVERRIDE_VERSION;
    override_version.versionId = version_id;

    ffxCreateBackendDX12Desc backend = {};
    backend.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_BACKEND_DX12;
    backend.device = h.dev;

    create.header.pNext = &api_version.header;
    api_version.header.pNext = &override_version.header;
    override_version.header.pNext = &backend.header;

    const ffxReturnCode_t result = FsrCreateGuarded(&g_fsr.context, &create.header);
    if (g_fg_fault != 0)
    {
        g_fsr.context = nullptr;
        ReportFailure("fg-create", "seh", static_cast<HRESULT>(g_fg_fault));
        Log("[fg] FSR CreateContext raised 0x%08X (caught) - Frame Generation off", g_fg_fault);
        return false;
    }
    if (result != FFX_API_RETURN_OK || !g_fsr.context)
    {
        g_fsr.context = nullptr;
        Log("[fg] FSR CreateContext failed %u", static_cast<unsigned>(result));
        return false;
    }
    g_fsr.frame_id = 0;
    g_fsr.ready = true;
    Log("[fg] FSR Frame Generation %s created at %ux%u (render %ux%u), format=%u",
        g_fsr.version, w, height, v.w, v.hgt, static_cast<unsigned>(format));
    return true;
}

// Records the interpolation of one real frame into g_fg.output[0]. The caller
// has the command list open and owns the surrounding barriers for `color`;
// `interpolate` false means a reset (no usable previous frame). Returns false
// on failure (the caller turns Frame Generation off for the session).
static bool FsrEvaluate(VideoState &v, ID3D12Resource *color, bool interpolate, double interval)
{
    const uint64_t frame = ++g_fsr.frame_id;
    const UINT w = g_fg.w, height = g_fg.height;
    const bool hdr = g_fg.format == DXGI_FORMAT_R10G10B10A2_UNORM;

    ffxConfigureDescFrameGeneration configure = {};
    configure.header.type = FFX_API_CONFIGURE_DESC_TYPE_FRAMEGENERATION;
    configure.frameGenerationEnabled = true;
    configure.allowAsyncWorkloads = false;
    configure.flags = FFX_FRAMEGENERATION_FLAG_NO_SWAPCHAIN_CONTEXT_NOTIFY;
    configure.onlyPresentGenerated = false;
    configure.generationRect = {0, 0, static_cast<int32_t>(w), static_cast<int32_t>(height)};
    configure.frameID = frame;
    ffxReturnCode_t result = FsrConfigureGuarded(&configure.header);

    if (result == FFX_API_RETURN_OK)
    {
        ffxDispatchDescFrameGenerationPrepareV2 prepare = {};
        prepare.header.type = FFX_API_DISPATCH_DESC_TYPE_FRAMEGENERATION_PREPARE_V2;
        prepare.frameID = frame;
        prepare.flags = 0;
        prepare.commandList = h.list;
        prepare.renderSize = {v.w, v.hgt};
        prepare.jitterOffset = {0.0f, 0.0f};
        // The motion field is in pixels of the render size.
        prepare.motionVectorScale = {1.0f, 1.0f};
        prepare.frameTimeDelta = static_cast<float>(interval * 1000.0);
        prepare.reset = !interpolate;
        prepare.cameraNear = 0.1f;
        prepare.cameraFar = 1000.0f;
        prepare.cameraFovAngleVertical = 1.0f;
        prepare.viewSpaceToMetersFactor = 1.0f;
        prepare.depth = ffxApiGetResourceDX12(g_fg.depth.tex, FFX_API_RESOURCE_STATE_COMPUTE_READ);
        prepare.motionVectors = ffxApiGetResourceDX12(v.mv.tex, FFX_API_RESOURCE_STATE_COMPUTE_READ);
        prepare.cameraUp[1] = prepare.cameraRight[0] = prepare.cameraForward[2] = 1.0f;
        result = FsrDispatchGuarded(&prepare.header);
    }
    if (result == FFX_API_RETURN_OK)
    {
        ffxDispatchDescFrameGeneration dispatch = {};
        dispatch.header.type = FFX_API_DISPATCH_DESC_TYPE_FRAMEGENERATION;
        dispatch.commandList = h.list;
        dispatch.presentColor = ffxApiGetResourceDX12(color, FFX_API_RESOURCE_STATE_COMPUTE_READ);
        dispatch.outputs[0] = ffxApiGetResourceDX12(g_fg.output[0].get(),
                                                    FFX_API_RESOURCE_STATE_UNORDERED_ACCESS);
        dispatch.numGeneratedFrames = 1;
        dispatch.reset = !interpolate;
        // The edited result reaches FG as HDR10 PQ; everything else is the
        // display's sRGB-encoded SDR picture.
        dispatch.backbufferTransferFunction = hdr ? FFX_API_BACKBUFFER_TRANSFER_FUNCTION_PQ
                                                  : FFX_API_BACKBUFFER_TRANSFER_FUNCTION_SRGB;
        dispatch.minMaxLuminance[0] = 0.0f;
        dispatch.minMaxLuminance[1] = hdr ? 1000.0f : 80.0f;
        dispatch.generationRect = {0, 0, static_cast<int32_t>(w), static_cast<int32_t>(height)};
        dispatch.frameID = frame;
        result = FsrDispatchGuarded(&dispatch.header);
    }
    if (g_fg_fault != 0)
    {
        AbortCommands();
        ReportFailure("fg-evaluate", "seh", static_cast<HRESULT>(g_fg_fault));
        Log("[fg] FSR dispatch raised 0x%08X (caught) - Frame Generation off", g_fg_fault);
        return false;
    }
    if (result != FFX_API_RETURN_OK)
    {
        // Never execute a list the runtime may have left half-recorded.
        AbortCommands();
        Log("[fg] FSR dispatch failed %u; falling back", static_cast<unsigned>(result));
        return false;
    }
    return true;
}
