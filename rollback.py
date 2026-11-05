#!/usr/bin/env python3
"""Restore only this experiment's editor setting, preserving unrelated edits."""
import json
from pathlib import Path

from client import SOCKET

record = json.loads((SOCKET.parent / 'editor-setting-backup.json').read_text())
target = Path(record['path'])
if not target.exists():
    print('Editor setting is already absent.')
else:
    current = json.loads(target.read_text())
    if current.get('claudeCode.claudeProcessWrapper') != record['installed_wrapper']:
        raise SystemExit('Wrapper setting changed since installation; nothing was overwritten.')
    current.pop('claudeCode.claudeProcessWrapper')
    if current:
        tmp = target.with_suffix('.claude-host-rollback.tmp')
        tmp.write_text(json.dumps(current, indent=2) + '\n')
        tmp.chmod(0o600)
        tmp.replace(target)
    elif not record['previously_existed']:
        target.unlink()
    else:
        raise SystemExit('No original contents recorded; nothing was overwritten.')
    print('Restored the original editor wrapper setting. Existing Claude workers were not stopped.')
