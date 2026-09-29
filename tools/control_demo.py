"""Send one bounded test operation to the demo launched by launch_houdini.py."""
import argparse
import json
import os
import fcntl
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('op')
    parser.add_argument('--args', default='{}', help='Additional operation arguments as JSON')
    args = parser.parse_args()
    lock = (ROOT/'runtime/control.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX)
    launch = json.loads((ROOT/'artifacts/launch.json').read_text())
    if launch.get('demo') is False and not launch.get('dev_control'):
        raise RuntimeError('The active session is not the test demo. Launch with --demo to use this test utility.')
    pid = launch['launcher_pid']
    reply = ROOT/'artifacts/control_result.json'
    previous = reply.stat().st_mtime_ns if reply.exists() else 0
    path = ROOT/'runtime'/f'houdini-{pid}.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'op': args.op, **json.loads(args.args)}))
    temporary.replace(path)
    if args.op == 'quit':
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try: os.kill(pid, 0)
            except ProcessLookupError: return
            time.sleep(.1)
        raise TimeoutError('Houdini is still shutting down; do not launch over it.')
    deadline = time.monotonic() + (120 if args.op in ('render_disk', 'render_mplay', 'mplay_regression') else 30)
    while time.monotonic() < deadline:
        if reply.exists() and reply.stat().st_mtime_ns > previous:
            result = json.loads(reply.read_text())
            print(json.dumps(result, indent=2))
            return 0 if result.get('ok') else 1
        time.sleep(.1)
    raise TimeoutError('Houdini did not complete the demo operation within 30 seconds')


if __name__ == '__main__':
    raise SystemExit(main())
