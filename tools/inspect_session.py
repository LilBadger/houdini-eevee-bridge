"""Read scene and native-node metadata without changing its source geometry."""
import hou
from pxr import Usd, UsdGeom, UsdShade


def inspect(detail_nodes=()):
    root = hou.node('/stage')
    output = root.displayNode()
    stage = output.stage()
    result = {'display': output.path(), 'nodes': [], 'prims': []}
    for node in root.children():
        item = {'path': node.path(), 'type': node.type().name(), 'inputs': [n.path() if n else None for n in node.inputs()]}
        if node.name() in detail_nodes:
            item['parms'] = {p.name(): p.evalAsString() for p in node.parms()}
        result['nodes'].append(item)
    for prim in Usd.PrimRange.Stage(stage, Usd.TraverseInstanceProxies()):
        item = {'path': str(prim.GetPath()), 'type': prim.GetTypeName(), 'instance': prim.IsInstance(),
                'proxy': prim.IsInstanceProxy(), 'loaded': prim.IsLoaded()}
        if prim.IsA(UsdGeom.Imageable):
            imageable = UsdGeom.Imageable(prim)
            item.update(purpose=str(imageable.ComputePurpose()), visibility=str(imageable.ComputeVisibility()))
        if prim.IsA(UsdGeom.Mesh):
            mesh = UsdGeom.Mesh(prim)
            mat = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()[0]
            item.update(points=len(mesh.GetPointsAttr().Get() or []), material=str(mat.GetPath()) if mat else None)
        result['prims'].append(item)
    subnet = root.createNode('subnet', '_eevee_development')
    try:
        result['node_parameters'] = {}
        for kind in ('rendersettings', 'renderproduct', 'usdrender_rop'):
            node = subnet.createNode(kind)
            result['node_parameters'][kind] = {
                p.name(): {'value': p.evalAsString(), 'label': p.parmTemplate().label(),
                           'menu': p.menuItems() if p.parmTemplate().type() == hou.parmTemplateType.Menu else []}
                for p in node.parms()
            }
        result['subnet_children'] = [(n.name(), n.type().name()) for n in subnet.children()]
    finally:
        subnet.destroy()
    return result
