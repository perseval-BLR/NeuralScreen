// The on-screen rate badge, drawn by the worker into what it presents (v2.6.0).
//
// It used to be drawn by the client on its HUD layer - a full-screen layered
// window over the picture - and the counter changes every time it is redrawn,
// so the layer was flipped ten times a second. Every flip of that window
// costs the Windows compositor a composition: with the counter on, a quarter
// of a video's frames reached the screen a refresh early or late (#166). So
// the client still renders the badge - same font, same theme, same corner -
// but into a small shared bitmap, and the worker blends it over each frame it
// presents. The HUD layer then holds nothing that changes.
//
// What it is drawn on: the presented picture only. Not the network's input,
// not Spout, not a recording or a screenshot - each export is taken before
// the badge goes on (PresentFrame, PresentBypass, PresentHdr), and on the
// Frame Generation path it goes on the slot's real and generated frames,
// which nothing exports.
//
// The bitmap: the client's mapping (BADG names it), a 32-byte header then
// kBadgeW x kBadgeH premultiplied sRGB RGBA8. The header's sequence is 0
// while the client writes and a new non-zero value when it is done; the
// worker copies only a stable one.

static const char kBadgeHlsl[] = R"(
cbuffer BadgeCB : register(b0)
{
    int2  gOrigin;   // the badge's top-left, in frame pixels
    uint2 gSize;     // the badge's size
    uint2 gFrame;    // the frame's size
    float gWhite;    // SDR white on an HDR target (scRGB units: 1.0 = 80 nits)
    uint  gFlags;    // bit0 a linear scRGB target, bit1 a PQ (HDR10) target
};
Texture2D<float4>   gSrc   : register(t0);
Texture2D<float4>   gBadge : register(t1);
RWTexture2D<float4> gDst   : register(u0);
float3 ToPq(float3 nits)
{
    const float m1 = 0.1593017578125, m2 = 78.84375;
    const float c1 = 0.8359375, c2 = 18.8515625, c3 = 18.6875;
    float3 y = pow(saturate(nits / 10000.0), m1);
    return pow((c1 + c2 * y) / (1.0 + c3 * y), m2);
}
[numthreads(8, 8, 1)]
void CSMain(uint3 id : SV_DispatchThreadID)
{
    if (id.x >= gSize.x || id.y >= gSize.y) return;
    int2 p = gOrigin + int2(id.xy);
    if (any(p < 0) || any(p >= int2(gFrame))) return;
    float4 c = gBadge.Load(int3(id.xy, 0));   // premultiplied sRGB
    if (c.a <= 0.0) return;
    float4 s = gSrc.Load(int3(p, 0));
    float3 rgb = c.rgb;
    if ((gFlags & 1u) != 0) rgb = pow(max(rgb, 0.0), 2.2) * gWhite;
    else if ((gFlags & 2u) != 0) rgb = ToPq(pow(max(rgb, 0.0), 2.2) * gWhite * 80.0);
    gDst[p] = float4(rgb + s.rgb * (1.0 - c.a), s.a);
}
)";

static constexpr UINT kBadgeW = 512;   // the largest badge kept (pitch 2048: a valid footprint)
static constexpr UINT kBadgeH = 64;
static constexpr uint32_t kBadgeMagic = 0x45474442u;   // "BDGE"

#pragma pack(push, 1)
struct BadgeHeader
{
    uint32_t magic, seq, w, h;
    uint32_t corner;   // 0 top-left, 1 top-right, 2 bottom-left, 3 bottom-right
    uint32_t margin;   // from the frame's edges, frame pixels
    uint32_t row;      // pushed down (top corners) or up by this much: the REC badge
    uint32_t reserved;
};
#pragma pack(pop)
static_assert(sizeof(BadgeHeader) == 32, "BadgeHeader != badge.HEADER_FMT");

