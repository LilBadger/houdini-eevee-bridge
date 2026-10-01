"""USD instancing: Blender objects sharing a prototype's data, or Geometry Nodes
instances with full matrices when there are many. Per-instance primvars become
instance attributes or object custom properties (see shader_utils.primvar)."""
import bpy
import numpy as np
from mathutils import Matrix

from protocol import array
from shader_utils import INSTANCE_PREFIX

NAVIGATION_FRACTION='Navigation fraction'
# Instancers with fewer instances always draw all of them.
DENSE_INSTANCES=1000


def navigation(session, navigating, percent):
    """Blender processes every instance on each redraw, on the CPU: 116,000 bubble
    instances took 120 ms per navigation frame. While the view changes, dense
    instancers draw a fixed random subset; settled and final frames draw all."""
    fraction = min(max(percent, 1), 100) / 100. if navigating else 1.
    for obj in session.point_instances.values():
        if int(obj.get('usd_instance_count', 0)) < DENSE_INSTANCES:
            continue
        node = obj.modifiers[0].node_group.nodes.get(NAVIGATION_FRACTION) if obj.modifiers else None
        if node is not None and node.outputs[0].default_value != fraction:
            node.outputs[0].default_value = fraction


def remove(worker,key):
    obj=worker.point_instances.pop(key,None)
    if obj is None:return
    worker.picks.release(obj)
    data=obj.data;groups=[m.node_group for m in obj.modifiers if m.type=='NODES']
    bpy.data.objects.remove(obj,do_unlink=True)
    if not data.users:bpy.data.meshes.remove(data)
    for group in groups:
        if group and not group.users:bpy.data.node_groups.remove(group)

def sync_objects(session, key, obj, transforms, primvars=None):
    """Instances of the prototype obj. transforms None removes them; primvars
    None keeps the previous per-instance primvars."""
    if primvars is not None:
        session.instance_primvars[key] = {name: array(values, np.float32).reshape(len(values), -1) if len(values) else
                                          np.zeros((0, 1), np.float32) for name, values in primvars.items()}
    if transforms is None:
        session.instance_state.pop(key, None)
        session.instance_primvars.pop(key, None)
        remove(session, key)
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
        sync(session, key, obj, transforms, session.visibility.get(key, True))
        return
    remove(session, key)
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
    primvars = rgba_primvars(session, key, len(objects))
    for index, instance in enumerate(objects):
        for name in [k for k in instance.keys() if k.startswith(INSTANCE_PREFIX) and k not in primvars]:
            del instance[name]
        for name, values in primvars.items():
            instance[name] = values[index].tolist()


def rgba_primvars(session, key, count):
    """Per-instance primvars as RGBA with alpha 1, by attribute name."""
    result = {}
    for name, values in session.instance_primvars.get(key, {}).items():
        if len(values) != count:
            session.warn(key, 'Instance primvar ' + name + ' has ' + str(len(values)) + ' values for ' +
                         str(count) + ' instances')
            continue
        rgba = np.zeros((count, 4), np.float32)
        rgba[:, 3] = 1.
        if values.shape[1] == 1:
            rgba[:, :3] = values        # a scalar reads the same as Fac, Color or Vector
        else:
            size = min(values.shape[1], 3)
            rgba[:, :size] = values[:, :size]
        result[INSTANCE_PREFIX + name] = rgba
    return result


def refresh(session, key, obj, visible):
    """Prototypes stay data owners but are hidden beside their instances, which
    follow the prim's visibility. Also assigns picking ids."""
    hidden = not visible or key in session.instances or key in session.point_instances
    if obj.hide_render != hidden:
        obj.hide_render = hidden
    if obj.hide_get(view_layer=session.view_layer) != hidden:
        obj.hide_set(hidden, view_layer=session.view_layer)
    others = list(session.instances.get(key, []))
    if key in session.point_instances:
        others.append(session.point_instances[key])
    for other in others:
        if other.hide_render == visible:
            other.hide_render = not visible
            other.hide_set(not visible, view_layer=session.view_layer)
    prim_id = session.prim_ids.get(key, -1)
    session.picks.assign(obj, prim_id)
    for index, instance in enumerate(session.instances.get(key, [])):
        session.picks.assign(instance, prim_id, index)
    if key in session.point_instances:
        session.picks.assign(session.point_instances[key], prim_id)


def finish(session, key, obj, update):
    """Visibility, instances, picking and material of a prim that is sent whole
    (curves, points, volumes)."""
    session.visibility[key] = update.get('visible', True)
    if 'prim_id' in update:
        session.prim_ids[key] = int(update['prim_id'])
    sync_objects(session, key, obj, update.get('instances'), update.get('instance_primvars', {}))
    refresh(session, key, obj, session.visibility[key])
    session.bind(key, update.get('material', ''))
    session.display_color(key, update)


