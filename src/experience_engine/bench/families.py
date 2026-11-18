"""Deterministic generators for the simulated connection-pool and API-timeout debugging tasks.

Variants differ in library, config layout, key names, units and log format, which is what the
validation (new numbers) and hidden-transfer (new variants) splits vary.
"""

import hashlib
import json
import random

from experience_engine.bench.environment import EnvironmentSpec
from experience_engine.domain.task import (
    PoolCapacityCriterion,
    SuccessCriterion,
    TaskSpec,
    TimeoutCoversLatencyCriterion,
    ValueUnchangedCriterion,
)

TENANT_ID = "bench"
ALLOWED_TOOLS = ["list_files", "read_file", "set_json_value"]
POOL_VARIANTS = ("sqlalchemy", "psycopg_pool", "hikari")
TIMEOUT_VARIANTS = ("httpx", "requests", "okhttp")


def rng_for(*parts: object) -> random.Random:
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _dump(doc: object) -> str:
    return json.dumps(doc, indent=2)


def _task(
    task_id: str,
    family: str,
    objective: str,
    inputs: dict[str, object],
    criteria: list[SuccessCriterion],
) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        tenant_id=TENANT_ID,
        objective=objective,
        inputs=inputs,
        task_family=family,
        success_criteria=criteria,
        max_tokens=60_000,
        max_steps=12,
        max_tool_calls=12,
        deadline_ms=300_000,
        risk_level="low",
        allowed_tools=list(ALLOWED_TOOLS),
    )


