// GPU recording - see gpu_recorder.h for the shape of it.
#include "gpu_recorder.h"

#include <d3d11_4.h>
#include <d3dcompiler.h>
#include <dxgi1_4.h>
#include <mfapi.h>
#include <mferror.h>
#include <mfidl.h>
#include <mfreadwrite.h>
#include <codecapi.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdarg>
#include <cstdio>
#include <deque>
#include <future>
#include <mutex>
#include <thread>

namespace {

// The worker's own log format, so these lines sit in NeuralScreen.log like
// every other one (the client lets "[grec]" through - pipeline._LOG_ALWAYS).
void Log(const char *fmt, ...)
{
    char line[1024];
    va_list ap;
    va_start(ap, fmt);
    _vsnprintf_s(line, sizeof(line), _TRUNCATE, fmt, ap);
    va_end(ap);
    SYSTEMTIME st;
    GetLocalTime(&st);
    fprintf(stderr, "%02u:%02u:%02u.%03u  %s\n", st.wHour, st.wMinute,
            st.wSecond, st.wMilliseconds, line);
    fflush(stderr);
}

template <class T> void SafeRelease(T *&p)
{
    if (p != nullptr) { p->Release(); p = nullptr; }
}

//: RGBA copies in flight between the worker's queue and the encoder. Three
//: is one being encoded, one waiting, one being written - more only buys
//: latency, fewer drops frames on the first hiccup.
constexpr int kSlots = 3;
//: NV12 surfaces the encoder may hold at once. NVENC keeps a few for its
//: lookahead; the rest is headroom for a sink that writes in bursts.
constexpr int kSurfaces = 8;
//: Audio is handed over in pieces of at least 10 ms and at most 100 ms.
constexpr int64_t kAudioMinFrames = 480;
constexpr int64_t kAudioMaxFrames = 4800;

struct Slot
{
    ID3D11Texture2D *tex11 = nullptr;   // the video processor reads this
    ID3D12Resource *tex12 = nullptr;    // the worker copies into this - the same memory
    ID3D11ShaderResourceView *srv = nullptr;   // the HDR10 conversion reads this
    HANDLE nt = nullptr;
    UINT w = 0, h = 0;
    DXGI_FORMAT format = DXGI_FORMAT_UNKNOWN;
    std::atomic<bool> busy{false};
};

struct Recorder;

// Guards SampleReturn::r_ against Teardown detaching it: a sample the encoder
// still held after Teardown's wait comes back later, possibly from one of
// Media Foundation's threads, after the Recorder has been deleted.
std::mutex g_return_mu;

// Hands an NV12 surface back when the encoder has let go of its sample. A
// tracked sample does not destroy itself when its last outside reference
// goes: it calls this, with itself as the result's object.
class SampleReturn : public IMFAsyncCallback
{
public:
    SampleReturn(Recorder *r, int index) : r_(r), index_(index) {}
    // The recorder is going away: a late return only releases its sample.
    // Called with g_return_mu held.
    void Detach() { r_ = nullptr; }
    STDMETHODIMP QueryInterface(REFIID riid, void **ppv) override
    {
        if (ppv == nullptr) return E_POINTER;
        if (riid == __uuidof(IUnknown) || riid == __uuidof(IMFAsyncCallback))
        {
            *ppv = static_cast<IMFAsyncCallback *>(this);
            AddRef();
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&ref_); }
    STDMETHODIMP_(ULONG) Release() override
    {
        const ULONG r = InterlockedDecrement(&ref_);
        if (r == 0) delete this;
        return r;
    }
    STDMETHODIMP GetParameters(DWORD *, DWORD *) override { return E_NOTIMPL; }
    STDMETHODIMP Invoke(IMFAsyncResult *result) override;

private:
    LONG ref_ = 1;
    Recorder *r_;
    int index_;
};

struct Surface
{
    ID3D11Texture2D *tex = nullptr;
    // HDR10: the P010 surface's two planes as render targets - 10-bit luma,
    // and chroma at half the size (HdrConvert).
    ID3D11RenderTargetView *rtv_y = nullptr, *rtv_uv = nullptr;
    // Our reference to the sample while the surface is free; nullptr while
    // the encoder holds it (SampleReturn puts it back).
    IMFSample *sample = nullptr;
    SampleReturn *callback = nullptr;
    std::atomic<bool> busy{false};
};

struct Item
{
    int slot;
    ID3D12Fence *fence;
    UINT64 value;
    int64_t time;
};

struct Recorder
{
    ID3D12Device *dev12 = nullptr;
    ID3D11Device *d11 = nullptr;
    ID3D11DeviceContext *ctx = nullptr;
    ID3D11VideoDevice *vdev = nullptr;
    ID3D11VideoContext *vctx = nullptr;
    ID3D11VideoProcessorEnumerator *vpe = nullptr;
    ID3D11VideoProcessor *vp = nullptr;
    UINT vp_in_w = 0, vp_in_h = 0;
    DXGI_FORMAT vp_in_format = DXGI_FORMAT_UNKNOWN;
    ID3D11Query *blit_done = nullptr;

    IMFDXGIDeviceManager *manager = nullptr;
    UINT manager_token = 0;
    IMFSinkWriter *writer = nullptr;
    DWORD video_stream = 0, audio_stream = 0;
    bool has_audio = false;
    uint32_t codec = 0;

    HANDLE audio_map = nullptr;
    GpuRecAudioRing *ring = nullptr;
    const int16_t *ring_data = nullptr;
    int64_t audio_read = 0;
    int64_t audio_base = 0;          // the ring frame that is time 0 in the file
    // The ring's size in frames, taken once at open and checked against the
    // mapping: the header is the client's to write, and a capacity read anew
    // on every pump could turn 0 (a division by zero) or outgrow the view.
    int64_t audio_capacity = 0;
    uint32_t audio_rate = 0, audio_channels = 0;
    bool audio_overrun_logged = false;

    Slot slots[kSlots];
    Surface surfaces[kSurfaces];
    UINT w = 0, h = 0, fps = 60;
    uint32_t bitrate = 0;
    // HDR10: 10-bit BT.2020 PQ from the frame to the file - P010 surfaces, a
    // Main10 profile, the colour tagged. Asked for at the start; cleared when
    // no 10-bit encoder takes it, and the recording goes on in SDR.
    bool hdr = false;
    UINT max_nits = 1000;
    // The HDR10 conversion's pipeline (EnsureHdrConverter), made once.
    ID3D11VertexShader *hdr_vs = nullptr;
    ID3D11PixelShader *hdr_ps_y = nullptr, *hdr_ps_uv = nullptr;
    ID3D11SamplerState *hdr_sampler = nullptr;
    ID3D11Buffer *hdr_cb = nullptr;
    UINT hdr_cb_w = 0, hdr_cb_h = 0;           // the slot size the constants are for
    // client_qpc: when the client's ring frame 0 was (its clock). origin_qpc:
    // when the file's time 0 is - the moment the encoder was ready, so a slow
    // setup (half a second when a codec is refused first) delays the whole
    // recording instead of opening it with a gap that only the sound fills.
    int64_t client_qpc = 0, origin_qpc = 0, qpc_freq = 1;
    int64_t last_slot = -1;
    wchar_t path[1024] = {};

