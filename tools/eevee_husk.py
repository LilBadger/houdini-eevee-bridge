"""Give each native USD Render ROP its own isolated EEVEE worker."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from start_worker import start, stop
from hde_runtime import houdini_program, new_session, renderer_environment, cache_root


def output_override(arguments):
    """Husk accepts both -o/--output VALUE and --output=VALUE; last one wins."""
    output = None
    for index, argument in enumerate(arguments):
        if argument in ('-o', '--output') and index + 1 < len(arguments):
            output = arguments[index + 1]
        elif argument.startswith('--output='):
            output = argument.split('=', 1)[1]
    return output


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    mplay = output_override(arguments) == 'ip'
    session = new_session('husk')
    manifest = session/'outputs.jsonl'
    env = renderer_environment()
    env.pop('HDEEVEE_SOCKET', None)
    env.update(HDEEVEE_ENDPOINT=str(session/'endpoint.json'),
               HDEEVEE_SESSION_DIR=str(session), HDEEVEE_OUTPUT_MANIFEST=str(manifest),
               HDEEVEE_FINAL_RENDER='1', HDEEVEE_AUTO_WORKER='0',
               HDEEVEE_DEMO='0', HDEEVEE_AUTO_SELECT='0', HDEEVEE_DEV_CONTROL='0')
    if mplay: env['HDEEVEE_MPLAY'] = '1'
    else: env.pop('HDEEVEE_MPLAY', None)
    log_dir = cache_root()/'logs'
    log_dir.mkdir(exist_ok=True)
    trace = log_dir/(session.name+'.jsonl')
    env['HDEEVEE_TRACE'] = str(trace)
    worker = None
    process = None
    handlers = {}

    def cancel(signum, frame):
        raise KeyboardInterrupt('Render canceled')

    def outputs():
        return [json.loads(line) for line in manifest.read_text(encoding='utf-8').splitlines()] if manifest.exists() else []

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers[sig] = signal.signal(sig, cancel)
        worker = start(parent=os.getpid(), environment=env)
        print('EEVEE worker: '+worker['gpu']+'; log: '+worker['log'], flush=True)
        # The worker encodes final files. Husk writes its intermediary to a
        # per-job scratch directory, except for the native MPlay destination.
        output = [] if mplay else ['--output', str(session/'beauty.$F4.exr')]
        with trace.with_suffix('.husk.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen([str(houdini_program('husk')), *arguments, *output],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding='utf-8', errors='replace')
            for line in process.stdout:
                log.write(line); log.flush()
                print(line, end='', flush=True)
            status = process.wait()
        records = outputs()
        if status == 0 and records:
            for record in records:
                if not record.get('mplay'):
                    Path(record['encoded']).replace(record['output'])
        elif status == 0:
            print('EEVEE did not produce an output image. See '+worker['log'], file=sys.stderr)
            status = 1
        return status
    except KeyboardInterrupt:
        print('EEVEE render canceled.', file=sys.stderr)
        return 130
    finally:
        if process and process.poll() is None:
            process.terminate()
            try: process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=8)
        if worker: stop(worker)
        for record in outputs():
            if 'encoded' in record: Path(record['encoded']).unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        for sig, handler in handlers.items(): signal.signal(sig, handler)
        # Preserve caches and logs after failures for diagnosis. All remain in
        # the per-user cache, never beside installed program files.


if __name__ == '__main__':
    raise SystemExit(main())
