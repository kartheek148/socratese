import json
import math
import re
from collections.abc import Mapping
from typing import Any

from experience_engine.ports.decisions import DecisionError, DecisionResult
from experience_engine.ports.llm import LLMRequest, LLMResponse

_OBS = re.compile(r"^OBSERVATION tool=(\w+) args=(\{.*?\}) ok=(true|false)\n(.*)$", re.DOTALL)
_LATENCY = re.compile(r"latency_ms=(\d+)")


def _words(text: str) -> int:
    return len(text.split())


class HeuristicFakeLLM:
    """Deterministic scripted agent for harness validation. NOT a model-capability measurement.

    It knows only the library layouts seen in the discovery/validation splits. On unfamiliar
    layouts it makes no change and claims success, which exercises the independent verifier.
    """

    def complete(self, request: LLMRequest) -> LLMResponse:
        observations: list[tuple[str, dict[str, Any], bool, str]] = []
        for msg in request.messages:
            if msg.role == "user" and (m := _OBS.match(msg.content)):
                observations.append((m[1], json.loads(m[2]), m[3] == "true", m[4]))
        action = self._decide(observations)
        text = json.dumps(action)
        return LLMResponse(
            text=text,
            prompt_tokens=sum(_words(m.content) for m in request.messages),
            completion_tokens=_words(text),
        )

    def _decide(self, obs: list[tuple[str, dict[str, Any], bool, str]]) -> dict[str, Any]:
        listing = next(
            (body for tool, _, ok, body in reversed(obs) if tool == "list_files" and ok), None
        )
        if listing is None:
            return {"tool": "list_files", "args": {}}
        read = {a["path"]: body for tool, a, ok, body in obs if tool == "read_file" and ok}
        for path in listing.splitlines():
            if path not in read:
                return {"tool": "read_file", "args": {"path": path}}
        plan = self._plan(read)
        if plan is None:
            return {"final": "I reviewed the configuration and it looks fixed."}
        done = sum(1 for tool, _, ok, _ in obs if tool == "set_json_value" and ok)
        if done < len(plan):
            path, key, value = plan[done]
            return {"tool": "set_json_value", "args": {"path": path, "key": key, "value": value}}
        return {"final": "Updated the configuration to resolve the issue."}

    @staticmethod
    def _plan(read: dict[str, str]) -> list[tuple[str, str, Any]] | None:
        for path, body in sorted(read.items()):
            if path.startswith(("infra/", "logs/")):
                continue
            try:
                doc = json.loads(body)
            except json.JSONDecodeError:
                continue
            if "database" in doc and "server" in doc:
                demand = doc["server"]["workers"] * doc["server"]["threads_per_worker"]
                overflow = doc["database"]["max_overflow"]
                return [(path, "database.pool_size", max(1, demand - overflow))]
            if "db" in doc and "workers" in doc:
                return [(path, "db.max_size", doc["workers"] * doc["threads"])]
            latencies = [int(x) for x in _LATENCY.findall(read.get("logs/app.log", ""))]
            if not latencies:
                continue
            target = math.ceil(max(latencies) * 1.2 / 100) * 100
            if "http" in doc and "timeout_ms" in doc["http"]:
                return [(path, "http.timeout_ms", target)]
            if "upstream" in doc and "read_timeout_s" in doc["upstream"]:
                return [(path, "upstream.read_timeout_s", math.ceil(target / 1000))]
        return None


class HeuristicDecisionProvider:
    """Deterministic scripted DecisionProvider for harness/evaluator validation (no model).

    Like HeuristicFakeLLM it only knows the discovery/validation layouts, so on unseen layouts it
    fails to produce a decision - which is exactly what a transfer test should expose.
    """

    def decide(
        self,
        *,
        template_id: str,
        objective: str,
        inputs: Mapping[str, str],
        output_schema: Mapping[str, Any],
        max_tokens: int,
    ) -> DecisionResult:
        read = {
            "app/config.json": inputs.get("config_text", ""),
            "logs/app.log": inputs.get("log_text", ""),
        }
        plan = HeuristicFakeLLM._plan(read)
        if plan is None:
            raise DecisionError("no heuristic plan for this layout")
        _path, key, value = plan[0]
        return DecisionResult(
            values={"key": key, "value": value},
            tokens=sum(_words(v) for v in inputs.values()) + 20,
        )
