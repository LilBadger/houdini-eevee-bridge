"""Persistent background EEVEE renderer serving one or more Houdini viewports.

The process owns the GPU context, the drawing areas and shared caches. Every
authenticated connection gets its own Session (Blender scene), so several
viewports or render delegates can use one worker concurrently. Requests are
processed one at a time in arrival order.
"""
import argparse
import json
import os
import pathlib
import secrets
import selectors
import signal
import socket
import sys
import time
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'tools'))
from hde_runtime import process_alive
if os.environ.get('HDEEVEE_PYTHON_DEPS'):
    sys.path.insert(0, os.environ['HDEEVEE_PYTHON_DEPS'])
import bpy
import gpu
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import picking
from material_pool import MaterialPool
from protocol import (PROTOCOL, SUPPORTED, ProtocolError, read_frame, send_frame,
                      remove_stale_segments)
from session import Session

VERSION = '0.7.4'
# Houdini's supervisor (tools/hde_installation.py) sets this: once no viewport has
# been connected for this many seconds, the worker exits so that all of its GPU
# memory returns to the driver, for Karma XPU for example. The supervisor starts a
# new worker when a viewport shows EEVEE again. 0 keeps the worker running.
IDLE_EXIT_SECONDS = float(os.environ.get('HDEEVEE_IDLE_EXIT_SECONDS') or 0)
SETTING_GROUPS = ('eevee', 'eevee.ray_tracing_options', 'render', 'render.image_settings',
                  'view_settings', 'display_settings')


class AuthenticationError(ProtocolError):
    pass


def resolve_owner(root, path):
    owner = root
    for part in path.split('.'):
        owner = getattr(owner, part)
    return owner


def copy_settings(source, target):
    """Give a new session scene the factory scene's render defaults."""
    for group in SETTING_GROUPS:
        src, dst = resolve_owner(source, group), resolve_owner(target, group)
        for prop in src.bl_rna.properties:
            if prop.identifier == 'rna_type' or prop.is_readonly or prop.type in ('POINTER', 'COLLECTION'):
                continue
            try:
                value = getattr(src, prop.identifier)
                if getattr(dst, prop.identifier) != value:
                    setattr(dst, prop.identifier, value)
            except (AttributeError, TypeError, ValueError):
                pass


def view3d(area):
    region = next(r for r in area.regions if r.type == 'WINDOW')
    return area, region, area.spaces.active


