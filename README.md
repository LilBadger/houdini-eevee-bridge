# EEVEE Bridge for Houdini

**Blender's EEVEE renderer, live in Houdini's Solaris viewport.**

EEVEE Bridge is a Hydra render delegate for Houdini 22 that renders your Solaris stage with Blender 5.2's EEVEE. It does not imitate EEVEE with custom shaders: a persistent, headless Blender process renders the scene with EEVEE itself, and the image appears in Houdini's viewport like any other Hydra renderer. Houdini stays the source of truth for geometry, lights, cameras, materials and render settings. You never open Blender or enable an add-on.

<p align="center">
  <img src="docs/demo.png" alt="Three spheres rendered by EEVEE in Houdini's Solaris viewport" width="720"><br>
  <sub>The bundled demo scene in Houdini's Solaris viewport. The blue sphere uses an EEVEE-only <i>Shader to RGB</i> toon material.</sub>
</p>

- **Live and responsive.** EEVEE renders on a background thread, so Houdini's interface never waits for Blender. While you tumble the camera or edit the scene, EEVEE draws quick reduced-resolution frames, then refines to full quality when you stop.
- **Works with your Solaris scene as it is.** It handles meshes, subdivision surfaces, instancing, curves and volumes, plus USD Preview Surface, MaterialX and Karma materials. Dome lights with HDRIs light the scene, and cameras render with depth of field.
- **A real viewport renderer.** Click-picking and selection highlighting work, and real depth keeps Houdini's handles and grid in place. You can run several EEVEE viewports at once. Render stats and status messages appear in Houdini.
- **Final frames too.** The *EEVEE Render Settings* LOP exposes EEVEE's settings. It renders stills, sequences, render passes and Cryptomatte through Houdini's USD Render ROP and husk, and can render to MPlay.

