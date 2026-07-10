"""Provider-configurable model client.

Selection via environment variables:
  LOOPER_PROVIDER  anthropic (default) | openai
  LOOPER_MODEL     optional model override
  ANTHROPIC_API_KEY / OPENAI_API_KEY  credentials for the chosen provider
"""

import os

from autogen_core.models import ChatCompletionClient

DEFAULT_MODELS = {
    "anthropic": "claude-opus-4-8",
    "openai": "gpt-4o",
}


def make_model_client() -> ChatCompletionClient:
    provider = os.environ.get("LOOPER_PROVIDER", "anthropic").lower()
    model = os.environ.get("LOOPER_MODEL") or DEFAULT_MODELS.get(provider)

    if provider == "anthropic":
        from autogen_ext.models.anthropic import AnthropicChatCompletionClient

        return AnthropicChatCompletionClient(model=model)
    if provider == "openai":
        from autogen_ext.models.openai import OpenAIChatCompletionClient

        return OpenAIChatCompletionClient(model=model)
    raise ValueError(
        f"Unknown LOOPER_PROVIDER={provider!r} — expected 'anthropic' or 'openai'"
    )
