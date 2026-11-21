import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from pydantic import SecretStr

from experience_engine.adapters.llm.openai_compatible import OpenAICompatibleClient
from experience_engine.adapters.results import InMemoryResultStore, JsonlResultStore
from experience_engine.bench.harness import run_benchmark
from experience_engine.bench.report import (
    assert_no_secrets,
    compare_runs,
    render_report,
    summarize,
    wilson_interval,
)
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.config import LLMSettings
from experience_engine.domain.benchmark import DatasetVersion, ModelSettings, Split
from experience_engine.execution.controller import FixedPolicy
from experience_engine.ports.llm import ChatMessage, LLMError, LLMRequest
from support import FakeClock, ScriptedLLM, config, correct_pool_fix, first_task


def _records(store, replies_for_success: bool):  # type: ignore[no-untyped-def]
    task = first_task()
    replies = correct_pool_fix(task) if replies_for_success else [{"final": "done"}]
    return run_benchmark(
        [task],
        dataset=DatasetVersion(
            name="d", version="1", split=Split.DISCOVERY, content_hash="h" * 64, n_tasks=1
        ),
        seed=0,
        config=config(),
        llm_factory=lambda _t, _s: ScriptedLLM(list(replies)),
        controller=FixedPolicy(),
        verifier=SimulationVerifier(),
        clock=FakeClock(),
        store=store,
    )


def test_store_is_append_only_and_tenant_scoped(tmp_path: Path) -> None:
    jsonl = JsonlResultStore(tmp_path / "r.jsonl")
    _records(jsonl, True)
    _records(jsonl, False)
    runs = jsonl.list_runs("bench")
    assert len(runs) == 2 and jsonl.list_runs("other-tenant") == []
    assert InMemoryResultStore().list_runs("bench") == []


def test_report_rates_false_claims_and_has_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EE_LLM_API_KEY", "sk-test-super-secret-value-123456")
    records = _records(InMemoryResultStore(), True) + _records(InMemoryResultStore(), False)
    s = summarize(records)
    assert (s.n, s.verified, s.false_claims) == (2, 1, 1)
    text = render_report(records)
    assert "False claims" in text and "sk-test" not in text
    with pytest.raises(ValueError):
        assert_no_secrets("leak sk-test-super-secret-value-123456")
    with pytest.raises(ValueError):
        assert_no_secrets("Authorization: Bearer abcdefghijklmnop123")


def test_wilson_interval_bounds() -> None:
    assert wilson_interval(0, 0) == (0.0, 0.0)
    lo, hi = wilson_interval(5, 10)
    assert 0.2 < lo < 0.5 < hi < 0.8


def test_compare_detects_difference() -> None:
    a = _records(InMemoryResultStore(), True)
    assert compare_runs(a, _records(InMemoryResultStore(), True)).identical
    diff = compare_runs(a, _records(InMemoryResultStore(), False))
    assert not diff.identical and diff.changed


class _Handler(BaseHTTPRequestHandler):
    calls = 0
    seen_auth = ""
    fail_first = 0

    def do_POST(self) -> None:  # noqa: N802
        type(self).calls += 1
        type(self).seen_auth = self.headers.get("Authorization", "")
        self.rfile.read(int(self.headers["Content-Length"]))
        if type(self).calls <= type(self).fail_first:
            self.send_response(503)
            self.end_headers()
            return
        body = json.dumps(
            {
                "choices": [{"message": {"content": '{"final": "x"}'}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def _client(port: int, retries: int = 2) -> OpenAICompatibleClient:
    settings = LLMSettings(
        base_url=f"http://127.0.0.1:{port}/v1",
        api_key=SecretStr("k-123"),
        model="m",
        max_retries=retries,
    )
    return OpenAICompatibleClient(settings, sleep=lambda _s: None)


def _request() -> LLMRequest:
    return LLMRequest(
        messages=[ChatMessage(role="user", content="hi")],
        model=ModelSettings(provider="p", model="m"),
    )


@pytest.fixture
def server():  # type: ignore[no-untyped-def]
    _Handler.calls, _Handler.fail_first = 0, 0
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()


def test_openai_adapter_parses_usage_and_sends_bearer(server) -> None:  # type: ignore[no-untyped-def]
    resp = _client(server.server_port).complete(_request())
    assert (resp.prompt_tokens, resp.completion_tokens) == (7, 3)
    assert _Handler.seen_auth == "Bearer k-123"


def test_openai_adapter_retries_then_raises(server) -> None:  # type: ignore[no-untyped-def]
    _Handler.fail_first = 1
    assert _client(server.server_port).complete(_request()).text
    _Handler.calls, _Handler.fail_first = 0, 99
    with pytest.raises(LLMError):
        _client(server.server_port, retries=1).complete(_request())
    assert _Handler.calls == 2


def test_settings_never_print_key() -> None:
    s = LLMSettings(api_key=SecretStr("topsecret"), model="m")
    assert "topsecret" not in repr(s) and "topsecret" not in s.model_dump_json()


def test_comparison_pairs_repeated_seeds_and_flags_experimental_catalogs() -> None:
    from experience_engine.bench.report import render_comparison

    def at(seed: int, ok: bool, tags: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
        rec = _records(InMemoryResultStore(), ok)[0]
        return rec.model_copy(update={"seed": seed, "tags": {**rec.tags, **(tags or {})}})

    a = [at(0, False), at(1, True), at(2, False)]
    c = [at(0, True), at(1, True), at(2, True, {"capability_catalog": "experimental"})]
    text = render_comparison(a, c)
    # one task x three seeds is three paired samples, not one
    assert "Paired runs: 3 over 1 tasks and seeds [0, 1, 2]" in text
    assert "both succeed 1, only C 2, only A 0, neither 0" in text
    assert "EXPERIMENTAL CATALOG" in text and "## By split and variant" in text
    assert not compare_runs(a, a).changed and compare_runs(a, c).changed
