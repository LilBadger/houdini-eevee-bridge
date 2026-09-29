"""Native, visibly wired Solaris sources for the EEVEE integration demo."""
import json
import math

import hou
from pxr import Gf, Sdf, UsdGeom, UsdShade

VERSION = '1'
GEOMETRY = (
    ('sphere_red', 'RedSphere', 'Red', (-2.2, 1, 0)),
    ('sphere_metal', 'MetalSphere', 'Metal', (0, 1, 0)),
    ('sphere_blue', 'BlueSphere', 'Blue', (2.2, 1, 0)),
    ('floor', 'Floor', 'Floor', (0, 0, 0)),
)


def _node(stage, kind, name, comment, position, color):
    node = stage.node(name)
    if node is not None:
        if node.userData('eevee_demo_owned') != '1':
            raise RuntimeError('Demo node name is already in use: ' + node.path())
        return node
    node = stage.createNode(kind, name)
    node.setUserData('eevee_demo_owned', '1')
    node.setComment(comment)
    node.setGenericFlag(hou.nodeFlag.DisplayComment, True)
    node.setPosition(hou.Vector2(position))
    node.setColor(hou.Color(color))
    return node


def _look_at(node, position, target):
    matrix = Gf.Matrix4d().SetLookAt(Gf.Vec3d(*position), Gf.Vec3d(*target), Gf.Vec3d(0, 1, 0)).GetInverse()
    transform = hou.Matrix4(tuple(float(matrix[r][c]) for r in range(4) for c in range(4)))
    node.parmTuple('t').set(position)
    node.parmTuple('r').set(transform.extractRotates())


def camera_orbit(node, degrees):
    angle = math.radians(degrees)
    _look_at(node, (5 * math.cos(angle) + 10 * math.sin(angle), 4.5,
                   10 * math.cos(angle) - 5 * math.sin(angle)), (0, .8, 0))


