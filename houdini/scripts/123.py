"""Project-local startup hook used only for the explicitly launched prototype."""
import os

if os.environ.get('HDEEVEE_DEMO') == '1':
    import importlib
    import json
    from pathlib import Path
    import sys
    import time
    import traceback
    import hou

    _eevee_root = Path(os.environ['HDEEVEE_PROJECT'])
    sys.path.insert(0, str(_eevee_root / 'tools'))
    _eevee_start = time.monotonic()
    _eevee_ready = False
    _eevee_activated = False
    _eevee_activation_attempts = 0
    _eevee_last_activation = 0
    _eevee_request = _eevee_root / 'runtime' / ('houdini-' + str(os.getpid()) + '.json')
    _eevee_request.parent.mkdir(exist_ok=True)

    def _eevee_bootstrap():
        global _eevee_ready
        if _eevee_ready or time.monotonic() - _eevee_start < 2:
            return
        if not hou.ui.paneTabOfType(hou.paneTabType.SceneViewer):
            return
        _eevee_ready = True
        try:
            import houdini_demo
            result = importlib.reload(houdini_demo).setup()
        except Exception:
            result = {'ok': False, 'error': traceback.format_exc(), 'pid': os.getpid()}
        (_eevee_root/'artifacts/houdini_status.json').write_text(json.dumps(result, indent=2))
        print('[EEVEE BOOTSTRAP] ' + json.dumps(result), flush=True)
        hou.ui.removeEventLoopCallback(_eevee_bootstrap)

    def _eevee_control():
        if not _eevee_request.exists():
            return
        try:
            request = json.loads(_eevee_request.read_text())
            _eevee_request.unlink()
            import houdini_demo
            result = importlib.reload(houdini_demo).control(request)
        except Exception:
            result = {'ok': False, 'error': traceback.format_exc()}
        (_eevee_root/'artifacts/control_result.json').write_text(json.dumps(result, indent=2))

    def _eevee_activate_view():
        global _eevee_activated, _eevee_activation_attempts, _eevee_last_activation
        if not _eevee_ready:
            return
        trace = Path(os.environ['HDEEVEE_TRACE'])
        if trace.exists() and trace.stat().st_size:
            _eevee_activated = True
            hou.ui.removeEventLoopCallback(_eevee_activate_view)
            return
        # Startup can replace the scene viewer after 123.py first sets its path.
        # Retry activation until the first native-render trace proves it is active.
        if time.monotonic()-_eevee_last_activation < 3:
            return
        _eevee_last_activation = time.monotonic()
        _eevee_activation_attempts += 1
        if _eevee_activation_attempts > 6:
            print('[EEVEE] Viewport activation timed out; run tools/control_demo.py bootstrap', flush=True)
            hou.ui.removeEventLoopCallback(_eevee_activate_view)
            return
        try:
            import houdini_demo
            importlib.reload(houdini_demo).setup()
        except Exception:
            print('[EEVEE] Activation: ' + traceback.format_exc(), flush=True)

    hou.ui.addEventLoopCallback(_eevee_bootstrap)
    hou.ui.addEventLoopCallback(_eevee_control)
    hou.ui.addEventLoopCallback(_eevee_activate_view)
elif os.environ.get('HDEEVEE_AUTO_SELECT') == '1':
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(os.environ['HDEEVEE_PROJECT'])/'tools'))
    import houdini_session
    houdini_session.activate()

if os.environ.get('HDEEVEE_AUTO_WORKER') == '1':
    import hde_installation
    hde_installation.activate()
