"""Chat model factory for the LiteLLM gateway (OpenAI-compatible ``/v1`` API).

Any provider the gateway exposes is reached by its gateway alias (``model``); nothing
provider-specific lives here. Provider-specific request tweaks go in ``LLM_PARAMS_JSON``
(for example ``{"parallel_tool_calls": false}``). If the final gateway is not
OpenAI-compatible, replace only this module.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from ..config import Settings


class GatewayNotConfigured(Exception):
    pass


class ModelNotConfigured(Exception):
    pass


def resolve_model_name(settings: Settings, requested: str | None) -> str:
    name = (requested or settings.default_model).strip()
    if not name:
        raise ModelNotConfigured
    return name


def build_model(settings: Settings, model: str, run_id: str) -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    key = settings.llm_api_key.get_secret_value()
    if not settings.llm_gateway_url or not key:
        raise GatewayNotConfigured
    params = dict(settings.llm_params)
    # Metadata only (no content): lets the gateway group one run's calls.
    extra_body = {"metadata": {"run_id": run_id}, **params.pop("extra_body", {})}
    return ChatOpenAI(
        model=model,
        base_url=settings.llm_gateway_url,
        api_key=settings.llm_api_key,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        extra_body=extra_body,
        model_kwargs=params,
    )


class _TestModel(FakeMessagesListChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


def build_test_model(prompt: str) -> BaseChatModel:
    """Offline model for smoke tests: no network, no gateway, no tools."""
    text = f"# Test report\n\nThe real Deep Agents engine ran with an offline model.\n\nPrompt length: {len(prompt)}."
    return _TestModel(responses=[AIMessage(content=text)])
