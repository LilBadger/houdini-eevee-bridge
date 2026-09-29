"""Stage-owned Dome Lights and EEVEE Render Settings world overrides."""
import json
import math
from mathutils import Matrix, Euler
from shader_utils import feed, vector, matrix_vector, image_file

IDENTITY = [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
DEFAULTS = {'source':'stage','intensity':1.,'exposure':0.,'color':[1.,1.,1.],
            'texture':'','rotation':[0.,0.,0.],'fallback':True}
# USD/OpenEXR latlong: +Y north, +Z image center, +X left quarter.
# Blender latlong: +Z north, +X image center, +Y left quarter.
USD_TO_TEXTURE = Matrix(((0,0,1),(1,0,0),(0,1,0)))


def direction_matrix(definition, basis, up_axis):
    params=definition.get('parameters',{})
    transform=Matrix(definition.get('transform',IDENTITY)).transposed().to_quaternion().to_matrix()
    # Hydra's legacy Dome Light is Y-pole; DomeLight_1 supplies its poleAxis.
    pole=params.get('poleAxis','Y')
    if pole=='scene': pole=up_axis
    if pole not in ('Y','Z'): raise ValueError('Unsupported Dome Light pole axis: '+str(pole))
    orient=(Matrix(params['domeOffset']).transposed().to_quaternion().to_matrix() if 'domeOffset' in params else
            Matrix.Rotation(math.pi/2,3,'X') if pole=='Z' else Matrix.Identity(3))
    return USD_TO_TEXTURE @ (transform @ orient).inverted() @ basis.to_3x3().inverted()


def sync(worker, configuration):
    config={**DEFAULTS,**configuration}
    if config['source'] not in ('stage','override','off'):
        raise ValueError('Unknown EEVEE environment source: '+str(config['source']))
    signature=json.dumps([config,worker.domes,worker.up_axis],sort_keys=True)
    if signature==worker.environment_signature: return
    # Update the signature only after successfully building the new world.
    tree=worker.scene.world.node_tree
    tree.nodes.clear()
    output=tree.nodes.new('ShaderNodeOutputWorld')
    definitions=[]
    multiplier=config['intensity']*2.**config['exposure']
    if config['source']=='stage':
        definitions=[d for _,d in sorted(worker.domes.items()) if d.get('visible',True)]
        # An intentionally disabled/hidden dome must not bring fallback lighting back.
        if not worker.domes and config['fallback']:
            definitions=[{'id':'Fallback Environment','parameters':{'color':[.035]*3,'intensity':1.}}]
    elif config['source']=='override':
        rotation=Euler([math.radians(x) for x in config['rotation']],'XYZ').to_matrix().to_4x4()
        definitions=[{'id':'Render Settings Environment','transform':[list(row) for row in rotation.transposed()],
                      'parameters':{'color':[1.,1.,1.], 'intensity':1., 'texture:file':config['texture'],
                                    'texture:format':'latlong','poleAxis':'scene'}}]
    shaders=[]
    for definition in definitions:
        params=definition.get('parameters',{})
        color=params.get('color',[1,1,1])
        intensity=params.get('intensity',1.)*2.**params.get('exposure',0.)*multiplier
        background=tree.nodes.new('ShaderNodeBackground'); background.label=definition['id']
        feed(tree,background.inputs['Strength'],intensity)
        filename=params.get('texture:file','')
        if filename:
            texture=tree.nodes.new('ShaderNodeTexEnvironment'); texture.label=filename
            projection=params.get('texture:format','automatic')
            if projection not in ('automatic','latlong','mirroredBall'):
                raise ValueError('Dome Light '+definition['id']+': unsupported HDRI format '+projection+'; use latlong or mirroredBall.')
            texture.projection='MIRROR_BALL' if projection=='mirroredBall' else 'EQUIRECTANGULAR'
            texture.image=image_file(filename,params.get('colorSpace:texture:file','auto'))
            coordinate=tree.nodes.new('ShaderNodeTexCoord')
            direction=matrix_vector(tree,direction_matrix(definition,worker.basis,worker.up_axis),coordinate.outputs['Generated'])
            feed(tree,texture.inputs['Vector'],direction)
            color=vector(tree,'MULTIPLY',texture.outputs['Color'],color)
        if params.get('enableColorTemperature',False):
            blackbody=tree.nodes.new('ShaderNodeBlackbody')
            blackbody.inputs['Temperature'].default_value=params.get('colorTemperature',6500.)
            color=vector(tree,'MULTIPLY',color,blackbody.outputs['Color'])
        color=vector(tree,'MULTIPLY',color,config['color'])
        feed(tree,background.inputs['Color'],color)
        shaders.append(background.outputs[0])
    if not shaders:
        black=tree.nodes.new('ShaderNodeBackground'); black.inputs['Strength'].default_value=0.
        shaders=[black.outputs[0]]
    result=shaders[0]
    for shader in shaders[1:]:
        add=tree.nodes.new('ShaderNodeAddShader')
        feed(tree,add.inputs[0],result); feed(tree,add.inputs[1],shader); result=add.outputs[0]
    feed(tree,output.inputs['Surface'],result)
    worker.environment_signature=signature
