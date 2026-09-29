"""USD light shaping (cones and IES profiles) and light textures, within what EEVEE draws.

A shaping cone on a sphere or disk light becomes a Blender spot light. EEVEE ignores
light shader nodes, so IES profiles and textures on non-dome lights cannot be
reproduced exactly: an IES profile becomes the spot cone that best matches its
falloff, and a texture tints the light with its average color. Both are reported.
"""
import math
import os
import re

import bpy
import numpy as np

_profiles = {}
_averages = {}


def ies_cone(filename):
    """(half angle, blend) in degrees/0..1 fitted to an IES LM-63 profile, or None if broad."""
    stat = os.stat(filename)
    key = (filename, stat.st_mtime_ns, stat.st_size)
    if key in _profiles:
        return _profiles[key]
    with open(filename, 'r', errors='replace') as stream:
        text = stream.read()
    tilt = re.search(r'TILT\s*=\s*(\S+)', text)
    if not tilt:
        raise ValueError('Not an IES profile: ' + filename)
    numbers = [float(x) for x in re.findall(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?', text[tilt.end():])]
    if tilt.group(1).upper() == 'INCLUDE':
        # Lamp-to-luminaire geometry, then angle/multiplier pairs.
        pairs = int(numbers[1])
        numbers = numbers[2 + 2 * pairs:]
    vertical, horizontal = int(numbers[3]), int(numbers[4])
    data = numbers[13:]
    angles = np.asarray(data[:vertical])
    candela = np.asarray(data[vertical + horizontal:vertical + horizontal + vertical * horizontal]).reshape(horizontal, vertical)
    profile = candela.mean(axis=0)
    peak = profile.max()
    result = None
    if peak > 0:
        # Type C photometry: 0 degrees points along the light's emission axis.
        below_half = angles[(profile < .5 * peak) & (angles >= angles[profile.argmax()])]
        below_tenth = angles[(profile < .1 * peak) & (angles >= angles[profile.argmax()])]
        if len(below_tenth) and below_tenth[0] < 90:
            outer = float(below_tenth[0])
            inner = float(below_half[0]) if len(below_half) else outer
            result = (max(outer, .5), min(1., max(0., (outer - inner) / outer)))
    _profiles[key] = result
    return result


def spot(session, key, kind, params):
    """(spot size in radians, blend) for a light that should be a Blender spot, else None."""
    if kind not in ('sphereLight', 'diskLight'):
        if params.get('shaping:cone:angle', 90) < 90 or params.get('shaping:ies:file'):
            session.warn(key, 'EEVEE cannot shape ' + kind + ' with a cone or IES profile; it is drawn unshaped')
        return None
    angle = params.get('shaping:cone:angle')
    softness = params.get('shaping:cone:softness', 0.)
    ies = params.get('shaping:ies:file')
    if ies:
        try:
            fitted = ies_cone(ies)
        except (OSError, ValueError, IndexError) as exc:
            session.warn(key, 'Cannot read IES profile: ' + str(exc))
            fitted = None
        if fitted is None:
            session.warn(key, 'IES profile ' + os.path.basename(ies) + ' is too broad for a cone; EEVEE draws it unshaped')
        else:
            session.warn(key, 'EEVEE cannot draw IES profiles; ' + os.path.basename(ies) + ' is approximated by a spot cone')
            angle = min(angle, fitted[0]) if angle is not None else fitted[0]
            softness = max(softness, fitted[1])
    if angle is None or angle >= 90:
        return None
    # USD fades the cone from angle * (1 - softness) to angle. Blender's spot blend
    # works on cosines: its falloff starts at about angle * sqrt(1 - blend). (Karma
    # also blurs the edge by the light's size; EEVEE evaluates the cone at the
    # light's centre, so large soft lights have harder cone edges than in Karma.)
    softness = min(1., max(0., float(softness)))
    return math.radians(min(180., max(1., 2. * float(angle)))), 1. - (1. - softness) ** 2


def average_color(filename):
    """Mean linear RGB of an image, cached by file state."""
    stat = os.stat(filename)
    key = (filename, stat.st_mtime_ns, stat.st_size)
    if key not in _averages:
        image = bpy.data.images.load(filename, check_existing=False)
        try:
            pixels = np.empty(len(image.pixels), dtype=np.float32)
            image.pixels.foreach_get(pixels)
            rgb = pixels.reshape(-1, image.channels)[:, :3] if image.channels >= 3 else np.repeat(pixels.reshape(-1, 1), 3, 1)
            if not image.is_float and image.colorspace_settings.name == 'sRGB':
                rgb = np.where(rgb <= .04045, rgb / 12.92, ((rgb + .055) / 1.055) ** 2.4)
            _averages[key] = [float(v) for v in rgb.mean(axis=0)]
        finally:
            bpy.data.images.remove(image)
    return _averages[key]


def tint(session, key, kind, params, color):
    """Color for a light with texture:file (non-dome), tinted by the texture's average."""
    filename = params.get('texture:file')
    if not filename or kind == 'domeLight':
        return color
    try:
        average = average_color(filename)
    except (OSError, RuntimeError) as exc:
        session.warn(key, 'Cannot read light texture: ' + str(exc))
        return color
    session.warn(key, 'EEVEE cannot draw textured lights; ' + os.path.basename(filename) + ' tints the light with its average color')
    return [c * a for c, a in zip(color, average)]
