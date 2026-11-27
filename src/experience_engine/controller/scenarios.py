"""Frozen synthetic scenarios for the M6.2 drift experiments (synthetic, ground truth known).

World: per family the rule default is a stale capability and the alternative is the planner. Each
stream draws a paired validation prior from the world (the first draw that resolves, so a
prior-backed switch exists; the conditioning is part of the protocol), then N decisions. Drift is a
perturbation of what the controller observes when it executes the alternative.
"""

import math
from dataclasses import dataclass, field
from datetime import timedelta

from experience_engine.controller.paired import PairedPrior, make_prior, tasks_hash
from experience_engine.controller.perturb import Perturbation, Phase, normal, unit
from experience_engine.controller.simulate import SIM_EPOCH, SIM_VERIFIER, TaskOutcome
from experience_engine.domain.controller import (
    PLANNING_KEY,
    CandidateFeature,
    PairedSource,
    Risk,
    action_key,
)
from experience_engine.domain.decision import Strategy
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.domain.stats import newcombe_paired_bounds

CAP_REF = "sim.cap@1"
CAP_KEY = action_key(Strategy.DIRECT_CAPABILITY, CAP_REF)
RISK: Risk = "low"
DELTA = 0.10


@dataclass(frozen=True)
class FamilySpec:
    name: str
    p_default: float = 0.50
    p_alt: float = 0.52
    tokens_default: float = 9000.0
    tokens_alt: float = 6500.0
    cv: float = 0.35
    rho: float = 0.5  # task-level outcome correlation between the two actions


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _lognormal_tokens(mean: float, cv: float, z: float) -> int:
    sigma = math.sqrt(math.log(1.0 + cv * cv))
    return max(1, round(math.exp(math.log(mean) - sigma * sigma / 2.0 + sigma * z)))


def make_world(
    families: tuple[FamilySpec, ...], n: int, seed: int, tag: str = "w"
) -> list[TaskOutcome]:
    """`n` full-information tasks (both actions' outcomes), deterministic in (seed, tag)."""
    label = f"{tag}{seed}"
    actions = [
        CandidateFeature(key=PLANNING_KEY, strategy=Strategy.GENERAL_PLANNING),
        CandidateFeature(key=CAP_KEY, strategy=Strategy.DIRECT_CAPABILITY, ref=CAP_REF),
    ]
    out: list[TaskOutcome] = []
    for i in range(n):
        spec = families[min(int(unit(label, "fam", i) * len(families)), len(families) - 1)]
        shared = normal(label, "sh", i)
        rho = spec.rho
        u_alt = _phi(math.sqrt(rho) * shared + math.sqrt(1.0 - rho) * normal(label, "ua", i))
        u_def = _phi(math.sqrt(rho) * shared + math.sqrt(1.0 - rho) * normal(label, "ud", i))

        def status(ok: bool) -> OutcomeStatus:
            return OutcomeStatus.VERIFIED_SUCCESS if ok else OutcomeStatus.FAILURE

        outcomes = {
            PLANNING_KEY: (
                status(u_alt < spec.p_alt),
                _lognormal_tokens(spec.tokens_alt, spec.cv, normal(label, "ta", i)),
            ),
            CAP_KEY: (
                status(u_def < spec.p_default),
                _lognormal_tokens(spec.tokens_default, spec.cv, normal(label, "td", i)),
            ),
        }
        out.append(TaskOutcome(f"{label}-{i}", spec.name, RISK, actions, CAP_KEY, outcomes))
    return out


def sample_prior(
    families: tuple[FamilySpec, ...],
    seed: int,
    *,
    n_pairs: int = 240,
    max_draws: int = 500,
    recorded_at_offset_hours: int = 0,
) -> tuple[PairedPrior, int]:
    """A paired prior per family from independent draws of the world. A draw is accepted when
    every family's paired lower bound clears -delta (so the controller has a prior-backed switch to
    monitor); returns the prior and the number of draws it took."""
    for draw in range(1, max_draws + 1):
        tasks: list[TaskOutcome] = []
        ok = True
        for k, fam in enumerate(families):
            part = make_world((fam,), n_pairs, seed * 1009 + draw * 17 + k, tag="p")
            wins = [(t.outcomes[PLANNING_KEY][0], t.outcomes[CAP_KEY][0]) for t in part]
            both = sum(a == b == OutcomeStatus.VERIFIED_SUCCESS for a, b in wins)
            alt_only = sum(
                a == OutcomeStatus.VERIFIED_SUCCESS and b != OutcomeStatus.VERIFIED_SUCCESS
                for a, b in wins
            )
            def_only = sum(
                b == OutcomeStatus.VERIFIED_SUCCESS and a != OutcomeStatus.VERIFIED_SUCCESS
                for a, b in wins
            )
            neither = n_pairs - both - alt_only - def_only
            lb, _ = newcombe_paired_bounds(both, alt_only, def_only, neither, 0.95)
            ok = ok and lb >= -DELTA
            tasks += part
        if ok:
            src = PairedSource(
                name=f"sim-prior-{seed}",
                arms=["A", "E"],
                splits=["VALIDATION"],
                dataset_hashes={},
                model="sim",
                runtime_version="rt-1",
                verifier_version=SIM_VERIFIER,
                n_pairs=len(tasks),
                n_unknown_dropped=0,
                n_tied_dropped=0,
                tasks_sha256=tasks_hash(tasks),
                validation_tokens=sum(tok for t in tasks for _, tok in t.outcomes.values()),
                capabilities=[CAP_REF],
                recorded_at=SIM_EPOCH - timedelta(hours=recorded_at_offset_hours),
            )
            return make_prior(tasks, [src], 30), draw
    raise RuntimeError("no resolvable prior within the draw budget")


