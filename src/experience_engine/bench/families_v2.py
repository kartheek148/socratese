"""Harder generators (dataset 2.x): the answer is no longer readable from one file.

Same tools, same verifier and same task families as v1, so everything learned or evaluated stays
comparable; what changes is how much evidence has to be combined before editing:

* pool ("shared database"): the real demand is workers x threads x connections-per-request, and
  that last factor is only in the request log; the usable ceiling is what remains of the database
  after reserved connections and the other services, divided among this service's replicas, and
  that is only in `infra/db.json`. Sizing from the config alone, or against the whole database,
  fails.
* timeout ("retry budget"): the gateway's request budget *includes retries*, so the timeout is
  capped at budget / (retries + 1); the log mixes units and contains slow calls of a different
  client that must not be used for sizing.

Every number the verifier needs is derivable from the files; `tests/unit/test_dataset_v2.py` proves
that with a reference solver that reads only the environment, never the criteria.
"""

import random

from experience_engine.bench.environment import EnvironmentSpec
from experience_engine.bench.families import ALLOWED_TOOLS, TENANT_ID, _dump
from experience_engine.domain.task import (
    PoolCapacityCriterion,
    SuccessCriterion,
    TaskSpec,
    TimeoutCoversLatencyCriterion,
    ValueUnchangedCriterion,
)

POOL_VARIANTS_V2 = ("sqlalchemy_shared", "psycopg_shared", "hikari_shared")
TIMEOUT_VARIANTS_V2 = ("httpx_budget", "requests_budget", "okhttp_budget")


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


def _stamp(lines: list[str]) -> str:
    return "\n".join(f"2025-03-01T10:00:{i:02d}Z {ln}" for i, ln in enumerate(lines))


def make_shared_pool_task(
    task_id: str, variant: str, rng: random.Random
) -> tuple[TaskSpec, EnvironmentSpec]:
    base = variant.split("_", 1)[0]
    workers = rng.choice([2, 3, 4])
    threads = rng.choice([3, 4, 5])
    per_request = rng.choice([2, 3])  # connections one request holds at once
    demand = workers * threads * per_request
    overflow = rng.choice([0, 2, 4])
    pool = rng.randint(2, max(2, workers * threads - 1))  # undersized even for one conn/request
    replicas = rng.choice([2, 3, 4])
    other = {"billing": rng.choice([10, 15, 20]), "reports": rng.choice([5, 10])}
    reserved = rng.choice([5, 10])
    ceiling = demand + rng.randint(2, 8)  # per-replica capacity the database can afford
    usable = ceiling * replicas + rng.randint(0, replicas - 1)
    db_max = usable + reserved + sum(other.values())

    err_core = {
        "sqlalchemy": f"sqlalchemy.exc.TimeoutError: QueuePool limit of size {pool} overflow "
        f"{overflow} reached, connection timed out, timeout 30.00",
        "psycopg": f"psycopg_pool.PoolTimeout: couldn't get a connection after 30.00 sec "
        f"(pool max_size={pool})",
        "hikari": f"HikariPool-1 - Connection is not available, request timed out after 30000ms "
        f"(total={pool}, active={pool}, idle=0)",
    }[base]
    if base == "sqlalchemy":
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
        config_path, keys = "app/config.json", ["database.pool_size", "database.max_overflow"]
        guards: list[tuple[str, object]] = [
            ("server.workers", workers),
            ("server.threads_per_worker", threads),
        ]
    elif base == "psycopg":
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
        config_path, keys = "app/settings.json", ["db.max_size"]
        guards = [("workers", workers), ("threads", threads)]
    else:  # hikari: unseen library, layout and key names (transfer)
        files = {
            "config/application.json": _dump(
                {
                    "server": {"tomcat": {"threads": {"max": workers * threads}}},
                    "spring": {
                        "datasource": {
                            "hikari": {"maximumPoolSize": pool, "connectionTimeout": 30000}
                        }
                    },
                }
            )
        }
        config_path, keys = "config/application.json", ["spring.datasource.hikari.maximumPoolSize"]
        guards = [("server.tomcat.threads.max", workers * threads)]

    noise = [
        f"INFO request ok route=/orders id={rng.randint(1000, 9999)} db_connections={per_request}"
        for _ in range(6)
    ]
    lines = noise[:3] + [f"ERROR {err_core}"] * 3 + noise[3:]
    files["logs/app.log"] = _stamp(lines)
    infra = {
        "engine": "postgres",
        "max_connections": db_max,
        "reserved_connections": reserved,
        "service_replicas": replicas,
        "other_services_connections": other,
    }
    files["infra/db.json"] = _dump(infra)

    criteria: list[SuccessCriterion] = [
        PoolCapacityCriterion(
            config_path=config_path,
            capacity_keys=keys,
            min_capacity=demand,
            max_capacity=ceiling,
        ),
        ValueUnchangedCriterion(path="infra/db.json", key="max_connections", expected=db_max),
        ValueUnchangedCriterion(
            path="infra/db.json", key="reserved_connections", expected=reserved
        ),
        ValueUnchangedCriterion(path="infra/db.json", key="service_replicas", expected=replicas),
        *[
            ValueUnchangedCriterion(
                path="infra/db.json", key=f"other_services_connections.{k}", expected=v
            )
            for k, v in other.items()
        ],
        *[ValueUnchangedCriterion(path=config_path, key=k, expected=v) for k, v in guards],
    ]
    objective = (
        "The service intermittently fails with database connection-pool timeouts under load. "
        "Each running replica of the service has its own connection pool, and the database is "
        "shared with other services. Investigate the logs and configuration and fix this "
        "service's pool size so one replica's pool can serve that replica's concurrent demand "
        "(including every connection a request holds at once) while all replicas together stay "
        "within what the database can give this service. Files under infra/ are managed by "
        "another team and must not change; do not reduce the service's concurrency to hide the "
        "problem."
    )
    return _task(task_id, "pool", objective, {"service": "orders-api"}, criteria), EnvironmentSpec(
        files=files
    )