static struct BadgeState {
    winrt::com_ptr<ID3D12RootSignature> root;
    winrt::com_ptr<ID3D12PipelineState> pso;
    winrt::com_ptr<ID3D12DescriptorHeap> heap;
    HANDLE mapping = nullptr;
    const BYTE *view = nullptr;
    ID3D12Resource *image = nullptr;      // kBadgeW x kBadgeH RGBA8, premultiplied
    ID3D12Resource *upload = nullptr;     // its upload buffer, mapped
    BYTE *upload_ptr = nullptr;
    ID3D12Resource *target = nullptr;     // the frame with the badge on it
    D3D12_RESOURCE_STATES target_state = D3D12_RESOURCE_STATE_COMMON;
    uint32_t seq = 0;                     // what the image holds
    UINT w = 0, hgt = 0, corner = 1, margin = 0, row = 0;
    bool pending = false;                 // uploaded on the next pass
    bool image_fresh = true;              // the image texture is still in COMMON
    bool failed = false;
    bool said = false;
    UINT ring = 0;
} g_badge;

static void CloseBadgeMapping()
{
    auto &s = g_badge;
    if (s.view) UnmapViewOfFile(s.view);
    if (s.mapping) CloseHandle(s.mapping);
    s.view = nullptr;
    s.mapping = nullptr;
    s.seq = 0;
    s.w = s.hgt = 0;
}

// The GPU side, with the video textures (a resize, a rebuild). The mapping
// stays open: the client names it once per worker, not once per size.
static void CloseBadge()
{
    auto &s = g_badge;
    s.seq = 0;          // upload the bitmap again into the new image
    if (g_submission_failed) return;
    if ((s.image || s.target) && h.fence && h.fence_value != 0 &&
        !WaitFenceValue(h.fence, h.fence_value, 30000, "badge-close"))
        return;
    for (ID3D12Resource **r : {&s.image, &s.upload, &s.target})
        if (*r != nullptr) { (*r)->Release(); *r = nullptr; }
    s.upload_ptr = nullptr;
    s.image_fresh = true;
    s.target_state = D3D12_RESOURCE_STATE_COMMON;
}

// BADG: the client's bitmap, by name; an empty name lets go of it.
static bool OpenBadge(const char *name)
{
    CloseBadgeMapping();
    if (name == nullptr || name[0] == '\0') return true;
    const SIZE_T size = sizeof(BadgeHeader) + static_cast<SIZE_T>(kBadgeW) * kBadgeH * 4;
    g_badge.mapping = OpenFileMappingA(FILE_MAP_READ, FALSE, name);
    if (g_badge.mapping == nullptr)
    { Log("[badge] mapping '%s' not found (%lu)", name, GetLastError()); return false; }
    g_badge.view = static_cast<const BYTE *>(MapViewOfFile(g_badge.mapping, FILE_MAP_READ, 0, 0, size));
    if (g_badge.view == nullptr)
    { Log("[badge] mapping '%s' could not be mapped (%lu)", name, GetLastError()); CloseBadgeMapping(); return false; }
    Log("[badge] the rate badge is drawn by the worker ('%s')", name);
    return true;
}

