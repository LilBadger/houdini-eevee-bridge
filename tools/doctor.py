"""Exercise the installed native plugin, TCP worker, and real EEVEE GPU draw."""
import json
import os
from pathlib import Path
import re
import subprocess
from hde_runtime import (ROOT, settings, new_session, renderer_environment, houdini_build, houdini_root,
                         houdini_program, native_supports, volume_helper, plugin_directory)
from start_worker import start, stop
from bridge_client import Client

# A red cube, lit, seen by a camera: the center pixel must come out red.
TEST_SCENE = """#usda 1.0
(
    upAxis = "Y"
)
def Cube "cube"
{
    double size = 1
    color3f[] primvars:displayColor = [(0.8, 0.3, 0.2)]
}
def Camera "camera"
{
    double3 xformOp:translate = (0, 0, 4)
    uniform token[] xformOpOrder = ["xformOp:translate"]
}
def DistantLight "sun"
{
    float inputs:intensity = 2
    bool inputs:normalize = 1
}
"""


def isolated(env, session):
    """The environment without the user's and studio's Houdini packages, which may
    register another EEVEE install whose plugin would be tested instead of this one."""
    result = dict(env, HOUDINI_USER_PREF_DIR=str(session/'prefs'/'houdini__HVER__'))
    result.pop('HOUDINI_PACKAGE_DIR', None)
    return result


def hydra_render(env, worker, session):
    """Render a small stage with husk through the EEVEE render delegate. This loads
    the native plugin into this Houdini build and runs it end to end."""
    scene, output = session/'doctor.usda', session/'doctor.exr'
    scene.write_text(TEST_SCENE, encoding='utf-8')
    result = subprocess.run([str(houdini_program('husk')), '-R', 'HdEeveeRendererPlugin', '--res', '32', '32',
                             '-c', '/camera', '-o', str(output), '--timelimit', '120', str(scene)],
                            env=dict(isolated(env, session), HDEEVEE_ENDPOINT=worker['endpoint']), capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=180)
    if result.returncode or not output.is_file():
        raise RuntimeError('Husk could not render with EEVEE in this Houdini build:\n'+(result.stdout+result.stderr)[-4000:])
    stats = subprocess.run([str(houdini_program('hoiiotool')), str(output), '--ch', 'R,G,B', '--cut', '1x1+16+16',
                            '--printstats'], env=env, capture_output=True, text=True, timeout=60)
    match = re.search(r'Stats Avg:\s+(\S+)\s+(\S+)\s+(\S+)', stats.stdout)
    if not match:
        raise RuntimeError('Cannot read the EEVEE test render: '+(stats.stdout+stats.stderr)[-2000:])
    red, green, blue = map(float, match.groups())
    if red < .1 or red <= max(green, blue):
        raise RuntimeError('The EEVEE test render through husk is wrong; its center pixel is %.3f %.3f %.3f' % (red, green, blue))
    return [round(red, 3), round(green, 3), round(blue, 3)]


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
    build = houdini_build(houdini_root())
    if not native_supports(info, build, configuration):
        raise RuntimeError('The native plugin was built for Houdini '+str(info.get('houdini_version'))+
                           ' and is not verified for Houdini '+str(build)+'. Reinstall for this build.')
    helper = subprocess.run([str(volume_helper())], env=env, capture_output=True, text=True, timeout=30)
    if helper.returncode != 1 or 'Usage: hde_volume' not in helper.stderr:
        raise RuntimeError('Houdini volume helper could not load: '+helper.stderr[-3000:])
    native = subprocess.run([str(houdini_program('husk')), '--list-renderers'], env=isolated(env, session),
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
        client.close()
        client = None
        pixel = hydra_render(env, worker, session)
        return {'ok':True, 'houdini':build, 'native_plugin':info, 'volume_helper':str(volume_helper()),
                'gpu':worker['gpu'], 'transport':'authenticated loopback TCP',
                'gpu_draw':[32,32], 'hydra_render':pixel, 'worker_log':worker['log'], 'bridge_ms':metadata.get('bridge_ms')}
    finally:
        if client: client.close()
        stop(worker)


if __name__ == '__main__':
    print(json.dumps(check(), indent=2))
