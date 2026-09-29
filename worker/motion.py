"""Build native Blender animation from Hydra's frame-relative shutter samples."""
import bpy
import numpy as np
from mathutils import Matrix


def linear_animation(owner):
    animation = owner.animation_data
    if not animation or not animation.action:
        return
    action = animation.action
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for curve in bag.fcurves:
                    for key in curve.keyframe_points:
                        key.interpolation = 'LINEAR'


def clear(owner):
    animation = owner.animation_data
    action = animation.action if animation else None
    owner.animation_data_clear()
    if action and action.users == 0:
        bpy.data.actions.remove(action)


def transform(obj, samples, frame, basis):
    clear(obj)
    if len(samples) < 2:
        return
    previous = None
    obj.rotation_mode = 'QUATERNION'
    current = obj.matrix_world.copy()
    for sample in samples:
        matrix = basis @ Matrix(np.asarray(sample['value'], dtype=np.float64).reshape(4, 4).tolist()).transposed()
        location, rotation, scale = matrix.decompose()
        if previous is not None and rotation.dot(previous) < 0:
            rotation.negate()
        previous = rotation.copy()
        obj.location, obj.rotation_quaternion, obj.scale = location, rotation, scale
        for name in ('location', 'rotation_quaternion', 'scale'):
            obj.keyframe_insert(name, frame=frame+sample['time'])
    linear_animation(obj)
    obj.matrix_world = current


def deformation(obj, samples, frame):
    if obj.data.shape_keys:
        clear(obj.data.shape_keys)
        obj.shape_key_clear()
    if len(samples) < 2:
        return
    count = len(obj.data.vertices)
    if any(len(s['value']) != count for s in samples):
        raise ValueError('Motion blur requires stable point count: '+obj.name)
    obj.shape_key_add(name='Basis')
    for index, sample in enumerate(samples):
        shape = obj.shape_key_add(name='USD_sample_'+str(index))
        shape.data.foreach_set('co', np.asarray(sample['value'], dtype=np.float32).ravel())
        for j, other in enumerate(samples):
            shape.value = float(j == index)
            shape.keyframe_insert('value', frame=frame+other['time'])
    linear_animation(obj.data.shape_keys)
