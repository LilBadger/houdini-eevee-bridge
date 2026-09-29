"""Select the renderer for the user's stage without creating scene content."""
import json
import os
from pathlib import Path
import time

import hou
from hde_runtime import cache_root


def activate():
    if not hou.isUIAvailable() or getattr(hou.session, '_eevee_select_scheduled', False):
        return
    hou.session._eevee_select_scheduled = True
    root = Path(os.environ['HDEEVEE_PROJECT'])
    started = time.monotonic()
    state = {'entered':False}

    def select_renderer():
        if time.monotonic()-started > 30:
            hou.session._eevee_select_scheduled = False
            hou.ui.removeEventLoopCallback(select_renderer)
            return
        viewer = hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)
        if not viewer:
            return
        try:
            if not state['entered']:
                source_path = os.environ.get('HDEEVEE_SOURCE_LOP')
                if source_path:
                    source = hou.node(source_path)
                    if not isinstance(source, hou.LopNode):
                        raise ValueError('Not an existing LOP node: ' + source_path)
                    source.setDisplayFlag(True)
                    viewer.setPwd(source.parent())
                    viewer.setCurrentNode(source)
                elif not viewer.isViewingSceneGraph():
                    viewer.setPwd(hou.node('/stage'))
                state['entered'] = True
                return  # Let Houdini construct its Solaris viewport first.
            if not viewer.isViewingSceneGraph():
                return
            renderer = next((name for name in viewer.hydraRenderers() if 'eevee' in name.lower()), None)
            if not renderer:
                raise RuntimeError('EEVEE Bridge was not discovered')
            viewer.setHydraRenderer(renderer)
            if viewer.isRendererPaused():
                viewer.setRendererPaused(False)
            viewer.curViewport().draw()
            result = {'ok':True, 'renderer':renderer, 'pid':os.getpid(),
                      'scene':hou.hipFile.path(), 'stage_network':viewer.pwd().path(), 'demo':False}
        except Exception as exc:
            result = {'ok':False, 'error':str(exc), 'pid':os.getpid(), 'demo':False}
        log = root/'artifacts' if os.environ.get('HDEEVEE_DEV_CONTROL') == '1' else cache_root()/'logs'
        log.mkdir(parents=True,exist_ok=True)
        (log/'houdini_status.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        hou.ui.removeEventLoopCallback(select_renderer)
        hou.session._eevee_select_scheduled = False

    hou.ui.addEventLoopCallback(select_renderer)
    if os.environ.get('HDEEVEE_DEV_CONTROL') == '1' and not getattr(hou.session, '_eevee_control_installed', False):
        import importlib
        import traceback
        request_path = root/'runtime'/('houdini-' + str(os.getpid()) + '.json')
        def control():
            if not request_path.exists():
                return
            try:
                request = json.loads(request_path.read_text())
                request_path.unlink()
                import houdini_demo
                result = importlib.reload(houdini_demo).control(request)
            except Exception:
                result = {'ok': False, 'error': traceback.format_exc()}
            (root/'artifacts/control_result.json').write_text(json.dumps(result, indent=2))
        hou.ui.addEventLoopCallback(control)
        hou.session._eevee_control_installed = True
