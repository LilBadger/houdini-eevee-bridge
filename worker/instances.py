"""USD point instancers as Geometry Nodes instances, retaining full matrices."""
import bpy
import numpy as np

def remove(worker,key):
    obj=worker.point_instances.pop(key,None)
    if obj is None:return
    worker.picks.release(obj)
    data=obj.data;groups=[m.node_group for m in obj.modifiers if m.type=='NODES']
    bpy.data.objects.remove(obj,do_unlink=True)
    if not data.users:bpy.data.meshes.remove(data)
    for group in groups:
        if group and not group.users:bpy.data.node_groups.remove(group)

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
