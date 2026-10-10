// The fisheye lens: the picture the viewer sees, re-projected from the
// rectilinear frame a game renders into a fisheye (equidistant) one.
//
// A game with a field of view of 110-120 degrees stretches the edges of the
// frame - that is what a rectilinear projection does to wide angles. A real
// wide lens does the opposite: equal angles take equal distances from the
// centre, so the edges are compressed and straight lines bow. Knowing the
// game's horizontal field of view is enough to undo one and apply the other:
//
//   rectilinear  r_src = f_rect * tan(theta)   f_rect = (W/2) / tan(fov/2)
//   equidistant  r_dst = f_fish * theta
//
// f_fish is chosen so that the frame's corners stay where they are: the
// corner's angle is the largest angle the source holds, and the mapping is
// convex, so every output pixel then lands inside the source - no black
// borders, and the price of the compression is that the centre is enlarged.
// A small field of view gives an almost unchanged picture; a large one, the
// look of a wide lens. A simple barrel distortion laid over a 90-degree frame
// looks false because it does not know what angles the pixels stand for.
//
// Where it runs: after the network, after the HDR composite and after Frame
// Generation - never on the network's input, the motion field or what DLSS-G
// interpolates (its motion is rectilinear). Each export site hands its source
// to LensApply, which writes the lens into a texture of its own and returns
// that in the source's state - or the source itself while the lens is off, so
// the ordinary path is the code it always was. The source is never written:
// the still-screen hold keeps v.output across frames, and a lens applied in
// place would compound.

static const char kLensHlsl[] = R"(
cbuffer LensCB : register(b0)
{
    uint2 gSize;      // output size = source size
    float gRect;      // rectilinear focal length, pixels
    float gFish;      // fisheye focal length, pixels
    float gNoise;     // webcam noise, 0..1
    uint  gSeed;      // the noise's frame: 30 a second, from the clock
    uint  gFlags;     // bit0 bend (the fisheye), bit1 a linear (scRGB) target
};
Texture2D<float4>   gSrc : register(t0);
RWTexture2D<float4> gDst : register(u0);
SamplerState        gLin : register(s0);

// Catmull-Rom through nine bilinear taps: sharper than one bilinear tap where
// the lens enlarges the centre, at a fixed cost.
float4 SampleCatmullRom(float2 uv, float2 size)
{
    float2 pos = uv * size;
    float2 c1 = floor(pos - 0.5) + 0.5;
    float2 f = pos - c1;
    float2 w0 = f * (-0.5 + f * (1.0 - 0.5 * f));
    float2 w1 = 1.0 + f * f * (-2.5 + 1.5 * f);
    float2 w2 = f * (0.5 + f * (2.0 - 1.5 * f));
    float2 w3 = f * f * (-0.5 + 0.5 * f);
    float2 w12 = w1 + w2;
    float2 o12 = w2 / w12;
    float2 t0 = (c1 - 1.0) / size;
    float2 t3 = (c1 + 2.0) / size;
    float2 t12 = (c1 + o12) / size;
    float4 r = 0;
    r += gSrc.SampleLevel(gLin, float2(t0.x,  t0.y),  0) * w0.x  * w0.y;
    r += gSrc.SampleLevel(gLin, float2(t12.x, t0.y),  0) * w12.x * w0.y;
    r += gSrc.SampleLevel(gLin, float2(t3.x,  t0.y),  0) * w3.x  * w0.y;
    r += gSrc.SampleLevel(gLin, float2(t0.x,  t12.y), 0) * w0.x  * w12.y;
    r += gSrc.SampleLevel(gLin, float2(t12.x, t12.y), 0) * w12.x * w12.y;
    r += gSrc.SampleLevel(gLin, float2(t3.x,  t12.y), 0) * w3.x  * w12.y;
    r += gSrc.SampleLevel(gLin, float2(t0.x,  t3.y),  0) * w0.x  * w3.y;
    r += gSrc.SampleLevel(gLin, float2(t12.x, t3.y),  0) * w12.x * w3.y;
    r += gSrc.SampleLevel(gLin, float2(t3.x,  t3.y),  0) * w3.x  * w3.y;
    return r;
}

