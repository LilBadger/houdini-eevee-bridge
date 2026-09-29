"""Run inside Blender: report version, schema compatibility and private deps."""
import importlib
import json
import os
from pathlib import Path
import sys
import bpy

if os.environ.get('HDEEVEE_PYTHON_DEPS'):
    sys.path.insert(0, os.environ['HDEEVEE_PYTHON_DEPS'])
root = Path(__file__).resolve().parents[1]
schema = json.loads((root/'houdini/eevee_settings.json').read_text(encoding='utf-8'))
missing = []
versions = {}
for name in ('numpy', 'OpenEXR', 'Imath'):
    try:
        module = importlib.import_module(name)
        versions[name] = getattr(module, '__version__', 'available')
        if name == 'OpenEXR' and not all(hasattr(module, n) for n in ('File','OutputFile','Header')):
            missing.append('OpenEXR')
    except ImportError:
        missing.append(name)
owners = {'eevee':bpy.context.scene.eevee,
          'ray_tracing_options':bpy.context.scene.eevee.ray_tracing_options}
incompatible = [group+'.'+p['name'] for group, owner in owners.items()
                for p in schema['groups'][group] if not hasattr(owner,p['name'])]
prefix = Path(sys.prefix)
candidates = [prefix/'bin/python.exe', prefix/'python.exe'] if os.name == 'nt' else sorted((prefix/'bin').glob('python3.*'))
if Path(sys.executable).name.startswith('python'): candidates.insert(0,Path(sys.executable))
python = next((p for p in candidates if p.is_file() and not p.name.endswith('-config')), None)
print('HDEEVEE_CHECK='+json.dumps({'blender':bpy.app.version_string, 'version':list(bpy.app.version),
    'python_version':list(sys.version_info[:3]), 'python':str(python) if python else None,
    'modules':versions, 'missing':missing, 'incompatible':incompatible, 'openvdb':bpy.app.build_options.openvdb}), flush=True)
