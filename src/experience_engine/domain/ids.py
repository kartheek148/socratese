"""UUIDv7 identifiers (RFC 9562): 48-bit unix-ms timestamp + random bits.

IDs are time-ordered at millisecond granularity; ordering within one millisecond is not defined.
"""

import os
import time
import uuid


def new_id(now_ms: int | None = None) -> str:
    ts = (time.time_ns() // 1_000_000) if now_ms is None else now_ms
    rand = int.from_bytes(os.urandom(10), "big") & ((1 << 74) - 1)
    rand_a = rand >> 62
    rand_b = rand & ((1 << 62) - 1)
    value = (ts & ((1 << 48) - 1)) << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b
    return str(uuid.UUID(int=value))
