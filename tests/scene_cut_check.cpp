// Driver for tests/test_scene_cut.py: runs native/scene_cut.h over grey
// sequences and prints what it decided, so the test can compare the worker's
// rule with app/scene_cut.py frame by frame.
//
// Input (binary, little-endian): int32 sequences; per sequence int32 frames,
// int32 width, int32 height, then frames * width * height bytes.
// Output: one line per frame - sequence frame cut dx dy sad n env mad_lo mad_hi.
#include "../native/scene_cut.h"

#include <cstdio>
#include <vector>

int main(int argc, char **argv)
{
    if (argc < 2) return 2;
    FILE *in = nullptr;
    if (fopen_s(&in, argv[1], "rb") != 0 || in == nullptr) return 3;
    int32_t sequences = 0;
    if (fread(&sequences, 4, 1, in) != 1) return 4;
    for (int32_t s = 0; s < sequences; ++s)
    {
        int32_t hdr[3] = {};
        if (fread(hdr, 4, 3, in) != 3) return 5;
        const int32_t frames = hdr[0], w = hdr[1], h = hdr[2];
        std::vector<uint8_t> grey(size_t(w) * h);
        ns_scene::Detector detector;
        for (int32_t f = 0; f < frames; ++f)
        {
            if (fread(grey.data(), 1, grey.size(), in) != grey.size()) return 6;
            const ns_scene::Result r = detector.Step(grey.data(), w, h, w);
            printf("%d %d %d %d %d %lld %lld %lld %lld %lld\n", s, f, r.cut ? 1 : 0, r.dx, r.dy,
                   (long long)r.sad, (long long)r.n, (long long)r.env,
                   (long long)r.mad_lo, (long long)r.mad_hi);
        }
    }
    fclose(in);
    return 0;
}
