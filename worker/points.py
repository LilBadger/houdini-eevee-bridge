"""USD Points (particles) as native Blender point clouds, which EEVEE draws as spheres.

widths are diameters, so the Blender radius is width / 2. float, float2 and float3
primvars become point attributes that materials can read with an Attribute node.
"""
import bpy
import numpy as np
from mathutils import Matrix

from protocol import array
import curves
import instances as instance_nodes

DEFAULT_WIDTH = 1.0   # USD/Hydra fallback when widths is not authored
COLUMNS = {'FLOAT': None, 'FLOAT2': 2, 'FLOAT_VECTOR': 3}
RESERVED = {'position', 'radius'}


def per_point(values, interpolation, count, name):
    if interpolation in ('constant', 'uniform'):
        if not len(values):
            raise ValueError('Empty primvar: ' + name)
        return np.broadcast_to(values[:1], (count,) + values.shape[1:])
    if len(values) != count:
        raise ValueError(name + ' has ' + str(len(values)) + ' values for ' + str(count) + ' points')
    return values


def write(cloud, name, data_type, values):
    existing = cloud.attributes.get(name)
    if existing is not None and (existing.data_type != data_type or existing.domain != 'POINT'):
        cloud.attributes.remove(existing)
        existing = None
    attribute = existing or cloud.attributes.new(name, data_type, 'POINT')
    prop = 'value' if data_type == 'FLOAT' else 'vector'
    attribute.data.foreach_set(prop, np.ascontiguousarray(values, dtype=np.float32).ravel())


def sync(session, update):
    key = update['id']
    obj = session.objects.get(key)
    if obj is None:
        obj = bpy.data.objects.new(key, bpy.data.pointclouds.new(key))
        session.scene.collection.objects.link(obj)
        session.objects[key] = obj
    cloud = obj.data
    points = array(update['points'], np.float32, 3)
    count = len(points)
    if len(cloud.points) != count:
        cloud.resize(count)
    if count:
        cloud.attributes['position'].data.foreach_set('vector', points.ravel())
        widths = update.get('widths')
        widths = array([DEFAULT_WIDTH] if widths is None or not len(widths) else widths, np.float32)
        interpolation = update.get('widths_interpolation') or ('constant' if len(widths) == 1 else 'vertex')
        write(cloud, 'radius', 'FLOAT', np.maximum(per_point(widths, interpolation, count, key + ' widths'), 0) * .5)
    exported = session.exported_attributes.setdefault(key, {})
    written = {}
    for group in ('uvs', 'attributes'):
        for name, definition in (update.get(group) or {}).items():
            if name in RESERVED or not count:
                continue
            data_type = 'FLOAT2' if group == 'uvs' else definition.get('type', 'FLOAT')
            try:
                values = per_point(array(definition['values'], np.float32, COLUMNS[data_type]),
                                   definition['interpolation'], count, key + ' ' + name)
            except ValueError as exc:
                session.warn(key, str(exc))
                continue
            write(cloud, name, data_type, values)
            written[name] = data_type
    for name in set(exported) - set(written):
        attribute = cloud.attributes.get(name)
        if attribute is not None:
            cloud.attributes.remove(attribute)
    session.exported_attributes[key] = written
    curves.mark_present(cloud, written, lambda name, values: write(cloud, name, 'FLOAT', values), count)
    obj.matrix_world = session.basis @ Matrix(update['transform']).transposed()
    instance_nodes.finish(session, key, obj, update)
    cloud.update_tag()