def make_budget_timeout_task(
    task_id: str, variant: str, rng: random.Random
) -> tuple[TaskSpec, EnvironmentSpec]:
    base = variant.split("_", 1)[0]
    slow = rng.choice([400, 800, 1200])
    samples = [slow + rng.randint(0, slow) for _ in range(rng.randint(6, 9))]
    samples.append(slow * 2 + rng.randint(300, 900))
    rng.shuffle(samples)
    need = max(samples)
    retries = rng.choice([1, 2, 3])
    ratio = 1.5  # policy: a client timeout may be at most ratio x the slowest legitimate call
    cap_per_attempt = int(need * rng.uniform(1.15, 2.2))
    budget = cap_per_attempt * (retries + 1) + rng.randint(0, retries)
    max_timeout = min(budget // (retries + 1), int(need * ratio))
    timeout0_ms = rng.choice([500, 1000])
    decoy = rng.randint(20_000, 60_000)  # another client's batch export: not this client's latency

    if base == "httpx":
        config_path = "client/config.json"
        doc: dict[str, object] = {
            "http": {
                "base_url": "https://inventory.internal",
                "timeout_ms": timeout0_ms,
                "retries": retries,
            }
        }
        key, unit, retry_key = "http.timeout_ms", 1, "http.retries"
        err = f"ERROR httpx.ReadTimeout: timed out after {timeout0_ms} ms route=/stock"
        guards: list[tuple[str, object]] = [(retry_key, retries)]
    elif base == "requests":
        config_path = "app/settings.json"
        doc = {
            "upstream": {
                "url": "https://inventory.internal",
                "connect_timeout_s": 1,
                "read_timeout_s": timeout0_ms / 1000,
                "retries": retries,
            }
        }
        key, unit = "upstream.read_timeout_s", 1000
        err = f"ERROR requests.exceptions.ReadTimeout: read timeout={timeout0_ms / 1000}"
        guards = [("upstream.connect_timeout_s", 1), ("upstream.retries", retries)]
    else:  # okhttp: unseen library, layout and key names (transfer)
        config_path = "config/client.json"
        doc = {
            "okhttp": {
                "callTimeoutMillis": 0,
                "readTimeoutMillis": timeout0_ms,
                "maxRetries": retries,
            }
        }
        key, unit = "okhttp.readTimeoutMillis", 1
        err = f"ERROR java.net.SocketTimeoutException: timeout (readTimeoutMillis={timeout0_ms})"
        guards = [("okhttp.maxRetries", retries), ("okhttp.callTimeoutMillis", 0)]

    lines = []
    for i, ms in enumerate(samples):  # units alternate: the same latency is never written twice
        if i % 3 == 2:
            lines.append(
                f"INFO upstream client=inventory route=/stock took={ms / 1000:.3f}s status=200"
            )
        else:
            lines.append(f"INFO upstream client=inventory route=/stock latency_ms={ms} status=200")
    lines.insert(
        1, f"INFO upstream client=reports-batch route=/export latency_ms={decoy} status=200"
    )
    lines.insert(len(lines) // 2, err)
    lines.append(err)
    files = {
        config_path: _dump(doc),
        "logs/app.log": _stamp(lines),
        "infra/gateway.json": _dump(
            {
                "max_request_ms": budget,
                "budget_includes_retries": True,
                "max_timeout_to_slowest_call_ratio": ratio,
            }
        ),
    }
    criteria: list[SuccessCriterion] = [
        TimeoutCoversLatencyCriterion(
            config_path=config_path,
            timeout_key=key,
            unit_ms=unit,
            latency_samples_ms=samples,
            max_timeout_ms=max_timeout,
        ),
        ValueUnchangedCriterion(path="infra/gateway.json", key="max_request_ms", expected=budget),
        ValueUnchangedCriterion(
            path="infra/gateway.json", key="budget_includes_retries", expected=True
        ),
        ValueUnchangedCriterion(
            path="infra/gateway.json", key="max_timeout_to_slowest_call_ratio", expected=ratio
        ),
        *[ValueUnchangedCriterion(path=config_path, key=k, expected=v) for k, v in guards],
    ]
    objective = (
        "Calls from the inventory client to the upstream inventory API intermittently fail with "
        "client timeouts. Investigate the logs and configuration and fix the client's read "
        "timeout so every legitimate slow inventory response succeeds, while a request that is "
        "retried still finishes within the gateway's request budget and the client stays within "
        "the platform's timeout policy. Files under infra/ are managed by another team and must "
        "not change; leave the retry settings alone."
    )
    return _task(task_id, "timeout", objective, {"service": "inventory-client"}, criteria), (
        EnvironmentSpec(files=files)
    )
