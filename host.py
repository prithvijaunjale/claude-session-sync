#!/usr/bin/env python3
"""Local, single-user Claude session owner. No HTTP listener or auto-restart."""

import argparse
import asyncio
import concurrent.futures
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import signal
import socket
import struct
import time
import uuid

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get('CLAUDE_SESSION_HOST_STATE',
                            f'/var/tmp/{getpass.getuser()}/claude-session-host'))
SOCKET = STATE / 'host.sock'
LIMIT = 32 * 1024 * 1024


def encode(message):
    return (json.dumps(message, separators=(',', ':')) + '\n').encode()


def argument(args, flag):
    for i, value in enumerate(args):
        if value == flag and i + 1 < len(args):
            return args[i + 1]
        if value.startswith(flag + '='):
            return value[len(flag) + 1:]
    return None


def native_owner(session):
    """Read only native registry metadata, never conversation contents."""
    directory = Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude'))) / 'sessions'
    if not directory.exists():
        return None
    for entry in directory.glob('*.json'):
        try:
            data = json.loads(entry.read_text())
            if data.get('sessionId') != session:
                continue
            pid = int(data['pid'])
            # Match the native extension's Linux PID domain: machine-id plus
            # PID namespace, NOT boot_id. Shared NFS homes contain other hosts.
            domain = data.get('pidDomain', '')
            own_domain = 'linux:' + Path('/etc/machine-id').read_text().strip() + ':' + os.readlink('/proc/self/ns/pid')
            if domain and domain != own_domain:
                continue
            stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            if str(stat[19]) == str(data.get('procStart')):
                return pid
        except FileNotFoundError:
            continue
        except (ValueError, KeyError, IndexError):
            continue
    return None


def native_catalog():
    """Read bounded transcript edges for the explicit session-list request."""
    root = Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude'))) / 'projects'
    items = []
    if not root.exists():
        return items
    for project in root.iterdir():
        if not project.is_dir():
            continue
        rows = []
        directory_hints = set()
        for transcript in project.glob('*.jsonl'):
            try:
                session = str(uuid.UUID(transcript.stem))
                size = transcript.stat().st_size
                with transcript.open('rb') as handle:
                    head = handle.read(16384)
                    tail = b''
                    if size > 16384:
                        handle.seek(max(16384, size - 16384))
                        tail = handle.read(16384)
                cwd, title = None, None
                for line in head.splitlines() + tail.splitlines():
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    cwd = event.get('cwd') or cwd
                    if event.get('type') == 'custom-title':
                        title = event.get('customTitle') or title
                    elif not title and event.get('type') == 'summary':
                        title = event.get('summary')
                if cwd and re.sub(r'[^a-zA-Z0-9-]', '-', cwd) == project.name:
                    directory_hints.add(cwd)
                rows.append({'session': session, 'cwd': cwd, 'title': title,
                             'state': 'saved', 'attached': False, 'pid': None,
                             'remote_url': None})
            except FileNotFoundError:
                continue
            except ValueError:
                continue
        for entry in rows:
            # Large first/last message lines can hide cwd from both bounded
            # samples. Use a directory identity verified by another transcript
            # only when that identity is unambiguous; never guess by decoding
            # hyphens back into slashes.
            if not entry['cwd'] and len(directory_hints) == 1:
                entry['cwd'] = next(iter(directory_hints))
            if entry['cwd']:
                items.append(entry)
    return items


def worker_in_io_wait(pid):
    """Check local procfs only when an explicit new conversation is requested."""
    try:
        for task in Path(f'/proc/{pid}/task').iterdir():
            try:
                state = (task / 'stat').read_text().rsplit(')', 1)[1].split()[0]
                if state == 'D':
                    return True
            except FileNotFoundError:
                continue
    except FileNotFoundError:
        pass
    return False


