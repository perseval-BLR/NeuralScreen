// Included after the capture bridge. All resources here belong to the worker's
// D3D12 device and are used on its fence-serialized command queue; only the
// copy into the back buffer runs on the present queue (CopyToBackBuffer).
static ID3D12RootSignature *g_hdr_rs = nullptr;
static ID3D12PipelineState *g_hdr_pso = nullptr;
static ID3D12DescriptorHeap *g_hdr_heap = nullptr;
static ID3D12Resource *g_hdr_output = nullptr;
// An HDR10 recording's frame when Frame Generation is off: g_hdr_output is
// FP16 scRGB then, and the recorder takes 10-bit PQ BT.2020.
static ID3D12Resource *g_rec_pq = nullptr;

static void CloseHdrResources()
{
    if (g_rec_pq) { g_rec_pq->Release(); g_rec_pq = nullptr; }
    if (g_hdr_output) { g_hdr_output->Release(); g_hdr_output = nullptr; }
    if (g_hdr_heap) { g_hdr_heap->Release(); g_hdr_heap = nullptr; }
    if (g_hdr_pso) { g_hdr_pso->Release(); g_hdr_pso = nullptr; }
    if (g_hdr_rs) { g_hdr_rs->Release(); g_hdr_rs = nullptr; }
}

static bool EnsurePresentFormat(bool hdr, bool pq)
{
    DXGI_SWAP_CHAIN_DESC1 desc = {};
    if (FAILED(g_present_swap->GetDesc1(&desc))) return false;
    const auto format = pq ? DXGI_FORMAT_R10G10B10A2_UNORM : hdr ? DXGI_FORMAT_R16G16B16A16_FLOAT : DXGI_FORMAT_R8G8B8A8_UNORM;
    const auto space = pq ? DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020 : hdr ? DXGI_COLOR_SPACE_RGB_FULL_G10_NONE_P709
                           : DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709;
    if (desc.Format != format)
    {
        CloseFgResources();
        // PresentFrame/PresentBypass release each back buffer and wait for GPU
        // work before returning; no old buffer reference survives here.
        // This failure is fatal on either path, and deliberately so: both
        // presents CopyResource into the back buffer, and a copy between
        // mismatched formats is not a wrong picture, it is a removed device.
        // Their copies ran on the present queue, where Present may still sit.
        FlushPresentQueue("present-format");
        if (FAILED(g_present_swap->ResizeBuffers(0, 0, 0, format, desc.Flags)))
        { Log("[hdr] swap chain format change failed"); return false; }
        Log("[hdr] presentation=%s", pq ? "HDR10 PQ (DLSS-G)" : hdr ? "FP16 scRGB" : "8-bit SDR");
        g_present_space_set = false;
    }
    // Once per swap chain, not once per frame. The space only changes with
    // the format, and the call above is the only thing that changes it.
    if (g_present_space_set && g_present_space == space) return true;
    g_present_space_set = true;
    g_present_space = space;
    UINT support = 0;
    if (FAILED(g_present_swap->CheckColorSpaceSupport(space, &support)) ||
        !(support & DXGI_SWAP_CHAIN_COLOR_SPACE_SUPPORT_FLAG_PRESENT) ||
        FAILED(g_present_swap->SetColorSpace1(space)))
    {
        Log("[hdr] presentation colour space %u unsupported", (unsigned)space);
        // On the HDR path the colour space IS the feature: an scRGB buffer
        // presented as if it were sRGB is worse than no HDR at all, so the
        // frame is refused and the capture falls back. On the SDR path it
        // is the space the chain was created with - the picture is right
        // without the call ever being made, and this program made presents
        // for a year without making it. Refusing here would turn "too
        // bright" into "nothing at all" on somebody's machine.
        return hdr ? false : true;
    }
    return true;
}

