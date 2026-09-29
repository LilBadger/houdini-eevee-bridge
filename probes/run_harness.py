"""Run build/hydra_harness against a freshly started EEVEE worker.

Example (after `cmake --build build --target hydra_harness`):

    python probes/run_harness.py --scene bench.usda --report report.json

`--root` selects the bridge tree whose worker is started and `--plugin` the
plugin resources directory, so two versions can be compared with the same
harness, scene and GPU.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=HERE, help='bridge tree providing tools/ and worker/')
    parser.add_argument('--plugin', type=Path, default=HERE / 'build/plugin/hdEevee/resources')
    parser.add_argument('--harness', type=Path, default=HERE / 'build/hydra_harness')
    parser.add_argument('--scene', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--gpu-device', default=None)
    parser.add_argument('options', nargs='*', help='harness key=value options (width, height, orbit, camera, time, ...)')
    parser.add_argument('--env', action='append', default=[], help='extra NAME=VALUE for the worker and the harness')
    args = parser.parse_args()
    sys.path.insert(0, str(args.root / 'tools'))
    from hde_runtime import houdini_environment
    from start_worker import start, stop
    env = houdini_environment()
    # Extra variables apply to the worker and the harness (for example
    # HDEEVEE_TRACE for the delegate, HDEEVEE_DUMP_SCENE for the worker).
    for item in args.env:
        name, value = item.split('=', 1)
        env[name] = value
    env.setdefault('HDEEVEE_CACHE_ROOT', tempfile.mkdtemp(prefix='hde-harness-'))
    os.environ['HDEEVEE_CACHE_ROOT'] = env['HDEEVEE_CACHE_ROOT']
    worker = start(device=args.gpu_device, environment=env)
    try:
        run_env = dict(env)
        run_env['HDEEVEE_ENDPOINT'] = worker['endpoint']
        run_env['HDEEVEE_SESSION_DIR'] = worker['session']
        run_env.pop('HDEEVEE_SOCKET', None)
        command = [str(args.harness), str(args.plugin), str(args.scene), str(args.report), *args.options]
        result = subprocess.run(command, env=run_env, capture_output=True, text=True, timeout=3600)
        (args.report.with_suffix('.log')).write_text(result.stdout + '\n--- stderr ---\n' + result.stderr)
        if result.returncode:
            print(result.stderr[-4000:], file=sys.stderr)
            return result.returncode
        report = json.loads(args.report.read_text())
        report['worker'] = {'gpu': worker.get('gpu'), 'log': worker['log']}
        args.report.write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
        return 0
    finally:
        stop(worker)


if __name__ == '__main__':
    raise SystemExit(main())