uint Hash(uint x)
{
    // PCG output permutation: one 32-bit hash per lattice point.
    uint s = x * 747796405u + 2891336453u;
    uint w = ((s >> ((s >> 28u) + 4u)) ^ s) * 277803737u;
    return (w >> 22u) ^ w;
}

// Three roughly Gaussian values in [-1, 1] at a lattice point: the sum of two
// uniforms, a triangle, is close enough at this strength and costs no log.
float3 Lattice(int2 q, uint seed)
{
    uint h = Hash(uint(q.x) * 1973u ^ uint(q.y) * 9277u ^ seed * 26699u);
    uint g = Hash(h);
    float3 a = float3(h & 1023u, (h >> 10) & 1023u, (h >> 20) & 1023u) / 1023.0;
    float3 b = float3(g & 1023u, (g >> 10) & 1023u, (g >> 20) & 1023u) / 1023.0;
    return a + b - 1.0;
}

// A small sensor's noise, the way a webcam shows it: not single pixels but
// soft specks about the size of a 720p pixel (demosaicing and the encoder
// smear it), mostly brightness with a little colour, and stronger in the
// shadows, where the sensor has the fewest photons to work with.
float3 WebcamNoise(float3 c, uint2 pos)
{
    float cell = max(1.0, float(gSize.y) / 720.0);
    float2 q = (float2(pos) + 0.5) / cell;
    int2 i = int2(floor(q - 0.5));
    float2 f = smoothstep(0.0, 1.0, q - 0.5 - float2(i));
    float3 n = lerp(lerp(Lattice(i, gSeed), Lattice(i + int2(1, 0), gSeed), f.x),
                    lerp(Lattice(i + int2(0, 1), gSeed), Lattice(i + int2(1, 1), gSeed), f.x), f.y);
    // In display terms: a linear target is encoded for the measure and back.
    bool lin = (gFlags & 2u) != 0;
    float3 e = lin ? pow(max(c, 0.0), 1.0 / 2.2) : c;
    float y = saturate(dot(e, float3(0.2126, 0.7152, 0.0722)));
    // Measured on the worker: at 1.0 about 10 codes of spread in the shadows
    // and 4 in the light - a cheap webcam in a dim room; 0.3 is a hint of it.
    float sigma = gNoise * (0.03 + 0.12 * (1.0 - y) * (1.0 - y));
    float3 chroma = float3(n.y, -0.5 * (n.y + n.z), n.z) * 0.35;
    e += sigma * (n.x + chroma);
    return lin ? pow(max(e, 0.0), 2.2) : e;
}

[numthreads(8, 8, 1)]
void CSMain(uint3 id : SV_DispatchThreadID)
{
    if (id.x >= gSize.x || id.y >= gSize.y) return;
    float2 size = float2(gSize);
    float4 c;
    if ((gFlags & 1u) != 0)
    {
        float2 centre = size * 0.5;
        float2 d = float2(id.xy) + 0.5 - centre;
        float r = length(d);
        float theta = r / gFish;
        if (theta >= 1.5607)       // past ~89.4 degrees: no source pixel to take
        {
            gDst[id.xy] = float4(0, 0, 0, 1);
            return;
        }
        float2 p = r > 1e-4 ? centre + d * (gRect * tan(theta) / r) : centre;
        if (any(p < 0.0) || any(p > size))
        {
            gDst[id.xy] = float4(0, 0, 0, 1);
            return;
        }
        c = SampleCatmullRom(p / size, size);
    }
    else
        c = gSrc.Load(int3(id.xy, 0));
    if (gNoise > 0.0)
        c.rgb = WebcamNoise(c.rgb, id.xy);
    // The filter overshoots a little at hard edges: never below black. Above
    // one is a real value in scRGB, and a UNORM target clamps it on store.
    gDst[id.xy] = float4(max(c.rgb, 0.0), 1.0);
}
)";

