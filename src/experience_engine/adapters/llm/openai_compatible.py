"""Provider adapter for any OpenAI-compatible /chat/completions endpoint (stdlib HTTP only)."""

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from experience_engine.config import LLMSettings
from experience_engine.ports.llm import LLMError, LLMRequest, LLMResponse

_RETRYABLE = {408, 409, 429, 500, 502, 503, 504}


class OpenAICompatibleClient:
    def __init__(
        self,
        settings: LLMSettings,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._settings = settings
        self._sleep = sleep

    def complete(self, request: LLMRequest) -> LLMResponse:
        s = self._settings
        body: dict[str, Any] = {
            "model": request.model.model,
            "messages": [m.model_dump() for m in request.messages],
            "temperature": request.model.temperature,
            "max_tokens": request.model.max_output_tokens,
        }
        if request.model.seed is not None:
            body["seed"] = request.model.seed
        http_request = urllib.request.Request(
            f"{s.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {s.api_key.get_secret_value()}",
            },
            method="POST",
        )
        last_error = "unknown error"
        for attempt in range(s.max_retries + 1):
            try:
                with urllib.request.urlopen(http_request, timeout=s.timeout_s) as resp:  # noqa: S310
                    return self._parse(json.loads(resp.read()))
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}"
                if exc.code not in _RETRYABLE:
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = type(exc).__name__
            except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                raise LLMError(f"malformed provider response: {type(exc).__name__}") from exc
            if attempt < s.max_retries:
                self._sleep(min(2.0**attempt, 8.0))
        raise LLMError(f"provider request failed: {last_error}")

    @staticmethod
    def _parse(data: dict[str, Any]) -> LLMResponse:
        text = data["choices"][0]["message"]["content"]
        usage = data["usage"]
        if not isinstance(text, str):
            raise TypeError("content is not text")
        return LLMResponse(
            text=text,
            prompt_tokens=int(usage["prompt_tokens"]),
            completion_tokens=int(usage["completion_tokens"]),
        )