class Worker:
    def __init__(self):
        gpu.init()
        self.window = bpy.context.window_manager.windows[0]
        areas = list(self.window.screen.areas)
        main = next(a for a in areas if a.type == 'VIEW_3D')
        others = [a for a in areas if a is not main]
        if len(others) < 2:
            raise RuntimeError('The factory screen needs two spare areas for ID and preview drawing')
        # Separate 3D view spaces keep the Workbench ID/preview settings from
        # invalidating the EEVEE view's accumulation.
        others[0].type = 'VIEW_3D'
        others[1].type = 'VIEW_3D'
        self.eevee_area, self.eevee_region, self.eevee_space = view3d(main)
        self.id_area, self.id_region, self.id_space = view3d(others[0])
        self.preview_area, self.preview_region, self.preview_space = view3d(others[1])
        space = self.eevee_space
        space.shading.type = 'RENDERED'
        space.shading.use_scene_world_render = True
        space.shading.use_scene_lights_render = True
        space.overlay.show_overlays = False
        space.show_gizmo = False
        picking.configure_id_space(self.id_space)
        picking.configure_preview_space(self.preview_space)
        # The ID pass must not be antialiased; this preference also covers
        # Blender's viewport SMAA used by offscreen Workbench drawing.
        bpy.context.preferences.system.viewport_aa = 'OFF'
        # Blender's GPU (OpenSubdiv) subdivision keeps large per-mesh
        # evaluation buffers: about 10 GB for a production shot whose CPU
        # subdivided result needs under 1 GB. CPU subdivision is the default;
        # HDEEVEE_GPU_SUBDIVISION=1 re-enables it (faster for deforming meshes).
        bpy.context.preferences.system.use_gpu_subdivision = os.environ.get('HDEEVEE_GPU_SUBDIVISION') == '1'
        first = bpy.context.scene
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        first.render.engine = 'BLENDER_EEVEE'
        self.template = bpy.data.scenes.new('hde-template')
        copy_settings(first, self.template)
        self.idle_scenes = [first]
        self.materials = MaterialPool()
        self.sessions = {}
        self.session_count = 0
        self.gpu = gpu.platform.renderer_get()
        print(json.dumps({'event': 'ready', 'protocol': PROTOCOL, 'version': VERSION, 'engine': first.render.engine,
                          'blender': bpy.app.version_string, 'gpu': self.gpu,
                          'backend': gpu.platform.backend_type_get()}), flush=True)

    def set_texture_limit(self, pixels):
        """Longest texture side on the GPU; aspect ratios are kept. Worker-wide: disk
        renders use their own workers. Blender applies it to viewport and final renders."""
        # Blender offers fixed sizes; a studio value such as 3000 uses the next smaller one.
        sizes = (8192, 4096, 2048, 1024, 512)
        wanted = 'CLAMP_OFF' if pixels <= 0 else 'CLAMP_%d' % next((s for s in sizes if s <= pixels), 512)
        system = bpy.context.preferences.system
        if system.gl_texture_limit == wanted:
            return
        system.gl_texture_limit = wanted
        # Existing GPU textures keep their size until they are uploaded again. EEVEE
        # instances still reference the old ones, so their render targets go too.
        for image in bpy.data.images:
            image.gl_free()
        for session in self.sessions.values():
            session.release_targets()

    def open_session(self):
        self.session_count += 1
        if self.idle_scenes:
            scene = self.idle_scenes.pop()
            copy_settings(self.template, scene)
        else:
            scene = bpy.data.scenes.new('hde-session')
            copy_settings(self.template, scene)
        session = Session(self, self.session_count, scene)
        self.sessions[session.number] = session
        return session

    def close_session(self, session):
        try:
            session.close()
        finally:
            self.sessions.pop(session.number, None)
            self.idle_scenes.append(session.scene)
            self.materials.trim()


class _Offscreens:
    """Old probes free `worker.offscreen` before Blender exits."""
    def __init__(self, session):
        self.session = session

    def __bool__(self):
        return bool(self.session.offscreens)

    def free(self):
        for _, offscreen in self.session.offscreens.values():
            offscreen.free()
        self.session.offscreens.clear()


class EeveeWorker:
    """In-process single-session interface used by the regression probes.

    It keeps the pre-0.6 API: attributes of one Session plus render() returning
    (metadata, color bytes, depth bytes, concatenated pass bytes).
    """
    def __init__(self):
        self._worker = Worker()
        self._session = self._worker.open_session()

    def __getattr__(self, name):
        return getattr(self._session, name)

    @property
    def space(self):
        return self._worker.eevee_space

    @property
    def offscreen(self):
        return _Offscreens(self._session)

    @offscreen.setter
    def offscreen(self, value):
        if value is None:
            _Offscreens(self._session).free()

    def mesh(self, update):
        import meshes
        meshes.sync(self._session, update)

    def render(self, request):
        metadata, parts = self._session.render({**request, 'protocol': 1, 'transport': 'inline'})
        data = b''.join(bytes(memoryview(part)) for part in parts)
        color_end = metadata['color_bytes']
        depth_end = color_end + metadata['depth_bytes']
        aov_end = depth_end + sum(a['bytes'] for a in metadata['aov_buffers'])
        return metadata, data[:color_end], data[color_end:depth_end], data[depth_end:aov_end]


class Client:
    def __init__(self, worker, conn):
        self.worker = worker
        self.conn = conn
        self.session = None

    def ensure_session(self):
        if self.session is None:
            self.session = self.worker.open_session()
        return self.session

    def close(self):
        if self.session is not None:
            session, self.session = self.session, None
            try:
                self.worker.close_session(session)
            except Exception:
                traceback.print_exc()
        try:
            self.conn.close()
        except OSError:
            pass


