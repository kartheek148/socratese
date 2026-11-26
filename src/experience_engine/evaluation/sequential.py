"""Predefined sequential stopping rules for validation (pure).

Stages (see docs/experiments/promotion-policy-v2-design.md section 6):
  1 static   - applicability refusals (no model tokens)
  2 screen   - a few held-out tasks, one repetition
  3 held-out - the full held-out suite
  4 final    - transfer, source replay, regression suite and the policy verdict
Early stages can only REJECT an asset that the data credibly show to be unfit; they never PASS
anything. Alpha is spent by a fixed schedule (union bound), and the ordinary confidence interval is
never inspected to decide when to stop.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass

from experience_engine.evaluation.risk import cp_upper, sign_test_worse


@dataclass(frozen=True)
class StagePlan:
    screening_tasks: int = 8
    screening_seeds: int = 1
    alpha_screen: float = 0.01
    alpha_heldout: float = 0.02

    @property
    def total_alpha(self) -> float:
        """Union bound on wrongly stopping an asset that truly meets the requirement."""
        return self.alpha_screen + self.alpha_heldout

    def config(self) -> dict[str, float | int]:
        return asdict(self)


@dataclass(frozen=True)
class RunPair:
    task_id: str
    baseline_verified: bool
    treatment_verified: bool


def stop_reasons(
    pairs: Iterable[RunPair], *, alpha: float, required_success_rate: float, stage: int
) -> list[str]:
    """Why to stop-and-reject at this look (empty = continue). Task-level: repetitions of a task
    are averaged first, so correlated repeats never inflate the evidence."""
    by_task: dict[str, list[RunPair]] = defaultdict(list)
    for p in pairs:
        by_task[p.task_id].append(p)
    if not by_task:
        return []
    treat = {t: sum(p.treatment_verified for p in v) / len(v) for t, v in by_task.items()}
    base = {t: sum(p.baseline_verified for p in v) / len(v) for t, v in by_task.items()}
    n = len(by_task)
    out: list[str] = []
    mass = sum(treat.values())
    ucb = cp_upper(mass, n, 1.0 - alpha)
    if ucb < required_success_rate:
        out.append(
            f"futility: success {mass / n:.0%} over {n} tasks, one-sided upper bound "
            f"{ucb:.0%} (alpha {alpha}) < required {required_success_rate:.0%}"
        )
    only_base = sum(1 for t in by_task if base[t] > treat[t])
    only_treat = sum(1 for t in by_task if treat[t] > base[t])
    pval = sign_test_worse(only_base, only_treat)
    if pval <= alpha:
        out.append(
            f"harm: worse than baseline on {only_base} tasks, better on {only_treat} "
            f"(one-sided exact p={pval:.4f} <= alpha {alpha})"
        )
    return [f"stage {stage}: {r}" for r in out]
