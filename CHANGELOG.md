# Changelog

## 0.7.4

- **Switching from Houdini VK to EEVEE frees Houdini's viewport memory.** Houdini keeps its own viewport renderer alive behind EEVEE with the whole scene loaded, textures at full size, and ignores scene changes while it is hidden. In a production scene that was 10.5 GB of video memory plus 6 GB spilled into system memory, which left EEVEE under 1 GB of real video memory and could crash Houdini. When a viewport switches from Houdini VK or GL to EEVEE, the bridge now shows Houdini's renderer an empty LOP network for a moment (a temporary node in `/obj`), so it drops the scene, then switches to EEVEE on the viewport's network: Houdini went from 10.5 GB to 2 GB and EEVEE loaded entirely into video memory. Switching back to Houdini VK loads its scene again as usual. The temporary node marks the scene as modified. `HDEEVEE_RELEASE_NATIVE_VIEWPORT=0` turns this off.
- **Switching from EEVEE to another renderer stops the worker at once**, from Houdini's renderer-changed viewer event, before Houdini VK loads the scene. Both used to hold the scene until Houdini VK finished loading, which could take half a minute and exhaust video memory.
- **Switching to any other renderer frees EEVEE's video memory, within a second.** 0.7.2 freed it only when Houdini deleted the EEVEE renderer, as it does when switching to Karma. Switching to Houdini VK or GL keeps EEVEE's renderer alive in the background, so the Blender worker kept the whole scene in video memory. Houdini now stops the worker half a second after no LOP viewport shows EEVEE, whichever renderer it switched to. In a test scene the worker's 3.1 GB was released in under a second when switching to Karma XPU or Houdini VK, and Karma XPU's memory was released when switching back to EEVEE.
- **Leaving the LOP network** (a viewport showing SOPs, say) stops the worker after 30 seconds, so moving between networks does not restart it each time. `HDEEVEE_OFFSCREEN_EXIT_SECONDS` sets this delay, and `0` keeps the worker running.
- **Switching back to EEVEE** shows the last EEVEE image at once. The scene is sent to a new worker with the next change, such as moving the camera.
- The worker exits as soon as it is no longer used instead of after Blender releases each resource, and Houdini asks its own GPU caches (viewport, OpenCL, Karma XPU) to release unneeded memory before a new worker starts.
- `HDEEVEE_IDLE_EXIT_SECONDS` now defaults to 0.5 seconds; `0` still keeps the worker running.
- **Changing the texture size limit frees the memory again.** Once the viewport had been navigated, EEVEE's navigation render target kept the previous textures alive, so lowering the limit freed little and raising it again added more memory each time. The worker now frees its render targets when the limit changes. In a production scene (54 textures), 2048 to 512 now frees 640 MB each time and 512 to 2048 adds it back; before, memory grew with every change.

## 0.7.3

### Windows installer

- **Prebuilt plugins for Houdini 22.0.368 and 22.0.429.** The Windows package and setup carry a plugin for each (the second in `native/22.0.429`), so both install without a compiler or a test-render fallback. `tools/package_release.py --native-root` can be repeated for more builds.
- **No internet access needed.** The OpenEXR module for Blender's Python ships in the package (`wheels/`) instead of being downloaded from PyPI during the install (`package_release.py --wheels`).
- **Houdini and Blender outside their default folders.** The setup, `install.cmd` and `install.py` also find Houdini 22.0 through the folders its installer registers, and `HFS`. `install.py` also finds Blender 5.2 through its installer's registration and Steam, and picks a 5.2 install when there are several instead of stopping.
- **Machines where husk has no license.** The install check's test render uses husk. Without a husk license, a plugin compiled for this exact Houdini build is installed without that render; one compiled for another build still has to pass it.
- **Full installer log.** The setup passes `HDEEVEE_INSTALL_LOG`, so everything the installer prints, including compiler and pip output, is also saved to `%TEMP%\houdini-eevee-install.log`.

## 0.7.2

- **Texture size limit, 2048 by default.** Textures larger than the limit are scaled down on the GPU, keeping their aspect ratio; the files are not changed. A 4K texture at 2048 needs a quarter of the video memory (eight 4K textures in a final render: 744 MB full size, 264 MB at 2048). **EEVEE Render Settings › Textures** sets the size and applies it to the viewport (on by default) and final renders (off by default). Without that node, viewports use `HDEEVEE_TEXTURE_LIMIT` (a studio default), or 2048. The viewport's own *Texture Size Limit* setting also gains *Full*.
- **Float textures in half precision.** EXR and 16-bit PNG or TIFF textures are kept as 16-bit floats on the GPU instead of 32-bit, which halves their memory (eight 4K EXRs: 2.8 GB to 1.4 GB, and 0.4 GB with the 2048 limit). Dome light HDRIs keep 32-bit so bright suns are not clipped. `HDEEVEE_FULL_PRECISION_TEXTURES=1` turns this off.
- **Windows setup program.** `houdini-eevee-<version>-windows-x86_64-setup.exe` installs for the current user without administrator rights, shows the installer's progress, removes older versions after the new one passes its checks, and adds an uninstall entry to Settings › Apps. Built with NSIS by `tools/package_release.py --setup`. It runs `install.py` with Houdini's bundled Python and starts no PowerShell, which endpoint security tools such as Bitdefender block. If it stops, even in a silent (`/S`) install, the reason is in `%TEMP%\houdini-eevee-setup.log`.
- **`install.cmd` and `uninstall.cmd` no longer use PowerShell.** They run `install.py` with Houdini's bundled Python directly, for the same reason.
- **Uninstall scripts.** `uninstall.cmd` and `uninstall.sh` (`install.py --uninstall-all`) remove every installed version, the package registration and its backups, logs and caches, after listing them. Only folders written by the installer are removed. The installer gains `--remove-old` to delete previous versions after a successful install, and `--reinstall` to replace the same version.
- **Switching away from EEVEE frees its video memory.** The Blender worker used to keep the scene's textures, compiled shaders and its EEVEE instance in video memory for the rest of the Houdini session, even with no EEVEE viewport open. Switching the viewport to Karma XPU could then run out of video memory and crash Houdini. Now the worker exits 2 seconds after the last EEVEE viewport closes, which returns all of its memory: 2.4 GB for the benchmark scene, within 3 seconds. It starts again when a viewport uses EEVEE, which costs a few seconds plus the scene's shader compilation. `HDEEVEE_IDLE_EXIT_SECONDS` sets the delay, and `0` keeps the worker running. A worker that exits this way does not count toward the three automatic restarts after crashes.

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
