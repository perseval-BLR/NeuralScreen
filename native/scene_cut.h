#pragma once
// The scene-cut decision on the 320x180 grey frame - the C++ twin of
// app/scene_cut.py, which explains the rule and carries its measurements.
// Integer arithmetic throughout, the same order of operations and the same
// tie rules, so both give the same decision on the same grey; the test
// (tests/test_scene_cut.py) builds this header into a small driver and
// compares them frame by frame.
#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <vector>

namespace ns_scene {

constexpr int64_t kNormCutPermille = 400;
constexpr int64_t kEnvCutPermille = 80;
constexpr int64_t kBlankX16 = 48;
constexpr int64_t kPictureX16 = 96;
constexpr int kCoarseRadius = 6;
constexpr int kTopK = 3;
constexpr int kGainBits = 12;

struct Image {
    int w = 0, h = 0;
    std::vector<uint8_t> px;
    uint8_t at(int x, int y) const { return px[size_t(y) * w + x]; }
};

inline Image Box2(const uint8_t *src, int w, int h, int pitch)
{
    Image out;
    out.w = w / 2; out.h = h / 2;
    out.px.resize(size_t(out.w) * out.h);
    for (int y = 0; y < out.h; ++y)
    {
        const uint8_t *a = src + size_t(2 * y) * pitch;
        const uint8_t *b = a + pitch;
        for (int x = 0; x < out.w; ++x)
        {
            const unsigned s = unsigned(a[2 * x]) + b[2 * x] + a[2 * x + 1] + b[2 * x + 1];
            out.px[size_t(y) * out.w + x] = uint8_t((s + 2) >> 2);
        }
    }
    return out;
}

inline Image Box2(const Image &g) { return Box2(g.px.data(), g.w, g.h, g.w); }

struct Sad { int64_t s = 0, n = 0; };

inline Sad Overlap(const Image &c, const Image &p, int dx, int dy)
{
    const int x0 = std::max(0, -dx), x1 = std::min(c.w, c.w - dx);
    const int y0 = std::max(0, -dy), y1 = std::min(c.h, c.h - dy);
    Sad r;
    if (x1 <= x0 || y1 <= y0) return r;
    for (int y = y0; y < y1; ++y)
    {
        const uint8_t *cr = &c.px[size_t(y) * c.w];
        const uint8_t *pr = &p.px[size_t(y + dy) * p.w];
        int64_t row = 0;
        for (int x = x0; x < x1; ++x) row += std::abs(int(cr[x]) - int(pr[x + dx]));
        r.s += row;
    }
    r.n = int64_t(y1 - y0) * (x1 - x0);
    return r;
}

inline bool Better(const Sad &a, const Sad &best)
{
    return best.n == 0 || a.s * best.n < best.s * a.n;
}

struct Shift { int dx = 0, dy = 0; Sad sad; };

inline Shift Align(const Image &cur, const Image &prev)
{
    const Image c2 = Box2(cur), p2 = Box2(prev);
    const Image c3 = Box2(c2), p3 = Box2(p2);
    Shift top[kTopK];
    int count = 0;
    for (int dy = -kCoarseRadius; dy <= kCoarseRadius; ++dy)
        for (int dx = -kCoarseRadius; dx <= kCoarseRadius; ++dx)
        {
            const Sad s = Overlap(c3, p3, dx, dy);
            if (s.n == 0) continue;
            if (count == kTopK && !Better(s, top[count - 1].sad)) continue;
            int i = count;
            while (i > 0 && Better(s, top[i - 1].sad)) --i;
            for (int j = std::min(count, kTopK - 1); j > i; --j) top[j] = top[j - 1];
            if (i < kTopK) { top[i].dx = dx; top[i].dy = dy; top[i].sad = s; }
            if (count < kTopK) ++count;
        }
    Shift refined[kTopK];
    for (int k = 0; k < count; ++k)
    {
        int bx = top[k].dx, by = top[k].dy;
        const Image *levels[2][2] = {{&c2, &p2}, {&cur, &prev}};
        for (auto &lv : levels)
        {
            const int cx = 2 * bx, cy = 2 * by;
            Sad best;
            for (int dy = cy - 1; dy <= cy + 1; ++dy)
                for (int dx = cx - 1; dx <= cx + 1; ++dx)
                {
                    const Sad s = Overlap(*lv[0], *lv[1], dx, dy);
                    if (s.n != 0 && Better(s, best)) { best = s; bx = dx; by = dy; }
                }
        }
        refined[k].dx = bx; refined[k].dy = by;
    }
    Shift result;
    result.sad = Overlap(cur, prev, 0, 0);
    for (int k = 0; k < count; ++k)
    {
        const Sad s = Overlap(cur, prev, refined[k].dx, refined[k].dy);
        if (s.n != 0 && Better(s, result.sad))
        { result.sad = s; result.dx = refined[k].dx; result.dy = refined[k].dy; }
    }
    return result;
}

struct Stats { int64_t mu = 0, mad = 0; };

inline Stats FrameStats(const Image &g)
{
    const int64_t n = int64_t(g.px.size());
    int64_t sum = 0;
    for (uint8_t v : g.px) sum += v;
    Stats st;
    st.mu = (sum * 2 + n) / (2 * n);
    for (uint8_t v : g.px) st.mad += std::abs(int64_t(v) - st.mu);
    return st;
}

// Floor division by 2^bits for a signed value, as numpy's >> does.
inline int64_t FloorShift(int64_t v, int bits) { return v >= 0 ? (v >> bits) : -((-v + (int64_t(1) << bits) - 1) >> bits); }

inline Image MapOnto(const Image &hi, const Stats &st_hi, const Stats &st_lo)
{
    const int64_t g = st_hi.mad ? (st_lo.mad * (int64_t(2) << kGainBits) + st_hi.mad) / (2 * st_hi.mad) : 0;
    Image out = hi;
    for (auto &v : out.px)
    {
        const int64_t m = st_lo.mu + FloorShift((int64_t(v) - st_hi.mu) * g + (int64_t(1) << (kGainBits - 1)), kGainBits);
        v = uint8_t(std::min<int64_t>(255, std::max<int64_t>(0, m)));
    }
    return out;
}

inline int64_t Envelope(const Image &cur, const Image &prev, int dx, int dy)
{
    const int x0 = std::max(0, -dx), x1 = std::min(cur.w, cur.w - dx);
    const int y0 = std::max(0, -dy), y1 = std::min(cur.h, cur.h - dy);
    int64_t e = 0;
    for (int y = y0; y < y1; ++y)
        for (int x = x0; x < x1; ++x)
        {
            const int px = x + dx, py = y + dy;
            int lo = 255, hi = 0;
            for (int oy = -1; oy <= 1; ++oy)
                for (int ox = -1; ox <= 1; ++ox)
                {
                    const int sx = std::min(prev.w - 1, std::max(0, px + ox));
                    const int sy = std::min(prev.h - 1, std::max(0, py + oy));
                    const int v = prev.at(sx, sy);
                    lo = std::min(lo, v); hi = std::max(hi, v);
                }
            const int c = cur.at(x, y);
            e += std::max(0, c - hi) + std::max(0, lo - c);
        }
    return e;
}

struct Result {
    bool cut = true;
    float score = 1.0f;
    int dx = 0, dy = 0;
    int64_t sad = 0, n = 0, env = 0, mad_lo = 0, mad_hi = 0;
};

// One frame: compares the new grey with the previous frame's L1 (kept here)
// and returns the decision. The first frame, and a frame of another size,
// is a cut.
class Detector {
public:
    void Reset() { prev_.px.clear(); prev_.w = prev_.h = 0; }

