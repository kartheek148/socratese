"""Paired validation priors (M6.1): held-out outcomes of the rule default and an alternative on the
same tasks, as 2x2 cells with provenance.

Only final production outcomes count (arm E includes the planner fallback); the promotion
evaluator's capability-alone arm is a different quantity (M5.10 Layer A) and is never used here.
"""

import hashlib
import json
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from experience_engine.domain.controller import PairedCell, PairedSource, paired_key
from experience_engine.domain.outcome import OutcomeStatus

if TYPE_CHECKING:
    from experience_engine.controller.simulate import TaskOutcome

HELD_OUT_SPLITS = frozenset({"VALIDATION", "HIDDEN_TRANSFER"})
SUBSAMPLE_SEED = 20260602


@dataclass(frozen=True)
class PairedPrior:
    cells: dict[str, PairedCell] = field(default_factory=dict)
    sources: list[PairedSource] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)  # cell key -> n (< n_min_pairs)

    @property
    def validation_tokens(self) -> int:
        return sum(s.validation_tokens for s in self.sources)


def _success(status: OutcomeStatus) -> bool:
    return status == OutcomeStatus.VERIFIED_SUCCESS


def build_paired_cells(tasks: "Sequence[TaskOutcome]") -> dict[str, PairedCell]:
    """One 2x2 table per (context, default, alternative); UNKNOWN outcomes are skipped."""
    acc: dict[str, list[int]] = {}
    for t in tasks:
        if t.default_key not in t.outcomes:
            continue
        d_status, d_tokens = t.outcomes[t.default_key]
        if d_status == OutcomeStatus.UNKNOWN:
            continue
        for key, (status, tokens) in t.outcomes.items():
            if key == t.default_key or status == OutcomeStatus.UNKNOWN:
                continue
            c = acc.setdefault(
                paired_key(t.family, t.risk, t.default_key, key), [0, 0, 0, 0, 0, 0, 0]
            )
            sd, sk = _success(d_status), _success(status)
            c[0] += 1
            c[1] += sd and sk
            c[2] += sd and not sk
            c[3] += sk and not sd
            c[4] += (not sd) and (not sk)
            c[5] += d_tokens
            c[6] += tokens
    return {
        k: PairedCell(
            n=v[0],
            both=v[1],
            default_only=v[2],
            alt_only=v[3],
            neither=v[4],
            default_tokens=v[5],
            alt_tokens=v[6],
        )
        for k, v in sorted(acc.items())
    }


def make_prior(
    tasks: "Sequence[TaskOutcome]",
    sources: Sequence[PairedSource],
    n_min_pairs: int = 30,
) -> PairedPrior:
    """Cells with fewer than `n_min_pairs` tasks are not used (reported in `excluded`)."""
    cells = build_paired_cells(tasks)
    return PairedPrior(
        cells={k: c for k, c in cells.items() if c.n >= n_min_pairs},
        sources=list(sources),
        excluded={k: c.n for k, c in cells.items() if c.n < n_min_pairs},
    )


def subsample(
    tasks: "Sequence[TaskOutcome]", n: int, seed: int = SUBSAMPLE_SEED
) -> "list[TaskOutcome]":
    """A seeded random subset of `n` tasks (used for the prior-size curve)."""
    order = list(range(len(tasks)))
    random.Random(seed).shuffle(order)
    return [tasks[i] for i in sorted(order[:n])]


def tasks_hash(tasks: "Sequence[TaskOutcome]") -> str:
    rows = [
        (t.task_id, t.default_key, sorted((k, s.value, tok) for k, (s, tok) in t.outcomes.items()))
        for t in sorted(tasks, key=lambda x: x.task_id)
    ]
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def version_problems(
    source: PairedSource, runtime_version: str, verifier_version: str
) -> list[str]:
    """Reasons a paired source must not seed a policy served by this runtime and verifier."""
    problems = []
    if source.runtime_version != runtime_version:
        problems.append(
            f"{source.name}: recorded under runtime {source.runtime_version!r}, serving "
            f"{runtime_version!r}"
        )
    if source.verifier_version != verifier_version:
        problems.append(
            f"{source.name}: judged by verifier {source.verifier_version!r}, serving "
            f"{verifier_version!r}"
        )
    return problems


def required_pairs(
    cell: PairedCell, margin: float, confidence: float, cap: int = 5000
) -> int | None:
    """Smallest number of paired tasks (counts scaled up proportionally) for which the paired
    lower bound would clear `-margin`; None if it cannot at the observed difference."""
    from experience_engine.domain.stats import newcombe_paired_bounds

    if cell.n == 0:
        return None
    step = max(1, cell.n // 20)
    m = cell.n
    while m <= cap:
        f = m / cell.n
        b, a_only, d_only, nn = (
            round(cell.both * f),
            round(cell.alt_only * f),
            round(cell.default_only * f),
            round(cell.neither * f),
        )
        lo, _ = newcombe_paired_bounds(b, a_only, d_only, nn, confidence)
        if lo >= -margin:
            return m
        m += step
    return None


@dataclass(frozen=True)
class ResolutionRow:
    key: str
    n: int
    draws: int
    p_admissible: float  # lower bound >= -delta: the alternative is proven non-inferior
    p_worse: float  # upper bound < -delta: proven inferior
    p_unresolved: float


def resolution_curve(
    tasks: "Sequence[TaskOutcome]",
    sizes: Sequence[int],
    *,
    draws: int,
    delta: float,
    confidence: float,
    seed: int = SUBSAMPLE_SEED,
) -> list[ResolutionRow]:
    """How often an independent validation of `n` paired tasks would resolve each cell.

    Each draw resamples `n` tasks with replacement from the recorded pairs (their empirical
    distribution stands in for the population), builds the 2x2 table and applies the same paired
    bound the policy uses. Descriptive planning evidence about validation size; it uses no live or
    held-out data.
    """
    from experience_engine.domain.stats import newcombe_paired_bounds

    groups: dict[str, list[TaskOutcome]] = {}
    for t in tasks:
        for k in t.outcomes:
            if k != t.default_key:
                groups.setdefault(paired_key(t.family, t.risk, t.default_key, k), []).append(t)
    rng = random.Random(seed)
    rows: list[ResolutionRow] = []
    for key, group in sorted(groups.items()):
        for n in sizes:
            ok = worse = 0
            for _ in range(draws):
                cell = build_paired_cells(rng.choices(group, k=n)).get(key)
                if cell is None or cell.n == 0:
                    continue
                lo, hi = newcombe_paired_bounds(
                    cell.both, cell.alt_only, cell.default_only, cell.neither, confidence
                )
                ok += lo >= -delta
                worse += hi < -delta
            rows.append(
                ResolutionRow(key, n, draws, ok / draws, worse / draws, 1 - (ok + worse) / draws)
            )
    return rows
