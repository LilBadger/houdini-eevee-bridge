"""Houdini-side demo setup and a bounded development test interface."""
import importlib
import os
from pathlib import Path

import hou

ROOT = Path(os.environ['HDEEVEE_PROJECT'])


def setup():
    viewer = hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)
    stage = hou.node('/stage')
    import linked_demo
    node = importlib.reload(linked_demo).build(stage)
    node.setDisplayFlag(True)
    node.setCurrent(True, clear_all_selected=True)
    viewer.setPwd(stage)
    viewer.curViewport().setCamera('/World/Camera')
    available = viewer.hydraRenderers()
    renderer = next((r for r in available if 'eevee' in r.lower()), None)
    if not renderer:
        raise RuntimeError('EEVEE plugin not discovered. Available: ' + repr(available))
    viewer.setHydraRenderer(renderer)
    if viewer.sceneGraphStageLocked():
        viewer.setSceneGraphStageLocked(False)
    if viewer.isRendererPaused():
        viewer.setRendererPaused(False)
    viewer.curViewport().draw()
    def refresh_view():
        if viewer.curViewport().cameraPath() != '/World/Camera':
            viewer.curViewport().setCamera('/World/Camera')
        viewer.referencePlane().setIsVisible(False)
        viewer.curViewport().draw()
        for pane in hou.ui.paneTabs():
            if pane.type() == hou.paneTabType.NetworkEditor:
                pane.setVisibleBounds(hou.BoundingRect(-1.5, -14, 18, 5))
    hou.ui.postEventCallback(refresh_view)
    for guide in (hou.viewportGuide.XYPlane, hou.viewportGuide.XZPlane, hou.viewportGuide.YZPlane,
                  hou.viewportGuide.OriginGnomon, hou.viewportGuide.NodeGuides):
        viewer.curViewport().settings().enableGuide(guide, False)
    viewer.constructionPlane().setIsVisible(False)
    viewer.referencePlane().setIsVisible(False)
    node.setSelected(True, clear_all_selected=True)
    for pane in hou.ui.paneTabs():
        if pane.type() == hou.paneTabType.NetworkEditor:
            pane.setPwd(stage)
            pane.setVisibleBounds(hou.BoundingRect(-1.5, -14, 18, 5))
    hip = ROOT / 'artifacts' / 'eevee_demo.hip'
    hou.hipFile.save(str(hip))
    hou.ui.triggerUpdate()
    return {'ok': True, 'renderer': viewer.currentHydraRenderer(), 'available': available,
            'hip': str(hip), 'houdini': hou.applicationVersionString(), 'pid': os.getpid()}


