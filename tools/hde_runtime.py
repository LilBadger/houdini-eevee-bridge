"""Portable paths and process utilities shared by Houdini, Blender and the installer."""
import ctypes
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.7.3'


def settings(environment=None):
    env = os.environ if environment is None else environment
    path = Path(env.get('HDEEVEE_CONFIG', ROOT / 'installation.json'))
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def cache_root():
    override = os.environ.get('HDEEVEE_CACHE_ROOT')
    if override:
        path = Path(override)
    elif os.name == 'nt':
        path = Path(os.environ.get('LOCALAPPDATA', tempfile.gettempdir())) / 'HoudiniEEVEE'
    else:
        path = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'houdini-eevee'
    path.mkdir(parents=True, exist_ok=True)
    return path


def newest_change(path):
    newest = path.stat().st_mtime
    for directory, _, files in os.walk(path):
        for name in files:
            try:
                newest = max(newest, os.stat(os.path.join(directory, name)).st_mtime)
            except OSError:
                pass
    return newest


def prune_cache(now=None):
    """Remove session folders and logs that nothing has touched for days.

    Session folders hold per-session volume and texture caches and are kept for
    diagnosis, so without pruning they grow without bound. Runs at most hourly.
    Files a running process still has open cannot be removed on Windows."""
    root = cache_root()
    now = time.time() if now is None else now
    marker = root / '.pruned'
    try:
        if now - marker.stat().st_mtime < 3600:
            return 0
    except OSError:
        pass
    marker.write_text(str(now), encoding='utf-8')
    session_age = float(os.environ.get('HDEEVEE_KEEP_SESSION_DAYS', 7)) * 86400
    log_age = float(os.environ.get('HDEEVEE_KEEP_LOG_DAYS', 14)) * 86400
    removed = 0
    sessions = root / 'sessions'
    for entry in (sessions.iterdir() if sessions.is_dir() else ()):
        try:
            if entry.is_dir() and now - newest_change(entry) > session_age:
                shutil.rmtree(entry, ignore_errors=True)
                removed += not entry.exists()
        except OSError:
            pass
    logs = root / 'logs'
    for entry in (logs.iterdir() if logs.is_dir() else ()):
        try:
            if entry.is_file() and now - entry.stat().st_mtime > log_age:
                entry.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def new_session(label='worker'):
    base = cache_root() / 'sessions'
    base.mkdir(exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=label+'-', dir=base))
    if os.name != 'nt':
        path.chmod(0o700)
    return path


def session_dir():
    value = os.environ.get('HDEEVEE_SESSION_DIR')
    if value:
        path = Path(value)
        path.mkdir(parents=True, exist_ok=True)
        return path
    path = cache_root() / 'sessions' / ('process-'+str(os.getpid()))
    path.mkdir(parents=True, exist_ok=True)
    return path


def houdini_root():
    value = os.environ.get('HFS') or os.environ.get('HDEEVEE_HOUDINI') or settings().get('houdini')
    if not value:
        raise RuntimeError('Houdini was not found. Run the installer with --houdini PATH.')
    path = Path(value)
    if not (path / 'toolkit/cmake/HoudiniConfig.cmake').is_file():
        raise RuntimeError('Invalid Houdini installation: '+str(path))
    return path


def houdini_build(root):
    """The exact build of a Houdini installation, for example '22.0.368', from its HDK."""
    file = Path(root) / 'toolkit/cmake/HoudiniConfigVersion.cmake'
    match = re.search(r'set\(\s*PACKAGE_VERSION\s+(\d+\.\d+\.\d+)', file.read_text()) if file.is_file() else None
    return match[1] if match else None


def native_supports(info, build, configuration=None):
    """Whether a native plugin, described by its build-info.json, may run in Houdini
    `build`: the build it was compiled for, or another build on which the installer
    verified it with a test render (installation.json native_houdini_version)."""
    configuration = settings() if configuration is None else configuration
    if info.get('system') != platform.system():
        return False
    return info.get('houdini_version') == build or (
        configuration.get('houdini_version') == build and
        configuration.get('native_houdini_version') == info.get('houdini_version'))


def houdini_program(name):
    return houdini_root() / 'bin' / (name + ('.exe' if os.name == 'nt' else ''))


