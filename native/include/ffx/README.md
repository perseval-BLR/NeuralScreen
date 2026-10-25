# AMD FSR SDK headers (vendored)

Only the headers the Frame Generation backend (`frame_generation_fsr.inl`)
compiles against, copied unchanged from the AMD FSR SDK 2.3.0
(https://github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK, tag `v2.3.0`,
MIT licence - see `LICENSE-AMD-FSR-SDK.md`):

- `api/include/ffx_api.h`, `ffx_api_types.h`, `dx12/ffx_api_dx12.h`
- `framegeneration/include/ffx_framegeneration.h`, `ffx_framegeneration_api_types.h`

The directory layout is kept because the SDK headers include each other by
relative path. No SDK source is compiled and nothing is linked: the runtime
(`amd_fidelityfx_loader_dx12.dll`, `amd_fidelityfx_framegeneration_dx12.dll`)
is loaded with `LoadLibrary` and its exports are resolved with
`GetProcAddress`.
