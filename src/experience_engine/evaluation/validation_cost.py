"""Measured validation cost (pure): what one evaluation actually spent vs reused.

Everything here is computed from per-task rows. "Executed" runs consumed model calls in this
evaluation; "cached" runs were served from a recorded run (same-session sharing or the persistent
baseline cache) and their tokens are those recorded when the run was first computed. Nothing is
extrapolated: a saving is reported only for runs that were actually avoided.
"""

from collections import defaultdict
from collections.abc import Iterable

from experience_engine.domain.evaluation import ArmResources, TaskResult, ValidationResources


def resources_from_rows(
    rows: Iterable[TaskResult],
    *,
    early_stopped: bool = False,
    stage_reached: int | None = None,
) -> ValidationResources:
    acc: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        a = acc[r.arm.value]
        if r.cache_hit:
            a["runs_cached"] += 1
            a["tokens_cached"] += r.tokens
            continue
        a["runs_executed"] += 1
        a["tokens_executed"] += r.tokens
        a["tool_calls_executed"] += r.tool_calls
        a["latency_ms_executed"] += r.latency_ms
        if r.prompt_tokens is not None and r.completion_tokens is not None:
            a["prompt_tokens"] += r.prompt_tokens
            a["completion_tokens"] += r.completion_tokens
            a["runs_with_split"] += 1
    return ValidationResources(
        by_arm={arm: ArmResources(**dict(v)) for arm, v in sorted(acc.items())},
        early_stopped=early_stopped,
        stage_reached=stage_reached,
    )


def total_executed_tokens(res: ValidationResources) -> int:
    return sum(a.tokens_executed for a in res.by_arm.values())


def total_cached_tokens(res: ValidationResources) -> int:
    return sum(a.tokens_cached for a in res.by_arm.values())


def format_resources(res: ValidationResources) -> list[str]:
    out = []
    for arm, a in res.by_arm.items():
        split = (
            f", input {a.prompt_tokens:,} / output {a.completion_tokens:,} over "
            f"{a.runs_with_split} runs"
            if a.runs_with_split
            else ""
        )
        out.append(
            f"{arm}: executed {a.runs_executed} runs, {a.tokens_executed:,} tokens, "
            f"{a.tool_calls_executed} tool calls{split}; reused {a.runs_cached} runs "
            f"({a.tokens_cached:,} tokens as recorded)"
        )
    return out
