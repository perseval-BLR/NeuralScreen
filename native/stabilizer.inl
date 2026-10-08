// Temporal stabilizer of the NR edit in the Boost path (on; NS_STAB=0 turns it off).
//
// The network's output shimmers from frame to frame even where the picture
// does not change: fed the same frame 40 times it moves by ~0.26 of 255 on
// average and up to 3 every frame, and moving content adds the error of the
// motion field on top. Only the EDIT is filtered - D = nr_out - nr_in at the
// work resolution, once per evaluated frame, after the last cascade pass and
// before the residual composite - so the native picture underneath is never
// delayed: a wrong history can misplace part of the enhancement, never the
// content. Per pixel:
//
//   u     = the motion vector or no motion, whichever explains this frame's
//           input better (a patch test against the 3x3 input range)
//   conf  = that test x a sub-pixel guard
//   H     = last frame's steadied edit at p + u, clamped to the current 3x3
//           edit range +- 0.02
//   D'    = H + alpha (D - H), alpha = 1 - 0.75 strength conf (0.25 at full trust)
//
// The design, its constants and the references it comes from (MPCVR-DLSS5's
// effect stabilizer - flicker 1.44-1.59 -> 1.06-1.14 at 100% effect -, Magpie,
// RenoDX, 2600th) are in _work/stabilizer/SPEC.md; the shader is that draft.
// The composite then reads g_stab.out instead of nr_out; at strength 0 the
// pass copies the edit through and keeps its history warm.
//
// History: any command (a parameter, pass or size change), a reset, a frame in
// bypass (NR off) or a failure starts it over; a frame kept without evaluation
// (an unchanged capture) does not, because its input did not change.

