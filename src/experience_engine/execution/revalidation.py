"""Out-of-band revalidation trials through the production execution path (M6.3).

A trial runs one revalidation task with a *forced* action (the quarantined alternative) through the
ordinary `TaskOrchestrator` and the independent outcome verifier. The orchestrator is built without
a run lifecycle, so nothing is written to the production run, decision or outcome tables: a trial
can never become a production decision or an observation of the drift monitor. The only record is
the revalidation trial row written by the revalidation service.
"""

from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from experience_engine.controller.drift import CellReference
from experience_engine.controller.revalidation import classify_run_outcome
from experience_engine.domain.controller import parse_action_key
from experience_engine.domain.decision import ControllerDecision
from experience_engine.domain.outcome import TerminationReason
from experience_engine.domain.revalidation import (
    ExecutorFacts,
    TrialKey,
    TrialResult,
)
from experience_engine.domain.task import TaskSpec
from experience_engine.execution.controller import (
    SELECTED_CAPABILITY_KEY,
    Controller,
    ControllerInput,
)
from experience_engine.execution.orchestrator import TaskOrchestrator
from experience_engine.ports.llm import LLMClient
from experience_engine.ports.tools import EnvironmentSession

FORCED_POLICY_VERSION = "revalidation-forced/1"
_TIMEOUTS = {TerminationReason.DEADLINE, TerminationReason.MAX_STEPS, TerminationReason.MAX_TOKENS}
_INVALID = {TerminationReason.INVALID_ACTION, TerminationReason.TOOL_NOT_ALLOWED}


class ForcedController:
    """Always selects the given action. Used only for revalidation trials: the quarantined action
    must be exercised on purpose, which no production controller is allowed to do."""

    policy_version = FORCED_POLICY_VERSION

    def __init__(self, action_key: str):
        self._strategy, self._ref = parse_action_key(action_key)

    def decide(self, ctx: ControllerInput) -> ControllerDecision:
        snapshot: dict[str, object] = {
            "task_family": ctx.task.task_family,
            "risk_level": ctx.task.risk_level,
            "revalidation_trial": True,
        }
        if self._ref is not None:
            snapshot[SELECTED_CAPABILITY_KEY] = self._ref
        return ControllerDecision(
            decision_id=ctx.decision_id,
            run_id=ctx.run_id,
            tenant_id=ctx.task.tenant_id,
            policy_version=self.policy_version,
            feature_snapshot=snapshot,
            eligible_actions=[self._strategy],
            estimates={},
            selected_action=self._strategy,
            selection_probability=1.0,
            reason="revalidation trial: the quarantined action is exercised on purpose",
        )


@dataclass(frozen=True)
class RevalidationTask:
    """A task from the revalidation pool with fresh environment and model factories (a trial must
    not share state with any other run)."""

    task: TaskSpec
    session: Callable[[], EnvironmentSession]
    llm: Callable[[], LLMClient]


class OrchestratorTrialSource:
    """`TrialSource` backed by the production orchestrator and the independent verifier."""

    def __init__(
        self,
        *,
        tenant_id: str,
        tasks: Sequence[RevalidationTask],
        build: Callable[[Controller], TaskOrchestrator],
        runtime_version: str,
        verifier_version: str,
        verification_available: Callable[[], bool] = lambda: True,
        supports: Callable[[str], bool] = lambda _key: True,
        workers: int = 1,
        clock: Callable[[], datetime] | None = None,
    ):
        self._tenant = tenant_id
        self._tasks = [t for t in tasks if t.task.tenant_id == tenant_id]  # tenant isolation
        self._build = build
        self._runtime = runtime_version
        self._verifier = verifier_version
        self._available = verification_available
        self._supports = supports
        self._next = 0
        self._workers = max(1, workers)
        # a real executor reports its own observation time; the case machine's `at` is a nominal
        # slot (it can lie in the future of the wall clock) and is used only without a clock
        self._clock = clock
        # results executed ahead of consumption (workers > 1); stamped when consumed
        self._ready: deque[TrialResult] = deque()
        self.unconsumed_tokens = 0  # tokens of prefetched trials that were never recorded

    def _take(self, family: str | None) -> RevalidationTask | None:
        while self._next < len(self._tasks):
            t = self._tasks[self._next]
            self._next += 1
            if family is None or t.task.task_family == family:
                return t
        return None

    def _execute(self, ref: CellReference, item: RevalidationTask, key_id: str) -> TrialResult:
        res = self._build(ForcedController(ref.alt_key)).execute(
            item.task, item.session(), item.llm(), run_tags={"revalidation": key_id}
        )
        out, term = res.outcome, res.runtime.termination
        verdict = classify_run_outcome(
            out.status,
            out.verification_results,
            timed_out=term in _TIMEOUTS,
            invalid_output=term in _INVALID,
        )
        return TrialResult(
            verdict=verdict,
            tokens=out.tokens_used,
            task_ref=item.task.task_id,
            observed_at=datetime.min,  # stamped when the trial is consumed
            runtime_version=self._runtime,
            verifier_version=out.verifier_version,
            capability_ref=ref.alt_key,
            latency_ms=out.execution_time_ms,
            tenant_id=out.tenant_id,
        )

    def facts(self, ref: CellReference, now: datetime) -> ExecutorFacts:
        left = sum(
            1
            for t in self._tasks[self._next :]
            if ref.family is None or t.task.task_family == ref.family
        )
        return ExecutorFacts(
            tenant_id=self._tenant,
            supported=self._supports(ref.alt_key),
            runtime_version=self._runtime,
            verifier_version=self._verifier,
            verification_available=self._available(),
            pool=left + len(self._ready),
        )

    def run(self, ref: CellReference, key: TrialKey, at: datetime) -> TrialResult | None:
        if not self._ready:
            batch: list[RevalidationTask] = []
            while len(batch) < self._workers:
                item = self._take(ref.family)
                if item is None:
                    break
                batch.append(item)
            if not batch:
                return None
            if len(batch) == 1:
                self._ready.append(self._execute(ref, batch[0], key.ident()))
            else:  # concurrent real executions; results are served in task order
                with ThreadPoolExecutor(len(batch)) as pool:
                    self._ready.extend(
                        pool.map(lambda it: self._execute(ref, it, key.ident()), batch)
                    )
        got = self._ready.popleft()
        return got.model_copy(update={"observed_at": self._clock() if self._clock else at})

    def close(self) -> int:
        """Tokens spent on trials that were executed but never consumed (disclosed, not hidden)."""
        self.unconsumed_tokens = sum(r.tokens for r in self._ready)
        return self.unconsumed_tokens
