"""Replay a recorded scene inside Blender and profile EEVEE's per-draw costs.

Record with HDEEVEE_DUMP_SCENE=DIR on the worker (for example through
probes/run_harness.py --env), then:

    blender --background --factory-startup --gpu-backend vulkan \
        --python probes/replay_profile.py -- DIR REPORT.json [--first-draw]

Each experiment toggles one EEVEE feature and times single-sample draws of a
slowly orbiting view, so every draw is a fresh navigation frame.
"""
import argparse
import json
import math
import pickle
import statistics
import sys
import time
from pathlib import Path

import bpy
from mathutils import Matrix

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'worker'))
from eevee_worker import EeveeWorker  # noqa: E402


def load(worker, directory):
    start = time.perf_counter()
    for path in sorted(directory.glob('update-*.pickle')):
        with open(path, 'rb') as stream:
            request = pickle.load(stream)
        if request.get('reset'):
            worker.reset()
        worker.configure(request.get('config') or {}, request)
        worker.update(request.get('changes', []))
    return time.perf_counter() - start


def orbit(view, degrees):
    """Rotate the USD row-major view matrix around the view's pivot 1 m ahead."""
    m = Matrix(view).transposed()
    pivot = Matrix.Translation((0, 0, -1.0))
    rotated = pivot @ Matrix.Rotation(math.radians(degrees), 4, 'Y') @ pivot.inverted() @ m
    return [list(row) for row in rotated.transposed()]


def draws(worker, base, count, width, height, extra=None):
    times = []
    for i in range(count):
        request = {**base, 'width': width, 'height': height, 'output_width': width, 'output_height': height,
                   'samples': 1, 'ids': False, 'depth': False, 'aovs': [], 'purpose': 'navigate',
                   'view': orbit(base['view'], 0.3 * (i + 1)), 'revision': i}
        request.update(extra or {})
        start = time.perf_counter()
        meta, _, _, _ = worker.render(request)
        times.append({'wall': (time.perf_counter() - start) * 1000, 'draw': meta['draw_ms']})
    tail = times[len(times) // 3:]
    return {'draw_ms': round(statistics.median(t['draw'] for t in tail), 2),
            'wall_ms': round(statistics.median(t['wall'] for t in tail), 2)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('dump', type=Path)
    parser.add_argument('report', type=Path)
    parser.add_argument('--first-draw', action='store_true', help='only time the first preview/EEVEE draws')
    parser.add_argument('--no-gpu-subdivision', action='store_true')
    parser.add_argument('--hide-instances', action='store_true')
    parser.add_argument('--width', type=int, default=826)
    parser.add_argument('--height', type=int, default=539)
    parser.add_argument('--count', type=int, default=12)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    if args.no_gpu_subdivision:
        bpy.context.preferences.system.use_gpu_subdivision = False
    worker = EeveeWorker()
    report = {'gpu_subdivision': bpy.context.preferences.system.use_gpu_subdivision}
    report['upload_s'] = load(worker, args.dump)
    with open(args.dump / 'render-last.pickle', 'rb') as stream:
        base = pickle.load(stream)
    base = {k: v for k, v in base.items() if k not in ('token', 'transport', 'reset', 'changes')}
    if args.first_draw:
        if args.hide_instances:
            for obj in worker.point_instances.values():
                obj.hide_set(True, view_layer=worker.view_layer)
        start = time.perf_counter()
        meta, _, _, _ = worker.render({**base, 'width': args.width, 'height': args.height, 'samples': 1,
                                       'ids': False, 'depth': False, 'aovs': [], 'allow_preview': True})
        report['preview_first_ms'] = (time.perf_counter() - start) * 1000
        report['preview_first_meta'] = {k: meta.get(k) for k in ('preview', 'draw_ms', 'update_ms')}
        start = time.perf_counter()
        meta, _, _, _ = worker.render({**base, 'width': args.width, 'height': args.height, 'samples': 1,
                                       'ids': False, 'depth': False, 'aovs': []})
        report['eevee_first_ms'] = (time.perf_counter() - start) * 1000
        report['eevee_second'] = draws(worker, base, 4, args.width, args.height)
    else:
        scene, eevee = worker.scene, worker.scene.eevee
        draws(worker, base, 3, args.width, args.height)  # warm up and compile
        experiments = {}
        experiments['baseline'] = draws(worker, base, args.count, args.width, args.height)

        def toggle(name, apply, restore):
            apply()
            experiments[name] = draws(worker, base, args.count, args.width, args.height)
            restore()

        toggle('no_shadows', lambda: setattr(eevee, 'use_shadows', False), lambda: setattr(eevee, 'use_shadows', True))
        toggle('no_raytracing', lambda: setattr(eevee, 'use_raytracing', False), lambda: setattr(eevee, 'use_raytracing', True))
        fast_gi = eevee.use_fast_gi
        toggle('no_fast_gi', lambda: setattr(eevee, 'use_fast_gi', False), lambda: setattr(eevee, 'use_fast_gi', fast_gi))
        reprojection = eevee.use_taa_reprojection
        toggle('no_taa_reprojection', lambda: setattr(eevee, 'use_taa_reprojection', False),
               lambda: setattr(eevee, 'use_taa_reprojection', reprojection))
        scale = eevee.shadow_resolution_scale
        toggle('shadow_scale_0.25', lambda: setattr(eevee, 'shadow_resolution_scale', 0.25),
               lambda: setattr(eevee, 'shadow_resolution_scale', scale))
        rt_scale = eevee.ray_tracing_options.resolution_scale
        toggle('raytrace_scale_4', lambda: setattr(eevee.ray_tracing_options, 'resolution_scale', '4'),
               lambda: setattr(eevee.ray_tracing_options, 'resolution_scale', rt_scale))
        points = list(worker.point_instances.values())

        def hide(state):
            for obj in points:
                obj.hide_set(state, view_layer=worker.view_layer)
        toggle('no_point_instances', lambda: hide(True), lambda: hide(False))
        subdivided = [o for o in worker.objects.values() if o.modifiers.get('USD subdivision')]

        def subdivide(enabled):
            for obj in subdivided:
                obj.modifiers['USD subdivision'].show_viewport = enabled
        toggle('no_subdivision', lambda: subdivide(False), lambda: subdivide(True))
        experiments['full_resolution'] = draws(worker, base, args.count, args.width * 2, args.height * 2)
        experiments['quarter_resolution'] = draws(worker, base, args.count, args.width // 2, args.height // 2)
        report['experiments'] = experiments
        report['scene'] = {'objects': len(worker.objects), 'point_instances': sum(int(o['usd_instance_count']) for o in points),
                           'object_instances': sum(len(v) for v in worker.instances.values()),
                           'subdivided_meshes': len(subdivided)}
    worker.offscreen = None
    args.report.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
