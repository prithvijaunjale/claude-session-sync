import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import host


class HostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_state = host.STATE
        host.STATE = Path(self.temp.name)
        self.owner_patch = patch.object(host, 'native_owner', return_value=None)
        self.owner_patch.start()
        self.host = host.Host(sys.executable)
        self.socket = str(Path(self.temp.name) / 'host.sock')
        self.server = await asyncio.start_unix_server(self.host.handle, path=self.socket)
        self.clients = []
        self.session = str(uuid.uuid4())
        self.args = [str(Path(__file__).with_name('fake_claude.py')), '--session-id', self.session]

    async def asyncTearDown(self):
        for writer in self.clients:
            writer.close()
            await writer.wait_closed()
        for worker in self.host.workers.values():
            await worker.stop()
        self.server.close()
        await self.server.wait_closed()
        self.host.storage.shutdown(wait=True)
        self.owner_patch.stop()
        host.STATE = self.original_state
        self.temp.cleanup()

    async def connect(self, op='attach', **extra):
        reader, writer = await asyncio.open_unix_connection(self.socket)
        self.clients.append(writer)
        writer.write(host.encode({'op': op, 'binary': sys.executable, 'args': self.args,
                                  'cwd': self.temp.name, **extra}))
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readline(), 2))
        return reader, writer, response

    async def until(self, reader, predicate):
        while True:
            message = json.loads(await asyncio.wait_for(reader.readline(), 2))
            if predicate(message):
                return message

    async def initialize(self, reader, writer):
        writer.write(host.encode({'type': 'control_request', 'request_id': 'client-initialize',
                                  'request': {'subtype': 'initialize', 'hooks': {}}}))
        await writer.drain()
        return await self.until(reader, lambda m: m.get('type') == 'control_response')

    async def detach(self, writer):
        writer.close()
        await writer.wait_closed()
        for _ in range(50):
            if self.host.workers[self.session].client is None:
                return
            await asyncio.sleep(0.01)
        self.fail('Client did not detach')

    async def test_disconnect_keeps_worker_and_replays_history(self):
        reader, writer, response = await self.connect()
        self.assertTrue(response['ok'])
        await self.initialize(reader, writer)
        writer.write(host.encode({'type': 'user', 'session_id': self.session,
                                  'message': {'role': 'user', 'content': 'hello'}}))
        await writer.drain()
        await self.until(reader, lambda m: m.get('type') == 'result')
        pid = self.host.workers[self.session].process.pid
        await self.detach(writer)
        reader, writer, response = await self.connect()
        self.assertEqual(response['pid'], pid)
        await self.initialize(reader, writer)
        answer = await self.until(reader, lambda m: m.get('type') == 'assistant')
        self.assertEqual(answer['uuid'], 'answer-1')
        self.assertIsNone(self.host.workers[self.session].process.returncode)

    async def test_idle_expiry_stops_only_after_full_three_days_and_resumes(self):
        reader, writer, first = await self.connect()
        await self.initialize(reader, writer)
        worker = self.host.workers[self.session]
        worker.activity_clock -= 3 * 86400 - 1
        await self.host.reap_idle()
        self.assertIsNone(worker.process.returncode)
        worker.activity_clock -= 2
        await self.host.reap_idle()
        self.assertIsNotNone(worker.process.returncode)
        self.assertEqual(worker.stopped_reason, 'idle-timeout')
        saved = json.loads((host.STATE / self.session / 'session.json').read_text())
        self.assertEqual(saved['state'], 'stopped')
        reader, writer, resumed = await self.connect()
        self.assertTrue(resumed['ok'])
        self.assertNotEqual(first['pid'], resumed['pid'])
        self.assertEqual(first['session'], resumed['session'])
        await self.initialize(reader, writer)

    async def test_busy_or_uncertain_workers_never_expire(self):
        reader, writer, _ = await self.connect()
        await self.initialize(reader, writer)
        worker = self.host.workers[self.session]
        worker.activity_clock -= 4 * 86400
        for state in ('working', 'starting', 'unresponsive', 'protocol-error', 'stopping'):
            worker.state = state
            self.assertFalse(worker.expired())
        worker.state = 'idle'
        for field, value, empty in [('pending', {'question': {}}, {}),
                                    ('internal', {'rpc': None}, {}),
                                    ('control_updates', {'settings': {}}, {}),
                                    ('background_tasks', {'task'}, set()),
                                    ('queued_inputs', 1, 0)]:
            setattr(worker, field, value)
            self.assertFalse(worker.expired(), field)
            setattr(worker, field, empty)
        self.assertTrue(worker.expired())
        with patch.object(host, 'worker_in_io_wait', return_value=True):
            await self.host.reap_idle()
        self.assertIsNone(worker.process.returncode)

    async def test_phone_activity_and_background_tasks_reset_idle_clock(self):
        reader, writer, _ = await self.connect()
        await self.initialize(reader, writer)
        worker = self.host.workers[self.session]
        for message in [{'type': 'user'}, {'type': 'assistant'}, {'type': 'stream_event'},
                        {'type': 'tool_progress'}, {'type': 'result'}]:
            worker.activity_clock -= 4 * 86400
            worker.observe(message)
            self.assertFalse(worker.expired())
        worker.observe({'type': 'system', 'subtype': 'background_tasks_changed',
                        'tasks': [{'task_id': 'bash'}]})
        worker.observe({'type': 'result'})
        worker.activity_clock -= 4 * 86400
        self.assertFalse(worker.expired())
        worker.observe({'type': 'system', 'subtype': 'task_updated',
                        'task_id': 'bash', 'patch': {'status': 'completed'}})
        self.assertFalse(worker.background_tasks)
        self.assertFalse(worker.expired())

    async def test_metadata_and_empty_task_snapshots_do_not_extend_idle(self):
        reader, writer, _ = await self.connect()
        await self.initialize(reader, writer)
        worker = self.host.workers[self.session]
        worker.activity_clock -= 4 * 86400
        before = worker.activity_clock
        for message in [{'type': 'system', 'subtype': 'init'},
                        {'type': 'system', 'subtype': 'heartbeat'},
                        {'type': 'stream_event', 'event': {'type': 'ping'}},
                        {'type': 'system', 'subtype': 'background_tasks_changed', 'tasks': []},
                        {'type': 'control_response'}]:
            worker.observe(message)
        await self.initialize(reader, writer)
        self.assertEqual(before, worker.activity_clock)
        self.assertTrue(worker.expired())

    async def test_queued_user_messages_hold_worker_until_all_results(self):
        reader, writer, _ = await self.connect()
        await self.initialize(reader, writer)
        worker = self.host.workers[self.session]
        worker.queued_inputs = 2
        worker.observe({'type': 'result'})
        self.assertEqual(worker.queued_inputs, 1)
        self.assertEqual(worker.state, 'working')
        worker.observe({'type': 'result', 'queued_turn_count': 1})
        self.assertEqual(worker.state, 'working')
        worker.observe({'type': 'result', 'queued_turn_count': 0})
        self.assertEqual(worker.state, 'idle')

    async def test_pending_approval_survives_detach(self):
        reader, writer, _ = await self.connect()
        await self.initialize(reader, writer)
        writer.write(host.encode({'type': 'user', 'session_id': self.session,
                                  'message': {'role': 'user', 'content': 'approval'}}))
        await writer.drain()
        await self.until(reader, lambda m: m.get('type') == 'control_request')
        await self.detach(writer)
        reader, writer, _ = await self.connect()
        await self.initialize(reader, writer)
        approval = await self.until(reader, lambda m: m.get('type') == 'control_request')
        self.assertEqual(approval['request_id'], 'permission-1')
        writer.write(host.encode({'type': 'control_response', 'response': {
            'subtype': 'success', 'request_id': 'permission-1', 'response': {'behavior': 'deny'}}}))
        await writer.drain()
        await self.until(reader, lambda m: m.get('type') == 'result')
        self.assertFalse(self.host.workers[self.session].pending)

    async def test_killing_adapter_releases_only_its_editor_hook(self):
        root = Path(self.temp.name)
        (root / 'projects.json').write_text(json.dumps([self.temp.name]))
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).resolve().parents[1] / 'client.py'),
            sys.executable, *self.args, '--input-format', 'stream-json',
            cwd=self.temp.name, env={**os.environ, 'CLAUDE_SESSION_HOST_STATE': self.temp.name},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            process.stdin.write(host.encode({'type': 'control_request', 'request_id': 'initialize',
                'request': {'subtype': 'initialize', 'hooks': {
                    'PreToolUse': [{'hookCallbackIds': ['autosave']}]}}}))
            await process.stdin.drain()
            await self.until(process.stdout, lambda m: m.get('type') == 'control_response')
            process.stdin.write(host.encode({'type': 'user', 'session_id': self.session,
                'message': {'role': 'user', 'content': 'hook'}}))
            await process.stdin.drain()
            await self.until(process.stdout, lambda m: m.get('type') == 'control_request')
            process.terminate()
            await asyncio.wait_for(process.wait(), 3)
            for _ in range(100):
                worker = self.host.workers[self.session]
                if worker.client is None and worker.state == 'idle':
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(worker.state, 'idle')
            self.assertFalse(worker.pending)
            self.assertIsNone(worker.process.returncode)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def test_second_controller_is_rejected(self):
        _, _, first = await self.connect()
        _, _, second = await self.connect()
        self.assertTrue(first['ok'])
        self.assertFalse(second['ok'])
        self.assertEqual(len(self.host.workers), 1)

    async def test_other_project_cannot_attach(self):
        await self.connect()
        _, _, response = await self.connect(cwd='/tmp/another-project')
        self.assertFalse(response['ok'])

    async def test_concurrent_other_project_cannot_attach(self):
        first, second = await asyncio.gather(self.connect(), self.connect(cwd='/tmp/another-project'))
        self.assertTrue(first[2]['ok'])
        self.assertFalse(second[2]['ok'])
        self.assertEqual(len(self.host.workers), 1)

    async def test_worker_limit_does_not_spawn_on_contention(self):
        await self.connect()
        other = [self.args[0], '--session-id', str(uuid.uuid4())]
        _, _, response = await self.connect(args=other)
        self.assertFalse(response['ok'])
        self.assertEqual(len(self.host.workers), 1)

    async def test_launcher_exec_keeps_pid_and_requested_project(self):
        reader, writer, response = await self.connect()
        init = await self.initialize(reader, writer)
        native = init['response']['response']
        self.assertEqual(native['pid'], response['pid'])
        self.assertEqual(native['cwd'], self.temp.name)

    async def test_io_wait_blocks_new_worker_even_with_free_capacity(self):
        reader, writer, _ = await self.connect()
        await self.initialize(reader, writer)
        self.host.max_workers = 2
        other = [self.args[0], '--session-id', str(uuid.uuid4())]
        with patch.object(host, 'worker_in_io_wait', return_value=True):
            _, _, response = await self.connect(args=other)
        self.assertFalse(response['ok'])
        self.assertIn('I/O wait', response['error'])
        self.assertEqual(len(self.host.workers), 1)

    async def test_second_requested_conversation_starts_only_when_healthy(self):
        reader, writer, first = await self.connect()
        await self.initialize(reader, writer)
        self.host.max_workers = 2
        other = [self.args[0], '--session-id', str(uuid.uuid4())]
        reader, writer, second = await self.connect(args=other)
        self.assertTrue(second['ok'])
        await self.initialize(reader, writer)
        self.assertNotEqual(first['pid'], second['pid'])
        self.assertEqual(len(self.host.workers), 2)
        third = [self.args[0], '--session-id', str(uuid.uuid4())]
        _, _, refused = await self.connect(args=third)
        self.assertFalse(refused['ok'])
        self.assertEqual(len(self.host.workers), 2)

    async def test_native_owner_is_preserved(self):
        with patch.object(host, 'native_owner', return_value=os.getpid()):
            _, _, response = await self.connect()
        self.assertFalse(response['ok'])
        self.assertIn('already owned', response['error'])
        self.assertFalse(self.host.workers)

    async def test_pending_nfs_read_refuses_new_worker(self):
        self.host.storage_pending = asyncio.get_running_loop().create_future()
        _, _, response = await self.connect()
        self.assertFalse(response['ok'])
        self.assertIn('possible NFS stall', response['error'])
        self.assertFalse(self.host.workers)
        self.host.storage_pending.cancel()

    async def test_explicit_resume_preserves_native_phone_identity(self):
        directory = host.STATE / self.session
        directory.mkdir()
        (directory / 'session.json').write_text(json.dumps({
            'cwd': self.temp.name, 'remote_id': 'previous-phone-session',
            'remote_url': 'https://example.invalid/session/test'}))
        _, _, response = await self.connect(op='create')
        self.assertTrue(response['ok'])
        self.assertEqual(response['remote_id'], 'previous-phone-session')

    async def test_successful_model_and_effort_controls_are_preserved(self):
        reader, writer, _ = await self.connect()
        await self.initialize(reader, writer)
        for request_id, request in [
            ('model', {'subtype': 'set_model', 'model': 'claude-haiku-5-5'}),
            ('effort', {'subtype': 'apply_flag_settings', 'settings': {'effortLevel': 'low'}})
        ]:
            writer.write(host.encode({'type': 'control_request', 'request_id': request_id, 'request': request}))
            await writer.drain()
            await self.until(reader, lambda m: m.get('response', {}).get('request_id') == request_id)
        info = self.host.workers[self.session].info()
        self.assertEqual(info['model'], 'claude-haiku-5-5')
        self.assertEqual(info['effort'], 'low')


if __name__ == '__main__':
    unittest.main()