    std::thread thread;
    // Lives here, not on GpuRecStart's stack: set_value may still be inside
    // the promise when the waiting thread wakes up and returns.
    std::promise<HRESULT> ready;
    std::mutex mu;
    std::condition_variable cv;
    std::deque<Item> queue;
    bool stopping = false;
    HANDLE fence_event = nullptr;
    std::atomic<uint32_t> written{0}, dropped{0};
    std::atomic<HRESULT> error{S_OK};
    ULONGLONG started_tick = 0, stopped_tick = 0;
    // NS_TEST_FAIL_STAGE=grec-write: the writes start failing after this
    // many frames, the way a full disk would (0 = never). The release tests
    // drive the worker's auto-stop with it.
    uint32_t fail_after = 0;
    // The converted frame waiting for its successor. A frame's duration is
    // the gap to the next one, and that is only known when the next one
    // arrives: when the pipeline runs slower than the recording's clock most
    // slots are empty, and a fixed 1/fps duration made the fragmented MP4 lay
    // each fragment's frames end to end - the picture ran ahead of the sound
    // by ~7% of the recording at 30 fps. Held with its allocator set, so its
    // surface is simply one more in flight.
    IMFSample *held = nullptr;
    int64_t held_time = 0;
};

Recorder *g_rec = nullptr;
std::atomic<bool> g_active{false};

STDMETHODIMP SampleReturn::Invoke(IMFAsyncResult *result)
{
    IUnknown *object = nullptr;
    IMFSample *sample = nullptr;
    if (result != nullptr && SUCCEEDED(result->GetObject(&object)) && object != nullptr)
    {
        object->QueryInterface(IID_PPV_ARGS(&sample));
        object->Release();
    }
    std::lock_guard<std::mutex> guard(g_return_mu);
    Recorder *r = r_;
    if (r == nullptr)
    {
        // Returned after Teardown gave up waiting: the Recorder may be gone.
        if (sample != nullptr) sample->Release();
        return S_OK;
    }
    Surface &s = r->surfaces[index_];
    {
        std::lock_guard<std::mutex> lock(r->mu);
        s.sample = sample;       // the reference GetObject handed us is ours again
        s.busy.store(false);
    }
    r->cv.notify_all();
    return S_OK;
}

void Fail(Recorder *r, const char *where, HRESULT hr)
{
    HRESULT expected = S_OK;
    if (r->error.compare_exchange_strong(expected, hr))
        Log("[grec] %s failed 0x%08X", where, static_cast<unsigned>(hr));
}

// --- devices ---------------------------------------------------------------

HRESULT CreateDevices(Recorder *r)
{
    // The worker's adapter, by LUID - the Spout bridge's rule, for the same
    // reason: a D3D11 device on another card would copy every frame across
    // the bus, silently.
    const LUID want = r->dev12->GetAdapterLuid();
    IDXGIFactory1 *factory = nullptr;
    IDXGIAdapter1 *adapter = nullptr;
    if (SUCCEEDED(CreateDXGIFactory1(IID_PPV_ARGS(&factory))))
    {
        IDXGIAdapter1 *candidate = nullptr;
        for (UINT i = 0; factory->EnumAdapters1(i, &candidate) != DXGI_ERROR_NOT_FOUND; ++i)
        {
            DXGI_ADAPTER_DESC1 ad = {};
            if (candidate == nullptr) continue;
            if (SUCCEEDED(candidate->GetDesc1(&ad)) &&
                ad.AdapterLuid.LowPart == want.LowPart &&
                ad.AdapterLuid.HighPart == want.HighPart)
            { adapter = candidate; break; }
            candidate->Release();
            candidate = nullptr;
        }
    }
    const D3D_FEATURE_LEVEL levels[] = { D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0 };
    HRESULT hr = D3D11CreateDevice(
        adapter, adapter != nullptr ? D3D_DRIVER_TYPE_UNKNOWN : D3D_DRIVER_TYPE_HARDWARE,
        nullptr, D3D11_CREATE_DEVICE_VIDEO_SUPPORT | D3D11_CREATE_DEVICE_BGRA_SUPPORT,
        levels, _countof(levels), D3D11_SDK_VERSION, &r->d11, nullptr, &r->ctx);
    SafeRelease(adapter);
    SafeRelease(factory);
    if (FAILED(hr)) return hr;
    // Media Foundation drives this device from its own threads.
    ID3D10Multithread *mt = nullptr;
    if (SUCCEEDED(r->d11->QueryInterface(IID_PPV_ARGS(&mt))))
    {
        mt->SetMultithreadProtected(TRUE);
        mt->Release();
    }
    hr = r->d11->QueryInterface(IID_PPV_ARGS(&r->vdev));
    if (FAILED(hr)) return hr;
    hr = r->ctx->QueryInterface(IID_PPV_ARGS(&r->vctx));
    if (FAILED(hr)) return hr;
    D3D11_QUERY_DESC qd = { D3D11_QUERY_EVENT, 0 };
    hr = r->d11->CreateQuery(&qd, &r->blit_done);
    if (FAILED(hr)) return hr;
    hr = MFCreateDXGIDeviceManager(&r->manager_token, &r->manager);
    if (FAILED(hr)) return hr;
    return r->manager->ResetDevice(r->d11, r->manager_token);
}

// --- the audio ring --------------------------------------------------------

bool OpenAudioRing(Recorder *r, const char *name)
{
    if (name == nullptr || name[0] == '\0') return false;
    r->audio_map = OpenFileMappingA(FILE_MAP_READ, FALSE, name);
    if (r->audio_map == nullptr)
    {
        Log("[grec] audio ring '%s' not found (err %lu) - recording without sound",
            name, GetLastError());
        return false;
    }
    void *view = MapViewOfFile(r->audio_map, FILE_MAP_READ, 0, 0, 0);
    if (view == nullptr)
    {
        Log("[grec] audio ring map failed (err %lu)", GetLastError());
        CloseHandle(r->audio_map);
        r->audio_map = nullptr;
        return false;
    }
    r->ring = static_cast<GpuRecAudioRing *>(view);
    if (r->ring->magic != GPUREC_AUDIO_MAGIC || r->ring->channels == 0 ||
        r->ring->channels > 8 || r->ring->capacity == 0 ||
        (r->ring->rate != 44100 && r->ring->rate != 48000))
    {
        Log("[grec] audio ring is not in the agreed shape (rate %u, %u ch) - "
            "recording without sound", r->ring->rate, r->ring->channels);
        UnmapViewOfFile(view);
        CloseHandle(r->audio_map);
        r->audio_map = nullptr;
        r->ring = nullptr;
        return false;
    }
    // The samples have to fit in what is actually mapped: capacity * channels
    // int16 after the header. The view is as large as the section (size 0
    // above), and VirtualQuery says how large that is.
    MEMORY_BASIC_INFORMATION mbi = {};
    const unsigned long long need = sizeof(GpuRecAudioRing) +
        static_cast<unsigned long long>(r->ring->capacity) * r->ring->channels * 2ull;
    if (VirtualQuery(view, &mbi, sizeof(mbi)) == 0 || need > mbi.RegionSize)
    {
        Log("[grec] audio ring claims %u frames, larger than its mapping - "
            "recording without sound", r->ring->capacity);
        UnmapViewOfFile(view);
        CloseHandle(r->audio_map);
        r->audio_map = nullptr;
        r->ring = nullptr;
        return false;
    }
    r->ring_data = reinterpret_cast<const int16_t *>(
        reinterpret_cast<const BYTE *>(view) + sizeof(GpuRecAudioRing));
    r->audio_rate = r->ring->rate;
    r->audio_channels = r->ring->channels;
    r->audio_capacity = r->ring->capacity;
    return true;
}

// --- the writer ------------------------------------------------------------

struct CodecChoice { GUID subtype; uint32_t id; const char *name; };

const CodecChoice kAv1 = { MFVideoFormat_AV1, GPUREC_CODEC_AV1, "AV1" };
const CodecChoice kHevc = { MFVideoFormat_HEVC, GPUREC_CODEC_HEVC, "HEVC" };
const CodecChoice kH264 = { MFVideoFormat_H264, GPUREC_CODEC_H264, "H.264" };

void SetVideoFormat(IMFMediaType *t, const GUID &subtype, UINT w, UINT h, UINT fps,
                    bool hdr)
{
    t->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
    t->SetGUID(MF_MT_SUBTYPE, subtype);
    MFSetAttributeSize(t, MF_MT_FRAME_SIZE, w, h);
    MFSetAttributeRatio(t, MF_MT_FRAME_RATE, fps, 1);
    MFSetAttributeRatio(t, MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
    t->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
    // The colour the video processor converts to, said the same way to the
    // encoder and in the file: BT.709 studio range - what every player
    // assumes for untagged HD video anyway, so nobody sees a contrast shift.
    t->SetUINT32(MF_MT_VIDEO_NOMINAL_RANGE, MFNominalRange_16_235);
    if (hdr)
    {
        // HDR10, as the video processor writes it into P010: BT.2020
        // primaries and matrix, the PQ curve. Tagged, a player tone-maps it;
        // untagged, it would show PQ code values as if they were SDR - the
        // washed-out grey an untagged HDR file is known for.
        t->SetUINT32(MF_MT_YUV_MATRIX, MFVideoTransferMatrix_BT2020_10);
        t->SetUINT32(MF_MT_VIDEO_PRIMARIES, MFVideoPrimaries_BT2020);
        t->SetUINT32(MF_MT_TRANSFER_FUNCTION, MFVideoTransFunc_2084);
        return;
    }
    t->SetUINT32(MF_MT_YUV_MATRIX, MFVideoTransferMatrix_BT709);
    t->SetUINT32(MF_MT_VIDEO_PRIMARIES, MFVideoPrimaries_BT709);
    t->SetUINT32(MF_MT_TRANSFER_FUNCTION, MFVideoTransFunc_709);
}

HRESULT AddVideo(Recorder *r, IMFSinkWriter *writer, const CodecChoice &c,
                 bool with_params, DWORD *stream)
{
    IMFMediaType *out = nullptr, *in = nullptr;
    IMFAttributes *params = nullptr;
    HRESULT hr = MFCreateMediaType(&out);
    if (SUCCEEDED(hr))
    {
        SetVideoFormat(out, c.subtype, r->w, r->h, r->fps, r->hdr);
        out->SetUINT32(MF_MT_AVG_BITRATE, r->bitrate);
        if (c.id == GPUREC_CODEC_H264)
            out->SetUINT32(MF_MT_MPEG2_PROFILE, eAVEncH264VProfile_High);
        else if (c.id == GPUREC_CODEC_HEVC)
            out->SetUINT32(MF_MT_MPEG2_PROFILE, r->hdr ? eAVEncH265VProfile_Main_420_10
                                                      : eAVEncH265VProfile_Main_420_8);
        else if (c.id == GPUREC_CODEC_AV1 && r->hdr)
            out->SetUINT32(MF_MT_MPEG2_PROFILE, eAVEncAV1VProfile_Main_420_10);
        if (r->hdr)
        {
            // The mastering display, for a player's tone mapping: this
            // display's peak (ST 2086). The content's own light levels
            // (MaxCLL/MaxFALL) are not known before it is recorded: unset.
            out->SetUINT32(MF_MT_MAX_MASTERING_LUMINANCE, r->max_nits);
            out->SetUINT32(MF_MT_MIN_MASTERING_LUMINANCE, 50);   // 0.005 nits (x0.0001)
        }
        hr = writer->AddStream(out, stream);
    }
    if (SUCCEEDED(hr)) hr = MFCreateMediaType(&in);
    if (SUCCEEDED(hr))
    {
        SetVideoFormat(in, r->hdr ? MFVideoFormat_P010 : MFVideoFormat_NV12,
                       r->w, r->h, r->fps, r->hdr);
        if (with_params && SUCCEEDED(MFCreateAttributes(&params, 4)))
        {
            // Variable bitrate around the mean with room for motion, as the
            // old recorder's "rc vbr, maxrate" did; a keyframe every two
            // seconds so the file seeks well; no B-frames, as before.
            params->SetUINT32(CODECAPI_AVEncCommonRateControlMode,
                              eAVEncCommonRateControlMode_PeakConstrainedVBR);
            params->SetUINT32(CODECAPI_AVEncCommonMeanBitRate, r->bitrate);
            params->SetUINT32(CODECAPI_AVEncCommonMaxBitRate,
                              (std::min)(r->bitrate * 2u, 400000000u));
            params->SetUINT32(CODECAPI_AVEncMPVGOPSize, r->fps * 2);
            params->SetUINT32(CODECAPI_AVEncMPVDefaultBPictureCount, 0);
        }
        hr = writer->SetInputMediaType(*stream, in, params);
    }
    SafeRelease(params);
    SafeRelease(in);
    SafeRelease(out);
    return hr;
}

HRESULT AddAudio(Recorder *r, IMFSinkWriter *writer, DWORD *stream)
{
    IMFMediaType *out = nullptr, *in = nullptr;
    HRESULT hr = MFCreateMediaType(&out);
    if (SUCCEEDED(hr))
    {
        out->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Audio);
        out->SetGUID(MF_MT_SUBTYPE, MFAudioFormat_AAC);
        out->SetUINT32(MF_MT_AUDIO_BITS_PER_SAMPLE, 16);
        out->SetUINT32(MF_MT_AUDIO_SAMPLES_PER_SECOND, r->audio_rate);
        out->SetUINT32(MF_MT_AUDIO_NUM_CHANNELS, r->audio_channels);
        // 192 kbit/s, the old recorder's rate - one of the four the Windows
        // AAC encoder accepts (12000/16000/20000/24000 bytes a second).
        out->SetUINT32(MF_MT_AUDIO_AVG_BYTES_PER_SECOND, 24000);
        hr = writer->AddStream(out, stream);
    }
    if (SUCCEEDED(hr)) hr = MFCreateMediaType(&in);
    if (SUCCEEDED(hr))
    {
        in->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Audio);
        in->SetGUID(MF_MT_SUBTYPE, MFAudioFormat_PCM);
        in->SetUINT32(MF_MT_AUDIO_BITS_PER_SAMPLE, 16);
        in->SetUINT32(MF_MT_AUDIO_SAMPLES_PER_SECOND, r->audio_rate);
        in->SetUINT32(MF_MT_AUDIO_NUM_CHANNELS, r->audio_channels);
        in->SetUINT32(MF_MT_AUDIO_BLOCK_ALIGNMENT, r->audio_channels * 2);
        in->SetUINT32(MF_MT_AUDIO_AVG_BYTES_PER_SECOND,
                      r->audio_rate * r->audio_channels * 2);
        in->SetUINT32(MF_MT_ALL_SAMPLES_INDEPENDENT, TRUE);
        hr = writer->SetInputMediaType(*stream, in, nullptr);
    }
    SafeRelease(in);
    SafeRelease(out);
    return hr;
}

