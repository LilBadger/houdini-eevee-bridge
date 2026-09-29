"""Reuse built EEVEE materials across scene resets and viewports.

Rebuilding a production scene after a renderer restart or reconnect used to
recreate every material, forcing EEVEE to generate and often compile its
shaders again. A material whose translated definition is unchanged is kept in
this pool when its owner releases it, and handed to the next owner that asks
for the same definition. Compiled GPU shaders stay attached to it.
"""
from collections import OrderedDict
import hashlib
import json
import time

import bpy

KEYS = ('graph', 'materialx_network', 'usd_network', 'parameters')


def digest(update, up_axis):
    definition = {key: update[key] for key in KEYS if key in update}
    text = json.dumps([definition, up_axis], sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha1(text.encode()).hexdigest()


class MaterialPool:
    def __init__(self, limit=512, lifetime=1800.):
        self.limit = limit
        self.lifetime = lifetime
        self.retired = OrderedDict()   # (digest, serial) -> (material, retired time)
        self.serial = 0
        self.reused = 0

    def take(self, key):
        for entry in list(self.retired):
            if entry[0] != key:
                continue
            material, _ = self.retired.pop(entry)
            try:
                material.name
            except ReferenceError:
                continue
            self.reused += 1
            return material
        return None

    def retire(self, key, material):
        if material is None:
            return
        if key is None:
            self.remove(material)
            return
        self.serial += 1
        self.retired[(key, self.serial)] = (material, time.monotonic())
        self.trim()

    def trim(self):
        now = time.monotonic()
        while self.retired:
            entry, (material, when) = next(iter(self.retired.items()))
            if len(self.retired) <= self.limit and now - when < self.lifetime:
                break
            self.retired.pop(entry)
            self.remove(material)

    @staticmethod
    def remove(material):
        try:
            if material.users == 0:
                bpy.data.materials.remove(material)
        except ReferenceError:
            pass