static const char kStabHlsl[] = R"(
Texture2D<float4>   gNrIn      : register(t0);
Texture2D<float4>   gNrOut     : register(t1);
Texture2D<float2>   gMotion    : register(t2);
Texture2D<float4>   gHistory   : register(t3);
Texture2D<float4>   gGuide     : register(t4);
RWTexture2D<float4> gStab      : register(u0);
RWTexture2D<float4> gHistoryOut: register(u1);
RWTexture2D<float4> gGuideOut  : register(u2);
SamplerState        gLinear    : register(s0);
cbuffer StabCB : register(b0)
{
    uint2  gSize; uint2 gMvSize; float2 gMvScale; float gAlphaMin; float gTol;
    float  gRngMeanLo; float gRngMeanHi; float gRngMaxLo; float gRngMaxHi;
    float  gSubLo; float gSubHi; float gMinMotion; uint gFlags;
};
static const uint STAB_FLAG_RESET = 1u;
static const uint STAB_FLAG_NO_ZERO = 4u;
static const uint STAB_FLAG_PASS = 8u;
#define TILE 8
#define HALO 2
#define SPAN (TILE + 2 * HALO)
#define RSPAN (TILE + 2)
groupshared float3 sIn[SPAN][SPAN];
groupshared float3 sEd[SPAN][SPAN];
groupshared float3 sLo[RSPAN][RSPAN];
groupshared float3 sHi[RSPAN][RSPAN];
float MaxCh(float3 v) { return max(v.r, max(v.g, v.b)); }
float Quality(int2 local, float2 centre, float2 u, out float centreError)
{
    const float2 invSize = 1.0 / float2(gSize);
    float eMean = 0.0, eMax = 0.0;
    centreError = 0.0;
    [unroll] for (int oy = -1; oy <= 1; ++oy)
    {
        [unroll] for (int ox = -1; ox <= 1; ++ox)
        {
            const float3 g  = gGuide.SampleLevel(gLinear, (centre + u + float2(ox, oy)) * invSize, 0).rgb;
            const int2   r  = local + int2(ox, oy) + 1;
            const float3 ex = max(max(sLo[r.y][r.x] - g, g - sHi[r.y][r.x]), 0.0);
            const float  d  = MaxCh(ex);
            eMean += d * (1.0 / 9.0);
            eMax = max(eMax, d);
            if (ox == 0 && oy == 0)
            {
                const int2 c = local + HALO;
                centreError = MaxCh(abs(sIn[c.y][c.x] - g));
            }
        }
    }
    const float2 q = centre + u;
    if (any(q < 0.0) || any(q > float2(gSize))) return 0.0;
    return (1.0 - smoothstep(gRngMeanLo, gRngMeanHi, eMean)) *
           (1.0 - smoothstep(gRngMaxLo, gRngMaxHi, eMax));
}
[numthreads(TILE, TILE, 1)]
void CSMain(uint3 gid : SV_GroupID, uint3 gtid : SV_GroupThreadID, uint3 id : SV_DispatchThreadID)
{
    const int2 last = int2(gSize) - 1;
    const int2 origin = int2(gid.xy) * TILE - HALO;
    const uint flat = gtid.y * TILE + gtid.x;
    for (uint i = flat; i < SPAN * SPAN; i += TILE * TILE)
    {
        const int2 t = int2(i % SPAN, i / SPAN);
        const int2 p = clamp(origin + t, int2(0, 0), last);
        const float3 a = gNrIn.Load(int3(p, 0)).rgb;
        const float3 b = gNrOut.Load(int3(p, 0)).rgb;
        sIn[t.y][t.x] = a;
        sEd[t.y][t.x] = b - a;
    }
    GroupMemoryBarrierWithGroupSync();
    for (uint j = flat; j < RSPAN * RSPAN; j += TILE * TILE)
    {
        const int2 r = int2(j % RSPAN, j / RSPAN);
        float3 lo = sIn[r.y + 1][r.x + 1];
        float3 hi = lo;
        [unroll] for (int y = 0; y <= 2; ++y)
            [unroll] for (int x = 0; x <= 2; ++x)
            {
                const float3 v = sIn[r.y + y][r.x + x];
                lo = min(lo, v); hi = max(hi, v);
            }
        sLo[r.y][r.x] = lo; sHi[r.y][r.x] = hi;
    }
    GroupMemoryBarrierWithGroupSync();
    if (any(id.xy >= gSize)) return;
    const int2 local = int2(gtid.xy);
    const int2 c = local + HALO;
    const float3 nrIn = sIn[c.y][c.x];
    const float3 edit = sEd[c.y][c.x];
    float3 steady = edit;
    if ((gFlags & (STAB_FLAG_RESET | STAB_FLAG_PASS)) == 0u)
    {
        float3 lo = edit, hi = edit;
        [unroll] for (int y = -1; y <= 1; ++y)
            [unroll] for (int x = -1; x <= 1; ++x)
            {
                const float3 v = sEd[c.y + y][c.x + x];
                lo = min(lo, v); hi = max(hi, v);
            }
        const float2 centre = float2(id.xy) + 0.5;
        const uint2 mt = min(uint2(centre * float2(gMvSize) / float2(gSize)), gMvSize - 1u);
        float2 v = gMotion.Load(int3(mt, 0)) * gMvScale;
        const bool hasMotion = all(isfinite(v)) && dot(v, v) >= gMinMotion * gMinMotion;
        if (!hasMotion) v = 0.0;
        float eV = 1.0, e0 = 1.0;
        const float qV = hasMotion ? Quality(local, centre, v, eV) : 0.0;
        float q0 = 0.0;
        if (!hasMotion || (gFlags & STAB_FLAG_NO_ZERO) == 0u)
            q0 = Quality(local, centre, float2(0.0, 0.0), e0);
        const bool useV = hasMotion && (qV > q0 || (gFlags & STAB_FLAG_NO_ZERO) != 0u);
        const float2 u = useV ? v : float2(0.0, 0.0);
        float conf = useV ? qV : q0;
        if (gSubHi > 0.0) conf *= 1.0 - smoothstep(gSubLo, gSubHi, useV ? eV : e0);
        const float4 h = gHistory.SampleLevel(gLinear, (centre + u) / float2(gSize), 0);
        if (!all(isfinite(h)) || h.a < 0.999) conf = 0.0;
        if (conf > 0.0)
        {
            const float3 old = clamp(h.rgb, lo - gTol, hi + gTol);
            const float alpha = 1.0 - (1.0 - gAlphaMin) * conf;
            steady = old + alpha * (edit - old);
        }
    }
    gStab[id.xy]       = float4(saturate(nrIn + steady), 1.0);
    gHistoryOut[id.xy] = float4(steady, 1.0);
    gGuideOut[id.xy]   = float4(nrIn, 1.0);
}
)";