> **Status:** Runs on **Linux** and **Windows**. Linux x86-64 is tested with Houdini 22.0.368, Blender 5.2.0 LTS and NVIDIA GPUs under Vulkan. On Windows 11, the plugin builds with Visual Studio 2022 and passes the installer's checks, including an EEVEE render on the GPU. See [Limitations](#limitations).

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Using it](#using-it)
- [Performance](#performance)
- [Features](#features)
- [Limitations](#limitations)
- [Development](#development)
- [License](#license)

The full install manual is in **[INSTALL.md](INSTALL.md)**. It covers Windows, installer options, updates and rollback, render farms and troubleshooting. Release notes are in [CHANGELOG.md](CHANGELOG.md).

## How it works

```text
 Houdini                                                 Blender 5.2
 ┌───────────────┐   ┌───────────────────┐  scene edits  ┌───────────────┐
 │ Solaris stage │──>│ EEVEE Bridge      │──────────────>│ EEVEE worker  │
 └───────────────┘   │ Hydra delegate    │     binary    │ one scene per │
                     │ render thread     │<──────────────│ viewport      │
                     └─────────┬─────────┘     pixels    └───────────────┘
                               v           shared memory
                    Solaris viewport or husk
```

- **Only changes travel.** The C++ delegate turns Hydra prims into compact edits and sends only what changed, over an authenticated loopback connection. Arrays go as raw binary, and unchanged arrays are not sent again. Pixels come back through shared memory.
- **One worker, many viewports.** A single Blender worker serves every viewport in a Houdini session. Each viewport gets its own Blender scene, and compiled shaders, textures and identical materials are shared between them.
- **Disk renders are isolated.** Each disk render or husk job starts its own worker, and the worker exits when the job ends or is cancelled.

## Requirements

| | |
| --- | --- |
| **Houdini** | Any 22.0 build (tested: 22.0.368 and 22.0.429). Prebuilt plugins are included for those two builds; for another build the installer compiles it with the HDK that ships with Houdini. |
| **Blender** | 5.2 LTS. The installer checks for EEVEE and OpenVDB support, and adds NumPy and OpenEXR privately if Blender lacks them. |
| **GPU** | A Vulkan-capable GPU and driver (tested on NVIDIA). OpenGL is available as a fallback. |
| **OS** | Linux x86-64, or Windows 11 x86-64. |
| **Build tools** | Only for Houdini builds other than the prebuilt one's: the C++ compiler your Houdini HDK expects and CMake 3.22 or later. On Windows, the Visual Studio 2022 Build Tools with *Desktop development with C++* are enough; the installer uses their bundled CMake. |

## Quick start

**Linux, any Houdini 22.0 build.** With CMake and the compiler your Houdini HDK expects installed, run this in a clone of this repository or the extracted source zip; it compiles the plugin against your Houdini:

```bash
git clone https://github.com/badgerz42/houdini-eevee-bridge.git
cd houdini-eevee-bridge
bash install.sh
```

If a release includes a prebuilt `houdini-eevee-<version>-linux-x86_64.zip`, run `bash install.sh` in its extracted folder instead. It uses the prebuilt plugin on the Houdini build it names, and on other builds works like the Windows installer below.

**Windows, any Houdini 22.0 build.** Close Houdini, then download and run `houdini-eevee-0.7.4-windows-x86_64-setup.exe` from [Releases](https://github.com/badgerz42/houdini-eevee-bridge/releases). It installs for your user without administrator rights and removes older versions once the new one has passed its checks. Settings › Apps uninstalls it. Windows may warn that the publisher is unknown, because the setup is not code-signed; choose *More info › Run anyway*.

Alternatively, extract `houdini-eevee-0.7.4-windows-x86_64.zip` and run `install.cmd` in it; `uninstall.cmd` removes every installed version.

Prebuilt plugins are included for Houdini 22.0.368 and 22.0.429. On another 22.0 build, the installer compiles the plugin if the [Visual Studio 2022 Build Tools](https://visualstudio.microsoft.com/downloads/) with *Desktop development with C++* are installed. Without them, it tries the prebuilt plugin and registers it only if a test render through it in your Houdini succeeds. The same command works in the source zip or a clone of this repository.

If the installer cannot find Houdini or Blender, pass `--houdini PATH --blender PATH`. For example, on Linux: `--houdini /opt/hfs22.0.368 --blender /path/to/blender`.

The installer copies the bridge to `~/.local/share/houdini-eevee/<version>` (on Windows, `%LOCALAPPDATA%\HoudiniEEVEE\<version>`) and adds one package file to your Houdini `packages` folder. It then renders a small test image with EEVEE on your GPU. It needs no administrator rights and makes no changes to `houdini.env` or your Blender preferences.

Restart Houdini, open a Solaris network, and choose **EEVEE Bridge** from the viewport's renderer menu.

## Using it

1. In a Solaris viewport, pick **EEVEE Bridge** from the renderer menu. The first time, the Blender worker starts in the background. The status bar reports progress while shaders compile, and Houdini stays usable meanwhile.
2. Optionally, add an **EEVEE Render Settings** LOP at the end of your stage. It holds EEVEE's quality settings, the environment, render passes, color management and output. Use **Render to Disk**, **Render to Disk in Background** or **Render to MPlay** to render final frames.
3. Look through a stage camera to see its exact framing and depth of field.

### Viewport options

These appear with the renderer's settings in the viewport's display options:

| Option | Default | What it does |
| --- | --- | --- |
| EEVEE Samples | 16 | Viewport samples when the stage has no EEVEE Render Settings. If it has one, that node's *Viewport Samples* value is used. |
| EEVEE Ray Tracing | On | Viewport ray tracing when the stage has no EEVEE Render Settings. |
| Scene Up Axis | Y | The stage's up axis when there are no EEVEE Render Settings. EEVEE Render Settings reads it from the stage. |
| Navigation Resolution | Half | Resolution of the quick frames drawn while the camera or scene changes. *Full* turns reduced-resolution frames off. |
| Texture Size Limit | Full Resolution | Largest texture size uploaded to the GPU for the viewport. Lower it to save video memory. Disk renders always use full-resolution textures. |
| Subdivision Surfaces | Fast | *Fast* shows the subdivided cage, like Houdini's own viewport. *Exact* evaluates the true limit surface, as disk renders do, but takes much longer to build. |

### Depth of field

Depth of field comes from the USD camera. It turns on when the camera's **F-Stop** is above 0, and it uses the camera's focal length, focus distance and aperture aspect. It shows when you look through the camera and in disk renders. **Disable Camera Depth of Field** on EEVEE Render Settings turns it off.

## Performance

The benchmark is a production shot: 110 meshes (37 of them subdivided), 99 materials, 52 4K textures and 116,347 point instances. It renders at 1653×1078 with 64 viewport samples.

A headless Hydra harness (`probes/hydra_harness.cpp`) called the render pass the way Houdini's viewport does, 60 times a second, and timed how long each call blocked the calling thread. Houdini's viewport makes this call on its UI thread.

| | 0.5.1 | 0.6.1 |
| --- | --- | --- |
| Opening the shot | One 50.4 s call that freezes Houdini | The UI never blocks for more than 60 ms. First image after 2.7 s, all 64 samples after 8.3 s. |
| Uploading the scene | 16 s | 1.9 s |
| Orbiting the camera | Every frame blocks the UI (median 101 ms) | The UI stays at 60 Hz (longest call 0.3 ms), and new EEVEE frames arrive about 13 times per second. |
| Worker video memory | about 17 GB | about 7 GB, or 3.4 GB less with a 2048 texture limit |

All measurements were taken on an NVIDIA GeForce RTX 5090.

In this shot, EEVEE itself needs 60–70 ms to draw one reduced-resolution frame, mostly because Blender processes all 116k instances on every draw. That makes Blender, not the bridge, the limit on navigation speed here. On a lighter scene (`probes/bench_scene.py`), the harness receives about 38 new frames per second while orbiting.

`tools/gpu_benchmark.py` times each GPU in your machine and recommends one for the worker. The worker can run on a different GPU from Houdini's display at no extra transfer cost.

## Features

### Geometry
- **Meshes.** Supports authored normals, UVs, float and vector primvars, display color and opacity, left-handed orientation and degenerate faces. Display color can vary per point, face or corner.
- **Per-face materials.** GeomSubsets bound to different materials render with each subset's material.
- **Implicit shapes.** Sphere, Cube, Cone, Cylinder, Capsule and Plane prims are converted to meshes.
- **Subdivision.** Supports Catmull-Clark and bilinear subdivision with creases, corners, and boundary and face-varying interpolation rules. EEVEE Render Settings sets separate viewport and render levels, plus a face budget per mesh.
- **Instancing.** Supports native USD instancing and point instancers, of meshes, curves, points and volumes. Large instancers use Geometry Nodes with full affine matrices, so shear and negative scale work.
- **Per-instance primvars.** Point instancer primvars and primvars authored on native instances reach materials, display color and MaterialX `geompropvalue`. As in Karma, a value the prototype authors itself wins.
- **Curves.** Linear and cubic basis curves (B-spline, Bézier and Catmull-Rom, including pinned and periodic ones) become EEVEE hair curves with their widths, display color and primvars.
- **Particles.** USD Points become Blender point clouds, drawn as spheres sized by their widths, with display color and primvars.
- **Volumes.** OpenVDB and native Houdini volumes work, including live SOP volumes. The **EEVEE Volume Material** LOP controls density, color, absorption, anisotropy, emission, flame and temperature.

### Materials
- **Context priority.** Render contexts are used in this order: `eevee`, MaterialX (`mtlx`), Karma (`kma`), then USD Preview Surface.
- **USD Preview Surface.** Supports UV readers, 2D transforms, color, roughness, normal and displacement textures, UDIMs, wrap modes, and texture color spaces: raw, sRGB, or `auto`, which reads 8-bit images as sRGB and float images as linear. An unauthored wrap mode is black outside 0–1, as in Karma.
- **Displacement.** USD Preview Surface displacement and MaterialX displacement (height or vector) move the surface, with bump mapping for detail finer than the mesh.
- **MaterialX Standard Surface.** Maps to Blender's Principled BSDF, including base color, metalness, roughness, IOR, specular, coat, sheen, transmission, subsurface, emission, opacity and normals.
- **MaterialX nodes.** Supports images and UDIMs, texture coordinates and placement, math, mix, clamp, remap, color correction, channel operations, normal maps and bump.
- **Procedural noise.** MaterialX noises (2D/3D Noise, Fractal, Cell, Worley, Unified) and Karma Voronoi noise stay procedural in EEVEE, with no texture baking.
- **Karma extras.** Karma ramps sample Houdini's own ramp evaluator. Karma hair renders as a transmissive fiber approximation.
- **EEVEE shader graphs.** Graphs authored on the stage enable EEVEE-only looks such as toon shading with *Shader to RGB*.

Standard Surface and Principled BSDF are different shading models, so materials look close to Karma but not identical. Unsupported shader nodes render magenta and are named in the worker log. VEX and compiled Karma shaders cannot run in EEVEE; use MaterialX equivalents or baked textures instead.

### Lights and environment
- **Lights.** Rect, disk, sphere, distant and cylinder lights, with intensity, exposure, color temperature, normalization and diffuse and specular multipliers. Brightness matches Karma, including distant lights, whose intensity is irradiance when normalized and otherwise the radiance of the sun's disc.
- **Spot lights.** Sphere and disk lights with a UsdLux cone angle and softness become EEVEE spot lights.
- **Light and shadow linking.** A light's light-link and shadow-link collections decide which objects it lights and which cast its shadows.
- **IES profiles and light textures.** EEVEE cannot draw either, so both are approximated and named in the log: an IES profile becomes the spot cone that fits it, and a textured light (other than a dome) takes the texture's average color.
- **World.** Dome lights drive EEVEE's world: lat-long HDRIs, rotation, tint and exposure. Multiple domes add together. EEVEE Render Settings can override the environment with an HDRI or a color, or turn it off.

### Cameras and motion
- **Cameras.** Perspective and orthographic cameras with lens shift, pixel aspect and clipping. Depth of field works in the viewport and in renders.
- **Motion blur.** Final renders blur camera, object, instance and deformation motion from USD time samples, or from point velocities and accelerations.

### Output
- **Final frames.** Stills and frame ranges render through Houdini's USD Render ROP, using a separate worker for each job. Output can be PNG, EXR or other formats Blender can write.
- **Render passes.** Depth, mist, normal, position, vector, diffuse and specular light and color, volume light, emission, environment, shadow, AO and transparency are available. Shader AOVs and Cryptomatte are written to a multilayer EXR or a `.passes.exr` file next to the image.
- **Viewport passes.** The viewport can display EEVEE's preview passes.
- **MPlay.** Renders beauty plus the standard passes to MPlay.

### Viewport
- **Background rendering.** Houdini's UI never waits for EEVEE. Navigation frames use reduced resolution, and refinement takes at most one intermediate step, because EEVEE restarts accumulation whenever the sample count changes.
- **Instant feedback.** A solid preview appears while a new session compiles its shaders. Progress and errors appear in the status bar and render stats.
- **Picking and depth.** A flat ID pass on settled frames supplies prim and instance IDs and window-space depth.
- **Viewport controls.** Multiple viewports, pause and resume, and automatic reconnection to a restarted worker.
- **Texture size limit.** Textures larger than 2048 pixels are scaled down on the GPU, keeping their aspect ratio; your files are not changed. A 4K texture then needs a quarter of the video memory. **EEVEE Render Settings › Textures** sets the size (8192 to 512) and whether it applies to the viewport (on by default) and to final renders (off by default). Without that node, the viewport uses `HDEEVEE_TEXTURE_LIMIT`, or 2048. Float textures (EXR, 16-bit PNG or TIFF) are kept in half precision, which halves their memory.
- **Video memory is freed when you switch away.** Half a second after no LOP viewport shows EEVEE, for example when you switch to Karma XPU or Houdini VK, the Blender worker exits and returns all its video memory. With no viewport on the LOP network at all, it waits 30 seconds. It starts again when a viewport uses EEVEE, and the scene is sent again, and its shaders recompiled, with the next change. `HDEEVEE_IDLE_EXIT_SECONDS` and `HDEEVEE_OFFSCREEN_EXIT_SECONDS` set the delays; `0` keeps the worker running.
- **Houdini's own viewport memory is freed when you switch to EEVEE.** Houdini VK keeps the whole scene in video memory behind EEVEE. On a switch from Houdini VK or GL to EEVEE, the bridge empties Houdini's viewport scene first, which returned 8.5 GB in a production scene. It marks the scene as modified; `HDEEVEE_RELEASE_NATIVE_VIEWPORT=0` turns it off.

## Limitations

- **Windows.** On Windows 11 the plugin builds and passes the installer's checks, but interactive use in a Houdini viewport on Windows hasn't been confirmed yet.
- **Houdini version.** Any Houdini 22.0 build works, but the prebuilt plugin is compiled for 22.0.368. On another build without a compiler, the installer keeps the prebuilt plugin only after a test render in that build succeeds; if anything then misbehaves, install the build tools and run the installer again, so it compiles the plugin for your build.
- **Material fidelity.** Karma and MaterialX materials are approximated with Blender's BSDFs. VEX shaders are not supported.
- **Picking.** Point instances drawn through Geometry Nodes pick as their prototype prim. Face and point picking is not provided.
- **Not translated yet.** Probe baking and arbitrary world shader graphs.
- **Particle shape.** EEVEE draws point clouds as low-polygon spheres, so very large particles look faceted.
- **Heavy instancing.** Very large instance counts limit EEVEE's own frame rate, because Blender processes every instance on each draw.
- **Displacement detail.** EEVEE does not dice surfaces the way Karma does, so displacement detail is limited by the mesh's density or subdivision level. Bump mapping adds the finer detail.
- **Light shapes.** IES profiles and light textures are approximated (see [Lights and environment](#lights-and-environment)). Cones on rect and cylinder lights are ignored, and a cylinder light is drawn as a rect light.
- **Unshaded prims.** Prims without a material use their display color with a plain Principled BSDF. Karma's own fallback material is darker.
- **Several viewports share one worker.** Each viewport has its own Blender scene, but they take turns on the GPU. On one GPU, a worker per viewport was slower overall (two viewports: about 58 instead of 80 combined frames per second) and needs about 1 GB more VRAM each, so it isn't offered.

## Development

Build the plugin with CMake. The installer's `--build` option runs the same steps.

```bash
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DHoudini_DIR=/opt/hfs22.0/toolkit/cmake
cmake --build build
```

| Path | Contents |
| --- | --- |
| `src/` | The Hydra delegate. `delegate.cpp` holds registration and the render pass, `renderer.cpp` the background renderer and `prims.cpp` the prims. The rest is scene state, the protocol and render buffers. |
| `worker/` | The Blender worker. `eevee_worker.py` is the server and `session.py` the per-viewport scene. Other modules handle meshes, materials, lights, volumes, passes and picking. |
| `houdini/` | Package contents: renderer registration, viewport options, HDAs and the startup hook. |
| `tools/` | Installer runtime, worker supervision, husk wrapper, diagnostics, GPU benchmark, packaging and a demo scene. |
| `probes/` | A headless Hydra harness and profiling tools. See [probes/README.md](probes/README.md). |

After a build, `python3 tools/launch_houdini.py --demo` starts Houdini with the bridge from this folder and creates the demo scene shown above. `python3 tools/package_release.py --native-root build` builds a release archive.

Bug reports are welcome. Please include your Houdini and Blender versions, your GPU, and the worker log from `~/.cache/houdini-eevee/logs/` (see [Troubleshooting](INSTALL.md#troubleshooting)).

## License

This project is released under the [MIT License](LICENSE). Bundled third-party files keep their own licenses:

- [nlohmann/json](https://github.com/nlohmann/json) ([MIT](third_party/nlohmann/LICENSE.MIT))
- MaterialX standard-library definitions in `worker/materialx_nodes.json` ([modified Apache 2.0](third_party/materialx/LICENSE.txt))

Blender and Houdini are not included and must be installed separately. Blender and EEVEE are trademarks of the Blender Foundation, and Houdini is a trademark of Side Effects Software. This project is not affiliated with or endorsed by either.
