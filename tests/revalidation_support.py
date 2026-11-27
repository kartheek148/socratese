"""Shared helpers for the M6.3 revalidation tests."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from experience_engine.controller.drift import CellReference
from experience_engine.domain.revalidation import (
    ExecutorFacts,
    TrialKey,
    TrialResult,
    TrialVerdict,
)


class Script:
    """A TrialSource with scripted verdicts; `over[i]` overrides fields of the i-th result."""

    def __init__(
        self,
        verdict: Callable[[int], TrialVerdict] | TrialVerdict,
        *,
        tokens: int = 6000,
        runtime: str | None = "rt-1",
        verifier: str | None = "v-1",
        tenant: str = "bench",
        pool: int = 10_000,
        available: bool = True,
        supported: bool = True,
        over: dict[int, dict[str, Any]] | None = None,
    ):
        self.verdict = verdict if callable(verdict) else (lambda _n, v=verdict: v)
        self.tokens, self.runtime, self.verifier = tokens, runtime, verifier
        self.tenant, self.pool, self.available, self.supported = tenant, pool, available, supported
        self.over = over or {}
        self.calls = 0

    def facts(self, ref: CellReference, now: datetime) -> ExecutorFacts:
        return ExecutorFacts(
            tenant_id=self.tenant,
            supported=self.supported,
            runtime_version=self.runtime,
            verifier_version=self.verifier,
            verification_available=self.available,
            pool=self.pool,
        )

    def run(self, ref: CellReference, key: TrialKey, at: datetime) -> TrialResult | None:
        n = self.calls
        self.calls += 1
        fields: dict[str, Any] = {
            "verdict": self.verdict(n),
            "tokens": self.tokens,
            "task_ref": f"t{n}",
            "observed_at": at,
            "runtime_version": self.runtime,
            "verifier_version": self.verifier,
            "capability_ref": ref.alt_key,
            "tenant_id": self.tenant,
            **self.over.get(n, {}),
        }
        return TrialResult(**fields)
