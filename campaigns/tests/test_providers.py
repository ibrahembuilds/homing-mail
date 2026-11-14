"""Checks the real provider code paths (Anthropic SDK, OpenRouter HTTP API)
against a local stand-in server: what is sent, and how replies are read."""
import http.server
import json
import threading

import anthropic
import pytest

from app import agent, ai_anthropic, ai_openrouter, create_app
from app.ai_common import ProviderError
from app.config import Config

needs_sdk2 = pytest.mark.skipif(int(anthropic.__version__.split('.')[0]) < 2,
                                reason='needs the anthropic SDK from requirements.txt (2.x)')


class FakeAPI:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _serve(self):
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                outer.requests.append({'path': self.path, 'headers': {k.lower(): v for k, v in self.headers.items()},
                                       'body': body})
                status, payload = outer.replies.pop(0)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            do_POST = do_GET = _serve

            def log_message(self, *args):
                pass
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def app_env(tmp_path):
    cfg = Config({'CAMPAIGNS_DATA': str(tmp_path), 'CAMPAIGNS_ADMIN_PASSWORD': 'x', 'PUBLIC_URL': 'https://m.example.com',
                  'CAMPAIGNS_RSPAMD_URL': 'http://127.0.0.1:9', 'BRAND_NAME': 'TestBrand'})
    app = create_app(cfg)
    yield app
    app.db.close()


def claude_message(content, stop_reason):
    return 200, {'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': 'claude-opus-5-5',
                 'content': content, 'stop_reason': stop_reason, 'stop_sequence': None,
                 'usage': {'input_tokens': 10, 'output_tokens': 5}}


@needs_sdk2
def test_anthropic_tool_loop_request_shape(app_env, monkeypatch):
    first = [{'type': 'thinking', 'thinking': '', 'signature': 'c2lnbmF0dXJl'},
             {'type': 'text', 'text': 'Let me look.'},
             {'type': 'tool_use', 'id': 'toolu_1', 'name': 'get_overview', 'input': {}}]
    api = FakeAPI([claude_message(first, 'tool_use'),
                   claude_message([{'type': 'text', 'text': 'You have no lists yet.'}], 'end_turn')])
    monkeypatch.setenv('ANTHROPIC_BASE_URL', api.url)
    db = app_env.db
    db.set_setting('ai_provider', 'anthropic')
    db.set_setting('ai_key_anthropic', 'sk-ant-test')
    try:
        new = agent.reply(db, app_env.cfg, agent.new_thread(db), 'What do I have?')
    finally:
        api.close()
    assert new[-1]['content']['text'] == 'You have no lists yet.'
    assert json.loads(new[2]['content']['results'][0]['output'])['brand'] == 'TestBrand'

    one, two = api.requests
    assert one['path'].startswith('/v1/messages')
    assert 'sk-ant-test' in (one['headers'].get('x-api-key') or one['headers'].get('authorization'))
    assert ai_anthropic.FALLBACK_BETA in one['headers']['anthropic-beta']
    body = one['body']
    assert body['model'] == 'claude-opus-5-5' and body['max_tokens'] == 16000
    assert body['fallbacks'] == 'default' and body['cache_control'] == {'type': 'ephemeral'}
    assert body['system'] == agent.SYSTEM and 'thinking' not in body and 'temperature' not in body
    assert [t['name'] for t in body['tools']] == [t['name'] for t in agent.TOOLS] + ['web_search']
    assert body['tools'][-1]['type'] == 'web_search_20260209'      # server-side web search, on by default
    assert body['messages'] == [{'role': 'user', 'content': 'What do I have?'}]
    # Second request: the assistant turn is replayed exactly, thinking block included.
    replayed = two['body']['messages']
    assert replayed[1] == {'role': 'assistant', 'content': first}
    assert replayed[2]['content'][0]['type'] == 'tool_result' and replayed[2]['content'][0]['tool_use_id'] == 'toolu_1'


