"""USD meshes as native Blender meshes, built with NumPy and foreach_set.

Topology, points, normals and primvars arrive as NumPy arrays (protocol 2) or
nested lists (protocol 1). No per-face or per-corner Python loops are used.
Faces Blender cannot represent (fewer than three corners, or a corner repeating
its neighbour) are skipped together with their face-varying/uniform data.
"""
import bpy
import numpy as np
from mathutils import Matrix

import instances as instance_nodes
import motion
import subdivision
from protocol import array

DOMAIN = {'vertex': 'POINT', 'varying': 'POINT', 'faceVarying': 'CORNER',
          'uniform': 'FACE', 'constant': 'POINT'}


class Topology:
    """Maps USD face/corner order to the Blender mesh that was built from it."""
    __slots__ = ('point_count', 'source_faces', 'source_corners', 'face_src',
                 'corner_src', 'counts', 'loops', 'orientation', 'smooth')

    def __init__(self, point_count, counts, indices, orientation, smooth):
        counts = np.asarray(counts, dtype=np.int64).reshape(-1)
        indices = np.asarray(indices, dtype=np.int64).reshape(-1)
        if (counts < 0).any() or int(counts.sum()) != len(indices):
            raise ValueError('Face vertex counts do not match face vertex indices')
        if len(indices) and (int(indices.min()) < 0 or int(indices.max()) >= point_count):
            raise ValueError('Face vertex index out of range')
        faces, corners = len(counts), len(indices)
        self.point_count, self.source_faces, self.source_corners = point_count, faces, corners
        self.orientation, self.smooth = orientation, smooth
        starts = np.zeros(faces, dtype=np.int64)
        if faces > 1:
            np.cumsum(counts[:-1], out=starts[1:])
        corner_face = np.repeat(np.arange(faces, dtype=np.int64), counts)
        local = np.arange(corners, dtype=np.int64) - starts[corner_face]
        following = np.where(local + 1 < counts[corner_face], np.arange(1, corners + 1), starts[corner_face])
        repeated = np.zeros(faces, dtype=bool)
        if corners:
            repeated[corner_face[indices == indices[following]]] = True
        keep = (counts >= 3) & ~repeated
        left = orientation == 'leftHanded'
        if keep.all() and not left:
            self.face_src = self.corner_src = None
            self.counts = counts.astype(np.int32)
            self.loops = indices.astype(np.int32)
            return
        order = (starts[corner_face] + counts[corner_face] - 1 - local) if left else np.arange(corners)
        self.face_src = None if keep.all() else np.flatnonzero(keep)
        self.corner_src = order[keep[corner_face]]
        self.counts = counts[keep].astype(np.int32)
        self.loops = indices[self.corner_src].astype(np.int32)

    @property
    def dropped(self):
        return self.source_faces - len(self.counts)

    def to_domain(self, values, interpolation, name):
        """Return (domain, values) in Blender element order."""
        domain = DOMAIN.get(interpolation)
        if domain is None:
            raise ValueError('Unsupported interpolation for ' + name + ': ' + str(interpolation))
        if interpolation == 'constant':
            if not len(values):
                raise ValueError('Empty constant primvar: ' + name)
            return 'POINT', np.broadcast_to(values[:1], (self.point_count,) + values.shape[1:])
        expected = {'POINT': self.point_count, 'CORNER': self.source_corners, 'FACE': self.source_faces}[domain]
        if len(values) != expected:
            raise ValueError(name + ' has ' + str(len(values)) + ' ' + interpolation + ' values; expected ' + str(expected))
        if domain == 'CORNER' and self.corner_src is not None:
            values = values[self.corner_src]
        elif domain == 'FACE' and self.face_src is not None:
            values = values[self.face_src]
        return domain, values

    def to_corners(self, values, interpolation, name):
        domain, values = self.to_domain(values, interpolation, name)
        if domain == 'CORNER':
            return values
        if domain == 'FACE':
            return np.repeat(values, self.counts, axis=0)
        return values[self.loops]


def build(mesh, points, topology):
    mesh.clear_geometry()
    mesh.vertices.add(len(points))
    mesh.vertices.foreach_set('co', points.ravel())
    mesh.loops.add(len(topology.loops))
    mesh.loops.foreach_set('vertex_index', topology.loops)
    mesh.polygons.add(len(topology.counts))
    starts = np.zeros(len(topology.counts), dtype=np.int32)
    if len(starts) > 1:
        np.cumsum(topology.counts[:-1], out=starts[1:])
    mesh.polygons.foreach_set('loop_start', starts)
    mesh.update(calc_edges=True)


def remove_attribute(mesh, name):
    existing = mesh.attributes.get(name)
    if existing is not None:
        mesh.attributes.remove(existing)


def write_attribute(mesh, name, data_type, domain, values):
    existing = mesh.attributes.get(name)
    if existing is not None and (existing.data_type != data_type or existing.domain != domain):
        mesh.attributes.remove(existing)
        existing = None
    attribute = existing or mesh.attributes.new(name, data_type, domain)
    prop = 'value' if data_type == 'FLOAT' else 'vector'
    attribute.data.foreach_set(prop, np.ascontiguousarray(values, dtype=np.float32).ravel())
    return attribute


