from datetime import UTC, datetime
from time import monotonic_ns


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ms(self) -> int:
        return monotonic_ns() // 1_000_000
