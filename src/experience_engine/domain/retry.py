import random
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded exponential backoff with full jitter. attempts counts failed attempts so far."""

    max_attempts: int = 5
    base_delay_s: float = 1.0
    max_delay_s: float = 60.0

    def exhausted(self, attempts: int) -> bool:
        return attempts >= self.max_attempts

    def delay_s(self, attempts: int, rng: Callable[[], float] = random.random) -> float:
        ceiling = min(self.max_delay_s, self.base_delay_s * (2 ** max(0, attempts - 1)))
        return float(ceiling * rng())
