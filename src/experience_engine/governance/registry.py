"""Generic asset registry service: the only component that moves a version between lifecycle states.

Shared by every governed learning asset (capabilities, memory artifacts). Every promotion is gated
by explicit, hashed criteria for the asset's risk class, every transition is atomic and audited,
and rollback restores the previously active version in the same transaction. Learning components
never import this module (architecture test).
"""

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import datetime

from experience_engine.domain.capability import CapabilityState, can_transition
from experience_engine.domain.evaluation import EvaluationReport, PromotionPolicy, Verdict
from experience_engine.ports.artifacts import ArtifactStore
from experience_engine.ports.clock import Clock
from experience_engine.ports.registry import (
    Pointer,
    RegistryStore,
    TransitionSpec,
    VersionRecord,
)

S = CapabilityState


class RegistryError(Exception):
    pass


class IllegalTransitionError(RegistryError):
    pass


class PromotionBlockedError(RegistryError):
    """A promotion gate is not satisfied. The message lists every unmet criterion."""


class GovernedRegistry[D]:
    """`D` is the typed immutable definition of the governed asset. The id parameters are named
    `capability_id` for historical reasons and hold the asset id (capability id or memory id)."""

    def __init__(
        self,
        store: RegistryStore[D],
        policies: Mapping[str, PromotionPolicy],
        clock: Clock,
        artifacts: ArtifactStore | None,
        risk_of: Callable[[VersionRecord[D]], str],
    ):
        self._store = store
        self._policies = dict(policies)
        self._clock = clock
        self._artifacts = artifacts
        self._risk_of = risk_of

    # ---- helpers ------------------------------------------------------------------------------
    def _get(self, tenant_id: str, capability_id: str, version: int) -> VersionRecord[D]:
        rec = self._store.get_version(tenant_id, capability_id, version)
        if rec is None:
            raise RegistryError(f"{capability_id} v{version} not found for tenant")
        return rec

    def risk_class(self, rec: VersionRecord[D]) -> str:
        return self._risk_of(rec)

    def policy_for(self, rec: VersionRecord[D]) -> PromotionPolicy:
        return self._policies[self.risk_class(rec)]

    def _one(
        self,
        rec: VersionRecord[D],
        to: CapabilityState,
        actor: str,
        reason: str,
        *,
        evaluation_id: str | None = None,
        policy_hash: str | None = None,
        pointer: Pointer | None = None,
        extra: list[TransitionSpec] | None = None,
    ) -> None:
        if not can_transition(rec.state, to):
            raise IllegalTransitionError(f"{rec.state.value} -> {to.value} is not allowed")
        self._store.apply_transitions(
            tenant_id=rec.tenant_id,
            capability_id=rec.capability_id,
            transitions=[TransitionSpec(rec.version, rec.state, to), *(extra or [])],
            pointer=pointer,
            actor=actor,
            reason=reason[:500],
            evaluation_id=evaluation_id,
            policy_hash=policy_hash,
            now=self._clock.now(),
        )

    # ---- evaluation ---------------------------------------------------------------------------
    def start_validation(
        self, tenant_id: str, capability_id: str, version: int, actor: str
    ) -> None:
        rec = self._get(tenant_id, capability_id, version)
        self._one(rec, S.VALIDATING, actor, "evaluation started")

    def complete_evaluation(self, report: EvaluationReport, actor: str) -> CapabilityState:
        """Persist the report and apply its verdict: PASS -> APPROVED, FAIL -> REJECTED,
        INCONCLUSIVE -> stays VALIDATING (more evidence needed)."""
        rec = self._get(report.tenant_id, report.capability_id, report.version)
        if rec.state != S.VALIDATING:
            raise IllegalTransitionError(
                f"evaluation results require VALIDATING, not {rec.state.value}"
            )
        payload = json.dumps(report.model_dump(mode="json"), sort_keys=True).encode()
        if self._artifacts is not None:
            # full report incl. per-task rows, content-addressed so the record is reproducible
            stored = self._artifacts.put(
                report.tenant_id,
                payload,
                role="evaluation_report",
                content_type="application/json",
            )
            self._store.register_artifact(report.tenant_id, stored.ref, stored.uri)
            sha = stored.ref.sha256
        else:
            sha = hashlib.sha256(payload).hexdigest()
        evaluation_id = self._store.record_evaluation(report, sha, self._clock.now())
        if report.verdict == Verdict.PASS:
            self._one(
                rec,
                S.APPROVED,
                actor,
                "evaluation passed: " + "; ".join(report.reasons),
                evaluation_id=evaluation_id,
                policy_hash=report.policy_hash,
            )
            return S.APPROVED
        if report.verdict == Verdict.FAIL:
            self._one(
                rec,
                S.REJECTED,
                actor,
                "evaluation failed: " + "; ".join(report.reasons),
                evaluation_id=evaluation_id,
                policy_hash=report.policy_hash,
            )
            return S.REJECTED
        return S.VALIDATING

    def reject(
        self, tenant_id: str, capability_id: str, version: int, actor: str, reason: str
    ) -> None:
        rec = self._get(tenant_id, capability_id, version)
        self._one(rec, S.REJECTED, actor, reason)

    # ---- rollout ------------------------------------------------------------------------------
    def _require_current_pass(self, rec: VersionRecord[D]) -> tuple[str, str]:
        ev = self._store.latest_evaluation(rec.tenant_id, rec.capability_id, rec.version)
        policy = self.policy_for(rec)
        if ev is None or ev.verdict != Verdict.PASS:
            raise PromotionBlockedError("no passing evaluation exists for this version")
        if ev.policy_hash != policy.policy_hash():
            raise PromotionBlockedError(
                "the promotion policy changed since this version was evaluated; re-evaluate"
            )
        return ev.evaluation_id, ev.policy_hash

    def _require_legal(self, rec: VersionRecord[D], to: CapabilityState) -> None:
        if not can_transition(rec.state, to):
            raise IllegalTransitionError(f"{rec.state.value} -> {to.value} is not allowed")

    def start_shadow(self, tenant_id: str, capability_id: str, version: int, actor: str) -> None:
        rec = self._get(tenant_id, capability_id, version)
        self._require_legal(rec, S.SHADOW)
        evaluation_id, policy_hash = self._require_current_pass(rec)
        self._one(
            rec,
            S.SHADOW,
            actor,
            "shadow rollout started",
            evaluation_id=evaluation_id,
            policy_hash=policy_hash,
        )

    def advance_to_canary(
        self, tenant_id: str, capability_id: str, version: int, actor: str
    ) -> None:
        rec = self._get(tenant_id, capability_id, version)
        self._require_legal(rec, S.CANARY)
        policy = self.policy_for(rec)
        stats = self._store.observation_stats(tenant_id, capability_id, version, "SHADOW")
        unmet = []
        if stats.n < policy.min_shadow_observations:
            unmet.append(
                f"{stats.n} shadow observations < required {policy.min_shadow_observations}"
            )
        else:
            failure_rate = 1 - stats.capability_verified / stats.n
            if failure_rate > policy.max_shadow_failure_rate:
                unmet.append(
                    f"shadow failure rate {failure_rate:.0%} > {policy.max_shadow_failure_rate:.0%}"
                )
            if stats.regressions > policy.max_regressions:
                unmet.append(f"{stats.regressions} shadow regressions > {policy.max_regressions}")
        if unmet:
            raise PromotionBlockedError("; ".join(unmet))
        self._one(rec, S.CANARY, actor, "shadow gates satisfied", policy_hash=policy.policy_hash())

    def activate(self, tenant_id: str, capability_id: str, version: int, actor: str) -> None:
        rec = self._get(tenant_id, capability_id, version)
        self._require_legal(rec, S.ACTIVE)
        policy = self.policy_for(rec)
        stats = self._store.observation_stats(tenant_id, capability_id, version, "CANARY")
        unmet = []
        if stats.n < policy.min_canary_observations:
            unmet.append(
                f"{stats.n} canary observations < required {policy.min_canary_observations}"
            )
        elif stats.capability_verified / stats.n < policy.min_canary_success_rate:
            unmet.append(
                f"canary success {stats.capability_verified / stats.n:.0%} "
                f"< {policy.min_canary_success_rate:.0%}"
            )
        if unmet:
            raise PromotionBlockedError("; ".join(unmet))
        pointer = self._store.get_pointer(tenant_id, capability_id)
        extra: list[TransitionSpec] = []
        if pointer.active_version is not None and pointer.active_version != version:
            # the previous active version stays recoverable: DEPRECATED + remembered as previous
            extra.append(TransitionSpec(pointer.active_version, S.ACTIVE, S.DEPRECATED))
        self._one(
            rec,
            S.ACTIVE,
            actor,
            "canary gates satisfied",
            policy_hash=policy.policy_hash(),
            pointer=Pointer(active_version=version, previous_version=pointer.active_version),
            extra=extra,
        )

    def rollback(
        self, tenant_id: str, capability_id: str, version: int, actor: str, reason: str
    ) -> None:
        """Roll a SHADOW/CANARY/ACTIVE version back. If it was the active version, the previously
        active version is restored in the same transaction (it is never lost)."""
        rec = self._get(tenant_id, capability_id, version)
        if rec.state not in (S.SHADOW, S.CANARY, S.ACTIVE):
            raise IllegalTransitionError(f"cannot roll back a {rec.state.value} version")
        pointer = self._store.get_pointer(tenant_id, capability_id)
        extra: list[TransitionSpec] = []
        new_pointer: Pointer | None = None
        if rec.state == S.ACTIVE and pointer.active_version == version:
            prev = pointer.previous_version
            if prev is not None:
                prev_rec = self._get(tenant_id, capability_id, prev)
                if prev_rec.state == S.DEPRECATED:
                    extra.append(TransitionSpec(prev, S.DEPRECATED, S.ACTIVE))
                    new_pointer = Pointer(active_version=prev, previous_version=None)
            if new_pointer is None:
                new_pointer = Pointer(active_version=None, previous_version=None)
        self._one(rec, S.ROLLED_BACK, actor, reason, pointer=new_pointer, extra=extra)

    # ---- observations (written by the shadow/canary harness, read by the gates) ----------------
    def record_observation(
        self,
        rec: VersionRecord[D],
        mode: str,
        task_id: str,
        capability_verified: bool,
        baseline_verified: bool | None,
        capability_tokens: int,
        baseline_tokens: int | None,
    ) -> None:
        expected = S.SHADOW if mode == "SHADOW" else S.CANARY
        if rec.state != expected:
            raise IllegalTransitionError(f"{mode} observations require a {expected.value} version")
        self._store.record_observation(
            tenant_id=rec.tenant_id,
            capability_id=rec.capability_id,
            version=rec.version,
            mode=mode,
            task_id=task_id,
            capability_verified=capability_verified,
            baseline_verified=baseline_verified,
            capability_tokens=capability_tokens,
            baseline_tokens=baseline_tokens,
            now=self._clock.now(),
        )

    def now(self) -> datetime:
        return self._clock.now()
