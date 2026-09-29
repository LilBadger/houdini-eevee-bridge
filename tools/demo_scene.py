"""Build a small USD scene for exercising the native EEVEE Hydra adapter."""
import json
import math
from pxr import Gf, Sdf, UsdGeom, UsdLux, UsdShade


def material(stage, path, color, metallic=0, roughness=.3, graph=None):
    mat = UsdShade.Material.Define(stage, path)
    shader = UsdShade.Shader.Define(stage, path + '/Shader')
    shader.CreateIdAttr('EeveeShaderGraph' if graph else 'UsdPreviewSurface')
    if graph:
        shader.CreateInput('graph', Sdf.ValueTypeNames.String).Set(json.dumps(graph))
    else:
        shader.CreateInput('diffuseColor', Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*color))
        shader.CreateInput('metallic', Sdf.ValueTypeNames.Float).Set(metallic)
        shader.CreateInput('roughness', Sdf.ValueTypeNames.Float).Set(roughness)
    shader.CreateOutput('surface', Sdf.ValueTypeNames.Token)
    mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), 'surface')
    return mat


def sphere(stage, path, center, radius=1, segments=48, rings=24):
    points, faces, counts = [], [], []
    for ring in range(rings+1):
        latitude = math.pi * ring / rings
        for segment in range(segments):
            angle = 2 * math.pi * segment / segments
            points.append((radius*math.sin(latitude)*math.cos(angle), radius*math.cos(latitude), -radius*math.sin(latitude)*math.sin(angle)))
    for ring in range(rings):
        for segment in range(segments):
            a = ring * segments + segment
            b = ring * segments + (segment+1) % segments
            faces += [a, b, b+segments, a+segments]
            counts.append(4)
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.CreatePointsAttr(points)
    mesh.CreateFaceVertexCountsAttr(counts)
    mesh.CreateFaceVertexIndicesAttr(faces)
    mesh.CreateSubdivisionSchemeAttr('catmullClark')
    mesh.AddTranslateOp().Set(Gf.Vec3d(*center))
    return mesh


def build(stage, orbit=0, red=(.6,.045,.02), power=1800, toon=False):
    # LOPs supplies a non-root edit layer, so stage metadata belongs upstream.
    if stage.GetEditTarget().GetLayer() == stage.GetRootLayer():
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
        UsdGeom.SetStageMetersPerUnit(stage, 1)
    UsdGeom.Xform.Define(stage, '/World')
    for index, (color, metallic, roughness) in enumerate([(red,0,.2),((.7,.72,.78),1,.15),((.02,.2,.45),.5,.4)]):
        mat=material(stage, '/World/Looks/Material'+str(index), color, metallic, roughness, toon_graph() if toon and index==2 else None)
        mesh=sphere(stage, '/World/Sphere'+str(index), ((index-1)*2.2,1,0))
        UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(mat)
    floor=UsdGeom.Mesh.Define(stage,'/World/Floor')
    floor.CreatePointsAttr([(-100,0,100),(100,0,100),(100,0,-100),(-100,0,-100)])
    floor.CreateFaceVertexCountsAttr([4])
    floor.CreateFaceVertexIndicesAttr([0,1,2,3])
    floor.CreateSubdivisionSchemeAttr('none')
    mat=material(stage,'/World/Looks/Floor',(.14,.16,.2),0,.3)
    UsdShade.MaterialBindingAPI.Apply(floor.GetPrim()).Bind(mat)
    light=UsdLux.RectLight.Define(stage,'/World/Key')
    light.CreateIntensityAttr(power)
    light.CreateWidthAttr(5)
    light.CreateHeightAttr(5)
    light.AddTransformOp().Set(Gf.Matrix4d().SetLookAt(Gf.Vec3d(2,6,4),Gf.Vec3d(0,0,0),Gf.Vec3d(0,1,0)).GetInverse())
    camera=UsdGeom.Camera.Define(stage,'/World/Camera')
    camera.CreateFocalLengthAttr(45)
    camera.CreateHorizontalApertureAttr(36)
    camera.CreateVerticalApertureAttr(20.25)
    camera.CreateClippingRangeAttr(Gf.Vec2f(.1,1000))
    angle = math.radians(orbit)
    position = Gf.Vec3d(5*math.cos(angle)+10*math.sin(angle),4.5,10*math.cos(angle)-5*math.sin(angle))
    camera.AddTransformOp().Set(Gf.Matrix4d().SetLookAt(position,Gf.Vec3d(0,.8,0),Gf.Vec3d(0,1,0)).GetInverse())


def toon_graph():
    return {'nodes': [
        {'id': 'diffuse', 'type': 'ShaderNodeBsdfDiffuse', 'inputs': {'Color': [1,1,1,1]}},
        {'id': 'rgb', 'type': 'ShaderNodeShaderToRGB'},
        {'id': 'threshold', 'type': 'ShaderNodeMath', 'properties': {'operation': 'GREATER_THAN'}, 'inputs': {'1': .35}},
        {'id': 'colors', 'type': 'ShaderNodeMixRGB', 'inputs': {'1': [.008,.025,.08,1], '2': [.02,.5,1,1]}},
        {'id': 'emission', 'type': 'ShaderNodeEmission'},
        {'id': 'output', 'type': 'ShaderNodeOutputMaterial'},
    ], 'links': [
        ['diffuse','BSDF','rgb','Shader'], ['rgb','Color','threshold',0],
        ['threshold',0,'colors',0], ['colors',0,'emission','Color'],
        ['emission','Emission','output','Surface'],
    ]}