    Result Step(const uint8_t *grey, int w, int h, int pitch)
    {
        Image l1 = Box2(grey, w, h, pitch);
        Result r;
        if (prev_.px.empty() || prev_.w != l1.w || prev_.h != l1.h)
        {
            prev_ = std::move(l1);
            return r;
        }
        const Stats sc = FrameStats(l1), sp = FrameStats(prev_);
        const int64_t npix = int64_t(l1.px.size());
        r.mad_lo = std::min(sc.mad, sp.mad);
        r.mad_hi = std::max(sc.mad, sp.mad);
        if (r.mad_lo * 16 < kBlankX16 * npix)
        {
            r.cut = r.mad_hi * 16 >= kPictureX16 * npix;
            r.score = r.cut ? 1.0f : 0.0f;
            prev_ = std::move(l1);
            return r;
        }
        Image c_img, p_img;
        if (sc.mad <= sp.mad) { c_img = l1; p_img = MapOnto(prev_, sp, sc); }
        else { c_img = MapOnto(l1, sc, sp); p_img = prev_; }
        const Shift s = Align(c_img, p_img);
        r.dx = s.dx; r.dy = s.dy; r.sad = s.sad.s; r.n = s.sad.n;
        r.env = Envelope(c_img, p_img, s.dx, s.dy);
        const int64_t den = r.n * 2 * r.mad_lo;
        r.cut = r.sad * npix * 1000 > kNormCutPermille * den &&
                r.env * npix * 1000 > kEnvCutPermille * den;
        r.score = den ? float((std::min)(1.0, double(r.sad * npix) / double(den))) : 1.0f;
        prev_ = std::move(l1);
        return r;
    }

private:
    Image prev_;
};

}  // namespace ns_scene
