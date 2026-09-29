"""Local worker protocol: framed JSON headers, binary array payloads, shared pixels.

Frame layout (both directions)::

    uint32 little-endian header length
    UTF-8 JSON header
    header['payload'] bytes of binary payload (absent or 0 means none)

Protocol 2 moves large arrays out of the JSON text. The header references them
as ``{"$b": "<id>"}`` and ``header['blobs'][id] = [offset, nbytes, dtype, shape]``
locates each one inside the payload. Protocol 1 headers (plain JSON arrays and
inline pixel replies) remain accepted for older tools.

Rendered pixels can be returned through a per-connection shared-memory segment
instead of the socket. The segment name is only valid for the authenticated
connection that received it and is replaced when a larger image is needed.
"""
import json
import os
import secrets
import socket
import struct
import time
from multiprocessing import shared_memory

import numpy as np

PROTOCOL = 2
SUPPORTED = (1, 2)
MAX_HEADER = 64 * 1024 * 1024
MAX_PAYLOAD = 8 * 1024 ** 3
FRAME_DEADLINE = 120.0
DTYPES = {'f2': np.float16, 'f4': np.float32, 'f8': np.float64, 'i4': np.int32,
          'u4': np.uint32, 'i8': np.int64, 'u1': np.uint8}


class ProtocolError(ValueError):
    pass


def receive_into(conn, view, alive):
    """Fill a memoryview. Socket timeouts only poll the owner process."""
    deadline = time.monotonic() + FRAME_DEADLINE
    started = False
    while view:
        try:
            count = conn.recv_into(view)
        except socket.timeout:
            if not alive():
                raise EOFError('Worker owner exited')
            # An idle connection may wait indefinitely between frames; a
            # frame that has started must finish within the deadline.
            if started and time.monotonic() > deadline:
                raise ProtocolError('Timed out while receiving a request')
            continue
        if not count:
            raise EOFError('Connection closed')
        started = True
        view = view[count:]


def receive_exact(conn, length, alive):
    data = bytearray(length)
    receive_into(conn, memoryview(data), alive)
    return data


def resolve(value, table, payload):
    """Replace blob references with NumPy views of the payload, in place."""
    if isinstance(value, dict):
        if len(value) == 1 and '$b' in value:
            return blob(table, payload, value['$b'])
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                value[key] = resolve(item, table, payload)
        return value
    if isinstance(value, list) and value and isinstance(value[0], (dict, list)):
        for index, item in enumerate(value):
            if isinstance(item, (dict, list)):
                value[index] = resolve(item, table, payload)
    return value


def blob(table, payload, key):
    entry = table.get(str(key))
    if entry is None:
        raise ProtocolError('Unknown blob reference: ' + str(key))
    offset, nbytes, dtype, shape = entry
    if dtype not in DTYPES:
        raise ProtocolError('Unsupported blob type: ' + str(dtype))
    kind = np.dtype(DTYPES[dtype])
    if offset < 0 or nbytes < 0 or offset + nbytes > len(payload) or nbytes % kind.itemsize:
        raise ProtocolError('Blob outside payload')
    count = nbytes // kind.itemsize
    if int(np.prod(shape, dtype=np.int64)) != count:
        raise ProtocolError('Blob shape does not match its size')
    return np.frombuffer(payload, dtype=kind, count=count, offset=offset).reshape(shape)


def read_frame(conn, alive, authenticate):
    """Read one request. ``authenticate(header)`` runs before any payload read."""
    length = struct.unpack('<I', receive_exact(conn, 4, alive))[0]
    if length > MAX_HEADER:
        raise ProtocolError('Request header exceeds size limit')
    header = json.loads(receive_exact(conn, length, alive))
    if not isinstance(header, dict):
        raise ProtocolError('Request header must be an object')
    authenticate(header)
    size = int(header.get('payload', 0) or 0)
    if size < 0 or size > MAX_PAYLOAD:
        raise ProtocolError('Request payload exceeds size limit')
    payload = receive_exact(conn, size, alive) if size else bytearray()
    table = header.pop('blobs', None)
    if table:
        resolve(header, table, payload)
    return header, payload


def send_frame(conn, header, parts=()):
    parts = [memoryview(p).cast('B') for p in parts if p is not None and len(p)]
    header['payload'] = sum(p.nbytes for p in parts)
    encoded = json.dumps(header, separators=(',', ':'), default=_json_default).encode()
    conn.sendall(struct.pack('<I', len(encoded)) + encoded)
    for part in parts:
        conn.sendall(part)


def _json_default(value):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError('Not JSON serializable: ' + type(value).__name__)


def array(value, dtype, columns=None):
    """Accept a protocol-2 NumPy blob or a protocol-1 nested list."""
    result = np.asarray(value, dtype=dtype)
    if columns:
        result = result.reshape(-1, columns)
    return np.ascontiguousarray(result)


class PixelSegment:
    """Shared memory for one connection's rendered buffers.

    POSIX segments are unlinked when replaced or closed. The client keeps
    reading an older mapping until it sees a new name, which is safe after
    unlinking. Windows mappings disappear with their last handle.
    """
    def __init__(self, prefix):
        self.prefix = prefix
        self.generation = 0
        self.shm = None

    def ensure(self, size):
        if self.shm is not None and self.shm.size >= size:
            return self.shm
        self.close()
        self.generation += 1
        # Grow in 8 MiB steps so small viewport resizes reuse the segment.
        capacity = max(8 << 20, (size + (8 << 20) - 1) // (8 << 20) * (8 << 20))
        name = self.prefix + '-' + str(self.generation) + '-' + secrets.token_hex(4)
        self.shm = shared_memory.SharedMemory(name=name, create=True, size=capacity, track=False)
        return self.shm

    @property
    def client_name(self):
        # Python prefixes POSIX names with '/', which shm_open expects.
        return ('/' + self.shm.name) if os.name != 'nt' else self.shm.name

    def close(self):
        if self.shm is None:
            return
        shm, self.shm = self.shm, None
        try:
            shm.close()
        except BufferError:
            pass
        if os.name != 'nt':
            try:
                shm.unlink()
            except FileNotFoundError:
                pass


def remove_stale_segments(alive):
    """Remove Linux shared-memory segments left by crashed workers."""
    root = '/dev/shm'
    if os.name == 'nt' or not os.path.isdir(root):
        return 0
    removed = 0
    for name in os.listdir(root):
        if not name.startswith('hde-'):
            continue
        parts = name.split('-')
        try:
            pid = int(parts[1])
        except (IndexError, ValueError):
            continue
        if pid != os.getpid() and not alive(pid):
            try:
                os.unlink(os.path.join(root, name))
                removed += 1
            except OSError:
                pass
    return removed
