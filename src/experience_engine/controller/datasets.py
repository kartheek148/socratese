"""Full-information task tables from recorded paired experiments (`ee-exp` arms A and E)."""

import hashlib
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from experience_engine.controller.paired import (
    HELD_OUT_SPLITS,
    SUBSAMPLE_SEED,
    PairedPrior,
    make_prior,
    subsample,
    tasks_hash,
)
from experience_engine.controller.simulate import TaskOutcome
from experience_engine.domain.controller import (
    PLANNING_KEY,
    CandidateFeature,
    PairedSource,
    Risk,
    action_key,
)
from experience_engine.domain.decision import Strategy
from experience_engine.domain.experiment import ExperimentResult, RunRow
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.evaluation.task_stats import task_binary

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
    id_prefix: str = "",
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
        name = f"{id_prefix}{task_id}#{rep}" if rep else f"{id_prefix}{task_id}"
        out.append(TaskOutcome(name, a.family, risk, actions, default, outcomes))
    return out, EnvReport(len(out), unknown, no_cap)


def paired_tasks(
    result: ExperimentResult,
    *,
    name: str,
    families: Sequence[str] | None = None,
    risk: Risk = "low",
    id_prefix: str = "",
) -> tuple[list[TaskOutcome], PairedSource]:
    """Task-level paired outcomes (arm A vs arm E, final outcome incl. fallback) for a prior.

    Hard rules: only held-out splits; repetitions collapse per task by strict majority (a tie
    drops the task); UNKNOWN is never a success or a failure (the task is dropped and counted);
    tasks without an offered capability carry no pair.
    """
    rows = [
        r
        for r in result.rows
        if r.arm in (PLANNING_ARM, CAPABILITY_ARM) and (families is None or r.family in families)
    ]
    illegal = sorted({r.split for r in rows} - HELD_OUT_SPLITS)
    if illegal:
        raise ValueError(
            f"paired priors may only use held-out splits, found {illegal} (discovery tasks are the "
            "source the capability was derived from)"
        )
    by_task: dict[str, dict[str, list[RunRow]]] = defaultdict(
        lambda: {PLANNING_ARM: [], CAPABILITY_ARM: []}
    )
    for r in rows:
        by_task[r.task_id][r.arm].append(r)
    tasks: list[TaskOutcome] = []
    unknown = tied = 0
    refs: set[str] = set()
    used_splits: set[str] = set()
    for task_id, arms in sorted(by_task.items()):
        a_known = [r for r in arms[PLANNING_ARM] if r.status != OutcomeStatus.UNKNOWN.value]
        e_known = [r for r in arms[CAPABILITY_ARM] if r.status != OutcomeStatus.UNKNOWN.value]
        if not arms[PLANNING_ARM] or not arms[CAPABILITY_ARM]:
            continue
        if not a_known or not e_known:
            unknown += 1
            continue
        used = [r for r in e_known if r.capability_used and r.asset_ref]
        if not used:
            continue  # no eligible capability: nothing to pair
        task_refs = {r.asset_ref for r in used if r.asset_ref}
        if len(task_refs) != 1 or len(used) != len(e_known):
            unknown += 1  # inconsistent capability across repetitions: not one comparable pair
            continue
        a_win = task_binary([r.verified for r in a_known])
        e_win = task_binary([r.verified for r in e_known])
        if a_win is None or e_win is None:
            tied += 1
            continue
        (ref,) = task_refs
        key = action_key(Strategy.DIRECT_CAPABILITY, ref)
        a_tok = round(sum(r.tokens for r in a_known) / len(a_known))
        e_tok = round(sum(r.tokens for r in e_known) / len(e_known))

        def status(win: bool) -> OutcomeStatus:
            return OutcomeStatus.VERIFIED_SUCCESS if win else OutcomeStatus.FAILURE

        tasks.append(
            TaskOutcome(
                f"{id_prefix}{task_id}",
                a_known[0].family,
                risk,
                [
                    CandidateFeature(key=PLANNING_KEY, strategy=Strategy.GENERAL_PLANNING),
                    CandidateFeature(key=key, strategy=Strategy.DIRECT_CAPABILITY, ref=ref),
                ],
                key,
                {PLANNING_KEY: (status(a_win), a_tok), key: (status(e_win), e_tok)},
            )
        )
        refs.add(ref)
        used_splits.add(a_known[0].split)
    cfg = result.config
    source = PairedSource(
        name=name,
        arms=[PLANNING_ARM, CAPABILITY_ARM],
        splits=sorted(used_splits),
        dataset_hashes={
            s: d.get("content_hash", "") for s, d in cfg.datasets.items() if s in used_splits
        },
        model=str(cfg.model.get("model", "")),
        runtime_version=cfg.runtime_version,
        verifier_version=cfg.verifier_version,
        n_pairs=len(tasks),
        n_unknown_dropped=unknown,
        n_tied_dropped=tied,
        tasks_sha256=tasks_hash(tasks),
        validation_tokens=sum(tok for t in tasks for _, tok in t.outcomes.values()),
        capabilities=sorted(refs),
    )
    return tasks, source


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


def load_prior(
    paths: Sequence[str | Path],
    *,
    families: Sequence[str] | None = None,
    n_min_pairs: int = 30,
    subsample_n: int = 0,
    subsample_families: Sequence[str] | None = None,
    seed: int = SUBSAMPLE_SEED,
) -> PairedPrior:
    """A paired prior from recorded `ee-ctl paired-run` / `ee-exp` files (held-out splits only).

    `subsample_n` keeps a seeded random `subsample_n` tasks per family (the sample-size curve); the
    provenance is recomputed for what is actually kept.
    """
    tasks: list[TaskOutcome] = []
    sources: list[PairedSource] = []
    for i, path in enumerate(paths):
        result = ExperimentResult.model_validate_json(Path(path).read_text())
        t, src = paired_tasks(result, name=Path(path).name, families=families, id_prefix=f"p{i}:")
        tasks += t
        sources.append(src)
    if subsample_n:
        kept: list[TaskOutcome] = []
        for fam in sorted({t.family or "" for t in tasks}):
            group = [t for t in tasks if (t.family or "") == fam]
            if subsample_families is None or fam in subsample_families:
                group = subsample(group, subsample_n, seed)
            kept += group
        tasks = kept
        sources = [
            src.model_copy(
                update={
                    "n_pairs": len(mine),
                    "tasks_sha256": tasks_hash(mine),
                    "validation_tokens": sum(tok for t in mine for _, tok in t.outcomes.values()),
                }
            )
            for i, src in enumerate(sources)
            for mine in [[t for t in tasks if t.task_id.startswith(f"p{i}:")]]
        ]
    return make_prior(tasks, sources, n_min_pairs)
