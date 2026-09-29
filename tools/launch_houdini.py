"""Optional launcher; normal installed Houdini startup also discovers EEVEE."""
import argparse
import json
import os
from pathlib import Path
import subprocess
from start_worker import start, stop
from bridge_client import Client
from hde_runtime import (ROOT, cache_root, new_session, renderer_environment,
                         houdini_program, settings, subprocess_options)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gpu-backend',choices=('opengl','vulkan'))
    parser.add_argument('--gpu-device',help='Blender Vulkan device ID; default is automatic')
    scene = parser.add_mutually_exclusive_group()
    scene.add_argument('--demo',action='store_true',help='Create the optional development test scene')
    scene.add_argument('--hip',type=Path,help='Open an existing Houdini scene')
    parser.add_argument('--dev-control',action='store_true')
    parser.add_argument('--lop-node',help='Existing LOP node to display')
    args = parser.parse_args()
    if args.hip and not args.hip.is_file(): parser.error('Scene does not exist: '+str(args.hip))
    session = new_session('launch')
    env = renderer_environment()
    logs = cache_root()/'logs'
    logs.mkdir(exist_ok=True)
    env.update(HDEEVEE_SESSION_DIR=str(session),HDEEVEE_ENDPOINT=str(session/'endpoint.json'),
               HDEEVEE_AUTO_WORKER='0',HDEEVEE_AUTO_SELECT='1',
               HDEEVEE_DEMO='1' if args.demo else '0',HDEEVEE_DEV_CONTROL='1' if args.dev_control else '0',
               HDEEVEE_TRACE=str(logs/(session.name+'.jsonl')))
    env.pop('HDEEVEE_SOCKET',None)
    if args.lop_node: env['HDEEVEE_SOURCE_LOP'] = args.lop_node
    worker = start(backend=args.gpu_backend,device=args.gpu_device,environment=env)
    # installation.json may name a site-specific launcher (a list of arguments).
    command = settings().get('launch_command') or [str(houdini_program('houdini')),'-foreground']
    if args.hip: command = [*command,str(args.hip.resolve())]
    try:
        with (logs/(session.name+'.houdini.log')).open('wb') as log:
            process = subprocess.Popen(command,env=env,cwd=Path.home(),stdout=log,stderr=subprocess.STDOUT,**subprocess_options())
        client = Client(worker['endpoint'])
        try: client.request({'op':'set_owner','pid':process.pid})
        finally: client.close()
    except BaseException:
        stop(worker)
        raise
    result = {'launcher_pid':process.pid,'worker':worker,'launch_command':command,'trace':env['HDEEVEE_TRACE'],
              'demo':args.demo,'dev_control':args.dev_control,'hip':str(args.hip.resolve()) if args.hip else None,'lop_node':args.lop_node}
    (logs/'latest-launch.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    if args.demo or args.dev_control:
        (ROOT/'artifacts').mkdir(exist_ok=True)
        (ROOT/'runtime').mkdir(exist_ok=True)
        (ROOT/'artifacts/launch.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__ == '__main__': main()
