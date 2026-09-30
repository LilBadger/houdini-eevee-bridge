"""Lazy EEVEE worker supervision for ordinary Houdini UI sessions."""
import atexit
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import time
import hou
from hde_runtime import cache_root, new_session
from start_worker import start, stop, is_running, exit_code


def activate():
    if not hou.isUIAvailable() or getattr(hou.session, '_hde_installation', None):
        return
    # Development launchers already own their explicitly started worker.
    if os.environ.get('HDEEVEE_ENDPOINT') or os.environ.get('HDEEVEE_SOCKET'):
        return
    session = new_session('houdini')
    for name, value in {'HDEEVEE_SESSION_DIR': str(session),
                        'HDEEVEE_ENDPOINT': str(session / 'endpoint.json')}.items():
        os.environ[name] = value
        hou.putenv(name, value)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='eevee-start')
    state = {'worker': None, 'future': None, 'last_check': 0., 'attempts': 0, 'stopped': False,
             'status': None, 'status_mtime': 0, 'unseen_since': None}
    # Free the worker's GPU memory once no viewport shows EEVEE: soon after a LOP viewport
    # switches to another renderer, later when no viewport shows the LOP scene at all
    # (while working in SOPs, say), so diving into a network does not force a restart.
    # HDEEVEE_IDLE_EXIT_SECONDS=0 keeps the worker running for the whole session.
    idle_exit = float(os.environ.get('HDEEVEE_IDLE_EXIT_SECONDS', '0.5') or 0)
    offscreen_exit = float(os.environ.get('HDEEVEE_OFFSCREEN_EXIT_SECONDS', '30') or 0)
    hou.session._hde_installation = state

    def finish():
        state['stopped'] = True
        future = state['future']
        if future:
            future.add_done_callback(lambda f: stop(f.result()) if not f.exception() else None)
        if state['worker']:
            stop(state['worker'])
        executor.shutdown(wait=False, cancel_futures=True)
    atexit.register(finish)

    def report_status():
        # The render delegates write their state here; show problems and the
        # one-time shader compilation in Houdini's status bar.
        path = Path(os.environ['HDEEVEE_SESSION_DIR']) / 'status.json'
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            return
        if mtime == state['status_mtime']:
            return
        state['status_mtime'] = mtime
        try:
            status = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        key = (status.get('state'), status.get('message'))
        if key == state['status']:
            return
        previous, state['status'] = state['status'], key
        if key[0] == 'error':
            hou.ui.setStatusMessage('EEVEE: ' + (key[1] or 'rendering failed'), hou.severityType.Error)
        elif key[0] == 'compiling':
            hou.ui.setStatusMessage('EEVEE: compiling shaders for this scene (first draw only)…')
        elif key[0] == 'converged' and previous and previous[0] in ('error', 'compiling', 'starting'):
            hou.ui.setStatusMessage('')

    def tick():
        now = time.monotonic()
        if state['stopped'] or now - state['last_check'] < .25:
            return
        state['last_check'] = now
        scene_viewers = [v for v in hou.ui.paneTabs() if isinstance(v, hou.SceneViewer) and v.isViewingSceneGraph()]
        viewers = [v for v in scene_viewers if 'eevee' in v.currentHydraRenderer().lower()]
        if state['future'] and state['future'].done():
            future, state['future'] = state['future'], None
            try:
                state['worker'] = future.result()
                log = cache_root() / 'logs'
                log.mkdir(exist_ok=True)
                (log / ('houdini-' + str(os.getpid()) + '.json')).write_text(
                    json.dumps(state['worker'], indent=2), encoding='utf-8')
                # Render delegates keep retrying until the worker is ready, so
                # no renderer restart (and second scene upload) is needed.
                hou.ui.setStatusMessage('EEVEE ready on ' + str(state['worker'].get('gpu', 'GPU')))
            except Exception as exc:
                print('[EEVEE] ' + str(exc), flush=True)
                hou.ui.setStatusMessage('EEVEE could not start. Run install.py --doctor; see the Python shell for details.',
                                        hou.severityType.Error)
        worker = state['worker']
        # Houdini keeps a renderer alive in the background when its viewport switches to
        # Houdini VK/GL or leaves the LOP scene, so the worker cannot see that EEVEE is
        # gone. Stop it here. A viewport showing EEVEE again starts a new worker, and the
        # background renderer reconnects and sends its scene again.
        if viewers or not worker or not is_running(worker) or idle_exit <= 0:
            state['unseen_since'] = None
        else:
            state['unseen_since'] = state['unseen_since'] or now
            delay = idle_exit if scene_viewers else offscreen_exit
            if delay > 0 and now - state['unseen_since'] >= delay:
                stop(worker)
                state.update(worker=None, attempts=0, unseen_since=None)
                worker = None
                hou.ui.setStatusMessage('EEVEE freed its GPU memory; it starts again when a viewport uses EEVEE.')
        if worker and not is_running(worker):
            code = exit_code(worker)
            stop(worker)
            state['worker'] = None
            if code == 0:
                # It exited by itself once no viewport used EEVEE, which frees its
                # GPU memory (HDEEVEE_IDLE_EXIT_SECONDS). That is not a failure.
                state['attempts'] = 0
                if not viewers:
                    hou.ui.setStatusMessage('EEVEE freed its GPU memory; it starts again when a viewport uses EEVEE.')
            else:
                # Delegates reconnect to a replacement worker and replay their scene.
                hou.ui.setStatusMessage('EEVEE worker exited; restarting it.', hou.severityType.Warning)
        if viewers and not state['worker'] and not state['future'] and state['attempts'] < 3:
            state['attempts'] += 1
            environment = os.environ.copy()
            environment['HDEEVEE_IDLE_EXIT_SECONDS'] = str(idle_exit)
            # Ask Houdini's own GPU users (viewport and texture caches, OpenCL, COPs, Karma XPU)
            # to return memory they hold but don't need, before Blender allocates its own.
            try:
                freed = hou.hscript('gpumem -f 100000')[0].strip()
                if freed:
                    print('[EEVEE] Before starting: ' + freed, flush=True)
            except hou.Error:
                pass
            state['future'] = executor.submit(start, parent=os.getpid(), environment=environment)
            hou.ui.setStatusMessage('Starting Blender EEVEE…')
        if viewers:
            report_status()
    hou.ui.addEventLoopCallback(tick)
