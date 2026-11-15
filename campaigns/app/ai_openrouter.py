"""OpenRouter provider for the agent (its chat-completions HTTP API).

OpenRouter fronts many model vendors; the operator picks the model in
Settings from the list OpenRouter itself returns.
"""
import json

import requests

from .ai_common import ProviderError

NAME = 'openrouter'
LABEL = 'OpenRouter'
API = 'https://openrouter.ai/api/v1'
DEFAULT_MODEL = ''  # chosen from the live model list, see pick_default()
NATIVE_WEB_SEARCH = False   # the agent gets a web_search tool that calls search() instead


def _headers(api_key):
    return {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}


def _messages(system, history):
    out = [{'role': 'system', 'content': system}]
    for item in history:
        role, body = item['role'], item['content']
        if role == 'user':
            out.append({'role': 'user', 'content': body['text']})
        elif role == 'assistant':
            message = {'role': 'assistant', 'content': body.get('text') or None}
            calls = body.get('tool_calls') or []
            if calls:
                message['tool_calls'] = [
                    {'id': c['id'], 'type': 'function',
                     'function': {'name': c['name'], 'arguments': json.dumps(c['input'])}} for c in calls]
            if message['content'] is None and not calls:
                continue
            out.append(message)
        elif role == 'tool':
            for result in body['results']:
                out.append({'role': 'tool', 'tool_call_id': result['id'], 'content': result['output']})
    return out


def _error_text(reply):
    try:
        error = reply.json().get('error') or {}
        return str(error.get('message') or '')[:300]
    except ValueError:
        return ''


def _post(api_key, body):
    """POST /chat/completions; returns the decoded reply or raises ProviderError."""
    try:
        reply = requests.post(f'{API}/chat/completions', headers=_headers(api_key), json=body, timeout=180)
    except requests.RequestException:
        raise ProviderError("Could not reach OpenRouter. Check the server's internet connection.", retry=True)
    if reply.status_code == 401:
        raise ProviderError('OpenRouter rejected the API key. Check it in Settings.')
    if reply.status_code == 402:
        raise ProviderError('The OpenRouter account is out of credits.')
    if reply.status_code == 429:
        raise ProviderError('OpenRouter rate limit reached. Wait a minute and try again.', retry=True)
    if reply.status_code != 200:
        raise ProviderError(f'OpenRouter returned an error ({reply.status_code}): {_error_text(reply)}',
                            retry=reply.status_code >= 500)
    try:
        data = reply.json()
    except ValueError:
        raise ProviderError('OpenRouter sent a reply that could not be read.', retry=True)
    if data.get('error'):
        raise ProviderError(f'OpenRouter error: {str(data["error"].get("message", ""))[:300]}')
    if not data.get('choices'):
        raise ProviderError('OpenRouter returned no answer.', retry=True)
    return data


def _usage(data):
    usage = data.get('usage') or {}
    return {'input': int(usage.get('prompt_tokens') or 0), 'output': int(usage.get('completion_tokens') or 0)}


def _content(message):
    content = message.get('content')
    if isinstance(content, list):
        content = '\n'.join(part.get('text', '') for part in content if isinstance(part, dict))
    return (content or '').strip()


def complete(api_key, model, system, history, tools, web_search=False):
    """One agent turn. Returns {'text', 'tool_calls', 'raw', 'note', 'model', 'usage'}.

    web_search is ignored here: OpenRouter's search plugin would run on every call, so
    the agent gets a web_search tool instead (see search()) and searches only when it
    needs to."""
    if not model:
        raise ProviderError('Choose an OpenRouter model in Settings first.')
    body = {
        'model': model,
        'max_tokens': 16000,
        'messages': _messages(system, history),
        'tools': [{'type': 'function', 'function': {'name': t['name'], 'description': t['description'],
                                                    'parameters': t['input_schema']}} for t in tools],
    }
    data = _post(api_key, body)
    choice = data['choices'][0]
    message = choice.get('message') or {}
    calls, note = [], ''
    truncated = choice.get('finish_reason') == 'length'
    for call in message.get('tool_calls') or []:
        function = call.get('function') or {}
        try:
            arguments = json.loads(function.get('arguments') or '{}')
        except ValueError:
            arguments = None
        if not isinstance(arguments, dict):
            truncated = True
            continue
        calls.append({'id': call.get('id') or f'call_{len(calls)}', 'name': function.get('name', ''),
                      'input': arguments})
    if truncated:
        note = 'The reply was cut off because it reached the length limit.'
        calls = []
    text = _content(message)
    sources = []
    for note_item in message.get('annotations') or []:
        cite = note_item.get('url_citation') if isinstance(note_item, dict) else None
        if cite and cite.get('url') and cite['url'] not in sources:
            sources.append(cite['url'])
    if sources and text:
        text += '\n\nSources: ' + ', '.join(sources[:6])
    return {'text': text, 'tool_calls': calls, 'raw': None, 'note': note,
            'model': data.get('model') or model, 'usage': _usage(data)}