def sync(worker,key,prototype,transforms,visible):
    obj=worker.point_instances.get(key)
    if obj is None:
        data=bpy.data.meshes.new(key+'/InstancePoints')
        obj=bpy.data.objects.new(key+'/Instances',data);worker.scene.collection.objects.link(obj)
        worker.point_instances[key]=obj
        tree=bpy.data.node_groups.new(key+'/USD Instancer','GeometryNodeTree')
        tree.interface.new_socket(name='Geometry',in_out='INPUT',socket_type='NodeSocketGeometry')
        tree.interface.new_socket(name='Geometry',in_out='OUTPUT',socket_type='NodeSocketGeometry')
        inp=tree.nodes.new('NodeGroupInput');out=tree.nodes.new('NodeGroupOutput')
        source=tree.nodes.new('GeometryNodeObjectInfo');source.inputs['Object'].default_value=prototype
        source.transform_space='ORIGINAL';source.inputs['As Instance'].default_value=False
        inst=tree.nodes.new('GeometryNodeInstanceOnPoints')
        tree.links.new(inp.outputs['Geometry'],inst.inputs['Points']);tree.links.new(source.outputs['Geometry'],inst.inputs['Instance'])
        # While navigating, dense instancers draw a fixed random subset (see navigation()).
        rnd=tree.nodes.new('FunctionNodeRandomValue');rnd.data_type='FLOAT';rnd.inputs['Seed'].default_value=7
        fraction=tree.nodes.new('ShaderNodeValue');fraction.name=NAVIGATION_FRACTION;fraction.outputs[0].default_value=1.
        keep=tree.nodes.new('FunctionNodeCompare');keep.data_type='FLOAT';keep.operation='LESS_EQUAL'
        tree.links.new(rnd.outputs['Value'],keep.inputs['A']);tree.links.new(fraction.outputs[0],keep.inputs['B'])
        tree.links.new(keep.outputs['Result'],inst.inputs['Selection'])
        matrix=tree.nodes.new('GeometryNodeInputNamedAttribute');matrix.data_type='FLOAT4X4';matrix.inputs['Name'].default_value='usd_instance_transform'
        transform=tree.nodes.new('GeometryNodeSetInstanceTransform')
        tree.links.new(inst.outputs['Instances'],transform.inputs['Instances'])
        tree.links.new(matrix.outputs['Attribute'],transform.inputs['Transform'])
        # Blender 5.2's SSS neighborhood buffer uses 16-bit object IDs. A
        # scene with more draw resources otherwise blacks out SSS materials.
        # Enable realization only when the scene exceeds that resource budget.
        realize=tree.nodes.new('GeometryNodeRealizeInstances');realize.name='USD resource limit'
        realize.mute=True
        tree.links.new(transform.outputs['Instances'],realize.inputs['Geometry'])
        tree.links.new(realize.outputs['Geometry'],out.inputs['Geometry'])
        obj.modifiers.new('USD instances','NODES').node_group=tree
    data=obj.data
    if len(data.vertices)!=len(transforms):
        data.clear_geometry();data.vertices.add(len(transforms))
    attr=data.attributes.get('usd_instance_transform') or data.attributes.new('usd_instance_transform','FLOAT4X4','POINT')
    # USD row-major matrix storage equals Blender's column-major attribute
    # storage for the equivalent transposed (column-vector) transform.
    attr.data.foreach_set('value',np.asarray(transforms,dtype=np.float32).ravel())
    primvars=rgba_primvars(worker,key,len(transforms))
    for name in [a.name for a in data.attributes if a.name.startswith(INSTANCE_PREFIX) and a.name not in primvars]:
        data.attributes.remove(data.attributes[name])
    for name,values in primvars.items():
        attribute=data.attributes.get(name) or data.attributes.new(name,'FLOAT_COLOR','POINT')
        attribute.data.foreach_set('color',values.ravel())
    data.update();obj.matrix_world=worker.basis
    obj.hide_render=not visible;obj.hide_set(not visible,view_layer=worker.view_layer)
    obj['usd_instance_count']=len(transforms)


def configure(worker):
    total=len(worker.objects)+sum(len(group) for group in worker.instances.values())+sum(int(o['usd_instance_count']) for o in worker.point_instances.values())
    def has_sss(obj):
        if not hasattr(obj.data,'materials'):return False
        return any(m and m.node_tree and any(
            n.bl_idname=='ShaderNodeSubsurfaceScattering' or
            (n.bl_idname=='ShaderNodeBsdfPrincipled' and
             (n.inputs['Subsurface Weight'].is_linked or n.inputs['Subsurface Weight'].default_value>0.))
            for n in m.node_tree.nodes) for m in obj.data.materials)
    # Blender assigns draw-resource IDs in collection order. Put SSS surfaces
    # first so their IDs fit its UINT_16 scattering buffer, while all remaining
    # instances retain shared geometry and their individual optical thickness.
    objects=list(worker.scene.collection.objects)
    sss={o for o in objects if has_sss(o)}
    groups={k:o for k,o in worker.point_instances.items() if has_sss(worker.objects[k])}
    sss.update(groups.values())
    count=len(sss)+sum(int(o['usd_instance_count'])-1 for o in groups.values())
    realized=set()
    for key,obj in sorted(groups.items(),key=lambda pair:int(pair[1]['usd_instance_count']),reverse=True):
        if count<=60000:break
        count-=int(obj['usd_instance_count'])-1;realized.add(key)
    if count>60000:raise ValueError('Too many independent subsurface objects for Blender\'s 16-bit scattering buffer')
    for key,obj in worker.point_instances.items():
        node=obj.modifiers[0].node_group.nodes['USD resource limit']
        if node.mute==(key in realized):
            node.mute=key not in realized
            if key in realized:print('[EEVEE] Batching dense subsurface instances to fit Blender\'s 16-bit scattering buffer; all instances retained.',flush=True)
    if total>60000:
        ordered=sorted(objects,key=lambda o:o not in sss)
        if ordered!=objects:
            for obj in objects:worker.scene.collection.objects.unlink(obj)
            for obj in ordered:worker.scene.collection.objects.link(obj)
    return total,bool(realized)