def make_pool_task(
    task_id: str, variant: str, rng: random.Random
) -> tuple[TaskSpec, EnvironmentSpec]:
    workers = rng.choice([2, 3, 4, 6])
    threads = rng.choice([4, 5, 8])
    demand = workers * threads
    overflow = rng.choice([0, 2, 5])
    pool = rng.randint(2, max(2, demand // 3))
    db_max = demand + rng.choice([20, 40, 80])
    reserved = rng.choice([5, 10])
    max_capacity = db_max - reserved

    noise = [f"INFO request ok route=/orders id={rng.randint(1000, 9999)}" for _ in range(6)]
    if variant == "sqlalchemy":
        files = {
            "app/config.json": _dump(
                {
                    "server": {"workers": workers, "threads_per_worker": threads},
                    "database": {
                        "url": "postgresql://app@db.internal/app",
                        "pool_size": pool,
                        "max_overflow": overflow,
                        "pool_timeout_s": 30,
                    },
                }
            )
        }
        err = (
            f"ERROR sqlalchemy.exc.TimeoutError: QueuePool limit of size {pool} overflow "
            f"{overflow} reached, connection timed out, timeout 30.00"
        )
        config_path = "app/config.json"
        keys = ["database.pool_size", "database.max_overflow"]
        guards = [("server.workers", workers), ("server.threads_per_worker", threads)]
    elif variant == "psycopg_pool":
        files = {
            "app/settings.json": _dump(
                {
                    "workers": workers,
                    "threads": threads,
                    "db": {
                        "dsn": "postgresql://app@db.internal/app",
                        "min_size": 1,
                        "max_size": pool,
                        "timeout": 30,
                    },
                }
            )
        }
        err = (
            "ERROR psycopg_pool.PoolTimeout: couldn't get a connection after 30.00 sec "
            f"(pool max_size={pool})"
        )
        config_path = "app/settings.json"
        keys = ["db.max_size"]
        guards = [("workers", workers), ("threads", threads)]
    else:  # hikari: unseen library, layout and key names (transfer)
        files = {
            "config/application.json": _dump(
                {
                    "server": {"tomcat": {"threads": {"max": demand}}},
                    "spring": {
                        "datasource": {
                            "hikari": {"maximumPoolSize": pool, "connectionTimeout": 30000}
                        }
                    },
                }
            )
        }
        err = (
            "ERROR HikariPool-1 - Connection is not available, request timed out after 30000ms "
            f"(total={pool}, active={pool}, idle=0, waiting={demand - pool})"
        )
        config_path = "config/application.json"
        keys = ["spring.datasource.hikari.maximumPoolSize"]
        guards = [("server.tomcat.threads.max", demand)]

    lines = noise[:3] + [err] * 3 + noise[3:]
    files["logs/app.log"] = "\n".join(
        f"2025-03-01T10:00:{i:02d}Z {ln}" for i, ln in enumerate(lines)
    )
    files["infra/db.json"] = _dump(
        {"engine": "postgres", "max_connections": db_max, "reserved_connections": reserved}
    )

    criteria: list[SuccessCriterion] = [
        PoolCapacityCriterion(
            config_path=config_path,
            capacity_keys=keys,
            min_capacity=demand,
            max_capacity=max_capacity,
        ),
        ValueUnchangedCriterion(path="infra/db.json", key="max_connections", expected=db_max),
        ValueUnchangedCriterion(
            path="infra/db.json", key="reserved_connections", expected=reserved
        ),
    ]
    criteria += [ValueUnchangedCriterion(path=config_path, key=k, expected=v) for k, v in guards]
    objective = (
        "The service intermittently fails with database connection-pool timeouts under load. "
        "Investigate the logs and configuration and fix the application configuration so the pool "
        "can serve the service's concurrent demand without exceeding the database's usable "
        "connection limit. Files under infra/ are managed by another team and must not change; do "
        "not reduce the service's concurrency to hide the problem."
    )
    return _task(task_id, "pool", objective, {"service": "orders-api"}, criteria), EnvironmentSpec(
        files=files
    )


def make_timeout_task(
    task_id: str, variant: str, rng: random.Random
) -> tuple[TaskSpec, EnvironmentSpec]:
    base = rng.choice([400, 800, 1200])
    samples = [base + rng.randint(0, base) for _ in range(rng.randint(7, 10))]
    samples.append(base * 2 + rng.randint(500, 1500))
    rng.shuffle(samples)
    gateway_max = rng.choice([10_000, 30_000, 60_000])
    timeout0_ms = rng.choice([500, 1000])

    if variant == "httpx":
        config_path = "client/config.json"
        doc = {
            "http": {
                "base_url": "https://inventory.internal",
                "timeout_ms": timeout0_ms,
                "retries": 2,
            }
        }
        key, unit = "http.timeout_ms", 1
        err = f"ERROR httpx.ReadTimeout: timed out after {timeout0_ms} ms route=/stock"
        guards = [("http.retries", 2)]
    elif variant == "requests":
        config_path = "app/settings.json"
        doc = {
            "upstream": {
                "url": "https://inventory.internal",
                "connect_timeout_s": 1,
                "read_timeout_s": timeout0_ms / 1000,
            }
        }
        key, unit = "upstream.read_timeout_s", 1000
        err = f"ERROR requests.exceptions.ReadTimeout: read timeout={timeout0_ms / 1000}"
        guards = [("upstream.connect_timeout_s", 1)]
    else:  # okhttp: unseen library, layout and key names (transfer)
        config_path = "config/client.json"
        doc = {
            "okhttp": {"callTimeoutMillis": 0, "readTimeoutMillis": timeout0_ms, "maxRetries": 2}
        }
        key, unit = "okhttp.readTimeoutMillis", 1
        err = f"ERROR java.net.SocketTimeoutException: timeout (readTimeoutMillis={timeout0_ms})"
        guards = [("okhttp.maxRetries", 2), ("okhttp.callTimeoutMillis", 0)]

    lines = [f"INFO upstream route=/stock latency_ms={ms} status=200" for ms in samples]
    lines.insert(len(lines) // 2, err)
    lines.append(err)
    files = {
        config_path: _dump(doc),
        "logs/app.log": "\n".join(f"2025-03-01T10:00:{i:02d}Z {ln}" for i, ln in enumerate(lines)),
        "infra/gateway.json": _dump({"max_request_ms": gateway_max}),
    }
    criteria: list[SuccessCriterion] = [
        TimeoutCoversLatencyCriterion(
            config_path=config_path,
            timeout_key=key,
            unit_ms=unit,
            latency_samples_ms=samples,
            max_timeout_ms=gateway_max,
        ),
        ValueUnchangedCriterion(
            path="infra/gateway.json", key="max_request_ms", expected=gateway_max
        ),
    ]
    criteria += [ValueUnchangedCriterion(path=config_path, key=k, expected=v) for k, v in guards]
    objective = (
        "Calls to the upstream inventory API intermittently fail with client timeouts. Investigate "
        "the logs and configuration and fix the client's read timeout so legitimate slow responses "
        "succeed, without exceeding the gateway's maximum request time. Files under infra/ are "
        "managed by another team and must not change; leave the retry settings alone."
    )
    return _task(task_id, "timeout", objective, {"service": "inventory-client"}, criteria), (
        EnvironmentSpec(files=files)
    )
