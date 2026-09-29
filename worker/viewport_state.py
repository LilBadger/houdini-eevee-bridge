"""RNA updates that preserve Blender's render caches."""
import struct


def equivalent(current, value):
    if current == value:
        return True
    # RNA float properties store float32. Comparing them to JSON/Python doubles
    # otherwise assigns values such as 0.012 again on every viewport draw.
    if isinstance(current, float) and isinstance(value, (float, int)):
        try:
            return current == struct.unpack('f', struct.pack('f', value))[0]
        except (OverflowError, struct.error):
            return False
    if not isinstance(value, (str, bytes)) and hasattr(value, '__len__'):
        try:
            return len(current) == len(value) and all(equivalent(a, b) for a, b in zip(current, value))
        except TypeError:
            pass
    return False


def assign(owner, name, value):
    if equivalent(getattr(owner, name), value):
        return False
    setattr(owner, name, value)
    return True
