"""Apply the installed EEVEE RNA settings and encode final image output."""
from array import array
import json
import os
from pathlib import Path
import tempfile
import re
import bpy
import render_passes
import numpy as np
from viewport_state import assign

SCHEMA = json.loads((Path(__file__).resolve().parents[1]/'houdini/eevee_settings.json').read_text())
ALLOWED = {group: {p['name'] for p in props} for group, props in SCHEMA['groups'].items()}


def apply(scene, config):
    owners = {'eevee': scene.eevee, 'ray_tracing_options': scene.eevee.ray_tracing_options,
              'render': scene.render, 'image_settings': scene.render.image_settings,
              'view_settings': scene.view_settings}
    for group, owner in owners.items():
        values = config.get(group, {})
        if group == 'image_settings' and 'file_format' in values:
            assign(owner, 'file_format', values['file_format'])
        for name, value in values.items():
            if name not in ALLOWED[group]:
                raise ValueError('Unknown EEVEE setting: ' + group + '.' + name)
            prop = owner.bl_rna.properties[name]
            if prop.type == 'BOOLEAN': value = bool(value)
            # Strand curves ignore USD widths (hair-thin lines). EEVEE Render Settings
            # nodes made before Cylinder became the default still store it.
            if group == 'render' and name == 'hair_type' and value == 'STRAND': value = 'CYLINDER'
            if group == 'image_settings' and name == 'color_depth':
                format = owner.file_format
                choices = ('16','32') if format.startswith('OPEN_EXR') else ('8','16') if format in ('PNG','TIFF') else ('8',)
                if value not in choices: value = choices[0]
            try:
                assign(owner, name, value)
            except (TypeError, ValueError) as exc:
                raise ValueError('Invalid EEVEE setting '+group+'.'+name+': '+str(value)) from exc


def output_path(template, frame):
    result = re.sub(r'\$FF|<FF>', lambda m: str(frame), template)
    def replace(match):
        width = int(match.group(1) or 0)
        return str(round(frame)).zfill(width)
    result = re.sub(r'\$F(\d*)', replace, result)
    result = re.sub(r'<F(\d*)>', replace, result)
    result = re.sub(r'%0?(\d*)d', replace, result)
    return result


def publish(encoded, target):
    manifest = os.environ.get('HDEEVEE_OUTPUT_MANIFEST')
    if manifest:
        with open(manifest, 'a') as stream:
            stream.write(json.dumps({'encoded': str(encoded), 'output': str(target)})+'\n')
    else:
        encoded.replace(target)


def final_image(scene, view_layer, config, width, height, frame, basis, requested=()):
    render_passes.apply({**config, 'passes': sorted(set(config.get('passes', [])) | {n.removeprefix('eevee:') for n in requested})}, view_layer)
    bpy.ops.render.render(write_still=False)
    result = bpy.data.images['Render Result']
    target = Path(output_path(config['output'], frame)) if config.get('output') and not os.environ.get('HDEEVEE_MPLAY') else None
    settings = scene.render.image_settings
    previous = settings.media_type, settings.file_format, settings.color_depth, settings.color_mode
    try:
        settings.media_type = 'MULTI_LAYER_IMAGE'
        settings.file_format = 'OPEN_EXR_MULTILAYER'
        settings.color_depth = '32'
        settings.color_mode = 'RGBA'
        with tempfile.TemporaryDirectory(prefix='eevee-readback-') as directory:
            path = Path(directory)/'passes.exr'
            result.save_render(str(path), scene=scene)
            file, passes = render_passes.read(path, basis)
            color = passes['Combined']
            if color.shape != (height, width, 4):
                raise ValueError('EEVEE returned unexpected image dimensions')
            settings.media_type, settings.file_format, settings.color_depth, settings.color_mode = previous
            if target:
                target.parent.mkdir(parents=True, exist_ok=True)
                encoded = target.with_name('.'+target.stem+'.eevee-'+str(os.getpid())+target.suffix)
                if config.get('output_multilayer'):
                    render_passes.write(file, encoded, config)
                else:
                    image = bpy.data.images.new('__eevee_beauty', width, height, alpha=True, float_buffer=True)
                    try:
                        image.pixels.foreach_set(color.ravel())
                        image.save_render(str(encoded), scene=scene)
                    finally:
                        bpy.data.images.remove(image)
                publish(encoded, target)
                if (config.get('passes') or config.get('shader_aovs')) and not config.get('output_multilayer'):
                    sidecar = target.with_name(target.stem+'.passes.exr')
                    encoded_passes = sidecar.with_name('.'+sidecar.stem+'.eevee-'+str(os.getpid())+'.exr')
                    render_passes.write(file, encoded_passes, config)
                    publish(encoded_passes, sidecar)
            return color, passes
    finally:
        settings.media_type, settings.file_format, settings.color_depth, settings.color_mode = previous