def search(api_key, model, query, max_results=6):
    """One web search through OpenRouter's search plugin, run only when the agent asks.
    Returns {'query', 'answer', 'results': [{'title', 'url', 'snippet'}]}."""
    query = ' '.join(str(query or '').split())[:300]
    if not query:
        raise ProviderError('Give something to search for.')
    body = {
        'model': model,
        'max_tokens': 900,
        'messages': [{'role': 'user', 'content':
                      f'Search the web for: {query}\n\nList the most useful results. For each: the title, the web '
                      'address and one plain sentence on what it says that is relevant. No introduction.'}],
        'plugins': [{'id': 'web', 'max_results': max(1, min(int(max_results), 10))}],
    }
    data = _post(api_key, body)
    message = data['choices'][0].get('message') or {}
    results, seen = [], set()
    for item in message.get('annotations') or []:
        cite = item.get('url_citation') if isinstance(item, dict) else None
        if not cite or not cite.get('url') or cite['url'] in seen:
            continue
        seen.add(cite['url'])
        results.append({'title': str(cite.get('title') or '')[:200], 'url': cite['url'][:500],
                        'snippet': ' '.join(str(cite.get('content') or '').split())[:400]})
    return {'query': query, 'answer': _content(message)[:3000], 'results': results[:10], 'usage': _usage(data)}


def _parse_json(text):
    """JSON from a model answer, tolerating code fences or text around it."""
    text = text.strip()
    if text.startswith('```'):
        text = text.strip('`')
        text = text[text.find('\n') + 1:] if '\n' in text else text
    try:
        return json.loads(text)
    except ValueError:
        first, last = text.find('{'), text.rfind('}')
        if first != -1 and last > first:
            return json.loads(text[first:last + 1])
        raise


def generate_json(api_key, model, system, prompt, schema, effort='low'):
    """A single structured answer. Returns {'data': dict, 'model', 'usage'}."""
    if not model:
        raise ProviderError('Choose an OpenRouter model in Settings first.')
    body = {
        'model': model,
        'max_tokens': 8000,
        'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': prompt}],
        # Models that support structured output follow the schema exactly; the
        # prompt also describes it for those that do not.
        'response_format': {'type': 'json_schema', 'json_schema': {'name': 'answer', 'strict': True, 'schema': schema}},
        'reasoning': {'effort': effort},
    }
    try:
        data = _post(api_key, body)
    except ProviderError as exc:
        # Some models reject structured output; ask once more without it.
        text = str(exc)
        if exc.retry or 'API key' in text or 'credits' in text:
            raise
        body.pop('response_format')
        data = _post(api_key, body)
    choice = data['choices'][0]
    if choice.get('finish_reason') == 'length':
        raise ProviderError('The answer was cut off at the length limit.')
    try:
        parsed = _parse_json(_content(choice.get('message') or {}))
    except ValueError:
        raise ProviderError('The model returned something that is not valid JSON.')
    if not isinstance(parsed, dict):
        raise ProviderError('The model returned something that is not a JSON object.')
    return {'data': parsed, 'model': data.get('model') or model, 'usage': _usage(data)}


# Good value picks, cheapest first within each use (OpenRouter prices, November 2026).
# Shown only when the live list still has them.
RECOMMENDED = {
    'agent': ('anthropic/claude-haiku-5.5', 'openai/gpt-6-luna', 'google/gemini-3.1-flash-lite',
              'qwen/qwen3.8-flash'),
    'writer': ('deepseek/deepseek-v4.1-flash', 'openai/gpt-5-nano', 'google/gemini-2.5-flash-lite',
               'mistralai/mistral-small-2603'),
}


def _fetch_models(api_key):
    try:
        reply = requests.get(f'{API}/models', headers=_headers(api_key) if api_key else {}, timeout=20)
        reply.raise_for_status()
        models = reply.json().get('data') or []
    except (requests.RequestException, ValueError):
        raise ProviderError('Could not load the model list from OpenRouter.')
    usable = [m for m in models if m.get('id') and 'tools' in (m.get('supported_parameters') or [])]
    usable.sort(key=lambda m: m.get('created') or 0, reverse=True)
    return usable


def _per_million(value):
    """Price per million tokens from OpenRouter's per-token string; None when variable."""
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return None if price < 0 else round(price * 1_000_000, 4)


def list_models(api_key):
    """Ids of models that support tool calling, newest first."""
    return [m['id'] for m in _fetch_models(api_key)]


def model_catalog(api_key):
    """Tool-capable models with prices: [{'id', 'name', 'input', 'output', 'context', 'free'}],
    newest first, plus the recommended picks that are on the list."""
    models = []
    for m in _fetch_models(api_key):
        pricing = m.get('pricing') or {}
        price_in, price_out = _per_million(pricing.get('prompt')), _per_million(pricing.get('completion'))
        models.append({'id': m['id'], 'name': m.get('name') or m['id'], 'input': price_in, 'output': price_out,
                       'context': m.get('context_length') or 0,
                       'free': price_in == 0 and price_out == 0})
    by_id = {m['id']: m for m in models}
    recommended = {use: [by_id[i] for i in ids if i in by_id] for use, ids in RECOMMENDED.items()}
    return models, recommended


def pick_default(model_ids):
    """Newest Claude model on the list, preferring the larger tiers."""
    claude = [m for m in model_ids if m.startswith('anthropic/claude')]
    for tier in ('opus', 'sonnet'):
        for model in claude:
            if tier in model:
                return model
    return (claude or model_ids or [''])[0]