class Worker:
    def __init__(self, host, session, binary, args, cwd, env):
        self.host, self.session, self.binary = host, session, binary
        self.args, self.cwd, self.env = args, cwd, env
        self.process = None
        self.client = None
        self.history = []
        self.pending = {}
        self.internal = {}
        self.initialized = False
        self.ready = False
        self.remote_url = None
        self.remote_id = None
        self.state = 'starting'
        self.start_time = time.time()
        self.tasks = []
        self.write_lock = asyncio.Lock()
        self.journal = None
        self.stderr = None
        self.init_event = None
        self.model = argument(args, '--model')
        self.effort = argument(args, '--effort')
        self.control_updates = {}
        self.last_activity = time.time()
        self.activity_clock = time.monotonic()
        self.background_tasks = set()
        self.queued_inputs = 0
        self.stopped_reason = None

    def touch(self):
        self.last_activity = time.time()
        self.activity_clock = time.monotonic()

    def observe(self, message):
        """Track real work from both phone and editor; ignore connection traffic."""
        kind, subtype = message.get('type'), message.get('subtype')
        if kind == 'stream_event' and message.get('event', {}).get('type') == 'ping':
            return
        if kind in ('user', 'assistant', 'stream_event', 'tool_progress'):
            self.touch()
            if kind != 'tool_progress':
                self.state = 'working'
        elif kind == 'result':
            self.touch()
            consumed = len(message.get('user_message_uuids') or []) or 1
            self.queued_inputs = max(0, self.queued_inputs - consumed,
                                     message.get('queued_turn_count') or 0)
            self.state = 'working' if self.queued_inputs else 'idle'
        elif kind == 'system':
            if subtype == 'background_tasks_changed':
                tasks = {item['task_id'] for item in message.get('tasks', [])}
                if tasks != self.background_tasks:
                    self.touch()
                self.background_tasks = tasks
            elif subtype in ('task_started', 'task_progress', 'task_updated', 'task_notification'):
                task_id = message.get('task_id')
                status = message.get('status') or message.get('patch', {}).get('status')
                if status in ('completed', 'failed', 'stopped', 'killed'):
                    self.background_tasks.discard(task_id)
                elif task_id:
                    self.background_tasks.add(task_id)
                self.touch()
            elif subtype == 'status' and message.get('status'):
                self.touch()
                self.state = 'working'

    def expired(self, now=None):
        return (self.process is not None and self.process.returncode is None
                and self.state == 'idle' and not self.pending and not self.internal
                and not self.control_updates and not self.background_tasks
                and not self.queued_inputs
                and (time.monotonic() if now is None else now) - self.activity_clock >= self.host.idle_seconds)

    def info(self):
        return {'session': self.session, 'cwd': self.cwd,
                'pid': self.process.pid if self.process else None,
                'state': self.state, 'attached': self.client is not None,
                'remote_url': self.remote_url, 'remote_id': self.remote_id,
                'model': self.model, 'effort': self.effort,
                'started_at': self.start_time, 'last_activity_at': self.last_activity,
                'background_tasks': sorted(self.background_tasks),
                'queued_inputs': self.queued_inputs, 'stopped_reason': self.stopped_reason}

    async def start(self):
        # Logs, socket and journal are on the same local filesystem as this code.
        directory = STATE / self.session
        directory.mkdir(mode=0o700, exist_ok=True)
        self.journal = (directory / 'events.jsonl').open('ab', buffering=0)
        self.stderr = (directory / 'stderr.log').open('ab', buffering=0)
        # Popen's child-exec handshake can block if its cwd is on stalled NFS.
        # Exec a local shell first; it changes directory and replaces itself
        # with Claude at the same PID. Any slow chdir stays in that one worker,
        # while the host remains able to report the stall and serve clients.
        self.process = await asyncio.create_subprocess_exec(
            '/bin/sh', '-c', 'cd "$1" || exit; shift; exec "$@"',
            'claude-session-host', self.cwd, self.binary, *self.args,
            cwd=str(ROOT), env=self.env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=self.stderr, limit=LIMIT, start_new_session=True)
        self.tasks = [asyncio.create_task(self.pump()), asyncio.create_task(self.watch())]
        self.save()

    def save(self):
        path = STATE / self.session / 'session.json'
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.info(), indent=2) + '\n')
        tmp.replace(path)

    async def send(self, message):
        if self.process is None or self.process.returncode is not None:
            raise RuntimeError('Claude worker has stopped; no automatic replacement was started')
        if self.state == 'stopping':
            raise RuntimeError('Claude worker is stopping; reopen the saved conversation to resume it')
        async with self.write_lock:
            request = message.get('request', {})
            if message.get('type') == 'control_request' and request.get('subtype') in ('set_model', 'apply_flag_settings'):
                self.control_updates[message['request_id']] = request
                self.touch()
            if message.get('type') == 'user':
                self.queued_inputs += 1
                self.touch()
                self.state = 'working'
            elif message.get('type') == 'control_response':
                self.touch()
            self.process.stdin.write(encode(message))
            await self.process.stdin.drain()
        if message.get('type') == 'user':
            self.state = 'working'
            self.save()

    async def deliver(self, message):
        if self.client is not None:
            self.client.write(encode(message))
            try:
                await asyncio.wait_for(self.client.drain(), timeout=5)
            except (ConnectionError, asyncio.TimeoutError):
                self.client.close()
                self.client = None

    async def pump(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                kind = message.get('type')
                self.observe(message)
                if kind == 'system' and message.get('subtype') == 'init':
                    self.init_event = message
                if kind == 'control_response':
                    response = message.get('response', {})
                    request_id = response.get('request_id')
                    update = self.control_updates.pop(request_id, None)
                    if update and response.get('subtype') == 'success':
                        if update['subtype'] == 'set_model':
                            self.model = update.get('model')
                        elif 'effortLevel' in update.get('settings', {}):
                            self.effort = update['settings']['effortLevel']
                        self.save()
                    future = self.internal.pop(request_id, None)
                    if future is not None:
                        if not future.done():
                            future.set_result(response)
                        continue
                    result = response.get('response', {})
                    if isinstance(result, dict) and result.get('session_url'):
                        self.remote_url = result['session_url']
                        self.remote_id = result.get('bridge_session_id')
                        self.save()
                elif kind == 'control_request':
                    self.pending[message['request_id']] = message
                    self.touch()
                elif kind == 'control_cancel_request':
                    self.pending.pop(message.get('request_id'), None)
                    self.touch()
                elif kind in ('user', 'assistant', 'result'):
                    # Stable native events carry UUIDs; keep them for reattachment.
                    self.history.append(message)
                    self.journal.write(line)
                    if kind == 'assistant' and message.get('message', {}).get('model'):
                        self.model = message['message']['model']
                if kind == 'result':
                    self.save()
                if self.ready:
                    await self.deliver(message)
        except (OSError, ValueError, asyncio.LimitOverrunError) as error:
            self.state = 'protocol-error'
            self.save()
            print(f'worker {self.session}: {type(error).__name__}', flush=True)

    async def watch(self):
        await self.process.wait()
        self.state = 'stopped'
        self.save()
        for future in self.internal.values():
            if not future.done():
                future.set_exception(RuntimeError('Claude exited before replying'))
        self.internal.clear()
        if self.client:
            self.client.close()
        self.stderr.close()
        self.journal.close()

    async def request(self, subtype, **fields):
        request_id = 'host-' + str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.internal[request_id] = future
        await self.send({'type': 'control_request', 'request_id': request_id,
                         'request': {'subtype': subtype, **fields}})
        try:
            response = await asyncio.wait_for(future, 30)
        except asyncio.TimeoutError:
            self.state = 'unresponsive'
            self.save()
            raise RuntimeError('Claude did not reply in 30s. Worker retained; no retry worker launched.')
        finally:
            self.internal.pop(request_id, None)
        if response.get('subtype') == 'error':
            raise RuntimeError(str(response.get('error')))
        self.initialized = True
        if subtype == 'initialize' and self.state == 'starting':
            self.state = 'idle'
            self.save()
        return response.get('response', {})

    async def stop(self, reason='explicit'):
        if self.process is None or self.process.returncode is not None:
            return
        self.stopped_reason = reason
        self.state = 'stopping'
        self.save()
        # Disconnecting a client never reaches this path.
        self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), 5)
        except asyncio.TimeoutError:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                os.killpg(self.process.pid, signal.SIGKILL)
                await asyncio.wait_for(self.process.wait(), 5)
        await asyncio.gather(*self.tasks, return_exceptions=True)