// One attempt: a writer for this codec in this container, with or without
// audio. Anything refused throws the writer (and the file it made) away.
HRESULT TryWriter(Recorder *r, const CodecChoice &c, const GUID &container,
                  bool audio, bool with_params)
{
    IMFAttributes *attrs = nullptr;
    HRESULT hr = MFCreateAttributes(&attrs, 5);
    if (FAILED(hr)) return hr;
    attrs->SetUnknown(MF_SINK_WRITER_D3D_MANAGER, r->manager);
    attrs->SetUINT32(MF_READWRITE_ENABLE_HARDWARE_TRANSFORMS, TRUE);
    // The writer must never make the worker wait: a frame it cannot take is
    // dropped by us, visibly counted, instead.
    attrs->SetUINT32(MF_SINK_WRITER_DISABLE_THROTTLING, TRUE);
    // The file name ends in ".partial" until the client publishes it, so the
    // container is named, never guessed from the extension.
    attrs->SetGUID(MF_TRANSCODE_CONTAINERTYPE, container);
    IMFSinkWriter *writer = nullptr;
    hr = MFCreateSinkWriterFromURL(r->path, nullptr, attrs, &writer);
    attrs->Release();
    DWORD vs = 0, as = 0;
    if (SUCCEEDED(hr)) hr = AddVideo(r, writer, c, with_params, &vs);
    if (SUCCEEDED(hr) && audio) hr = AddAudio(r, writer, &as);
    if (SUCCEEDED(hr)) hr = writer->BeginWriting();
    if (FAILED(hr))
    {
        SafeRelease(writer);
        DeleteFileW(r->path);
        return hr;
    }
    r->writer = writer;
    r->video_stream = vs;
    r->audio_stream = as;
    r->has_audio = audio;
    r->codec = c.id;
    return S_OK;
}

HRESULT CreateWriter(Recorder *r, uint32_t want)
{
    // What to try, in order. Fragmented MP4 is what survives a worker that
    // dies mid-recording (everything up to the last fragment plays); a plain
    // MP4 without its index at the end does not play at all. So "auto" takes
    // any codec in a fragmented file before the best codec in a plain one -
    // this system's MP4 sink takes AV1 and H.264 fragmented, not HEVC. A
    // codec asked for by name is kept over the container, with H.264 as the
    // last resort either way.
    //
    // HDR10 needs a 10-bit encoder: AV1 or HEVC Main10 (NVENC has no 10-bit
    // H.264). They go first, by the same rules - any in a fragmented file
    // first, or one asked for by name in either container - and when none
    // opens, the recording goes on in SDR: a recording beats no recording.
    struct Attempt { const CodecChoice *codec; bool fragmented; };
    Attempt plan[6] = {};
    int n = 0;
    if (r->hdr && (want == GPUREC_CODEC_AV1 || want == GPUREC_CODEC_HEVC))
    {
        const bool hevc = want == GPUREC_CODEC_HEVC;
        for (const CodecChoice *k : { hevc ? &kHevc : &kAv1, hevc ? &kAv1 : &kHevc })
            for (bool frag : { true, false }) plan[n++] = { k, frag };
    }
    else if (r->hdr)
    {
        for (bool frag : { true, false })
            for (const CodecChoice *k : { &kAv1, &kHevc }) plan[n++] = { k, frag };
    }
    else if (want == GPUREC_CODEC_AUTO)
    {
        for (bool frag : { true, false })
            for (const CodecChoice *k : { &kAv1, &kHevc, &kH264 }) plan[n++] = { k, frag };
    }
    else
    {
        const CodecChoice *first = want == GPUREC_CODEC_AV1 ? &kAv1
                                 : want == GPUREC_CODEC_HEVC ? &kHevc : &kH264;
        for (bool frag : { true, false }) plan[n++] = { first, frag };
        if (first != &kH264)
            for (bool frag : { true, false }) plan[n++] = { &kH264, frag };
    }
    const bool audio = r->ring != nullptr;
    HRESULT last = E_FAIL;
    for (int i = 0; i < n; ++i)
    {
        const CodecChoice &codec = *plan[i].codec;
        const GUID container = plan[i].fragmented ? MFTranscodeContainerType_FMPEG4
                                                  : MFTranscodeContainerType_MPEG4;
        const char *kind = plan[i].fragmented ? "fragmented" : "plain";
        // With the rate-control parameters first; a driver that refuses one
        // of them gets its defaults rather than no recording.
        for (int with_params = 1; with_params >= 0; --with_params)
        {
            HRESULT hr = TryWriter(r, codec, container, audio, with_params != 0);
            if (SUCCEEDED(hr))
            {
                Log("[grec] %s encoder%s, %s MP4, %ux%u at %u fps, %u kbit/s%s%s",
                    codec.name, r->hdr ? " HDR10 (10-bit BT.2020 PQ)" : "", kind,
                    r->w, r->h, r->fps, r->bitrate / 1000,
                    audio ? ", AAC audio" : ", no audio",
                    with_params ? "" : " (driver's rate control)");
                return S_OK;
            }
            last = hr;
            // Sound is the part most likely to be refused on an odd machine;
            // losing it must not lose the picture too.
            if (audio)
            {
                hr = TryWriter(r, codec, container, false, with_params != 0);
                if (SUCCEEDED(hr))
                {
                    Log("[grec] %s encoder, %s MP4, without audio (the AAC stream "
                        "was refused: 0x%08X)", codec.name, kind,
                        static_cast<unsigned>(last));
                    return S_OK;
                }
            }
        }
        Log("[grec] %s%s in %s MP4 refused: 0x%08X", codec.name,
            r->hdr ? " HDR10" : "", kind, static_cast<unsigned>(last));
    }
    if (r->hdr)
    {
        Log("[grec] no 10-bit encoder took HDR10 (0x%08X) - recording in SDR",
            static_cast<unsigned>(last));
        r->hdr = false;
        return CreateWriter(r, want);
    }
    return last;
}

