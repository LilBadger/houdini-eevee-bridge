"""Test/client utility for the local EEVEE worker protocol.

NumPy arrays anywhere in a request are sent as binary blobs (protocol 2).
Replies may place pixels in a shared-memory segment; they are copied out and
returned as bytes (color, depth) plus ``client.buffers`` NumPy arrays.
"""
import json
import os
import socket
import struct
from multiprocessing import shared_memory
from pathlib import Path

import numpy as np

PROTOCOL = 2
DTYPES = {np.dtype(np.float16): 'f2', np.dtype(np.float32): 'f4', np.dtype(np.float64): 'f8',
          np.dtype(np.int32): 'i4', np.dtype(np.uint32): 'u4', np.dtype(np.int64): 'i8',
          np.dtype(np.uint8): 'u1'}
TYPES = {'f4': np.float32, 'i4': np.int32, 'u4': np.uint32}


def encode(request):
    """Move NumPy arrays into a binary payload referenced from the header."""
    blobs, parts, offset = {}, [], 0

    def walk(value):
        nonlocal offset
        if isinstance(value, np.ndarray):
            array = np.ascontiguousarray(value)
            if array.dtype not in DTYPES:
                raise TypeError('Unsupported array type ' + str(array.dtype))
            key = str(len(blobs))
            pad = (-offset) % 16
            if pad:
                parts.append(b'\0' * pad)
                offset += pad
            blobs[key] = [offset, array.nbytes, DTYPES[array.dtype], list(array.shape)]
            parts.append(array.view(np.uint8).reshape(-1))
            offset += array.nbytes
            return {'$b': key}
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [walk(v) for v in value]
        return value

    header = walk(request)
    if blobs:
        header['blobs'] = blobs
    header['payload'] = offset
    return header, parts


def decode_picks(pick, table):
    """Packed RGBA8 pick indices + [prim, instance] table -> two int32 images."""
    valid = (pick >= 0xFF000000)
    index = np.where(valid, pick & 0x00FFFFFF, 0)
    index[index >= table.shape[1]] = 0
    prim, instance = table[0][index], table[1][index]
    prim[~valid] = -1
    instance[~valid] = -1
    return prim, instance


class Client:
    def __init__(self, path, protocol=PROTOCOL, timeout=30):
        path = Path(path)
        descriptor = json.loads(path.read_text()) if path.suffix == '.json' else None
        if descriptor and descriptor.get('host') != '127.0.0.1':
            raise ValueError('EEVEE only connects to loopback workers')
        self.protocol = protocol
        self.token = descriptor['token'] if descriptor else None
        self.socket = socket.socket(socket.AF_INET if descriptor else socket.AF_UNIX, socket.SOCK_STREAM)
        if descriptor:
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.socket.settimeout(timeout)
        self.socket.connect(('127.0.0.1', descriptor['port']) if descriptor else str(path))
        self.shm = None
        self.aovs = {}
        self.buffers = {}

    def close(self):
        if self.shm is not None:
            self.shm.close()
            self.shm = None
        self.socket.close()

    def receive(self, count):
        result = bytearray(count)
        view = memoryview(result)
        while view:
            received = self.socket.recv_into(view)
            if not received:
                raise EOFError('Worker closed the connection')
            view = view[received:]
        return result

    def send(self, request):
        request = {'protocol': self.protocol, **request}
        if self.token:
            request['token'] = self.token
        header, parts = encode(request)
        payload = json.dumps(header, separators=(',', ':')).encode()
        self.socket.sendall(struct.pack('<I', len(payload)) + payload)
        for part in parts:
            self.socket.sendall(part)

    def request(self, request):
        self.send(request)
        length = struct.unpack('<I', self.receive(4))[0]
        if length > 64 * 1024 * 1024:
            raise ValueError('Invalid reply length')
        metadata = json.loads(self.receive(length))
        payload = self.receive(int(metadata.get('payload', 0) or 0)) if 'payload' in metadata else None
        if not metadata.get('ok'):
            raise RuntimeError(metadata.get('error'))
        self.buffers, self.aovs = {}, {}
        if 'shm' in metadata:
            source = self._segment(metadata['shm'])
        elif payload is not None:
            source = memoryview(payload)
        else:
            # Protocol 1 inline replies predate the payload field.
            color = self.receive(metadata.get('color_bytes', 0))
            depth = self.receive(metadata.get('depth_bytes', 0))
            self.aovs = {a['name']: self.receive(a['bytes']) for a in metadata.get('aov_buffers', [])}
            return metadata, color, depth
        for descriptor in metadata.get('buffers', []):
            dtype = np.dtype(TYPES[descriptor['dtype']])
            count = descriptor['bytes'] // dtype.itemsize
            array = np.frombuffer(source, dtype=dtype, count=count, offset=descriptor['offset']).copy()
            shape = descriptor.get('shape') or ((descriptor['height'], descriptor['width']) +
                                                ((descriptor['channels'],) if descriptor['channels'] > 1 else ()))
            self.buffers[descriptor['name']] = array.reshape(shape)
        if 'pick' in self.buffers:
            self.buffers['prim_id'], self.buffers['instance_id'] = decode_picks(
                self.buffers['pick'], self.buffers['pick_table'])
        names = {'color', 'depth', 'prim_id', 'instance_id', 'pick', 'pick_table'}
        self.aovs = {name: array.tobytes() for name, array in self.buffers.items() if name not in names}
        color = self.buffers['color'].tobytes() if 'color' in self.buffers else b''
        depth = self.buffers['depth'].tobytes() if 'depth' in self.buffers else b''
        return metadata, color, depth

    def _segment(self, info):
        name = info['name'].lstrip('/') if os.name != 'nt' else info['name']
        if self.shm is None or self.shm.name != name:
            if self.shm is not None:
                self.shm.close()
            self.shm = shared_memory.SharedMemory(name=name, create=False, track=False)
        return self.shm.buf