class Host:
    def __init__(self, binary, max_workers=1, idle_seconds=3 * 24 * 60 * 60):
        self.binary, self.max_workers = binary, max_workers
        self.idle_seconds = idle_seconds
        self.workers = {}
        self.creation_lock = asyncio.Lock()
        # One NFS reader. A stalled read never creates more reader threads.
        self.storage = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.storage_pending = None

    async def reap_idle(self):
        for worker in list(self.workers.values()):
            if worker.expired() and not worker_in_io_wait(worker.process.pid):
                # Recheck after the procfs inspection. No await between the
                # final decision and marking/closing the worker's input.
                if worker.expired():
                    print(f'worker {worker.session}: stopping after three days inactive', flush=True)
                    await worker.stop(reason='idle-timeout')
                    # Stable replay is also on disk; release the host's copy.
                    worker.history.clear()

    async def idle_loop(self):
        while True:
            await asyncio.sleep(60)
            await self.reap_idle()

    async def read_storage(self, function, *args):
        if self.storage_pending is not None and not self.storage_pending.done():
            raise RuntimeError('Native registry read is still pending; possible NFS stall. No new worker started.')
        self.storage_pending = asyncio.get_running_loop().run_in_executor(self.storage, function, *args)
        try:
            return await asyncio.wait_for(asyncio.shield(self.storage_pending), 15)
        except asyncio.TimeoutError:
            raise RuntimeError('Native registry read exceeded 15s; possible NFS stall. No new worker started.')

    async def owner(self, session):
        return await self.read_storage(native_owner, session)

    def existing(self, session, cwd):
        worker = self.workers.get(session)
        if worker is None:
            return None
        if worker.cwd != cwd:
            raise RuntimeError('Session belongs to another project directory')
        if worker.state == 'stopped' and worker.process.returncode is not None:
            # A new, explicit open may resume a confirmed-dead worker. Never
            # replace a merely slow, unresponsive, or protocol-error worker.
            self.workers.pop(session)
            return None
        if worker.state in ('protocol-error', 'unresponsive'):
            raise RuntimeError('Existing worker needs inspection; automatic respawn is disabled')
        if worker.state == 'stopping':
            raise RuntimeError('Worker is stopping; reopen after it exits')
        return worker

    async def get(self, message):
        args = list(message.get('args', []))
        cwd = message.get('cwd')
        if not cwd or not os.path.isabs(cwd):
            raise ValueError('An absolute working directory is required')
        requested = argument(args, '--resume') or argument(args, '--session-id') or message.get('session')
        worker = self.existing(requested, cwd)
        if worker is not None:
            return worker
        async with self.creation_lock:
            # Re-check after waiting: two clients must never spawn two owners.
            worker = self.existing(requested, cwd)
            if worker is not None:
                return worker
            for existing in self.workers.values():
                if existing.process is None or existing.process.returncode is not None:
                    continue
                if existing.state in ('starting', 'unresponsive', 'protocol-error'):
                    raise RuntimeError('Another worker has not completed startup or needs inspection; no additional worker started.')
                if worker_in_io_wait(existing.process.pid):
                    raise RuntimeError('A worker is in uninterruptible I/O wait; possible NFS stall. No additional worker started.')
            if any(w.process and w.process.returncode is None for w in self.workers.values()):
                if sum(w.process.returncode is None for w in self.workers.values() if w.process) >= self.max_workers:
                    raise RuntimeError(f'The session host is at its {self.max_workers}-conversation limit. Close one before starting another.')
            session = requested or str(uuid.uuid4())
            uuid.UUID(session)
            owner = await self.owner(session)
            if owner is not None:
                raise RuntimeError(f'Session is already owned by native Claude PID {owner}; it was not taken over')
            previous = STATE / session / 'session.json'
            saved = json.loads(previous.read_text()) if previous.exists() else {}
            if saved and saved.get('cwd') != cwd:
                raise RuntimeError('Saved session belongs to another project directory')
            if not args:
                args = ['-p', '--input-format', 'stream-json', '--output-format', 'stream-json',
                        '--verbose', '--replay-user-messages', '--include-partial-messages',
                        '--permission-prompt-tool=stdio']
                if requested:
                    args += ['--resume', requested]
                for key in ('model', 'effort'):
                    value = message.get(key) or saved.get(key)
                    if value:
                        args += ['--' + key, str(value)]
            if requested is None:
                args += ['--session-id', session]
            env = dict(message.get('env') or os.environ)
            binary = message.get('binary') or self.binary
            worker = Worker(self, session, binary, args, cwd, env)
            if saved:
                worker.remote_id = saved.get('remote_id')
                worker.remote_url = saved.get('remote_url')
            self.workers[session] = worker
            try:
                await worker.start()
            except BaseException:
                self.workers.pop(session, None)
                raise
            return worker

    async def handle(self, reader, writer):
        worker = None
        try:
            peer = writer.get_extra_info('socket')
            _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                raise PermissionError('Same-user connections only')
            first = json.loads(await asyncio.wait_for(reader.readline(), 10))
            op = first.get('op')
            if op == 'list':
                sessions = {w.session: w.info() for w in self.workers.values()}
                if first.get('include_saved'):
                    for entry in await self.read_storage(native_catalog):
                        sessions.setdefault(entry['session'], entry)
                writer.write(encode({'ok': True, 'sessions': list(sessions.values())}))
            elif op == 'stop':
                session = first['session']
                await self.workers[session].stop()
                writer.write(encode({'ok': True}))
            elif op in ('create', 'attach'):
                worker = await self.get(first)
                if op == 'create':
                    if not worker.initialized:
                        await worker.request('initialize', hooks={})
                    options = {'enabled': True, 'name': first.get('name', 'Claude shared session'),
                               'keep_session_on_exit': True}
                    if worker.remote_id:
                        options['reattach_session_id'] = worker.remote_id
                    result = await worker.request('remote_control', **options)
                    worker.remote_url = result.get('session_url')
                    worker.remote_id = result.get('bridge_session_id')
                    worker.save()
                    writer.write(encode({'ok': True, **worker.info()}))
                else:
                    if worker.client is not None:
                        raise RuntimeError('Session already has a VS Code client; detach that view first')
                    worker.client = writer
                    worker.ready = False
                    writer.write(encode({'ok': True, **worker.info()}))
                    await writer.drain()
                    while line := await reader.readline():
                        message = json.loads(line)
                        request = message.get('request', {})
                        if message.get('type') == 'control_request' and request.get('subtype') == 'initialize':
                            # CLI supports reinitialization over stdin. Map the
                            # response back to this client rather than replaying
                            # old request IDs from a disconnected extension.
                            result = await worker.request('initialize', **{k: v for k, v in request.items() if k != 'subtype'})
                            writer.write(encode({'type': 'control_response', 'response': {
                                'subtype': 'success', 'request_id': message['request_id'], 'response': result}}))
                            if worker.init_event:
                                writer.write(encode(worker.init_event))
                            for event in worker.history:
                                writer.write(encode(event))
                            for event in worker.pending.values():
                                writer.write(encode(event))
                            await writer.drain()
                            worker.ready = True
                        else:
                            if message.get('type') == 'control_response':
                                request_id = message.get('response', {}).get('request_id')
                                worker.pending.pop(request_id, None)
                            await worker.send(message)
            else:
                raise ValueError('Unknown host operation')
            await writer.drain()
        except (Exception, asyncio.CancelledError) as error:
            if not writer.is_closing():
                writer.write(encode({'ok': False, 'error': str(error)}))
                try:
                    await writer.drain()
                except ConnectionError:
                    pass
        finally:
            if worker is not None and worker.client is writer:
                worker.client = None
                worker.ready = False
                worker.save()
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def run(self):
        STATE.mkdir(mode=0o700, exist_ok=True)
        os.chmod(STATE, 0o700)
        lock = (STATE / 'host.lock').open('w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        SOCKET.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self.handle, path=str(SOCKET), limit=LIMIT)
        os.chmod(SOCKET, 0o600)
        stopped = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, stopped.set)
        print(f'Claude session host ready; socket={SOCKET}; max_workers={self.max_workers}', flush=True)
        cleanup = asyncio.create_task(self.idle_loop())
        try:
            async with server:
                await stopped.wait()
        finally:
            cleanup.cancel()
            await asyncio.gather(cleanup, return_exceptions=True)
            for worker in self.workers.values():
                await worker.stop()
            SOCKET.unlink(missing_ok=True)
            self.storage.shutdown(wait=False, cancel_futures=True)
            lock.close()


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    parser.add_argument('--max-workers', type=int, default=1)
    options = parser.parse_args()
    if options.max_workers < 1:
        parser.error('--max-workers must be positive')
    asyncio.run(Host(options.binary, max_workers=options.max_workers).run())
