# Changelog

## 0.7.1

### Installer

- **Any Houdini 22.0 build.** The installer no longer requires the build the prebuilt plugin was compiled for (22.0.368). On another 22.0 build it compiles the plugin itself when a C++ compiler is installed. On Windows the Visual Studio 2022 Build Tools are enough: the CMake bundled with them is found automatically, with no Developer Command Prompt. Without a compiler, or if the build fails, it uses the prebuilt plugin only if a test render through it in that Houdini succeeds. `--no-build` never builds.
- **Several Houdini 22.0 builds.** The installer uses the one in `HFS`, or else the newest installed build, instead of stopping. `--houdini` still picks one.
- **A real render test.** The install check and `--doctor` render a small scene with `husk` through the EEVEE plugin, with other installed Houdini packages ignored. Before, they only listed renderers, which another registered EEVEE install could satisfy.
- Windows builds no longer print hundreds of C++20 deprecation warnings from Houdini's USD headers.

## 0.7.0

### Scene compatibility

- **Particles.** USD Points render as Blender point clouds, sized by their widths (diameters), with display color and primvars. They were not rendered before.
- **Implicit shapes.** Sphere, Cube, Cone, Cylinder, Capsule and Plane prims are converted to meshes by USD's implicit-surface scene index. They were not rendered before.
- **Cubic curves.** B-spline, Bézier and Catmull-Rom curves render with their exact shape, including pinned and periodic wrap. B-splines become uniform cubic NURBS; Bézier and Catmull-Rom curves become Blender Bézier curves with equivalent handles.
- **Curve thickness.** Curves render as round tubes of their widths (Blender's *Cylinder* curve shape), as in Karma. Blender's default *Strand* shape draws thin lines that ignore the width. EEVEE Render Settings › Curves Shape still offers Strand, which is lighter for very dense grooms, and Strip.
- **Curve and point primvars.** Display color and float, float2 and float3 primvars on curves and points become attributes, like on meshes.
- **Per-face materials.** GeomSubsets bound to different materials now render with each subset's material. Houdini's Hydra provides subsets as child prims of the mesh.
- **Display color.** A display color that varies per point, face or corner is shown in full; before, only its first value was used.
- **Bilinear subdivision** meshes, such as converted cubes, are now shaded flat instead of smooth.
- **Texture color spaces.** USD Preview Surface textures with `sourceColorSpace` `auto` read float images such as EXR as linear; before, every non-raw texture was decoded as sRGB, which darkened linear textures. An asset's color-space metadata is honored, and UDIMs work.
- **Texture wrap.** A USD Preview Surface texture without an authored wrap mode is black outside 0–1, as in Karma, instead of repeating.
- **Instanced curves, points and volumes.** Point instancers and native instances of curves, particles and volumes render; before, only meshes could be instanced.
- **Per-instance primvars.** Point instancer primvars (per-instance color, for example) and primvars authored on native instances reach materials, display color and MaterialX `geompropvalue`. As in Karma, a value authored on the prototype itself wins.
- **MaterialX `geompropvalue` on curves and points** reads their primvars instead of always returning the default.

### Lights and materials

- **Distant lights match Karma's brightness.** A normalized distant light's intensity is irradiance, as in Karma; before, it was multiplied by 4, so Houdini's Distant Light LOP (normalized by default) rendered four times brighter than in Karma. An unnormalized distant light's intensity is the radiance of the sun's disc, as in USD and Karma. **Scenes lit by distant lights render darker than in 0.6.1**; other lights are unchanged and were already within about 10% of Karma (cylinder lights, drawn as rect lights, differ more).
- **Light and shadow linking.** A light's light-link collection limits the objects it lights, and its shadow-link collection the objects that cast its shadows.
- **Spot lights.** Sphere and disk lights with a UsdLux cone angle and softness become EEVEE spot lights.
- **IES profiles and light textures are approximated.** EEVEE cannot draw them. An IES profile becomes the spot cone that fits its beam, and a textured rect, disk, sphere or cylinder light takes the texture's average color. Each approximation is named in the log.
- **Displacement.** USD Preview Surface displacement and MaterialX displacement (height or vector) move the surface, with bump mapping for detail finer than the mesh. EEVEE does not dice surfaces, so detail depends on the mesh density or subdivision level.
- **Default volume shading.** Volumes without a material are white, or tinted by their display color, as in Karma; before, they were Blender's mid gray.

### Performance

- **Large meshes load about 3× faster.** A 2.25-million-quad mesh went from 2.7 s to 0.9 s. Flat shading and point positions are now written as whole arrays; Blender's `shade_flat()` and `MeshVertex.co` loop per element. Deforming meshes update their points about 100× faster.
- **Volumes are exported once per geometry change.** A live SOP volume used to be written to a new VDB for each of its fields (density, temperature, flame, velocity) on every sync. It is now exported once and shared, and an unchanged volume keeps its file, so the worker reuses its composed volume without reloading.
- **Automatic cleanup.** Session folders nothing has used for 7 days, and logs older than 14 days, are removed when a worker starts (`HDEEVEE_KEEP_SESSION_DAYS`, `HDEEVEE_KEEP_LOG_DAYS`).
- **Development tools.** `probes/vram_profile.py` works on Windows, where per-process GPU memory is not reported, and the Hydra harness uses 1 ms timer resolution on Windows so it simulates a 60 Hz viewport.

## 0.6.1

### Windows

- The plugin now links Houdini's Python libraries, which the MSVC linker requires. Windows 11 builds with Visual Studio 2022 and passes the installer's checks, including an EEVEE render on the GPU.
- Prebuilt Windows package for Houdini 22.0.368: `houdini-eevee-0.6.1-windows-x86_64.zip`, built with MSVC 19.42 to match Houdini.
- The render buttons on **EEVEE Render Settings** work. The USD Render ROP reads backslashes in its render command as escape characters, so every render failed with `CreateProcess failed`.
- Rendering to MPlay no longer waits for the MPlay window to be closed after each frame.

### Final renders

- A frame range renders in one husk process with one EEVEE worker, instead of starting Blender and compiling every shader again for each frame. Three frames of a small test scene went from 16.7 s to 6.1 s; the saving grows with the number of materials.
- **Output Color → View** defaults to **ACES 2.0** instead of AgX, and lists Blender's views in a menu. On the sRGB display it is numerically identical to Houdini 22's default *sRGB - Display / ACES 2.0 - SDR 100 nits (Rec.709)* view, so PNG, JPEG and TIFF files now look like Houdini's viewport and MPlay. EXR files, the viewport and MPlay are unchanged: they stay scene-linear in Linear Rec.709, which is also Houdini's working space.

### Video memory and first draw

Measured on a production shot (1653×1078, 110 meshes of which 37 subdivided, 99 materials, 52 4K textures, 116,347 point instances):

- **CPU subdivision in the viewport.** Blender's GPU (OpenSubdiv) subdivision kept about 10 GB of evaluation buffers for this shot; the CPU result needs under 1 GB, with no measurable difference in draw speed for static meshes. Worker VRAM went from 16.8 GB to 7.2 GB. `install.py --gpu-subdivision` (or `HDEEVEE_GPU_SUBDIVISION=1`) restores GPU subdivision, which is faster for heavily deforming subdivided meshes.
- **Fast subdivision surfaces in the viewport.** Blender's exact limit-surface evaluation made the first viewport draw take 30 s. The viewport now shows the refined cage at the viewport subdivision level (as Houdini's GL viewport does): first image after 2.7 s, fully converged after 8.3 s instead of 36 s. **Subdivision Surfaces → Exact (Limit Surface)** in the viewport's EEVEE options restores the exact surface; disk renders always use it.
- **Texture Size Limit** viewport option (Full, 8192, 4096, 2048, 1024). A 2048 limit saved 3.4 GB on this shot. Disk renders always use full-resolution textures.
- The half-resolution navigation EEVEE instance (about 1 GB fixed cost per EEVEE instance, mostly shadow pool) is released 60 s after navigation stops (`HDEEVEE_IDLE_TARGET_SECONDS`). Idle preview and viewport-pass targets are released too. A small follow-up draw makes Blender's Vulkan backend actually return the memory.

