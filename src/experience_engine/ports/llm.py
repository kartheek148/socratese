from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from experience_engine.domain.benchmark import ModelSettings


class ChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str


class LLMRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: list[ChatMessage]
    model: ModelSettings


class LLMResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    prompt_tokens: int
    completion_tokens: int


class LLMError(Exception):
    """Infrastructure failure (network, auth, rate limit, malformed provider reply)."""


class LLMClient(Protocol):
    def complete(self, request: LLMRequest) -> LLMResponse: ...
