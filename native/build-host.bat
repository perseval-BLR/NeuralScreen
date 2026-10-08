@echo off
rem dlss5-feed-host64.exe -- the 64-bit NGX host (desktop-nr live build).
cd /d "%~dp0"
setlocal
call "%~dp0vcvars.bat" || exit /b 1
rem The module the NGX calls leave from. Its FILE NAME is what the feature
rem library checks - see ns_forwarder.cpp. Built first: without it the worker
rem falls back to calling the library itself, which only passes while the
rem worker is named nvngx.dll.
cl /nologo /O2 /EHsc /W3 /MD /std:c++17 /Iinclude /LD ns_forwarder.cpp ^
   /Fe:nvngx.dll_ns-forwarder.dll ^
   /link kernel32.lib d3d12.lib
if errorlevel 1 exit /b 1

rem The worker needs the flag and the launcher does not: it is the only target
rem that pulls in C++/WinRT (Windows Graphics Capture), and the SDK's
rem winrt/base.h reaches for <experimental/coroutine> whenever
rem __cpp_lib_coroutine is unset - which it is under /std:c++17, the standard
rem every build line here uses. MSVC 14.51 - the toolchain this bench has -
rem turned that header into a hard error (STL1011); 14.44, which the project
rem was written against, only warned. Older MSVC accepts the define and says
rem nothing, so the line is correct on both. Do not move the build to C++20
rem as a way out: it changes the goto rules and dlss5-feed-host64.cpp fails
rem with three C2362 errors on `goto fail_capture`.
cl /nologo /O2 /EHsc /W3 /MD /std:c++17 /D_SILENCE_EXPERIMENTAL_COROUTINE_DEPRECATION_WARNINGS /Iinclude /Isrc dlss5-feed-host64.cpp spout_bridge.cpp gpu_recorder.cpp ^
   /Fe:nvngx.dll.build ^
   /link lib\Windows_x86_64\x64\nvsdk_ngx_d.lib SpoutDX.lib version.lib kernel32.lib user32.lib gdi32.lib advapi32.lib ole32.lib d3d11.lib d3d12.lib dxgi.lib d3dcompiler.lib WindowsApp.lib dwmapi.lib mfplat.lib mfreadwrite.lib mfuuid.lib
if errorlevel 1 (
    del nvngx.dll.build nvngx.dll.build.manifest >nul 2>&1
    exit /b 1
)
rem Linked to a temporary name and moved into place only after the link
rem succeeds: MSVC's linker deletes its output when a link fails (one
rem unresolved symbol and the working nvngx.dll was gone, measured
rem 2026-10-08) - the same trap build-clang.bat closed for lld-link.
move /y nvngx.dll.build nvngx.dll >nul 2>&1
if exist nvngx.dll.build (
    echo Could not replace nvngx.dll. If NeuralScreen is running, close it and build again.
    del nvngx.dll.build nvngx.dll.build.manifest >nul 2>&1
    exit /b 1
)
move /y nvngx.dll.build.manifest nvngx.dll.manifest >nul 2>&1
endlocal
echo host built.
