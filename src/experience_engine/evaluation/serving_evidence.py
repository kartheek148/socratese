"""Final-outcome paired evidence from an evaluation's rows (M6.2-A, pure).

Pairs the stateless planner (arm A) with the capability as production serves it (arm E_SERVING:
matcher -> executor -> planner fallback) on the held-out and transfer tasks of one evaluation. The
result feeds controller priors only; it is never an input of a promotion decision.
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence

from experience_engine.domain.evaluation import Arm, ServingEvidence, Suite, TaskResult
from experience_engine.evaluation.task_stats import task_binary

PAIRED_SUITES = (Suite.HELD_OUT, Suite.TRANSFER)  # never DISCOVERY / source replay
UNKNOWN = "UNKNOWN"
VERIFIED = "VERIFIED_SUCCESS"


def build_serving_evidence(
    rows: Sequence[TaskResult],
    *,
    capability_ref: str,
    family: str,
    risk_class: str,
    capability_hash: str = "",
    catalog: str = "EVALUATION_STATIC_CATALOG",
) -> ServingEvidence:
    scoped = [
        r
        for r in rows
        if r.suite in PAIRED_SUITES and r.arm in (Arm.BASELINE, Arm.SERVING) and r.family == family
    ]
    seen: dict[tuple[str, str, str, int], int] = defaultdict(int)
    for r in scoped:
        seen[(r.suite.value, r.arm.value, r.task_id, r.seed)] += 1
    duplicates = sum(c - 1 for c in seen.values())
    by_task: dict[str, dict[Arm, list[TaskResult]]] = defaultdict(
        lambda: {Arm.BASELINE: [], Arm.SERVING: []}
    )
    for r in scoped:
        by_task[r.task_id][r.arm].append(r)
    both = default_only = alt_only = neither = 0
    unknown = tied = not_offered = missing = 0
    d_tok = a_tok = 0
    sig: list[tuple[str, bool, bool, int, int]] = []
    for task_id, arms in sorted(by_task.items()):
        a_rows, e_rows = arms[Arm.BASELINE], arms[Arm.SERVING]
        if not a_rows or not e_rows:
            continue
        a_known = [r for r in a_rows if r.status != UNKNOWN]
        e_known = [r for r in e_rows if r.status != UNKNOWN]
        if not a_known or not e_known:
            unknown += 1
            continue
        if not all(r.capability_used and r.asset_ref == capability_ref for r in e_known):
            not_offered += 1  # the capability was not the one served: not this comparison
            continue
        a_win = task_binary([r.status == VERIFIED for r in a_known])
        e_win = task_binary([r.status == VERIFIED for r in e_known])
        if a_win is None or e_win is None:
            tied += 1
            continue
        a_t = round(sum(r.tokens for r in a_known) / len(a_known))
        e_t = round(sum(r.tokens for r in e_known) / len(e_known))
        if min(r.tokens for r in (*a_known, *e_known)) <= 0:
            missing += 1
        both += int(a_win and e_win)
        alt_only += int(a_win and not e_win)  # planner solved it, the capability did not
        default_only += int(e_win and not a_win)
        neither += int(not a_win and not e_win)
        d_tok += e_t
        a_tok += a_t
        sig.append((task_id, a_win, e_win, a_t, e_t))
    n = both + default_only + alt_only + neither
    serving_rows = [r for r in scoped if r.arm == Arm.SERVING]
    return ServingEvidence(
        capability_ref=capability_ref,
        family=family,
        risk_class=risk_class,
        suites=[s.value for s in PAIRED_SUITES],
        n=n,
        both=both,
        default_only=default_only,
        alt_only=alt_only,
        neither=neither,
        default_tokens=d_tok,
        alt_tokens=a_tok,
        n_unknown_dropped=unknown,
        n_tied_dropped=tied,
        n_not_offered=not_offered,
        n_missing_cost=missing,
        n_duplicate_rows=duplicates,
        tasks_sha256=hashlib.sha256(json.dumps(sig, sort_keys=True).encode()).hexdigest(),
        capability_hash=capability_hash,
        catalog=catalog,
        serving_tokens=sum(r.tokens for r in serving_rows),
        n_runs=len(serving_rows),
    )
