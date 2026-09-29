"""Launch an isolated Blender worker on Linux or Windows."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time

from hde_runtime import (ROOT, blender_program, blender_environment, cache_root, new_session, prune_cache, settings,
                         subprocess_options)

_processes = {}


def start(socket_path=None, parent=0, backend=None, device=None, environment=None):
    env = dict(os.environ if environment is None else environment)
    configuration = settings(env)
    try:
        prune_cache()
    except OSError:
        pass   # housekeeping never prevents a render
    session = Path(env['HDEEVEE_SESSION_DIR']) if env.get('HDEEVEE_SESSION_DIR') else new_session()
    session.mkdir(parents=True, exist_ok=True)
    env['HDEEVEE_SESSION_DIR'] = str(session)
    path = Path(socket_path) if socket_path is not None else session/'endpoint.json'
    legacy = socket_path is not None and path.suffix != '.json'
    if legacy and os.name == 'nt':
        raise RuntimeError('Use a TCP endpoint on Windows, not a Unix socket')
    if path.exists():
        raise RuntimeError('Worker endpoint already exists: '+str(path))
    env = blender_environment(env)
    deps = env.get('HDEEVEE_PYTHON_DEPS') or configuration.get('python_deps')
    if deps: env['HDEEVEE_PYTHON_DEPS'] = str(deps)
    if 'HDEEVEE_GPU_SUBDIVISION' not in env and configuration.get('gpu_subdivision'):
        env['HDEEVEE_GPU_SUBDIVISION'] = '1'
    backend = backend or env.get('HDEEVEE_GPU_BACKEND') or configuration.get('gpu_backend','vulkan')
    device = device if device is not None else env.get('HDEEVEE_GPU_DEVICE', configuration.get('gpu_device'))
    executable = env.get('HDEEVEE_BLENDER') or configuration.get('blender') or str(blender_program())
    command = [executable, '--background', '--factory-startup', '--gpu-backend', backend]
    if device and device != 'auto' and backend == 'vulkan':
        command += ['--gpu-device', str(device), '--gpu-device-no-fallback']
    command += ['--python-exit-code','1','--python',str(ROOT/'worker/eevee_worker.py'),'--',
                '--socket' if legacy else '--endpoint-file',str(path)]
    if parent: command += ['--parent',str(parent)]
    log_dir = cache_root()/'logs'
    log_dir.mkdir(exist_ok=True)
    logfile = log_dir/(session.name+'.log')
    with logfile.open('wb') as log:
        process = subprocess.Popen(command, env=env, cwd=session, stdout=log,
                                   stderr=subprocess.STDOUT, **subprocess_options())
    _processes[process.pid] = process
    result = {'pid':process.pid,'socket':str(path),'endpoint':str(path),'log':str(logfile),
              'session':str(session),'transport':'unix' if legacy else 'tcp'}
    deadline = time.monotonic()+60
    try:
        while time.monotonic()<deadline:
            if process.poll() is not None:
                raise RuntimeError('EEVEE worker failed; see '+str(logfile)+'\n'+logfile.read_text(errors='replace')[-6000:])
            if path.exists():
                from bridge_client import Client
                client = Client(path)
                try: metadata, _, _ = client.request({'op':'ping'})
                finally: client.close()
                result['gpu'] = metadata['gpu']
                return result
            time.sleep(.1)
        raise TimeoutError('EEVEE worker did not become ready; see '+str(logfile))
    except BaseException:
        stop(result)
        raise


def stop(worker):
    """Only terminate a process this Python caller created; never an arbitrary PID."""
    process = _processes.pop(worker['pid'], None)
    if process and process.poll() is None:
        process.terminate()
        try: process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=8)
    Path(worker['socket']).unlink(missing_ok=True)


def is_running(worker):
    process = _processes.get(worker['pid'])
    return process is not None and process.poll() is None


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket', help='Optional legacy development socket')
    parser.add_argument('--parent',type=int,default=0)
    args = parser.parse_args()
    print(json.dumps(start(args.socket,parent=args.parent)))
