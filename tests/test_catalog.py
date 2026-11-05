import json
import os
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host import native_catalog, native_owner


class CatalogTests(unittest.TestCase):
    def test_native_owner_uses_machine_id_and_pid_namespace_not_boot_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'sessions'
            root.mkdir()
            pid = os.getpid()
            token = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
            domain = 'linux:' + Path('/etc/machine-id').read_text().strip() + ':' + os.readlink('/proc/self/ns/pid')
            session = str(uuid.uuid4())
            record = {'pid': pid, 'sessionId': session, 'procStart': token, 'pidDomain': domain}
            filename = root / f'{pid}.json'
            filename.write_text(json.dumps(record))
            with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': directory}):
                self.assertEqual(native_owner(session), pid)
                record['pidDomain'] = 'linux:another-machine:pid:[1]'
                filename.write_text(json.dumps(record))
                self.assertIsNone(native_owner(session))
                record['pidDomain'] = domain
                record['procStart'] = str(int(token) - 1)
                filename.write_text(json.dumps(record))
                self.assertIsNone(native_owner(session))

    def test_large_edge_messages_do_not_hide_unambiguous_project_session(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / 'projects' / '-tmp-project'
            project.mkdir(parents=True)
            (project / (str(uuid.uuid4())+'.jsonl')).write_text(json.dumps({'type': 'user', 'cwd': '/tmp/project'})+'\n')
            large_id = str(uuid.uuid4())
            (project / (large_id+'.jsonl')).write_text(json.dumps({'type': 'assistant', 'message': {'content': 'x'*65536}})+'\n')
            with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': directory}):
                entries = native_catalog()
            self.assertEqual(len(entries), 2)
            self.assertEqual(next(e['cwd'] for e in entries if e['session'] == large_id), '/tmp/project')

    def test_ambiguous_encoded_directory_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / 'projects' / '-a-b-c'
            project.mkdir(parents=True)
            for cwd in ['/a/b-c', '/a-b/c']:
                (project / (str(uuid.uuid4())+'.jsonl')).write_text(json.dumps({'cwd': cwd})+'\n')
            (project / (str(uuid.uuid4())+'.jsonl')).write_text(json.dumps({'type': 'assistant'})+'\n')
            with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': directory}):
                self.assertEqual(len(native_catalog()), 2)


if __name__ == '__main__':
    unittest.main()
