// present_follow_check.exe - the follower's per-step decision, driven with the
// numbers real reports carry. No worker, no GPU, no windows: just the header
// the worker compiles, so a regression in the rule fails here by itself.
//
// Built and run by tests/test_present_follow.py. Prints one JSON line:
//   {"checks": N, "failed": M, "failures": ["..."]}
// Exit code 0 when every check passed.
//
// The cases are the two rectangles of one window as Windows reports them:
//   Windows 10 - GetWindowRect (the capture's size, invisible resize border
//                included) against DWMWA_EXTENDED_FRAME_BOUNDS (the frame).
//                Issue #30 measured 1354x853 against 1340x846.
//   Windows 11 - the two agree, and every slack is zero.
#include <cstdio>
#include <cstdint>
#include <string>
#include <vector>

#include "present_follow.h"

namespace {

int checks_total = 0;
int failures_total = 0;
std::vector<std::string> failures;

void Check(bool ok, const std::string &what) {
    ++checks_total;
    if (!ok) {
        ++failures_total;
        if (failures.size() < 8) failures.push_back(what);
    }
}

using ns_present_follow::CaptureSize;
using ns_present_follow::CaptureSlack;
using ns_present_follow::InitialCaptureSize;
using ns_present_follow::DecideForStep;
using ns_present_follow::Verdict;

void Case(const char *name, std::uint32_t bw, std::uint32_t bh,
          std::uint32_t fw, std::uint32_t fh, std::uint32_t sw,
          std::uint32_t sh, Verdict want) {
    const Verdict got = DecideForStep(bw, bh, fw, fh, sw, sh);
    Check(got == want, std::string(name) + ": verdict " +
                           (got == Verdict::Place ? "Place" : "Hide") +
                           ", wanted " +
                           (want == Verdict::Place ? "Place" : "Hide"));
}

}  // namespace

int main() {
    // 1. Windows 10, issue #30's own measurement (1354x853 captured against a
    //    1340x846 frame). The buffer IS the capture's size - this is what the
    //    client builds from the reported size - so the slack has to be taken
    //    into account or every window on Windows 10 is judged too large.
    {
        const std::uint32_t sw = CaptureSlack(1354, 1340, true);
        const std::uint32_t sh = CaptureSlack(853, 846, true);
        Check(sw == 14, "issue #30: horizontal slack is 14, got " +
                            std::to_string(sw));
        Check(sh == 7, "issue #30: vertical slack is 7, got " +
                           std::to_string(sh));
        Case("issue #30 window, buffer at the capture size", 1354, 853,
             1340, 846, sw, sh, Verdict::Place);
        // The regression itself: the same step with the slack ignored - the
        // shape of the code before this fix, which hid the overlay.
        Case("issue #30 window, slack ignored (the #139 bug)", 1354, 853,
             1340, 846, 0, 0, Verdict::Hide);
    }

    // 2. Windows 11: capture and frame agree, nothing changes. Every slack is
    //    zero and the rule is exactly the one it always was.
    {
        const std::uint32_t sw = CaptureSlack(1920, 1920, true);
        const std::uint32_t sh = CaptureSlack(1080, 1080, true);
        Check(sw == 0 && sh == 0, "Windows 11: slack is zero");
        Case("Windows 11 exact fit", 1920, 1080, 1920, 1080, sw, sh,
             Verdict::Place);
        Case("Windows 11 buffer smaller than the frame", 1280, 720, 1920,
             1080, sw, sh, Verdict::Place);
    }

    // 3. A REAL resize must still hide the overlay - the behaviour the rule
    //    exists for. A window shrunk well past the buffer is not a border; it
    //    is a resize the client has not landed yet.
    {
        Case("a real resize: 1920-wide buffer, 1280-wide frame", 1920, 1080,
             1280, 720, 0, 0, Verdict::Hide);
        Case("a real resize, height only", 1920, 1080, 1920, 700, 0, 0,
             Verdict::Hide);
        // The mirror: a buffer SMALLER than a frame that grew - placeable, it
        // sits in the corner (the old "swallow it" case).
        Case("the frame grew, buffer smaller", 1280, 720, 1920, 1080, 0, 0,
             Verdict::Place);
    }

    // 4. The slack is bounded by the measurement, never a licence to place a
    //    genuinely oversized buffer: a buffer larger than the frame BY MORE
    //    than the border still hides.
    {
        Case("buffer 20 px over the frame, border is 14", 1360, 853, 1340,
             846, 14, 7, Verdict::Hide);
        // And the border cannot rescue a mismatch on the other axis.
        Case("width fits with the border, height does not", 1354, 900, 1340,
             846, 14, 7, Verdict::Hide);
    }

    // 5. CaptureSlack's own contract: a capture narrower than the frame is
    //    clamped to zero rather than reported as a negative slack, and an
    //    unknown capture size leaves the comparison alone.
    {
        Check(CaptureSlack(1280, 1920, true) == 0,
              "a narrower capture gives zero slack, not a negative one");
        Check(CaptureSlack(2000, 1900, false) == 0,
              "an unknown capture gives zero slack");
        Check(CaptureSlack(0, 0, true) == 0, "identical rectangles give zero");
    }

    // 6. The size a window capture opens at (#140): the frames WGC delivers,
    //    not the item's GetWindowRect size. Opened at the item's size the
    //    buffer was 14x7 px larger than every frame and that strip was black.
    {
        auto opens = [](const char *name, CaptureSize got, std::uint32_t w,
                        std::uint32_t h) {
            Check(got.w == w && got.h == h,
                  std::string(name) + ": opens at " + std::to_string(got.w) +
                      "x" + std::to_string(got.h) + ", wanted " +
                      std::to_string(w) + "x" + std::to_string(h));
        };
        opens("issue #140 window (Windows 10)",
              InitialCaptureSize(813, 1017, true, 799, 1010), 799, 1010);
        opens("issue #30 window (Windows 10)",
              InitialCaptureSize(1354, 853, true, 1340, 846), 1340, 846);
        opens("Windows 11: item and frame agree",
              InitialCaptureSize(1920, 1080, true, 1920, 1080), 1920, 1080);
        opens("frame query failed: the item's size",
              InitialCaptureSize(813, 1017, false, 799, 1010), 813, 1017);
        opens("a frame larger than the item never grows the capture",
              InitialCaptureSize(1920, 1080, true, 1936, 1096), 1920, 1080);
        opens("an empty frame rect is ignored",
              InitialCaptureSize(813, 1017, true, 0, 0), 813, 1017);
    }

    std::printf("{\"checks\": %d, \"failed\": %d, \"failures\": [",
                checks_total, failures_total);
    for (std::size_t i = 0; i < failures.size(); ++i)
        std::printf("%s\"%s\"", i ? ", " : "", failures[i].c_str());
    std::printf("]}\n");
    return failures_total == 0 ? 0 : 1;
}
