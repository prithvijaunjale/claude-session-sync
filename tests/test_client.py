import json
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import client
from client import ReplayFilter, hook_ids


class ReplayTests(unittest.TestCase):
    def test_saved_messages_are_not_appended_twice_to_native_panel(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'events.jsonl'
            events = [{'type': 'user', 'uuid': 'phone-user'},
                      {'type': 'assistant', 'uuid': 'phone-answer'},
                      {'type': 'result', 'uuid': 'completed-turn'}]
            journal.write_text(''.join(json.dumps(e)+'\n' for e in events))
            replay = ReplayFilter(journal)
            self.assertTrue(all(replay.omit(e) for e in events[:2]))
            # A real completion is still required to settle the panel's SDK
            # run state even when its visible messages came from history.
            self.assertFalse(replay.omit(events[2]))
            self.assertFalse(replay.omit({'type': 'assistant', 'uuid': 'new-answer'}))
            self.assertFalse(replay.omit({'type': 'control_request', 'request_id': 'approval'}))

    def test_partial_journal_append_does_not_hide_new_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'events.jsonl'
            journal.write_text('{"type":"assistant", "uuid":')
            self.assertFalse(ReplayFilter(journal).omit({'type': 'assistant', 'uuid': 'new'}))

    def test_only_registered_editor_callbacks_are_identified(self):
        request = {'hooks': {'PreToolUse': [{'matcher': 'Read', 'hookCallbackIds': ['autosave']}],
                             'PostToolUse': [{'hookCallbackIds': ['diagnostics']}]}}
        self.assertEqual(hook_ids(request), {'autosave', 'diagnostics'})
        self.assertEqual(hook_ids({}), set())

    def test_metadata_probe_keeps_native_behavior_without_hosting_a_session(self):
        class ExecCalled(Exception):
            pass
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'projects.json').write_text(json.dumps([str(Path.cwd())]))
            with patch.object(client, 'SOCKET', root / 'host.sock'), patch.dict(client.os.environ, {'CLAUDE_CODE_CONFIG_PROBE': '1'}), patch.object(client.os, 'execv', side_effect=ExecCalled) as execute:
                with self.assertRaises(ExecCalled):
                    client.wrapper(['/real/claude', '--input-format', 'stream-json'])
                execute.assert_called_once_with('/real/claude', ['/real/claude', '--input-format', 'stream-json'])

    def test_phone_create_inherits_parent_model_and_effort(self):
        listing = {'ok': True, 'sessions': [{'session': 'parent', 'model': 'claude-haiku-5-5', 'effort': 'low'}]}
        with patch.object(sys, 'argv', ['client.py', 'create', '--cwd', '/tmp/project', '--inherit-current']), patch.object(client, 'project_enabled', return_value=True), patch.dict(client.os.environ, {'CLAUDE_CODE_SESSION_ID': 'parent'}), patch.object(client, 'connect', side_effect=[(unittest.mock.Mock(), listing), (unittest.mock.Mock(), {'ok': True})]) as connect, contextlib.redirect_stdout(io.StringIO()):
            client.main()
        request = connect.call_args.args[0]
        self.assertEqual(request['model'], 'claude-haiku-5-5')
        self.assertEqual(request['effort'], 'low')

    def test_explicit_model_choice_wins_over_parent(self):
        listing = {'ok': True, 'sessions': [{'session': 'parent', 'model': 'other', 'effort': 'high'}]}
        with patch.object(sys, 'argv', ['client.py', 'create', '--cwd', '/tmp/project', '--inherit-current', '--model', 'claude-haiku-5-5', '--effort', 'low']), patch.object(client, 'project_enabled', return_value=True), patch.dict(client.os.environ, {'CLAUDE_CODE_SESSION_ID': 'parent'}), patch.object(client, 'connect', side_effect=[(unittest.mock.Mock(), listing), (unittest.mock.Mock(), {'ok': True})]) as connect, contextlib.redirect_stdout(io.StringIO()):
            client.main()
        request = connect.call_args.args[0]
        self.assertEqual(request['model'], 'claude-haiku-5-5')
        self.assertEqual(request['effort'], 'low')

    def test_unapproved_project_does_not_contact_host(self):
        with patch.object(sys, 'argv', ['client.py', 'create', '--cwd', '/unapproved/project']), patch.object(client, 'project_enabled', return_value=False), patch.object(client, 'connect') as connect:
            with self.assertRaisesRegex(RuntimeError, 'not enabled'):
                client.main()
            connect.assert_not_called()

    def test_restricted_project_picker_does_not_scan_nfs(self):
        with patch.object(sys, 'argv', ['client.py', 'projects']), patch.object(client, 'configured_projects', return_value=['/test/project']), patch.object(client, 'connect') as connect, contextlib.redirect_stdout(io.StringIO()) as output:
            client.main()
        self.assertEqual(json.loads(output.getvalue())['projects'], ['/test/project'])
        connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