def set_normals(mesh, topology, normals, interpolation, key):
    if normals is None or not len(normals):
        remove_attribute(mesh, 'custom_normal')
        return False
    normals = array(normals, np.float32, 3)
    if interpolation == 'constant' and len(normals) == 1:
        domain, values = 'FACE', np.broadcast_to(normals[:1], (len(topology.counts), 3))
    else:
        domain, values = topology.to_domain(normals, interpolation or 'vertex', key + ' normals')
    # Blender 5 stores free custom normals as a float vector attribute. This
    # is orders of magnitude faster than normals_split_custom_set().
    write_attribute(mesh, 'custom_normal', 'FLOAT_VECTOR', domain, values)
    return True


def sync(session, update):
    key = update['id']
    obj = session.objects.get(key)
    if obj is None:
        obj = bpy.data.objects.new(key, bpy.data.meshes.new(key))
        session.scene.collection.objects.link(obj)
        session.objects[key] = obj
        obj.matrix_world = session.basis
    mesh = obj.data
    topology = session.topology.get(key)
    rebuilt = False
    if 'points' in update:
        points = array(update['points'], np.float32, 3)
        if mesh.shape_keys:
            motion.clear(mesh.shape_keys)
            obj.shape_key_clear()
        if 'counts' in update:
            smooth = bool(update.get('smooth', False))
            topology = Topology(len(points), array(update['counts'], np.int64), array(update['indices'], np.int64),
                                update.get('orientation'), smooth)
            session.topology[key] = topology
            build(mesh, points, topology)
            rebuilt = True
            if topology.dropped:
                session.warn(key, str(topology.dropped) + ' degenerate faces were skipped')
        elif topology is not None and len(points) == len(mesh.vertices):
            mesh.vertices.foreach_set('co', points.ravel())
            mesh.update()
        else:
            raise ValueError('Point count changed without topology: ' + key)
    if topology is None:
        topology = session.topology.get(key)
    names_changed = rebuilt
    if topology is not None and ('uvs' in update or 'attributes' in update or rebuilt):
        try:
            names_changed |= sync_primvars(session, key, mesh, topology, update, rebuilt)
        except ValueError as exc:
            # Keep the geometry; only the inconsistent primvar is dropped.
            session.warn(key, str(exc))
            names_changed = True
    if topology is not None and ('normals' in update or rebuilt):
        has_normals = False
        if 'normals' in update:
            try:
                has_normals = set_normals(mesh, topology, update['normals'], update.get('normals_interpolation'), key)
            except ValueError as exc:
                session.warn(key, str(exc) + '; using computed normals')
                remove_attribute(mesh, 'custom_normal')
        if has_normals or topology.smooth:
            mesh.shade_smooth()
        else:
            mesh.shade_flat()
    if names_changed and topology is not None:
        mark_present(session, key, mesh)
    if 'subsets' in update:
        session.subsets[key] = [(s['material'], array(s['indices'], np.int64)) for s in update['subsets'] or []]
        session.bind_subsets(key, [material for material, _ in session.subsets[key]])
    if topology is not None and ('subsets' in update or rebuilt) and key in session.subsets:
        set_face_materials(mesh, topology, session.subsets[key])
    if 'transform' in update:
        obj.matrix_world = session.basis @ Matrix(update['transform']).transposed()
    if 'subdivision' in update or 'subdivision_scheme' in update:
        subdivision.sync(obj, update)
        session.dirty_geometry = True
    if 'visible' in update:
        session.visibility[key] = update['visible']
    if 'prim_id' in update:
        session.prim_ids[key] = int(update['prim_id'])
    if 'instances' in update:
        sync_instances(session, key, obj, update['instances'])
    visible = session.visibility.get(key, True)
    # Prototypes remain data owners but are hidden beside their instances.
    hidden = not visible or key in session.instances or key in session.point_instances
    if obj.hide_render != hidden:
        obj.hide_render = hidden
    if obj.hide_get(view_layer=session.view_layer) != hidden:
        obj.hide_set(hidden, view_layer=session.view_layer)
    for instance in session.instances.get(key, []):
        if instance.hide_render == visible:
            instance.hide_render = not visible
            instance.hide_set(not visible, view_layer=session.view_layer)
    if key in session.point_instances:
        points_object = session.point_instances[key]
        if points_object.hide_render == visible:
            points_object.hide_render = not visible
            points_object.hide_set(not visible, view_layer=session.view_layer)
    prim_id = session.prim_ids.get(key, -1)
    session.picks.assign(obj, prim_id)
    for index, instance in enumerate(session.instances.get(key, [])):
        session.picks.assign(instance, prim_id, index)
    if key in session.point_instances:
        session.picks.assign(session.point_instances[key], prim_id)
    if 'material' in update:
        session.bind(key, update['material'])
    if 'color' in update and not session.bindings.get(key):
        session.display_material(key, update['color'], update.get('color_varying', False))