def build(stage):
    existing = stage.node('EEVEE_OUT')
    if existing is not None and existing.userData('eevee_demo_version') == VERSION:
        if existing.type().name() == 'null':
            import render_settings
            render_settings.install(existing.path())
        return existing
    geometry = []
    for index, (name, prim, material, position) in enumerate(GEOMETRY):
        node = _node(stage, 'sopcreate', name, 'Editable Houdini geometry\nDouble-click to edit SOPs',
                     (index * 3.5, 3), (.35, .65, .95))
        node.parm('primpath').set('/World/' + prim)
        node.parm('pathprefix').set('/World/' + prim)
        node.parmTuple('t').set(position)
        subnet = node.node('sopnet/create')
        source = subnet.createNode('grid' if name == 'floor' else 'sphere', 'shape')
        if name == 'floor':
            source.setParms({'type': 'poly', 'orient': 'zx', 'sizex': 200, 'sizey': 200, 'rows': 2, 'cols': 2})
        else:
            source.setParms({'type': 'polymesh', 'orient': 'y', 'rows': 49, 'cols': 96})
            source.parmTuple('rad').set((1, 1, 1))
        normals = subnet.createNode('normal', 'normals')
        normals.setInput(0, source)
        normals.setParms({'type': 'typepoint', 'cuspangle': 180})
        output = subnet.createNode('output', 'OUT')
        output.setInput(0, normals)
        output.setDisplayFlag(True)
        output.setRenderFlag(True)
        subnet.layoutChildren()
        geometry.append(node)
    merged = _node(stage, 'merge', 'merge_geometry', 'Four Houdini geometry branches', (5.25, 0), (.35, .65, .95))
    for index, node in enumerate(geometry):
        merged.setInput(index, node)

    library = _node(stage, 'materiallibrary', 'materials', 'Houdini USD Preview Surface materials\nDouble-click to edit shaders',
                    (5.25, -2), (.85, .55, .25))
    library.setInput(0, merged)
    library.parm('matpathprefix').set('/World/Looks/')
    for name, color, metallic, roughness in (
        ('Red', (.6, .045, .02), 0, .2), ('Metal', (.7, .72, .78), 1, .15),
        ('Blue', (.02, .2, .45), .5, .4), ('Floor', (.14, .16, .2), 0, .3),
    ):
        shader = library.createNode('usdpreviewsurface', name)
        shader.parmTuple('diffuseColor').set(color)
        shader.setParms({'metallic': metallic, 'roughness': roughness})
        shader.setMaterialFlag(True)
    library.layoutChildren()

    toon = _node(stage, 'pythonscript', 'eevee_toon_material', 'Stage-authored EEVEE Shader to RGB\nOverrides only the Blue material',
                 (5.25, -4), (.85, .55, .25))
    toon.setInput(0, library)
    group = toon.parmTemplateGroup()
    group.insertBefore(group.entries()[0], hou.FolderParmTemplate('toon_controls', 'EEVEE Material', parm_templates=(
        hou.ToggleParmTemplate('enabled', 'Use Shader to RGB', default_value=True),
        hou.FloatParmTemplate('threshold', 'Light Threshold', 1, default_value=(.35,), min=0, max=1),
    )))
    toon.setParmTemplateGroup(group)
    toon.parm('python').set('import linked_demo\nn = hou.pwd()\nlinked_demo.author_toon(n.editableStage(), n.evalParm("enabled"), n.evalParm("threshold"))\n')

    assigned = _node(stage, 'assignmaterial', 'assign_materials', 'Bind Houdini materials to source objects',
                     (5.25, -6), (.85, .55, .25))
    assigned.setInput(0, toon)
    assigned.parm('nummaterials').set(len(GEOMETRY))
    for index, (_, prim, material, _) in enumerate(GEOMETRY, 1):
        assigned.parm('primpattern' + str(index)).set('/World/' + prim)
        assigned.parm('matspecpath' + str(index)).set('/World/Looks/' + material)

    light = _node(stage, 'light', 'key_light', 'Houdini area light', (10, -4), (.95, .8, .35))
    light.setParms({'primpath': '/World/Key', 'lighttype': 'UsdLuxRectLight'})
    for name, value in [('inputs:intensity', 1800), ('inputs:width', 5), ('inputs:height', 5)]:
        light.parm(hou.text.encodeParm(name)).set(value)
    _look_at(light, (2, 6, 4), (0, 0, 0))

    camera = _node(stage, 'camera', 'render_camera', 'Houdini camera used by EEVEE', (13.5, -4), (.7, .6, .95))
    camera.setParms({'primpath': '/World/Camera', 'focalLength': 45, 'aperture': 'set',
                     'horizontalAperture': 36, 'verticalAperture': 20.25,
                     'clippingRange1': .1, 'clippingRange2': 1000})
    camera_orbit(camera, 0)

    scene = _node(stage, 'merge', 'merge_scene', 'Geometry + materials + light + camera', (7, -8), (.45, .75, .6))
    for index, node in enumerate((assigned, light, camera)):
        scene.setInput(index, node)
    axes = _node(stage, 'configurelayer', 'stage_coordinates', 'Houdini Y-up / meters', (7, -10), (.45, .75, .6))
    axes.setInput(0, scene)
    axes.setParms({'setupaxis': True, 'upaxis': 'y', 'setmetersperunit': True, 'metersperunit': 1})
    output = _node(stage, 'null', 'EEVEE_OUT', 'Display this stage with EEVEE Bridge\nAll scene content comes from the wired inputs',
                   (7, -12), (.25, .85, .45))
    output.setInput(0, axes)
    output.cook(force=True)
    errors = [(n.path(), n.errors()) for n in (*geometry, merged, library, toon, assigned, light, camera, scene, axes, output) if n.errors()]
    if errors:
        raise RuntimeError('Linked demo failed to cook: ' + repr(errors))
    report = inspect(output)
    if len(report['meshes']) != 4 or len(report['lights']) != 1 or len(report['cameras']) != 1:
        raise RuntimeError('Incomplete linked stage: ' + repr(report))
    if any(not m['material'] for m in report['meshes']):
        raise RuntimeError('Missing material binding: ' + repr(report))
    output.setUserData('eevee_demo_version', VERSION)
    import render_settings
    render_settings.install(output.path())
    output = stage.node('EEVEE_OUT')
    # These three names were generated by the previous prototype/inspection tools.
    for path in ('/stage/EEVEE_demo', '/stage/_eevee_inspect', '/obj/_eevee_inspect'):
        old = hou.node(path)
        if old is not None:
            old.destroy()
    return output


