"""Additive UI startup: does not replace a studio's 123/456 scripts."""
import os

if os.environ.get('HDEEVEE_AUTO_WORKER') == '1':
    import hde_installation
    hde_installation.activate()

if os.environ.get('HDEEVEE_AUTO_SELECT') == '1' and os.environ.get('HDEEVEE_DEMO') != '1':
    import houdini_session
    houdini_session.activate()
