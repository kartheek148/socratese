"""ee-relay: publish pending outbox rows to RabbitMQ (also the reconciliation loop)."""

import argparse
import logging
import time

from experience_engine.adapters.clock import SystemClock
from experience_engine.adapters.db.engine import InfraSettings, make_engine
from experience_engine.adapters.db.learning_store import SqlLearningStore
from experience_engine.adapters.db.outbox import SqlOutbox
from experience_engine.adapters.mq.celery_app import CeleryEventPublisher, create_celery_app
from experience_engine.learning.collector import ExecutionCompletedHandler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ee-relay")
    parser.add_argument("--once", action="store_true", help="run a single relay pass and exit")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between passes")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    settings = InfraSettings.from_env()
    engine = make_engine(settings.database_url)
    app = create_celery_app(
        settings, ExecutionCompletedHandler(SqlLearningStore(engine), SystemClock())
    )
    outbox = SqlOutbox(engine)
    publisher = CeleryEventPublisher(app, settings)
    while True:
        result = outbox.relay_once(publisher)
        print(f"published={result.published} failed={result.failed} dead={result.dead_lettered}")
        if args.once:
            return 0 if result.failed == 0 else 1
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
