#!/usr/bin/env python3
"""Verify the real wrapper twice against one already-created test worker."""
import argparse
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time
import uuid

from client import connect


class View:
    def __init__(self, binary, session, cwd):
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name('client.py')), binary,
             '-p', '--input-format', 'stream-json', '--output-format',
             'stream-json', '--verbose', '--resume', session],
            cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.selector = selectors.DefaultSelector()
        for stream in (self.process.stdout, self.process.stderr):
            self.selector.register(stream, selectors.EVENT_READ)
        self.buffer = b''
        self.stderr = b''

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + '\n').encode())
        self.process.stdin.flush()

    def until(self, predicate, timeout=40):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                message = json.loads(line)
                if predicate(message):
                    return message
            for event, _ in self.selector.select(min(1, max(0, deadline-time.monotonic()))):
                chunk = os.read(event.fileobj.fileno(), 65536)
                if not chunk:
                    self.selector.unregister(event.fileobj)
                    if event.fileobj is self.process.stdout:
                        raise RuntimeError('Wrapper closed: ' + self.stderr.decode()[-1000:])
                elif event.fileobj is self.process.stdout:
                    self.buffer += chunk
                else:
                    self.stderr += chunk
        raise RuntimeError('Wrapper response timeout; no retry worker was launched')

    def initialize(self):
        request = str(uuid.uuid4())
        self.send({'type': 'control_request', 'request_id': request,
                   'request': {'subtype': 'initialize', 'hooks': {}}})
        return self.until(lambda m: m.get('type') == 'control_response'
                          and m.get('response', {}).get('request_id') == request)

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.selector.close()
        self.process.stdout.close()
        self.process.stderr.close()


def snapshot(session):
    sock, response = connect({'op': 'list'})
    sock.close()
    return next(s for s in response['sessions'] if s['session'] == session)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--session', required=True)
    parser.add_argument('--binary', required=True)
    args = parser.parse_args()
    before = snapshot(args.session)
    print('Original worker PID:', before['pid'], flush=True)
    for attempt in range(2):
        view = View(args.binary, args.session, before['cwd'])
        try:
            response = view.initialize()['response']
            if response.get('subtype') != 'success':
                raise RuntimeError(str(response))
            print('Wrapper initialization', attempt+1, 'succeeded; native PID:',
                  response.get('response', {}).get('pid'), flush=True)
        finally:
            view.close()
        # Wait only for this connection's detach to be observed.
        for _ in range(50):
            current = snapshot(args.session)
            if not current['attached']:
                break
            time.sleep(0.05)
        if current['pid'] != before['pid'] or current['state'] == 'stopped':
            raise RuntimeError('Worker identity changed across detach')
        print('Wrapper detached; original worker remains available.', flush=True)
    print('Native phone URL unchanged:', snapshot(args.session)['remote_url'] == before['remote_url'], flush=True)


if __name__ == '__main__':
    main()