HRESULT CreateSurfaces(Recorder *r)
{
    for (int j = 0; j < kSurfaces; ++j)
    {
        Surface &s = r->surfaces[j];
        D3D11_TEXTURE2D_DESC d = {};
        d.Width = r->w;
        d.Height = r->h;
        d.MipLevels = 1;
        d.ArraySize = 1;
        d.Format = r->hdr ? DXGI_FORMAT_P010 : DXGI_FORMAT_NV12;
        d.SampleDesc.Count = 1;
        d.Usage = D3D11_USAGE_DEFAULT;
        d.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_VIDEO_ENCODER;
        HRESULT hr = r->d11->CreateTexture2D(&d, nullptr, &s.tex);
        if (FAILED(hr))
        {
            d.BindFlags = D3D11_BIND_RENDER_TARGET;
            hr = r->d11->CreateTexture2D(&d, nullptr, &s.tex);
        }
        if (FAILED(hr)) return hr;
        IMFMediaBuffer *buffer = nullptr;
        hr = MFCreateDXGISurfaceBuffer(__uuidof(ID3D11Texture2D), s.tex, 0, FALSE, &buffer);
        if (FAILED(hr)) return hr;
        IMF2DBuffer *b2 = nullptr;
        DWORD length = 0;
        if (SUCCEEDED(buffer->QueryInterface(IID_PPV_ARGS(&b2))) &&
            SUCCEEDED(b2->GetContiguousLength(&length)))
            buffer->SetCurrentLength(length);
        SafeRelease(b2);
        IMFTrackedSample *tracked = nullptr;
        hr = MFCreateTrackedSample(&tracked);
        if (SUCCEEDED(hr)) hr = tracked->QueryInterface(IID_PPV_ARGS(&s.sample));
        SafeRelease(tracked);
        if (SUCCEEDED(hr)) hr = s.sample->AddBuffer(buffer);
        buffer->Release();
        if (FAILED(hr)) return hr;
        s.callback = new SampleReturn(r, j);
    }
    return S_OK;
}

// --- conversion ------------------------------------------------------------

bool EnsureProcessor(Recorder *r, const Slot &slot)
{
    if (r->vp != nullptr && slot.w == r->vp_in_w && slot.h == r->vp_in_h &&
        slot.format == r->vp_in_format)
        return true;
    SafeRelease(r->vp);
    SafeRelease(r->vpe);
    D3D11_VIDEO_PROCESSOR_CONTENT_DESC cd = {};
    cd.InputFrameFormat = D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE;
    cd.InputWidth = slot.w;
    cd.InputHeight = slot.h;
    cd.OutputWidth = r->w;
    cd.OutputHeight = r->h;
    cd.InputFrameRate = { r->fps, 1 };
    cd.OutputFrameRate = { r->fps, 1 };
    cd.Usage = D3D11_VIDEO_USAGE_OPTIMAL_QUALITY;
    HRESULT hr = r->vdev->CreateVideoProcessorEnumerator(&cd, &r->vpe);
    if (FAILED(hr)) { Fail(r, "video processor enumerator", hr); return false; }
    UINT support = 0;
    if (FAILED(r->vpe->CheckVideoProcessorFormat(slot.format, &support)) ||
        (support & D3D11_VIDEO_PROCESSOR_FORMAT_SUPPORT_INPUT) == 0)
    {
        Fail(r, "the video processor cannot read this frame format", E_NOTIMPL);
        Log("[grec] (format %u)", static_cast<unsigned>(slot.format));
        return false;
    }
    hr = r->vdev->CreateVideoProcessor(r->vpe, 0, &r->vp);
    if (FAILED(hr)) { Fail(r, "video processor", hr); return false; }
    // SDR only: HDR10 is converted by HdrConvert. The frame's colour, from
    // its format: 8-bit sRGB (the network's output) or FP16 scRGB.
    const DXGI_COLOR_SPACE_TYPE in_cs =
        slot.format == DXGI_FORMAT_R16G16B16A16_FLOAT ? DXGI_COLOR_SPACE_RGB_FULL_G10_NONE_P709
                                                      : DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709;
    const DXGI_COLOR_SPACE_TYPE out_cs = DXGI_COLOR_SPACE_YCBCR_STUDIO_G22_LEFT_P709;
    ID3D11VideoContext1 *vc1 = nullptr;
    if (SUCCEEDED(r->vctx->QueryInterface(IID_PPV_ARGS(&vc1))))
    {
        vc1->VideoProcessorSetStreamColorSpace1(r->vp, 0, in_cs);
        vc1->VideoProcessorSetOutputColorSpace1(r->vp, out_cs);
        vc1->Release();
    }
    else
    {
        D3D11_VIDEO_PROCESSOR_COLOR_SPACE in = {};
        in.RGB_Range = 0;                 // full range RGB
        D3D11_VIDEO_PROCESSOR_COLOR_SPACE out = {};
        out.YCbCr_Matrix = 1;             // BT.709
        out.Nominal_Range = D3D11_VIDEO_PROCESSOR_NOMINAL_RANGE_16_235;
        r->vctx->VideoProcessorSetStreamColorSpace(r->vp, 0, &in);
        r->vctx->VideoProcessorSetOutputColorSpace(r->vp, &out);
    }
    // A frame of another shape than the recording (a captured window that
    // was resized) is fitted inside it, centred, with black around it -
    // the recording goes on instead of ending at the first resize.
    const double scale = (std::min)(static_cast<double>(r->w) / slot.w,
                                    static_cast<double>(r->h) / slot.h);
    const LONG dw = static_cast<LONG>(slot.w * scale + 0.5) & ~1L;
    const LONG dh = static_cast<LONG>(slot.h * scale + 0.5) & ~1L;
    const LONG dx = (static_cast<LONG>(r->w) - dw) / 2;
    const LONG dy = (static_cast<LONG>(r->h) - dh) / 2;
    const RECT src = { 0, 0, static_cast<LONG>(slot.w), static_cast<LONG>(slot.h) };
    const RECT dst = { dx, dy, dx + dw, dy + dh };
    const RECT full = { 0, 0, static_cast<LONG>(r->w), static_cast<LONG>(r->h) };
    r->vctx->VideoProcessorSetStreamFrameFormat(r->vp, 0, D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE);
    r->vctx->VideoProcessorSetStreamSourceRect(r->vp, 0, TRUE, &src);
    r->vctx->VideoProcessorSetStreamDestRect(r->vp, 0, TRUE, &dst);
    r->vctx->VideoProcessorSetOutputTargetRect(r->vp, TRUE, &full);
    D3D11_VIDEO_COLOR black = {};
    black.YCbCr.Y = 16.0f / 255.0f;
    black.YCbCr.Cb = 0.5f;
    black.YCbCr.Cr = 0.5f;
    black.YCbCr.A = 1.0f;
    r->vctx->VideoProcessorSetOutputBackgroundColor(r->vp, TRUE, &black);
    // The picture is the network's output: no driver "enhancement" on top.
    r->vctx->VideoProcessorSetStreamAutoProcessingMode(r->vp, 0, FALSE);
    r->vp_in_w = slot.w;
    r->vp_in_h = slot.h;
    r->vp_in_format = slot.format;
    if (slot.w != r->w || slot.h != r->h)
        Log("[grec] frames are %ux%u now - fitted into the %ux%u recording",
            slot.w, slot.h, r->w, r->h);
    return true;
}