// The lens settings, from LENS (client -> worker). Read between frames.
static bool  g_lens_on = false;
static float g_lens_fov = 110.0f;   // the game's horizontal field of view, degrees
static float g_lens_noise = 0.0f;   // webcam noise over the lens, 0..1

static constexpr float kLensFovMin = 30.0f;
static constexpr float kLensFovMax = 170.0f;

// One target per use within a frame: what is shown, what is exported (Spout
// and the GPU recording), a second recording source on the HDR path, the
// pixel readback, and a scratch for Frame Generation's frames, which is
// copied back at once. A target outlives its list: the shown one is read by
// the back-buffer copy after the producer's list has run.
enum LensSlot { LENS_SHOWN, LENS_EXPORT, LENS_RECORD, LENS_PIXELS, LENS_FG, LENS_SLOTS };

static struct LensState {
    winrt::com_ptr<ID3D12RootSignature> root;
    winrt::com_ptr<ID3D12PipelineState> pso;
    winrt::com_ptr<ID3D12DescriptorHeap> heap;
    ID3D12Resource *target[LENS_SLOTS] = {};
    D3D12_RESOURCE_STATES state[LENS_SLOTS] = {};
    UINT ring = 0;               // next SRV/UAV pair in the heap
    bool failed = false;         // pipeline creation failed: off for the process
    bool said = false;
} g_lens;

// Descriptor pairs written one per dispatch, round the ring. Every frame
// waits for its own work before the next one is recorded (the deferred tail
// keeps at most one in flight), so a pair is rewritten only long after the
// GPU last read it - at most eight dispatches a frame against 256 pairs.
static constexpr UINT kLensPairs = 256;

static bool LensActive()
{
    return g_lens_on && !g_lens.failed && !g_submission_failed;
}