static bool BadgePipeline()
{
    auto &s = g_badge;
    if (s.pso) return true;
    if (s.failed) return false;
    D3D12_DESCRIPTOR_RANGE ranges[2] = {
        {D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 2, 0, 0, 0},
        {D3D12_DESCRIPTOR_RANGE_TYPE_UAV, 1, 0, 0, 2}};
    D3D12_ROOT_PARAMETER roots[2] = {};
    roots[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    roots[0].Constants = {0, 0, 8};
    roots[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    roots[1].DescriptorTable = {2, ranges};
    D3D12_ROOT_SIGNATURE_DESC rd = {2, roots, 0, nullptr, D3D12_ROOT_SIGNATURE_FLAG_NONE};
    winrt::com_ptr<ID3DBlob> code, errors;
    if (FAILED(D3D12SerializeRootSignature(&rd, D3D_ROOT_SIGNATURE_VERSION_1, code.put(), errors.put())) ||
        FAILED(h.dev->CreateRootSignature(0, code->GetBufferPointer(), code->GetBufferSize(),
                                          IID_PPV_ARGS(s.root.put()))))
    { s.failed = true; Log("[badge] root signature failed - no rate badge"); return false; }
    code = nullptr; errors = nullptr;
    if (FAILED(D3DCompile(kBadgeHlsl, sizeof(kBadgeHlsl) - 1, "badge.hlsl", nullptr, nullptr, "CSMain",
                          "cs_5_0", D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, code.put(), errors.put())))
    {
        s.failed = true;
        Log("[badge] shader failed to compile: %s - no rate badge",
            errors ? static_cast<const char *>(errors->GetBufferPointer()) : "?");
        return false;
    }
    D3D12_COMPUTE_PIPELINE_STATE_DESC pd = {};
    pd.pRootSignature = s.root.get();
    pd.CS = {code->GetBufferPointer(), code->GetBufferSize()};
    // Three descriptors a pass; up to seven passes a frame with FG at 6x (the
    // real frame and five generated), and every frame waits for its
    // predecessor - 32 passes round the ring.
    D3D12_DESCRIPTOR_HEAP_DESC hd = {D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV, 96,
                                     D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE, 0};
    if (FAILED(h.dev->CreateComputePipelineState(&pd, IID_PPV_ARGS(s.pso.put()))) ||
        FAILED(h.dev->CreateDescriptorHeap(&hd, IID_PPV_ARGS(s.heap.put()))))
    { s.failed = true; s.pso = nullptr; Log("[badge] pipeline failed - no rate badge"); return false; }
    return true;
}

// A stable new bitmap from the mapping into the upload buffer. False while
// there is no badge to draw.
static bool BadgeRefresh()
{
    auto &s = g_badge;
    if (s.view == nullptr) return false;
    BadgeHeader head = {};
    memcpy(&head, s.view, sizeof(head));
    if (head.magic != kBadgeMagic || head.seq == 0) return s.w != 0 && !s.image_fresh;
    if (head.seq == s.seq) return s.w != 0 && !s.image_fresh;
    if (head.w == 0 || head.h == 0)
    {
        s.seq = head.seq;
        s.w = s.hgt = 0;
        return false;
    }
    if (s.upload_ptr == nullptr) return false;
    // The upload buffer may still be read by the last frame's copy.
    if (!s.image_fresh && h.fence && h.fence_value != 0 &&
        !WaitFenceValue(h.fence, h.fence_value, 30000, "badge-upload"))
        return false;
    const UINT w = (std::min)(head.w, kBadgeW), hgt = (std::min)(head.h, kBadgeH);
    const BYTE *pixels = s.view + sizeof(BadgeHeader);
    memset(s.upload_ptr, 0, static_cast<size_t>(kBadgeW) * 4 * kBadgeH);
    for (UINT y = 0; y < hgt; ++y)
        memcpy(s.upload_ptr + static_cast<size_t>(y) * kBadgeW * 4,
               pixels + static_cast<size_t>(y) * kBadgeW * 4, static_cast<size_t>(w) * 4);
    // Read again: a sequence that moved while copying is a torn bitmap - the
    // next frame takes the finished one.
    uint32_t after = 0;
    memcpy(&after, s.view + offsetof(BadgeHeader, seq), sizeof(after));
    if (after != head.seq) return s.w != 0 && !s.image_fresh;
    s.seq = head.seq;
    s.w = w;
    s.hgt = hgt;
    s.corner = head.corner & 3u;
    s.margin = head.margin;
    s.row = head.row;
    s.pending = true;
    return true;
}

static bool BadgeResources(const D3D12_RESOURCE_DESC &frame)
{
    auto &s = g_badge;
    if (!s.image)
    {
        s.image = MakeTex(kBadgeW, kBadgeH, DXGI_FORMAT_R8G8B8A8_UNORM, false);
        s.image_fresh = true;
        D3D12_HEAP_PROPERTIES up = {};
        up.Type = D3D12_HEAP_TYPE_UPLOAD;
        D3D12_RESOURCE_DESC bd = {};
        bd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
        bd.Width = static_cast<UINT64>(kBadgeW) * kBadgeH * 4;
        bd.Height = 1; bd.DepthOrArraySize = 1; bd.MipLevels = 1;
        bd.SampleDesc.Count = 1;
        bd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        if (FAILED(h.dev->CreateCommittedResource(&up, D3D12_HEAP_FLAG_NONE, &bd,
                                                  D3D12_RESOURCE_STATE_GENERIC_READ, nullptr,
                                                  IID_PPV_ARGS(&s.upload))) ||
            FAILED(s.upload->Map(0, nullptr, reinterpret_cast<void **>(&s.upload_ptr))))
        {
            for (ID3D12Resource **r : {&s.image, &s.upload})
                if (*r != nullptr) { (*r)->Release(); *r = nullptr; }
            s.upload_ptr = nullptr;
            return false;
        }
    }
    if (s.target)
    {
        const auto d = s.target->GetDesc();
        if (d.Width != frame.Width || d.Height != frame.Height || d.Format != frame.Format)
        {
            if (h.fence && h.fence_value != 0 &&
                !WaitFenceValue(h.fence, h.fence_value, 30000, "badge-resize"))
                return false;
            s.target->Release();
            s.target = nullptr;
        }
    }
    if (!s.target)
    {
        s.target = MakeTex(static_cast<UINT>(frame.Width), frame.Height, frame.Format, true);
        s.target_state = D3D12_RESOURCE_STATE_COMMON;
    }
    return s.image != nullptr && s.target != nullptr;
}

// The frame `src` (resting in `state`) with the badge on it, in `state` - or
// `src` itself when there is no badge to draw. `src` is never written.
static ID3D12Resource *BadgeApply(ID3D12GraphicsCommandList *list, ID3D12Resource *src,
                                  D3D12_RESOURCE_STATES state)
{
    auto &s = g_badge;
    if (src == nullptr || list == nullptr || g_submission_failed || s.view == nullptr)
        return src;
    const auto desc = src->GetDesc();
    const bool linear = desc.Format == DXGI_FORMAT_R16G16B16A16_FLOAT;
    const bool pq = desc.Format == DXGI_FORMAT_R10G10B10A2_UNORM;
    if (!linear && !pq && desc.Format != DXGI_FORMAT_R8G8B8A8_UNORM &&
        desc.Format != DXGI_FORMAT_B8G8R8A8_UNORM)
        return src;
    if (!BadgePipeline() || !BadgeResources(desc)) return src;
    if (!BadgeRefresh() && !s.pending) return src;
    if (s.w == 0) return src;
    if (!s.said)
    {
        Log("[badge] drawing the rate badge into the presented picture (%ux%u)", s.w, s.hgt);
        s.said = true;
    }
    if (s.pending)
    {
        D3D12_RESOURCE_BARRIER to = Transition(s.image,
            s.image_fresh ? D3D12_RESOURCE_STATE_COMMON : D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
            D3D12_RESOURCE_STATE_COPY_DEST);
        list->ResourceBarrier(1, &to);
        D3D12_TEXTURE_COPY_LOCATION dl = {};
        dl.pResource = s.image;
        dl.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        D3D12_TEXTURE_COPY_LOCATION sl = {};
        sl.pResource = s.upload;
        sl.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
        sl.PlacedFootprint.Footprint = {DXGI_FORMAT_R8G8B8A8_UNORM, kBadgeW, kBadgeH, 1, kBadgeW * 4};
        list->CopyTextureRegion(&dl, 0, 0, 0, &sl, nullptr);
        D3D12_RESOURCE_BARRIER back = Transition(s.image, D3D12_RESOURCE_STATE_COPY_DEST,
                                                 D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        list->ResourceBarrier(1, &back);
        s.pending = false;
        s.image_fresh = false;
    }
    const int fw = static_cast<int>(desc.Width), fh = static_cast<int>(desc.Height);
    const int m = static_cast<int>(s.margin), row = static_cast<int>(s.row);
    const int bw = static_cast<int>(s.w), bh = static_cast<int>(s.hgt);
    const bool right = s.corner == 1 || s.corner == 3;
    const bool bottom = s.corner >= 2;
    const int ox = right ? fw - m - bw : m;
    const int oy = bottom ? fh - m - bh - row : m + row;
    // The frame as it is, then the badge over it.
    if (state != D3D12_RESOURCE_STATE_COPY_SOURCE)
    {
        auto to_src = Transition(src, state, D3D12_RESOURCE_STATE_COPY_SOURCE);
        list->ResourceBarrier(1, &to_src);
    }
    D3D12_RESOURCE_BARRIER pre = Transition(s.target, s.target_state, D3D12_RESOURCE_STATE_COPY_DEST);
    list->ResourceBarrier(1, &pre);
    list->CopyResource(s.target, src);
    D3D12_RESOURCE_BARRIER mid[2] = {
        Transition(s.target, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_UNORDERED_ACCESS),
        Transition(src, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE)};
    list->ResourceBarrier(2, mid);
    const UINT step = h.dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    const UINT base = (s.ring++ % 32u) * 3u;
    D3D12_CPU_DESCRIPTOR_HANDLE cpu = s.heap->GetCPUDescriptorHandleForHeapStart();
    cpu.ptr += static_cast<SIZE_T>(base) * step;
    D3D12_SHADER_RESOURCE_VIEW_DESC sd = {};
    sd.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
    sd.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
    sd.Texture2D.MipLevels = 1;
    sd.Format = desc.Format;
    h.dev->CreateShaderResourceView(src, &sd, cpu); cpu.ptr += step;
    sd.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    h.dev->CreateShaderResourceView(s.image, &sd, cpu); cpu.ptr += step;
    D3D12_UNORDERED_ACCESS_VIEW_DESC ud = {};
    ud.Format = desc.Format;
    ud.ViewDimension = D3D12_UAV_DIMENSION_TEXTURE2D;
    h.dev->CreateUnorderedAccessView(s.target, nullptr, &ud, cpu);
    struct { int origin[2]; UINT size[2], frame[2]; float white; UINT flags; } cb = {
        {ox, oy}, {s.w, s.hgt}, {static_cast<UINT>(desc.Width), desc.Height},
        (std::max)(1.0f, g_hdr_frame_white), (linear ? 1u : 0u) | (pq ? 2u : 0u)};
    static_assert(sizeof(cb) == 32, "BadgeCB is 8 constants");
    ID3D12DescriptorHeap *heaps[] = {s.heap.get()};
    list->SetDescriptorHeaps(1, heaps);
    list->SetComputeRootSignature(s.root.get());
    list->SetPipelineState(s.pso.get());
    list->SetComputeRoot32BitConstants(0, 8, &cb, 0);
    D3D12_GPU_DESCRIPTOR_HANDLE gpu = s.heap->GetGPUDescriptorHandleForHeapStart();
    gpu.ptr += static_cast<UINT64>(base) * step;
    list->SetComputeRootDescriptorTable(1, gpu);
    list->Dispatch((s.w + 7) / 8, (s.hgt + 7) / 8, 1);
    // Back to `state`, each only where it is a real change: a barrier from a
    // state to the same state is invalid (PresentFrame rests in UAV,
    // PresentBypass in NON_PIXEL_SHADER_RESOURCE).
    D3D12_RESOURCE_BARRIER post[2];
    UINT n = 0;
    if (state != D3D12_RESOURCE_STATE_UNORDERED_ACCESS)
        post[n++] = Transition(s.target, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, state);
    if (state != D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE)
        post[n++] = Transition(src, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, state);
    if (n) list->ResourceBarrier(n, post);
    s.target_state = state;
    return s.target;
}

// The same on a texture resting in COPY_SOURCE, written back into it: the
// Frame Generation slot's frames, which only the presenter reads.
static void BadgeInPlace(ID3D12GraphicsCommandList *list, ID3D12Resource *tex)
{
    ID3D12Resource *out = BadgeApply(list, tex, D3D12_RESOURCE_STATE_COPY_SOURCE);
    if (out == tex || out == nullptr) return;
    auto to_dst = Transition(tex, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_COPY_DEST);
    list->ResourceBarrier(1, &to_dst);
    list->CopyResource(tex, out);
    auto back = Transition(tex, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_COPY_SOURCE);
    list->ResourceBarrier(1, &back);
}