bool Blit(Recorder *r, const Slot &slot, ID3D11Texture2D *dst)
{
    if (!EnsureProcessor(r, slot)) return false;
    ID3D11VideoProcessorInputView *iv = nullptr;
    ID3D11VideoProcessorOutputView *ov = nullptr;
    D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC ivd = {};
    ivd.ViewDimension = D3D11_VPIV_DIMENSION_TEXTURE2D;
    D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC ovd = {};
    ovd.ViewDimension = D3D11_VPOV_DIMENSION_TEXTURE2D;
    HRESULT hr = r->vdev->CreateVideoProcessorInputView(slot.tex11, r->vpe, &ivd, &iv);
    if (SUCCEEDED(hr)) hr = r->vdev->CreateVideoProcessorOutputView(dst, r->vpe, &ovd, &ov);
    if (SUCCEEDED(hr))
    {
        D3D11_VIDEO_PROCESSOR_STREAM stream = {};
        stream.Enable = TRUE;
        stream.pInputSurface = iv;
        hr = r->vctx->VideoProcessorBlt(r->vp, ov, 0, 1, &stream);
    }
    SafeRelease(iv);
    SafeRelease(ov);
    if (FAILED(hr)) { Fail(r, "video processor blit", hr); return false; }
    return true;
}

// --- HDR10 ------------------------------------------------------------------
//
// The video processor cannot make HDR10 here: asked with
// CheckVideoProcessorFormatConversion (22.09.2026, RTX 5080), this driver's
// converts within BT.709 only - no BT.2020, no PQ, and no FP16 input at all.
// So an HDR10 frame is converted by two draws straight into the P010
// surface's planes. The frame arrives as PQ code values (R10G10B10A2, the
// worker's HDR composite), and the BT.2020 matrix is linear in them: there
// is no curve to apply, only the matrix, the studio range and the chroma
// downsample. A frame of another shape is fitted in with black around it,
// as the video processor does for SDR.
const char kHdr10Hlsl[] =
    "Texture2D<float4> src : register(t0);\n"
    "SamplerState lin : register(s0);\n"
    // rect: where the frame lands in the recording, in its luma pixels.
    "cbuffer P : register(b0) { float4 rect; };\n"
    "static const float3 KY = float3(0.2627, 0.6780, 0.0593);\n"
    "float3 Src(float2 luma) {\n"
    "  float2 uv = (luma - rect.xy) / rect.zw;\n"
    "  if (any(uv < 0) || any(uv > 1)) return 0;\n"       // the letterbox: PQ black
    "  return src.SampleLevel(lin, uv, 0).rgb; }\n"
    "float4 VS(uint id : SV_VertexID) : SV_Position {\n"
    "  float2 t = float2((id << 1) & 2, id & 2);\n"
    "  return float4(t * float2(2, -2) + float2(-1, 1), 0, 1); }\n"
    // P010 keeps its 10 bits at the top of each 16-bit word: the code value is
    // rounded here, so the word's low bits are zero rather than a remainder
    // an encoder would truncate.
    "float Word(float v) { return clamp(round(v), 0, 1023) * 64.0 / 65535.0; }\n"
    "float PSY(float4 p : SV_Position) : SV_Target {\n"
    "  return Word(64.0 + 876.0 * dot(Src(p.xy), KY)); }\n"
    // Chroma, left-sited (BT.2020 / HEVC default): level with luma column 2i,
    // between rows 2j and 2j+1 - a [1 2 1]/4 filter across, [1 1]/2 down.
    // At the frame's edge the samples are held inside it; a block wholly in
    // the letterbox is neutral, so no colour bleeds into the bars.
    "float2 PSUV(float4 p : SV_Position) : SV_Target {\n"
    "  float2 c = float2(floor(p.x) * 2.0 + 0.5, floor(p.y) * 2.0 + 1.0);\n"
    "  if (c.x < rect.x || c.y < rect.y || c.x > rect.x + rect.z || c.y > rect.y + rect.w)\n"
    "    return float2(Word(512.0), Word(512.0));\n"
    "  float2 lo = rect.xy + 0.5, hi = rect.xy + rect.zw - 0.5;\n"
    "  float3 sum = 0;\n"
    "  [unroll] for (int dx = -1; dx <= 1; ++dx) {\n"
    "    float w = dx == 0 ? 0.25 : 0.125;\n"
    "    [unroll] for (int dy = 0; dy <= 1; ++dy)\n"
    "      sum += w * Src(clamp(float2(c.x + dx, c.y - 0.5 + dy), lo, hi)); }\n"
    "  float y = dot(sum, KY);\n"
    "  return float2(Word(512.0 + 896.0 * (sum.b - y) / 1.8814),\n"
    "                Word(512.0 + 896.0 * (sum.r - y) / 1.4746)); }\n";

HRESULT CompileHdr(const char *entry, const char *target, ID3DBlob **code)
{
    ID3DBlob *errors = nullptr;
    const HRESULT hr = D3DCompile(kHdr10Hlsl, sizeof(kHdr10Hlsl) - 1, "hdr10-convert",
                                  nullptr, nullptr, entry, target, 0, 0, code, &errors);
    if (FAILED(hr) && errors != nullptr)
        Log("[grec] HDR10 shader %s: %s", entry,
            static_cast<const char *>(errors->GetBufferPointer()));
    SafeRelease(errors);
    return hr;
}

bool EnsureHdrConverter(Recorder *r)
{
    if (r->hdr_ps_uv != nullptr) return true;
    ID3DBlob *vs = nullptr, *y = nullptr, *uv = nullptr;
    HRESULT hr = CompileHdr("VS", "vs_5_0", &vs);
    if (SUCCEEDED(hr)) hr = CompileHdr("PSY", "ps_5_0", &y);
    if (SUCCEEDED(hr)) hr = CompileHdr("PSUV", "ps_5_0", &uv);
    if (SUCCEEDED(hr))
        hr = r->d11->CreateVertexShader(vs->GetBufferPointer(), vs->GetBufferSize(),
                                        nullptr, &r->hdr_vs);
    if (SUCCEEDED(hr))
        hr = r->d11->CreatePixelShader(y->GetBufferPointer(), y->GetBufferSize(),
                                       nullptr, &r->hdr_ps_y);
    if (SUCCEEDED(hr))
        hr = r->d11->CreatePixelShader(uv->GetBufferPointer(), uv->GetBufferSize(),
                                       nullptr, &r->hdr_ps_uv);
    SafeRelease(vs);
    SafeRelease(y);
    SafeRelease(uv);
    if (SUCCEEDED(hr))
    {
        D3D11_SAMPLER_DESC sd = {};
        sd.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
        sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
        sd.MaxLOD = D3D11_FLOAT32_MAX;
        hr = r->d11->CreateSamplerState(&sd, &r->hdr_sampler);
    }
    if (SUCCEEDED(hr))
    {
        D3D11_BUFFER_DESC bd = {};
        bd.ByteWidth = 16;
        bd.Usage = D3D11_USAGE_DEFAULT;
        bd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
        hr = r->d11->CreateBuffer(&bd, nullptr, &r->hdr_cb);
    }
    if (FAILED(hr))
    {
        Fail(r, "the HDR10 conversion", hr);
        SafeRelease(r->hdr_vs);
        SafeRelease(r->hdr_ps_y);
        SafeRelease(r->hdr_ps_uv);
        SafeRelease(r->hdr_sampler);
        SafeRelease(r->hdr_cb);
        return false;
    }
    Log("[grec] HDR10 conversion by shader (the video processor converts within BT.709 only)");
    return true;
}