static bool LensPipeline()
{
    auto &s = g_lens;
    if (s.pso) return true;
    if (s.failed) return false;
    D3D12_DESCRIPTOR_RANGE ranges[2] = {
        {D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 1, 0, 0, 0},
        {D3D12_DESCRIPTOR_RANGE_TYPE_UAV, 1, 0, 0, 1}};
    D3D12_ROOT_PARAMETER roots[2] = {};
    roots[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    roots[0].Constants = {0, 0, 7};
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
    { s.failed = true; Log("[lens] root signature failed - the lens stays off"); return false; }
    code = nullptr; errors = nullptr;
    if (FAILED(D3DCompile(kLensHlsl, sizeof(kLensHlsl) - 1, "lens.hlsl", nullptr, nullptr, "CSMain",
                          "cs_5_0", D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, code.put(), errors.put())))
    {
        s.failed = true;
        Log("[lens] shader failed to compile: %s - the lens stays off",
            errors ? static_cast<const char *>(errors->GetBufferPointer()) : "?");
        return false;
    }
    D3D12_COMPUTE_PIPELINE_STATE_DESC pd = {};
    pd.pRootSignature = s.root.get();
    pd.CS = {code->GetBufferPointer(), code->GetBufferSize()};
    D3D12_DESCRIPTOR_HEAP_DESC hd = {D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV, 2 * kLensPairs,
                                     D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE, 0};
    if (FAILED(h.dev->CreateComputePipelineState(&pd, IID_PPV_ARGS(s.pso.put()))) ||
        FAILED(h.dev->CreateDescriptorHeap(&hd, IID_PPV_ARGS(s.heap.put()))))
    { s.failed = true; s.pso = nullptr; Log("[lens] pipeline failed - the lens stays off"); return false; }
    return true;
}

// Every lens target released; the next use makes them again. The producer
// queue is waited for first (the back-buffer copy that reads the shown target
// is waited for on the CPU when it is made); after a failed submission the
// targets are kept until the process ends.
static void CloseLens()
{
    if (g_submission_failed) return;
    bool any = false;
    for (int i = 0; i < LENS_SLOTS; ++i) any = any || g_lens.target[i] != nullptr;
    if (any && h.fence && h.fence_value != 0 &&
        !WaitFenceValue(h.fence, h.fence_value, 30000, "lens-close"))
        return;
    for (int i = 0; i < LENS_SLOTS; ++i)
        if (g_lens.target[i] != nullptr) { g_lens.target[i]->Release(); g_lens.target[i] = nullptr; }
}

// The slot's target in the source's size and format, made on first use and
// whenever either changes (a resize, HDR switching the composite's format).
static ID3D12Resource *LensTarget(LensSlot slot, const D3D12_RESOURCE_DESC &src)
{
    auto &s = g_lens;
    ID3D12Resource *&t = s.target[slot];
    if (t != nullptr)
    {
        const auto d = t->GetDesc();
        if (d.Width == src.Width && d.Height == src.Height && d.Format == src.Format) return t;
        // Its last reader is this frame's predecessor at the latest: wait for
        // the producer queue before it goes. A size or format change is a
        // rare event - a resize, an HDR switch - never a per-frame one.
        if (h.fence && h.fence_value != 0 &&
            !WaitFenceValue(h.fence, h.fence_value, 30000, "lens-resize"))
            return nullptr;
        t->Release();
        t = nullptr;
    }
    t = MakeTex(static_cast<UINT>(src.Width), src.Height, src.Format, true);
    s.state[slot] = D3D12_RESOURCE_STATE_COMMON;
    if (t == nullptr)
        Log("[lens] target %llux%u could not be created - this frame goes out without the lens",
            static_cast<unsigned long long>(src.Width), src.Height);
    return t;
}

// The lens of `src` (resting in `state`), recorded into `list`: returns the
// lens target in that same state, so the caller's own barriers and copies
// work on it unchanged - or `src` itself when the lens is off or cannot run.
static ID3D12Resource *LensApply(ID3D12GraphicsCommandList *list, ID3D12Resource *src,
                                 D3D12_RESOURCE_STATES state, LensSlot slot)
{
    if (!LensActive() || src == nullptr || list == nullptr) return src;
    if (!LensPipeline()) return src;
    auto &s = g_lens;
    const auto desc = src->GetDesc();
    ID3D12Resource *dst = LensTarget(slot, desc);
    if (dst == nullptr) return src;
    if (!s.said)
    {
        Log("[lens] on: fisheye from a %.0f-degree field of view, webcam noise %.0f%%",
            g_lens_fov, g_lens_noise * 100.0f);
        s.said = true;
    }
    const UINT w = static_cast<UINT>(desc.Width), hgt = desc.Height;
    // The focal lengths, from the field of view and this frame's size.
    const float fov = std::clamp(g_lens_fov, kLensFovMin, kLensFovMax) * 3.14159265f / 180.0f;
    const float half_w = 0.5f * static_cast<float>(w);
    const float half_diag = 0.5f * std::sqrt(static_cast<float>(w) * w + static_cast<float>(hgt) * hgt);
    const float f_rect = half_w / std::tan(0.5f * fov);
    const float corner = std::atan(half_diag / f_rect);
    // The noise changes 30 times a second by the clock, not per dispatch:
    // what is shown, exported and recorded in one frame carries the same
    // specks, and Frame Generation's frames are not each given their own -
    // a sensor reads out at its own rate whatever the display does.
    const bool linear = desc.Format == DXGI_FORMAT_R16G16B16A16_FLOAT;
    struct { UINT size[2]; float rect, fish, noise; UINT seed, flags; } cb = {
        {w, hgt}, f_rect, half_diag / corner, std::clamp(g_lens_noise, 0.0f, 1.0f),
        static_cast<UINT>(GetTickCount64() / 33u), 1u | (linear ? 2u : 0u)};

    const UINT step = h.dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    const UINT pair = s.ring++ % kLensPairs;
    D3D12_CPU_DESCRIPTOR_HANDLE cpu = s.heap->GetCPUDescriptorHandleForHeapStart();
    cpu.ptr += static_cast<SIZE_T>(pair) * 2u * step;
    D3D12_SHADER_RESOURCE_VIEW_DESC sd = {};
    sd.Format = desc.Format;
    sd.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
    sd.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
    sd.Texture2D.MipLevels = 1;
    h.dev->CreateShaderResourceView(src, &sd, cpu);
    cpu.ptr += step;
    D3D12_UNORDERED_ACCESS_VIEW_DESC ud = {};
    ud.Format = desc.Format;
    ud.ViewDimension = D3D12_UAV_DIMENSION_TEXTURE2D;
    h.dev->CreateUnorderedAccessView(dst, nullptr, &ud, cpu);

    D3D12_RESOURCE_BARRIER pre[2];
    UINT npre = 0;
    if (state != D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE)
        pre[npre++] = Transition(src, state, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    if (s.state[slot] != D3D12_RESOURCE_STATE_UNORDERED_ACCESS)
        pre[npre++] = Transition(dst, s.state[slot], D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    else
    {
        // Back to back on the same target (Frame Generation's scratch): the
        // previous write must land before this one.
        pre[npre] = {};
        pre[npre].Type = D3D12_RESOURCE_BARRIER_TYPE_UAV;
        pre[npre].UAV.pResource = dst;
        ++npre;
    }
    list->ResourceBarrier(npre, pre);
    ID3D12DescriptorHeap *heaps[] = {s.heap.get()};
    list->SetDescriptorHeaps(1, heaps);
    list->SetComputeRootSignature(s.root.get());
    list->SetPipelineState(s.pso.get());
    list->SetComputeRoot32BitConstants(0, 7, &cb, 0);
    D3D12_GPU_DESCRIPTOR_HANDLE gpu = s.heap->GetGPUDescriptorHandleForHeapStart();
    gpu.ptr += static_cast<UINT64>(pair) * 2u * step;
    list->SetComputeRootDescriptorTable(1, gpu);
    list->Dispatch((w + 7) / 8, (hgt + 7) / 8, 1);
    D3D12_RESOURCE_BARRIER post[2];
    UINT npost = 0;
    if (state != D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE)
        post[npost++] = Transition(src, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, state);
    if (state != D3D12_RESOURCE_STATE_UNORDERED_ACCESS)
        post[npost++] = Transition(dst, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, state);
    if (npost) list->ResourceBarrier(npost, post);
    s.state[slot] = state;
    return dst;
}

// The Frame Generation path's frames, warped where they lie: `image` rests in
// COPY_SOURCE (a generated frame in its slot, or a slot's real frame) and is
// overwritten with its own lens through the scratch target.
static void LensInPlace(ID3D12GraphicsCommandList *list, ID3D12Resource *image)
{
    if (!LensActive() || image == nullptr) return;
    ID3D12Resource *lens = LensApply(list, image, D3D12_RESOURCE_STATE_COPY_SOURCE, LENS_FG);
    if (lens == image) return;
    D3D12_RESOURCE_BARRIER to = Transition(image, D3D12_RESOURCE_STATE_COPY_SOURCE,
                                           D3D12_RESOURCE_STATE_COPY_DEST);
    list->ResourceBarrier(1, &to);
    list->CopyResource(image, lens);
    D3D12_RESOURCE_BARRIER back = Transition(image, D3D12_RESOURCE_STATE_COPY_DEST,
                                             D3D12_RESOURCE_STATE_COPY_SOURCE);
    list->ResourceBarrier(1, &back);
}
