// present_follow.h - the decision the follower takes for one step.
//
// The overlay the worker presents into keeps the size of the buffer it was
// built for (the swap chain, the textures and the client's shared memory are
// all sized for one frame, so a resize means the client rebuilding the
// pipeline). The window it follows can be any size, so every step answers one
// question: can the buffer be shown inside the frame right now?
//
//   * the buffer FITS  - place it in the frame's top-left corner. A buffer
//     smaller than the frame sits inside it; an equal one covers it exactly.
//   * the buffer is LARGER - it cannot be shown without hanging past the
//     frame's right/bottom edges (the trail of copies) or stretching stale
//     content into the new rect (the shimmer). It is hidden until the client's
//     live resize lands the new size.
//
// "Larger" is measured against the surface the buffer is PRESENTED on, not
// against the frame alone. The two are not the same rectangle, and on Windows
// 10 the difference is the invisible resize border: the capture comes back at
// the GetWindowRect size, border included, while the frame below is
// DWMWA_EXTENDED_FRAME_BOUNDS without it. One window in issue #30 measured
// 1354x853 captured against 1340x846 framed - 14 and 7 pixels of border for
// the same window.
//
// Taken literally, `buffer > frame` is true for EVERY window on Windows 10,
// because the client builds the buffer from the size the capture reported. The
// overlay was therefore hidden on the first follow step of the first
// window-mode switch and stayed hidden: the effect was gone for as long as
// that window was captured, while the capture border stayed on screen (#139).
// Fullscreen came back clean because there is no follower there at all.
// Windows 11 is why it never reproduced on the bench - there the capture and
// the frame agree, and every slack below is zero.
//
// The border is measured in the same step rather than assumed to be a
// constant: the caller passes what the capture exceeds the frame by. On
// Windows 11 that is zero and this is exactly the comparison it always was.
//
// Header-only and free of Windows types on purpose: it is the part of the
// follower that can be driven with real numbers from a report, so
// tests/present_follow_check.cpp runs it on #30's and #139's measurements.
#pragma once

#include <cstdint>

namespace ns_present_follow {

enum class Verdict {
    Place,   // the buffer fits the frame: move/resize the overlay onto it
    Hide,    // the buffer is larger: hide until the client's resize lands
};

// How much the capture exceeds the frame on one axis - the invisible resize
// border, in pixels, or zero when there is none (Windows 11, or a failed
// query). `capture_side` and `frame_side` are the same axis of the two
// rectangles; a capture NARROWER than the frame is clamped to zero rather
// than treated as a negative slack.
inline std::uint32_t CaptureSlack(std::int32_t capture_side,
                                 std::int32_t frame_side,
                                 bool capture_known) {
    if (!capture_known) return 0u;
    const long long d = static_cast<long long>(capture_side) -
                        static_cast<long long>(frame_side);
    return d > 0 ? static_cast<std::uint32_t>(d) : 0u;
}

// The step's verdict: may the buffer be presented inside the frame?
inline Verdict DecideForStep(std::uint32_t buffer_w, std::uint32_t buffer_h,
                            std::uint32_t frame_w, std::uint32_t frame_h,
                            std::uint32_t slack_w, std::uint32_t slack_h) {
    if (buffer_w > frame_w + slack_w) return Verdict::Hide;
    if (buffer_h > frame_h + slack_h) return Verdict::Hide;
    return Verdict::Place;
}

// The size a window capture opens at (#140). The capture item reports the
// GetWindowRect size, but on Windows 10 the frames WGC delivers carry only the
// visible frame (their ContentSize is DWMWA_EXTENDED_FRAME_BOUNDS): one window
// in #140 reported 813x1017 and delivered 799x1010. Opened at the item's size,
// the client built every buffer 14x7 px too large, the frame pool was quietly
// recreated at the content size, and the strip the frames never reach stayed
// black - under the network, on screen past the window's edge, and in every
// screenshot. Opened at the frame's size, the buffer is what arrives.
//
// The frame is taken only where it is SMALLER than the item, axis by axis: a
// failed query, Windows 11 (the two agree) or anything odd keeps the item's
// size, and a real mismatch is still caught by the pool's ContentSize check.
struct CaptureSize {
    std::uint32_t w;
    std::uint32_t h;
};

inline CaptureSize InitialCaptureSize(std::int32_t item_w, std::int32_t item_h,
                                      bool frame_known, std::int32_t frame_w,
                                      std::int32_t frame_h) {
    CaptureSize size = {item_w > 0 ? static_cast<std::uint32_t>(item_w) : 0u,
                        item_h > 0 ? static_cast<std::uint32_t>(item_h) : 0u};
    if (!frame_known) return size;
    if (frame_w > 0 && static_cast<std::uint32_t>(frame_w) < size.w)
        size.w = static_cast<std::uint32_t>(frame_w);
    if (frame_h > 0 && static_cast<std::uint32_t>(frame_h) < size.h)
        size.h = static_cast<std::uint32_t>(frame_h);
    return size;
}

}  // namespace ns_present_follow
