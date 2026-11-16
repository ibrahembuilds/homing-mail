"""Anthropic provider, through the official SDK.

The agent keeps conversations in a provider-neutral form (see agent.py). An
assistant turn produced by this provider also carries its original content
blocks, which are replayed unchanged so thinking blocks stay valid.
"""
import json

import anthropic

from .ai_common import ProviderError

NAME = 'anthropic'
LABEL = 'Anthropic'
DEFAULT_MODEL = 'claude-opus-5-5'
NATIVE_WEB_SEARCH = True    # Anthropic runs web search itself, only when the model decides to
# Models that accept the server-side refusal fallback: when the model declines
# a request for policy reasons the API re-runs it on a fallback model.
FALLBACK_MODELS = ('claude-opus-5-5', 'claude-opus-5', 'claude-sonnet-5-5', 'claude-fable-5-2', 'claude-fable-5-1')
FALLBACK_BETA = 'server-side-fallback-2026-07-01'


def _client(api_key):
    return anthropic.Anthropic(api_key=api_key, timeout=180.0, max_retries=2)


def _messages(history):
    out = []
    for item in history:
        role, body = item['role'], item['content']
        if role == 'user':
            out.append({'role': 'user', 'content': body['text']})
        elif role == 'assistant':
            if body.get('provider') == NAME and body.get('raw'):
                out.append({'role': 'assistant', 'content': body['raw']})
                continue
            blocks = []
            if body.get('text'):
                blocks.append({'type': 'text', 'text': body['text']})
            for call in body.get('tool_calls') or []:
                blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['name'], 'input': call['input']})
            if blocks:
                out.append({'role': 'assistant', 'content': blocks})
        elif role == 'tool':
            out.append({'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': r['id'], 'content': r['output'],
                 **({'is_error': True} if r.get('is_error') else {})}
                for r in body['results']]})
    return out


def _raw_blocks(response):
    blocks = response.to_dict().get('content') or []
    cleaned = []
    for block in blocks:
        if block.get('type') == 'text':
            block = {k: v for k, v in block.items() if v is not None}
        cleaned.append(block)
    return cleaned


def _usage(response):
    usage = getattr(response, 'usage', None)
    if usage is None:
        return {'input': 0, 'output': 0}
    total_in = sum(int(getattr(usage, name, 0) or 0)
                   for name in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))
    return {'input': total_in, 'output': int(getattr(usage, 'output_tokens', 0) or 0)}


def _create(api_key, model, params):
    """One Messages API call with the refusal fallback where supported; errors
    become ProviderError with a message for the operator."""
    client = _client(api_key)
    try:
        if model in FALLBACK_MODELS:
            return client.beta.messages.create(model=model, betas=[FALLBACK_BETA], fallbacks='default', **params)
        return client.messages.create(model=model, **params)
    except anthropic.AuthenticationError:
        raise ProviderError('Anthropic rejected the API key. Check it in Settings.')
    except anthropic.PermissionDeniedError as exc:
        raise ProviderError(f'This Anthropic API key is not allowed to do that: {exc.message}')
    except anthropic.NotFoundError:
        raise ProviderError(f'Anthropic does not know the model "{model}". Pick another one in Settings.')
    except anthropic.RateLimitError:
        raise ProviderError('Anthropic rate limit reached. Wait a minute and try again.', retry=True)
    except anthropic.BadRequestError as exc:
        raise ProviderError(f'Anthropic refused the request: {exc.message}')
    except anthropic.APIStatusError as exc:
        raise ProviderError(f'Anthropic returned an error ({exc.status_code}). Try again shortly.',
                            retry=exc.status_code >= 500)
    except anthropic.APIConnectionError:
        raise ProviderError("Could not reach Anthropic. Check the server's internet connection.", retry=True)


WEB_SEARCH_TOOL = {'type': 'web_search_20260209', 'name': 'web_search', 'max_uses': 5}


def complete(api_key, model, system, history, tools, web_search=False):
    """One agent turn. Returns {'text', 'tool_calls', 'raw', 'note', 'model', 'usage', 'paused'}."""
    model = model or DEFAULT_MODEL
    tool_defs = [{'name': t['name'], 'description': t['description'], 'input_schema': t['input_schema']}
                 for t in tools]
    if web_search:
        tool_defs.append(WEB_SEARCH_TOOL)   # runs on Anthropic's side; results come back in the reply
    response = _create(api_key, model, dict(
        max_tokens=16000,
        system=system,
        tools=tool_defs,
        messages=_messages(history),
        cache_control={'type': 'ephemeral'},
    ))
    text_parts, calls = [], []
    for block in response.content:
        if block.type == 'text':
            text_parts.append(block.text)
        elif block.type == 'tool_use':
            calls.append({'id': block.id, 'name': block.name, 'input': dict(block.input or {})})
    note = ''
    if response.stop_reason == 'refusal':
        details = getattr(response, 'stop_details', None)
        reason = getattr(details, 'explanation', None) or 'no reason given'
        note = f'The model declined this request ({reason}).'
        calls = []
    elif response.stop_reason == 'max_tokens':
        note = 'The reply was cut off because it reached the length limit.'
        calls = []  # a truncated tool call must not run
    return {'text': '\n'.join(p for p in text_parts if p).strip(), 'tool_calls': calls,
            'raw': _raw_blocks(response) if response.stop_reason not in ('refusal', 'max_tokens') else None,
            'note': note, 'model': response.model, 'usage': _usage(response),
            # A long web search can pause the turn; sending the conversation again continues it.
            'paused': response.stop_reason == 'pause_turn'}


def generate_json(api_key, model, system, prompt, schema, effort='low'):
    """A single structured answer. Returns {'data': dict, 'model', 'usage'}.

    The system prompt is the part shared by many calls (a draft, the rules), so
    it carries the cache marker; the prompt holds what changes per call.
    """
    model = model or DEFAULT_MODEL
    response = _create(api_key, model, dict(
        max_tokens=16000,
        system=[{'type': 'text', 'text': system, 'cache_control': {'type': 'ephemeral'}}],
        messages=[{'role': 'user', 'content': prompt}],
        output_config={'effort': effort, 'format': {'type': 'json_schema', 'schema': schema}},
    ))
    if response.stop_reason == 'refusal':
        raise ProviderError('The model declined to write this one.')
    if response.stop_reason == 'max_tokens':
        raise ProviderError('The answer was cut off at the length limit.')
    text = next((b.text for b in response.content if b.type == 'text'), '')
    try:
        data = json.loads(text)
    except ValueError:
        raise ProviderError('The model returned something that is not valid JSON.')
    return {'data': data, 'model': response.model, 'usage': _usage(response)}


def list_models(api_key):
    """Model ids available to this key, newest first."""
    try:
        return [m.id for m in _client(api_key).models.list(limit=100)]
    except anthropic.AuthenticationError:
        raise ProviderError('Anthropic rejected the API key.')
    except anthropic.APIError as exc:
        raise ProviderError(f'Could not load the model list: {type(exc).__name__}')
