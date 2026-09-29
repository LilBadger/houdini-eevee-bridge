"""Stage-owned EEVEE volume material and binding LOP."""
import json
from pathlib import Path
import hou
from pxr import Sdf, UsdShade

ROOT = Path(__file__).resolve().parents[1]
TYPE = 'eevee::volume_material::1.0'


def templates():
    group=hou.ParmTemplateGroup()
    parms=[
        hou.StringParmTemplate('primpattern','Volumes / Primitives',1,default_value=('/World/Smoke',)),
        hou.StringParmTemplate('matpath','Material Path',1,default_value=('/Looks/EEVEEVolume',)),
        hou.StringParmTemplate('density_field','Density Field',1,default_value=('density',)),
        hou.FloatParmTemplate('density','Density',1,default_value=(1.,),min=0,max=10),
        hou.FloatParmTemplate('color','Scattering Color',3,default_value=(.8,.8,.8),naming_scheme=hou.parmNamingScheme.RGBA),
        hou.StringParmTemplate('color_field','Color Field',1,default_value=('',)),
        hou.FloatParmTemplate('anisotropy','Anisotropy',1,default_value=(0.,),min=-1,max=1),
        hou.FloatParmTemplate('absorption','Absorption Color',3,default_value=(0.,0.,0.),naming_scheme=hou.parmNamingScheme.RGBA),
        hou.FloatParmTemplate('emission','Emission Strength',1,default_value=(0.,),min=0,max=20),
        hou.FloatParmTemplate('emission_color','Emission Color',3,default_value=(1.,1.,1.),naming_scheme=hou.parmNamingScheme.RGBA),
        hou.StringParmTemplate('emission_field','Emission / Flame Field',1,default_value=('',)),
        hou.FloatParmTemplate('blackbody','Blackbody Intensity',1,default_value=(0.,),min=0,max=10),
        hou.FloatParmTemplate('blackbody_tint','Blackbody Tint',3,default_value=(1.,1.,1.),naming_scheme=hou.parmNamingScheme.RGBA),
        hou.StringParmTemplate('temperature_field','Temperature Field',1,default_value=('temperature',)),
        hou.FloatParmTemplate('temperature_scale','Temperature Scale',1,default_value=(1.,),min=0,max=2000),
        hou.FloatParmTemplate('temperature_offset','Temperature Offset (K)',1,default_value=(0.,),min=0,max=10000),
    ]
    for p in parms: group.append(p)
    return group


def graph(node):
    rgba=lambda name:[*node.evalParmTuple(name),1.]
    inputs={'Color':rgba('color'), 'Color Attribute':node.evalParm('color_field'),
            'Density':node.evalParm('density'), 'Density Attribute':node.evalParm('density_field'),
            'Anisotropy':node.evalParm('anisotropy'), 'Absorption Color':rgba('absorption'),
            'Emission Strength':node.evalParm('emission'), 'Emission Color':rgba('emission_color'),
            'Blackbody Intensity':node.evalParm('blackbody'), 'Blackbody Tint':rgba('blackbody_tint'),
            'Temperature Attribute':'', 'Temperature':node.evalParm('temperature_offset')}
    result={'nodes':[{'id':'volume','type':'ShaderNodeVolumePrincipled','inputs':inputs},
                     {'id':'output','type':'ShaderNodeOutputMaterial'}],
            'links':[['volume','Volume','output','Volume']]}
    for field, scale, bias, socket in (
        (node.evalParm('emission_field'),node.evalParm('emission'),0.,'Emission Strength'),
        (node.evalParm('temperature_field'),node.evalParm('temperature_scale'),node.evalParm('temperature_offset'),'Temperature')):
        if not field: continue
        key='field_'+socket.replace(' ','_')
        result['nodes'].extend([
            {'id':key,'type':'ShaderNodeAttribute','properties':{'attribute_name':field}},
            {'id':key+'_scale','type':'ShaderNodeMath','properties':{'operation':'MULTIPLY_ADD'},'inputs':{'1':scale,'2':bias}},
        ])
        result['links'].extend([[key,'Fac',key+'_scale','Value'],[key+'_scale','Value','volume',socket]])
    return result


def author(python_node):
    node=python_node.parent()
    rule=hou.LopSelectionRule(); rule.setPathPattern(node.evalParm('primpattern'))
    targets=rule.expandedPaths(node.inputs()[0])
    stage=python_node.editableStage()
    path=Sdf.Path(node.evalParm('matpath'))
    if not path.IsAbsolutePath() or not path.IsPrimPath():
        raise hou.NodeError('Choose an absolute Material Path.')
    material=UsdShade.Material.Define(stage,path)
    shader=UsdShade.Shader.Define(stage,path.AppendChild('Volume'))
    shader.CreateIdAttr('EeveeShaderGraph')
    shader.CreateInput('graph',Sdf.ValueTypeNames.String).Set(json.dumps(graph(node)))
    shader.CreateOutput('volume',Sdf.ValueTypeNames.Token)
    material.CreateVolumeOutput('eevee').ConnectToSource(shader.ConnectableAPI(),'volume')
    material.CreateVolumeOutput().ConnectToSource(shader.ConnectableAPI(),'volume')
    for target in targets:
        prim=stage.GetPrimAtPath(str(target))
        if prim: UsdShade.MaterialBindingAPI.Apply(prim).Bind(material)


def install():
    library=ROOT/'houdini/otls/eevee_volume_material.hda'
    existing=hou.lopNodeTypeCategory().nodeTypes().get(TYPE)
    if existing:
        existing.definition().setParmTemplateGroup(templates())
        leftover=hou.node('/stage/_eevee_volume_definition')
        if leftover: leftover.destroy()
        return str(library)
    subnet=hou.node('/stage').createNode('subnet','_eevee_volume_definition')
    try:
        script=subnet.createNode('pythonscript','author_volume_material')
        script.setInput(0,subnet.indirectInputs()[0])
        script.parm('python').set('import volume_material\nvolume_material.author(hou.pwd())\n')
        output=subnet.node('output0') or subnet.createNode('output','output0')
        output.setInput(0,script); output.setDisplayFlag(True)
        asset=subnet.createDigitalAsset(name=TYPE,hda_file_name=str(library),description='EEVEE Volume Material',min_num_inputs=1,max_num_inputs=1)
        definition=asset.type().definition()
        definition.setParmTemplateGroup(templates())
        definition.setIcon('VOP_volume')
        definition.addSection('Help','# EEVEE Volume Material\n\nAssign a Stage-owned Principled Volume shader. Density and temperature field names are USD volume field relationship names. Use Temperature Scale and Offset to convert simulation temperatures into kelvin. Empty density field gives a uniform volume inside a closed mesh.\n')
    finally:
        temporary=hou.node('/stage/_eevee_volume_definition')
        if temporary: temporary.destroy()
    return str(library)
