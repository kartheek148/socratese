"""Lifecycle economics: what it costs to *own* reusable experience, not just to execute it.

    lifecycle_cost_X(N) = L_X + N * e_X + M_X          (tokens; N future task executions)

Because arms differ in success rate, arms are also compared per *verified success*:
    c_X = e_X / success_rate_X          (tokens per verified success, failures' cost included)
    break-even vs the stateless baseline, for N verified successes:  N* = (L_X + M_X) / (c_A - c_X)
Never reached when c_X >= c_A. Maintenance M is an explicit input (not measured here).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from experience_engine.domain.experiment import LearningLedger, RunRow


def learning_tokens(arm: str, ledger: LearningLedger, *, charge_validation_to_all: bool) -> int:
    """Tokens spent producing what this arm reuses. Arm A reuses nothing."""
    if arm == "A":
        return 0
    base = ledger.discovery_tokens + ledger.analysis_tokens
    extra = {
        "B": 0,
        "C": ledger.consolidation_tokens,
        "D": ledger.compilation_tokens,
        "E": ledger.compilation_tokens,
    }[arm]
    # As built, only the capability is validated (it is the only arm with a promotion gate). The
    # sensitivity variant charges the same validation to every learning arm.
    validated = arm == "E" or charge_validation_to_all
    return base + extra + (ledger.validation_tokens if validated else 0)


def learning_wall_ms(arm: str, ledger: LearningLedger) -> int:
    if arm == "A":
        return 0
    return (
        ledger.wall_ms_analysis
        + {
            "B": 0,
            "C": ledger.wall_ms_consolidation,
            "D": ledger.wall_ms_compilation,
            "E": ledger.wall_ms_compilation,
        }[arm]
    )


@dataclass(frozen=True)
class ArmEconomics:
    arm: str
    n_runs: int
    verified: int
    success_rate: float
    tokens_per_run: float  # e
    tokens_per_success: float | None  # c
    tool_calls_per_run: float
    latency_ms_per_run: float
    retrieved_tokens_per_run: float
    retrieval_ms_per_run: float
    learning_tokens: int  # L
    maintenance_tokens: int  # M
    learning_wall_ms: int
    breakeven_successes: float | None  # None = never recovered
    breakeven_tasks: float | None
    breakeven_note: str

    def lifecycle_cost(self, n_tasks: int) -> float:
        return self.learning_tokens + n_tasks * self.tokens_per_run + self.maintenance_tokens


def _breakeven(extra: float, saving: float) -> float | None:
    if extra <= 0:
        return 0.0
    return extra / saving if saving > 0 else None


def lifecycle_table(
    rows: Sequence[RunRow],
    ledger: LearningLedger,
    *,
    arms: Sequence[str],
    maintenance_tokens: int = 0,
    charge_validation_to_all: bool = False,
    where: Callable[[RunRow], bool] | None = None,
) -> dict[str, ArmEconomics]:
    sel = [r for r in rows if r.status != "UNKNOWN" and (where is None or where(r))]

    def stats(arm: str) -> tuple[int, int, float, float, float, float, float]:
        rs = [r for r in sel if r.arm == arm]
        n = len(rs)
        if n == 0:
            return (0, 0, 0.0, 0.0, 0.0, 0.0, 0.0)
        return (
            n,
            sum(r.verified for r in rs),
            sum(r.tokens for r in rs) / n,
            sum(r.tool_calls for r in rs) / n,
            sum(r.latency_ms for r in rs) / n,
            sum(r.retrieved_tokens for r in rs) / n,
            sum(r.retrieval_ms for r in rs) / n,
        )

    base = stats("A")
    c_a = base[2] / (base[1] / base[0]) if base[0] and base[1] else None
    out: dict[str, ArmEconomics] = {}
    for arm in arms:
        n, ok, e, calls, lat, ret_tok, ret_ms = stats(arm)
        rate = ok / n if n else 0.0
        c = e / rate if rate else None
        learn = learning_tokens(arm, ledger, charge_validation_to_all=charge_validation_to_all)
        maint = 0 if arm == "A" else maintenance_tokens
        if arm == "A":
            be_s, be_t, note = None, None, "reference"
        elif not n:
            be_s, be_t, note = None, None, "no runs"
        elif c is None:
            be_s, be_t, note = None, None, "no verified successes: never recovered"
        elif c_a is None:
            be_s, be_t, note = 0.0, 0.0, "baseline had no verified successes"
        else:
            be_s = _breakeven(learn + maint, c_a - c)
            be_t = _breakeven(learn + maint, base[2] - e)
            note = (
                "" if be_s is not None else "execution is not cheaper per success: never recovered"
            )
        out[arm] = ArmEconomics(
            arm=arm,
            n_runs=n,
            verified=ok,
            success_rate=rate,
            tokens_per_run=e,
            tokens_per_success=c,
            tool_calls_per_run=calls,
            latency_ms_per_run=lat,
            retrieved_tokens_per_run=ret_tok,
            retrieval_ms_per_run=ret_ms,
            learning_tokens=learn,
            maintenance_tokens=maint,
            learning_wall_ms=learning_wall_ms(arm, ledger),
            breakeven_successes=be_s,
            breakeven_tasks=be_t,
            breakeven_note=note,
        )
    return out
