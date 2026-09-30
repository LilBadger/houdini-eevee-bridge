"""Build a redistributable source ZIP, optionally containing host native binaries."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile
from hde_runtime import ROOT, VERSION, platform_tag


def checksum(path):
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_name(path.name+'.sha256').write_text(digest+'  '+path.name+'\n')


def find_makensis():
    """NSIS 3's compiler: on PATH, installed, or the portable zip unpacked in LOCALAPPDATA/Programs."""
    local=Path(os.environ.get('LOCALAPPDATA',''))/'Programs'
    candidates=[shutil.which('makensis'),*sorted(local.glob('nsis-*/makensis.exe'),reverse=True),
                Path(os.environ.get('ProgramFiles(x86)','C:/Program Files (x86)'))/'NSIS/makensis.exe',
                Path(os.environ.get('ProgramFiles','C:/Program Files'))/'NSIS/makensis.exe']
    found=next((Path(c) for c in candidates if c and Path(c).is_file()),None)
    if not found: raise SystemExit('NSIS 3 was not found; install it (https://nsis.sourceforge.io) for --setup.')
    return found


def build_setup(archive, output):
    """houdini-eevee-<version>-windows-x86_64-setup.exe from the Windows package archive."""
    setup=output.resolve()/(archive.stem+'-setup.exe')
    with tempfile.TemporaryDirectory(prefix='eevee-setup-') as temp:
        with zipfile.ZipFile(archive) as package: package.extractall(temp)
        payload=Path(temp)/archive.stem
        subprocess.run([str(find_makensis()),'/V2','/DVERSION='+VERSION,'/DPAYLOAD='+str(payload),
                        '/DOUTFILE='+str(setup),str(ROOT/'tools/windows_setup.nsi')],check=True)
    checksum(setup)
    return setup


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-root',type=Path,action='append',
                        help='CMake install folder with plugin/ and bin/; repeat for more Houdini builds (stored in native/<build>)')
    parser.add_argument('--wheels',type=Path,help='Folder of Python wheels for Blender, bundled so installs need no internet')
    parser.add_argument('--output',type=Path,default=ROOT/'dist')
    parser.add_argument('--setup',action='store_true',help='Also build the Windows setup .exe with NSIS (needs --native-root)')
    args=parser.parse_args()
    name='houdini-eevee-'+VERSION+('-'+platform_tag() if args.native_root else '-source')
    args.output.mkdir(parents=True,exist_ok=True)
    archive=args.output/(name+'.zip')
    files=[]
    for directory in ('tools','worker','houdini','src','third_party','docs'):
        for path in (ROOT/directory).rglob('*'):
            if path.is_file() and not any(part in ('__pycache__','backup') for part in path.parts) and path.suffix not in ('.pyc','.bak'):
                if path.relative_to(ROOT).as_posix() not in ('houdini/scripts/123.py','houdini/scripts/456.py'):
                    files.append((path,path.relative_to(ROOT)))
    for filename in ('install.py','install.sh','install.cmd','install.ps1','uninstall.sh','uninstall.cmd','uninstall.ps1',
                     'CMakeLists.txt','README.md','INSTALL.md','CHANGELOG.md','LICENSE'):
        files.append((ROOT/filename,Path(filename)))
    for index, native in enumerate(args.native_root or []):
        # The first build sits at the top, where older installers look; others in native/<build>.
        build=json.loads((native/'plugin/hdEevee/build-info.json').read_text(encoding='utf-8'))['houdini_version']
        base=Path('.') if index == 0 else Path('native')/build
        for directory in ('plugin','bin'):
            for path in (native/directory).rglob('*'):
                if path.is_file(): files.append((path,base/path.relative_to(native)))
    if args.wheels:
        for path in sorted(args.wheels.glob('*.whl')): files.append((path,Path('wheels')/path.name))
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as output:
        for path,relative in sorted(files): output.write(path,str(Path(name)/relative))
    checksum(archive)
    print(archive)
    if args.setup:
        if not args.native_root or os.name != 'nt': raise SystemExit('--setup needs --native-root on Windows.')
        print(build_setup(archive,args.output))


if __name__=='__main__':main()
