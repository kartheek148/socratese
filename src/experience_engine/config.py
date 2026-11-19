import os

from pydantic import BaseModel, ConfigDict, SecretStr

DEFAULT_BASE_URL = "https://api.openai.com/v1"


class LLMSettings(BaseModel):
    """Settings for an OpenAI-compatible chat-completions endpoint. The key is never printed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str = DEFAULT_BASE_URL
    api_key: SecretStr | None = None
    model: str
    temperature: float = 0.0
    max_output_tokens: int = 1024
    timeout_s: float = 60.0
    max_retries: int = 2

    @classmethod
    def from_env(cls) -> "LLMSettings":
        """Reads EE_LLM_API_KEY, EE_LLM_MODEL; optional EE_LLM_BASE_URL, EE_LLM_TEMPERATURE,
        EE_LLM_MAX_OUTPUT_TOKENS."""
        missing = [n for n in ("EE_LLM_API_KEY", "EE_LLM_MODEL") if not os.environ.get(n)]
        if missing:
            raise RuntimeError(f"missing environment variables: {', '.join(missing)}")
        return cls(
            base_url=os.environ.get("EE_LLM_BASE_URL", DEFAULT_BASE_URL),
            api_key=SecretStr(os.environ["EE_LLM_API_KEY"]),
            model=os.environ["EE_LLM_MODEL"],
            temperature=float(os.environ.get("EE_LLM_TEMPERATURE", "0")),
            max_output_tokens=int(os.environ.get("EE_LLM_MAX_OUTPUT_TOKENS", "1024")),
        )
