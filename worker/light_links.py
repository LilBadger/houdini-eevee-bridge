"""USD light and shadow linking as Blender light linking.

Hydra gives each light a collection token for lightLink and shadowLink, and each
prim the tokens of the collections that include it ("categories"). Every token
becomes a Blender collection of the included objects, used as the light's
receiver (light linking) or blocker (shadow linking) collection.
"""
import bpy

from viewport_state import assign


def objects_of(session, key):
    """The Blender objects drawing one prim: itself, its instance copies, its instancer."""
    result = []
    if key in session.objects:
        result.append(session.objects[key])
    result.extend(session.instances.get(key, []))
    if key in session.point_instances:
        result.append(session.point_instances[key])
    return result


def anchor(session):
    """An unrendered empty that keeps link collections non-empty: Blender treats an
    empty receiver collection as "everything", but USD means "nothing"."""
    name = 'hde%d:link_anchor' % session.number
    return bpy.data.objects.get(name) or bpy.data.objects.new(name, None)


def sync(session):
    prefix = 'hde%d:link:' % session.number
    tokens = {token for pair in session.light_links.values() for token in pair if token}
    collections = {}
    for token in tokens:
        collection = bpy.data.collections.get(prefix + token) or bpy.data.collections.new(prefix + token)
        wanted = {anchor(session)}
        for key, categories in session.categories.items():
            if token in categories:
                wanted.update(objects_of(session, key))
        for obj in list(collection.objects):
            if obj not in wanted:
                collection.objects.unlink(obj)
        for obj in wanted:
            if collection.objects.get(obj.name) is None:
                collection.objects.link(obj)
        collections[token] = collection
    for collection in list(bpy.data.collections):
        if collection.name.startswith(prefix) and collection.name[len(prefix):] not in tokens:
            bpy.data.collections.remove(collection)
    for key, (light_token, shadow_token) in session.light_links.items():
        obj = session.objects.get(key)
        if obj is None or obj.type != 'LIGHT':
            continue
        assign(obj.light_linking, 'receiver_collection', collections.get(light_token))
        assign(obj.light_linking, 'blocker_collection', collections.get(shadow_token))


def clear(session):
    prefix = 'hde%d:link:' % session.number
    for collection in list(bpy.data.collections):
        if collection.name.startswith(prefix):
            bpy.data.collections.remove(collection)
    obj = bpy.data.objects.get('hde%d:link_anchor' % session.number)
    if obj is not None:
        bpy.data.objects.remove(obj)