def author_toon(stage, enabled, threshold):
    if not enabled:
        return
    from demo_scene import toon_graph
    graph = toon_graph()
    graph['nodes'][2]['inputs']['1'] = threshold
    material = UsdShade.Material.Get(stage, '/World/Looks/Blue')
    if not material:
        raise RuntimeError('Blue material must be authored by the upstream Material Library')
    shader = UsdShade.Shader.Define(stage, '/World/Looks/Blue/EEVEE_ShaderToRGB')
    shader.CreateIdAttr('EeveeShaderGraph')
    shader.CreateInput('graph', Sdf.ValueTypeNames.String).Set(json.dumps(graph))
    shader.CreateOutput('surface', Sdf.ValueTypeNames.Token)
    material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), 'surface')


def inspect(output):
    stage = output.stage()
    report = {'output': output.path(), 'up_axis': str(UsdGeom.GetStageUpAxis(stage)),
              'meshes': [], 'lights': [], 'cameras': [], 'nodes': [], 'materials': []}
    for node in output.parent().children():
        if node.userData('eevee_demo_owned') == '1':
            report['nodes'].append({'path': node.path(), 'type': node.type().name(),
                                    'inputs': [n.path() if n else None for n in node.inputs()],
                                    'errors': node.errors()})
    for prim in stage.Traverse():
        if prim.IsA(UsdGeom.Mesh):
            mesh = UsdGeom.Mesh(prim)
            binding = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()[0]
            matrix = UsdGeom.XformCache().GetLocalToWorldTransform(prim)
            report['meshes'].append({'prim': str(prim.GetPath()), 'points': len(mesh.GetPointsAttr().Get() or []),
                                     'material': str(binding.GetPath()) if binding else None,
                                     'position': list(matrix.ExtractTranslation()),
                                     'normals': len(mesh.GetNormalsAttr().Get() or []),
                                     'normals_interpolation': str(mesh.GetNormalsInterpolation())})
        elif prim.GetTypeName().endswith('Light'):
            report['lights'].append(str(prim.GetPath()))
        elif prim.IsA(UsdGeom.Camera):
            report['cameras'].append(str(prim.GetPath()))
        elif prim.IsA(UsdShade.Shader):
            shader = UsdShade.Shader(prim)
            color = shader.GetInput('diffuseColor').Get() if shader.GetInput('diffuseColor') else None
            report['materials'].append({'prim': str(prim.GetPath()), 'shader': str(shader.GetIdAttr().Get()),
                                        'color': list(color) if color is not None else None})
    return report


def edit(parameters):
    """Test helper: change the real source-node parameters, then let LOPs cook."""
    for name, value in parameters.items():
        if name == 'red':
            hou.node('/stage/materials/Red').parmTuple('diffuseColor').set(value)
        elif name == 'key_power':
            hou.node('/stage/key_light').parm(hou.text.encodeParm('inputs:intensity')).set(value)
        elif name == 'orbit':
            camera_orbit(hou.node('/stage/render_camera'), value)
        elif name == 'toon':
            hou.node('/stage/eevee_toon_material').parm('enabled').set(value)
        elif name == 'red_position':
            hou.node('/stage/sphere_red').parmTuple('t').set(value)
        elif name == 'red_radius':
            hou.node('/stage/sphere_red/sopnet/create/shape').parmTuple('rad').set((value,) * 3)
        else:
            raise ValueError('Unknown demo parameter: ' + name)
    hou.ui.triggerUpdate()
