// Run the production HDR shaders on WARP: no HDR monitor or NVIDIA runtime
// required. All comparisons use GPU readback, including FP16 quantization.
#define NOMINMAX
#include <windows.h>
#include <d3d11.h>
#include <d3dcompiler.h>
#include <wrl/client.h>
#include <DirectXPackedVector.h>
#include <array>
#include <vector>
#include <cstdio>
#include <cmath>
#include <stdexcept>
#include <cstring>
#include "../native/hdr_shaders.h"
#include "../native/hdr_display.h"
using Microsoft::WRL::ComPtr;
using namespace DirectX::PackedVector;
constexpr UINT W = 17, H = 9; // also exercises dispatch bounds
using Pixel = std::array<float, 4>;
static ComPtr<ID3D11Device> dev;
static ComPtr<ID3D11DeviceContext> ctx;
static void check(bool ok, const char *what) { if (!ok) throw std::runtime_error(what); }
static void hr(HRESULT v) { check(SUCCEEDED(v), "Direct3D call failed"); }
static ComPtr<ID3D11Texture2D> texture(DXGI_FORMAT fmt, const void *data, UINT bpp, bool output=false,
                                       UINT w=W, UINT h=H)
{
    D3D11_TEXTURE2D_DESC d = {};
    d.Width=w; d.Height=h; d.MipLevels=1; d.ArraySize=1; d.Format=fmt; d.SampleDesc.Count=1;
    d.BindFlags=D3D11_BIND_SHADER_RESOURCE | (output ? D3D11_BIND_UNORDERED_ACCESS : 0);
    D3D11_SUBRESOURCE_DATA init={data,w*bpp,0};
    ComPtr<ID3D11Texture2D> t; hr(dev->CreateTexture2D(&d,data ? &init : nullptr,&t)); return t;
}
static float srgb_to_linear(unsigned char v)
{
    const float x=v/255.0f;
    return x<=.04045f ? x/12.92f : std::pow((x+.055f)/1.055f,2.4f);
}
static std::vector<unsigned char> read(ID3D11Texture2D *t, UINT bpp)
{
    D3D11_TEXTURE2D_DESC d; t->GetDesc(&d); d.BindFlags=0;
    d.Usage=D3D11_USAGE_STAGING; d.CPUAccessFlags=D3D11_CPU_ACCESS_READ;
    ComPtr<ID3D11Texture2D> staging; hr(dev->CreateTexture2D(&d,nullptr,&staging));
    ctx->CopyResource(staging.Get(),t);
    D3D11_MAPPED_SUBRESOURCE m; hr(ctx->Map(staging.Get(),0,D3D11_MAP_READ,0,&m));
    std::vector<unsigned char> bytes(W*H*bpp);
    for(UINT y=0;y<H;++y) memcpy(bytes.data()+y*W*bpp,(char*)m.pData+y*m.RowPitch,W*bpp);
    ctx->Unmap(staging.Get(),0); return bytes;
}
static void dispatch(const char *shader, std::vector<ID3D11Texture2D*> inputs,
                     ID3D11Texture2D *out, const void *constants, UINT size=16)
{
    ComPtr<ID3DBlob> code,errors;
    HRESULT result=D3DCompile(shader,strlen(shader),"hdr-test",nullptr,nullptr,"CSMain","cs_5_0",
                             D3DCOMPILE_WARNINGS_ARE_ERRORS,0,&code,&errors);
    if(FAILED(result) && errors) fprintf(stderr,"%s\n",(char*)errors->GetBufferPointer());
    hr(result);
    ComPtr<ID3D11ComputeShader> cs; hr(dev->CreateComputeShader(code->GetBufferPointer(),code->GetBufferSize(),nullptr,&cs));
    std::vector<ComPtr<ID3D11ShaderResourceView>> views(inputs.size());
    std::vector<ID3D11ShaderResourceView*> ptrs;
    for(size_t i=0;i<inputs.size();++i) { hr(dev->CreateShaderResourceView(inputs[i],nullptr,&views[i])); ptrs.push_back(views[i].Get()); }
    ComPtr<ID3D11UnorderedAccessView> uav; hr(dev->CreateUnorderedAccessView(out,nullptr,&uav));
    // A constant buffer is whole 16-byte registers: `size` is the caller's
    // struct, padded with zeroes (a field the caller does not pass reads 0).
    std::array<unsigned char,32> bytes{}; check(size<=bytes.size(),"constants too large");
    memcpy(bytes.data(),constants,size);
    D3D11_BUFFER_DESC bd={}; bd.ByteWidth=(size+15)/16*16; bd.Usage=D3D11_USAGE_DEFAULT; bd.BindFlags=D3D11_BIND_CONSTANT_BUFFER;
    D3D11_SUBRESOURCE_DATA data={bytes.data(),0,0}; ComPtr<ID3D11Buffer> cb; hr(dev->CreateBuffer(&bd,&data,&cb));
    ctx->CSSetShader(cs.Get(),nullptr,0); ctx->CSSetConstantBuffers(0,1,cb.GetAddressOf());
    ctx->CSSetShaderResources(0,(UINT)ptrs.size(),ptrs.data());
    ctx->CSSetUnorderedAccessViews(0,1,uav.GetAddressOf(),nullptr);
    ctx->Dispatch((W+7)/8,(H+7)/8,1);
    ctx->ClearState();
}
int main()
{
    try {
        D3D_FEATURE_LEVEL fl=D3D_FEATURE_LEVEL_11_0;
        hr(D3D11CreateDevice(nullptr,D3D_DRIVER_TYPE_WARP,nullptr,0,&fl,1,D3D11_SDK_VERSION,&dev,nullptr,&ctx));
        std::vector<HALF> raw(W*H*4);
        const Pixel samples[]={{0,0,0,1},{.001f,.001f,.001f,1},{.18f,.18f,.18f,1},
            {1,1,1,1},{2.5f,2.5f,2.5f,1},{12.5f,12.5f,12.5f,1},{125,125,125,1},
            {-.25f,4,1,1},{10,.01f,2,1},{65504,65504,65504,1}};
        for(UINT i=0;i<W*H;++i) for(UINT c=0;c<4;++c) raw[i*4+c]=XMConvertFloatToHalf(samples[i%10][c]);
        auto native=texture(DXGI_FORMAT_R16G16B16A16_FLOAT,raw.data(),8);
        auto proxy=texture(DXGI_FORMAT_R8G8B8A8_UNORM,nullptr,4,true);
        struct { UINT fp; float white; UINT rotate; UINT hdr; } cap={1,2.5f,0,1};
        dispatch(kHdrCaptureHlsl,{native.Get()},proxy.Get(),&cap);
        auto proxyBytes=read(proxy.Get(),4);
        check(proxyBytes[0]==0 && proxyBytes[1]==0,"black lifted in capture");
        check(proxyBytes[4*3] < proxyBytes[4*4] && proxyBytes[4*4] < proxyBytes[4*5] &&
              proxyBytes[4*5] < proxyBytes[4*6],"HDR highlights clipped in proxy");
        check(proxyBytes[4*7]==0,"negative gamut must be clamped only in SDR proxy");
        auto result=texture(DXGI_FORMAT_R16G16B16A16_FLOAT,nullptr,8,true);
        struct { float white; UINT bypass,split,hdr; } comp={2.5f,0,UINT_MAX,1};
        dispatch(kHdrCompositeHlsl,{native.Get(),proxy.Get(),proxy.Get()},result.Get(),&comp);
        auto unchanged=read(result.Get(),8);
        check(memcmp(unchanged.data(),raw.data(),unchanged.size())==0,"zero edit must preserve original FP16 bit for bit");
        // Landscape (flipped): both proxies come off the capture turned over,
        // so the native frame must be read turned over too (issue #47) - with
        // a zero edit the output is the capture rotated 180, bit for bit.
        {
            struct { float white; UINT bypass,split,hdr,rotate180; } rot={2.5f,0,UINT_MAX,1,1};
            dispatch(kHdrCompositeHlsl,{native.Get(),proxy.Get(),proxy.Get()},result.Get(),&rot,sizeof(rot));
            auto turned=read(result.Get(),8);
            bool exact=true;
            for(UINT y=0;y<H;++y) for(UINT x=0;x<W;++x)
                exact=exact && memcmp(turned.data()+(y*W+x)*8,
                                      raw.data()+((H-1-y)*W+(W-1-x))*4,8)==0;
            check(exact,"with rotate180 the composite must read the native frame turned over, like the proxies");
        }
        // A window resizing under HDR: the capture changes size before the
        // output does, and the composite runs on the mismatched pair until the
        // client resizes (PresentHdr). Larger capture: the output is its
        // top-left corner, bit for bit with a zero edit. Smaller capture: the
        // rows it covers stay exact, and the rest comes from the SDR proxy
        // lifted by white - never the black of an out-of-range read.
        {
            const UINT BW=W+4, BH=H+2;
            std::vector<HALF> big(BW*BH*4, XMConvertFloatToHalf(3.0f));
            for(UINT y=0;y<H;++y) memcpy(big.data()+y*BW*4,raw.data()+y*W*4,W*4*sizeof(HALF));
            auto bigNative=texture(DXGI_FORMAT_R16G16B16A16_FLOAT,big.data(),8,false,BW,BH);
            dispatch(kHdrCompositeHlsl,{bigNative.Get(),proxy.Get(),proxy.Get()},result.Get(),&comp);
            auto corner=read(result.Get(),8);
            check(memcmp(corner.data(),raw.data(),corner.size())==0,
                  "a capture larger than the output must compose its top-left corner exactly");
            const UINT SH=H-3;
            auto smallNative=texture(DXGI_FORMAT_R16G16B16A16_FLOAT,raw.data(),8,false,W,SH);
            dispatch(kHdrCompositeHlsl,{smallNative.Get(),proxy.Get(),proxy.Get()},result.Get(),&comp);
            auto filled=read(result.Get(),8);
            check(memcmp(filled.data(),raw.data(),W*SH*8)==0,
                  "the rows a smaller capture covers must stay exact");
            const HALF *px=(const HALF*)filled.data();
            bool lit=false;
            for(UINT y=SH;y<H;++y) for(UINT x=0;x<W;++x) for(UINT c=0;c<3;++c) {
                const UINT i=(y*W+x)*4+c;
                const float got=XMConvertHalfToFloat(px[i]);
                const float want=comp.white*srgb_to_linear(proxyBytes[i]);
                check(std::isfinite(got) && std::abs(got-want)<=.01f*want+.002f,
                      "outside a smaller capture the SDR proxy must fill in");
                lit=lit || got>0.1f;
            }
            check(lit,"the fill below a smaller capture came out black");
        }
        auto pqResult=texture(DXGI_FORMAT_R10G10B10A2_UNORM,nullptr,4,true);
        comp.hdr=3;
        dispatch(kHdrCompositeHlsl,{native.Get(),proxy.Get(),proxy.Get()},pqResult.Get(),&comp);
        auto pqBytes=read(pqResult.Get(),4);
        for(UINT i=0;i<7;++i) {
            const UINT packed=((const UINT*)pqBytes.data())[i];
            const double linear=double(XMConvertHalfToFloat(raw[i*4]))*.008;
            const double q=std::pow(linear,2610.0/16384.0);
            const double expected=std::pow((3424.0/4096.0+(2413.0/128.0)*q)/
                (1+(2392.0/128.0)*q),2523.0/32.0);
            for(UINT c=0;c<3;++c)
                check(std::abs(double((packed>>(c*10))&1023)/1023.0-expected)<.0015,
                      "HDR10 PQ absolute luminance is wrong");
        }
        check((((const UINT*)pqBytes.data())[5]&1023) > (((const UINT*)pqBytes.data())[3]&1023),
              "HDR10 highlights clipped to SDR white");
        comp.hdr=1;
        auto edits=proxyBytes;
        for(UINT i=0;i<W*H;++i) for(UINT c=0;c<3;++c) edits[i*4+c]=255;
        auto edited=texture(DXGI_FORMAT_R8G8B8A8_UNORM,edits.data(),4);
        comp.bypass=1;
        dispatch(kHdrCompositeHlsl,{native.Get(),proxy.Get(),edited.Get()},result.Get(),&comp);
        auto bypass=read(result.Get(),8);
        check(memcmp(bypass.data(),raw.data(),bypass.size())==0,"bypass changed HDR original");
        comp.bypass=0; comp.split=8;
        dispatch(kHdrCompositeHlsl,{native.Get(),proxy.Get(),edited.Get()},result.Get(),&comp);
        auto split=read(result.Get(),8);
        for(UINT y=0;y<H;++y) check(memcmp(split.data()+y*W*8,raw.data()+y*W*4,8*8)==0,"wipe changed raw side");
        const HALF *values=(const HALF*)split.data();
        bool changed=false;
        for(UINT i=0;i<W*H*4;++i) {
            float f=XMConvertHalfToFloat(values[i]);
            check(std::isfinite(f),"HDR composite produced NaN/Inf");
            if(i%4<3 && values[i]!=raw[i]) changed=true;
        }
        check(changed,"neural edit was discarded");
        comp.hdr=0; comp.split=UINT_MAX;
        dispatch(kHdrCompositeHlsl,{native.Get(),proxy.Get(),edited.Get()},result.Get(),&comp);
        auto sdr=read(result.Get(),8);
        for(UINT i=0;i<W*H*4;++i) check(XMConvertHalfToFloat(((HALF*)sdr.data())[i])==1.0f,"SDR output must be linear unit white");
        // The old BGRA path must remain a channel-correct, byte-exact copy.
        std::vector<unsigned char> bgra(W*H*4);
        for(UINT i=0;i<W*H;++i) {bgra[i*4]=17; bgra[i*4+1]=93; bgra[i*4+2]=201; bgra[i*4+3]=255;}
        auto sdrInput=texture(DXGI_FORMAT_B8G8R8A8_UNORM,bgra.data(),4);
        cap.fp=0; dispatch(kHdrCaptureHlsl,{sdrInput.Get()},proxy.Get(),&cap);
        auto rgba=read(proxy.Get(),4);
        for(UINT i=0;i<W*H;++i) check(rgba[i*4]==201 && rgba[i*4+1]==93 && rgba[i*4+2]==17 && rgba[i*4+3]==255,"SDR channel regression");
        // "Landscape (flipped)": duplication hands back the UNROTATED
        // desktop, so the shader turns it over as it reads (issue #47).
        // W and H are odd, which puts one pixel at the centre mapping to
        // itself and no pair mapping to a neighbour.
        std::vector<unsigned char> marked(W*H*4);
        for(UINT y=0;y<H;++y) for(UINT x=0;x<W;++x) {
            const UINT i=(y*W+x)*4;
            marked[i]=(unsigned char)(x*13+1); marked[i+1]=(unsigned char)(y*29+2);
            marked[i+2]=(unsigned char)((x+y)*7+3); marked[i+3]=255; }
        auto markedTex=texture(DXGI_FORMAT_B8G8R8A8_UNORM,marked.data(),4);
        cap.fp=0; cap.rotate=1;
        dispatch(kHdrCaptureHlsl,{markedTex.Get()},proxy.Get(),&cap);
        auto turned=read(proxy.Get(),4);
        for(UINT y=0;y<H;++y) for(UINT x=0;x<W;++x) {
            const UINT d=(y*W+x)*4, s=((H-1-y)*W+(W-1-x))*4;
            check(turned[d]==marked[s+2] && turned[d+1]==marked[s+1] &&
                  turned[d+2]==marked[s],"180 rotation did not turn the frame over"); }
        cap.rotate=0;
        dispatch(kHdrCaptureHlsl,{markedTex.Get()},proxy.Get(),&cap);
        auto upright=read(proxy.Get(),4);
        for(UINT i=0;i<W*H;++i)
            check(upright[i*4]==marked[i*4+2] && upright[i*4+1]==marked[i*4+1] &&
                  upright[i*4+2]==marked[i*4],"the unrotated path moved a pixel");
        // #99: a 10-bit SDR display duplicates as FP16 with HDR OFF, and that
        // frame's 1.0 IS white. Running the HDR tone map on it turned white
        // into ToSrgb(1/(white+Peak)) - the 74 %-grey report. Same frame, same
        // values, only the hdr flag differs: with it, unit white must come out
        // 255; without it, 187 (the old, wrong behaviour) - so this cannot
        // pass by the tone map simply being gone.
        std::vector<HALF> unit(W*H*4);
        for(UINT i=0;i<W*H;++i) {
            unit[i*4]=XMConvertFloatToHalf(1.0f); unit[i*4+1]=XMConvertFloatToHalf(1.0f);
            unit[i*4+2]=XMConvertFloatToHalf(1.0f); unit[i*4+3]=XMConvertFloatToHalf(1.0f); }
        auto unitTex=texture(DXGI_FORMAT_R16G16B16A16_FLOAT,unit.data(),8);
        cap.fp=1; cap.white=1.0f; cap.hdr=0;
        dispatch(kHdrCaptureHlsl,{unitTex.Get()},proxy.Get(),&cap);
        auto sdrFloat=read(proxy.Get(),4);
        check(sdrFloat[0]==255 && sdrFloat[1]==255 && sdrFloat[2]==255,
              "FP16 without HDR must keep SDR white at 255, not tone-map it");
        cap.hdr=1;
        dispatch(kHdrCaptureHlsl,{unitTex.Get()},proxy.Get(),&cap);
        auto hdrTone=read(proxy.Get(),4);
        // ToSrgb(1/(1+1)) = 0.7354 -> 187.5, so the exact byte depends on the
        // conversion's rounding; a range keeps the check about the tone map
        // being applied at all, which is the contract.
        check(hdrTone[0] >= 180 && hdrTone[0] <= 195,
              "a real scRGB capture at unit white must still tone-map (~187)");
        // Shown as SDR (HDR compatibility off, no composite to invert the
        // tone map): SDR white keeps its place, highlights roll off above it.
        cap.hdr=2;
        dispatch(kHdrCaptureHlsl,{unitTex.Get()},proxy.Get(),&cap);
        auto shownSdr=read(proxy.Get(),4);
        check(shownSdr[0] >= 248 && shownSdr[1] >= 248 && shownSdr[2] >= 248,
              "an scRGB capture shown as SDR must keep SDR white near 255, not grey (187)");
        cap.white=2.05f;
        dispatch(kHdrCaptureHlsl,{native.Get()},proxy.Get(),&cap);
        auto shownHigh=read(proxy.Get(),4);
        check(shownHigh[12] < shownHigh[16] && shownHigh[16] < shownHigh[20],
              "an scRGB capture shown as SDR must keep its highlights in order");
        cap.white=1.0f;
        // Source HDR and the output preference are independent. Reuse the
        // same texture across mode changes so stale conversion state fails.
        for (UINT sourceHdr : {1u, 0u, 1u}) {
            for (UINT outputHdr : {0u, 1u}) {
                cap.fp=1; cap.white=2.05f; cap.hdr=sourceHdr;
                dispatch(kHdrCaptureHlsl,{native.Get()},proxy.Get(),&cap);
                auto converted=read(proxy.Get(),4);
                if (sourceHdr) {
                    check(converted[12] < converted[16] && converted[16] < converted[20],
                          "HDR source highlights clipped when output HDR is off");
                    check(std::abs(int(converted[12])-155)<=1 &&
                          std::abs(int(converted[16])-196)<=1 &&
                          std::abs(int(converted[20])-239)<=1,
                          "HDR source conversion changed with output preference");
                } else {
                    check(converted[12]==255,"SDR FP16 white must not be tone-mapped");
                }
                comp.white=cap.white; comp.bypass=1; comp.split=UINT_MAX; comp.hdr=outputHdr;
                dispatch(kHdrCompositeHlsl,{native.Get(),proxy.Get(),proxy.Get()},result.Get(),&comp);
                auto presented=read(result.Get(),8);
                if (outputHdr) {
                    check(memcmp(presented.data(),raw.data(),presented.size())==0,
                          "HDR bypass changed during source mode transitions");
                } else {
                    const HALF *linear=(const HALF*)presented.data();
                    check(XMConvertHalfToFloat(linear[12])<=1.0f,
                          "SDR presentation exceeded unit white");
                    if (sourceHdr)
                        check(linear[12]<linear[16] && linear[16]<linear[20],
                              "SDR presentation lost HDR source highlight detail");
                }
            }
        }
        cap.hdr=0;
        auto display=QueryHdrDisplay(MonitorFromPoint(POINT{0,0},MONITOR_DEFAULTTOPRIMARY));
        printf("Display probe: HDR=%d, SDR white=%.1f nits\n",display.enabled,display.white*80);
        puts("PASS: HDR shader compilation, highlights, signed gamut, zero-edit identity, capture/output size mismatch (clipped, filled), bypass, wipe, finite edits, SDR output, channel order, 180 rotation in capture and composite (#47) and SDR-FP16 white (#99) (WARP)");
        return 0;
    } catch(const std::exception &e) { fprintf(stderr,"FAIL: %s\n",e.what()); return 1; }
}
