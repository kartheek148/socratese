"""Contrastive analysis of verified-success vs failed trajectories.

Findings are associations over small samples and are reported as hypotheses. They inform
candidate limitations and test design; they are never treated as causal facts (architecture §5.2).
"""

from collections.abc import Sequence

from experience_engine.domain.experience import ContrastiveFinding, FindingKind, Trajectory


def _support(trajs: Sequence[Trajectory]) -> dict[str, float]:
    counts: dict[str, int] = {}
    for t in trajs:
        for token in set(t.tool_tokens):
            counts[token] = counts.get(token, 0) + 1
    n = len(trajs)
    return {k: v / n for k, v in counts.items()} if n else {}


def contrast(
    successes: Sequence[Trajectory],
    failures: Sequence[Trajectory],
    *,
    critical_success_min: float = 0.8,
    gap: float = 0.3,
) -> list[ContrastiveFinding]:
    s_sup, f_sup = _support(successes), _support(failures)
    findings: list[ContrastiveFinding] = []
    for token in sorted(set(s_sup) | set(f_sup)):
        s, f = s_sup.get(token, 0.0), f_sup.get(token, 0.0)
        kind = FindingKind.NEUTRAL
        if failures:  # no contrast is possible without counterexamples
            if s >= critical_success_min and s - f >= gap:
                kind = FindingKind.CRITICAL
            elif f >= 0.5 and f - s >= gap:
                kind = FindingKind.AVOIDABLE
        findings.append(
            ContrastiveFinding(
                token=token,
                kind=kind,
                success_support=round(s, 4),
                failure_support=round(f, 4),
                n_success=len(successes),
                n_failure=len(failures),
            )
        )
    return findings
