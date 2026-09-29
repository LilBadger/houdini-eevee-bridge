"""Compose USD volume fields as OpenVDB grids for native EEVEE volumes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import bpy
from mathutils import Matrix
from hde_runtime import session_dir, volume_helper, houdini_environment

def cache():
    return session_dir()/'cache/volume_cache'


def prepare(definition, fields):
    descriptors = []
    for binding in definition['fields']:
        if binding['id'] not in fields:
            raise ValueError('Missing USD volume field: '+binding['id'])
        field = fields[binding['id']]
        source = Path(field['file'])
        stat = source.stat()
        descriptors.append({**field, 'alias':binding['name'], 'mtime':stat.st_mtime_ns, 'size':stat.st_size})
    if not descriptors:
        return None
    key = hashlib.sha256(json.dumps([descriptors, definition['transform']], sort_keys=True).encode()).hexdigest()
    path = cache()/(key+'.vdb')
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        # Houdini ships OpenVDB on both platforms. Keep its ABI out of Blender
        # and avoid relying on distribution-specific Python OpenVDB bindings.
        with tempfile.TemporaryDirectory(prefix='compose-',dir=path.parent) as directory:
            manifest = Path(directory)/'fields.json'
            manifest.write_text(json.dumps({'transform':definition['transform'],'fields':descriptors}))
            temporary = Path(directory)/'volume.vdb'
            result = subprocess.run([str(volume_helper()),str(manifest),str(temporary)],
                env=houdini_environment(),capture_output=True,text=True,timeout=120)
            if result.returncode:
                raise RuntimeError('Volume composition failed: '+result.stderr[-4000:])
            temporary.replace(path)
    return str(path)


def sync(worker, definition):
    key = definition['id']
    path = prepare(definition, worker.fields)
    obj = worker.objects.get(key)
    if path is None:
        if obj:
            obj.hide_render = True
            obj.hide_set(True, view_layer=worker.view_layer)
        return
    if obj is None:
        obj = bpy.data.objects.new(key, bpy.data.volumes.new(key))
        worker.scene.collection.objects.link(obj)
        worker.objects[key] = obj
    data = obj.data
    if data.filepath != path:
        data.grids.unload()
        data.filepath = path
        data.grids.load()
        if data.grids.error_message:
            raise ValueError('Cannot read volume '+key+': '+data.grids.error_message)
    names = {f['name'] for f in definition['fields']}
    data.velocity_grid = 'velocity' if 'velocity' in names else 'vel' if 'vel' in names else ''
    data.velocity_unit = 'SECOND'
    obj.matrix_world = worker.basis @ Matrix(definition['transform']).transposed()
    obj.hide_render = not definition.get('visible', True)
    obj.hide_set(obj.hide_render, view_layer=worker.view_layer)
    if 'prim_id' in definition:
        worker.prim_ids[key] = int(definition['prim_id'])
    worker.picks.assign(obj, worker.prim_ids.get(key, -1))
    material_id = definition.get('material', '')
    worker.bindings[key] = material_id
    worker.bound.setdefault(material_id, set()).add(key)
    mat = worker.materials.get(material_id)
    if mat is None:
        mat = bpy.data.materials.get('__eevee_default_volume')
        if mat is None:
            mat = bpy.data.materials.new('__eevee_default_volume')
            tree = mat.node_tree
            tree.nodes.clear()
            shader = tree.nodes.new('ShaderNodeVolumePrincipled')
            shader.inputs['Density'].default_value = 1.
            shader.inputs['Density Attribute'].default_value = 'density'
            output = tree.nodes.new('ShaderNodeOutputMaterial')
            tree.links.new(shader.outputs['Volume'], output.inputs['Volume'])
    if not (len(data.materials) == 1 and data.materials[0] == mat):
        data.materials.clear()
        data.materials.append(mat)
