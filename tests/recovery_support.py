"""Shared helpers for the M6.4 Phase 1 recovery-evidence tests."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from experience_engine.controller.drift import CellReference
from experience_engine.domain.recovery import (
    RecoveryPolicy,
    TimeoutOrigin,
    TrialProvenance,
    VerificationStatus,
)
from experience_engine.domain.revalidation import (
    ExecutorFacts,
    TrialKey,
    TrialResult,
    TrialVerdict,
)
from revalidation_support import Script

POLICY = RecoveryPolicy(
    accepted_executors=("exec-1",),
    accepted_models={"model-a": ("1",)},
    validated_runtimes=("rt-1",),
    accepted_tools={"sql": ("2",)},
    accepted_verifiers={"verifier-x": ("v-1",)},
    accepted_sources=("revalidation-pool",),
)

GOOD: dict[str, Any] = {
    "executor_id": "exec-1",
    "model_id": "model-a",
    "model_version": "1",
    "tool_versions": {"sql": "2"},
    "verifier_id": "verifier-x",
    "verifier_independent": True,
    "evidence_source": "revalidation-pool",
    "pool_id": "pool-1",
    "task_family": "timeout",
    "risk_level": "low",
}


def provenance(n: int, status: VerificationStatus | None, **over: Any) -> TrialProvenance:
    base: dict[str, Any] = {**GOOD, "execution_id": f"x{n}", "verification_status": status}
    return TrialProvenance(**{**base, **over})


def status_of(verdict: TrialVerdict) -> VerificationStatus:
    if verdict == TrialVerdict.VERIFICATION_UNAVAILABLE:
        return VerificationStatus.UNAVAILABLE
    if verdict in (TrialVerdict.INFRASTRUCTURE_FAILURE, TrialVerdict.EXECUTION_INCOMPLETE):
        return VerificationStatus.NOT_COMPLETED
    return VerificationStatus.VERIFIED


class AttestedScript(Script):
    """A `Script` whose executor attests everything the recovery gates need.

    `verdict(n)` scripts the n-th attempt; `down(now)` says whether the verifier is unavailable
    when a round starts (reported by `facts`, so probes and the pre-round check see it);
    `infra_down(now)` does the same for the infrastructure. `prov(n)` overrides provenance
    fields of one attempt, `origin(n)` sets a timeout origin."""

    def __init__(
        self,
        verdict: Callable[[int], TrialVerdict] | TrialVerdict,
        *,
        down: Callable[[datetime], bool] = lambda _now: False,
        infra_down: Callable[[datetime], bool] = lambda _now: False,
        prov: dict[int, dict[str, Any]] | None = None,
        all_prov: dict[str, Any] | None = None,
        origin: dict[int, TimeoutOrigin] | None = None,
        facts_over: dict[str, Any] | None = None,
        **kw: Any,
    ):
        kw.setdefault("runtime", "rt-1")
        kw.setdefault("verifier", "v-1")
        super().__init__(verdict, **kw)
        self.down, self.infra_down = down, infra_down
        self.prov_over = prov or {}
        self.all_prov = all_prov or {}  # applied to every attempt, under `prov`
        self.origins = origin or {}
        self.facts_over = facts_over or {}

    def facts(self, ref: CellReference, now: datetime) -> ExecutorFacts:
        base = super().facts(ref, now)
        return base.model_copy(
            update={
                "verification_available": not self.down(now),
                "infrastructure_available": not self.infra_down(now),
                **{k: v for k, v in GOOD.items() if k in _FACT_FIELDS},
                **self.facts_over,
            }
        )

    def run(self, ref: CellReference, key: TrialKey, at: datetime) -> TrialResult | None:
        n = self.calls
        res = super().run(ref, key, at)
        assert res is not None
        over = {**self.all_prov, **self.prov_over.get(n, {})}
        prov = provenance(n, status_of(res.verdict), **over)
        return TrialResult.model_validate(
            {
                **res.model_dump(),
                "provenance": prov.model_dump(),
                "timeout_origin": self.origins.get(n),
            }
        )


_FACT_FIELDS = {
    "executor_id",
    "model_id",
    "model_version",
    "tool_versions",
    "verifier_id",
    "verifier_independent",
}