static struct StabState {
    winrt::com_ptr<ID3D12RootSignature> root;
    winrt::com_ptr<ID3D12PipelineState> pso;
    winrt::com_ptr<ID3D12DescriptorHeap> heap;   // two tables of 8: one per parity
    ID3D12Resource *out = nullptr, *hist[2] = {}, *guide[2] = {};
    ID3D12Resource *bound_in = nullptr, *bound_out = nullptr, *bound_mv = nullptr;
    UINT w = 0, hgt = 0;
    unsigned parity = 0;          // which history slot is READ this frame
    bool fresh = false;           // resources still in COMMON
    bool valid = false;           // the history may be used on the next evaluate
    bool failed = false;          // pipeline creation failed: off for the process
    bool said = false;
} g_stab;

static bool StabRequested()
{
    static int cached = -1;
    if (cached < 0)
    {
        char buf[8] = {};
        // On unless NS_STAB=0 (measured: the edit's instability -43% on a pan,
        // -45% on a moving object, -49% on still content, effect kept within
        // 2%, about +0.1 ms at 1248x702 and +0.3 ms at 2496x1404).
        cached = (GetEnvironmentVariableA("NS_STAB", buf, sizeof(buf)) > 0 && buf[0] == '0') ? 0 : 1;
    }
    return cached == 1;
}

static float StabStrength()
{
    static float cached = -1.0f;
    if (cached < 0.0f)
    {
        char buf[16] = {};
        cached = GetEnvironmentVariableA("NS_STAB_STRENGTH", buf, sizeof(buf)) > 0
            ? (std::min)(1.0f, (std::max)(0.0f, static_cast<float>(atof(buf)))) : 1.0f;
    }
    return cached;
}

// The history no longer describes the next frame's input.
static void StabForget() { g_stab.valid = false; }

static void CloseStab()
{
    auto &s = g_stab;
    if (g_submission_failed) { s.out = nullptr; s.hist[0] = s.hist[1] = nullptr; s.guide[0] = s.guide[1] = nullptr; }
    // Frames still in flight may read these: wait for the last one first.
    else if (s.out != nullptr && h.fence && h.fence_value != 0 &&
             !WaitFenceValue(h.fence, h.fence_value, 30000, "stab-close"))
        return;
    for (ID3D12Resource **r : {&s.out, &s.hist[0], &s.hist[1], &s.guide[0], &s.guide[1]})
        if (*r != nullptr) { (*r)->Release(); *r = nullptr; }
    s.bound_in = s.bound_out = s.bound_mv = nullptr;
    s.w = s.hgt = 0;
    s.parity = 0;
    s.valid = false;
    s.fresh = false;
}