### Depth of field

- Camera depth of field now works in the viewport. EEVEE applies a camera's depth of field only when viewing through that camera; the worker now draws in camera mode when the Stage camera has an F-Stop above 0 (framing is unchanged).
- The aperture uses the camera's real focal length, so blur size follows the USD camera (focal length, F-Stop, focus distance and DOF aspect); disk renders use the same values. **Disable Camera Depth of Field** on EEVEE Render Settings still turns it off.

### Installer

- Every replaced package registration is kept as `houdini_eevee.json.<version>`; `install.py --rollback VERSION` restores a specific one (`--rollback` alone restores the most recent).

## 0.6.0

This release reworks how Houdini and Blender exchange data and how the viewport schedules EEVEE frames. Scene translation (materials, lights, volumes, passes, disk rendering) is unchanged unless listed.

### Houdini stays responsive

- The render delegate draws on a background thread. Houdini's render-pass call only posts the current view and shows the newest finished frame; it no longer waits for Blender. A slow first shader compile, a heavy scene upload or a long high-sample draw no longer freezes the Houdini UI.
- While the camera or scene changes, EEVEE draws one sample at reduced resolution (**Navigation Resolution** in the viewport's EEVEE display options: Full, Half, Third or Quarter; default Half), upscaled in Houdini. When the view is still, it refines at full resolution to the Viewport Samples target.
- EEVEE restarts its accumulation whenever the sample count changes, so the old 1 → 4 → 16 → target sequence repeated work. Refinement now uses at most one intermediate step, sized from the measured per-sample cost, before the final target. Settled views are not redrawn.
- A solid Workbench preview is shown while a new session's EEVEE shaders compile. Houdini's status bar reports compilation and errors.
- Houdini's viewport pause button pauses EEVEE.

### Faster data exchange

- Protocol 2: arrays (points, topology, normals, UVs, primvars, instance transforms) travel as raw binary blobs instead of JSON text. Blender builds meshes, attributes, UV maps and custom normals with NumPy and `foreach_set`; custom normals use Blender 5's float attribute instead of `normals_split_custom_set`.
- Unchanged arrays are recognised by content hash and not sent again, even when Hydra marks them dirty. Instance transforms are recomputed only when the instancer or transform changes.
- Shutter motion samples, velocities and accelerations are sent only for final renders with motion blur enabled; the viewport uses EEVEE's own motion history.
- The delegate's scene snapshot shares array storage with pending edits instead of holding a second JSON copy.
- Pixels return through a per-connection shared-memory segment instead of the socket. Blender reads straight into it; Houdini's buffers share the frame's storage. Color is delivered as 16-bit float (EEVEE's own precision), converted with F16C on the render thread. `HDEEVEE_COLOR_FORMAT=float` restores 32-bit color.

### Picking, depth and multiple viewports

- Viewport picking and selection highlighting: prim and instance IDs come from a flat Workbench ID pass (`needsselection` is enabled). Instances drawn as separate objects report their instance index; Geometry Nodes point instances resolve to their prototype prim.
- Depth is now real window-space depth from the same pass. The previous release sent an all-zero depth buffer.
- The ID/depth pass runs on settled frames, not on reduced-resolution navigation frames.
- Several viewports (or other render delegates) share one worker without replacing each other's scenes: each connection has its own Blender scene. Compiled shaders, textures and reusable materials are shared.

### Robustness

- A prim that fails to translate is reported (Houdini console, status, `scene_errors` stat) instead of aborting the whole scene update and forcing a full resend loop. Placeholder normals are ignored rather than rejected.
- Materials are kept by translated content when a renderer restarts, a scene resets or another viewport asks for the same material, so shaders need not be rebuilt.
- The delegate waits for a starting worker and reconnects to a replacement worker, replaying its scene; Houdini no longer restarts the renderer (and re-uploads the scene) after the worker starts.
- The worker removes its shared memory on termination and cleans up segments left by crashed workers.
- Per-frame whole-scene checks (subdivision, instance batching, render settings, environment) run only when something changed.
- A render settings prim that becomes inactive no longer stays active.

### Tools and project

- `tools/gpu_benchmark.py` compares Vulkan GPUs with a synthetic scene and recommends `--gpu-device`.
- `install.py --rollback` restores the package registration that the last install replaced.
- `probes/hydra_harness.cpp` (target `hydra_harness`) drives a delegate headlessly like a viewport and reports UI blocking, presented frames, picking and depth; `probes/run_harness.py` runs it with a fresh worker. `probes/replay_profile.py` replays a recorded scene (`HDEEVEE_DUMP_SCENE`) inside Blender to profile EEVEE features.
- The native plugin is split into protocol, scene, prim, renderer, buffer and delegate sources.
