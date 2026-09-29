"""Shared texture loading and small, explicit Blender shader operations."""
import glob
import os
import re
import bpy


class Channels(tuple):
    """Four independent shader values; Blender color sockets discard alpha."""


def image_file(filename, color_space='auto'):
    """Load a texture without changing the color space of another material's image."""
    filename = os.path.normpath(filename)
    udim = '<UDIM>' in filename or '%(UDIM)d' in filename
    if udim:
        filename = filename.replace('%(UDIM)d','<UDIM>')
        matches = sorted(glob.glob(filename.replace('<UDIM>','[0-9][0-9][0-9][0-9]')))
        if not matches: raise ValueError('No UDIM tiles found: '+filename)
        first = matches[0]
    else:
        first = filename
    aliases = {'raw':'Non-Color', 'Raw':'Non-Color', 'none':'Non-Color',
               'lin_rec709':'Linear Rec.709', 'lin_rec709_scene':'Linear Rec.709',
               'lin_ap1_scene':'ACEScg', 'lin_ap0_scene':'ACES2065-1',
               'srgb_texture':'sRGB', 'srgb_rec709_scene':'sRGB', 'sRGB':'sRGB'}
    wanted = aliases.get(color_space,color_space)
    for image in bpy.data.images:
        if image.get('hde_file') == filename and image.get('hde_color_space') == wanted:
            return image
    try:
        image = bpy.data.images.load(first, check_existing=False)
    except RuntimeError as exc:
        raise ValueError('Cannot load texture '+filename) from exc
    try:
        if wanted not in ('auto','',None):
            try: image.colorspace_settings.name = wanted
            except (TypeError, ValueError) as exc:
                raise ValueError('Texture color space '+str(color_space)+' is not available in Blender: '+filename) from exc
        if udim:
            image.source = 'TILED'
            image.filepath = filename
            pattern = re.compile(re.escape(filename).replace(re.escape('<UDIM>'),r'(\d{4})'))
            for path in matches:
                number = int(pattern.fullmatch(path)[1])
                if number not in {tile.number for tile in image.tiles}: image.tiles.new(number)
        image['hde_file'], image['hde_color_space'] = filename, wanted or 'auto'
        return image
    except Exception:
        bpy.data.images.remove(image)
        raise


def feed(tree, socket, value):
    if isinstance(value,Channels):
        # Vector/color consumers explicitly use RGB/XYZ. Alpha remains separately
        # addressable by MaterialX separate4/extract until such a conversion.
        value=combine(tree,value[:3]) if socket.type in ('RGBA','VECTOR') else scalar(tree,'MULTIPLY',scalar(tree,'ADD',scalar(tree,'ADD',value[0],value[1]),value[2]),1/3)
    if isinstance(value, bpy.types.NodeSocket):
        tree.links.new(value, socket)
    else:
        if socket.type in ('RGBA','VECTOR'):
            size = 4 if socket.type == 'RGBA' else 3
            if not isinstance(value,(list,tuple)): value = [value]*3
            value = list(value)[:size] + [1. if socket.type == 'RGBA' else 0.]*(size-len(value))
        elif isinstance(value,(list,tuple)):
            value = sum(value[:3])/len(value[:3])
        socket.default_value = value


def scalar(tree, operation, *values):
    node = tree.nodes.new('ShaderNodeMath'); node.operation = operation
    for socket,value in zip(node.inputs,values): feed(tree,socket,value)
    return node.outputs[0]


def vector(tree, operation, *values):
    node = tree.nodes.new('ShaderNodeVectorMath'); node.operation = operation
    for socket,value in zip(node.inputs,values): feed(tree,socket,value)
    return node.outputs['Value' if operation in ('DOT_PRODUCT','DISTANCE','LENGTH') else 'Vector']


def components(tree, value, size=3):
    if not isinstance(value,bpy.types.NodeSocket):
        return (list(value)+[0.]*size)[:size] if isinstance(value,(list,tuple)) else [value]*size
    if value.type in ('VALUE','INT','BOOLEAN'): return [value]*size
    node=tree.nodes.new('ShaderNodeSeparateXYZ'); feed(tree,node.inputs[0],value)
    return (list(node.outputs)+[1.])[:size]


def combine(tree, values):
    values=list(values)
    if len(values)==4: return Channels(values)
    node=tree.nodes.new('ShaderNodeCombineXYZ')
    for socket,value in zip(node.inputs,values): feed(tree,socket,value)
    return node.outputs[0]


def component_math(tree, operation, size, *values):
    if size == 1: return scalar(tree,operation,*values)
    if size == 3 and operation in ('ADD','SUBTRACT','MULTIPLY','DIVIDE','MINIMUM','MAXIMUM','ABSOLUTE','FLOOR','CEIL'):
        return vector(tree,operation,*values)
    values=[components(tree,v,size) for v in values]
    return combine(tree,[scalar(tree,operation,*(v[i] for v in values)) for i in range(size)])


def matrix_vector(tree, matrix, value):
    return combine(tree,[vector(tree,'DOT_PRODUCT',value,list(row)) for row in matrix])
