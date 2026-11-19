"""Vertex AI via its OpenAI-compatible endpoint, authenticated with a service-account key file.

The key file is read only to mint short-lived OAuth access tokens; it is never copied, logged, or
sent anywhere except Google's token endpoint. Keep it out of git (see .gitignore).
"""

import threading
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from experience_engine.config import LLMSettings

SCOPE = "https://www.googleapis.com/auth/cloud-platform"
DEFAULT_LOCATION = "global"
# Gemini "thinking" tokens count against max_tokens, so leave generous headroom.
DEFAULT_MAX_OUTPUT_TOKENS = 4096


class VertexConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    credentials_file: Path
    project_id: str
    location: str = DEFAULT_LOCATION
    model: str = "gemini-2.5-flash"

    @property
    def provider_label(self) -> str:
        return f"vertex-ai:{self.location}"

    def settings(
        self, temperature: float = 0.0, max_output_tokens: int | None = None
    ) -> LLMSettings:
        host = (
            "aiplatform.googleapis.com"
            if self.location == "global"
            else f"{self.location}-aiplatform.googleapis.com"
        )
        base = (
            f"https://{host}/v1/projects/{self.project_id}"
            f"/locations/{self.location}/endpoints/openapi"
        )
        publisher_model = self.model if "/" in self.model else f"google/{self.model}"
        return LLMSettings(
            base_url=base,
            model=publisher_model,
            temperature=temperature,
            max_output_tokens=max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS,
            timeout_s=120.0,
        )


def vertex_config_from_env(env: dict[str, str]) -> VertexConfig:
    """EE_GCP_CREDENTIALS_FILE (required), EE_VERTEX_LOCATION, EE_LLM_MODEL; project comes from
    the key file unless EE_GCP_PROJECT is set."""
    import json

    path = env.get("EE_GCP_CREDENTIALS_FILE") or env.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not path:
        raise RuntimeError("missing environment variable: EE_GCP_CREDENTIALS_FILE")
    file = Path(path)
    if not file.is_file():
        raise RuntimeError(f"credentials file not found: {file.name}")
    project = env.get("EE_GCP_PROJECT") or json.loads(file.read_text()).get("project_id")
    if not project:
        raise RuntimeError("no project id in credentials file; set EE_GCP_PROJECT")
    return VertexConfig(
        credentials_file=file,
        project_id=project,
        location=env.get("EE_VERTEX_LOCATION", DEFAULT_LOCATION),
        model=env.get("EE_LLM_MODEL", "gemini-2.5-flash"),
    )


def service_account_token_provider(credentials_file: Path) -> Callable[[], str]:
    """Returns a thread-safe callable yielding a valid access token, refreshing on expiry."""
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(
        str(credentials_file), scopes=[SCOPE]
    )
    lock = threading.Lock()

    def token() -> str:
        with lock:
            if not creds.valid:
                creds.refresh(Request())
            return str(creds.token)

    return token