static bool StabPipeline()
{
    auto &s = g_stab;
    if (s.pso) return true;
    if (s.failed) return false;
    D3D12_DESCRIPTOR_RANGE ranges[2] = {
        {D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 5, 0, 0, 0},
        {D3D12_DESCRIPTOR_RANGE_TYPE_UAV, 3, 0, 0, 5}};
    D3D12_ROOT_PARAMETER roots[2] = {};
    roots[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    roots[0].Constants = {0, 0, 16};
    roots[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    roots[1].DescriptorTable = {2, ranges};
    D3D12_STATIC_SAMPLER_DESC samp = {};
    samp.Filter = D3D12_FILTER_MIN_MAG_MIP_LINEAR;
    samp.AddressU = samp.AddressV = samp.AddressW = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
    samp.MaxLOD = D3D12_FLOAT32_MAX;
    samp.ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    D3D12_ROOT_SIGNATURE_DESC rd = {2, roots, 1, &samp, D3D12_ROOT_SIGNATURE_FLAG_NONE};
    winrt::com_ptr<ID3DBlob> code, errors;
    if (FAILED(D3D12SerializeRootSignature(&rd, D3D_ROOT_SIGNATURE_VERSION_1, code.put(), errors.put())) ||
        FAILED(h.dev->CreateRootSignature(0, code->GetBufferPointer(), code->GetBufferSize(),
                                          IID_PPV_ARGS(s.root.put()))))
    { s.failed = true; Log("[stab] root signature failed - the stabilizer stays off"); return false; }
    code = nullptr; errors = nullptr;
    if (FAILED(D3DCompile(kStabHlsl, sizeof(kStabHlsl) - 1, nullptr, nullptr, nullptr, "CSMain", "cs_5_0",
                          D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, code.put(), errors.put())))
    {
        s.failed = true;
        Log("[stab] shader failed to compile: %s - the stabilizer stays off",
            errors ? static_cast<const char *>(errors->GetBufferPointer()) : "?");
        return false;
    }
    D3D12_COMPUTE_PIPELINE_STATE_DESC pd = {};
    pd.pRootSignature = s.root.get();
    pd.CS = {code->GetBufferPointer(), code->GetBufferSize()};
    D3D12_DESCRIPTOR_HEAP_DESC hd = {D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV, 16,
                                     D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE, 0};
    if (FAILED(h.dev->CreateComputePipelineState(&pd, IID_PPV_ARGS(s.pso.put()))) ||
        FAILED(h.dev->CreateDescriptorHeap(&hd, IID_PPV_ARGS(s.heap.put()))))
    { s.failed = true; s.pso = nullptr; Log("[stab] pipeline failed - the stabilizer stays off"); return false; }
    return true;
}

// Resources at the network's size and the two descriptor tables. Rebuilt only
// when the inputs are new resources (RNSZ, MOTS) - never while a frame that
// reads the shader-visible heap may still be running.
static bool EnsureStab(VideoState &v, UINT nw, UINT nh)
{
    auto &s = g_stab;
    if (!StabPipeline()) return false;
    if (s.out && s.w == nw && s.hgt == nh && s.bound_in == v.nr_in &&
        s.bound_out == v.nr_out && s.bound_mv == v.mv.tex)
        return true;
    CloseStab();
    s.out = MakeTex(nw, nh, DXGI_FORMAT_R8G8B8A8_UNORM, true);
    for (int i = 0; i < 2; ++i)
    {
        s.hist[i] = MakeTex(nw, nh, DXGI_FORMAT_R16G16B16A16_FLOAT, true);
        s.guide[i] = MakeTex(nw, nh, DXGI_FORMAT_R8G8B8A8_UNORM, true);
    }
    if (!s.out || !s.hist[0] || !s.hist[1] || !s.guide[0] || !s.guide[1])
    { CloseStab(); Log("[stab] textures %ux%u could not be created", nw, nh); return false; }
    const UINT step = h.dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    D3D12_CPU_DESCRIPTOR_HANDLE cpu = s.heap->GetCPUDescriptorHandleForHeapStart();
    auto srv = [&](ID3D12Resource *r, DXGI_FORMAT f) {
        D3D12_SHADER_RESOURCE_VIEW_DESC sd = {};
        sd.Format = f; sd.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
        sd.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
        sd.Texture2D.MipLevels = 1;
        h.dev->CreateShaderResourceView(r, &sd, cpu); cpu.ptr += step;
    };
    auto uav = [&](ID3D12Resource *r, DXGI_FORMAT f) {
        D3D12_UNORDERED_ACCESS_VIEW_DESC ud = {};
        ud.Format = f; ud.ViewDimension = D3D12_UAV_DIMENSION_TEXTURE2D;
        h.dev->CreateUnorderedAccessView(r, nullptr, &ud, cpu); cpu.ptr += step;
    };
    for (unsigned p = 0; p < 2; ++p)   // table p: read slot p, write slot 1 - p
    {
        srv(v.nr_in, DXGI_FORMAT_R8G8B8A8_UNORM);
        srv(v.nr_out, DXGI_FORMAT_R8G8B8A8_UNORM);
        srv(v.mv.tex, DXGI_FORMAT_R16G16_FLOAT);
        srv(s.hist[p], DXGI_FORMAT_R16G16B16A16_FLOAT);
        srv(s.guide[p], DXGI_FORMAT_R8G8B8A8_UNORM);
        uav(s.out, DXGI_FORMAT_R8G8B8A8_UNORM);
        uav(s.hist[1 - p], DXGI_FORMAT_R16G16B16A16_FLOAT);
        uav(s.guide[1 - p], DXGI_FORMAT_R8G8B8A8_UNORM);
    }
    s.w = nw; s.hgt = nh;
    s.bound_in = v.nr_in; s.bound_out = v.nr_out; s.bound_mv = v.mv.tex;
    s.parity = 0;
    s.fresh = true;
    s.valid = false;
    if (!s.said)
    {
        s.said = true;
        Log("[stab] NR edit stabilizer on at %ux%u, strength %.2f (NS_STAB)", nw, nh, StabStrength());
    }
    return true;
}

// Records the pass. nr_in and v.mv.tex rest in NON_PIXEL_SHADER_RESOURCE and
// nr_out has just been put there; on return g_stab.out is in the same state,
// ready for the composite, and StabAfterCompose puts it back.
static bool StabilizeEdit(VideoState &v, UINT nw, UINT nh, bool reset)
{
    auto &s = g_stab;
    if (!EnsureStab(v, nw, nh)) return false;
    const unsigned r = s.parity, w = 1u - r;
    if (s.fresh)
    {
        D3D12_RESOURCE_BARRIER b[5] = {
            Transition(s.out, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_UNORDERED_ACCESS),
            Transition(s.hist[r], D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
            Transition(s.guide[r], D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
            Transition(s.hist[w], D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_UNORDERED_ACCESS),
            Transition(s.guide[w], D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_UNORDERED_ACCESS)};
        h.list->ResourceBarrier(5, b);
        s.fresh = false;
    }
    const float strength = StabStrength();
    struct { UINT size[2], mv[2]; float mvscale[2], alpha_min, tol, rng[4], sub[2], min_motion; UINT flags; } cb;
    cb.size[0] = nw; cb.size[1] = nh;
    cb.mv[0] = v.w; cb.mv[1] = v.hgt;
    cb.mvscale[0] = float(nw) / float(v.w); cb.mvscale[1] = float(nh) / float(v.hgt);
    cb.alpha_min = 1.0f - 0.75f * strength;
    cb.tol = 0.02f;
    cb.rng[0] = 0.004f; cb.rng[1] = 0.020f; cb.rng[2] = 0.020f; cb.rng[3] = 0.080f;
    cb.sub[0] = 0.03f; cb.sub[1] = 0.12f;
    cb.min_motion = 0.5f;
    cb.flags = (reset || !s.valid ? 1u : 0u) | (strength <= 0.0f ? 8u : 0u);
    static_assert(sizeof(cb) == 64, "StabCB is 16 constants");
    ID3D12DescriptorHeap *heaps[] = {s.heap.get()};
    h.list->SetDescriptorHeaps(1, heaps);
    h.list->SetComputeRootSignature(s.root.get());
    h.list->SetPipelineState(s.pso.get());
    h.list->SetComputeRoot32BitConstants(0, 16, &cb, 0);
    D3D12_GPU_DESCRIPTOR_HANDLE gpu = s.heap->GetGPUDescriptorHandleForHeapStart();
    gpu.ptr += UINT64(r) * 8u * h.dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    h.list->SetComputeRootDescriptorTable(1, gpu);
    h.list->Dispatch((nw + 7) / 8, (nh + 7) / 8, 1);
    D3D12_RESOURCE_BARRIER after[5] = {
        Transition(s.out, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
        Transition(s.hist[w], D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
        Transition(s.guide[w], D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
        Transition(s.hist[r], D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_UNORDERED_ACCESS),
        Transition(s.guide[r], D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_UNORDERED_ACCESS)};
    h.list->ResourceBarrier(5, after);
    // The barriers above already swap the roles, so the parity follows them
    // now; a frame that then fails to submit closes the resources (StabFailed).
    s.parity = w;
    s.valid = true;
    return true;
}

static void StabAfterCompose()
{
    D3D12_RESOURCE_BARRIER back = Transition(g_stab.out, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                                             D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    h.list->ResourceBarrier(1, &back);
}

// The frame that recorded the pass did not run: the tracked states are not
// the real ones, so the resources are made again next time.
static void StabFailed()
{
    if (g_stab.out != nullptr && !g_submission_failed)
        g_stab.bound_in = nullptr;   // forces EnsureStab to rebuild
    g_stab.valid = false;
}
