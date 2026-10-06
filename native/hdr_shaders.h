#pragma once

// scRGB uses linear BT.709 primaries; 1.0 is 80 nits. The neural runtime
// continues to receive SDR. Keep its quantized input as the residual anchor.
#define NS_HDR_COLOR_FUNCTIONS \
    "float3 ToLinear(float3 x) { return float3(" \
    "x.r <= .04045 ? x.r / 12.92 : pow(max((x.r + .055) / 1.055, 0), 2.4)," \
    "x.g <= .04045 ? x.g / 12.92 : pow(max((x.g + .055) / 1.055, 0), 2.4)," \
    "x.b <= .04045 ? x.b / 12.92 : pow(max((x.b + .055) / 1.055, 0), 2.4)); }\n" \
    "float3 ToSrgb(float3 x) { x = max(x, 0); return float3(" \
    "x.r <= .0031308 ? x.r * 12.92 : 1.055 * pow(x.r, 1.0/2.4) - .055," \
    "x.g <= .0031308 ? x.g * 12.92 : 1.055 * pow(x.g, 1.0/2.4) - .055," \
    "x.b <= .0031308 ? x.b * 12.92 : 1.055 * pow(x.b, 1.0/2.4) - .055); }\n" \
    "float Peak(float3 x) { return max(0, max(x.r, max(x.g, x.b))); }\n"

// rotate180: Desktop Duplication hands back the UNROTATED desktop, so on
// "Landscape (flipped)" the source is upside down against what the user
// sees. The frame keeps its size, so reading it from the opposite corner is
// the whole correction - and it has to happen here, before the network, the
// optical flow and the gray channel all take their copy (issue #47).
static const char kHdrCaptureHlsl[] =
    NS_HDR_COLOR_FUNCTIONS
    "Texture2D<float4> src : register(t0);\n"
    "RWTexture2D<float4> dst : register(u0);\n"
    "cbuffer Params : register(b0) { uint isFloat; float white; uint rotate180; uint hdr; };\n"
    "[numthreads(8,8,1)] void CSMain(uint3 p : SV_DispatchThreadID) {\n"
    " uint w,h; dst.GetDimensions(w,h); if(p.x>=w || p.y>=h) return;\n"
    " int2 s = rotate180 ? int2(w-1-p.x, h-1-p.y) : int2(p.xy);\n"
    " float4 c=src.Load(int3(s,0));\n"
    // isFloat and hdr are two different facts. "the frame arrived as FP16" is
    // not "the picture is scRGB" - a 10-bit SDR scan-out also duplicates as
    // FP16 (the format is pinned so it cannot flap, issue #86), and that frame
    // carries an SDR desktop whose 1.0 IS white. Dividing it by white+Peak
    // tone-mapped an SDR picture: white landed at ToSrgb(1/(1+1)) = 0.735, the
    // "whites turned grey" report (#99, 10 bpc with HDR off). Only a real
    // scRGB capture gets the tone map; FP16-without-HDR is linear SDR and
    // needs nothing but the display encode.
    //
    // hdr == 2: a real scRGB capture that is SHOWN as SDR (HDR compatibility
    // off). The tone map above is the half of a pair - the HDR composite
    // inverts it - and shown on its own it put SDR white at 0.5 linear, 187
    // of 255: with Windows HDR on and the switch off (its default) every
    // white the program showed was grey next to the desktop around it.
    // Here SDR white maps to itself, linearly up to 0.9 of it, and only what
    // is brighter than that rolls off on a rational tail towards 1 - white
    // lands at ~250, and highlights several times brighter still keep their
    // order instead of clipping to one value.
    " if(isFloat && hdr==2) { float3 x=max(c.rgb,0)/white; float3 t=max(x-0.9,0);\n"
    "   x=x<=0.9 ? x : 0.9+0.1*t/(t+0.0667); c=float4(ToSrgb(x),1); }\n"
    " else if(isFloat) c=float4(ToSrgb(max(c.rgb,0)/(hdr ? white+Peak(c.rgb) : 1.0)),1);\n"
    " dst[p.xy]=c; }\n";

static const char kHdrCompositeHlsl[] =
    NS_HDR_COLOR_FUNCTIONS
    "Texture2D<float4> nativeFrame : register(t0);\n"
    "Texture2D<float4> proxyIn : register(t1);\n"
    "Texture2D<float4> proxyOut : register(t2);\n"
    "RWTexture2D<float4> dst : register(u0);\n"
    "cbuffer Params : register(b0) { float white; uint bypass; uint split; uint hdrDisplay; uint rotate180; };\n"
    "[numthreads(8,8,1)] void CSMain(uint3 p : SV_DispatchThreadID) {\n"
    " uint w,h; dst.GetDimensions(w,h); if(p.x>=w || p.y>=h) return;\n"
    " float3 a=ToLinear(proxyIn.Load(int3(p.xy,0)).rgb);\n"
    " float3 b=ToLinear(proxyOut.Load(int3(p.xy,0)).rgb);\n"
    // Until the client resizes after a window shrank, the capture is smaller
    // than the output and does not cover all of it (PresentHdr). There the
    // SDR proxy stands in, lifted by the capture's own scale, rather than
    // the black an out-of-range Load returns.
    //
    // rotate180: the native frame is the duplication's own texture, as
    // unrotated as the capture shader found it, while both proxies were
    // turned over on capture. Read the same way, or a "Landscape (flipped)"
    // display gets the original upside down under an upright correction
    // (issue #47).
    " uint nw,nh; nativeFrame.GetDimensions(nw,nh);\n"
    " int2 q = rotate180 ? int2(nw-1-p.x, nh-1-p.y) : int2(p.xy);\n"
    " float3 original=(p.x<nw && p.y<nh) ? nativeFrame.Load(int3(q,0)).rgb : white*a;\n"
    " bool raw=bypass || (split!=0xffffffff && p.x<split);\n"
    // No inverse tone mapping: it becomes singular near white. Lift a bounded
    // linear residual using the same scale as capture, preserving signed gamut
    // and highlights exactly when the neural edit is zero.
    " float3 result=raw ? original : original+(white+Peak(original))*clamp(b-a,-.25,.25);\n"
    " if(!bypass && split!=0xffffffff && p.x>=split && p.x<split+2)\n"
    "   result=white*ToLinear(float3(.25,.65,1));\n"
    " if(!(hdrDisplay & 1)) result=raw ? a : b;\n"
    // DLSS-G supports HDR10, not scRGB. Convert linear BT.709 (80 nits/unit)
    // to BT.2020 and ST.2084, preserving absolute luminance up to 10000 nits.
    " if(hdrDisplay & 2) {\n"
    "   float3 rec2020=mul(float3x3(.627404,.329283,.043313,\n"
    "       .069097,.919540,.011362,.016391,.088013,.895595),result);\n"
    "   float3 q=pow(saturate(rec2020*.008),2610.0/16384.0);\n"
    "   result=pow((3424.0/4096.0+(2413.0/128.0)*q)/(1+(2392.0/128.0)*q),2523.0/32.0);\n"
    " }\n"
    " dst[p.xy]=float4(clamp(result,-65504,65504),1); }\n";