static bool EnsureHdrPipeline(UINT w, UINT height, bool pq)
{
    const auto format = pq ? DXGI_FORMAT_R10G10B10A2_UNORM : DXGI_FORMAT_R16G16B16A16_FLOAT;
    if (g_hdr_output)
    {
        const auto d = g_hdr_output->GetDesc();
        if (d.Width == w && d.Height == height && d.Format == format) return true;
    }
    // Either a size change or a half-built attempt from last time: both
    // start from nothing.
    CloseHdrResources();
    winrt::com_ptr<ID3DBlob> code, errors, signature;
    HRESULT hr = D3DCompile(kHdrCompositeHlsl, sizeof(kHdrCompositeHlsl)-1,
        "hdr-composite", nullptr, nullptr, "CSMain", "cs_5_0", 0, 0, code.put(), errors.put());
    if (FAILED(hr))
    { Log("[hdr] compile: %s", errors ? (char *)errors->GetBufferPointer() : "failed"); return false; }
    D3D12_DESCRIPTOR_RANGE ranges[2] = {
        {D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 3, 0, 0, 0},
        {D3D12_DESCRIPTOR_RANGE_TYPE_UAV, 1, 0, 0, 3}};
    D3D12_ROOT_PARAMETER params[2] = {};
    params[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    params[0].DescriptorTable = {2, ranges};
    params[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    params[1].Constants = {0, 0, 5};   // white, bypass, split, hdr, rotate180
    D3D12_ROOT_SIGNATURE_DESC rs = {2, params};
    if (FAILED(D3D12SerializeRootSignature(&rs, D3D_ROOT_SIGNATURE_VERSION_1, signature.put(), errors.put())))
        return false;
    if (FAILED(h.dev->CreateRootSignature(0, signature->GetBufferPointer(), signature->GetBufferSize(),
                                         IID_PPV_ARGS(&g_hdr_rs)))) return false;
    D3D12_COMPUTE_PIPELINE_STATE_DESC ps = {};
    ps.pRootSignature = g_hdr_rs;
    ps.CS = {code->GetBufferPointer(), code->GetBufferSize()};
    if (FAILED(h.dev->CreateComputePipelineState(&ps, IID_PPV_ARGS(&g_hdr_pso)))) return false;
    D3D12_DESCRIPTOR_HEAP_DESC heap = {};
    heap.Type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;
    // Two tables of four: the composite into g_hdr_output, and the same
    // composite PQ-encoded into g_rec_pq for an HDR10 recording.
    heap.NumDescriptors = 8;
    heap.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
    if (FAILED(h.dev->CreateDescriptorHeap(&heap, IID_PPV_ARGS(&g_hdr_heap)))) return false;
    g_hdr_output = MakeTex(w, height, format, true);
    return g_hdr_output != nullptr;
}

static bool PresentHdr(VideoState &v, bool bypass, bool allow_fg, UINT64 *submitted)
{
    // NR OFF is not a reason for ordinary presentation either: FG owns the
    // present loop on both paths. `bypass` still selects WHICH frame is
    // composed (the raw capture, below) - it just no longer decides whether
    // the presenter runs. `allow_fg` is false only for the one ordinary
    // present that follows a failed FG attempt on this same frame.
    const bool framegen = allow_fg && FgRequested();
    if (!framegen) StopFgPresentation();
    const UINT w = v.upscale ? v.full_w : v.w;
    const UINT height = v.upscale ? v.full_h : v.hgt;
    if (!g_dda_d12 || !g_dda_ready) return false;
    // The capture changes size before the output does. A window going
    // fullscreen hands WGC its new size at once, while the client resizes
    // only once the new size has held for half a second (follow_window),
    // with a live RNSZ. The SDR path clips for that half second (the
    // swizzle copy); this one used to refuse the frame, and a refused frame
    // ends the worker (exit 9): a restart at the old size and another for
    // the new one, ~4 s, on EVERY fullscreen toggle with HDR on - four in
    // one user's evening. The composite reads all three inputs by
    // coordinate and copies nothing between them, so it clips the same
    // way: the capture's top-left corner at the output size, and where the
    // capture is the smaller one, the SDR proxy fills in (the shader).
    const auto native_desc = g_dda_d12->GetDesc();
    const bool clipped = native_desc.Width != w || native_desc.Height != height;
    static bool clip_said = false;
    if (clipped && !clip_said)
        Log("[hdr] capture %llux%u vs output %ux%u - composed clipped until the "
            "client resizes", (unsigned long long)native_desc.Width,
            native_desc.Height, w, height);
    clip_said = clipped;
    if (!EnsurePresentFormat(true, framegen) || !EnsureHdrPipeline(w, height, framegen)) return false;
    // An HDR10 recording takes this frame as the display gets it - 10-bit PQ
    // BT.2020 - when its slot is due: with Frame Generation that is the
    // composite itself; without it, the same composite dispatched a second
    // time with the PQ encode on, into g_rec_pq.
    int64_t rec_t = 0;
    const bool rec = g_rec_hdr && RecordWanted(&rec_t);
    bool rec_pq = rec && !framegen;
    if (rec_pq)
    {
        const auto d = g_rec_pq != nullptr ? g_rec_pq->GetDesc() : D3D12_RESOURCE_DESC{};
        if (g_rec_pq == nullptr || d.Width != w || d.Height != height)
        {
            if (g_rec_pq) { g_rec_pq->Release(); g_rec_pq = nullptr; }
            g_rec_pq = MakeTex(w, height, DXGI_FORMAT_R10G10B10A2_UNORM, true);
        }
        rec_pq = g_rec_pq != nullptr;   // without it the slot goes by: the last frame holds
    }

    const UINT stride = h.dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    auto cpu = g_hdr_heap->GetCPUDescriptorHandleForHeapStart();
    // A bypass must never read an uninitialized neural output.
    ID3D12Resource *inputs[] = {g_dda_d12, v.color.tex, bypass ? v.color.tex : v.output};
    for (auto *input : inputs)
    {
        D3D12_SHADER_RESOURCE_VIEW_DESC srv = {};
        srv.Format = input->GetDesc().Format;
        srv.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
        srv.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
        srv.Texture2D.MipLevels = 1;
        h.dev->CreateShaderResourceView(input, &srv, cpu);
        cpu.ptr += stride;
    }
    D3D12_UNORDERED_ACCESS_VIEW_DESC uav = {};
    uav.Format = g_hdr_output->GetDesc().Format;
    uav.ViewDimension = D3D12_UAV_DIMENSION_TEXTURE2D;
    h.dev->CreateUnorderedAccessView(g_hdr_output, nullptr, &uav, cpu);
    if (rec_pq)
    {
        // The second table: the same three inputs, the recording's target.
        for (auto *input : inputs)
        {
            cpu.ptr += stride;
            D3D12_SHADER_RESOURCE_VIEW_DESC srv = {};
            srv.Format = input->GetDesc().Format;
            srv.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
            srv.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
            srv.Texture2D.MipLevels = 1;
            h.dev->CreateShaderResourceView(input, &srv, cpu);
        }
        cpu.ptr += stride;
        uav.Format = DXGI_FORMAT_R10G10B10A2_UNORM;
        h.dev->CreateUnorderedAccessView(g_rec_pq, nullptr, &uav, cpu);
    }
    winrt::com_ptr<ID3D12Resource> bb;
    if (!framegen && FAILED(g_present_swap->GetBuffer(g_present_swap->GetCurrentBackBufferIndex(),
                                         __uuidof(ID3D12Resource), bb.put_void()))) return false;
    if (!BeginCommands()) return false;
    D3D12_RESOURCE_BARRIER pre[] = {
        Transition(g_hdr_output, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_UNORDERED_ACCESS),
        Transition(g_dda_d12, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
        Transition(v.output, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE)};
    h.list->ResourceBarrier(bypass ? 2 : 3, pre);
    h.list->SetDescriptorHeaps(1, &g_hdr_heap);
    h.list->SetComputeRootSignature(g_hdr_rs);
    h.list->SetPipelineState(g_hdr_pso);
    h.list->SetComputeRootDescriptorTable(0, g_hdr_heap->GetGPUDescriptorHandleForHeapStart());
    // rotate180 exactly as the capture shader gets it: only a duplicated
    // desktop comes back unrotated (a WGC window is already composed).
    struct { float white; UINT bypass, split, hdr, rotate180; } constants = {
        g_hdr_frame_white, bypass ? 1u : 0u, g_hdr_split,
        (g_capture_display.enabled ? 1u : 0u) | (framegen ? 2u : 0u),
        (g_dda_active && g_capture_rotate180) ? 1u : 0u};
    h.list->SetComputeRoot32BitConstants(1, 5, &constants, 0);
    h.list->Dispatch((w+7)/8, (height+7)/8, 1);
    if (rec_pq)
    {
        auto to_uav = Transition(g_rec_pq, D3D12_RESOURCE_STATE_COMMON,
                                 D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        h.list->ResourceBarrier(1, &to_uav);
        auto table = g_hdr_heap->GetGPUDescriptorHandleForHeapStart();
        table.ptr += 4 * static_cast<UINT64>(stride);
        h.list->SetComputeRootDescriptorTable(0, table);
        auto pq = constants;
        pq.hdr |= 2u;   // the shader's PQ encode, as for DLSS-G
        h.list->SetComputeRoot32BitConstants(1, 5, &pq, 0);
        h.list->Dispatch((w+7)/8, (height+7)/8, 1);
        auto to_copy = Transition(g_rec_pq, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                                  D3D12_RESOURCE_STATE_COPY_SOURCE);
        h.list->ResourceBarrier(1, &to_copy);
    }
    auto to_copy_source = Transition(g_hdr_output, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                                     D3D12_RESOURCE_STATE_COPY_SOURCE);
    h.list->ResourceBarrier(1, &to_copy_source);
    // Both candidates are in COPY_SOURCE here. The back buffer is written on
    // the present queue once this list is done (CopyToBackBuffer, below).
    // The recording takes the fisheye lens too, when it is on.
    if (rec_pq)
        RecordCopyAt(h.list, LensApply(h.list, g_rec_pq, D3D12_RESOURCE_STATE_COPY_SOURCE,
                                       LENS_RECORD), rec_t);
    else if (rec && framegen)
        RecordCopyAt(h.list, LensApply(h.list, g_hdr_output, D3D12_RESOURCE_STATE_COPY_SOURCE,
                                       LENS_RECORD), rec_t);
    if (rec_pq)
    {
        auto back = Transition(g_rec_pq, D3D12_RESOURCE_STATE_COPY_SOURCE,
                               D3D12_RESOURCE_STATE_COMMON);
        h.list->ResourceBarrier(1, &back);
    }
    D3D12_RESOURCE_BARRIER post[] = {
        Transition(g_hdr_output, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_COMMON),
        Transition(g_dda_d12, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COMMON),
        Transition(v.output, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_UNORDERED_ACCESS)};
    h.list->ResourceBarrier(1, post);
    h.list->ResourceBarrier(bypass ? 1 : 2, post + 1);
    // What the screen gets: the composite, or its fisheye lens. With Frame
    // Generation the composite goes to DLSS-G unbent and FgPresent puts the
    // lens on what it shows.
    ID3D12Resource *shown = framegen ? g_hdr_output
        : LensApply(h.list, g_hdr_output, D3D12_RESOURCE_STATE_COMMON, LENS_SHOWN);
    // Spout consumers are SDR, and so is every recording but an HDR10 one
    // (taken above; RecordCopy stands down for it).
    auto rest = bypass ? D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE : D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
    ID3D12Resource *export_src = LensApply(h.list, bypass ? v.color.tex : v.output, rest,
                                           LENS_EXPORT);
    auto export_pre = Transition(export_src, rest, D3D12_RESOURCE_STATE_COPY_SOURCE);
    h.list->ResourceBarrier(1, &export_pre);
    ExportCopy(h.list, export_src, w, height);
    auto export_post = Transition(export_src, D3D12_RESOURCE_STATE_COPY_SOURCE, rest);
    h.list->ResourceBarrier(1, &export_post);
    const auto fence = EndCommands();
    // The deferred tail, as on the SDR path (#149): with `submitted` the
    // upload and the evaluation were not waited for, so this fence covers
    // them too - the 60 s budget is theirs - and the client is answered
    // now, while the GPU runs. Nothing it does next can touch this frame:
    // the next message is read only after the present below.
    if (submitted) *submitted = fence;
    if (fence != 0 && !framegen) SendEarlyReply();
    if (!ProfileWait(PS_PRESENT, fence, submitted ? 60000 : 2000, "hdr-present"))
    {
        if (g_submission_failed) bb.detach();
        return false;
    }
    // The same status reading as the SDR path: a mode change is a SUCCESS
    // code, and a chain the desktop has moved out from under shows nothing
    // while every present on it reports success (#58).
    if (framegen)
    {
        // The export follows the same source this path composed and showed.
        if (FgPresent(v, g_hdr_output, D3D12_RESOURCE_STATE_COMMON, bypass))
        {
            if (submitted) *submitted = g_fg_present_fence;
            return true;
        }
        // Ordinary output for this frame, and never FG again inside it. A
        // refused multiplier (FgStepDown) lowers only the ceiling and leaves
        // g_fg.failed clear: the lower count takes effect at the next frame
        // header (ConfigureFgFrame). Re-entering with FG allowed rebuilt the
        // feature at the SAME refused count and recursed until the stack ran
        // out - HDR with 3x/4x on a card that refuses it. The SDR paths
        // already fall through to a plain present here.
        return PresentHdr(v, bypass, false, submitted);
    }
    if (!CopyToBackBuffer(bb.get(), shown, D3D12_RESOURCE_STATE_COMMON, "hdr-present"))
    {
        if (g_submission_failed) bb.detach();
        return false;
    }
    const bool ok = PresentStatus(g_present_swap->Present(0, 0), "hdr present");
    if (ok) { RevealOnFirstPresent(); SpoutBridgeSend(); }
    return ok;
}
