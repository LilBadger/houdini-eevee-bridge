"""Bounded integration fixtures kept separate from the user's displayed Stage."""
import json
from pathlib import Path
import hou
from pxr import Usd,UsdGeom,Sdf

ROOT=Path(__file__).resolve().parents[1]


def volume_fixture():
    stage=hou.node('/stage')
    name='_eevee_volume_validation'
    subnet=stage.node(name) or stage.createNode('subnet',name)
    sop=subnet.node('smoke') or subnet.createNode('sopcreate','smoke')
    sop.parm('primpath').set('/World/Smoke');sop.parm('pathprefix').set('/World/Smoke')
    geo=sop.node('sopnet/create')
    source=geo.node('density') or geo.createNode('python','density')
    source.parm('python').set('''import hou, math
geo=hou.pwd().geometry()
geo.addAttrib(hou.attribType.Prim, 'name', '')
volume=geo.createVolume(32,32,32,hou.BoundingBox(-1,-1,-1,1,1,1))
volume.setAttribValue('name','density')
volume.setAllVoxels([3*math.exp(-sum(((v-15.5)/9)**2 for v in (x,y,z))*2) for z in range(32) for y in range(32) for x in range(32)])
''')
    output=geo.node('OUT') or geo.createNode('output','OUT');output.setInput(0,source);output.setDisplayFlag(True);output.setRenderFlag(True)
    node=subnet.node('OUT') or subnet.createNode('null','OUT'); node.setInput(0,sop)
    geometry=node.stage()
    fields=[]
    for prim in geometry.Traverse():
        if 'Asset' in prim.GetTypeName():
            fields.append({'path':str(prim.GetPath()),'type':prim.GetTypeName(),
                           'attributes':{a.GetName():str(a.Get()) for a in prim.GetAttributes()}})
    path=ROOT/'artifacts/integration/houdini_volume.usdc'
    geometry.Export(str(path))
    # Prepare a standalone native-husk render while the live SOP owner exists.
    fixture=Usd.Stage.Open(str(ROOT/'artifacts/integration/passes.usda'))
    smoke=Usd.Stage.Open(str(path))
    fixture.RemovePrim('/World/Smoke')
    Sdf.CopySpec(smoke.GetRootLayer(),Sdf.Path('/World/Smoke'),fixture.GetRootLayer(),Sdf.Path('/World/Smoke'))
    config=json.loads(fixture.GetPrimAtPath('/Render/EEVEE').GetAttribute('eevee:config').Get())
    config['output']=str(ROOT/'artifacts/integration/houdini_volume.exr')
    fixture.GetPrimAtPath('/Render/EEVEE').GetAttribute('eevee:config').Set(json.dumps(config))
    fixture.Export(str(ROOT/'artifacts/integration/houdini_volume.usda'))
    return {'node':node.path(),'fields':fields}


def cleanup():
    node=hou.node('/stage/_eevee_volume_validation')
    if node:node.destroy()
    return {}


def activate_volume():
    import render_settings, volume_material
    subnet=hou.node('/stage/_eevee_volume_validation')
    viewer=hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)
    if not hasattr(hou.session,'_eevee_before_volume'):
        hou.session._eevee_before_volume=(viewer.pwd().path(),viewer.currentNode().path(),viewer.curViewport().cameraPath())
        hou.session._eevee_before_volume_camera=viewer.curViewport().defaultCamera().stash()
    lights=subnet.node('camera_and_light') or subnet.createNode('pythonscript','camera_and_light')
    lights.setInput(0,subnet.node('smoke'))
    lights.parm('python').set("""from pxr import Usd,Sdf
import hou
stage=hou.pwd().editableStage()
source=Usd.Stage.Open("""+repr(str(ROOT/'artifacts/integration/passes.usda'))+""")
for path in ('/World/Camera','/World/Light'):
    Sdf.CopySpec(source.GetRootLayer(),Sdf.Path(path),stage.GetEditTarget().GetLayer(),Sdf.Path(path))
""")
    material=subnet.node('volume_material') or subnet.createNode(volume_material.TYPE,'volume_material')
    material.setInput(0,lights)
    material.setParms({'primpattern':'/World/Smoke/volume_0','matpath':'/Looks/TestVolume','density':1.5,'colorr':.15,'colorg':.45,'colorb':.8})
    output=subnet.node('OUT');output.setInput(0,material)
    if output.type().name()!=render_settings.TYPE:
        render_settings.install(output.path())
        output=subnet.node('OUT')
    output.setParms({'resolution1':384,'resolution2':256,'eevee__taa_samples':16,'eevee__taa_render_samples':32,
                     'camera':'/World/Camera','outputimage':str(ROOT/'artifacts/integration/houdini_volume.exr'),
                     'output_multilayer':True,'pass__volume_light':True,'pass__z':True})
    output.setDisplayFlag(True)
    subnet_output=subnet.node('output0') or subnet.createNode('output','output0')
    subnet_output.setInput(0,output)
    main=hou.node('/stage/EEVEE_OUT')
    if not hasattr(hou.session,'_eevee_volume_input'):
        hou.session._eevee_volume_input=main.inputs()[0].path()
    merged=hou.node('/stage/_eevee_volume_merge_validation') or hou.node('/stage').createNode('merge','_eevee_volume_merge_validation')
    merged.setInput(0,hou.node(hou.session._eevee_volume_input));merged.setInput(1,subnet)
    main.setInput(0,merged);main.setDisplayFlag(True)
    viewer.setPwd(hou.node('/stage'));viewer.setCurrentNode(main);viewer.curViewport().setCamera(None)
    viewer.restartRenderer()
    viewer.curViewport().draw()
    hou.ui.triggerUpdate()
    return {'output':output.path(),'errors':output.errors()}


def restore_view():
    viewer=hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)
    pwd,path,camera=hou.session._eevee_before_volume
    if hasattr(hou.session,'_eevee_volume_input'):
        hou.node('/stage/EEVEE_OUT').setInput(0,hou.node(hou.session._eevee_volume_input))
        del hou.session._eevee_volume_input
        merged=hou.node('/stage/_eevee_volume_merge_validation')
        if merged: merged.destroy()
    output=hou.node(path)
    output.setDisplayFlag(True)
    viewer.setPwd(hou.node(pwd));viewer.setCurrentNode(output)
    viewer.curViewport().setCamera(camera or None)
    if hasattr(hou.session,'_eevee_before_volume_camera'):
        viewer.curViewport().setDefaultCamera(hou.session._eevee_before_volume_camera)
        del hou.session._eevee_before_volume_camera
    del hou.session._eevee_before_volume
    hou.ui.triggerUpdate()
    return {'output':path}


def render_volume():
    import render_settings
    render_settings.render(hou.node('/stage/_eevee_volume_validation/OUT'))
    return {'output':str(ROOT/'artifacts/integration/houdini_volume.exr')}