class Server:
    def __init__(self, worker, server, token, owner_pid):
        self.worker = worker
        self.server = server
        self.token = token
        self.owner_pid = owner_pid
        self.running = True
        self.selector = selectors.DefaultSelector()
        self.selector.register(server, selectors.EVENT_READ, None)
        self.used = False            # a viewport has connected at least once
        self.unused_since = None
        self.idle_exit = False

    def alive(self):
        return not self.owner_pid or process_alive(self.owner_pid)

    def unused(self):
        """True once no viewport has been connected for IDLE_EXIT_SECONDS. A worker
        that never had a viewport keeps waiting for the one it was started for."""
        if self.worker.sessions:
            self.used, self.unused_since = True, None
            return False
        if IDLE_EXIT_SECONDS <= 0 or not self.used:
            return False
        now = time.monotonic()
        if self.unused_since is None:
            self.unused_since = now
        return now - self.unused_since >= IDLE_EXIT_SECONDS

    def authenticate(self, header):
        if self.token and not secrets.compare_digest(str(header.get('token', '')), self.token):
            raise AuthenticationError('Worker authentication failed')
        if header.get('protocol') not in SUPPORTED:
            raise ProtocolError('Unsupported protocol version')

    def serve(self):
        while self.running:
            if not self.alive():
                break
            if self.unused():
                print('[EEVEE] No viewport has used this worker for %g s; exiting to free its GPU memory.'
                      % IDLE_EXIT_SECONDS, flush=True)
                self.idle_exit = True
                break
            # Settled viewports send no requests; release idle targets here.
            for session in list(self.worker.sessions.values()):
                session.release_idle_targets()
            # While an idle exit is pending, look again soon rather than after a second.
            for key, _ in self.selector.select(timeout=0.1 if self.unused_since is not None else 1.0):
                if key.data is None:
                    self.accept()
                    continue
                client = key.data
                keep = False
                try:
                    keep = self.handle(client)
                except (EOFError, ConnectionError, OSError):
                    keep = False
                except ProtocolError as exc:
                    self.reply_error(client, exc)
                    keep = False
                if not keep:
                    self.selector.unregister(client.conn)
                    client.close()
                if not self.running:
                    break
        for key in list(self.selector.get_map().values()):
            if key.data is not None:
                key.data.close()

    def accept(self):
        conn, _ = self.server.accept()
        if conn.family == socket.AF_INET:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.settimeout(1)
        self.selector.register(conn, selectors.EVENT_READ, Client(self.worker, conn))

    @staticmethod
    def reply_error(client, exc):
        try:
            send_frame(client.conn, {'ok': False, 'error': str(exc)})
        except OSError:
            pass

    def handle(self, client):
        request, _ = read_frame(client.conn, self.alive, self.authenticate)
        op = request.get('op')
        if os.environ.get('HDEEVEE_DUMP_SCENE') and op in ('update', 'render'):
            dump(request, op)
        try:
            if op == 'shutdown':
                send_frame(client.conn, {'ok': True})
                self.running = False
            elif op == 'ping':
                send_frame(client.conn, {'ok': True, 'protocol': PROTOCOL, 'version': VERSION,
                                         'engine': 'BLENDER_EEVEE', 'gpu': self.worker.gpu,
                                         'sessions': len(self.worker.sessions)})
            elif op == 'set_owner':
                requested = int(request['pid'])
                if requested <= 0 or self.owner_pid not in (0, requested):
                    raise ValueError('Invalid worker owner')
                if not process_alive(requested):
                    raise ValueError('Worker owner does not exist')
                self.owner_pid = requested
                send_frame(client.conn, {'ok': True, 'owner_pid': self.owner_pid})
            elif op == 'hello':
                self.adopt_owner(request)
                session = client.ensure_session()
                send_frame(client.conn, {'ok': True, 'protocol': PROTOCOL, 'version': VERSION,
                                         'session': session.number, 'gpu': self.worker.gpu,
                                         'features': ['binary', 'shm', 'ids', 'depth', 'preview', 'sessions']})
            elif op == 'update':
                self.adopt_owner(request)
                session = client.ensure_session()
                if request.get('reset'):
                    session.reset()
                session.configure(request.get('config') or {}, request)
                errors = session.update(request.get('changes', []))
                reply = {'ok': True, 'applied': len(request.get('changes', []))}
                if errors:
                    reply['errors'] = errors
                send_frame(client.conn, reply)
            elif op == 'render':
                self.adopt_owner(request)
                session = client.ensure_session()
                metadata, parts = session.render(request)
                metadata['protocol'] = request.get('protocol', PROTOCOL)
                send_frame(client.conn, metadata, parts)
            else:
                raise ValueError('Unknown operation: ' + str(op))
        except (EOFError, ConnectionError, ProtocolError):
            raise
        except Exception as exc:
            traceback.print_exc()
            send_frame(client.conn, {'ok': False, 'error': str(exc) or type(exc).__name__})
        return self.running

    def adopt_owner(self, request):
        if not self.owner_pid and request.get('owner_pid'):
            self.owner_pid = int(request['owner_pid'])


