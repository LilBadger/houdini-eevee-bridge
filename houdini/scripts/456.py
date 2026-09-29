"""Existing scene: attach EEVEE to its viewport, never create demo geometry."""
import os
if os.environ.get('HDEEVEE_AUTO_SELECT') == '1' and os.environ.get('HDEEVEE_DEMO') != '1':
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(os.environ['HDEEVEE_PROJECT'])/'tools'))
    import houdini_session
    houdini_session.activate()

if os.environ.get('HDEEVEE_AUTO_WORKER') == '1':
    import hde_installation
    hde_installation.activate()