bool HdrConvert(Recorder *r, Slot &slot, Surface &dst)
{
    if (!EnsureHdrConverter(r)) return false;
    HRESULT hr = S_OK;
    if (slot.srv == nullptr)
        hr = r->d11->CreateShaderResourceView(slot.tex11, nullptr, &slot.srv);
    if (SUCCEEDED(hr) && dst.rtv_y == nullptr)
    {
        ID3D11Device3 *d3 = nullptr;
        hr = r->d11->QueryInterface(IID_PPV_ARGS(&d3));
        D3D11_RENDER_TARGET_VIEW_DESC1 rd = {};
        rd.ViewDimension = D3D11_RTV_DIMENSION_TEXTURE2D;
        ID3D11RenderTargetView1 *y = nullptr, *uv = nullptr;
        if (SUCCEEDED(hr))
        {
            rd.Format = DXGI_FORMAT_R16_UNORM;
            rd.Texture2D.PlaneSlice = 0;
            hr = d3->CreateRenderTargetView1(dst.tex, &rd, &y);
        }
        if (SUCCEEDED(hr))
        {
            rd.Format = DXGI_FORMAT_R16G16_UNORM;
            rd.Texture2D.PlaneSlice = 1;
            hr = d3->CreateRenderTargetView1(dst.tex, &rd, &uv);
        }
        SafeRelease(d3);
        if (SUCCEEDED(hr)) { dst.rtv_y = y; dst.rtv_uv = uv; }
        else { SafeRelease(y); SafeRelease(uv); }
    }
    if (FAILED(hr)) { Fail(r, "HDR10 conversion views", hr); return false; }

    // Media Foundation drives this device from its own threads too: hold the
    // device across the state and the draws, so nothing lands between them.
    ID3D10Multithread *mt = nullptr;
    r->d11->QueryInterface(IID_PPV_ARGS(&mt));
    if (mt != nullptr) mt->Enter();
    if (slot.w != r->hdr_cb_w || slot.h != r->hdr_cb_h)
    {
        // The same fit as the video processor's (EnsureProcessor).
        const double scale = (std::min)(static_cast<double>(r->w) / slot.w,
                                        static_cast<double>(r->h) / slot.h);
        const float dw = static_cast<float>(static_cast<LONG>(slot.w * scale + 0.5) & ~1L);
        const float dh = static_cast<float>(static_cast<LONG>(slot.h * scale + 0.5) & ~1L);
        const float rect[4] = { static_cast<float>((static_cast<LONG>(r->w) - static_cast<LONG>(dw)) / 2),
                                static_cast<float>((static_cast<LONG>(r->h) - static_cast<LONG>(dh)) / 2),
                                dw, dh };
        r->ctx->UpdateSubresource(r->hdr_cb, 0, nullptr, rect, 0, 0);
        if (slot.w != r->w || slot.h != r->h)
            Log("[grec] frames are %ux%u now - fitted into the %ux%u HDR10 recording",
                slot.w, slot.h, r->w, r->h);
        r->hdr_cb_w = slot.w;
        r->hdr_cb_h = slot.h;
    }
    r->ctx->ClearState();
    r->ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    r->ctx->VSSetShader(r->hdr_vs, nullptr, 0);
    r->ctx->PSSetShaderResources(0, 1, &slot.srv);
    r->ctx->PSSetSamplers(0, 1, &r->hdr_sampler);
    r->ctx->PSSetConstantBuffers(0, 1, &r->hdr_cb);
    D3D11_VIEWPORT vp = { 0.0f, 0.0f, static_cast<float>(r->w), static_cast<float>(r->h), 0.0f, 1.0f };
    ID3D11RenderTargetView *target = dst.rtv_y;
    r->ctx->OMSetRenderTargets(1, &target, nullptr);
    r->ctx->RSSetViewports(1, &vp);
    r->ctx->PSSetShader(r->hdr_ps_y, nullptr, 0);
    r->ctx->Draw(3, 0);
    vp.Width = static_cast<float>(r->w / 2);
    vp.Height = static_cast<float>(r->h / 2);
    target = dst.rtv_uv;
    r->ctx->OMSetRenderTargets(1, &target, nullptr);
    r->ctx->RSSetViewports(1, &vp);
    r->ctx->PSSetShader(r->hdr_ps_uv, nullptr, 0);
    r->ctx->Draw(3, 0);
    // Nothing of ours stays bound to the surface the encoder is about to read.
    r->ctx->ClearState();
    if (mt != nullptr) { mt->Leave(); mt->Release(); }
    return true;
}

// --- the encoder thread ----------------------------------------------------

void PumpAudio(Recorder *r, bool drain)
{
    if (!r->has_audio || r->ring == nullptr || r->writer == nullptr) return;
    // A plain acquire read: the mapping is read-only here, so no interlocked
    // operation (they all need write access, even a compare that fails).
    // The client stored the samples before the count.
    const int64_t written = ReadAcquire64(
        const_cast<volatile LONG64 *>(&r->ring->written));
    int64_t avail = written - r->audio_read;
    const int64_t capacity = r->audio_capacity;
    if (avail > capacity)
    {
        // The client ran a whole ring ahead of us. The lost stretch is gone;
        // the timestamps still follow the sample count, so the sound after
        // it stays in sync with the picture. The read resumes a quarter of
        // the ring behind the newest sample rather than a whole ring: the
        // oldest stretch (written - capacity) is exactly where the client
        // writes next, and reading it would copy torn audio.
        const int64_t keep = capacity - capacity / 4;
        if (!r->audio_overrun_logged)
        {
            r->audio_overrun_logged = true;
            Log("[grec] audio ring overrun: %lld frames skipped",
                static_cast<long long>(avail - keep));
        }
        r->audio_read = written - keep;
        avail = keep;
    }
    const UINT ch = r->audio_channels;
    while (avail > 0)
    {
        const int64_t n = (std::min)(avail, kAudioMaxFrames);
        if (!drain && n < kAudioMinFrames) break;
        IMFMediaBuffer *buffer = nullptr;
        const DWORD bytes = static_cast<DWORD>(n * ch * 2);
        HRESULT hr = MFCreateMemoryBuffer(bytes, &buffer);
        BYTE *dst = nullptr;
        if (SUCCEEDED(hr)) hr = buffer->Lock(&dst, nullptr, nullptr);
        if (SUCCEEDED(hr))
        {
            // Two pieces when the stretch wraps around the end of the ring.
            const int64_t start = r->audio_read % capacity;
            const int64_t first = (std::min)(n, capacity - start);
            memcpy(dst, r->ring_data + start * ch, static_cast<size_t>(first * ch * 2));
            if (first < n)
                memcpy(dst + first * ch * 2, r->ring_data,
                       static_cast<size_t>((n - first) * ch * 2));
            buffer->Unlock();
            buffer->SetCurrentLength(bytes);
        }
        IMFSample *sample = nullptr;
        if (SUCCEEDED(hr)) hr = MFCreateSample(&sample);
        if (SUCCEEDED(hr)) hr = sample->AddBuffer(buffer);
        if (SUCCEEDED(hr))
        {
            sample->SetSampleTime((r->audio_read - r->audio_base) * 10000000LL /
                                  r->audio_rate);
            sample->SetSampleDuration(n * 10000000LL / r->audio_rate);
            hr = r->writer->WriteSample(r->audio_stream, sample);
        }
        SafeRelease(sample);
        SafeRelease(buffer);
        if (FAILED(hr)) { Fail(r, "audio write", hr); r->has_audio = false; return; }
        r->audio_read += n;
        avail -= n;
    }
}

// Write the frame held back, lasting until `next_time` (at least one slot).
// Returns false when the write failed; the error is recorded either way.
bool WriteHeld(Recorder *r, int64_t next_time)
{
    IMFSample *sample = r->held;
    if (sample == nullptr) return true;
    r->held = nullptr;
    const int64_t slot = 10000000LL / r->fps;
    sample->SetSampleDuration((std::max)(slot, next_time - r->held_time));
    HRESULT hr = S_OK;
    if (r->fail_after != 0 && r->written.load() >= r->fail_after)
    {
        Log("[grec] injecting a write failure (NS_TEST_FAIL_STAGE)");
        hr = HRESULT_FROM_WIN32(ERROR_DISK_FULL);
    }
    if (SUCCEEDED(hr)) hr = r->writer->WriteSample(r->video_stream, sample);
    sample->Release();   // the last of ours: SampleReturn fires once the writer is done too
    if (FAILED(hr))
    {
        Fail(r, "video write", hr);
        r->dropped.fetch_add(1);
        return false;
    }
    r->written.fetch_add(1);
    return true;
}

int AcquireSurface(Recorder *r)
{
    std::unique_lock<std::mutex> lock(r->mu);
    for (int attempt = 0; attempt < 2; ++attempt)
    {
        for (int j = 0; j < kSurfaces; ++j)
        {
            Surface &s = r->surfaces[j];
            if (!s.busy.load() && s.sample != nullptr)
            {
                s.busy.store(true);
                return j;
            }
        }
        // All of them are with the encoder: give it a moment to hand one back.
        r->cv.wait_for(lock, std::chrono::milliseconds(100));
    }
    return -1;
}

