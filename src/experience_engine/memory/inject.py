"""Prompt augmentation shared by the memory arms (one mechanism, so arms differ only in content)."""

from experience_engine.memory.builders import estimate_tokens
from experience_engine.ports.llm import ChatMessage, LLMClient, LLMRequest, LLMResponse


class MemoryAugmentedLLM:
    """Appends retrieved experience to the system prompt of every call. Its tokens are charged by
    the provider like any other prompt token, so lifecycle numbers include them."""

    def __init__(self, inner: LLMClient, text: str):
        self._inner = inner
        self._text = text
        self.first_prompt_tokens = 0  # estimated context size at the first call

    def complete(self, request: LLMRequest) -> LLMResponse:
        messages = list(request.messages)
        if self._text and messages and messages[0].role == "system":
            merged = f"{messages[0].content}\n\n{self._text}"
            messages[0] = ChatMessage(role="system", content=merged)
        if not self.first_prompt_tokens:
            self.first_prompt_tokens = estimate_tokens("".join(m.content for m in messages))
        return self._inner.complete(request.model_copy(update={"messages": messages}))
