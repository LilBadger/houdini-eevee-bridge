"""Native EEVEE passes, preserving EXR layers and Cryptomatte metadata."""
import json
from pathlib import Path
import bpy
import numpy as np
import OpenEXR
import Imath

PASSES = json.loads((Path(__file__).resolve().parents[1]/'houdini/eevee_passes.json').read_text())


def apply(config, layer=None):
    layer = layer or bpy.context.view_layer
    enabled = set(config.get('passes', []))
    unknown = enabled - {p['id'] for p in PASSES}
    if unknown:
        raise ValueError('Unknown EEVEE passes: ' + ', '.join(sorted(unknown)))
    for p in PASSES:
        owner, name = (layer.eevee, p['property'].split('.')[1]) if '.' in p['property'] else (layer, p['property'])
        # Actual depth is also needed for Hydra final-render readback.
        setattr(owner, name, p['id'] in enabled or p['id'] == 'z')
    layer.pass_cryptomatte_depth = config.get('cryptomatte_depth', 6)
    wanted = config.get('shader_aovs', [])
    if [(a.name, a.type) for a in layer.aovs] != [(a['name'], a['type']) for a in wanted]:
        for aov in list(layer.aovs):
            layer.aovs.remove(aov)
        for item in wanted:
            aov = layer.aovs.add()
            aov.name, aov.type = item['name'], item['type']
    layer.use_pass_combined = True


def read(path, basis):
    file = OpenEXR.File(str(path), separate_channels=True)
    arrays = {}
    inverse = np.asarray(basis.inverted().to_3x3(), dtype=np.float32)
    for part in file.parts:
        prefix, name = part.name().rsplit('.', 1)
        channels = part.channels
        keys = ['R', 'G', 'B', 'A'] if name == 'Combined' else ['X', 'Y', 'Z'] if name in ('Normal','Position') else None
        if keys is not None and all(prefix+'.'+name+'.'+c in channels for c in keys):
            data = np.stack([channels[prefix+'.'+name+'.'+c].pixels for c in keys], axis=-1).astype(np.float32)
            if name in ('Normal','Position'):
                data = data @ inverse.T
                for index, channel in enumerate(keys):
                    channels[prefix+'.'+name+'.'+channel].pixels = np.ascontiguousarray(data[..., index])
        else:
            # Channel order matters for motion vectors and Cryptomatte.
            suffixes = [key.rsplit('.', 1)[-1] for key in channels]
            order = next((seq for seq in ('RGBA','XYZW','rgba','RGB','XYZ','Z','V') if set(seq)==set(suffixes)), sorted(suffixes))
            data = np.stack([channels[prefix+'.'+name+'.'+c].pixels for c in order], axis=-1).astype(np.float32)
        arrays[name] = np.ascontiguousarray(data[::-1])
    return file, arrays


def write(file, path, config):
    enabled = {'Combined'} | {p['layer'] for p in PASSES if p['id'] in config.get('passes', [])}
    enabled.update(a['name'] for a in config.get('shader_aovs', []))
    channels, payload = {}, {}
    # Use the stable single-part multilayer writer. The installed OpenEXR
    # multipart Python writer raises a GIL error while copying NumPy headers.
    first = file.parts[0]
    window = first.header['dataWindow']
    width, height = (window[1]-window[0]+1).tolist()
    header = OpenEXR.Header(width, height)
    header['dataWindow'] = Imath.Box2i(Imath.V2i(*window[0].tolist()), Imath.V2i(*window[1].tolist()))
    header['pixelAspectRatio'] = first.header.get('pixelAspectRatio', 1.)
    codec = config.get('image_settings', {}).get('exr_codec', 'ZIP')
    enum_name = {'NONE':'NO_COMPRESSION','HTJ2K':'HTJ2K256_COMPRESSION'}.get(codec,codec+'_COMPRESSION')
    if any(n.startswith('Crypto') for n in enabled) and codec=='PXR24':
        raise ValueError('PXR24 quantizes Cryptomatte hashes; choose a lossless EXR codec such as ZIP or PIZ.')
    header['compression'] = Imath.Compression(getattr(Imath.Compression, enum_name))
    for part in file.parts:
        for key, value in part.header.items():
            if isinstance(value, str) and key not in ('name', 'type'):
                header[key] = value.encode()
        name = part.name().rsplit('.', 1)[-1]
        if name not in enabled and not any(name.startswith(n) for n in enabled if n.startswith('Crypto')):
            continue
        half = config.get('image_settings', {}).get('color_depth') == '16' and name not in ('Depth','Position','Normal','Vector') and not name.startswith('Crypto')
        pixel_type = Imath.PixelType(Imath.PixelType.HALF if half else Imath.PixelType.FLOAT)
        for key, channel in part.channels.items():
            channels[key] = Imath.Channel(pixel_type)
            payload[key] = np.ascontiguousarray(channel.pixels, dtype=np.float16 if half else np.float32).tobytes()
    header['channels'] = channels
    output = OpenEXR.OutputFile(str(path), header)
    try:
        output.writePixels(payload)
    finally:
        output.close()