void EncodeItem(Recorder *r, const Item &it)
{
    Slot &slot = r->slots[it.slot];
    // 1. The worker's copy into the slot has to have landed.
    if (it.fence->GetCompletedValue() < it.value)
    {
        it.fence->SetEventOnCompletion(it.value, r->fence_event);
        WaitForSingleObject(r->fence_event, 2000);
    }
    const UINT64 done = it.fence->GetCompletedValue();
    if (done == UINT64_MAX || done < it.value)
    {
        // The slot stays busy: the worker's copy may still be on its way into
        // it, and a slot handed back now could be read while it lands. The
        // recording has failed anyway, so nothing waits for the slot again.
        Fail(r, "waiting for the frame copy", done == UINT64_MAX
             ? DXGI_ERROR_DEVICE_REMOVED : HRESULT_FROM_WIN32(WAIT_TIMEOUT));
        r->dropped.fetch_add(1);
        return;
    }
    // 2. A surface for the converted frame.
    const int j = AcquireSurface(r);
    if (j < 0)
    {
        slot.busy.store(false);
        r->dropped.fetch_add(1);
        return;
    }
    Surface &s = r->surfaces[j];
    // 3. RGBA -> NV12, then wait for the GPU to have read the slot, which is
    // what lets the worker copy the next frame into it.
    const bool ok = r->hdr ? HdrConvert(r, slot, s) : Blit(r, slot, s.tex);
    r->ctx->End(r->blit_done);
    r->ctx->Flush();
    bool read = false;
    for (int spin = 0; spin < 2000; ++spin)
    {
        if (r->ctx->GetData(r->blit_done, nullptr, 0, 0) != S_FALSE) { read = true; break; }
        Sleep(spin < 50 ? 0 : 1);
    }
    if (!read)
    {
        // Two seconds and the GPU has still not read the slot: handing it
        // back would let the worker copy the next frame into it mid-read (a
        // torn frame, and a "every copy was waited for" that is not true).
        // Keep it, and end the recording - a stall this long is a hung GPU.
        Fail(r, "reading the frame copy", HRESULT_FROM_WIN32(WAIT_TIMEOUT));
        std::lock_guard<std::mutex> lock(r->mu);
        s.busy.store(false);
        r->dropped.fetch_add(1);
        return;
    }
    slot.busy.store(false);
    if (!ok)
    {
        std::lock_guard<std::mutex> lock(r->mu);
        s.busy.store(false);
        r->dropped.fetch_add(1);
        return;
    }
    // 4. The sample is lent from here on: our reference goes with it, and
    // SampleReturn brings it back when the encoder has let go. It waits as
    // the held frame until the next one fixes its duration (see `held`), and
    // the frame held before it goes to the writer now.
    IMFSample *sample = nullptr;
    {
        std::lock_guard<std::mutex> lock(r->mu);
        sample = s.sample;
        s.sample = nullptr;
    }
    sample->SetSampleTime(it.time);
    IMFTrackedSample *tracked = nullptr;
    HRESULT hr = sample->QueryInterface(IID_PPV_ARGS(&tracked));
    if (SUCCEEDED(hr)) hr = tracked->SetAllocator(s.callback, nullptr);
    SafeRelease(tracked);
    if (FAILED(hr))
    {
        sample->Release();
        Fail(r, "video sample", hr);
        r->dropped.fetch_add(1);
        return;
    }
    if (!WriteHeld(r, it.time))
    {
        sample->Release();
        r->dropped.fetch_add(1);
        return;
    }
    r->held = sample;
    r->held_time = it.time;
}

void Teardown(Recorder *r)
{
    SafeRelease(r->held);   // after an error the last frame is never written
    SafeRelease(r->writer);
    // The encoder hands the last surfaces back as it shuts down, possibly
    // from one of its own threads: wait a moment for them. One that never
    // comes back is left alone rather than freed under the encoder's feet.
    {
        std::unique_lock<std::mutex> lock(r->mu);
        r->cv.wait_for(lock, std::chrono::milliseconds(1000), [r] {
            for (const Surface &s : r->surfaces)
                if (s.tex != nullptr && s.sample == nullptr) return false;
            return true;
        });
    }
    // A surface still out is detached first: its callback outlives the
    // Recorder (the encoder holds a reference), and a return that arrives
    // after the delete used to write into freed memory.
    {
        std::lock_guard<std::mutex> guard(g_return_mu);
        for (Surface &s : r->surfaces)
            if (s.callback != nullptr) s.callback->Detach();
    }
    for (Surface &s : r->surfaces)
    {
        if (s.sample != nullptr) SafeRelease(s.sample);
        SafeRelease(s.rtv_y);
        SafeRelease(s.rtv_uv);
        SafeRelease(s.tex);
        if (s.callback != nullptr) { s.callback->Release(); s.callback = nullptr; }
    }
    SafeRelease(r->vp);
    SafeRelease(r->vpe);
    SafeRelease(r->hdr_vs);
    SafeRelease(r->hdr_ps_y);
    SafeRelease(r->hdr_ps_uv);
    SafeRelease(r->hdr_sampler);
    SafeRelease(r->hdr_cb);
    SafeRelease(r->blit_done);
    SafeRelease(r->vctx);
    SafeRelease(r->vdev);
    if (r->manager != nullptr) { r->manager->Release(); r->manager = nullptr; }
    if (r->ring != nullptr) { UnmapViewOfFile(r->ring); r->ring = nullptr; }
    if (r->audio_map != nullptr) { CloseHandle(r->audio_map); r->audio_map = nullptr; }
}

void EncoderMain(Recorder *r, const char *audio_name)
{
    const HRESULT co = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    HRESULT hr = MFStartup(MF_VERSION, MFSTARTUP_LITE);
    const bool mf = SUCCEEDED(hr);
    if (SUCCEEDED(hr)) hr = CreateDevices(r);
    if (SUCCEEDED(hr)) OpenAudioRing(r, audio_name);
    if (SUCCEEDED(hr)) hr = CreateWriter(r, r->codec);
    if (SUCCEEDED(hr)) hr = CreateSurfaces(r);
    if (FAILED(hr))
    {
        if (r->writer != nullptr) { r->writer->Release(); r->writer = nullptr; DeleteFileW(r->path); }
        Teardown(r);
        if (mf) MFShutdown();
        if (SUCCEEDED(co)) CoUninitialize();
        r->ready.set_value(hr);
        return;
    }
    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);
    r->origin_qpc = now.QuadPart;
    if (r->ring != nullptr)
    {
        // Sound captured during the setup belongs before time 0: skipped.
        int64_t base = (r->origin_qpc - r->client_qpc) * r->audio_rate / r->qpc_freq;
        if (base < 0) base = 0;
        r->audio_base = r->audio_read = base;
    }
    r->started_tick = GetTickCount64();
    r->ready.set_value(S_OK);   // audio_name is the caller's: not touched again

    for (;;)
    {
        Item it = {};
        bool have = false;
        {
            std::unique_lock<std::mutex> lock(r->mu);
            // Woken by a frame, or every 10 ms for the sound.
            r->cv.wait_for(lock, std::chrono::milliseconds(10),
                           [r] { return !r->queue.empty() || r->stopping; });
            if (!r->queue.empty())
            {
                it = r->queue.front();
                r->queue.pop_front();
                have = true;
            }
            else if (r->stopping)
                break;
        }
        PumpAudio(r, false);
        if (have && r->error.load() == S_OK) EncodeItem(r, it);
        else if (have) { r->slots[it.slot].busy.store(false); r->dropped.fetch_add(1); }
    }
    PumpAudio(r, true);
    if (r->held != nullptr && r->error.load() == S_OK)
    {
        LARGE_INTEGER now;
        QueryPerformanceCounter(&now);
        WriteHeld(r, (now.QuadPart - r->origin_qpc) * 10000000LL / r->qpc_freq);
    }
    r->stopped_tick = GetTickCount64();
    if (r->written.load() == 0)
        Log("[grec] no frame was recorded - the file will be empty");
    hr = r->writer->Finalize();
    if (FAILED(hr)) Fail(r, "finalize", hr);
    Teardown(r);
    if (mf) MFShutdown();
    if (SUCCEEDED(co)) CoUninitialize();
}

void ReleaseSlot(Slot &s)
{
    SafeRelease(s.srv);
    SafeRelease(s.tex12);
    SafeRelease(s.tex11);
    if (s.nt != nullptr) { CloseHandle(s.nt); s.nt = nullptr; }
    s.w = s.h = 0;
    s.format = DXGI_FORMAT_UNKNOWN;
}

