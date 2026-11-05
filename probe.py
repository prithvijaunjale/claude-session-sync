#!/usr/bin/env python3
"""Single-worker, bounded native Claude protocol compatibility probe."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
import uuid


class Claude:
    def __init__(self, binary, cwd):
        self.session = str(uuid.uuid4())
        self.process = subprocess.Popen(
            [binary, '-p', '--input-format', 'stream-json', '--output-format',
             'stream-json', '--verbose', '--replay-user-messages',
             '--include-partial-messages', '--session-id', self.session],
            cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
        )
        self.selector = selectors.DefaultSelector()
        for stream in [self.process.stdout, self.process.stderr]:
            self.selector.register(stream, selectors.EVENT_READ)
        self.buffer = b''
        self.errors = b''

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + '\n').encode())
        self.process.stdin.flush()

    def receive(self, predicate, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                if not line.strip():
                    continue
                message = json.loads(line)
                print('event', message.get('type'), message.get('subtype', ''), flush=True)
                if predicate(message):
                    return message
            events = self.selector.select(min(1, max(0, deadline-time.monotonic())))
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    self.selector.unregister(key.fileobj)
                    if key.fileobj is self.process.stdout:
                        raise RuntimeError(f'Claude stdout closed; exit={self.process.poll()}')
                elif key.fileobj is self.process.stdout:
                    self.buffer += chunk
                else:
                    self.errors = (self.errors + chunk)[-8192:]
        pid = self.process.pid
        try:
            status = Path(f'/proc/{pid}/status').read_text()
            state = next(l for l in status.splitlines() if l.startswith('State:'))
            wchan = Path(f'/proc/{pid}/wchan').read_text().strip()
        except OSError:
            state, wchan = 'unavailable', 'unavailable'
        raise TimeoutError(f'No matching response in {timeout}s; PID={pid}; {state}; wchan={wchan}. No retry worker launched.')

    def request(self, subtype, **fields):
        request_id = str(uuid.uuid4())
        self.send({'type': 'control_request', 'request_id': request_id,
                   'request': {'subtype': subtype, **fields}})
        result = self.receive(lambda m: m.get('type') == 'control_response'
                              and m.get('response', {}).get('request_id') == request_id)
        response = result['response']
        if response.get('subtype') == 'error':
            raise RuntimeError(str(response.get('error')))
        return response.get('response', {})

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        self.selector.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    parser.add_argument('--remote-control', action='store_true')
    parser.add_argument('--turn', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    state = root / '.state'
    state.mkdir(mode=0o700, exist_ok=True)
    with (state / 'probe.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = Claude(args.binary, str(root))
        enabled = False
        try:
            print('Probe worker PID', worker.process.pid, 'session', worker.session, flush=True)
            response = worker.request('initialize', hooks={})
            print('Initialize succeeded; response keys:', sorted(response), flush=True)
            if args.remote_control:
                response = worker.request('remote_control', enabled=True, name='Claude shared-session compatibility test')
                enabled = True
                print('Remote Control response:', json.dumps(response), flush=True)
            if args.turn:
                worker.send({'type': 'user', 'session_id': worker.session,
                             'parent_tool_use_id': None,
                             'message': {'role': 'user', 'content': 'Reply with exactly HOST_PROBE_OK. Do not use any tools.'}})
                result = worker.receive(lambda m: m.get('type') == 'result', timeout=45)
                print('Turn result:', result.get('subtype'), 'error:', result.get('is_error'), flush=True)
        finally:
            try:
                if enabled and worker.process.poll() is None:
                    print('Disable Remote Control:', worker.request('remote_control', enabled=False), flush=True)
            finally:
                worker.close()
                print('Probe worker stopped; existing service untouched.', flush=True)


if __name__ == '__main__':
    main()