def dump(request, op):
    """Developer aid: record received scene edits for offline replay/profiling."""
    import pickle
    directory = pathlib.Path(os.environ['HDEEVEE_DUMP_SCENE'])
    directory.mkdir(parents=True, exist_ok=True)
    record = {k: v for k, v in request.items() if k != 'token'}
    if op == 'update':
        count = len(list(directory.glob('update-*.pickle')))
        with open(directory / ('update-%05d.pickle' % count), 'wb') as stream:
            pickle.dump(record, stream, protocol=pickle.HIGHEST_PROTOCOL)
    else:
        record.pop('changes', None)
        with open(directory / 'render-last.pickle', 'wb') as stream:
            pickle.dump(record, stream, protocol=pickle.HIGHEST_PROTOCOL)


def main():
    parser = argparse.ArgumentParser()
    endpoint = parser.add_mutually_exclusive_group(required=True)
    endpoint.add_argument('--socket', help='Legacy Unix socket; Linux development only')
    endpoint.add_argument('--endpoint-file', help='Private descriptor for authenticated loopback TCP')
    parser.add_argument('--parent', type=int, default=0)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    path = pathlib.Path(args.endpoint_file or args.socket)
    if args.socket and len(os.fsencode(path)) >= 104:
        raise ValueError('Unix socket path is too long')
    os.umask(0o077)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError('Refusing to replace an existing worker endpoint: ' + str(path))
    removed = remove_stale_segments(process_alive)
    if removed:
        print('[EEVEE] Removed ' + str(removed) + ' stale shared-memory segments', flush=True)
    worker = Worker()
    token = secrets.token_hex(32) if args.endpoint_file else None
    listener = socket.socket(socket.AF_INET if args.endpoint_file else socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0) if args.endpoint_file else str(path))
    listener.listen(8)
    if args.endpoint_file:
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'protocol': PROTOCOL, 'host': '127.0.0.1',
                                         'port': listener.getsockname()[1], 'token': token, 'pid': os.getpid()}))
        if os.name != 'nt':
            temporary.chmod(0o600)
        temporary.replace(path)
    server = Server(worker, listener, token, args.parent)
    # Terminating the worker must still unlink its shared-memory segments.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        server.serve()
    finally:
        for session in list(worker.sessions.values()):
            try:
                session.close()
            except Exception:
                traceback.print_exc()
        listener.close()
        path.unlink(missing_ok=True)
    if server.idle_exit:
        # Nothing is left to save; ending the process now returns the GPU memory at
        # once, instead of after Blender frees every data-block and GPU resource.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)


if __name__ == '__main__':
    main()
