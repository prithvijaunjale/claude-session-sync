"""Deterministic native protocol fixture; never contacts Claude or uses tools."""
import json
import sys
import os


def emit(message):
    print(json.dumps(message), flush=True)


delayed_init = None
for line in sys.stdin:
    message = json.loads(line)
    kind = message.get('type')
    if kind == 'control_request':
        request = message['request']
        result = {'session_url': 'https://example.invalid/session/test',
                  'bridge_session_id': request.get('reattach_session_id', 'fake-remote')} if request['subtype'] == 'remote_control' else {'hooks_applied': True, 'pid': os.getpid(), 'cwd': os.getcwd()}
        reply = {'type': 'control_response', 'response': {'subtype': 'success',
                 'request_id': message['request_id'], 'response': result}}
        if request['subtype'] == 'initialize' and request.get('fixture_delay_reply'):
            delayed_init = reply
        else:
            emit(reply)
    elif kind == 'user':
        if delayed_init is not None:
            emit(delayed_init)
            delayed_init = None
        session = message['session_id']
        if not message.get('fixture_continuation'):
            emit({'type': 'system', 'subtype': 'init', 'session_id': session})
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
            emit({'type': 'assistant', 'session_id': session, 'uuid': 'answer-1',
                  'message': {'role': 'assistant', 'content': 'fixture answer'}})
            emit({'type': 'result', 'session_id': session, 'subtype': 'success'})
    elif kind == 'control_response':
        emit({'type': 'result', 'subtype': 'success'})
