"""USD linear BasisCurves become native EEVEE hair curves with varying radii."""
import bpy
import numpy as np
from mathutils import Matrix

def sync(worker,update):
    key=update['id'];counts=np.asarray(update['counts'],dtype=np.int32).reshape(-1)
    points=np.asarray(update['points'],dtype=np.float32).reshape(-1,3)
    indices=np.asarray(update.get('indices',[]),dtype=np.int32).reshape(-1)
    if len(indices):points=points[indices]
    if int(counts.sum())!=len(points):raise ValueError('Curve topology/point count mismatch: '+key)
    if update.get('type','linear')!='linear':
        raise ValueError('Unsupported USD cubic curve basis '+update.get('basis','')+' at '+key)
    obj=worker.objects.get(key)
    if obj is None:
        obj=bpy.data.objects.new(key,bpy.data.hair_curves.new(key));worker.scene.collection.objects.link(obj);worker.objects[key]=obj
    data=obj.data
    existing=np.empty(len(data.curves),dtype=np.int32)
    if len(existing):data.curves.foreach_get('points_length',existing)
    if len(existing)!=len(counts) or (existing!=counts).any():
        if len(data.curves):data.remove_curves()
        data.add_curves(counts.tolist())
    data.set_types(type='POLY')
    data.position_data.foreach_set('vector',points.ravel())
    widths=update.get('widths')
    widths=np.asarray([.01] if widths is None or not len(widths) else widths,dtype=np.float32).reshape(-1)
    interp=update.get('widths_interpolation','constant')
    if interp=='constant':widths=np.full(len(points),widths[0],dtype=np.float32)
    elif interp=='uniform':widths=np.repeat(widths,counts)
    elif len(indices):widths=widths[indices]
    if len(widths)!=len(points):raise ValueError('Curve width count mismatch: '+key)
    radius=data.attributes.get('radius') or data.attributes.new('radius','FLOAT','POINT')
    radius.data.foreach_set('value',np.maximum(widths,0)*.5)
    cyclic=data.attributes.get('cyclic') or data.attributes.new('cyclic','BOOLEAN','CURVE')
    cyclic.data.foreach_set('value',np.full(len(counts),update.get('wrap')=='periodic'))
    obj.matrix_world=worker.basis@Matrix(update['transform']).transposed()
    obj.hide_render=not update.get('visible',True);obj.hide_set(obj.hide_render,view_layer=worker.view_layer)
    if 'prim_id' in update:worker.prim_ids[key]=int(update['prim_id'])
    worker.picks.assign(obj,worker.prim_ids.get(key,-1))
    worker.bind(key,update.get('material',''))
    data.update_tag()