@needs_sdk2
def test_anthropic_errors_and_refusals_are_explained(app_env, monkeypatch):
    api = FakeAPI([
        (401, {'type': 'error', 'error': {'type': 'authentication_error', 'message': 'invalid x-api-key'}}),
        claude_message([], 'refusal'),
        (200, {'data': [{'id': 'claude-opus-5-5', 'type': 'model', 'display_name': 'Opus', 'created_at': '2026-01-01T00:00:00Z'}],
               'has_more': False, 'first_id': 'claude-opus-5-5', 'last_id': 'claude-opus-5-5'}),
    ])
    monkeypatch.setenv('ANTHROPIC_BASE_URL', api.url)
    history = [{'role': 'user', 'content': {'text': 'hi'}}]
    try:
        with pytest.raises(ProviderError, match='rejected the API key'):
            ai_anthropic.complete('bad', '', 'sys', history, agent.TOOLS)
        refused = ai_anthropic.complete('k', 'claude-opus-4-8', 'sys', history, agent.TOOLS)
        assert 'declined' in refused['note'] and refused['tool_calls'] == [] and refused['raw'] is None
        assert 'fallbacks' not in api.requests[-1]['body']          # only sent to models that accept it
        assert ai_anthropic.list_models('k') == ['claude-opus-5-5']
    finally:
        api.close()


def test_openrouter_tool_loop_request_shape(app_env, monkeypatch):
    api = FakeAPI([
        (200, {'data': [{'id': 'openai/gpt-x', 'created': 9, 'supported_parameters': ['tools']},
                        {'id': 'anthropic/claude-opus-9', 'created': 5, 'supported_parameters': ['tools']},
                        {'id': 'anthropic/claude-old', 'created': 1, 'supported_parameters': []}]}),
        (200, {'model': 'anthropic/claude-opus-9', 'choices': [{'finish_reason': 'tool_calls', 'message': {
            'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'call_1', 'type': 'function', 'function': {'name': 'list_contact_lists', 'arguments': '{}'}}]}}]}),
        (200, {'model': 'anthropic/claude-opus-9', 'choices': [{'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': 'No lists yet.'}}]}),
        (402, {'error': {'message': 'Insufficient credits'}}),
    ])
    monkeypatch.setattr(ai_openrouter, 'API', api.url)
    db = app_env.db
    db.set_setting('ai_provider', 'openrouter')
    db.set_setting('ai_key_openrouter', 'sk-or-test')
    try:
        thread = agent.new_thread(db)
        new = agent.reply(db, app_env.cfg, thread, 'Which lists do I have?')
        with pytest.raises(ProviderError, match='out of credits'):
            agent.reply(db, app_env.cfg, thread, 'again')
    finally:
        api.close()
    assert new[-1]['content']['text'] == 'No lists yet.' and new[1]['content']['model'] == 'anthropic/claude-opus-9'
    assert db.setting('ai_model_openrouter') == 'anthropic/claude-opus-9'      # chosen once, then remembered
    models, one, two, _ = api.requests[0], api.requests[1], api.requests[2], api.requests[3]
    assert models['path'] == '/models' and one['path'] == '/chat/completions'
    assert one['headers']['authorization'] == 'Bearer sk-or-test'
    assert one['body']['model'] == 'anthropic/claude-opus-9'           # newest Claude with tool support
    assert one['body']['messages'][0] == {'role': 'system', 'content': agent.SYSTEM}
    assert one['body']['tools'][0]['type'] == 'function' and 'parameters' in one['body']['tools'][0]['function']
    sent = two['body']['messages']
    assert sent[2]['tool_calls'][0]['id'] == 'call_1' and sent[3] == {'role': 'tool', 'tool_call_id': 'call_1', 'content': '[]'}
    # The failed turn left the conversation usable: the user message is kept.
    assert agent.history(db, thread)[-1]['role'] == 'user'
