# Installing EEVEE Bridge

The installer sets up EEVEE Bridge as an ordinary per-user Houdini package. It does not modify your Houdini or Blender installations, `houdini.env`, or Blender's preferences, and it needs no administrator rights.

- [1. Before you start](#1-before-you-start)
- [2. Install on Linux](#2-install-on-linux)
- [3. Install on Windows](#3-install-on-windows)
- [4. What the installer does](#4-what-the-installer-does)
- [5. First run in Houdini](#5-first-run-in-houdini)
- [6. Check an installation](#6-check-an-installation)
- [7. Installer options](#7-installer-options)
- [8. Choosing the GPU](#8-choosing-the-gpu)
- [9. Update, roll back, uninstall](#9-update-roll-back-uninstall)
- [10. Render farms](#10-render-farms)
- [11. Environment variables](#11-environment-variables)
- [Troubleshooting](#troubleshooting)

## 1. Before you start

You need:

| | |
| --- | --- |
| **Houdini 22.0** | Tested with 22.0.368. The installer reads Houdini's HDK (the `toolkit` folder), which a standard Houdini installation includes. |
| **Blender 5.2 LTS** | Tested with 5.2.0 LTS. It must include OpenVDB, as official builds do. |
| **GPU driver** | A Vulkan-capable driver (tested with NVIDIA). |
| **Python 3.10+** | To run the installer. On Linux, the system `python3` is enough. On Windows, `install.cmd` uses the Python that ships with Houdini. |
| **Build tools** | Only when building from source: CMake 3.22 or later, plus the compiler your Houdini build expects. That is GCC on Linux and Visual Studio 2022 Build Tools on Windows (tested: MSVC 19.44). On Linux, Ninja or Make works. |

Blender needs **NumPy** and the **OpenEXR** Python module. If they are missing, the installer downloads them from PyPI into the bridge's own folder, leaving Blender untouched. This step needs internet access.

OpenEXR currently publishes wheels up to Python 3.13. Some Linux distributions build Blender with a newer Python. In that case Blender must already provide OpenEXR, Imath and NumPy, or you can point `HDEEVEE_PYTHON_DEPS` at a folder that contains them.

## 2. Install on Linux

### From the prebuilt release (any Houdini 22.0 build)

1. Download `houdini-eevee-0.7.2-linux-x86_64.zip` from the [releases page](https://github.com/badgerz42/houdini-eevee-bridge/releases).
2. Extract it and run the installer:

   ```bash
   unzip houdini-eevee-0.7.2-linux-x86_64.zip
   cd houdini-eevee-0.7.2-linux-x86_64
   bash install.sh
   ```

3. Restart Houdini.

The prebuilt plugin is compiled for **Houdini 22.0.368**. On another Houdini 22.0 build, the installer compiles the plugin itself when CMake and the compiler your HDK expects are installed; otherwise it keeps the prebuilt plugin only if a test render with it in your Houdini succeeds. If the prebuilt binary does not load on your distribution, for example because of an older system C++ library, `--build` compiles it even for 22.0.368.

### From source (any Houdini 22.0 build)

```bash
git clone https://github.com/badgerz42/houdini-eevee-bridge.git
cd houdini-eevee-bridge
bash install.sh
```

With no prebuilt plugin in the folder, the installer compiles one. You can also run `install.sh` from the extracted source archive, `houdini-eevee-0.7.2-source.zip`.

### When Houdini or Blender are not found

The installer uses the Houdini in `$HFS`, or else the newest build in `/opt/hfs22.0.*` and `~/houdini-22.0.*`. It looks for Blender on your `PATH`. To choose, or if it finds none, name them:

```bash
bash install.sh --houdini /opt/hfs22.0.368 --blender /opt/blender-5.2/blender
```

## 3. Install on Windows

> Tested on Windows 11 with Houdini 22.0.368, Blender 5.2.0 LTS and Visual Studio 2022 Build Tools (MSVC 19.44). The plugin builds, and the doctor passes, including an EEVEE render on the GPU. Interactive use in a Houdini viewport on Windows hasn't been confirmed yet, so please report what you find.

### From the prebuilt release (any Houdini 22.0 build)

1. Download `houdini-eevee-0.7.2-windows-x86_64.zip` from the [releases page](https://github.com/badgerz42/houdini-eevee-bridge/releases).
2. Extract it, and in the extracted folder run:

   ```bat
   install.cmd
   ```

3. Restart Houdini.

The prebuilt plugin is compiled for **Houdini 22.0.368** with MSVC 19.42, the same compiler as that Houdini, so on 22.0.368 it needs no compiler or extra Visual C++ runtime. On another Houdini 22.0 build, the installer:

- compiles the plugin if the [Visual Studio 2022 Build Tools](https://visualstudio.microsoft.com/downloads/) with the *Desktop development with C++* workload are installed. It uses the CMake that comes with them and needs no Developer Command Prompt. This takes a few minutes.
- otherwise tries the prebuilt plugin, and registers it only if a test render through it in your Houdini succeeds. If it fails, install the Build Tools and run `install.cmd` again.

### From source (any Houdini 22.0 build)

1. Install Houdini 22.0, Blender 5.2, and the [Visual Studio 2022 Build Tools](https://visualstudio.microsoft.com/downloads/) with the *Desktop development with C++* workload. A separate [CMake](https://cmake.org/download/) is optional.
2. Download `houdini-eevee-0.7.2-source.zip` (or clone the repository) and extract it.
3. In the extracted folder, run:

   ```bat
   install.cmd
   ```

   If several Houdini 22.0 builds are installed, the installer uses the newest. To choose one, or if Houdini or Blender live in non-default folders, name them:

   ```bat
   install.cmd --houdini "C:\Program Files\Side Effects Software\Houdini 22.0.368" --blender "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"
   ```

4. Restart Houdini.

`install.cmd` finds Houdini's bundled Python, so a separate Python install is not required. The bridge installs to `%LOCALAPPDATA%\HoudiniEEVEE`. Its package file goes into the `houdini22.0\packages` folder inside your Documents folder. Paths with spaces, and Documents redirected to OneDrive, are supported.

**Installing on more Windows machines without a compiler.** After one machine has built the plugin and passed `--doctor`, package that installed copy:

```bat
"C:\Program Files\Side Effects Software\Houdini 22.0.368\python313\python.exe" tools\package_release.py --native-root .
```

Adjust the path to Houdini's bundled Python for your version. The resulting archive in `dist\` contains the Windows plugin, and installs with plain `install.cmd` on any machine with the same Houdini build.

## 4. What the installer does

1. It finds Houdini 22.0 (the one in `HFS`, or the newest installed build) and checks that it has the HDK.
2. It finds Blender 5.2 and checks for EEVEE, OpenVDB, NumPy and OpenEXR. If Python modules are missing, it installs them privately.
3. It uses the prebuilt plugin when it was compiled for your Houdini build. For another 22.0 build it compiles the plugin if build tools are available, and otherwise uses the prebuilt plugin only if the test render in step 5 succeeds.
4. It copies everything to a new, versioned folder:
   - Linux: `~/.local/share/houdini-eevee/0.7.2-h22.0.368`
   - Windows: `%LOCALAPPDATA%\HoudiniEEVEE\0.7.2-h22.0.368`
5. It starts a Blender worker and renders a small test image with EEVEE on your GPU, then renders a test scene with `husk` through the EEVEE plugin in your Houdini, ignoring other installed packages. Use `--no-gpu-check` to skip this on a machine without a GPU.
6. It writes `houdini_eevee.json` to your Houdini packages folder:
   - Linux: `~/houdini22.0/packages`
   - Windows: `Documents\houdini22.0\packages`

   The package activates only in the Houdini build it was made for. Any registration it replaces is kept for rollback.

The install folder also holds `installation.json`, which records this machine's Houdini and Blender paths and GPU choices. Logs and temporary files go elsewhere; see [Troubleshooting](#troubleshooting).

## 5. First run in Houdini

1. Start Houdini as usual and open a Solaris (LOP) network.
2. In the viewport's renderer menu, choose **EEVEE Bridge**.
3. The Blender worker starts in the background. The status bar shows its progress. The first draw of a scene compiles EEVEE's shaders, and a solid preview is shown meanwhile. Houdini stays usable during all of this.
4. Optionally, add an **EEVEE Render Settings** LOP at the end of your stage to control EEVEE's quality and to render to disk or MPlay.

The viewport's display options include EEVEE settings: samples, ray tracing, up axis, navigation resolution, texture size limit and subdivision accuracy. They are described in the [README](README.md#viewport-options).

The installed startup hook, `scripts/python/uiready.py`, only adds to Houdini's startup. It does not replace a studio's `123.py` or `456.py`, and it never adds anything to your scenes.

## 6. Check an installation

From the **installed** folder, run the doctor:

```bash
cd ~/.local/share/houdini-eevee/0.7.2-h22.0.368
python3 install.py --doctor
```

On Windows, run `install.cmd --doctor` in the installed folder.

The doctor checks that:

- Houdini's USD finds the renderer plugin.
- The volume helper runs.
- A worker starts and authenticates.
- EEVEE actually draws on the GPU.

Each run writes a log to `~/.cache/houdini-eevee/logs/doctor-*.log`.

## 7. Installer options

| Option | Purpose |
| --- | --- |
| `--houdini PATH` | Houdini 22.0 installation folder (`$HFS`). |
| `--blender PATH` | Blender 5.2 executable. |
| `--build` | Build the plugin against this Houdini even when a prebuilt one matches. `--jobs N` sets the number of parallel compile jobs (default 4). |
| `--no-build` | Never build. On a Houdini build the prebuilt plugin was not compiled for, use it only if the test render succeeds. |
| `--prefix PATH` | Install into this new folder instead of the default versioned one. Spaces are allowed. |
| `--packages-dir PATH` | Write the Houdini package into this folder, for example a studio or render-account packages folder. |
| `--gpu-device ID` | Pin the worker to a Vulkan GPU. The default, `auto`, lets Blender choose. See [Choosing the GPU](#8-choosing-the-gpu). |
| `--gpu-backend opengl` | Use Blender's OpenGL backend instead of Vulkan. |
| `--gpu-subdivision` | Use Blender's GPU subdivision in the viewport. It is faster for heavily deforming subdivided meshes but uses far more video memory (about 10 GB more on a large production shot). |
| `--no-gpu-check` | Skip the GPU test render, for example when preparing an install on a machine without a GPU. Run `--doctor` on the real machine afterwards. |
| `--no-register` | Install the files but leave your Houdini packages folder unchanged. |
| `--dry-run` | Show what would be installed, and where, without doing it. |
| `--doctor` | Check this installed copy (run it from the installed folder). |
| `--uninstall` | Remove this copy's package registration (run it from the installed folder). |
| `--rollback [VERSION]` | Restore the registration that the last install replaced, or a specific earlier one such as `0.7.1-h22.0.368`. |

## 8. Choosing the GPU

By default Blender picks the GPU. On a machine with several GPUs, benchmark them:

```bash
python3 tools/gpu_benchmark.py
```

It renders the same synthetic scene on each Vulkan GPU and prints a recommended `--gpu-device`. The worker copies pixels through system memory, so it can run on a different GPU from the one driving Houdini's display at no extra cost. This also keeps EEVEE from competing with Houdini's own viewport drawing.

To list devices yourself, run `blender --background --gpu-backend vulkan --gpu-device help`. To change the GPU of an existing install, edit `gpu_device` in its `installation.json` (for example `"1"`, or `"auto"`), or set `HDEEVEE_GPU_DEVICE` in Houdini's environment. Restart Houdini afterwards.

## 9. Update, roll back, uninstall

**Updating.** Run the new release's installer. Each version installs into its own folder, and existing installs are never overwritten, because a running Houdini may have their plugin loaded. The package registration switches to the new version only after the new copy passes its checks. Restart Houdini to use it.

**Reinstalling the same version.** The installer refuses to overwrite an existing folder. Either remove the old folder first (with Houdini closed), or pass a different `--prefix`.

**Rolling back.** Every replaced registration is kept next to the package as `houdini_eevee.json.<version>` and `houdini_eevee.json.previous`:

```bash
python3 install.py --rollback                   # the registration the last install replaced
python3 install.py --rollback 0.7.1-h22.0.368   # a specific earlier version
```

Restart Houdini afterwards.

**Uninstalling.** Run this from the installed folder:

```bash
python3 install.py --uninstall
```

It removes the package registration and leaves the files in place. After restarting Houdini, you can delete the install folder, and optionally `~/.cache/houdini-eevee` (on Windows, `%LOCALAPPDATA%\HoudiniEEVEE`). `--uninstall` does not remove a registration that now belongs to another install.

## 10. Render farms

Disk renders go through Houdini's USD Render ROP with the bridge's husk wrapper, `tools/eevee_husk.py`. The **EEVEE Render Settings** LOP sets this up for you.

Each job starts its own isolated Blender worker. The wrapper writes job logs, stages output files until the render succeeds, and stops its worker when the job is cancelled.

On render nodes:

- **Install for the right account.** Install and register the bridge for the account that runs the render client. Alternatively, set `HOUDINI_PACKAGE_DIR` in the job environment to a folder holding the package, which you can prepare with `--packages-dir`. An artist's user package is not visible to a service account.
- **Use the same versions.** Every render node needs the same Houdini build, Blender version and bridge version as the artists. The package only activates in the Houdini build it was made for.
- **Make assets reachable.** Scene files, textures, VDBs and output folders must be reachable from the render node. The bridge does not remap paths between operating systems, so use relative paths, a shared project root or a USD asset resolver.
- **Test a real GPU render.** Render one frame in the actual render-node session before production. A successful desktop render does not prove that the GPU works in a service or headless session.
- **No MPlay.** Render nodes should write files, because MPlay needs a desktop.
- **Local worker only.** Each render node runs its own local worker. It listens only on the loopback interface and is not a network render service.

The bridge does not include a Deadline or other farm submitter yet.

## 11. Environment variables

Set these in Houdini's environment, for example in the shell that launches Houdini, in `houdini.env`, or in a package. They override `installation.json`.

| Variable | Effect |
| --- | --- |
| `HDEEVEE_GPU_DEVICE` | Vulkan GPU for the worker (`auto` or a device index). |
| `HDEEVEE_GPU_BACKEND` | `vulkan` (default) or `opengl`. |
| `HDEEVEE_GPU_SUBDIVISION` | `1` uses Blender's GPU subdivision in the viewport. |
| `HDEEVEE_CACHE_ROOT` | Folder for logs and per-session files, for example a local scratch disk on render nodes. |
| `HDEEVEE_IDLE_TARGET_SECONDS` | Seconds before idle viewport render targets release their video memory (default 60). |
| `HDEEVEE_IDLE_EXIT_SECONDS` | Seconds after the last EEVEE viewport closes (for example, when you switch to Karma) before the worker exits and frees all its video memory (default 2). It starts again when a viewport uses EEVEE. `0` keeps it running for the whole Houdini session, which avoids the restart and shader compilation when you switch back. |
| `HDEEVEE_COLOR_FORMAT` | `float` sends 32-bit viewport color instead of 16-bit half floats. |
| `HDEEVEE_PYTHON_DEPS` | Folder with OpenEXR, Imath and NumPy modules for Blender's Python. |
| `HDEEVEE_KEEP_SESSION_DAYS` | Days before an unused session folder is removed (default 7). |
| `HDEEVEE_KEEP_LOG_DAYS` | Days before a log file is removed (default 14). |

## Troubleshooting

### Where the logs are

| | Linux | Windows |
| --- | --- | --- |
| Logs | `~/.cache/houdini-eevee/logs` | `%LOCALAPPDATA%\HoudiniEEVEE\logs` |
| Session files | `~/.cache/houdini-eevee/sessions` | `%LOCALAPPDATA%\HoudiniEEVEE\sessions` |

The logs folder holds these files:

- `houdini-*.log`: the worker log for an interactive Houdini session.
- `husk-*.log`: the log for a disk render.
- `doctor-*.log`: the output of `--doctor`.

Session folders are kept for diagnosis. When a worker starts, session folders nothing has touched for 7 days and logs older than 14 days are removed; see `HDEEVEE_KEEP_SESSION_DAYS` and `HDEEVEE_KEEP_LOG_DAYS`. You can also delete them yourself whenever Houdini is not running.

### Common problems

**EEVEE Bridge is not in the renderer menu.**
- Check that `houdini_eevee.json` is in your packages folder.
- Check that your Houdini build is the one the bridge was installed for, since the package activates only there. After a Houdini update, run the installer again for the new build.
- Run `--doctor` from the installed folder.

**The viewport stays empty or the status bar shows an error.**
- Read the newest `houdini-*.log`. It names the prim or shader that failed.
- If a worker crashes, the viewport restarts it and reconnects, up to three times per Houdini session. After that, fix the cause and restart Houdini.

**The worker does not start on Vulkan.**
- Update the GPU driver.
- Pin a specific GPU with `--gpu-device`.
- As a fallback, reinstall with `--gpu-backend opengl`.

**Surfaces render magenta.**
- The material uses a shader node that has no EEVEE translation. The worker log names the node.
- VEX-based shaders cannot run in EEVEE. Use MaterialX nodes, or bake the pattern to a texture.

**No depth of field.**
- Look through the camera.
- Set the camera's **F-Stop** above 0 and set a focus distance.
- Make sure **Disable Camera Depth of Field** is off on EEVEE Render Settings.

**High video memory use.**
- Lower **Texture Size Limit** in the viewport's EEVEE options. On a production shot, 2048 saved 3.4 GB.
- Keep **Subdivision Surfaces** on *Fast*, and do not use `--gpu-subdivision` unless you need it.
- Video memory for the navigation frames is released 60 s after you stop navigating.
- Every EEVEE viewport has a fixed cost of about 1 GB.

**The first frame of a scene takes a while.**
- EEVEE compiles shaders the first time it sees a material, and they are reused afterwards. A solid preview is shown meanwhile, and Houdini stays usable.
- *Exact* subdivision takes much longer to prepare than *Fast*.

**Reporting a bug.**
- [Open an issue](https://github.com/badgerz42/houdini-eevee-bridge/issues).
- Include your Houdini, Blender, GPU and driver versions, the relevant log, and if possible a small scene that reproduces it.
