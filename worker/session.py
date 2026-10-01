"""One Hydra render delegate's EEVEE scene inside the shared Blender worker.

Each connection owns a Session with its own Blender scene, camera, world,
pick table, offscreen targets and shared-memory pixel segment. Several Houdini
viewports can therefore render at the same time without replacing each
other's scenes. GPU shaders, textures and retired materials are shared.
"""
import json
import math
import os
import time
import zlib

import bpy
import gpu
import numpy as np
from mathutils import Matrix

import curves
import environment
import instances as instance_nodes
import light_links
import light_shaping
import material_pool
import materialx_material
import meshes
import motion
import points
import render_config
import render_passes
import subdivision
import usd_material
import volumes
from picking import PickTable
from protocol import PixelSegment
from shader_utils import primvar
from viewport_state import assign

# Right-handed basis: Houdini +X -> Blender +X, +Y -> +Z, +Z -> -Y.
# Convert world transforms and the view together, not the projection.
Y_UP = Matrix(((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))
MAX_PIXELS = 8192 * 8192 // 2
IDLE_TARGET_SECONDS = float(os.environ.get('HDEEVEE_IDLE_TARGET_SECONDS', 60))


class Session:
    def __init__(self, worker, number, scene):
        self.worker = worker
        self.number = number
        self.scene = scene
        self.view_layer = scene.view_layers[0]
        scene.render.engine = 'BLENDER_EEVEE'
        scene.eevee.taa_samples = 16
        scene.eevee.use_raytracing = True
        # USD curves are round tubes of their widths, as in Karma; Blender's
        # default strands are thin lines that ignore the width.
        scene.render.hair_type = 'CYLINDER'
        # Only the Workbench ID/preview passes read these.
        scene.display.viewport_aa = 'OFF'
        scene.display.render_aa = 'OFF'
        if scene.world is None or not scene.world.get('hde_world'):
            scene.world = bpy.data.worlds.new('HoudiniWorld')
            scene.world['hde_world'] = True
        scene.world.use_nodes = True
        self.camera = next((o for o in scene.objects if o.get('hde_camera')), None)
        if self.camera is None:
            self.camera = bpy.data.objects.new('__eevee_camera', bpy.data.cameras.new('__eevee_camera'))
            self.camera['hde_camera'] = True
            scene.collection.objects.link(self.camera)
        scene.camera = self.camera
        self.objects = {}
        self.materials = {}
        self.material_digest = {}
        self.materialx_defs = {}
        self.bindings = {}
        self.bound = {}
        self.subset_bindings = {}
        self.subsets = {}
        self.display = {}
        self.display_colors = {}
        self.categories = {}
        self.light_links = {}
        self.links_dirty = False
        self.topology = {}
        self.instances = {}
        self.point_instances = {}
        self.instancing_motion = False
        self.instance_state = {}
        self.instance_primvars = {}
        self.visibility = {}
        self.prim_ids = {}
        self.domes = {}
        self.environment_signature = None
        self.material_warnings = {}
        self.geometry_warnings = {}
        self.exported_attributes = {}
        self.fields = {}
        self.volume_defs = {}
        self.motion_defs = {}
        self.up_axis = 'Y'
        self.basis = Y_UP.copy()
        self.picks = PickTable()
        self.segment = PixelSegment('hde-' + str(os.getpid()) + '-' + str(number))
        self.offscreens = {}
        self.frames = 0
        self.scene_epoch = 0
        self.camera_state = None
        self.config_signature = None
        self.config = {}
        self.dirty_geometry = True
        self.dirty_environment = True
        self.eevee_drawn = False
        self.last_draw_ms = None
        self.timings = {}
        self.scratch_arrays = {}
        self.offscreen_used = {}
        self.limit_surface = None

    # ------------------------------------------------------------------ state
    def close(self):
        self.reset()
        for _, offscreen in self.offscreens.values():
            offscreen.free()
        self.offscreens.clear()
        self.scratch_arrays.clear()
        self.segment.close()

    def reset(self):
        self.scene_epoch += 1
        self.camera_state = None
        self.eevee_drawn = False
        for key in list(self.point_instances):
            instance_nodes.remove(self, key)
        for objects in self.instances.values():
            for obj in objects:
                bpy.data.objects.remove(obj, do_unlink=True)
        self.instances.clear()
        self.instance_state.clear()
        self.instance_primvars.clear()
        for obj in list(self.objects.values()):
            data = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if data is not None and data.users == 0:
                self.remove_data(data)
        retired = []
        for key, mat in list(self.materials.items()):
            if not any(m == mat for m in retired):
                retired.append(mat)
                self.worker.materials.retire(self.material_digest.get(key), mat)
        for image in list(bpy.data.images):
            if image.get('hde_ramp') and not image.users:
                bpy.data.images.remove(image)
        for name in ('objects', 'materials', 'material_digest', 'materialx_defs', 'bindings', 'bound',
                     'subset_bindings', 'subsets', 'display', 'display_colors', 'categories', 'light_links',
                     'topology', 'visibility', 'prim_ids', 'fields', 'volume_defs', 'motion_defs', 'domes',
                     'material_warnings', 'geometry_warnings', 'exported_attributes'):
            getattr(self, name).clear()
        self.picks.reset()
        light_links.clear(self)
        self.links_dirty = False
        self.environment_signature = None
        self.dirty_geometry = True
        self.dirty_environment = True

    @staticmethod
    def remove_data(data):
        if isinstance(data, bpy.types.Mesh):
            bpy.data.meshes.remove(data)
        elif isinstance(data, bpy.types.Curves):
            bpy.data.hair_curves.remove(data)
        elif isinstance(data, bpy.types.Volume):
            bpy.data.volumes.remove(data)
        elif isinstance(data, bpy.types.PointCloud):
            bpy.data.pointclouds.remove(data)
        elif isinstance(data, bpy.types.Light):
            bpy.data.lights.remove(data)

    def warn(self, key, message):
        messages = self.geometry_warnings.setdefault(key, [])
        if message not in messages:
            messages.append(message)
            print('[EEVEE] ' + key + ': ' + message, flush=True)

    def set_up_axis(self, up_axis):
        if up_axis not in ('Y', 'Z'):
            raise ValueError('Scene up axis must be Y or Z')
        if up_axis == self.up_axis:
            return
        basis = Matrix.Identity(4) if up_axis == 'Z' else Y_UP.copy()
        conversion = basis @ self.basis.inverted()
        for obj in [*self.objects.values(), *(o for group in self.instances.values() for o in group)]:
            obj.matrix_world = conversion @ obj.matrix_world
        for obj in self.point_instances.values():
            obj.matrix_world = basis
        self.up_axis, self.basis = up_axis, basis
        for key, definition in list(self.materialx_defs.items()):
            self.material({'id': key, 'materialx_network': definition})
        self.dirty_environment = True

    def set_instancing_motion(self, enabled):
        if enabled == self.instancing_motion:
            return
        self.instancing_motion = enabled
        # A settings-only edit need not dirty Hydra's instance transforms.
        # Rebuild from the current transforms when changing representation.
        for key, transforms in list(self.instance_state.items()):
            if key in self.objects:
                instance_nodes.sync_objects(self, key, self.objects[key], transforms)
                instance_nodes.refresh(self, key, self.objects[key], self.visibility.get(key, True))

    def configure(self, config, request):
        """Apply Stage render settings only when they actually change."""
        up_axis = config.get('up_axis', request.get('up_axis', 'Y'))
        signature = json.dumps([config, up_axis], sort_keys=True, separators=(',', ':'), default=str)
        if signature == self.config_signature:
            return
        self.config_signature = signature
        self.config = config
        self.set_instancing_motion(bool(config.get('render', {}).get('use_motion_blur', False)))
        self.set_up_axis(up_axis)
        eevee = {k: v for k, v in config.get('eevee', {}).items() if k != 'taa_samples'}
        render_config.apply(self.scene, {**config, 'eevee': eevee})
        fps = config.get('fps', 24) or 24
        assign(self.scene.render, 'fps', max(1, round(fps)))
        assign(self.scene.render, 'fps_base', self.scene.render.fps / fps)
        for name, value in config.get('mist', {}).items():
            assign(self.scene.world.mist_settings, name, value)
        self.dirty_geometry = True
        self.dirty_environment = True

    # --------------------------------------------------------------- materials
    def material(self, update):
        key = update['id']
        digest = material_pool.digest(update, self.up_axis)
        mat = self.materials.get(key)
        if mat is not None and self.material_digest.get(key) == digest:
            return
        if 'materialx_network' in update:
            self.materialx_defs[key] = update['materialx_network']
        else:
            self.materialx_defs.pop(key, None)
        if mat is not None and self.shares(key, mat):
            # Identical materials share one Blender material; an edit gets its own.
            del self.materials[key]
            self.material_digest.pop(key, None)
            mat = None
        if mat is None:
            # Copies of an asset carry identical materials: build (and let EEVEE
            # compile) each definition once, then reuse it in this scene or later.
            same = next((k for k, d in self.material_digest.items()
                         if d == digest and k != key and k in self.materials), None)
            mat = self.materials[same] if same is not None else self.worker.materials.take(digest)
            if mat is not None:
                self.materials[key] = mat
                self.material_digest[key] = digest
                warnings = json.loads(mat.get('hde_warnings', '[]'))
                if warnings:
                    self.material_warnings[key] = warnings
                self.rebind(key)
                return
            mat = bpy.data.materials.new(key)
            self.materials[key] = mat
        self.material_warnings.pop(key, None)
        tree = mat.node_tree
        warnings = []
        if 'graph' in update:
            graph = update['graph']
            if isinstance(graph, str):
                graph = json.loads(graph)
            tree.nodes.clear()
            nodes = {}
            for definition in graph['nodes']:
                if definition['type'] == 'hde:primvar':
                    nodes[definition['id']] = primvar(tree, definition['name'])
                    continue
                node = tree.nodes.new(definition['type'])
                nodes[definition['id']] = node
                for prop, value in definition.get('properties', {}).items():
                    setattr(node, prop, value)
                for name, value in definition.get('inputs', {}).items():
                    node.inputs[int(name) if name.isdecimal() else name].default_value = value
            for a, a_socket, b, b_socket in graph.get('links', []):
                tree.links.new(nodes[a].outputs[a_socket], nodes[b].inputs[b_socket])
        elif 'materialx_network' in update:
            try:
                warnings = materialx_material.network(tree, update['materialx_network'], self.basis)
            except (materialx_material.TranslationError, ValueError) as exc:
                warnings = [str(exc)]
                tree.nodes.clear()
                usd_material.principled(tree, {'diffuseColor': [1., 0., 1.], 'roughness': .5})
            if 'materialx_displacement' in update:
                try:
                    warnings += materialx_material.displacement(tree, update['materialx_displacement'], self.basis)
                except (materialx_material.TranslationError, ValueError) as exc:
                    warnings.append('Displacement: ' + str(exc))
        elif 'usd_network' in update:
            usd_material.network(tree, update['usd_network'])
        else:
            tree.nodes.clear()
            usd_material.principled(tree, update.get('parameters', {}))
        if warnings:
            self.material_warnings[key] = warnings
            for warning in warnings:
                print('[EEVEE] Material ' + key + ': ' + warning, flush=True)
        # Transmission needs EEVEE's material-level ray-tracing switch as well
        # as the scene switch. Set it afresh on edits, including shader graphs.
        mat.use_raytrace_refraction = any(
            n.bl_idname in ('ShaderNodeBsdfGlass', 'ShaderNodeBsdfRefraction') or
            (n.bl_idname == 'ShaderNodeBsdfPrincipled' and
             (n.inputs['Transmission Weight'].is_linked or n.inputs['Transmission Weight'].default_value > 0.))
            for n in tree.nodes)
        # True displacement moves vertices (plus bump for detail finer than the mesh);
        # EEVEE does not dice, so its detail depends on the mesh or subdivision level.
        displaced = any(n.bl_idname == 'ShaderNodeOutputMaterial' and n.inputs['Displacement'].is_linked for n in tree.nodes)
        mat.displacement_method = 'BOTH' if displaced else 'BUMP'
        # The Workbench preview shown while shaders compile uses this color.
        base = next((n for n in tree.nodes if n.bl_idname == 'ShaderNodeBsdfPrincipled'), None)
        if base is not None and not base.inputs['Base Color'].is_linked:
            mat.diffuse_color = tuple(base.inputs['Base Color'].default_value)
        self.material_digest[key] = digest
        mat['hde_warnings'] = json.dumps(warnings)
        self.dirty_geometry = True
        self.rebind(key)

    def shares(self, key, mat):
        return any(m == mat for k, m in self.materials.items() if k != key)

    def delete_material(self, key):
        self.materialx_defs.pop(key, None)
        self.material_warnings.pop(key, None)
        mat = self.materials.pop(key, None)
        if mat is None:
            return
        # Detach it from this scene before another viewport may reuse it.
        for obj_key in self.bound.get(key, ()):
            obj = self.objects.get(obj_key)
            if obj is not None and obj.data is not None and hasattr(obj.data, 'materials'):
                obj.data.materials.clear()
        digest = self.material_digest.pop(key, None)
        if not self.shares(key, mat):
            self.worker.materials.retire(digest, mat)

    def bind(self, key, material_id):
        previous = self.bindings.get(key)
        if previous is not None and previous != material_id and previous not in self.subset_bindings.get(key, ()):
            self.bound.get(previous, set()).discard(key)
        self.bindings[key] = material_id
        self.bound.setdefault(material_id, set()).add(key)
        self.assign_material(key)

    def bind_subsets(self, key, material_ids):
        """Per-face materials: slot 0 is the prim's own material, slot i+1 subset i."""
        for previous in self.subset_bindings.get(key, ()):
            if previous not in material_ids and previous != self.bindings.get(key):
                self.bound.get(previous, set()).discard(key)
        self.subset_bindings[key] = list(material_ids)
        for material_id in material_ids:
            self.bound.setdefault(material_id, set()).add(key)
        self.assign_material(key)

    def assign_material(self, key):
        obj = self.objects.get(key)
        if obj is None or obj.data is None or not hasattr(obj.data, 'materials'):
            return
        base = self.materials.get(self.bindings.get(key)) or self.materials.get(self.display.get(key))
        subsets = self.subset_bindings.get(key, ())
        if base is None and not subsets:
            return
        wanted = [base] + [self.materials.get(m) or base for m in subsets]
        slots = obj.data.materials
        if list(slots) == wanted:
            return
        slots.clear()
        for mat in wanted:
            slots.append(mat)

    def rebind(self, material_key):
        for obj_key in list(self.bound.get(material_key, ())):
            self.assign_material(obj_key)

    def display_material(self, key, color, varying=False):
        """Material for prims without a bound material, from USD displayColor."""
        if varying:
            # Shared by every prim whose displayColor varies per point, face or corner.
            material_key = '__hde_display_attribute'
            if material_key not in self.materials:
                self.material({'id': material_key, 'graph': {
                    'nodes': [{'id': 'color', 'type': 'hde:primvar', 'name': 'displayColor'},
                              {'id': 'bsdf', 'type': 'ShaderNodeBsdfPrincipled', 'inputs': {'Roughness': 0.4}},
                              {'id': 'output', 'type': 'ShaderNodeOutputMaterial'}],
                    'links': [['color', 'Color', 'bsdf', 'Base Color'], ['bsdf', 'BSDF', 'output', 'Surface']]}})
        else:
            material_key = key + '/display'
            self.material({'id': material_key, 'parameters': {'diffuseColor': list(color), 'roughness': 0.4}})
        self.display[key] = material_key
        self.assign_material(key)

    def display_color(self, key, update):
        """Apply displayColor to a prim without a bound material. update is the
        prim's latest change; its displayColor is kept for later rebinding."""
        if 'color' in update:
            self.display_colors[key] = (update['color'], update.get('color_varying', False))
        if self.bindings.get(key):
            return
        color, varying = self.display_colors.get(key, (None, False))
        # Per-instance displayColor needs the attribute-reading material.
        varying = varying or 'displayColor' in self.instance_primvars.get(key, {})
        if color is not None or varying:
            self.display_material(key, color, varying)

    # ------------------------------------------------------------------ lights
    def light(self, update):
        key, kind = update['id'], update['type']
        params = update.get('parameters', {})
        if kind == 'domeLight':
            self.domes[key] = update
            self.dirty_environment = True
            return
        color = light_shaping.tint(self, key, kind, params, params.get('color', [1, 1, 1]))
        intensity = params.get('intensity', 1) * 2 ** params.get('exposure', 0)
        light_type = {'rectLight': 'AREA', 'diskLight': 'AREA', 'distantLight': 'SUN',
                      'sphereLight': 'POINT', 'cylinderLight': 'AREA'}.get(kind, 'POINT')
        spot = light_shaping.spot(self, key, kind, params)
        if spot is not None:
            light_type = 'SPOT'
        obj = self.objects.get(key)
        if obj is None:
            obj = bpy.data.objects.new(key, bpy.data.lights.new(key, light_type))
            self.scene.collection.objects.link(obj)
            self.objects[key] = obj
            obj.matrix_world = self.basis
        assign(obj.data, 'type', light_type)
        light = obj.data   # the RNA type follows the light type
        link = (params.get('lightLink', ''), params.get('shadowLink', ''))
        if self.light_links.get(key, ('', '')) != link:
            self.light_links[key] = link
            self.links_dirty = True
        assign(light, 'color', list(color))
        normalize = bool(params.get('normalize', False))
        if light.type == 'SUN':
            # Blender's normalized sun strength is irradiance. As in Karma, a normalized
            # distant light's intensity is irradiance too; otherwise it is the radiance
            # of the sun's disc, so irradiance scales with the disc's solid angle.
            half_angle = math.radians(params.get('angle', 0.53)) / 2
            assign(light, 'normalize', True)
            assign(light, 'energy', intensity if normalize else intensity * 2 * math.pi * (1 - math.cos(half_angle)))
        else:
            # Match Blender's USD radiance -> radiant-flux conversion. Normalize is
            # essential: USD's default is radiance independent of emitter area.
            assign(light, 'normalize', normalize)
            assign(light, 'energy', intensity * math.pi)
        assign(light, 'use_temperature', bool(params.get('enableColorTemperature', False)))
        assign(light, 'temperature', params.get('colorTemperature', 6500.))
        assign(light, 'diffuse_factor', params.get('diffuse', 1.))
        assign(light, 'specular_factor', params.get('specular', 1.))
        if light.type == 'AREA':
            assign(light, 'shape', 'RECTANGLE' if kind in ('rectLight', 'cylinderLight') else 'DISK')
            if kind == 'cylinderLight':
                assign(light, 'size', params.get('length', 1.))
                assign(light, 'size_y', params.get('radius', .5) * 2)
            else:
                assign(light, 'size', params.get('width', params.get('radius', 0.5) * 2))
                if light.shape == 'RECTANGLE':
                    assign(light, 'size_y', params.get('height', 1))
        if light.type in ('POINT', 'SPOT'):
            assign(light, 'shadow_soft_size', 0. if params.get('treatAsPoint', False) else params.get('radius', 0.5))
        if light.type == 'SPOT':
            assign(light, 'spot_size', spot[0])
            assign(light, 'spot_blend', spot[1])
        if light.type == 'SUN':
            assign(light, 'angle', params.get('angle', 0.53) * 0.0174532925199433)
        if 'transform' in update:
            obj.matrix_world = self.basis @ Matrix(update['transform']).transposed()
        hidden = not update.get('visible', True)
        if obj.hide_render != hidden:
            obj.hide_render = hidden
            obj.hide_set(hidden, view_layer=self.view_layer)

    # ------------------------------------------------------------------ update
    def update(self, changes):
        """Apply scene edits. One bad prim is reported, not fatal to the batch."""
        errors = {}
        if not changes:
            return errors
        self.scene_epoch += 1
        volumes_changed = False
        materials_changed = False
        for change in changes:
            try:
                kind = self.apply_change(change)
            except Exception as exc:
                key = str(change.get('id', '?'))
                errors[key] = str(exc) or type(exc).__name__
                self.geometry_warnings[key] = [errors[key]]
                print('[EEVEE] Scene edit failed for ' + key + ': ' + errors[key], flush=True)
                continue
            volumes_changed |= kind in ('field', 'delete_field', 'volume')
            materials_changed |= kind in ('material', 'delete_material')
        if volumes_changed:
            for definition in self.volume_defs.values():
                try:
                    volumes.sync(self, definition)
                except Exception as exc:
                    errors[definition['id']] = str(exc)
                    print('[EEVEE] Volume failed for ' + definition['id'] + ': ' + str(exc), flush=True)
            self.dirty_geometry = True
        if materials_changed:
            for image in list(bpy.data.images):
                if image.get('hde_ramp') and not image.users:
                    bpy.data.images.remove(image)
        if self.links_dirty:
            light_links.sync(self)
            self.links_dirty = False
        return errors

    RAYS = (('camera', 'visible_camera'), ('shadow', 'visible_shadow'), ('diffuse', 'visible_diffuse'),
            ('glossy', 'visible_glossy'), ('transmission', 'visible_transmission'), ('volume', 'visible_volume_scatter'))

    def object_properties(self, change):
        """Karma holdout and render visibility (see KarmaObjectProperties in the delegate)."""
        obj = self.objects.get(change['id'])
        if obj is None:
            return
        if 'holdout' in change and obj.is_holdout != bool(change['holdout']):
            obj.is_holdout = bool(change['holdout'])
        rays = change.get('ray_visibility')
        if isinstance(rays, dict):
            for name, attribute in self.RAYS:
                value = bool(rays.get(name, True))
                if getattr(obj, attribute) != value:
                    setattr(obj, attribute, value)

    def apply_change(self, change):
        kind = change['kind']
        if kind in ('mesh', 'curves', 'points', 'light', 'volume'):
            names = [n for n in ('transform_samples', 'point_samples', 'instance_samples',
                                 'velocities', 'accelerations') if n in change]
            if names:
                animation = self.motion_defs.setdefault(change['id'], {})
                for name in names:
                    animation[name] = change[name]
            if 'categories' in change and self.categories.get(change['id']) != change['categories']:
                self.categories[change['id']] = change['categories']
                self.links_dirty = True
            elif kind != 'light' and change.get('instances') is not None and self.light_links:
                self.links_dirty = True   # instance copies must join their prototype's link collections
        if kind == 'material':
            self.material(change)
        elif kind == 'mesh':
            meshes.sync(self, change)
            self.object_properties(change)
            self.dirty_geometry = True
        elif kind == 'curves':
            curves.sync(self, change)
            self.object_properties(change)
            self.dirty_geometry = True
        elif kind == 'points':
            points.sync(self, change)
            self.object_properties(change)
            self.dirty_geometry = True
        elif kind == 'light':
            self.light(change)
        elif kind == 'field':
            self.fields[change['id']] = change
        elif kind == 'delete_field':
            self.fields.pop(change['id'], None)
        elif kind == 'volume':
            self.volume_defs[change['id']] = change
        elif kind == 'delete':
            self.delete(change['id'])
        elif kind == 'delete_material':
            self.delete_material(change['id'])
        else:
            raise ValueError('Unknown scene change: ' + str(kind))
        return kind

    def delete(self, key):
        instance_nodes.remove(self, key)
        self.exported_attributes.pop(key, None)
        self.instance_state.pop(key, None)
        if self.domes.pop(key, None) is not None:
            self.dirty_environment = True
        for instance in self.instances.pop(key, []):
            self.picks.release(instance)
            bpy.data.objects.remove(instance, do_unlink=True)
        obj = self.objects.pop(key, None)
        if obj is not None:
            self.picks.release(obj)
            data = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if data is not None and data.users == 0:
                self.remove_data(data)
        previous = self.bindings.pop(key, None)
        if previous is not None:
            self.bound.get(previous, set()).discard(key)
        for material_id in self.subset_bindings.pop(key, ()):
            self.bound.get(material_id, set()).discard(key)
        for name in ('topology', 'visibility', 'prim_ids', 'volume_defs', 'motion_defs', 'geometry_warnings',
                     'subsets', 'display', 'display_colors', 'instance_primvars'):
            getattr(self, name).pop(key, None)
        if self.categories.pop(key, None) is not None or self.light_links.pop(key, None) is not None:
            self.links_dirty = True
        self.dirty_geometry = True

    # ------------------------------------------------------------------ render
    def offscreen(self, purpose, width, height, fmt):
        self.offscreen_used[purpose] = time.monotonic()
        wanted = (width, height, fmt)
        entry = self.offscreens.get(purpose)
        if entry is not None and entry[0] == wanted:
            return entry[1]
        if entry is not None:
            entry[1].free()
        offscreen = gpu.types.GPUOffScreen(width, height, format=fmt)
        self.offscreens[purpose] = (wanted, offscreen)
        return offscreen

    def update_camera(self, request, config, view, projection, width, height, frame):
        final = bool(request.get('final_render'))
        # Navigation frames are drawn smaller; keep the camera description
        # stable so switching resolution does not dirty the Blender scene.
        out_w = int(request.get('output_width', width))
        out_h = int(request.get('output_height', height))
        camera_state = json.dumps([request['view'], request['projection'], self.up_axis, out_w, out_h, frame,
                                   request.get('camera', {}), request.get('camera_samples', []),
                                   config.get('disable_dof', False), final], sort_keys=True, default=str)
        # A camera matrix is decomposed internally into location/quaternion/scale.
        # Reassigning the same source matrix can still differ by a few float bits
        # on readback and invalidate the whole EEVEE accumulation every draw.
        if camera_state == self.camera_state:
            return
        assign(self.camera, 'matrix_world', view.inverted())
        assign(self.scene.render, 'resolution_x', out_w)
        assign(self.scene.render, 'resolution_y', out_h)
        assign(self.scene.render, 'resolution_percentage', 100)
        pixel_ratio = abs(projection[1][1] / projection[0][0]) * out_h / out_w
        assign(self.scene.render, 'pixel_aspect_x', max(1., pixel_ratio))
        assign(self.scene.render, 'pixel_aspect_y', max(1., 1. / pixel_ratio))
        camera = self.camera.data
        params = request.get('camera', {})
        perspective = abs(projection[3][3]) < .5
        focal = float(params.get('focal_length', 0) or 0)
        dof = (perspective and focal > 0 and float(params.get('fstop', 0) or 0) > 0 and
               float(params.get('focus_distance', 0) or 0) > 0 and not config.get('disable_dof', False))
        assign(camera, 'sensor_fit', 'HORIZONTAL')
        assign(camera, 'type', 'PERSP' if perspective else 'ORTHO')
        if perspective:
            # EEVEE's aperture is lens * 1e-3 / (2 * f-stop) scene units. With
            # DOF, use the camera's real focal length (world units expressed as
            # "mm"), then choose the sensor that reproduces Houdini's projection.
            lens = min(5000., max(1., focal * 1000.)) if dof else 18. * projection[0][0]
            sensor = 2. * lens / projection[0][0]
            if dof and not 1. <= sensor <= 1000.:
                lens, sensor = 18. * projection[0][0], 36.
            assign(camera, 'lens', max(.1, lens))
            assign(camera, 'sensor_width', sensor)
            assign(camera, 'shift_x', projection[0][2] * .5)
            assign(camera, 'shift_y', projection[1][2] * .5 * out_h / out_w / pixel_ratio)
        else:
            assign(camera, 'sensor_width', 36.)
            assign(camera, 'ortho_scale', 2. / projection[0][0])
            assign(camera, 'shift_x', -projection[0][3] * .5)
            assign(camera, 'shift_y', -projection[1][3] * .5 * out_h / out_w / pixel_ratio)
        assign(camera, 'clip_start', max(.00001, params.get('clip_start', .1)))
        assign(camera, 'clip_end', max(camera.clip_start + .1, params.get('clip_end', 10000.)))
        assign(camera.dof, 'use_dof', dof)
        if dof:
            assign(camera.dof, 'aperture_fstop', float(params['fstop']))
            assign(camera.dof, 'focus_distance', max(.00001, float(params['focus_distance'])))
            aspect = float(params.get('dof_aspect', 1) or 1)
            assign(camera.dof, 'aperture_ratio', min(2., max(.01, aspect)))
        self.camera_state = camera_state

    def prepare_eevee_space(self, view, projection, render_pass):
        space = self.worker.eevee_space
        dof = self.camera.data.dof.use_dof
        assign(space, 'camera', self.camera)
        assign(space, 'use_local_camera', True)
        assign(space.shading, 'use_dof', dof)
        assign(space.shading, 'render_pass', render_pass)
        assign(space.region_3d, 'view_matrix', view)
        # EEVEE applies a camera's depth of field only when viewing through
        # it. The explicit view/projection matrices still define the framing.
        mode = 'CAMERA' if dof else 'PERSP' if abs(projection[3][3]) < 0.5 else 'ORTHO'
        assign(space.region_3d, 'view_perspective', mode)

    @staticmethod
    def read_color(offscreen, target, fmt):
        """Read the offscreen color attachment straight into ``target``."""
        height, width = target.shape[:2]
        buffer = gpu.types.Buffer(fmt, target.size, target.reshape(-1))
        with offscreen.bind():
            gpu.state.active_framebuffer_get().read_color(0, 0, width, height, 4, 0, fmt, data=buffer)

    def draw_eevee(self, purpose, view, projection, target, render_pass):
        worker = self.worker
        height, width = target.shape[:2]
        offscreen = self.offscreen(purpose, width, height, 'RGBA16F')
        with bpy.context.temp_override(window=worker.window, area=worker.eevee_area, region=worker.eevee_region):
            self.prepare_eevee_space(view, projection, render_pass)
            offscreen.draw_view3d(self.scene, self.view_layer, worker.eevee_space, worker.eevee_region, view, projection,
                                  do_color_management=False, draw_background=not self.scene.render.film_transparent)
            drawn = time.perf_counter()
            self.read_color(offscreen, target, 'FLOAT')
        self.timings['color_read_ms'] = self.timings.get('color_read_ms', 0) + (time.perf_counter() - drawn) * 1000

    def draw_workbench(self, purpose, area, region, space, view, projection, width, height, fmt, background):
        worker = self.worker
        offscreen = self.offscreen(purpose, width, height, fmt)
        with bpy.context.temp_override(window=worker.window, area=area, region=region):
            assign(space.region_3d, 'view_perspective', 'PERSP' if abs(projection[3][3]) < 0.5 else 'ORTHO')
            offscreen.draw_view3d(self.scene, self.view_layer, space, region, view, projection,
                                  do_color_management=False, draw_background=background)
        return offscreen

    def draw_ids(self, view, projection, pick, depth):
        """Workbench ID pass: packed RGBA8 pick indices and window-space depth."""
        worker = self.worker
        height, width = pick.shape
        start = time.perf_counter()
        offscreen = self.draw_workbench('ids', worker.id_area, worker.id_region, worker.id_space,
                                        view, projection, width, height, 'RGBA8', False)
        drawn = time.perf_counter()
        with offscreen.bind():
            framebuffer = gpu.state.active_framebuffer_get()
            colors = gpu.types.Buffer('UBYTE', pick.size * 4, pick.reshape(-1).view(np.uint8))
            framebuffer.read_color(0, 0, width, height, 4, 0, 'UBYTE', data=colors)
            if depth is not None:
                framebuffer.read_depth(0, 0, width, height,
                                       data=gpu.types.Buffer('FLOAT', depth.size, depth.reshape(-1)))
        self.timings['ids_draw_ms'] = (drawn - start) * 1000
        self.timings['ids_read_ms'] = (time.perf_counter() - drawn) * 1000

    def draw_preview(self, view, projection, target):
        worker = self.worker
        height, width = target.shape[:2]
        offscreen = self.draw_workbench('preview', worker.preview_area, worker.preview_region, worker.preview_space,
                                        view, projection, width, height, 'RGBA16F', True)
        self.read_color(offscreen, target, 'FLOAT')

    def draw_viewport_pass(self, definition, view, projection, width, height):
        """Live EEVEE pass in its own offscreen, so the beauty keeps accumulating."""
        name = definition['id']
        render_pass = definition['preview'] or 'POSITION'
        color = np.empty((height, width, 4), dtype=np.float32)
        self.draw_eevee('aov:' + name, view, projection, color, render_pass)
        count = pass_channels(definition)
        pixels = color[..., :3 if name == 'z' else count]
        if name == 'z':
            matrix = np.asarray(view)
            z = -(pixels @ matrix[2, :3] + matrix[2, 3])
            z[np.all(pixels == 0, axis=-1)] = 1e10
            pixels = z
        elif name in ('normal', 'position'):
            pixels = pixels @ np.asarray(self.basis.inverted().to_3x3()).T
        return pixels

    def render(self, request):
        start = time.perf_counter()
        self.timings = {}
        final = bool(request.get('final_render'))
        if request.get('reset'):
            self.reset()
        config = request.get('config') or {}
        self.configure(config, request)
        frame = request.get('frame', 1.)
        if not isinstance(frame, (int, float)) or not math.isfinite(frame) or abs(frame) > 1000000:
            frame = 1.
        if self.scene.frame_current_final != frame:
            self.scene.frame_set(math.floor(frame), subframe=frame - math.floor(frame))
        change_errors = self.update(request.get('changes', []))
        width, height = int(request['width']), int(request['height'])
        if not (1 <= width <= 8192 and 1 <= height <= 8192 and width * height <= MAX_PIXELS):
            raise ValueError('Invalid render dimensions')
        eevee_config = config.get('eevee', {})
        target_samples = int(eevee_config.get('taa_samples', request.get('samples', 16)))
        samples = max(1, min(int(request.get('samples', target_samples)), 4096))
        if not final:
            assign(self.scene.eevee, 'taa_samples', samples)
        if 'raytracing' in request and 'use_raytracing' not in eevee_config:
            assign(self.scene.eevee, 'use_raytracing', bool(request['raytracing']))
        view = Matrix(request['view']).transposed() @ self.basis.inverted()
        projection = Matrix(request['projection']).transposed()
        self.update_camera(request, config, view, projection, width, height, frame)
        limit_surface = final or bool(request.get('subdivision_limit_surface', False))
        if limit_surface != self.limit_surface:
            self.limit_surface = limit_surface
            self.dirty_geometry = True
        if self.dirty_geometry:
            self.subdivision_limited = subdivision.configure(self, config, limit_surface)
            self.instance_count, self.instances_realized = instance_nodes.configure(self)
            self.dirty_geometry = False
        if self.dirty_environment:
            environment.sync(self, config.get('environment', {}))
            self.dirty_environment = False
        # EEVEE Render Settings limit textures in the viewport and/or final renders;
        # without it, the viewport uses the delegate's limit and final renders full size.
        limits = config.get('texture_limit')
        if isinstance(limits, dict):
            limit = int(limits.get('size', 0)) if limits.get('render' if final else 'viewport') else 0
        else:
            limit = 0 if final else int(request.get('texture_limit', 0) or 0)
        self.worker.set_texture_limit(limit)
        instance_nodes.navigation(self, not final and bool(request.get('navigating', request.get('purpose') == 'navigate')),
                                  int(request.get('navigation_instances', 10) or 10), final)
        with bpy.context.temp_override(window=self.worker.window, area=self.worker.eevee_area,
                                       region=self.worker.eevee_region):
            self.view_layer.depsgraph.update()
        updated = time.perf_counter()
        preview = not final and bool(request.get('allow_preview')) and not self.eevee_drawn
        want_ids = not final and bool(request.get('ids'))
        want_depth = bool(request.get('depth'))
        passes = []
        for name in request.get('aovs', []):
            definition = next((p for p in render_passes.PASSES if p['id'] == name.removeprefix('eevee:')), None)
            if definition is None:
                raise ValueError('Unsupported render pass: ' + name)
            if not final and not definition['preview'] and definition['id'] != 'z':
                raise ValueError('Pass requires final rendering: ' + name)
            if not preview:
                passes.append((name, definition))
        output = Output(self, request)
        output.reserve('color', (height, width, 4), np.float32)
        if want_depth:
            output.reserve('depth', (height, width), np.float32)
        for name, definition in passes:
            channels = pass_channels(definition)
            output.reserve(name, (height, width, channels) if channels > 1 else (height, width), np.float32)
        if want_ids:
            output.reserve('pick', (height, width), np.uint32)
            output.reserve('pick_table', (2, self.picks.next), np.int32)
        output.allocate()
        rendered = None
        if final:
            color, rendered, depth = self.final_render(config, request, view, projection, width, height, frame)
            np.copyto(output['color'], color)
            if want_depth:
                np.copyto(output['depth'], depth)
            drawn = time.perf_counter()
        else:
            purpose = 'navigate' if request.get('purpose') == 'navigate' else 'beauty'
            if preview:
                self.draw_preview(view, projection, output['color'])
            else:
                self.draw_eevee(purpose, view, projection, output['color'], config.get('preview_pass', 'COMBINED'))
                self.eevee_drawn = True
            drawn = time.perf_counter()
            if want_ids or want_depth:
                pick = output['pick'] if want_ids else self.scratch('pick', (height, width), np.uint32)
                self.draw_ids(view, projection, pick, output['depth'] if want_depth else None)
            if want_ids:
                table = output['pick_table']
                table[0] = self.picks.prim[:self.picks.next]
                table[1] = self.picks.instance[:self.picks.next]
        for name, definition in passes:
            if final:
                pixels = rendered.get(definition['layer'])
                if pixels is None:
                    raise ValueError('EEVEE did not produce requested pass: ' + name)
            else:
                pixels = self.draw_viewport_pass(definition, view, projection, width, height)
            target = output[name]
            np.copyto(target, pixels.reshape(target.shape))
        finished = time.perf_counter()
        self.frames += 1

        if not preview and not final:
            self.last_draw_ms = (drawn - updated) * 1000
        metadata = {
            'ok': True, 'frame': self.frames, 'revision': request.get('revision', 0),
            'width': width, 'height': height, 'format': 'RGBA32F', 'origin': 'bottom-left',
            'colorspace': 'scene_linear', 'engine': 'BLENDER_EEVEE', 'gpu': self.worker.gpu,
            'session': self.number, 'purpose': request.get('purpose', 'refine'), 'preview': preview,
            'update_ms': (updated - start) * 1000, 'draw_ms': (drawn - updated) * 1000,
            'readback_ms': (finished - drawn) * 1000, 'total_ms': (finished - start) * 1000,
            'objects': len(self.objects), 'materials': len(self.materials),
            'samples': samples if not final else self.scene.eevee.taa_render_samples,
            'target_samples': target_samples, 'raytracing': self.scene.eevee.use_raytracing,
            'refinement_complete': final or (not preview and samples >= target_samples),
            'volume_count': len(self.volume_defs),
            'draw_resources_before_batching': getattr(self, 'instance_count', 0),
            'point_instances': sum(int(o['usd_instance_count']) for o in self.point_instances.values()),
            'instances_realized': getattr(self, 'instances_realized', False),
            'subdivision_budget_limited': getattr(self, 'subdivision_limited', 0),
            'material_warnings': {k: v for k, v in self.material_warnings.items() if v},
            'geometry_warnings': {k: v for k, v in self.geometry_warnings.items() if v},
            'materials_reused': self.worker.materials.reused,
            'motion_objects': sum(any(len(v) > 1 for v in a.values()) for a in self.motion_defs.values()),
            'camera_motion_samples': len(request.get('camera_samples', [])),
            'timings': self.timings,
        }
        if change_errors:
            metadata['errors'] = change_errors
        if final:
            metadata['render_passes'] = list(rendered)
            if os.environ.get('HDEEVEE_MPLAY') and os.environ.get('HDEEVEE_OUTPUT_MANIFEST'):
                with open(os.environ['HDEEVEE_OUTPUT_MANIFEST'], 'a') as manifest:
                    manifest.write(json.dumps({'mplay': True, 'frame': frame, 'render_passes': list(rendered)}) + '\n')
        if os.environ.get('HDEEVEE_TRACE'):
            self.trace(metadata, config, request, output['color'], final, frame)
        return output.finish(metadata, [name for name, _ in passes])

    def release_idle_targets(self):
        """Free render targets that are not in use; each EEVEE target is a full
        EEVEE instance (shadow pool, film and ray-tracing buffers)."""
        now = time.monotonic()
        self.release_targets([purpose for purpose in self.offscreens if purpose not in ('beauty', 'ids', 'flush') and
                              ((purpose == 'preview' and self.eevee_drawn) or
                               now - self.offscreen_used.get(purpose, now) > IDLE_TARGET_SECONDS)])

    def release_targets(self, purposes=None):
        """Free the given render targets (all but the flush target by default).
        An EEVEE target keeps the GPU textures it last drew with alive, so after
        the texture size limit changes every target must go; the next draw makes
        a new one."""
        purposes = [p for p in self.offscreens if p != 'flush'] if purposes is None else purposes
        for purpose in purposes:
            self.offscreens.pop(purpose)[1].free()
            self.offscreen_used.pop(purpose, None)
        if purposes:
            # Blender's Vulkan backend destroys freed GPU resources only when
            # it next submits work. A tiny flat draw returns the memory now.
            worker = self.worker
            self.draw_workbench('flush', worker.id_area, worker.id_region, worker.id_space,
                                Matrix.Identity(4), Matrix.Identity(4), 8, 8, 'RGBA8', False)

    def scratch(self, name, shape, dtype):
        array = self.scratch_arrays.get(name)
        if array is None or array.shape != shape or array.dtype != dtype:
            array = self.scratch_arrays[name] = np.empty(shape, dtype=dtype)
        return array

    def final_render(self, config, request, view, projection, width, height, frame):
        if os.environ.get('HDEEVEE_SAVE_BLEND'):
            bpy.ops.wm.save_as_mainfile(filepath=os.environ['HDEEVEE_SAVE_BLEND'])
        for key, animation in self.motion_defs.items():
            obj = self.objects.get(key)
            if obj is None:
                continue
            motion.transform(obj, animation.get('transform_samples', []), frame, self.basis)
            if obj.type == 'MESH':
                samples = animation.get('point_samples', [])
                velocity = animation.get('velocities')
                velocity = np.zeros((0, 3), np.float32) if velocity is None else np.asarray(velocity, np.float32).reshape(-1, 3)
                if len(samples) < 2 and len(velocity) == len(obj.data.vertices) and len(velocity):
                    points = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
                    obj.data.attributes['position'].data.foreach_get('vector', points)
                    points = points.reshape(-1, 3)
                    acceleration = animation.get('accelerations')
                    acceleration = (np.asarray(acceleration, np.float32).reshape(-1, 3)
                                    if acceleration is not None and len(acceleration) == len(points) else np.zeros_like(points))
                    fps = config.get('fps', 24) or 24
                    span = max(1., self.scene.render.motion_blur_shutter)
                    samples = [{'time': t, 'value': points + velocity * (t / fps) + .5 * acceleration * (t / fps) ** 2}
                               for t in (-span, 0., span)]
                motion.deformation(obj, samples, frame)
            for index, instance in enumerate(self.instances.get(key, [])):
                samples = [{'time': s['time'], 'value': s['value'][index]} for s in animation.get('instance_samples', [])
                           if index < len(s['value'])]
                motion.transform(instance, samples, frame, self.basis)
        motion.transform(self.camera, request.get('camera_samples', []), frame, self.basis)
        self.scene.frame_set(math.floor(frame), subframe=frame - math.floor(frame))
        window = self.worker.window
        window.scene, window.view_layer = self.scene, self.view_layer
        with bpy.context.temp_override(window=window, area=self.worker.eevee_area, region=self.worker.eevee_region):
            color, passes = render_config.final_image(self.scene, self.view_layer, config, width, height, frame,
                                                      self.basis, request.get('aovs', []))
        depth = None
        if request.get('depth'):
            z = passes['Depth'][..., 0]
            ndc = (-projection[2][2] * z + projection[2][3]) / (-projection[3][2] * z + projection[3][3])
            depth = np.clip(ndc * .5 + .5, 0, 1).astype(np.float32)
        return color, passes, depth

    def trace(self, metadata, config, request, color, final, frame):
        metadata['config'] = config
        metadata['effective_eevee'] = {p['name']: getattr(self.scene.eevee, p['name'])
                                       for p in render_config.SCHEMA['groups']['eevee']}
        metadata['final_render'] = final
        metadata['source_frame'] = frame
        metadata['textures'] = [i.filepath for i in bpy.data.images if i.source in ('FILE', 'TILED') and i.users]
        metadata['source_up_axis'] = self.up_axis
        metadata['blender_up_axis'] = 'Z'
        metadata['pixel_crc32'] = zlib.crc32(np.ascontiguousarray(color).tobytes())
        metadata['changes'] = [{'kind': c['kind'], 'id': c['id'], 'fields': list(c),
                                'parameters': c.get('parameters', {})} for c in request.get('changes', [])]
        metadata['meshes'] = {
            key: {'vertices': len(obj.data.vertices), 'custom_normals': obj.data.has_custom_normals,
                  'world_position': list(obj.matrix_world.translation), 'material': self.bindings.get(key),
                  'uv_layers': list(obj.data.uv_layers.keys()),
                  'instances': [list(o.matrix_world.translation) for o in self.instances.get(key, [])]}
            for key, obj in self.objects.items() if obj.type == 'MESH'}
        if self.frames <= 3:
            print(json.dumps({'event': 'render', **{k: v for k, v in metadata.items() if k != 'config'}}, default=str),
                  flush=True)


def pass_channels(definition):
    if definition['id'] == 'z' or definition['type'] == 'float':
        return 1
    return 4 if definition['type'] == 'color4f' else 3


class Output:
    """Reply buffers, planned before drawing so GPU reads land in their final place.

    With ``transport == 'shm'`` every buffer is a view into the session's
    shared-memory segment and nothing is copied afterwards. Otherwise private
    arrays are sent inline after the header (protocol 1 order: color, depth,
    passes, then the pick buffers that protocol 1 clients never request).
    """
    CODES = {np.dtype(np.float32): 'f4', np.dtype(np.int32): 'i4', np.dtype(np.uint32): 'u4'}

    def __init__(self, session, request):
        self.session = session
        self.shared = request.get('transport') == 'shm'
        # Protocol 1 clients read tightly packed buffers in order.
        self.align = 64 if request.get('protocol', 2) >= 2 else 1
        self.plan = []
        self.arrays = {}
        self.segment = None

    def reserve(self, name, shape, dtype):
        self.plan.append((name, tuple(int(s) for s in shape), np.dtype(dtype)))

    def allocate(self):
        offset = 0
        layout = []
        for name, shape, dtype in self.plan:
            nbytes = int(np.prod(shape)) * dtype.itemsize
            layout.append((name, shape, dtype, offset, nbytes))
            offset += (nbytes + self.align - 1) // self.align * self.align
        self.layout = layout
        if self.shared:
            self.segment = self.session.segment.ensure(max(offset, 64))
            for name, shape, dtype, start, _ in layout:
                self.arrays[name] = np.ndarray(shape, dtype=dtype, buffer=self.segment.buf, offset=start)
        else:
            for name, shape, dtype, _, _ in layout:
                self.arrays[name] = np.empty(shape, dtype=dtype)

    def __getitem__(self, name):
        return self.arrays[name]

    def finish(self, metadata, aov_names):
        descriptors = []
        for name, shape, dtype, start, nbytes in self.layout:
            descriptors.append({'name': name, 'dtype': self.CODES[dtype], 'shape': list(shape),
                                'channels': shape[2] if len(shape) == 3 else 1,
                                'width': shape[1], 'height': shape[0], 'offset': start, 'bytes': nbytes})
        metadata['buffers'] = descriptors
        sizes = {d['name']: d['bytes'] for d in descriptors}
        # Protocol 1 fields, kept for older diagnostic tools.
        metadata['color_bytes'] = sizes.get('color', 0)
        metadata['depth_bytes'] = sizes.get('depth', 0)
        metadata['aov_buffers'] = [{'name': n, 'bytes': sizes[n]} for n in aov_names]
        if self.shared:
            metadata['shm'] = {'name': self.session.segment.client_name, 'size': self.segment.size}
            self.arrays.clear()
            return metadata, []
        parts = []
        for name, _, _, start, nbytes in self.layout:
            array = self.arrays[name]
            parts.append(array.view(np.uint8).reshape(-1))
            pad = (-nbytes) % self.align
            if pad:
                parts.append(bytes(pad))
        self.arrays.clear()
        return metadata, parts
