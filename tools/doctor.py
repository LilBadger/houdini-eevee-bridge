"""Exercise the installed native plugin, TCP worker, and real EEVEE GPU draw."""
import json
import os
from pathlib import Path
import subprocess
from hde_runtime import (ROOT, settings, new_session, renderer_environment,
                         houdini_program, volume_helper, plugin_directory)
from start_worker import start, stop
from bridge_client import Client


def check():
    configuration = settings()
    env = renderer_environment()
    env['HDEEVEE_AUTO_WORKER'] = '0'
    env['HDEEVEE_AUTO_SELECT'] = '0'
    env['HDEEVEE_DEMO'] = '0'
    env.pop('HDEEVEE_SOCKET', None)
    env.pop('HDEEVEE_ENDPOINT', None)
    session = new_session('doctor')
    env['HDEEVEE_SESSION_DIR'] = str(session)
    info = json.loads((plugin_directory()/'build-info.json').read_text(encoding='utf-8'))
    if configuration.get('houdini_version') and info['houdini_version'] != configuration['houdini_version']:
        raise RuntimeError('Native plugin does not match the configured Houdini build')
    helper = subprocess.run([str(volume_helper())], env=env, capture_output=True, text=True, timeout=30)
    if helper.returncode != 1 or 'Usage: hde_volume' not in helper.stderr:
        raise RuntimeError('Houdini volume helper could not load: '+helper.stderr[-3000:])
    native = subprocess.run([str(houdini_program('husk')), '--list-renderers'], env=env,
                            capture_output=True, text=True, timeout=60)
    if native.returncode or 'HdEeveeRendererPlugin' not in native.stdout+native.stderr:
        raise RuntimeError('Husk did not discover EEVEE: '+(native.stdout+native.stderr)[-4000:])
    worker = start(parent=os.getpid(), environment=env)
    client = None
    try:
        client = Client(worker['endpoint'])
        request = {'op':'render','reset':True, 'width':32,'height':32,'samples':1,
            'view':[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,-5,1]],
            'projection':[[1,0,0,0],[0,1,0,0],[0,0,-1.002,-1],[0,0,-.2002,0]],
            'changes':[], 'raytracing':False}
        metadata,color,_ = client.request(request)
        if len(color) != 32*32*16: raise RuntimeError('EEVEE GPU readback returned incorrect dimensions')
        return {'ok':True, 'native_plugin':info, 'volume_helper':str(volume_helper()),
                'gpu':worker['gpu'], 'transport':'authenticated loopback TCP',
                'gpu_draw':[32,32], 'worker_log':worker['log'], 'bridge_ms':metadata.get('bridge_ms')}
    finally:
        if client: client.close()
        stop(worker)


if __name__ == '__main__':
    print(json.dumps(check(), indent=2))
