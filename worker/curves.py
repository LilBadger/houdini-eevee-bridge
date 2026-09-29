"""USD BasisCurves as native EEVEE hair curves.

Linear curves become poly curves. Cubic curves keep their exact shape:
- bspline: uniform cubic NURBS. "pinned" ends use USD's phantom points.
- bezier: Blender Bezier curves; every third control point lies on the curve.
- catmullRom: Blender Bezier curves with the equivalent handles, (P[i+1] - P[i-1]) / 6.
widths and float/float2/float3 primvars become point or curve attributes.
"""
import bpy
import numpy as np
from mathutils import Matrix

import instances as instance_nodes
from protocol import array

DEFAULT_WIDTH = .01
HANDLE_FREE = 0
KNOTS_NORMAL = 0
COLUMNS = {'FLOAT': None, 'FLOAT2': 2, 'FLOAT_VECTOR': 3}
RESERVED = {'position', 'radius', 'handle_left', 'handle_right', 'handle_type_left', 'handle_type_right',
            'curve_type', 'nurbs_order', 'knots_mode', 'nurbs_weight', 'resolution', 'cyclic'}


def starts_of(counts):
    starts = np.zeros(len(counts), dtype=np.int64)
    if len(counts) > 1:
        np.cumsum(counts[:-1], out=starts[1:])
    return starts


class Shape:
    """Blender curves built from USD curves, and how USD values map onto their points.

    `source` gives, for every Blender point, the USD vertex whose vertex-interpolated
    values it takes. `varying` gives the number of USD varying values per curve.
    """

    def __init__(self, kind, counts, positions, source, varying, handles=None, cyclic=False):
        self.kind, self.counts, self.positions, self.source = kind, counts, positions, source
        self.varying, self.handles, self.cyclic = varying, handles, cyclic


def linear(points, counts, periodic):
    return Shape('POLY', counts, points, np.arange(len(points)), counts, cyclic=periodic)


def bspline(points, counts, wrap):
    starts = starts_of(counts)
    if wrap == 'periodic':
        return Shape('NURBS', counts, points, np.arange(len(points)), counts, cyclic=True)
    if wrap != 'pinned':
        return Shape('NURBS', counts, points, np.arange(len(points)), np.maximum(counts - 2, 1))
    # Pinned: a phantom point before and after each curve, 2*P0 - P1 and 2*Pn - Pn-1,
    # makes the uniform B-spline start and end exactly on the end points.
    if (counts < 2).any():
        raise ValueError('Pinned B-spline curves need at least two points')
    ends = starts + counts - 1
    out_counts = counts + 2
    out_starts = starts_of(out_counts)
    source = np.empty(int(out_counts.sum()), dtype=np.int64)
    curve = np.repeat(np.arange(len(counts)), counts)
    local = np.arange(len(points)) - starts[curve]
    source[out_starts[curve] + 1 + local] = np.arange(len(points))
    source[out_starts] = starts
    source[out_starts + out_counts - 1] = ends
    positions = points[source].copy()
    positions[out_starts] = 2 * points[starts] - points[starts + 1]
    positions[out_starts + out_counts - 1] = 2 * points[ends] - points[ends - 1]
    return Shape('NURBS', out_counts, positions, source, counts)


def bezier(points, counts, wrap):
    """Cubic Bezier: on-curve control points every third vertex."""
    periodic = wrap == 'periodic'
    segments = counts // 3 if periodic else (counts - 1) // 3
    if (segments < 1).any() or (not periodic and ((counts - 1) % 3).any()) or (periodic and (counts % 3).any()):
        raise ValueError('Bezier curve vertex counts must be 3n+1 (or 3n when periodic)')
    starts = starts_of(counts)
    out_counts = segments if periodic else segments + 1
    curve = np.repeat(np.arange(len(counts)), out_counts)
    local = np.arange(int(out_counts.sum())) - np.repeat(starts_of(out_counts), out_counts)
    anchor = 3 * local
    size = counts[curve]
    on = starts[curve] + anchor
    if periodic:
        left = starts[curve] + (anchor - 1) % size
        right = starts[curve] + (anchor + 1) % size
    else:
        left = np.where(anchor > 0, on - 1, on)
        right = np.where(anchor < size - 1, on + 1, on)
    return Shape('BEZIER', out_counts, points[on], on, out_counts, (points[left], points[right]), periodic)


