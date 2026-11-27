"""Synthetic, labelled perturbations of the alternative's observed outcomes (M6.2 experiments).

A perturbation is applied at stream *position* i to the outcome the controller observes when it
executes `action` (the monitored alternative). The rule default's recorded outcomes are never
touched. Randomness is a hash of (seed label, position), so every controller on the same stream
sees the same perturbed draw (common random numbers) and every run replays exactly.
"""

import hashlib
import math
from dataclasses import dataclass, field


def unit(label: str, tag: str, index: int) -> float:
    digest = hashlib.sha256(f"{label}|{tag}|{index}".encode()).hexdigest()
    return (int(digest[:12], 16) + 0.5) / float(16**12)


def normal(label: str, tag: str, index: int) -> float:
    """Standard normal from two hash-derived uniforms (Box-Muller)."""
    u1, u2 = unit(label, tag + "1", index), unit(label, tag + "2", index)
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


@dataclass(frozen=True)
class Phase:
    """A change of the alternative's behaviour over [start, end) (end None: to the end)."""

    start: int
    end: int | None = None
    ramp: int = 0  # linear build-up over this many positions (gradual drift)
    survival: float = 1.0  # a success survives with this probability at full effect
    cost_mult: float = 1.0  # token multiplier at full effect
    runtime: str | None = None  # runtime label observations carry while active
    family: str | None = None  # None: every family

    def effect(self, i: int, family: str | None) -> float:
        if self.family is not None and family != self.family:
            return 0.0
        if i < self.start or (self.end is not None and i >= self.end):
            return 0.0
        if self.ramp <= 0:
            return 1.0
        return min(1.0, (i - self.start + 1) / self.ramp)


@dataclass(frozen=True)
class Perturbation:
    action: str
    seed_label: str
    phases: tuple[Phase, ...] = field(default_factory=tuple)
    noise_block: int = 30  # decisions per block of the random success effect
    noise_sd: float = 0.0  # sd of the per-block success-rate shift (noisy environment)
    noise_base_p: float = 0.52
    token_cv: float = 0.0  # extra multiplicative token noise
    base_runtime: str = "rt-1"

    def apply(
        self, i: int, family: str | None, success: bool, tokens: int
    ) -> tuple[bool, int, str]:
        runtime = self.base_runtime
        mult = 1.0
        for k, ph in enumerate(self.phases):
            e = ph.effect(i, family)
            if e <= 0.0:
                continue
            surv = 1.0 - e * (1.0 - ph.survival)
            if success and unit(self.seed_label, f"surv{k}", i) >= surv:
                success = False
            mult *= 1.0 + e * (ph.cost_mult - 1.0)
            if ph.runtime is not None:
                runtime = ph.runtime
        if self.noise_sd > 0.0:
            b = self.noise_sd * normal(self.seed_label, "blk", i // max(1, self.noise_block))
            p0 = self.noise_base_p
            if b < 0 and success and unit(self.seed_label, "flip", i) < -b / p0:
                success = False
            elif b > 0 and not success and unit(self.seed_label, "flip", i) < b / (1.0 - p0):
                success = True
        if self.token_cv > 0.0:
            sigma = math.sqrt(math.log(1.0 + self.token_cv**2))
            mult *= math.exp(sigma * normal(self.seed_label, "tok", i) - sigma * sigma / 2.0)
        return success, max(1, round(tokens * mult)), runtime

    def material(
        self, i: int, family: str | None, p_alt: float, tol_success: float, tol_cost: float
    ) -> bool:
        """Ground truth: is the evidence materially wrong at position i (for the alternative)?"""
        for ph in self.phases:
            e = ph.effect(i, family)
            if e <= 0.0:
                continue
            drop = p_alt * e * (1.0 - ph.survival)
            if drop >= tol_success or e * (ph.cost_mult - 1.0) >= tol_cost or ph.runtime:
                return True
        return False