def set_face_materials(mesh, topology, subsets):
    """Material slot per face from USD GeomSubsets (slot 0: the prim's own material)."""
    index = np.zeros(topology.source_faces, dtype=np.int32)
    for slot, (_, faces) in enumerate(subsets, start=1):
        index[faces[(faces >= 0) & (faces < topology.source_faces)]] = slot
    if topology.face_src is not None:
        index = index[topology.face_src]
    if len(index) == len(mesh.polygons):
        mesh.polygons.foreach_set('material_index', index)


def sync_primvars(session, key, mesh, topology, update, rebuilt):
    """Write UV maps and float/vector attributes. Returns True if names changed."""
    partial = bool(update.get('primvars_partial')) and not rebuilt
    uvs = update.get('uvs', {}) or {}
    attributes = update.get('attributes', {}) or {}
    removed = set(update.get('primvars_removed', []))
    exported = session.exported_attributes.setdefault(key, {})
    before = set(exported) | set(mesh.uv_layers.keys())
    if not partial:
        for layer in list(mesh.uv_layers):
            if layer.name not in uvs:
                mesh.uv_layers.remove(layer)
        for name in list(exported):
            if name not in attributes:
                remove_attribute(mesh, name)
                exported.pop(name)
    for name in removed:
        layer = mesh.uv_layers.get(name)
        if layer is not None:
            mesh.uv_layers.remove(layer)
        if name in exported:
            remove_attribute(mesh, name)
            exported.pop(name)
    for name, definition in uvs.items():
        values = array(definition['values'], np.float32, 2)
        if not len(values):
            continue
        corners = topology.to_corners(values, definition['interpolation'], key + ' ' + name)
        if name in exported:
            remove_attribute(mesh, name)
            exported.pop(name)
        write_attribute(mesh, name, 'FLOAT2', 'CORNER', corners)
    for name, definition in attributes.items():
        data_type = definition.get('type', 'FLOAT')
        columns = 3 if data_type == 'FLOAT_VECTOR' else None
        values = array(definition['values'], np.float32, columns)
        if not len(values):
            continue
        domain, values = topology.to_domain(values, definition['interpolation'], key + ' ' + name)
        layer = mesh.uv_layers.get(name)
        if layer is not None:
            mesh.uv_layers.remove(layer)
        write_attribute(mesh, name, data_type, domain, values)
        exported[name] = data_type
    if mesh.attributes.get('displayOpacity') is None:
        # USD displayOpacity defaults to 1. A missing Blender attribute reads
        # as zero and would make imported assets invisible.
        write_attribute(mesh, 'displayOpacity', 'FLOAT', 'POINT', np.ones(len(mesh.vertices), np.float32))
    after = set(exported) | set(mesh.uv_layers.keys())
    return before != after


def mark_present(session, key, mesh):
    # Explicit markers implement MaterialX geompropvalue defaults; Blender's
    # Attribute node cannot report whether an attribute exists.
    names = set(session.exported_attributes.get(key, {})) | set(mesh.uv_layers.keys()) | {'displayOpacity'}
    for attribute in list(mesh.attributes):
        if attribute.name.startswith('hde:present:') and attribute.name[12:] not in names:
            mesh.attributes.remove(attribute)
    ones = None
    for name in names:
        if mesh.attributes.get(name) is None:
            continue
        marker = 'hde:present:' + name
        existing = mesh.attributes.get(marker)
        if existing is not None and len(existing.data) == len(mesh.vertices):
            continue
        if ones is None:
            ones = np.ones(len(mesh.vertices), dtype=np.float32)
        write_attribute(mesh, marker, 'FLOAT', 'POINT', ones)


def sync_instances(session, key, obj, transforms):
    if transforms is None:
        session.instance_state.pop(key, None)
        instance_nodes.remove(session, key)
        for instance in session.instances.pop(key, []):
            session.picks.release(instance)
            bpy.data.objects.remove(instance, do_unlink=True)
        return
    transforms = array(transforms, np.float32).reshape(-1, 4, 4)
    session.instance_state[key] = transforms
    session.dirty_geometry = True
    # Matrix attributes avoid creating tens of thousands of Blender objects.
    # Shutter motion blur needs individually animated objects instead.
    if len(transforms) > 256 and not session.instancing_motion:
        for instance in session.instances.pop(key, []):
            session.picks.release(instance)
            bpy.data.objects.remove(instance, do_unlink=True)
        instance_nodes.sync(session, key, obj, transforms, session.visibility.get(key, True))
        return
    instance_nodes.remove(session, key)
    objects = session.instances.setdefault(key, [])
    while len(objects) > len(transforms):
        extra = objects.pop()
        session.picks.release(extra)
        bpy.data.objects.remove(extra, do_unlink=True)
    while len(objects) < len(transforms):
        instance = bpy.data.objects.new(key + '/instance_' + str(len(objects)), obj.data)
        session.scene.collection.objects.link(instance)
        objects.append(instance)
    for instance, transform in zip(objects, transforms):
        instance.matrix_world = session.basis @ Matrix(transform.tolist()).transposed()
