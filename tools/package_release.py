"""Build a redistributable source ZIP, optionally containing host native binaries."""
import argparse
import hashlib
from pathlib import Path
import sys
import zipfile
from hde_runtime import ROOT, VERSION, platform_tag


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-root',type=Path,help='CMake build/install folder containing plugin/ and bin/')
    parser.add_argument('--output',type=Path,default=ROOT/'dist')
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
    for filename in ('install.py','install.sh','install.cmd','install.ps1','CMakeLists.txt','README.md','INSTALL.md','CHANGELOG.md','LICENSE'):
        files.append((ROOT/filename,Path(filename)))
    if args.native_root:
        for directory in ('plugin','bin'):
            for path in (args.native_root/directory).rglob('*'):
                if path.is_file(): files.append((path,path.relative_to(args.native_root)))
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as output:
        for path,relative in sorted(files): output.write(path,str(Path(name)/relative))
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(digest+'  '+archive.name+'\n')
    print(archive)


if __name__=='__main__':main()
