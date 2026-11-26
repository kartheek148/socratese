"""Full-information task tables from recorded paired experiments (`ee-exp` arms A and E)."""

import hashlib
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from experience_engine.controller.simulate import TaskOutcome
from experience_engine.domain.controller import PLANNING_KEY, CandidateFeature, Risk, action_key
from experience_engine.domain.decision import Strategy
from experience_engine.domain.experiment import ExperimentResult, RunRow
from experience_engine.domain.outcome import OutcomeStatus

PLANNING_ARM = "A"  # stateless planning
CAPABILITY_ARM = "E"  # production path: capability with planner fallback


@dataclass(frozen=True)
class EnvReport:
    n_pairs: int
    n_unknown_dropped: int
    n_without_capability: int


def _status(row: RunRow) -> OutcomeStatus:
    return OutcomeStatus(row.status)


def from_experiment(
    result: ExperimentResult,
    *,
    risk: Risk = "low",
    families: Sequence[str] | None = None,
) -> tuple[list[TaskOutcome], EnvReport]:
    """One TaskOutcome per (task, repetition) with both arms recorded and verified-known.

    The capability action exists only for rows where a capability was actually offered and used;
    its outcome is the *final* outcome of the production run (planner fallback included).
    """
    by: dict[tuple[str, int], dict[str, RunRow]] = defaultdict(dict)
    for r in result.rows:
        if r.arm in (PLANNING_ARM, CAPABILITY_ARM) and (families is None or r.family in families):
            by[(r.task_id, r.rep)][r.arm] = r
    out: list[TaskOutcome] = []
    unknown = no_cap = 0
    for (task_id, rep), arms in sorted(by.items()):
        a, e = arms.get(PLANNING_ARM), arms.get(CAPABILITY_ARM)
        if a is None or e is None:
            continue
        if _status(a) == OutcomeStatus.UNKNOWN or _status(e) == OutcomeStatus.UNKNOWN:
            unknown += 1
            continue
        actions = [CandidateFeature(key=PLANNING_KEY, strategy=Strategy.GENERAL_PLANNING)]
        outcomes = {PLANNING_KEY: (_status(a), a.tokens)}
        default = PLANNING_KEY
        if e.capability_used and e.asset_ref:
            key = action_key(Strategy.DIRECT_CAPABILITY, e.asset_ref)
            actions.append(
                CandidateFeature(key=key, strategy=Strategy.DIRECT_CAPABILITY, ref=e.asset_ref)
            )
            outcomes[key] = (_status(e), e.tokens)
            default = key
        else:
            no_cap += 1
        name = f"{task_id}#{rep}" if rep else task_id
        out.append(TaskOutcome(name, a.family, risk, actions, default, outcomes))
    return out, EnvReport(len(out), unknown, no_cap)


def with_protected_share(
    tasks: Sequence[TaskOutcome], share: float, risk: Risk = "high", salt: str = "protect"
) -> list[TaskOutcome]:
    """Relabel a deterministic share of tasks (by id hash) as a protected risk class."""
    out = []
    for t in tasks:
        h = int(hashlib.sha256(f"{salt}|{t.task_id}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        out.append(
            TaskOutcome(
                t.task_id,
                t.family,
                risk if h < share else t.risk,
                t.actions,
                t.default_key,
                t.outcomes,
            )
        )
    return out


def describe(tasks: Sequence[TaskOutcome]) -> dict[str, dict[str, tuple[int, float, float]]]:
    """family -> action key -> (n, success rate, mean tokens): the recorded per-action truth."""
    acc: dict[str, dict[str, list[tuple[bool, int]]]] = defaultdict(lambda: defaultdict(list))
    for t in tasks:
        for k, (st, tok) in t.outcomes.items():
            acc[t.family or "-"][k].append((st == OutcomeStatus.VERIFIED_SUCCESS, tok))
    return {
        f: {
            k: (len(v), sum(s for s, _ in v) / len(v), sum(t for _, t in v) / len(v))
            for k, v in sorted(d.items())
        }
        for f, d in sorted(acc.items())
    }
