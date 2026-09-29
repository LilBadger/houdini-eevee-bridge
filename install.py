#!/usr/bin/env python3
"""Per-user Linux/Windows installer. No changes to Houdini or Blender installs."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).resolve().parent
sys.path.insert(0, str(SOURCE/'tools'))
from hde_runtime import (VERSION, blender_environment, cache_root, houdini_build, houdini_environment,
                         houdini_python, platform_tag)


def run(command, env=None, timeout=None):
    print('> '+subprocess.list2cmdline(list(map(str, command))), flush=True)
    subprocess.run(list(map(str, command)), env=env, check=True, timeout=timeout)


def version(hfs):
    if not (Path(hfs)/'toolkit/cmake/HoudiniConfigVersion.cmake').is_file():
        raise RuntimeError('Not a Houdini SDK installation: '+str(hfs))
    build = houdini_build(hfs)
    if not build or not build.startswith('22.0.'):
        raise RuntimeError('This release requires Houdini 22.0 with its HDK installed.')
    return build


def choose(candidates, label, option, explicit=None):
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.exists(): raise RuntimeError(label+' does not exist: '+str(path))
        return path
    found = sorted({Path(p).resolve() for p in candidates if p and Path(p).exists()})
    if len(found) == 1: return found[0]
    if not found: raise RuntimeError(label+' was not found. Supply '+option+' PATH.')
    raise RuntimeError('Multiple '+label+' installations found; select one with '+option+':\n'+'\n'.join(map(str,found)))


def find_houdini(explicit):
    """Any Houdini 22.0 build: the one named by --houdini, HDEEVEE_HOUDINI or HFS, else the newest installed."""
    preferred = explicit or os.environ.get('HDEEVEE_HOUDINI') or os.environ.get('HFS')
    if preferred and (explicit or str(houdini_build(preferred) or '').startswith('22.0.')):
        return choose([],'Houdini','--houdini',preferred)
    if os.name == 'nt':
        candidates = list((Path(os.environ.get('ProgramFiles','C:/Program Files'))/'Side Effects Software').glob('Houdini 22.0.*'))
    else:
        candidates = list(Path('/opt').glob('hfs22.0.*')) + list(Path.home().glob('houdini-22.0.*'))
    found = {Path(p).resolve(): houdini_build(p) for p in candidates if str(houdini_build(p) or '').startswith('22.0.')}
    if not found: raise RuntimeError('Houdini 22.0 was not found. Supply --houdini PATH.')
    number = lambda build: tuple(int(part) for part in build.split('.'))
    newest = max(found, key=lambda path: number(found[path]))
    if len(found) > 1:
        print('Found Houdini '+', '.join(sorted(found.values(), key=number))+'; installing for the newest, '+found[newest]+
              '. Use --houdini PATH for another build.',flush=True)
    return newest


def find_blender(explicit):
    preferred = explicit or os.environ.get('HDEEVEE_BLENDER')
    candidates = [shutil.which('blender')]
    if os.name == 'nt':
        candidates += list((Path(os.environ.get('ProgramFiles','C:/Program Files'))/'Blender Foundation').glob('Blender 5.2*/blender.exe'))
    return choose(candidates,'Blender','--blender',preferred)


def packages_directory(explicit=None):
    if explicit: return Path(explicit).expanduser().resolve()
    if os.environ.get('HOUDINI_USER_PREF_DIR'):
        return Path(os.environ['HOUDINI_USER_PREF_DIR'].replace('__HVER__','22.0'))/'packages'
    if os.name == 'nt':
        # The Windows Documents known folder also respects OneDrive/redirection.
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None,5,None,0,buffer) != 0:
            raise RuntimeError('Cannot locate Windows Documents; use --packages-dir.')
        return Path(buffer.value)/'houdini22.0/packages'
    return Path.home()/'houdini22.0/packages'


def probe_blender(executable, deps=None):
    env = blender_environment()
    deps = deps or os.environ.get('HDEEVEE_PYTHON_DEPS')
    if deps: env['HDEEVEE_PYTHON_DEPS'] = str(deps)
    else: env.pop('HDEEVEE_PYTHON_DEPS',None)
    result = subprocess.run([str(executable),'--background','--factory-startup','--python-exit-code','1',
        '--python',str(SOURCE/'tools/check_blender.py')],env=env,capture_output=True,text=True,
        encoding='utf-8',errors='replace',timeout=90)
    marker = next((line.split('=',1)[1] for line in result.stdout.splitlines() if line.startswith('HDEEVEE_CHECK=')),None)
    if result.returncode or not marker:
        raise RuntimeError('Blender compatibility check failed:\n'+(result.stdout+result.stderr)[-5000:])
    info = json.loads(marker)
    if info['version'][:2] != [5,2] or info['incompatible'] or not info['openvdb']:
        raise RuntimeError('Use Blender 5.2 with EEVEE and OpenVDB support. Details: '+json.dumps(info))
    return info


def install_dependencies(info, target):
    python = info['python']
    if not python: raise RuntimeError('Blender Python was not found; cannot install private OpenEXR dependencies.')
    env = blender_environment()
    requirements = ['OpenEXR>=3.3,<4']
    if 'numpy' in info['missing']: requirements.append('numpy>=1.26,<3')
    # Reuse Blender's bundled pip wheel without installing into its runtime.
    bootstrap = ('import glob,runpy,sys; '
        'w=glob.glob(sys.prefix+"/lib/python*/ensurepip/_bundled/pip-*.whl")+'
        'glob.glob(sys.prefix+"/Lib/ensurepip/_bundled/pip-*.whl"); '
        'sys.path[:0]=w; runpy.run_module("pip",run_name="__main__")')
    try:
        run([python,'-c',bootstrap,'install','--disable-pip-version-check','--only-binary=:all:',
             '--no-deps','--target',target,*requirements],env=env,timeout=300)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError('Private Blender dependency installation failed. Use a Blender build whose Python has matching OpenEXR wheels, or supply matching OpenEXR/Imath/NumPy modules through HDEEVEE_PYTHON_DEPS. No render passes were disabled. Python 3.14 currently has no OpenEXR PyPI wheel.') from exc


def native_build(root):
    """The Houdini 22.0 build a prebuilt native plugin in root was compiled for, or None."""
    manifest = root/'plugin/hdEevee/build-info.json'
    suffix = '.dll' if os.name == 'nt' else '.so'
    if not manifest.exists(): return None
    info = json.loads(manifest.read_text(encoding='utf-8'))
    usable = (str(info.get('houdini_version','')).startswith('22.0.') and info.get('system') == platform.system()
        and info.get('architecture','').lower() in ('amd64','x86_64')
        and (root/('plugin/hdEevee/lib/hdEevee'+suffix)).is_file()
        and (root/('bin/hde_volume'+('.exe' if os.name == 'nt' else ''))).is_file())
    return info['houdini_version'] if usable else None


def find_cmake():
    """CMake able to build the plugin, or None. On Windows this also needs a Visual Studio
    C++ toolset; CMake from PATH, its installer or the copy bundled with Visual Studio works."""
    if os.name != 'nt':
        return shutil.which('cmake')
    vswhere = Path(os.environ.get('ProgramFiles(x86)','C:/Program Files (x86)'))/'Microsoft Visual Studio/Installer/vswhere.exe'
    if not vswhere.is_file(): return None
    result = subprocess.run([str(vswhere),'-latest','-products','*','-requires','Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
                             '-property','installationPath'],capture_output=True,text=True)
    studio = result.stdout.strip().splitlines()[0] if result.returncode == 0 and result.stdout.strip() else None
    if not studio: return None
    candidates = [shutil.which('cmake'), Path(os.environ.get('ProgramFiles','C:/Program Files'))/'CMake/bin/cmake.exe',
                  Path(studio)/'Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe']
    return next((str(path) for path in candidates if path and Path(path).is_file()),None)


def package(root, wanted):
    return {'enable':"houdini_version == '"+wanted+"' and houdini_os == '"+('windows' if os.name=='nt' else 'linux')+"'",
        'load_package_once':True, 'hpath':str(root/'houdini').replace('\\','/'),
        'env':[{'HDEEVEE_ROOT':{'value':str(root).replace('\\','/'),'method':'replace'}},
               {'HDEEVEE_AUTO_WORKER':{'value':'1','method':'replace'}},
               {'PYTHONPATH':str(root/'tools').replace('\\','/')},
               {'PXR_PLUGINPATH_NAME':str(root/'plugin/hdEevee/resources').replace('\\','/')} ]}


def default_prefix(wanted):
    base = (Path(os.environ.get('LOCALAPPDATA',Path.home()))/'HoudiniEEVEE' if os.name=='nt'
            else Path(os.environ.get('XDG_DATA_HOME',Path.home()/'.local/share'))/'houdini-eevee')
    return base/(VERSION+'-h'+wanted)


def install(args):
    hfs = find_houdini(args.houdini)
    wanted = version(hfs)
    blender = find_blender(args.blender)
    prefix = Path(args.prefix).expanduser().resolve() if args.prefix else default_prefix(wanted)
    package_file = packages_directory(args.packages_dir)/'houdini_eevee.json'
    if prefix.exists():
        raise RuntimeError('Destination already exists: '+str(prefix)+'\nUse a new --prefix for an update; existing installs are never overwritten while Houdini may be using them.')
    if package_file.exists():
        previous = json.loads(package_file.read_text(encoding='utf-8'))
        if not any('HDEEVEE_ROOT' in item for item in previous.get('env',[]) if isinstance(item,dict)):
            raise RuntimeError('Refusing to replace an unrelated package: '+str(package_file))
    info = probe_blender(blender)
    print('Houdini '+wanted+'; Blender '+info['blender']+'; '+platform_tag(),flush=True)
    # The plugin is compiled against one exact Houdini build. For another 22.0 build,
    # build it from source when a compiler is available; otherwise use a prebuilt
    # plugin only if a test render through it succeeds in this build (see doctor.py).
    prebuilt = [(root,native_build(root)) for root in (SOURCE,SOURCE/'build-portable',SOURCE/'build')]
    prebuilt = [(root,build) for root,build in prebuilt if build]
    exact = next((root for root,build in prebuilt if build == wanted),None)
    other = next(((root,build) for root,build in prebuilt if build != wanted),None)
    cmake = None if args.no_build else find_cmake()
    buildable = bool(cmake) and (SOURCE/'src').is_dir()
    if args.build and not buildable:
        raise RuntimeError('--build needs CMake and a C++ compiler. On Windows install Visual Studio 2022 Build Tools with "Desktop development with C++" (its CMake is found automatically).')
    if exact and not args.build: plan, native = 'prebuilt', exact
    elif buildable: plan, native = 'build', None
    elif other: plan, native = 'verify', other[0]
    else:
        raise RuntimeError('No native plugin for Houdini '+wanted+'. Install CMake and a C++ compiler supported by this Houdini HDK, then rerun; on Windows, Visual Studio 2022 Build Tools with "Desktop development with C++" is enough.')
    if plan == 'build' and not args.build:
        print('No prebuilt plugin for Houdini '+wanted+(' (this package has '+other[1]+')' if other else '')+'; building it with '+cmake+'.',flush=True)
    if plan == 'verify':
        print('No prebuilt plugin for Houdini '+wanted+(' and building is off (--no-build)' if args.no_build else ' and no C++ build tools')+
              '; trying the plugin built for Houdini '+other[1]+
              '. It is registered only if a test render in Houdini '+wanted+' succeeds.',flush=True)
        if args.no_gpu_check:
            raise RuntimeError('A plugin built for Houdini '+other[1]+' must pass a test render in Houdini '+wanted+'; remove --no-gpu-check or install C++ build tools.')
    if args.dry_run:
        print(json.dumps({'install_to':str(prefix),'package':str(package_file),
                          'native':{'prebuilt':str(native),'build':'build with '+str(cmake),
                                    'verify':'verify the plugin built for Houdini '+(other[1] if other else '')}[plan],
                         'missing_blender_modules':info['missing']},indent=2))
        return
    prefix.parent.mkdir(parents=True,exist_ok=True)
    env = houdini_environment(hfs)
    env.update(HDEEVEE_AUTO_WORKER='0', HDEEVEE_DEMO='0', HDEEVEE_AUTO_SELECT='0')
    with tempfile.TemporaryDirectory(prefix='.eevee-install-',dir=prefix.parent) as temp:
        staging = Path(temp)/'payload'
        staging.mkdir()
        # Explicit allowlist: no personal scenes, textures, caches or machine config.
        for name in ('tools','worker','houdini','src','third_party'):
            shutil.copytree(SOURCE/name,staging/name,ignore=shutil.ignore_patterns('__pycache__','backup','*.pyc','*.bak','*.hda.bak'))
        # Development hooks must not mask an artist/studio's scene startup.
        for name in ('123.py','456.py'):
            (staging/'houdini/scripts'/name).unlink(missing_ok=True)
        for name in ('install.py','install.sh','install.cmd','install.ps1','CMakeLists.txt','README.md','INSTALL.md','CHANGELOG.md','LICENSE'):
            if (SOURCE/name).is_file(): shutil.copy2(SOURCE/name,staging/name)
        if plan == 'build':
            build = Path(temp)/'build'
            try:
                # Select the host's installed VS generator on Windows. Ninja is
                # also usable from a compiler-initialized developer shell.
                run([cmake,'-S',SOURCE,'-B',build,'-DCMAKE_BUILD_TYPE=Release',
                     '-DHoudini_DIR='+str(hfs/'toolkit/cmake')],env=env)
                run([cmake,'--build',build,'--config','Release','--parallel',str(args.jobs)],env=env)
                run([cmake,'--install',build,'--config','Release','--prefix',staging],env=env)
            except subprocess.CalledProcessError:
                if args.build or not other: raise
                plan, native = 'verify', other[0]
                print('The build failed; trying the plugin built for Houdini '+other[1]+' instead. It is registered only if a test render in Houdini '+wanted+' succeeds.',flush=True)
                for name in ('plugin','bin'): shutil.rmtree(staging/name,ignore_errors=True)
        if plan != 'build':
            shutil.copytree(native/'plugin',staging/'plugin')
            shutil.copytree(native/'bin',staging/'bin')
        if os.name != 'nt':
            # ZIP extractors do not consistently restore Unix execute bits.
            helper = staging/'bin/hde_volume'
            helper.chmod(helper.stat().st_mode | 0o111)
        assets = staging/'houdini/otls'
        if not all((assets/name).is_file() for name in ('eevee_render_settings.hda','eevee_volume_material.hda')):
            env['HOUDINI_PATH'] = '&'
            env['PYTHONPATH'] = str(staging/'tools')
            run([hfs/'bin'/('hython.exe' if os.name=='nt' else 'hython'),staging/'tools/build_assets.py'],env=env)
        if info['missing']:
            install_dependencies(info,staging/'dependencies')
            info = probe_blender(blender,staging/'dependencies')
            if info['missing']: raise RuntimeError('Blender dependencies still missing: '+str(info['missing']))
        compiled_for = json.loads((staging/'plugin/hdEevee/build-info.json').read_text(encoding='utf-8'))['houdini_version']
        configuration = {'bridge_version':VERSION,'houdini':str(hfs),'houdini_version':wanted,'native_houdini_version':compiled_for,
            'blender':str(blender),'blender_version':info['blender'],'gpu_backend':args.gpu_backend,
            'gpu_device':args.gpu_device, 'gpu_subdivision':bool(args.gpu_subdivision),
            'package_file':str(package_file) if not args.no_register else None}
        if (staging/'dependencies').is_dir(): configuration['python_deps'] = str(prefix/'dependencies')
        elif os.environ.get('HDEEVEE_PYTHON_DEPS'):
            configuration['python_deps'] = str(Path(os.environ['HDEEVEE_PYTHON_DEPS']).resolve())
        (staging/'installation.json').write_text(json.dumps(configuration,indent=2),encoding='utf-8')
        staging.rename(prefix)
    try:
        if not args.no_gpu_check:
            doctor_env = houdini_environment(hfs)
            for key in ('HDEEVEE_CONFIG','HDEEVEE_PLUGIN_DIR','HDEEVEE_VOLUME_HELPER','HDEEVEE_PYTHON_DEPS'):
                doctor_env.pop(key,None)
            result = subprocess.run([str(houdini_python(hfs)),'-E',str(prefix/'tools/doctor.py')],env=doctor_env,
                capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=150)
            (prefix/'doctor-install.log').write_text(result.stdout+result.stderr,encoding='utf-8')
            if result.returncode:
                hint = ('\nThe plugin built for Houdini '+compiled_for+' does not work in Houdini '+wanted+
                        '. Install C++ build tools (on Windows, Visual Studio 2022 Build Tools with "Desktop development with C++") and rerun, so the installer can build it.'
                        if compiled_for != wanted else '')
                raise RuntimeError('Installation copied but NOT registered: validation failed. See '+str(prefix/'doctor-install.log')+hint+'\n'+result.stderr[-3000:])
            print(result.stdout,flush=True)
        generated = package(prefix,wanted)
        (prefix/'houdini_eevee.json').write_text(json.dumps(generated,indent=2),encoding='utf-8')
        if not args.no_register:
            package_file.parent.mkdir(parents=True,exist_ok=True)
            if package_file.exists():
                backup = package_file.with_suffix('.json.previous')
                shutil.copy2(package_file,backup)
                # Also keep a copy per replaced version, so older releases stay
                # restorable after several updates (install.py --rollback VERSION).
                try:
                    replaced = json.loads(package_file.read_text(encoding='utf-8'))
                    root = next(item['HDEEVEE_ROOT']['value'] for item in replaced.get('env',[])
                                if isinstance(item,dict) and 'HDEEVEE_ROOT' in item)
                    shutil.copy2(package_file,package_file.with_name('houdini_eevee.json.'+Path(root).name))
                except (ValueError,StopIteration,KeyError,TypeError):
                    pass
            temporary = package_file.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(generated,indent=2),encoding='utf-8')
            temporary.replace(package_file)
        print('Installed: '+str(prefix)+'\n'+('Package: '+str(package_file) if not args.no_register else 'Not registered (--no-register).'))
        print('Restart Houdini normally, select EEVEE in Solaris, and add EEVEE Render Settings.')
    except Exception:
        print('Files retained for diagnosis at '+str(prefix)+'. Existing Houdini packages were not changed.',file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--houdini',help='Houdini 22 installation directory (HFS)')
    parser.add_argument('--blender',help='Blender 5.2 executable')
    parser.add_argument('--prefix',help='New installation directory')
    parser.add_argument('--packages-dir',help='Houdini packages folder (worker account or studio override)')
    parser.add_argument('--build',action='store_true',help='Build the native plugin against this Houdini SDK')
    parser.add_argument('--no-build',action='store_true',help='Never build; use the prebuilt plugin, verified by a test render if it was built for another Houdini 22.0 build')
    parser.add_argument('--jobs',type=int,default=4)
    parser.add_argument('--gpu-backend',choices=('vulkan','opengl'),default='vulkan')
    parser.add_argument('--gpu-device',default='auto',help='Blender Vulkan GPU ID; default lets Blender choose')
    parser.add_argument('--gpu-subdivision',action='store_true',help='Use Blender GPU subdivision in the viewport (much more video memory; faster for deforming subdivided meshes)')
    parser.add_argument('--no-register',action='store_true',help='Prepare an install without modifying user packages')
    parser.add_argument('--no-gpu-check',action='store_true',help='Prepare on a machine without GPU access; run --doctor on the worker before rendering')
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--doctor',action='store_true',help='Check this installed copy')
    parser.add_argument('--uninstall',action='store_true',help='Unregister this installed copy; keep files and scenes')
    parser.add_argument('--rollback',nargs='?',const='previous',metavar='VERSION',
                        help='Restore the registration the last install replaced, or a replaced VERSION such as 0.5.0-h22.0.368')
    args = parser.parse_args()
    if sys.platform not in ('linux','win32') or platform.machine().lower() not in ('x86_64','amd64'):
        parser.error('This release supports Linux and Windows x86-64.')
    try:
        if args.doctor:
            from doctor import check
            print(json.dumps(check(),indent=2))
        elif args.rollback:
            package_file = packages_directory(args.packages_dir)/'houdini_eevee.json'
            backup = (package_file.with_suffix('.json.previous') if args.rollback == 'previous'
                      else package_file.with_name('houdini_eevee.json.'+args.rollback))
            if not backup.exists(): raise RuntimeError('No previous registration to restore: '+str(backup))
            previous = json.loads(backup.read_text(encoding='utf-8'))
            if not any('HDEEVEE_ROOT' in item for item in previous.get('env',[]) if isinstance(item,dict)):
                raise RuntimeError('The backup is not an EEVEE Bridge registration: '+str(backup))
            temporary = package_file.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(previous,indent=2),encoding='utf-8')
            temporary.replace(package_file)
            root = next(item['HDEEVEE_ROOT']['value'] for item in previous['env'] if isinstance(item,dict) and 'HDEEVEE_ROOT' in item)
            print('Restored the previous registration: '+root+'\nRestart Houdini to use it.')
        elif args.uninstall:
            config = SOURCE/'installation.json'
            if not config.exists(): raise RuntimeError('Run --uninstall from the installed copy.')
            item = json.loads(config.read_text(encoding='utf-8')).get('package_file')
            if item and Path(item).exists():
                installed = json.loads(Path(item).read_text(encoding='utf-8'))
                if installed == package(SOURCE,json.loads(config.read_text(encoding='utf-8'))['houdini_version']):
                    Path(item).unlink()
                    print('Removed package: '+item)
                else: raise RuntimeError('Package now points to another installation; it was left unchanged.')
            print('Unregistered. Files retained at '+str(SOURCE)+'. Restart Houdini before removing this directory.')
        else: install(args)
    except (RuntimeError,subprocess.SubprocessError,OSError,ValueError) as exc:
        print('EEVEE installation: '+str(exc),file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
