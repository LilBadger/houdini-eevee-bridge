"""Export a LOP node's composed stage for headless Hydra tests (run with hython).

    hython probes/export_lop_stage.py SCENE.hip /stage/EEVEE_OUT OUTPUT_DIR [--frame F]

Houdini keeps LOP edits in anonymous layers. Each one is saved into OUTPUT_DIR
and a new root layer lists them in the original order, so the exported stage
composes exactly like the LOP node's stage while external assets stay
referenced in place. The HIP file is loaded read-only and never saved.
"""
import argparse
import json
from pathlib import Path

import hou
from pxr import Sdf, UsdGeom


def save_anonymous(layer, directory, saved):
    """Save `layer` (recursively fixing anonymous sublayers); return its new path."""
    if layer.identifier in saved:
        return saved[layer.identifier]
    target = directory / ('layer_%03d.usdc' % len(saved))
    saved[layer.identifier] = str(target)
    copy = Sdf.Layer.CreateAnonymous()
    copy.TransferContent(layer)
    paths = []
    for sub in layer.subLayerPaths:
        child = Sdf.Layer.Find(sub) or Sdf.Layer.FindOrOpen(sub)
        paths.append(save_anonymous(child, directory, saved) if child is not None and child.anonymous else sub)
    copy.subLayerPaths = paths
    copy.Export(str(target))
    return str(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('hip')
    parser.add_argument('node')
    parser.add_argument('output', type=Path)
    parser.add_argument('--frame', type=float, default=None)
    args = parser.parse_args()
    hou.hipFile.load(args.hip, suppress_save_prompt=True, ignore_load_warnings=True)
    if args.frame is not None:
        hou.setFrame(args.frame)
    node = hou.node(args.node)
    if node is None:
        raise SystemExit('No LOP node ' + args.node)
    stage = node.stage()
    args.output.mkdir(parents=True, exist_ok=True)
    root = stage.GetRootLayer()
    saved = {}
    target = args.output / 'stage.usda'
    copy = Sdf.Layer.CreateAnonymous()
    copy.TransferContent(root)
    paths = []
    for sub in root.subLayerPaths:
        layer = Sdf.Layer.Find(sub) or Sdf.Layer.FindOrOpen(sub)
        paths.append(save_anonymous(layer, args.output, saved) if layer is not None and layer.anonymous else sub)
    copy.subLayerPaths = paths
    copy.Export(str(target))
    cameras = [str(p.GetPath()) for p in stage.Traverse() if p.IsA(UsdGeom.Camera)]
    settings = [str(p.GetPath()) for p in stage.Traverse() if p.GetAttribute('eevee:config')]
    report = {'stage': str(target), 'frame': hou.frame(), 'fps': hou.fps(), 'cameras': cameras,
              'render_settings': settings, 'layers': paths,
              'up_axis': str(UsdGeom.GetStageUpAxis(stage))}
    (args.output / 'export.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
