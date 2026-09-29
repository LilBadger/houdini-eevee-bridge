"""Generate portable HDAs in a clean headless Houdini process."""
from pathlib import Path
import sys
import hou
sys.path.insert(0, str(Path(__file__).resolve().parent))
import render_settings
import volume_material

hou.node('/stage').createNode('null', 'EEVEE_OUT')
print(render_settings.install())
print(volume_material.install())
