"""Translate USD Preview Surface networks into actual EEVEE shader nodes."""
import math
import bpy

from shader_utils import image_file, primvar

INPUTS = {'diffuseColor': 'Base Color', 'base_color': 'Base Color',
          'metallic': 'Metallic', 'roughness': 'Roughness', 'opacity': 'Alpha',
          'ior': 'IOR', 'transmission': 'Transmission Weight',
          'transmission_weight': 'Transmission Weight', 'coat': 'Coat Weight',
          'clearcoat': 'Coat Weight', 'clearcoatRoughness': 'Coat Roughness',
          'emissiveColor': 'Emission Color', 'emission_color': 'Emission Color',
          'emission_strength': 'Emission Strength', 'subsurface': 'Subsurface Weight',
          'normal': 'Normal'}
DEFAULTS = {'diffuseColor': [.18, .18, .18], 'metallic': 0., 'roughness': .5,
            'opacity': 1., 'ior': 1.5, 'emissiveColor': [0., 0., 0.]}


def texture_color_space(parameters):
    """USD sourceColorSpace is raw, sRGB or auto. Hydra's scene-index adapter may also
    pass the asset's colorSpace metadata as colorSpace:file. For auto, Blender decides
    by file type, as USD specifies: 8-bit images are sRGB, float images linear."""
    source = parameters.get('sourceColorSpace', 'auto')
    if source in ('raw', 'sRGB'):
        return source
    return parameters.get('colorSpace:file') or 'auto'


def texture(filename, color_space):
    try:
        return image_file(filename, color_space)
    except ValueError as exc:
        if 'color space' in str(exc) and color_space != 'auto':
            print('[EEVEE] ' + str(exc) + '; using the file\'s own color space', flush=True)
            return texture(filename, 'auto')
        print('[EEVEE] Missing texture: ' + filename, flush=True)
        image = bpy.data.images.get('__missing_texture') or bpy.data.images.new('__missing_texture', 1, 1)
        image.pixels[:] = (1, 0, 1, 1)
        return image


def displacement_input(tree, value=0.):
    """USD Preview Surface displacement: a distance along the normal, in object space."""
    output = next(n for n in tree.nodes if n.bl_idname == 'ShaderNodeOutputMaterial')
    node = tree.nodes.new('ShaderNodeDisplacement')
    node.inputs['Midlevel'].default_value = 0.
    node.inputs['Scale'].default_value = 1.
    node.inputs['Height'].default_value = value
    tree.links.new(node.outputs['Displacement'], output.inputs['Displacement'])
    return node.inputs['Height']


def principled(tree, parameters):
    node = tree.nodes.new('ShaderNodeBsdfPrincipled')
    # Reset to USD defaults every time: an unauthored value must not retain its
    # previous Blender value after a Houdini parameter is restored to default.
    for name, value in {**DEFAULTS, **parameters}.items():
        if name in INPUTS and name != 'normal':
            socket = node.inputs[INPUTS[name]]
            if socket.type == 'RGBA' and isinstance(value, (list, tuple)) and len(value) == 3:
                value = [*value, 1.]
            socket.default_value = value
    node.inputs['Emission Strength'].default_value = parameters.get('emission_strength', 1.)
    output = tree.nodes.new('ShaderNodeOutputMaterial')
    tree.links.new(node.outputs['BSDF'], output.inputs['Surface'])
    if parameters.get('displacement'):
        displacement_input(tree, float(parameters['displacement']))
    return node


