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
        token_provider: Callable[[], str] | None = None,
    ):
        self._settings = settings
        self._sleep = sleep
        self._token_provider = token_provider
        if token_provider is None and settings.api_key is None:
            raise ValueError("an api_key or a token_provider is required")

    def _bearer(self) -> str:
        if self._token_provider is not None:
            try:
                return self._token_provider()
            except (
                Exception
            ) as exc:  # auth problems are infrastructure failures, never agent failures
                raise LLMError(f"could not obtain access token: {type(exc).__name__}") from exc
        assert self._settings.api_key is not None
        return self._settings.api_key.get_secret_value()

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
        payload = json.dumps(body).encode()
        last_error = "unknown error"
        for attempt in range(s.max_retries + 1):
            # Built per attempt so a refreshed/rotated token is always used.
            http_request = urllib.request.Request(
                f"{s.base_url.rstrip('/')}/chat/completions",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._bearer()}",
                },
                method="POST",
            )
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
        prompt = int(usage["prompt_tokens"])
        completion = int(usage["completion_tokens"])
        # Some providers (e.g. Gemini "thinking") bill reasoning tokens that are not part of
        # completion_tokens; total_tokens is the billed figure, so never under-count.
        total = usage.get("total_tokens")
        if isinstance(total, int) and total - prompt > completion:
            completion = total - prompt
        return LLMResponse(text=text, prompt_tokens=prompt, completion_tokens=completion)