def catmull_rom(points, counts, wrap):
    """Catmull-Rom as Bezier: the tangent at P[i] is (P[i+1] - P[i-1]) / 2."""
    starts = starts_of(counts)
    periodic, pinned = wrap == 'periodic', wrap == 'pinned'
    minimum = 2 if (periodic or pinned) else 4
    if (counts < minimum).any():
        raise ValueError('Catmull-Rom curves need at least ' + str(minimum) + ' points')
    out_counts = counts if (periodic or pinned) else counts - 2
    curve = np.repeat(np.arange(len(counts)), out_counts)
    local = np.arange(int(out_counts.sum())) - np.repeat(starts_of(out_counts), out_counts)
    size = counts[curve]
    first = starts[curve]
    index = local if (periodic or pinned) else local + 1
    on = first + index
    if periodic:
        before = points[first + (index - 1) % size]
        after = points[first + (index + 1) % size]
    else:
        # Pinned ends use USD's phantom points: 2*P0 - P1 and 2*Pn - Pn-1.
        previous = np.maximum(index - 1, 0) + first
        following = np.minimum(index + 1, size - 1) + first
        before, after = points[previous].copy(), points[following].copy()
        at_start, at_end = index == 0, index == size - 1
        before[at_start] = 2 * points[on[at_start]] - points[following[at_start]]
        after[at_end] = 2 * points[on[at_end]] - points[previous[at_end]]
    tangent = (after - before) / 6.
    positions = points[on]
    return Shape('BEZIER', out_counts, positions, on, out_counts, (positions - tangent, positions + tangent), periodic)


def shape(update, points, counts):
    kind, wrap = update.get('type', 'linear'), update.get('wrap', 'nonperiodic')
    if kind == 'linear':
        return linear(points, counts, wrap == 'periodic')
    basis = update.get('basis', 'bezier')
    if basis == 'bspline':
        return bspline(points, counts, wrap)
    if basis == 'bezier':
        return bezier(points, counts, wrap)
    if basis == 'catmullRom':
        return catmull_rom(points, counts, wrap)
    raise ValueError('Unsupported USD cubic curve basis ' + str(basis))


def resample(values, source_counts, target_counts):
    """Linear interpolation of per-curve value runs onto a different number of points."""
    result = np.empty((int(target_counts.sum()),) + values.shape[1:], dtype=np.float32)
    source_starts, target_starts = starts_of(source_counts), starts_of(target_counts)
    for s0, sn, t0, tn in zip(source_starts, source_counts, target_starts, target_counts):
        run = values[s0:s0 + sn]
        if sn == tn:
            result[t0:t0 + tn] = run
            continue
        x = np.linspace(0., 1., tn)
        xp = np.linspace(0., 1., sn) if sn > 1 else np.zeros(1)
        if run.ndim == 1:
            result[t0:t0 + tn] = np.interp(x, xp, run)
        else:
            for c in range(run.shape[1]):
                result[t0:t0 + tn, c] = np.interp(x, xp, run[:, c])
    return result


def to_domain(values, interpolation, curves_shape, vertex_count, curve_count, name):
    """Return (Blender domain, values) for a USD curve primvar."""
    if interpolation == 'constant':
        if not len(values):
            raise ValueError('Empty primvar: ' + name)
        return 'POINT', np.broadcast_to(values[:1], (len(curves_shape.positions),) + values.shape[1:])
    if interpolation == 'uniform':
        if len(values) != curve_count:
            raise ValueError(name + ' has ' + str(len(values)) + ' uniform values for ' + str(curve_count) + ' curves')
        return 'CURVE', values
    if interpolation == 'vertex':
        if len(values) != vertex_count:
            raise ValueError(name + ' has ' + str(len(values)) + ' vertex values for ' + str(vertex_count) + ' vertices')
        return 'POINT', values[curves_shape.source]
    varying = np.asarray(curves_shape.varying, dtype=np.int64)
    if len(values) != int(varying.sum()):
        raise ValueError(name + ' has ' + str(len(values)) + ' varying values; expected ' + str(int(varying.sum())))
    return 'POINT', resample(values, varying, curves_shape.counts)


def write(data, name, data_type, domain, values):
    existing = data.attributes.get(name)
    if existing is not None and (existing.data_type != data_type or existing.domain != domain):
        data.attributes.remove(existing)
        existing = None
    attribute = existing or data.attributes.new(name, data_type, domain)
    prop = 'value' if data_type in ('FLOAT', 'INT8', 'INT', 'BOOLEAN') else 'vector'
    if data_type in ('INT8', 'INT'):
        values = np.asarray(values, dtype=np.int32)   # RNA integer arrays are 32-bit
    attribute.data.foreach_set(prop, np.ascontiguousarray(values).ravel())


