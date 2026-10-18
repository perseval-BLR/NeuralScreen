# Third-party notices

NeuralScreen is distributed with third-party components. This file is a
notice and attribution index; the corresponding license texts shipped with
the components remain controlling.

## NVIDIA components

The package currently contains two different classes of NVIDIA software:

- `nvngx_dlssg.dll` 310.9.16.0 is an NVIDIA-signed public DLSS-G
  redistributable and remains subject to NVIDIA's applicable terms.
- `nvngx_dlssnr.dll` 310.8.0 is a leaked, non-public DLSS-NR build. The project
  has not documented a grant that permits redistributing this file. Its
  presence in an archive, an attribution notice, or a research-only label does
  not create that permission. This remains an unresolved distribution risk.

NGX headers and import libraries remain subject to their SDK terms. All NVIDIA
files remain NVIDIA property. NeuralScreen is independent and is not affiliated
with or endorsed by NVIDIA.

## Spout2

The package contains Spout2 runtime libraries and integration code.

Copyright (c) 2014-2025, Lynn Jarvis. All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice,
   this list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.

## DLSS5-Feeder

Parts of the native worker (`native/dlss5-feed-host64.cpp`) and the feed IPC
header (`native/src/feed_ipc.h`) derive from DLSS5-Feeder by Jean-Laurent
ROUZIES, used under the MIT License. Upstream project:
https://github.com/jlrouzies-fr/DLSS5-Feeder

MIT License

Copyright (c) 2026 Jean-Laurent ROUZIES

Portions of DLSS5-Feeder are derived from dlss5-dx11-bridge, Copyright (c) 2026
NIGos (MIT).

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Python and bundled Python packages

The package contains CPython and selected runtime files from these projects:

- PyAV / FFmpeg
- NumPy
- OpenCV
- Pillow
- pygame
- DXcam
- MSS
- pystray
- comtypes
- colorama, packaging, pywin32-ctypes, six and typing-extensions

These components remain under their respective licenses. Package metadata and
license files included in the bundled runtime are preserved in the release
where supplied upstream. FFmpeg licensing depends on the options used to build
the bundled libraries; recipients should consult the bundled PyAV/FFmpeg
license materials before redistribution.

## Fonts

IBM Plex fonts are distributed under the SIL Open Font License 1.1. The full
license text is included as `fonts/OFL.txt`.

## No relicensing

Nothing in the NeuralScreen license grants additional rights to third-party
components. Names and trademarks belong to their respective owners.
