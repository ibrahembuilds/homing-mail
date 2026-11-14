"""Which AI provider, key and model to use for each job."""
from . import ai_anthropic, ai_openrouter
from .ai_common import ProviderError

PROVIDERS = {ai_anthropic.NAME: ai_anthropic, ai_openrouter.NAME: ai_openrouter}


class NotConfigured(Exception):
    """No usable AI key; the message tells the operator what to do."""


def configured(settings):
    provider = settings.get('ai_provider') or ai_openrouter.NAME
    return provider in PROVIDERS and bool(settings.get(f'ai_key_{provider}'))


def resolve(db, settings, purpose='agent'):
    """Returns (provider module, api key, model) for 'agent' or 'writer'.

    The writer (per-lead emails, reply sorting, reply drafts) uses its own model
    when one is set, otherwise the agent's. With OpenRouter and no model chosen,
    the newest suitable model is picked once and saved.
    """
    name = settings.get('ai_provider') or ai_openrouter.NAME
    provider = PROVIDERS.get(name)
    if provider is None:
        raise NotConfigured('Choose an AI provider in Settings.')
    api_key = settings.get(f'ai_key_{name}', '')
    if not api_key:
        raise NotConfigured(f'Add your {provider.LABEL} API key in Settings → AI.')
    model = ''
    if purpose == 'writer':
        model = (settings.get('ai_model_writer') or '').strip()
    model = model or settings.get(f'ai_model_{name}') or provider.DEFAULT_MODEL
    if not model and hasattr(provider, 'pick_default'):
        model = provider.pick_default(provider.list_models(api_key))
        if not model:
            raise NotConfigured(f'Choose a {provider.LABEL} model in Settings → AI.')
        db.set_setting(f'ai_model_{name}', model)
    return provider, api_key, model


__all__ = ['PROVIDERS', 'NotConfigured', 'ProviderError', 'configured', 'resolve']