def control(request):
    viewer = hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)
    operation = request['op']
    result = {'ok': True, 'op': operation, 'pid': os.getpid()}
    if operation in ('bootstrap', 'linked_demo'):
        return setup()
    if operation == 'status':
        result.update(hip=hou.hipFile.path(), unsaved=hou.hipFile.hasUnsavedChanges(),
                      frame=hou.frame(),
                      view_camera={'translation':viewer.curViewport().defaultCamera().translation(),
                          'rotation':viewer.curViewport().defaultCamera().rotation().asTuple(),
                          'pivot':viewer.curViewport().defaultCamera().pivot(),
                          'focal':viewer.curViewport().defaultCamera().focalLength(),
                          'aperture':viewer.curViewport().defaultCamera().aperture()},
                      camera=viewer.curViewport().cameraPath(), pwd=viewer.pwd().path(),
                      stage_locked=viewer.sceneGraphStageLocked(), update_mode=str(hou.updateModeSetting()),
                      nodes=[(n.path(), n.type().name(), n.errors()) for n in hou.node('/stage').children()])
        if viewer.pwd().path() == '/stage':
            result.update(renderer=viewer.currentHydraRenderer(), renderers=viewer.hydraRenderers(), paused=viewer.isRendererPaused())
    elif operation == 'camera':
        if viewer.curViewport().cameraPath() != request['path']:
            viewer.curViewport().setCamera(request['path'])
    elif operation in ('reload_demo', 'inspect_stage'):
        import linked_demo
        importlib.reload(linked_demo)
        output = hou.node('/stage/EEVEE_OUT')
        result.update(linked_demo.inspect(output))
    elif operation == 'diagnose':
        from pxr import Plug
        import linked_demo
        plugin = Plug.Registry().GetPluginWithName('hdEevee')
        result['plugin'] = {'path': plugin.path, 'loaded_before': plugin.isLoaded,
                            'load_result': plugin.Load(), 'loaded_after': plugin.isLoaded} if plugin else None
        result.update(linked_demo.inspect(hou.node('/stage/EEVEE_OUT')))
        result['display'] = str(hou.node('/stage').displayNode())
        result['paused'] = viewer.isRendererPaused()
        result['update_mode'] = str(hou.updateModeSetting())
        viewer.restartRenderer()
        viewer.curViewport().draw()
    elif operation == 'logs':
        sink = hou.logging.defaultSink()
        path = ROOT/'artifacts/houdini_internal_logs.json'
        if sink:
            hou.logging.saveLogsToFile(tuple(sink.logEntries()), str(path))
        result['path'] = str(path)
    elif operation == 'inspect_session':
        import inspect_session
        result.update(importlib.reload(inspect_session).inspect(request.get('detail_nodes', ())))
    elif operation == 'export_stage':
        from pxr import Usd, Sdf
        path = ROOT/'artifacts/source_stage.usdc'
        hou.node('/stage/EEVEE_OUT').stage().Export(str(path))
        stage = Usd.Stage.Open(str(path))
        textures = ROOT/'runtime/source_textures'
        textures.mkdir(parents=True, exist_ok=True)
        for prim in stage.TraverseAll():
            for attr in prim.GetAttributes():
                value = attr.Get()
                if isinstance(value, Sdf.AssetPath) and value.path.startswith('opdef:') and value.path.lower().endswith(('.jpg', '.png', '.exr')):
                    target = textures/value.path.rsplit('?', 1)[-1]
                    source, section = value.path.removeprefix('opdef:/').split('?', 1)
                    category, typename = source.split('/', 1)
                    definition = hou.nodeType(hou.nodeTypeCategories()[category], typename).definition()
                    target.write_bytes(definition.sections()[section].binaryContents())
                    attr.Set(Sdf.AssetPath(str(target)))
        stage.GetRootLayer().Save()
        result['stage'] = str(path)
    elif operation == 'install_settings':
        import render_settings
        result.update(importlib.reload(render_settings).install())
        import volume_material
        result['volume_material_library'] = importlib.reload(volume_material).install()
    elif operation == 'volume_fixture':
        import houdini_integration
        result.update(importlib.reload(houdini_integration).volume_fixture())
    elif operation == 'integration_cleanup':
        import houdini_integration
        result.update(houdini_integration.cleanup())
    elif operation in ('activate_volume', 'restore_view', 'render_volume'):
        import houdini_integration
        result.update(getattr(importlib.reload(houdini_integration), operation)())
    elif operation == 'volume_registry':
        import ctypes, json
        stage=hou.node('/stage/_eevee_volume_validation/OUT').stage()
        path=next(p.GetAttribute('filePath').Get().path for p in stage.Traverse() if p.GetTypeName()=='HoudiniFieldAsset')
        library=ctypes.CDLL(str(ROOT/'build/libvolume_registry_probe.so'))
        library.inspect_volume_registry.argtypes=[ctypes.c_char_p]
        library.inspect_volume_registry.restype=ctypes.c_char_p
        result['paths']=json.loads(library.inspect_volume_registry(path.encode()))
    elif operation == 'volume_density':
        source=hou.node('/stage/_eevee_volume_validation/smoke/sopnet/create/density')
        source.parm('python').set(source.evalParm('python').replace('3*math.exp','8*math.exp'))
        hou.ui.triggerUpdate()
    elif operation == 'settings':
        node = hou.node('/stage/EEVEE_OUT')
        for name, value in request.get('parameters', {}).items():
            parm = node.parm(name)
            if parm is None: raise ValueError('Unknown render setting: ' + name)
            if parm.parmTemplate().type() == hou.parmTemplateType.Menu:
                if isinstance(value, str): value = list(parm.menuItems()).index(value)
                parm.deleteAllKeyframes()
            parm.set(value)
        hou.ui.triggerUpdate()
        result['errors'] = node.errors()
    elif operation == 'render_disk':
        import render_settings
        render_settings.render(hou.node('/stage/EEVEE_OUT'))
    elif operation == 'render_mplay':
        hou.node('/stage/EEVEE_OUT').parm('render_mplay').pressButton()
    elif operation == 'inspect_mplay':
        rop = hou.node('/stage/EEVEE_OUT/render_to_disk')
        result['parameters'] = [
            {'name':p.name(), 'label':p.parmTemplate().label(), 'value':p.evalAsString(),
             'callback':p.parmTemplate().scriptCallback()}
            for p in rop.parms()
            if any(term in p.name().lower() for term in ('execute','mplay','monitor','image','display','rendercommand','renderpreview'))]
    elif operation == 'mplay_regression':
        node = hou.node('/stage/EEVEE_OUT')
        parameters = {'pass__normal':1, 'pass__z':1, 'trange':1,
                      'f1':hou.frame(), 'f2':hou.frame()+1, 'f3':1}
        saved = {name:(node.parm(name).keyframes(),node.evalParm(name)) for name in parameters}
        frame = hou.frame()
        try:
            for name,value in parameters.items():
                node.parm(name).deleteAllKeyframes()
                node.parm(name).set(value)
            node.parm('render_mplay').pressButton()
        finally:
            for name,(keyframes,value) in saved.items():
                node.parm(name).deleteAllKeyframes()
                if keyframes:node.parm(name).setKeyframes(keyframes)
                else:node.parm(name).set(value)
            if hou.frame()!=frame:hou.setFrame(frame)
            hou.ui.triggerUpdate()
        result['parameters_restored'] = all(node.evalParm(name)==value for name,(_,value) in saved.items())
    elif operation == 'frame_test_scene':
        viewer.curViewport().setCamera(None)
        viewer.curViewport().frameBoundingBox(hou.BoundingBox(-4, 0, -2, 8, 8, 10))
    elif operation == 'instance_probe':
        merge = hou.node('/stage/merge_geometry')
        probe = hou.node('/stage/_eevee_instance_probe')
        if request.get('remove'):
            if probe: probe.destroy()
        elif probe is None:
            source = hou.node(request['source'])
            probe = hou.copyNodesTo((source,), source.parent())[0]
            probe.setName('_eevee_instance_probe')
            probe.parm('primpath').set('/eevee_instance_probe')
            probe.parmTuple('t').set((0,0,3))
            merge.setNextInput(probe)
        hou.ui.triggerUpdate()
    elif operation == 'inspect_render_settings':
        import render_settings
        node = hou.node('/stage/EEVEE_OUT')
        result['configuration'] = render_settings.configuration(node)
        prim = node.stage().GetPrimAtPath(node.evalParm('primpath'))
        result['attributes'] = {a.GetName(): str(a.Get()) for a in prim.GetAttributes()}
        result['color_attributes'] = {a.GetName(): str(a.Get()) for a in node.stage().GetPrimAtPath(node.evalParm('primpath')+'/Color').GetAttributes()}
        result['rop'] = {p.name(): p.evalAsString() for p in node.node('render_to_disk').parms()
                         if p.name() in ('renderer','rendercommand','loppath','rendersettings','trange','f1','f2','f3','soho_foreground','husk_gpu')}
        result['frame_parameters'] = []
        for n in (node,node.node('render_to_disk')):
            for name in ('trange','f1','f2','f3','execute'):
                p=n.parm(name)
                if p:
                    result['frame_parameters'].append({'path':p.path(),'value':p.evalAsString(), 'type':str(p.parmTemplate().type()),
                        'expression':p.expression() if p.keyframes() else None,
                        'callback':p.parmTemplate().scriptCallback()})
    elif operation == 'save':
        hou.hipFile.save(str(ROOT / 'artifacts' / 'eevee_demo.hip'))
    elif operation == 'select_settings':
        node = hou.node('/stage/EEVEE_OUT')
        node.setSelected(True, clear_all_selected=True)
        node.setCurrent(True, clear_all_selected=True)
    elif operation == 'checkpoint':
        path=ROOT/'artifacts'/('checkpoint-'+str(os.getpid())+'.hip')
        hou.hipFile.save(str(path))
        result['hip']=str(path)
    elif operation == 'resume':
        result['paused_before'] = viewer.isRendererPaused()
        result['stage_locked_before'] = viewer.sceneGraphStageLocked()
        if viewer.sceneGraphStageLocked():
            viewer.setSceneGraphStageLocked(False)
        if viewer.isRendererPaused():
            viewer.setRendererPaused(False)
        viewer.curViewport().draw()
        result['paused_after'] = viewer.isRendererPaused()
    elif operation == 'refresh_stage':
        result['current_node'] = str(viewer.currentNode())
        result['display_node'] = str(hou.node('/stage').displayNode())
        viewer.setCurrentNode(hou.node('/stage/EEVEE_OUT'))
        viewer.setSceneGraphStageLocked(True)
        viewer.setSceneGraphStageLocked(False)
        hou.ui.triggerUpdate()
        viewer.curViewport().draw()
    elif operation == 'test_frame':
        hou.setFrame(hou.frame() + 1)
        hou.ui.triggerUpdate()
        viewer.curViewport().draw()
    elif operation == 'set_frame':
        hou.setFrame(float(request['frame']))
        hou.ui.triggerUpdate()
        viewer.curViewport().draw()
    elif operation == 'layout':
        # Rebalance only this test window; do not save a global desktop preset.
        viewer.pane().getSplitParent().setSplitFraction(.52)
        network = hou.ui.paneTabOfType(hou.paneTabType.NetworkEditor)
        network.pane().setSplitFraction(.72)
        for node in hou.node('/stage').children():
            if node.userData('eevee_demo_owned') == '1':
                node.setGenericFlag(hou.nodeFlag.DisplayComment, False)
        network.setVisibleBounds(hou.BoundingRect(-1.5, -14, 18, 5))
        result['fractions'] = [viewer.pane().getSplitFraction(), network.pane().getSplitFraction()]
        def describe(pane):
            return {'pane': str(pane), 'split': pane.isSplit(), 'fraction': pane.getSplitFraction(),
                    'direction': pane.getSplitDirection(),
                    'children': [describe(pane.getSplitChild(i)) for i in (0, 1) if pane.getSplitChild(i)]}
        top = viewer.pane()
        while top.getSplitParent():
            top = top.getSplitParent()
        result['panes'] = describe(top)
    elif operation == 'qt_edit':
        from PySide6 import QtCore
        import linked_demo
        parameters = dict(request['parameters'])
        QtCore.QTimer.singleShot(0, lambda: linked_demo.edit(parameters))
    elif operation == 'edit':
        import linked_demo
        importlib.reload(linked_demo).edit(request['parameters'])
    elif operation == 'capture':
        options = viewer.flipbookSettings().stash()
        options.frameRange((hou.frame(), hou.frame()))
        options.outputToMPlay(False)
        options.useResolution(False)
        options.cropOutMaskOverlay(True)
        options.beautyPassOnly(True)
        options.renderAllViewports(False)
        name = request.get('name', 'houdini_eevee')
        if not name.replace('_','').isalnum():
            raise ValueError('Capture name must contain only letters, digits and underscores')
        path = ROOT/'artifacts'/(name + '.$F4.png')
        options.output(str(path))
        was_paused = viewer.isRendererPaused()
        try:
            viewer.flipbook(viewer.curViewport(), options, open_dialog=False)
        finally:
            if viewer.isRendererPaused() != was_paused:
                viewer.setRendererPaused(was_paused)
        result['image'] = hou.expandString(str(path))
    elif operation == 'window_capture':
        path = ROOT/'artifacts/houdini_eevee_window.png'
        if not hou.qt.mainWindow().grab().save(str(path)):
            raise RuntimeError('Could not capture the Houdini window')
        result['image'] = str(path)
    elif operation == 'inspect_nodes':
        loptypes = hou.lopNodeTypeCategory().nodeTypes()
        result['types'] = {name:[key for key in loptypes if key.split('::')[0]==name] for name in ('sopimport','materiallibrary','assignmaterial','camera','light','merge','null','configurelayer','sopcreate','sceneimport')}
        subnet = hou.node('/stage').node('_eevee_inspect') or hou.node('/stage').createNode('subnet','_eevee_inspect')
        result['parameters'] = {}
        for typename in ('sopimport','materiallibrary','assignmaterial','camera','light','configurelayer'):
            node = subnet.node(typename) or subnet.createNode(typename,typename)
            result['parameters'][typename] = {p.name():{'value':p.evalAsString(),'label':p.parmTemplate().label()} for p in node.parms() if p.name() not in ('')}
        result['voptypes'] = [key for key in hou.vopNodeTypeCategory().nodeTypes() if 'preview' in key.lower() or key in ('principledshader::2.0','subnet','suboutput')]
        from pxr import UsdGeom
        result['stage_up_axis'] = str(UsdGeom.GetStageUpAxis(hou.node('/stage/EEVEE_OUT').stage()))
        lib=subnet.node('materiallibrary')
        shader=lib.node('preview') or lib.createNode('usdpreviewsurface','preview')
        result['preview_parameters']={p.name():p.evalAsString() for p in shader.parms()}
        result['menus']={}
        for typename,names in {'sopimport':['importtype','transform','parentprimtype','bindmaterials'], 'light':['lighttype'], 'camera':['aperture'],'configurelayer':['upaxis']}.items():
            n=subnet.node(typename)
            result['menus'][typename]={name:{'items':n.parm(name).menuItems(),'labels':n.parm(name).menuLabels()} for name in names if n.parm(name)}
        create=subnet.node('sopcreate') or subnet.createNode('sopcreate','sopcreate')
        result['sopcreate']={'children':[(n.path(),n.type().name()) for n in create.allSubChildren()], 'parameters':{p.name():p.evalAsString() for p in create.parms() if p.name() in ('primpath','pathprefix','tx','ty','tz','rx','ry','rz','soppath','importpath')}}
        geo = hou.node('/obj').node('_eevee_inspect') or hou.node('/obj').createNode('geo','_eevee_inspect')
        result['sop_parameters']={}
        for typename in ('sphere','grid','normal'):
            n=geo.node(typename) or geo.createNode(typename,typename)
            result['sop_parameters'][typename]={p.name():{'value':p.evalAsString(),'menu':p.menuItems() if p.parmTemplate().type()==hou.parmTemplateType.Menu else []} for p in n.parms()}
    elif operation == 'quit':
        hou.exit(suppress_save_prompt=True)
    else:
        raise ValueError('Unknown demo operation: ' + operation)
    return result
