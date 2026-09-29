"""Compare EEVEE worker speed on each Vulkan GPU with the same synthetic scene.

    python tools/gpu_benchmark.py [--devices 0,1] [--json report.json]

The worker copies pixels through CPU memory, so it may run on a different GPU
from the one driving Houdini's display at no extra transfer cost. A separate
GPU also stops EEVEE from competing with Houdini's own viewport drawing.
Install with the chosen device: install.py --gpu-device INDEX
"""
import argparse
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np
from bridge_client import Client
from hde_runtime import blender_environment, blender_program
from start_worker import start, stop


def devices():
    result = subprocess.run([str(blender_program()), '--background', '--factory-startup', '--gpu-backend', 'vulkan',
                             '--gpu-device', 'help'], env=blender_environment(), capture_output=True, text=True, timeout=120)
    found = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit():
            found.append({'index': int(parts[0]), 'id': parts[1], 'name': parts[2]})
    return found


def scene():
    n = 600
    xs, zs = np.meshgrid(np.linspace(-20, 20, n, dtype=np.float32), np.linspace(-20, 20, n, dtype=np.float32))
    points = np.stack([xs.ravel(), np.zeros(n * n, np.float32), zs.ravel()], 1)
    i = np.arange((n - 1) * (n - 1))
    v0 = (i // (n - 1)) * n + i % (n - 1)
    indices = np.stack([v0, v0 + n, v0 + n + 1, v0 + 1], 1).astype(np.int32).ravel()
    counts = np.full((n - 1) * (n - 1), 4, np.int32)
    cube = np.array([[-1, -1, 1], [1, -1, 1], [-1, 1, 1], [1, 1, 1], [-1, 1, -1], [1, 1, -1], [-1, -1, -1], [1, -1, -1]],
                    np.float32) * .1
    cube_indices = np.array([0, 1, 3, 2, 2, 3, 5, 4, 4, 5, 7, 6, 6, 7, 1, 0, 1, 7, 5, 3, 6, 0, 2, 4], np.int32)
    rng = np.random.default_rng(7)
    transforms = np.tile(np.identity(4, np.float32), (30000, 1, 1))
    transforms[:, 3, 0] = rng.uniform(-18, 18, 30000)
    transforms[:, 3, 1] = rng.uniform(0.1, 3, 30000)
    transforms[:, 3, 2] = rng.uniform(-18, 18, 30000)
    identity = np.identity(4).tolist()
    changes = [{'kind': 'material', 'id': '/m/%d' % k, 'parameters': {
        'diffuseColor': [.2 + .07 * k, .5, .8 - .07 * k], 'roughness': .1 + .08 * k, 'metallic': float(k % 3 == 0)}}
        for k in range(10)]
    changes.append({'kind': 'mesh', 'id': '/floor', 'prim_id': 1, 'points': points, 'counts': counts, 'indices': indices,
                    'transform': identity, 'visible': True, 'material': '/m/1'})
    changes.append({'kind': 'mesh', 'id': '/cubes', 'prim_id': 2, 'points': cube, 'counts': np.full(6, 4, np.int32),
                    'indices': cube_indices, 'transform': identity, 'visible': True, 'material': '/m/3',
                    'instances': transforms})
    for k in range(3):
        angle = 2 * math.pi * k / 3
        changes.append({'kind': 'light', 'id': '/light%d' % k, 'type': 'rectLight', 'visible': True,
                        'parameters': {'intensity': 40, 'width': 4, 'height': 4},
                        'transform': [[1, 0, 0, 0], [0, 0, -1, 0], [0, 1, 0, 0], [12 * math.cos(angle), 10, 12 * math.sin(angle), 1]]})
    changes.append({'kind': 'light', 'id': '/sky', 'type': 'domeLight', 'visible': True,
                    'parameters': {'intensity': .5, 'color': [.6, .7, .9]}, 'transform': identity})
    return changes


def view(angle):
    eye = np.array([30 * math.sin(angle), 12, 30 * math.cos(angle)])
    forward = -eye / np.linalg.norm(eye)
    side = np.cross(forward, [0, 1, 0]); side /= np.linalg.norm(side)
    up = np.cross(side, forward)
    m = np.identity(4)
    m[0, :3], m[1, :3], m[2, :3] = side, up, -forward
    m[:3, 3] = -m[:3, :3] @ eye
    return m.T.tolist()


def projection(width, height):
    f = 1 / math.tan(math.radians(20))
    m = np.zeros((4, 4))
    m[0, 0], m[1, 1] = f * height / width, f
    m[2, 2], m[2, 3], m[3, 2] = -1.0002, -.20002, -1
    return m.T.tolist()


def measure(device):
    worker = start(device=str(device['index']))
    client = None
    try:
        client = Client(worker['endpoint'], timeout=900)
        started = time.perf_counter()
        client.request({'op': 'update', 'reset': True, 'changes': scene()})
        upload = time.perf_counter() - started
        base = {'op': 'render', 'transport': 'shm', 'raytracing': True}
        started = time.perf_counter()
        client.request({**base, 'view': view(0), 'projection': projection(960, 540), 'width': 960, 'height': 540, 'samples': 1})
        first = time.perf_counter() - started
        navigate = []
        for i in range(24):
            meta, _, _ = client.request({**base, 'view': view(.01 * (i + 1)), 'projection': projection(960, 540),
                                         'width': 960, 'height': 540, 'samples': 1, 'revision': i})
            navigate.append(meta['total_ms'])
        meta, _, _ = client.request({**base, 'view': view(.5), 'projection': projection(1920, 1080),
                                     'width': 1920, 'height': 1080, 'samples': 16, 'ids': True, 'depth': True})
        return {**device, 'gpu': worker.get('gpu'), 'upload_s': round(upload, 2), 'first_draw_s': round(first, 2),
                'navigate_ms': round(statistics.median(navigate[6:]), 2),
                'refine_16_1080p_ms': round(meta['total_ms'], 1)}
    finally:
        if client:
            client.close()
        stop(worker)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--devices', help='comma-separated Vulkan device indices (default: all)')
    parser.add_argument('--json', type=Path)
    args = parser.parse_args()
    available = devices()
    if args.devices:
        wanted = {int(x) for x in args.devices.split(',')}
        available = [d for d in available if d['index'] in wanted]
    results = []
    for device in available:
        print('Benchmarking', device['index'], device['name'], flush=True)
        try:
            results.append(measure(device))
        except Exception as exc:
            results.append({**device, 'error': str(exc)})
        print(json.dumps(results[-1]), flush=True)
    usable = [r for r in results if 'error' not in r]
    report = {'results': results}
    if usable:
        best = min(usable, key=lambda r: r['navigate_ms'] + r['refine_16_1080p_ms'] / 16)
        report['recommended_device'] = best['index']
        report['recommendation'] = ('install.py --gpu-device ' + str(best['index']) + '  (' + best['name'] + ')')
    if args.json:
        args.json.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