def mark_present(data, names, write_marker, count):
    """Presence markers for MaterialX geompropvalue defaults (see meshes.mark_present)."""
    for attribute in [a.name for a in data.attributes if a.name.startswith('hde:present:') and a.name[12:] not in names]:
        data.attributes.remove(data.attributes[attribute])
    ones = np.ones(count, dtype=np.float32)
    for name in names:
        write_marker('hde:present:' + name, ones)


def sync(session, update):
    key = update['id']
    counts = array(update['counts'], np.int64).reshape(-1)
    points = array(update['points'], np.float32, 3)
    indices = array(update.get('indices', []), np.int64).reshape(-1)
    if len(indices):
        points = points[indices]
    if int(counts.sum()) != len(points):
        raise ValueError('Curve topology/point count mismatch: ' + key)
    curves_shape = shape(update, points, counts)
    obj = session.objects.get(key)
    if obj is None:
        obj = bpy.data.objects.new(key, bpy.data.hair_curves.new(key))
        session.scene.collection.objects.link(obj)
        session.objects[key] = obj
    data = obj.data
    wanted = curves_shape.counts.astype(np.int32)
    existing = np.empty(len(data.curves), dtype=np.int32)
    if len(existing):
        data.curves.foreach_get('points_length', existing)
    # Converting between curve types can change point counts, so a type change rebuilds.
    if len(existing) != len(wanted) or (existing != wanted).any() or obj.get('hde_curve_type') != curves_shape.kind:
        if len(data.curves):
            data.remove_curves()
        data.add_curves(wanted.tolist())
        data.set_types(type=curves_shape.kind)
        obj['hde_curve_type'] = curves_shape.kind
    if len(data.points) != len(curves_shape.positions):
        raise ValueError('Blender changed the point count of curves ' + key)
    data.position_data.foreach_set('vector', np.ascontiguousarray(curves_shape.positions, dtype=np.float32).ravel())
    if curves_shape.kind == 'BEZIER':
        left, right = curves_shape.handles
        free = np.full(len(curves_shape.positions), HANDLE_FREE, dtype=np.int8)
        write(data, 'handle_type_left', 'INT8', 'POINT', free)
        write(data, 'handle_type_right', 'INT8', 'POINT', free)
        write(data, 'handle_left', 'FLOAT_VECTOR', 'POINT', left.astype(np.float32))
        write(data, 'handle_right', 'FLOAT_VECTOR', 'POINT', right.astype(np.float32))
    elif curves_shape.kind == 'NURBS':
        write(data, 'nurbs_order', 'INT8', 'CURVE', np.minimum(wanted, 4).astype(np.int8))
        write(data, 'knots_mode', 'INT8', 'CURVE', np.full(len(wanted), KNOTS_NORMAL, dtype=np.int8))
    write(data, 'cyclic', 'BOOLEAN', 'CURVE', np.full(len(wanted), curves_shape.cyclic))
    widths = update.get('widths')
    widths = array([DEFAULT_WIDTH] if widths is None or not len(widths) else widths, np.float32).reshape(-1)
    interpolation = update.get('widths_interpolation') or ('constant' if len(widths) == 1 else 'vertex')
    if interpolation == 'vertex' and len(indices) and len(widths) != len(points):
        widths = widths[indices]
    domain, widths = to_domain(widths, interpolation, curves_shape, len(points), len(counts), key + ' widths')
    radius = np.maximum(widths, 0) * .5
    if domain == 'CURVE':
        radius = np.repeat(radius, wanted)
    write(data, 'radius', 'FLOAT', 'POINT', radius.astype(np.float32))
    exported = session.exported_attributes.setdefault(key, {})
    written = {}
    for group in ('uvs', 'attributes'):
        for name, definition in (update.get(group) or {}).items():
            if name in RESERVED:
                continue
            data_type = 'FLOAT2' if group == 'uvs' else definition.get('type', 'FLOAT')
            try:
                values = array(definition['values'], np.float32, COLUMNS[data_type])
                domain, values = to_domain(values, definition['interpolation'], curves_shape, len(points),
                                           len(counts), key + ' ' + name)
            except ValueError as exc:
                session.warn(key, str(exc))
                continue
            write(data, name, data_type, domain, values.astype(np.float32))
            written[name] = data_type
    for name in set(exported) - set(written):
        attribute = data.attributes.get(name)
        if attribute is not None:
            data.attributes.remove(attribute)
    session.exported_attributes[key] = written
    mark_present(data, written, lambda name, values: write(data, name, 'FLOAT', 'POINT', values), len(data.points))
    obj.matrix_world = session.basis @ Matrix(update['transform']).transposed()
    instance_nodes.finish(session, key, obj, update)
    data.update_tag()