@dataclass(frozen=True)
class Scenario:
    sid: str
    title: str
    n: int = 800
    families: tuple[FamilySpec, ...] = (FamilySpec("timeout"),)
    phases: tuple[Phase, ...] = ()
    noise_sd: float = 0.0
    token_cv: float = 0.0
    max_age_days: float | None = None
    truth_is_drift: bool = True  # False: perturbed observations are luck, not a change of the world
    drifting_family: str | None = None  # ground-truth drift restricted to this family
    stale_from: int | None = None  # evidence is materially stale from this position on

    def perturbation(self, seed: int) -> Perturbation:
        return Perturbation(
            action=PLANNING_KEY,
            seed_label=f"{self.sid}-{seed}",
            phases=self.phases,
            noise_sd=self.noise_sd,
            noise_base_p=self.families[0].p_alt,
            token_cv=self.token_cv,
        )

    def material_positions(
        self, seed: int, tol_success: float = 0.10, tol_cost: float = 0.30
    ) -> dict[str, list[bool]]:
        """Ground truth per family: is the alternative's evidence materially wrong at position i?"""
        pt = self.perturbation(seed)
        out: dict[str, list[bool]] = {}
        for fam in self.families:
            out[fam.name] = [
                (self.truth_is_drift and pt.material(i, fam.name, fam.p_alt, tol_success, tol_cost))
                or (self.stale_from is not None and i >= self.stale_from)
                for i in range(self.n)
            ]
        return out


_BOTH = (FamilySpec("timeout"), FamilySpec("pool"))
ONSET = 250

SCENARIOS: dict[str, Scenario] = {
    s.sid: s
    for s in (
        Scenario("S1", "stable"),
        Scenario("S2", "abrupt success collapse", phases=(Phase(ONSET, survival=0.35),)),
        Scenario(
            "S3",
            "gradual degradation",
            phases=(Phase(ONSET, ramp=300, survival=0.40),),
        ),
        Scenario("S4", "cost-only drift", phases=(Phase(ONSET, cost_mult=1.5),)),
        Scenario(
            "S5",
            "temporary disturbance",
            phases=(Phase(ONSET, end=400, survival=0.35),),
        ),
        Scenario("S6", "noisy environment", noise_sd=0.08, token_cv=0.7, truth_is_drift=False),
        Scenario(
            "S7",
            "context-specific drift",
            families=_BOTH,
            phases=(Phase(ONSET, survival=0.35, family="timeout"),),
            drifting_family="timeout",
        ),
        Scenario("S8", "runtime change", phases=(Phase(ONSET, runtime="rt-2"),)),
        Scenario("S8b", "evidence staleness", max_age_days=ONSET / 24.0, stale_from=ONSET),
        Scenario("S9a", "insufficient evidence, stable", n=40),
        Scenario("S9b", "insufficient evidence, drift", n=40, phases=(Phase(25, survival=0.35),)),
        Scenario(
            "S10",
            "misleading early observations",
            phases=(Phase(0, end=25, survival=0.70),),
            truth_is_drift=False,
        ),
        Scenario(
            "S10b",
            "severe early transient",
            phases=(Phase(0, end=25, survival=0.35),),
        ),
    )
}


@dataclass(frozen=True)
class Stream:
    scenario: Scenario
    seed: int
    tasks: list[TaskOutcome]
    prior: PairedPrior
    prior_draws: int
    perturbation: Perturbation
    material: dict[str, list[bool]] = field(default_factory=dict)


def build_stream(scenario: Scenario, seed: int) -> Stream:
    # evidence staleness: the evidence is `age` old at position i (1 decision = 1 hour)
    prior, draws = sample_prior(scenario.families, seed)
    tasks = make_world(scenario.families, scenario.n, seed, tag="s")
    return Stream(
        scenario,
        seed,
        tasks,
        prior,
        draws,
        scenario.perturbation(seed),
        scenario.material_positions(seed),
    )
