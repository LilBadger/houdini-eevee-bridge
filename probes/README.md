# Probes

Development tools for testing and profiling the bridge without Houdini's user interface. None of them are needed to use the bridge, and the installer does not copy them.

## Hydra harness

`hydra_harness.cpp` loads the render delegate through Houdini's USD libraries and drives it the way a Solaris viewport does. It calls the render pass 60 times a second while orbiting the camera, editing the scene or resizing, and reports several measurements:

- How long each call blocked the calling thread, which in Houdini is the UI thread.
- How many new frames were presented, and how long convergence took.
- Pick IDs and depth.

It runs without Houdini's UI, so it needs no Houdini license.

Build it and run it with a fresh worker:

```bash
cmake --build build --target hydra_harness
python3 probes/bench_scene.py bench.usda
python3 probes/run_harness.py --scene bench.usda --report report.json width=1280 height=720 orbit=120
```

The harness takes `key=value` options:

| Option | Meaning |
| --- | --- |
| `width`, `height` | Viewport size (default 1280×720). |
| `camera` | The USD camera to look through (default: the first camera in the stage). |
| `time` | The time code to render. |
| `pivot` | The point the camera orbits around, as `x,y,z`. |
| `orbit`, `degrees` | Number of 60 Hz orbit ticks, and degrees per tick. |
| `second` | `1` also renders a second, independent viewport. |
| `samples` | Overrides the viewport sample count. |
| `config_prim` | An EEVEE render settings prim to use. |
| `edit` | Changes a shader's color input and checks that the image updates, as `/shader/prim:input:r,g,b`. |
| `settle` | Longest wait for convergence, in seconds. |
| `output` | Also writes a final render to this path. |
| `idle` | Seconds to stay idle at the end, to observe idle memory release. |

`run_harness.py` has further options:

- `--root` and `--plugin` choose which bridge tree and plugin build to test, so two versions can be compared with the same harness, scene and GPU.
- `--gpu-device` selects the worker's GPU.
- `--env NAME=VALUE` sets environment variables for both the worker and the harness.

## Scenes and profiling

| File | Purpose |
| --- | --- |
| `bench_scene.py` | Writes a reproducible USD benchmark scene. It needs no Houdini or `pxr` module. The scene has a UV-mapped floor, spheres with Preview Surface materials, a point instancer, a distant light, a dome light and a camera. |
| `export_lop_stage.py` | Exports a LOP node's composed stage for harness tests. It runs in `hython`. |
| `replay_profile.py` | Replays a recorded scene inside Blender and times single-sample EEVEE draws, toggling one EEVEE feature at a time. |
| `vram_profile.py` | Replays a recorded scene and measures where the worker's video memory goes. It uses `nvidia-smi`. |

To record a scene for the replay tools, set `HDEEVEE_DUMP_SCENE=DIR` on the worker, for example with `run_harness.py --env HDEEVEE_DUMP_SCENE=/tmp/dump`. Then replay it:

```bash
blender --background --factory-startup --gpu-backend vulkan --python probes/replay_profile.py -- /tmp/dump report.json
blender --background --factory-startup --gpu-backend vulkan --python probes/vram_profile.py -- /tmp/dump vram.json
```

## Registration checks

`registry_probe.cpp` checks that Houdini's USD discovers the renderer plugin. `volume_registry_probe.cpp` checks access to Houdini's volume registry.

```bash
cmake --build build --target registry_probe
./build/registry_probe "$PWD/build/plugin/hdEevee/resources"
```