bool EnsureSlot(Recorder *r, Slot &s, UINT w, UINT h, DXGI_FORMAT format)
{
    if (s.tex12 != nullptr && s.w == w && s.h == h && s.format == format) return true;
    ReleaseSlot(s);
    D3D11_TEXTURE2D_DESC d = {};
    d.Width = w;
    d.Height = h;
    d.MipLevels = 1;
    d.ArraySize = 1;
    d.Format = format;
    d.SampleDesc.Count = 1;
    d.Usage = D3D11_USAGE_DEFAULT;
    d.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
    d.MiscFlags = D3D11_RESOURCE_MISC_SHARED | D3D11_RESOURCE_MISC_SHARED_NTHANDLE;
    HRESULT hr = r->d11->CreateTexture2D(&d, nullptr, &s.tex11);
    IDXGIResource1 *r1 = nullptr;
    if (SUCCEEDED(hr)) hr = s.tex11->QueryInterface(IID_PPV_ARGS(&r1));
    if (SUCCEEDED(hr))
        hr = r1->CreateSharedHandle(nullptr, DXGI_SHARED_RESOURCE_READ | DXGI_SHARED_RESOURCE_WRITE,
                                    nullptr, &s.nt);
    SafeRelease(r1);
    if (SUCCEEDED(hr)) hr = r->dev12->OpenSharedHandle(s.nt, IID_PPV_ARGS(&s.tex12));
    if (FAILED(hr))
    {
        Fail(r, "shared frame slot", hr);
        ReleaseSlot(s);
        return false;
    }
    s.w = w;
    s.h = h;
    s.format = format;
    return true;
}

}  // namespace

bool GpuRecStart(ID3D12Device *dev, const GpuRecParams &p, GpuRecStarted *out)
{
    if (g_rec != nullptr) GpuRecStop();
    GpuRecStarted local = {};
    GpuRecStarted &o = out != nullptr ? *out : local;
    o = GpuRecStarted{};
    o.hr = E_FAIL;
    if (dev == nullptr || p.path == nullptr || p.width < 64 || p.height < 64)
        return false;
    Recorder *r = new Recorder();
    r->dev12 = dev;
    // NV12 has half-resolution chroma: the frame is rounded down to even.
    r->w = p.width & ~1u;
    r->h = p.height & ~1u;
    r->fps = (std::max)(1u, (std::min)(p.fps, 240u));
    // The old recorder's cq 16 came out around 64 Mbit/s at 4K. The same
    // bits per pixel per second, from the size and the rate.
    r->bitrate = p.bitrate != 0 ? p.bitrate : static_cast<uint32_t>(
        (std::min)(400000000.0, (std::max)(4000000.0,
                                           0.15 * r->w * r->h * r->fps)));
    r->codec = p.codec;
    r->hdr = p.hdr;
    r->max_nits = p.hdr_max_nits != 0 ? p.hdr_max_nits : 1000;
    r->client_qpc = p.start_qpc;
    char stage[32] = {};
    GetEnvironmentVariableA("NS_TEST_FAIL_STAGE", stage, sizeof(stage));
    if (_stricmp(stage, "grec-write") == 0) r->fail_after = 30;
    LARGE_INTEGER freq;
    QueryPerformanceFrequency(&freq);
    r->qpc_freq = freq.QuadPart;
    wcsncpy_s(r->path, p.path, _TRUNCATE);
    r->fence_event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    std::future<HRESULT> result = r->ready.get_future();
    r->thread = std::thread(EncoderMain, r, p.audio_name);
    const HRESULT hr = result.get();
    o.hr = hr;
    if (FAILED(hr))
    {
        r->thread.join();
        if (r->fence_event != nullptr) CloseHandle(r->fence_event);
        // Teardown leaves the device to GpuRecStop, which never runs for a
        // start that failed: every refused start (NVENC sessions all taken,
        // a path that cannot be written) leaked a video-capable device.
        SafeRelease(r->ctx);
        SafeRelease(r->d11);
        delete r;
        Log("[grec] GPU recording could not start: 0x%08X", static_cast<unsigned>(hr));
        return false;
    }
    // The copy slots are made now, at the frame's own size, rather than on
    // the first frames of the recording, where making one is a hitch.
    for (Slot &s : r->slots)
        EnsureSlot(r, s, p.width, p.height,
                   r->hdr ? DXGI_FORMAT_R10G10B10A2_UNORM : DXGI_FORMAT_R8G8B8A8_UNORM);
    o.codec = r->codec;
    o.hdr = r->hdr;
    o.audio = r->has_audio;
    o.origin_qpc = r->origin_qpc;
    o.width = r->w;
    o.height = r->h;
    o.fps = r->fps;
    o.bitrate = r->bitrate;
    g_rec = r;
    g_active.store(true);
    return true;
}

bool GpuRecActive()
{
    return g_active.load() && g_rec != nullptr;
}

bool GpuRecFailed()
{
    return g_rec != nullptr && g_rec->error.load() != S_OK;
}

bool GpuRecFrameDue(int64_t *sample_time)
{
    Recorder *r = g_rec;
    if (r == nullptr || !g_active.load() || r->error.load() != S_OK) return false;
    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);
    int64_t elapsed = now.QuadPart - r->origin_qpc;
    if (elapsed < 0) elapsed = 0;
    const int64_t slot = elapsed * r->fps / r->qpc_freq;
    if (slot <= r->last_slot) return false;
    r->last_slot = slot;
    if (sample_time) *sample_time = slot * 10000000LL / r->fps;
    return true;
}

int GpuRecReserve(ID3D12Resource *src)
{
    Recorder *r = g_rec;
    if (r == nullptr || src == nullptr) return -1;
    const D3D12_RESOURCE_DESC d = src->GetDesc();
    for (int i = 0; i < kSlots; ++i)
    {
        Slot &s = r->slots[i];
        if (s.busy.load()) continue;
        if (!EnsureSlot(r, s, static_cast<UINT>(d.Width), d.Height, d.Format)) return -1;
        s.busy.store(true);
        return i;
    }
    r->dropped.fetch_add(1);
    return -1;
}

void GpuRecCopy(ID3D12GraphicsCommandList *list, int slot, ID3D12Resource *src)
{
    Recorder *r = g_rec;
    if (r == nullptr || slot < 0 || slot >= kSlots || list == nullptr) return;
    ID3D12Resource *dst = r->slots[slot].tex12;
    D3D12_RESOURCE_BARRIER b = {};
    b.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    b.Transition.pResource = dst;
    b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    b.Transition.StateBefore = D3D12_RESOURCE_STATE_COMMON;
    b.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_DEST;
    list->ResourceBarrier(1, &b);
    list->CopyResource(dst, src);
    b.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST;
    b.Transition.StateAfter = D3D12_RESOURCE_STATE_COMMON;
    list->ResourceBarrier(1, &b);
}

void GpuRecSubmit(int slot, ID3D12Fence *fence, UINT64 value, int64_t sample_time)
{
    Recorder *r = g_rec;
    if (r == nullptr || slot < 0 || slot >= kSlots) return;
    {
        std::lock_guard<std::mutex> lock(r->mu);
        r->queue.push_back(Item{ slot, fence, value, sample_time });
    }
    r->cv.notify_all();
}

void GpuRecCancel(int slot)
{
    Recorder *r = g_rec;
    if (r == nullptr || slot < 0 || slot >= kSlots) return;
    r->slots[slot].busy.store(false);
}

GpuRecStats GpuRecStop()
{
    GpuRecStats st = {};
    Recorder *r = g_rec;
    if (r == nullptr) { st.hr = S_FALSE; return st; }
    g_active.store(false);
    {
        std::lock_guard<std::mutex> lock(r->mu);
        r->stopping = true;
    }
    r->cv.notify_all();
    r->thread.join();
    st.written = r->written.load();
    st.dropped = r->dropped.load();
    st.codec = r->codec;
    st.had_audio = r->has_audio;   // the ring is already unmapped here
    st.audio_frames = static_cast<uint32_t>(
        (std::min)(r->audio_read - r->audio_base, int64_t(UINT32_MAX)));
    st.hr = r->error.load();
    const ULONGLONG end = r->stopped_tick != 0 ? r->stopped_tick : GetTickCount64();
    st.duration_ms = static_cast<uint32_t>(end - r->started_tick);
    Log("[grec] finalized: %u frames, %u dropped, %.1f s%s", st.written, st.dropped,
        st.duration_ms / 1000.0, st.hr == S_OK ? "" : " - with an error (see above)");
    // Every copy the worker submitted was waited for by the encoder thread
    // before it finished, so the slots are idle and can go.
    for (Slot &s : r->slots) ReleaseSlot(s);
    SafeRelease(r->ctx);
    SafeRelease(r->d11);
    if (r->fence_event != nullptr) CloseHandle(r->fence_event);
    g_rec = nullptr;
    delete r;
    return st;
}
