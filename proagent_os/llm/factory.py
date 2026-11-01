from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, List, Optional

from proagent_os.config import get_settings
from proagent_os.llm.base import LLMMessage, LLMProvider, LLMResponse, ToolChoice
from proagent_os.llm.mock import MockLLMProvider
from proagent_os.llm.openai_compat import OpenAICompatProvider
from proagent_os.metering import current_tenant_id, llm_token_quantity, record_usage
from proagent_os.observability import SPAN_LLM_CHAT, span


class ObservedLLMProvider(LLMProvider):
    """Span and usage row around the configured provider. No network of its own."""

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner

    async def chat(
        self,
        messages: List[LLMMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: ToolChoice = None,
    ) -> LLMResponse:
        with span(SPAN_LLM_CHAT, {"llm.provider": type(self.inner).__name__}) as current:
            response = await self.inner.chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tools,
                tool_choice=tool_choice,
            )
            quantity = llm_token_quantity(response)
            if current is not None:
                current.set_attribute("llm.model", response.model)
                current.set_attribute("llm.total_tokens", quantity)
            tenant_id = current_tenant_id()
            if tenant_id:
                record_usage(tenant_id, "llm_tokens", quantity)
            return response


@lru_cache
def get_llm() -> LLMProvider:
    settings = get_settings()
    if settings.llm_provider == "openai":
        if not settings.llm_api_key:
            # Fall back to mock when key missing so boot never hard-fails
            inner: LLMProvider = MockLLMProvider()
        else:
            inner = OpenAICompatProvider(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                model=settings.llm_model,
            )
    else:
        inner = MockLLMProvider()
    return ObservedLLMProvider(inner)


def reset_llm_cache() -> None:
    get_llm.cache_clear()
