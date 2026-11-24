"""Dataset v3 adds one hidden-only family for *cross-family* transfer.

`queue` is structurally the same problem as `pool` - a pool of a scarce shared resource must cover
this service's real demand yet fit what is left for it - but nothing a pool capability or memory
would name literally appears here: different file layout, vocabulary, library and keys, and the
task family is not one the learning plane ever sees in discovery.

  demand  = consumer instances x concurrency per instance x channels each message holds
            (channels-per-message appears only in the log)
  ceiling = (max_channels - reserved - other services) // replicas  (only in infra/broker.json)
"""

import random

from experience_engine.bench.environment import EnvironmentSpec
from experience_engine.bench.families import _dump
from experience_engine.bench.families_v2 import _stamp, _task
from experience_engine.domain.task import (
    PoolCapacityCriterion,
    SuccessCriterion,
    TaskSpec,
    ValueUnchangedCriterion,
)

QUEUE_VARIANTS = ("amqp_channels",)


def make_queue_task(
    task_id: str, variant: str, rng: random.Random
) -> tuple[TaskSpec, EnvironmentSpec]:
    instances = rng.choice([2, 3, 4])
    concurrency = rng.choice([3, 4, 5])
    per_message = rng.choice([2, 3])
    demand = instances * concurrency * per_message
    pool = rng.randint(2, max(2, instances * concurrency - 1))
    replicas = rng.choice([2, 3, 4])
    other = {"audit": rng.choice([10, 15, 20]), "mailer": rng.choice([5, 10])}
    reserved = rng.choice([5, 10])
    ceiling = demand + rng.randint(2, 8)
    usable = ceiling * replicas + rng.randint(0, replicas - 1)
    max_channels = usable + reserved + sum(other.values())

    config_path = "messaging/consumer.json"
    files = {
        config_path: _dump(
            {
                "consumer": {"instances": instances, "concurrency": concurrency},
                "broker": {
                    "url": "amqp://worker@mq.internal/orders",
                    "channel_pool": {"size": pool, "acquire_timeout_s": 30},
                },
            }
        )
    }
    noise = [
        f"INFO message handled queue=orders id={rng.randint(1000, 9999)} "
        f"channels_per_message={per_message}"
        for _ in range(6)
    ]
    err = (
        "ERROR amqp.exceptions.ResourceError: channel pool exhausted, could not acquire a "
        f"channel within 30.00s (pool size={pool})"
    )
    files["logs/app.log"] = _stamp(noise[:3] + [err] * 3 + noise[3:])
    files["infra/broker.json"] = _dump(
        {
            "engine": "rabbitmq",
            "max_channels": max_channels,
            "reserved_channels": reserved,
            "service_replicas": replicas,
            "other_services_channels": other,
        }
    )
    criteria: list[SuccessCriterion] = [
        PoolCapacityCriterion(
            config_path=config_path,
            capacity_keys=["broker.channel_pool.size"],
            min_capacity=demand,
            max_capacity=ceiling,
        ),
        ValueUnchangedCriterion(
            path="infra/broker.json", key="max_channels", expected=max_channels
        ),
        ValueUnchangedCriterion(
            path="infra/broker.json", key="reserved_channels", expected=reserved
        ),
        ValueUnchangedCriterion(
            path="infra/broker.json", key="service_replicas", expected=replicas
        ),
        *[
            ValueUnchangedCriterion(
                path="infra/broker.json", key=f"other_services_channels.{k}", expected=v
            )
            for k, v in other.items()
        ],
        ValueUnchangedCriterion(path=config_path, key="consumer.instances", expected=instances),
        ValueUnchangedCriterion(path=config_path, key="consumer.concurrency", expected=concurrency),
    ]
    objective = (
        "The order consumer intermittently stalls because it cannot acquire a message-broker "
        "channel under load. Each running replica of the consumer has its own channel pool, and "
        "the broker is shared with other services. Investigate the logs and configuration and fix "
        "this consumer's channel pool size so one replica's pool can serve that replica's "
        "concurrent demand (including every channel a message holds at once) while all replicas "
        "together stay within what the broker can give this service. Files under infra/ are "
        "managed by another team and must not change; do not reduce the consumer's concurrency "
        "to hide the problem."
    )
    return _task(task_id, "queue", objective, {"service": "order-consumer"}, criteria), (
        EnvironmentSpec(files=files)
    )
