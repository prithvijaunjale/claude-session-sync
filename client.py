#!/usr/bin/env python3
"""Native VS Code wrapper and small local host command client."""

import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import sys
import uuid

ROOT = Path(__file__).resolve().parent
SOCKET = Path(os.environ.get('CLAUDE_SESSION_HOST_STATE',
                             f'/var/tmp/{getpass.getuser()}/claude-session-host')) / 'host.sock'


def configured_projects():
    config = SOCKET.parent / 'projects.json'
    return json.loads(config.read_text()) if config.exists() else []


def project_enabled(cwd):
    projects = configured_projects()
    return '*' in projects or os.path.abspath(cwd) in projects


class ReplayFilter:
    """The official panel reads saved history itself; don't append it again."""
    def __init__(self, journal):
        self.saved = set()
        if journal.exists():
            with journal.open() as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue  # A live append may not have its final newline yet.
                    if event.get('type') in ('user', 'assistant') and event.get('uuid'):
                        self.saved.add((event['type'], event['uuid']))

    def omit(self, event):
        return (event.get('type'), event.get('uuid')) in self.saved


def hook_ids(request):
    return {callback for matchers in request.get('hooks', {}).values()
            for matcher in matchers for callback in matcher.get('hookCallbackIds', [])}


def connect(message):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(45)
    sock.connect(str(SOCKET))
    sock.sendall((json.dumps(message) + '\n').encode())
    result = b''
    while not result.endswith(b'\n'):
        chunk = sock.recv(1)
        if not chunk:
            raise RuntimeError('Host disconnected before answering')
        result += chunk
    response = json.loads(result)
    if not response.get('ok'):
        raise RuntimeError(response.get('error', 'Host request failed'))
    sock.settimeout(None)
    return sock, response