def network(tree, definition):
    tree.nodes.clear()
    inputs, outputs = {}, {}
    nodes_by_id = {n['id']: n for n in definition['nodes']}
    incoming = {(target, socket): (source, output) for source,output,target,socket in definition.get('links', [])}
    def normal_uv(key, visited=None):
        visited = set() if visited is None else visited
        if key in visited: return ''
        visited.add(key)
        item = nodes_by_id[key]
        if item['type'] == 'UsdPrimvarReader_float2': return item['parameters'].get('varname', 'st')
        for socket in ('normal', 'st', 'in'):
            link = incoming.get((key,socket))
            if link:
                value = normal_uv(link[0], visited)
                if value: return value
        return ''
    for item in definition['nodes']:
        key, kind, p = item['id'], item['type'], item['parameters']
        ins, outs = {}, {}
        if kind == 'UsdPreviewSurface':
            node = principled(tree, p)
            ins = {name: node.inputs[target] for name, target in INPUTS.items()}
            # USD Preview normals are tangent-space [-1,1]; Blender Normal Map
            # consumes [0,1]. Undo that encoding at this connection boundary.
            scale = tree.nodes.new('ShaderNodeVectorMath'); scale.operation = 'MULTIPLY_ADD'
            scale.inputs[1].default_value = (.5, .5, .5)
            scale.inputs[2].default_value = (.5, .5, .5)
            normal = tree.nodes.new('ShaderNodeNormalMap')
            normal.uv_map = normal_uv(key)
            tree.links.new(scale.outputs['Vector'], normal.inputs['Color'])
            tree.links.new(normal.outputs['Normal'], node.inputs['Normal'])
            ins['normal'] = scale.inputs[0]
            # An unconnected normal keeps the geometric normal.
            scale.inputs[0].default_value = p.get('normal', (0, 0, 1))
            if (key, 'displacement') in incoming and not p.get('displacement'):
                ins['displacement'] = displacement_input(tree)
            elif p.get('displacement'):
                ins['displacement'] = next(n for n in tree.nodes if n.bl_idname == 'ShaderNodeDisplacement').inputs['Height']
        elif kind == 'UsdUVTexture':
            node = tree.nodes.new('ShaderNodeTexImage')
            filename = p.get('file', '')
            if filename:
                node.image = texture(filename, texture_color_space(p))
            # The unauthored useMetadata falls back to black, as in Karma. Blender has
            # one wrap mode per image node, so wrapS is used for both directions.
            wrap = p.get('wrapS', 'useMetadata')
            node.extension = {'clamp': 'EXTEND', 'repeat': 'REPEAT', 'mirror': 'MIRROR'}.get(wrap, 'CLIP')
            ins['st'] = node.inputs['Vector']
            scale = p.get('scale', (1, 1, 1, 1)); bias = p.get('bias', (0, 0, 0, 0))
            rgb = tree.nodes.new('ShaderNodeVectorMath'); rgb.operation = 'MULTIPLY_ADD'
            rgb.inputs[1].default_value = scale[:3]; rgb.inputs[2].default_value = bias[:3]
            tree.links.new(node.outputs['Color'], rgb.inputs[0])
            separate = tree.nodes.new('ShaderNodeSeparateXYZ')
            tree.links.new(rgb.outputs['Vector'], separate.inputs[0])
            alpha = tree.nodes.new('ShaderNodeMath'); alpha.operation = 'MULTIPLY_ADD'
            alpha.inputs[1].default_value = scale[3]; alpha.inputs[2].default_value = bias[3]
            tree.links.new(node.outputs['Alpha'], alpha.inputs[0])
            outs = {'rgb': rgb.outputs['Vector'], 'r': separate.outputs['X'],
                    'g': separate.outputs['Y'], 'b': separate.outputs['Z'], 'a': alpha.outputs[0]}
        elif kind == 'UsdTransform2d':
            node = tree.nodes.new('ShaderNodeMapping'); node.vector_type = 'POINT'
            node.inputs['Location'].default_value = (*p.get('translation', (0, 0)), 0)
            node.inputs['Rotation'].default_value = (0, 0, math.radians(p.get('rotation', 0)))
            node.inputs['Scale'].default_value = (*p.get('scale', (1, 1)), 1)
            ins['in'] = node.inputs['Vector']; outs['result'] = node.outputs['Vector']
        elif kind == 'UsdPrimvarReader_float2':
            node = tree.nodes.new('ShaderNodeUVMap'); node.uv_map = p.get('varname', 'st')
            outs['result'] = node.outputs['UV']
        elif kind.startswith('UsdPrimvarReader_'):
            varname = p.get('varname', '')
            node = primvar(tree, varname)
            outs['result'] = node.outputs['Fac' if kind.endswith('_float') else 'Vector']
        else:
            raise ValueError('Unsupported USD shader node: ' + kind)
        node.label = key.rsplit('/', 1)[-1]
        inputs[key], outputs[key] = ins, outs
    for source, source_socket, target, target_socket in definition.get('links', []):
        if target_socket in inputs[target] and source_socket in outputs[source]:
            tree.links.new(outputs[source][source_socket], inputs[target][target_socket])
