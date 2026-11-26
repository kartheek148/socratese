"""Persistent cache of baseline (control-arm) outcomes shared across evaluation sessions.

A recorded run is reused only when everything that determines it is identical: the task content,
the dataset it came from, the repetition, the model configuration, the prompt/runtime/verifier/
environment versions and the agent configuration. Any difference is a miss, never a stale hit.

Independence caveat (documented in the M5.9 design): the provider is stochastic, so a cached run is
ONE draw. Every asset compared against it shares that draw; each paired comparison stays valid,
but their baselines are not independent of each other, and a lucky draw stays lucky.
"""

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from experience_engine.bench.dataset import BenchTask
from experience_engine.evaluation.arms import ArmOutcome


@dataclass(frozen=True)
class BaselineIdentity:
    model_settings: dict[str, object]
    prompt_version: str
    runtime_version: str
    verifier_version: str
    environment_version: str
    agent_config_id: str


def _canonical(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def task_fingerprint(bench: BenchTask) -> str:
    """Hash of everything about the task a run can depend on (spec and environment files)."""
    payload = {
        "task": bench.task.model_dump(mode="json"),
        "files": dict(sorted(bench.environment.files.items())),
        "family": bench.family,
        "variant": bench.variant,
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()


class BaselineStore:
    def __init__(self, root: Path, identity: BaselineIdentity):
        self._root = root
        self._identity = identity
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.puts = 0
        self.corrupt = 0
        self.tokens_avoided = 0  # tokens of runs served from disk, as recorded when computed
        root.mkdir(parents=True, exist_ok=True)

    def key(self, bench: BenchTask, seed: int, dataset_hash: str) -> str:
        return hashlib.sha256(
            _canonical(
                {
                    "identity": asdict(self._identity),
                    "task": task_fingerprint(bench),
                    "task_id": bench.task.task_id,
                    "dataset": dataset_hash,
                    "seed": seed,
                }
            )
        ).hexdigest()

    def _path(self, key: str) -> Path:
        return self._root / key[:2] / f"{key}.json"

    def get(self, key: str) -> ArmOutcome | None:
        path = self._path(key)
        try:
            data = json.loads(path.read_text())
            outcome = ArmOutcome(**data["outcome"])
            if data.get("key") != key:
                raise ValueError("key mismatch")
        except FileNotFoundError:
            with self._lock:
                self.misses += 1
            return None
        except (ValueError, TypeError, KeyError):
            with self._lock:  # an unreadable entry is a miss, and is overwritten by the recompute
                self.misses += 1
                self.corrupt += 1
            return None
        with self._lock:
            self.hits += 1
            self.tokens_avoided += outcome.tokens
        return outcome

    def put(self, key: str, outcome: ArmOutcome) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = json.dumps({"key": key, "outcome": asdict(outcome)}, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(blob)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        with self._lock:
            self.puts += 1

    @property
    def hit_rate(self) -> float | None:
        total = self.hits + self.misses
        return self.hits / total if total else None


def make_baseline_store(
    root: Path, *, model_settings: dict[str, object], verifier_version: str
) -> BaselineStore:
    """Store for the standard baseline agent. The provider ignores `seed`, so it is not part of the
    identity (the repetition index already is); every other setting is."""
    from experience_engine import RUNTIME_VERSION
    from experience_engine.bench.environment import ENVIRONMENT_VERSION
    from experience_engine.execution.protocol import PROMPT_VERSION

    return BaselineStore(
        root,
        BaselineIdentity(
            model_settings={k: v for k, v in sorted(model_settings.items()) if k != "seed"},
            prompt_version=PROMPT_VERSION,
            runtime_version=RUNTIME_VERSION,
            verifier_version=verifier_version,
            environment_version=ENVIRONMENT_VERSION,
            agent_config_id="baseline-agent",
        ),
    )