def wrapper(argv):
    binary, args = argv[0], argv[1:]
    if not any('stream-json' in a for a in args):
        # Authentication, version and other short-lived CLI commands retain
        # their normal executable and lifecycle.
        os.execv(binary, [binary, *args])
    # The editor declares its setting machine-wide. Explicit local opt-in
    # keeps other project launches on the unchanged official executable.
    projects = configured_projects()
    if '*' not in projects and os.getcwd() not in projects:
        from host import argument, native_owner
        session = argument(args, '--resume') or argument(args, '--session-id')
        if session:
            # A wrapper makes the extension skip its own already-open check.
            # Preserve that protection for passthrough projects too.
            def expired(*_):
                raise TimeoutError('Native registry read exceeded 15s; possible NFS stall. No Claude process started.')
            previous = signal.signal(signal.SIGALRM, expired)
            signal.alarm(15)
            try:
                owner = native_owner(session)
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, previous)
            if owner is not None:
                raise RuntimeError(f"Conversation already belongs to Claude PID {owner}; continue in its existing view")
        os.execv(binary, [binary, *args])
    if os.environ.get('CLAUDE_CODE_CONFIG_PROBE') == '1':
        # This is the extension's metadata-only query, not a conversation.
        # Keep its native behavior; serializing its local lock prevents
        # overlapping NFS-backed configuration reads if one probe stalls.
        descriptor = os.open(SOCKET.parent / 'config-probe.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.set_inheritable(descriptor, True)
            os.execv(binary, [binary, *args])
        except BaseException:
            os.close(descriptor)
            raise
    sock, response = connect({'op': 'attach', 'binary': binary, 'args': args,
                       'cwd': os.getcwd(), 'env': dict(os.environ)})
    uuid.UUID(response['session'])
    replay = ReplayFilter(SOCKET.parent / response['session'] / 'events.jsonl')
    selector = selectors.DefaultSelector()
    output = b''
    input_buffer = b''
    callbacks = set()
    pending_hooks = set()
    initialized = False
    previous_term = signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    selector.register(sock, selectors.EVENT_READ)
    selector.register(sys.stdin, selectors.EVENT_READ)
    try:
        while True:
            for key, _ in selector.select():
                if key.fileobj is sock:
                    chunk = sock.recv(65536)
                    if not chunk:
                        return
                    # Errors after the initial host handshake belong on stderr,
                    # never in the extension's native protocol stream.
                    output += chunk
                    while b'\n' in output:
                        line, output = output.split(b'\n', 1)
                        message = json.loads(line)
                        if message.get('ok') is False:
                            raise RuntimeError(message.get('error', 'Host connection failed'))
                        request = message.get('request', {})
                        if message.get('type') == 'control_request' and request.get('subtype') == 'hook_callback' and request.get('callback_id') in callbacks:
                            pending_hooks.add(message['request_id'])
                        if replay.omit(message):
                            continue
                        sys.stdout.buffer.write(line + b'\n')
                        sys.stdout.buffer.flush()
                else:
                    chunk = os.read(sys.stdin.fileno(), 65536)
                    if not chunk:
                        return
                    input_buffer += chunk
                    while b'\n' in input_buffer:
                        line, input_buffer = input_buffer.split(b'\n', 1)
                        message = json.loads(line)
                        request = message.get('request', {})
                        if message.get('type') == 'control_request' and request.get('subtype') == 'initialize':
                            callbacks = hook_ids(request)
                            initialized = True
                        elif message.get('type') == 'control_response':
                            pending_hooks.discard(message.get('response', {}).get('request_id'))
                        sock.sendall(line + b'\n')
    finally:
        # Only editor-provided SDK callbacks are released here. Native hooks
        # from ~/.claude/settings.json remain in effect; permissions are never
        # approved automatically. Otherwise an offline autosave callback could
        # leave the phone waiting for a window that has already closed.
        if initialized and callbacks:
            try:
                sock.settimeout(0.2)
                for request_id in pending_hooks:
                    sock.sendall((json.dumps({'type': 'control_response', 'response': {
                        'subtype': 'success', 'request_id': request_id, 'response': {'output': {}}}}) + '\n').encode())
                sock.sendall((json.dumps({'type': 'control_request', 'request_id': 'detach-' + str(uuid.uuid4()),
                    'request': {'subtype': 'initialize', 'hooks': {}}}) + '\n').encode())
            except OSError:
                pass
        signal.signal(signal.SIGTERM, previous_term)
        selector.close()
        sock.close()


def main():
    if len(sys.argv) > 1 and os.path.isabs(sys.argv[1]):
        wrapper(sys.argv[1:])
        return
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['list', 'projects', 'create', 'stop'])
    parser.add_argument('--cwd', default=os.getcwd())
    parser.add_argument('--name', default='Claude shared session')
    parser.add_argument('--session')
    parser.add_argument('--model')
    parser.add_argument('--effort', choices=['low', 'medium', 'high', 'xhigh', 'max'])
    parser.add_argument('--inherit-current', action='store_true')
    args = parser.parse_args()
    if args.operation == 'create' and not project_enabled(args.cwd):
        raise RuntimeError('This folder is not enabled for shared sessions yet. No worker or setting was changed.')
    if args.operation == 'projects' and '*' not in configured_projects():
        print(json.dumps({'ok': True, 'projects': sorted(configured_projects())}, indent=2))
        return
    message = {'op': 'list' if args.operation == 'projects' else args.operation,
               'cwd': args.cwd, 'name': args.name}
    if args.operation == 'projects':
        message['include_saved'] = True
    if args.session:
        message['session'] = args.session
    for key in ('model', 'effort'):
        if getattr(args, key):
            message[key] = getattr(args, key)
    if args.operation == 'create' and args.inherit_current:
        parent = os.environ.get('CLAUDE_CODE_SESSION_ID')
        if parent:
            connection, listing = connect({'op': 'list'})
            connection.close()
            current = next((s for s in listing['sessions'] if s['session'] == parent), None)
            if current:
                for key in ('model', 'effort'):
                    if current.get(key):
                        message.setdefault(key, current[key])
    sock, response = connect(message)
    sock.close()
    if args.operation == 'projects':
        response = {'ok': True, 'projects': sorted({s['cwd'] for s in response['sessions']})}
    print(json.dumps(response, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        print(f'Claude session host: {error}', file=sys.stderr)
        sys.exit(1)
