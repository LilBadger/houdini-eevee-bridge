"""Viewport picking and depth from a flat Workbench pass.

Every Blender object that represents Hydra geometry gets a 24-bit pick index
encoded in its object color. A flat, unlit, non-antialiased Workbench draw
returns that color exactly in an RGBA8 target, together with real depth. The
table maps pick indices back to Hydra prim and instance IDs. EEVEE shading does
not read object colors in any material this bridge builds.
"""
import numpy as np

LIMIT = (1 << 24) - 1


def encode(pick):
    return ((pick & 255) / 255.0, ((pick >> 8) & 255) / 255.0, ((pick >> 16) & 255) / 255.0, 1.0)


class PickTable:
    def __init__(self):
        self.prim = np.full(1024, -1, dtype=np.int32)
        self.instance = np.full(1024, -1, dtype=np.int32)
        self.free = []
        self.next = 1

    def _allocate(self):
        if self.free:
            return self.free.pop()
        if self.next > LIMIT:
            return 0
        pick = self.next
        self.next += 1
        if pick >= len(self.prim):
            size = len(self.prim) * 2
            self.prim = np.resize(self.prim, size); self.prim[pick:] = -1
            self.instance = np.resize(self.instance, size); self.instance[pick:] = -1
        return pick

    def assign(self, obj, prim_id, instance_id=-1):
        pick = int(obj.get('hde_pick', 0))
        if not pick:
            pick = self._allocate()
            if not pick:
                return
            obj['hde_pick'] = pick
        self.prim[pick] = prim_id
        self.instance[pick] = instance_id
        color = encode(pick)
        # Object color edits tag the depsgraph; only write actual changes.
        if tuple(round(c * 255) for c in obj.color) != tuple(round(c * 255) for c in color):
            obj.color = color

    def release(self, obj):
        pick = int(obj.get('hde_pick', 0))
        if pick:
            self.prim[pick] = -1
            self.instance[pick] = -1
            self.free.append(pick)
            del obj['hde_pick']

    def reset(self):
        self.__init__()

    def decode(self, rgba):
        """RGBA8 pixels (h, w, 4) -> (prim ids, instance ids) as int32 arrays."""
        pixels = rgba.astype(np.uint32)
        pick = pixels[..., 0] | (pixels[..., 1] << 8) | (pixels[..., 2] << 16)
        valid = (rgba[..., 3] == 255) & (pick > 0) & (pick < self.next)
        pick = np.where(valid, pick, 0)
        prim = self.prim[pick]
        instance = self.instance[pick]
        prim[~valid] = -1
        instance[~valid] = -1
        return np.ascontiguousarray(prim), np.ascontiguousarray(instance)


def configure_id_space(space):
    shading = space.shading
    shading.type = 'SOLID'
    shading.light = 'FLAT'
    shading.color_type = 'OBJECT'
    for name in ('show_object_outline', 'show_cavity', 'show_shadows', 'show_xray',
                 'show_specular_highlight', 'use_dof', 'show_backface_culling'):
        if hasattr(shading, name):
            setattr(shading, name, False)
    space.overlay.show_overlays = False
    space.show_gizmo = False


def configure_preview_space(space):
    """Solid studio preview shown while EEVEE compiles a new scene's shaders."""
    shading = space.shading
    shading.type = 'SOLID'
    shading.light = 'STUDIO'
    shading.color_type = 'MATERIAL'
    shading.background_type = 'VIEWPORT'
    shading.background_color = (0.035, 0.035, 0.035)
    for name in ('show_object_outline', 'show_xray', 'use_dof'):
        if hasattr(shading, name):
            setattr(shading, name, False)
    space.overlay.show_overlays = False
    space.show_gizmo = False
