import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from experience_engine.adapters.llm.openai_compatible import OpenAICompatibleClient
from experience_engine.adapters.llm.vertex import VertexConfig, vertex_config_from_env
from experience_engine.cli import main
from experience_engine.config import LLMSettings
from experience_engine.domain.benchmark import ModelSettings
from experience_engine.ports.llm import ChatMessage, LLMError, LLMRequest


class _Handler(BaseHTTPRequestHandler):
    auth_headers: list[str] = []
    fail_first = 0
    usage: dict[str, int] = {"prompt_tokens": 7, "completion_tokens": 3}

    def do_POST(self) -> None:  # noqa: N802
        cls = type(self)
        cls.auth_headers.append(self.headers.get("Authorization", ""))
        self.rfile.read(int(self.headers["Content-Length"]))
        if len(cls.auth_headers) <= cls.fail_first:
            self.send_response(503)
            self.end_headers()
            return
        body = json.dumps(
            {"choices": [{"message": {"content": '{"final": "x"}'}}], "usage": cls.usage}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server():  # type: ignore[no-untyped-def]
    _Handler.auth_headers = []
    _Handler.fail_first = 0
    _Handler.usage = {"prompt_tokens": 7, "completion_tokens": 3}
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()


def _client(port: int, provider, retries: int = 2) -> OpenAICompatibleClient:  # type: ignore[no-untyped-def]
    settings = LLMSettings(base_url=f"http://127.0.0.1:{port}/v1", model="m", max_retries=retries)
    return OpenAICompatibleClient(settings, sleep=lambda _s: None, token_provider=provider)


def _request() -> LLMRequest:
    return LLMRequest(
        messages=[ChatMessage(role="user", content="hi")],
        model=ModelSettings(provider="p", model="m"),
    )


def test_token_provider_is_consulted_on_every_attempt(server) -> None:  # type: ignore[no-untyped-def]
    tokens = iter(["tok-1", "tok-2"])
    _Handler.fail_first = 1
    _client(server.server_port, lambda: next(tokens)).complete(_request())
    assert _Handler.auth_headers == ["Bearer tok-1", "Bearer tok-2"]


def test_token_failure_is_an_llm_error_without_leaking_details(server) -> None:  # type: ignore[no-untyped-def]
    def boom() -> str:
        raise RuntimeError("private-key-material-here")

    with pytest.raises(LLMError) as err:
        _client(server.server_port, boom).complete(_request())
    assert "private-key" not in str(err.value) and _Handler.auth_headers == []


def test_client_requires_some_credential() -> None:
    with pytest.raises(ValueError):
        OpenAICompatibleClient(LLMSettings(model="m"))


def test_reasoning_tokens_are_counted_in_completion_tokens(server) -> None:  # type: ignore[no-untyped-def]
    _Handler.usage = {"prompt_tokens": 7, "completion_tokens": 10, "total_tokens": 40}
    resp = _client(server.server_port, lambda: "t").complete(_request())
    assert (resp.prompt_tokens, resp.completion_tokens) == (7, 33)  # 23 hidden reasoning tokens
    _Handler.usage = {"prompt_tokens": 7, "completion_tokens": 10, "total_tokens": 17}
    resp = _client(server.server_port, lambda: "t").complete(_request())
    assert resp.completion_tokens == 10


def _key_file(tmp_path: Path) -> Path:
    path = tmp_path / "sa.json"
    path.write_text(json.dumps({"type": "service_account", "project_id": "demo-proj"}))
    return path


def test_vertex_config_builds_endpoint_and_hides_project_in_label(tmp_path: Path) -> None:
    env = {"EE_GCP_CREDENTIALS_FILE": str(_key_file(tmp_path)), "EE_LLM_MODEL": "gemini-2.5-flash"}
    cfg = vertex_config_from_env(env)
    s = cfg.settings()
    assert s.base_url == (
        "https://aiplatform.googleapis.com/v1/projects/demo-proj/locations/global/endpoints/openapi"
    )
    assert s.model == "google/gemini-2.5-flash" and s.api_key is None
    assert "demo-proj" not in cfg.provider_label and cfg.provider_label == "vertex-ai:global"
    regional = VertexConfig(
        credentials_file=cfg.credentials_file, project_id="p", location="us-central1"
    ).settings()
    assert regional.base_url.startswith("https://us-central1-aiplatform.googleapis.com/")


def test_vertex_config_errors_are_clear(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="EE_GCP_CREDENTIALS_FILE"):
        vertex_config_from_env({})
    with pytest.raises(RuntimeError, match="not found"):
        vertex_config_from_env({"EE_GCP_CREDENTIALS_FILE": str(tmp_path / "missing.json")})


def test_cli_vertex_without_credentials_exits_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EE_EVALUATOR_ROLE", "evaluator")
    for name in ("EE_GCP_CREDENTIALS_FILE", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(name, raising=False)
    assert (
        main(["run", "--llm", "vertex", "--split", "discovery", "--out", str(tmp_path / "o")]) == 2
    )
