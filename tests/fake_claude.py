"""Deterministic native protocol fixture; never contacts Claude or uses tools."""
import json
import sys
import os


def emit(message):
    print(json.dumps(message), flush=True)


for line in sys.stdin:
    message = json.loads(line)
    kind = message.get('type')
    if kind == 'control_request':
        request = message['request']
        result = {'session_url': 'https://example.invalid/session/test',
                  'bridge_session_id': request.get('reattach_session_id', 'fake-remote')} if request['subtype'] == 'remote_control' else {'hooks_applied': True, 'pid': os.getpid(), 'cwd': os.getcwd()}
        emit({'type': 'control_response', 'response': {'subtype': 'success',
              'request_id': message['request_id'], 'response': result}})
    elif kind == 'user':
        emit({'type': 'system', 'subtype': 'init', 'session_id': message['session_id']})
        emit(message)
        if message['message']['content'] == 'hook':
            emit({'type': 'control_request', 'request_id': 'hook-1',
                  'request': {'subtype': 'hook_callback', 'callback_id': 'autosave',
                              'input': {'hook_event_name': 'PreToolUse'}}})
        elif message['message']['content'] == 'approval':
            emit({'type': 'control_request', 'request_id': 'permission-1',
                  'request': {'subtype': 'can_use_tool', 'tool_name': 'Bash',
                              'input': {'command': 'true'}}})
        else:
            emit({'type': 'assistant', 'uuid': 'answer-1', 'message': {'role': 'assistant', 'content': 'fixture answer'}})
            emit({'type': 'result', 'subtype': 'success'})
    elif kind == 'control_response':
        emit({'type': 'result', 'subtype': 'success'})
