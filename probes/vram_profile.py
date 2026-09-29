"""Measure where an EEVEE worker's GPU memory goes for a recorded scene.

    blender --background --factory-startup --gpu-backend vulkan --gpu-device 1 \
        --python probes/vram_profile.py -- DUMP_DIR REPORT.json

Uses nvidia-smi's per-process memory. Stages: empty worker, scene loaded,
first full-resolution EEVEE draw, navigation-size draw, ID pass; then the
effect of freeing the navigation target, texture size limits, half-float
textures and the shadow pool size.
"""
import json
import os
import pickle
import subprocess
import sys
import time
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'worker'))
sys.path.insert(0, str(ROOT / 'probes'))
from eevee_worker import EeveeWorker  # noqa: E402
from replay_profile import load  # noqa: E402


def vram():
    time.sleep(.5)
    out = subprocess.run(['nvidia-smi', '--query-compute-apps=pid,used_memory', '--format=csv,noheader,nounits'],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        pid, used = [x.strip() for x in line.split(',')]
        if pid.isdigit() and int(pid) == os.getpid() and used.isdigit():
            return int(used)
    # Windows (WDDM) reports no per-process memory. Fall back to the whole GPU:
    # the differences between stages stay valid while nothing else allocates.
    out = subprocess.run(['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'],
                         capture_output=True, text=True).stdout.split()
    return int(out[0]) if out and out[0].isdigit() else None


def textures():
    rows = []
    for image in bpy.data.images:
        if image.source not in ('FILE', 'TILED') or not image.users:
            continue
        w, h = image.size
        tiles = len(image.tiles) if image.source == 'TILED' else 1
        rows.append({'name': image.name, 'size': [w, h], 'tiles': tiles, 'float': image.is_float,
                     'half': getattr(image, 'use_half_precision', None), 'channels': image.channels,
                     'depth': image.depth, 'colorspace': image.colorspace_settings.name})
    return rows


def breakdown(worker, render, W, H, report, report_path):
    """Geometry (flat pass) -> EEVEE at 64x64 (textures, pools) -> full resolution."""
    session = worker._session
    stages = report['stages']
    if '--hide-instances' in sys.argv:
        for obj in session.point_instances.values():
            obj.hide_set(True, view_layer=session.view_layer)
        for group in session.instances.values():
            for obj in group:
                obj.hide_set(True, view_layer=session.view_layer)
    if '--no-subdivision' in sys.argv:
        for obj in session.objects.values():
            modifier = obj.modifiers.get('USD subdivision')
            if modifier:
                modifier.show_viewport = False
    render(W, H, 'navigate', ids=True, allow_preview=True)       # Workbench preview + ID pass: geometry only
    stages['geometry_workbench'] = vram()
    render(64, 64, 'refine')
    stages['eevee_64px'] = vram()
    render(W, H, 'refine')
    stages['eevee_full_res'] = vram()
    objects = list(session.objects.values())
    report['scene'] = {
        'meshes': sum(1 for o in objects if o.type == 'MESH'),
        'curves': sum(1 for o in objects if o.type == 'CURVES'),
        'curve_points': sum(len(o.data.points) for o in objects if o.type == 'CURVES'),
        'mesh_vertices': sum(len(o.data.vertices) for o in objects if o.type == 'MESH'),
        'mesh_faces': sum(len(o.data.polygons) for o in objects if o.type == 'MESH'),
        'subdivided_meshes': sum(1 for o in objects if o.modifiers.get('USD subdivision')),
        'subdivided_faces': sum(len(o.data.polygons) for o in objects if o.modifiers.get('USD subdivision')),
        'gpu_subdivision': bpy.context.preferences.system.use_gpu_subdivision,
        'point_instances': sum(int(o['usd_instance_count']) for o in session.point_instances.values()),
    }
    for obj in objects:
        if obj.type == 'CURVES':
            obj.hide_set(True, view_layer=session.view_layer)
    render(W, H, 'refine')
    stages['curves_hidden'] = vram()
    for obj in objects:
        modifier = obj.modifiers.get('USD subdivision')
        if modifier:
            modifier.show_viewport = False
    render(W, H, 'refine')
    stages['subdivision_off'] = vram()
    worker.offscreen = None
    report_path.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


def main():
    dump, report_path = [Path(p) for p in sys.argv[sys.argv.index('--') + 1:][:2]]
    if '--no-gpu-subdivision' in sys.argv:
        bpy.context.preferences.system.use_gpu_subdivision = False
    report = {'stages': {}}
    report['stages']['worker_init'] = None
    worker = EeveeWorker()
    report['stages']['worker_init'] = vram()
    load(worker, dump)
    report['stages']['scene_loaded'] = vram()
    with open(dump / 'render-last.pickle', 'rb') as stream:
        base = pickle.load(stream)
    base = {k: v for k, v in base.items() if k not in ('token', 'transport', 'reset', 'changes')}
    W, H = base.get('output_width', base['width']), base.get('output_height', base['height'])

    def render(width, height, purpose, samples=1, ids=False, **extra):
        return worker.render({**base, 'width': width, 'height': height, 'output_width': W, 'output_height': H,
                              'samples': samples, 'purpose': purpose, 'ids': ids, 'depth': ids, 'aovs': [], **extra})

    if '--breakdown' in sys.argv:
        breakdown(worker, render, W, H, report, report_path)
        return
    render(W, H, 'refine')
    report['stages']['eevee_full_res'] = vram()
    render(W // 2, H // 2, 'navigate')
    report['stages']['plus_navigation_instance'] = vram()
    render(W, H, 'refine', ids=True)
    report['stages']['plus_id_pass'] = vram()
    report['textures'] = textures()
    tex_bytes = 0
    for t in report['textures']:
        per = (16 if t['float'] and not t['half'] else 8 if t['float'] else 4)
        tex_bytes += t['size'][0] * t['size'][1] * t['tiles'] * per * 4 / 3
    report['texture_estimate_mb'] = round(tex_bytes / 2 ** 20)
    session = worker._session
    entry = session.offscreens.pop('navigate', None)
    if entry:
        entry[1].free()
    report['stages']['navigation_instance_freed'] = vram()
    experiments = {}
    prefs = bpy.context.preferences.system
    for limit in ('4096', '2048'):
        prefs.gl_texture_limit = 'CLAMP_' + limit
        for image in bpy.data.images:
            image.gl_free()
        render(W, H, 'refine')
        experiments['texture_limit_' + limit] = vram()
    prefs.gl_texture_limit = 'CLAMP_OFF'
    for image in bpy.data.images:
        if image.is_float and hasattr(image, 'use_half_precision'):
            image.use_half_precision = True
        image.gl_free()
    render(W, H, 'refine')
    experiments['half_float_textures'] = vram()
    scene = worker.scene
    report['shadow_pool_size'] = scene.eevee.shadow_pool_size
    for size in ('256', '128'):
        scene.eevee.shadow_pool_size = size
        render(W, H, 'refine')
        experiments['shadow_pool_' + size] = vram()
    report['experiments'] = experiments
    report['resolution'] = [W, H]
    worker.offscreen = None
    report_path.write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != 'textures'}, indent=2))
    print('textures:', len(report['textures']))


if __name__ == '__main__':
    main()