def houdini_python(root=None):
    root = Path(root) if root else houdini_root()
    override = os.environ.get('HDEEVEE_PYTHON')
    if override:
        return Path(override)
    if os.name == 'nt':
        candidates = [root/'python313/python.exe', root/'python/python.exe', root/'python/bin/python.exe']
        candidates.extend(root.glob('python*/python.exe'))
    else:
        candidates = sorted((root/'python/bin').glob('python3.[0-9]*'))
    for path in candidates:
        if path.is_file() and not path.name.endswith('-config'):
            return path
    # hython is available on every supported Houdini installation.
    return root / 'bin' / ('hython.exe' if os.name == 'nt' else 'hython')


def blender_program():
    value = os.environ.get('HDEEVEE_BLENDER') or settings().get('blender') or shutil.which('blender')
    if value and Path(value).is_file():
        return Path(value)
    raise RuntimeError('Blender was not found. Run the installer or set HDEEVEE_BLENDER to its executable.')


def command_line(arguments):
    arguments = [str(x) for x in arguments]
    return subprocess.list2cmdline(arguments) if os.name == 'nt' else shlex.join(arguments)


def houdini_environment(root=None, base=None):
    env = dict(os.environ if base is None else base)
    hfs = Path(root) if root else houdini_root()
    env['HFS'] = str(hfs)
    env['PATH'] = str(hfs/'bin') + os.pathsep + env.get('PATH', '')
    if os.name != 'nt':
        paths = [hfs/'dsolib', hfs/'python/lib']
        env['LD_LIBRARY_PATH'] = os.pathsep.join(map(str, paths)) + os.pathsep + env.get('LD_LIBRARY_PATH', '')
    return env


def plugin_directory():
    override = os.environ.get('HDEEVEE_PLUGIN_DIR') or settings().get('plugin_dir')
    for path in ([Path(override)] if override else []) + [ROOT/'plugin/hdEevee', ROOT/'build/plugin/hdEevee']:
        if (path/'resources/plugInfo.json').is_file():
            return path
    raise RuntimeError('The native EEVEE plugin is missing. Run the installer/build for this Houdini version.')


def renderer_environment(base=None):
    env = houdini_environment(base=base)
    plugin = plugin_directory()
    manifest = plugin/'build-info.json'
    if manifest.is_file():
        info = json.loads(manifest.read_text(encoding='utf-8'))
        if not native_supports(info, houdini_build(env['HFS'])):
            raise RuntimeError('EEVEE native plugin does not match this Houdini build/OS. Reinstall or rebuild for '+env['HFS'])
    env['HDEEVEE_ROOT'] = str(ROOT)
    env['HDEEVEE_PROJECT'] = str(ROOT)  # Compatibility with development tools.
    for name, path in [('HOUDINI_PATH', ROOT/'houdini'), ('PYTHONPATH', ROOT/'tools'),
                       ('PXR_PLUGINPATH_NAME', plugin/'resources')]:
        env[name] = str(path) + os.pathsep + env.get(name, '&' if name == 'HOUDINI_PATH' else '')
    return env


def volume_helper():
    name = 'hde_volume' + ('.exe' if os.name == 'nt' else '')
    override = os.environ.get('HDEEVEE_VOLUME_HELPER') or settings().get('volume_helper')
    for path in ([Path(override)] if override else []) + [ROOT/'bin'/name, ROOT/'build/bin'/name]:
        if path.is_file():
            return path
    raise RuntimeError('The EEVEE volume helper is missing; reinstall/build the bridge.')


def blender_environment(base=None):
    env = dict(os.environ if base is None else base)
    for name in ('LD_LIBRARY_PATH', 'PYTHONPATH', 'PYTHONHOME', 'QT_PLUGIN_PATH',
                 'QT_QPA_PLATFORM_PLUGIN_PATH', 'OCIO'):
        env.pop(name, None)
    # Houdini's DLL directory must not shadow Blender's own libraries.
    hfs = env.get('HFS')
    if hfs and os.name == 'nt':
        root = os.path.normcase(os.path.abspath(hfs)) + os.sep
        env['PATH'] = os.pathsep.join(p for p in env.get('PATH', '').split(os.pathsep)
                                    if not os.path.normcase(os.path.abspath(p)).startswith(root))
    env['PYTHONUNBUFFERED'] = '1'
    return env


def process_alive(pid):
    if pid <= 0:
        return False
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    # os.kill(pid, 0) terminates processes on Windows. Query a handle instead.
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5  # Access denied is not evidence of exit.
    try:
        code = ctypes.c_uint32()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)


def subprocess_options():
    return {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}


def platform_tag():
    machine = platform.machine().lower()
    return ('windows' if os.name == 'nt' else 'linux') + '-' + ('x86_64' if machine in ('amd64','x86_64') else machine)
