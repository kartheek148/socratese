"""Celery worker entrypoint, configured from the environment.

uv run celery -A experience_engine.adapters.mq.worker:app worker -P solo -Q ee.learning -l INFO
"""

from celery.signals import worker_init

from experience_engine.adapters.clock import SystemClock
from experience_engine.adapters.db.engine import InfraSettings, make_engine
from experience_engine.adapters.db.learning_store import SqlLearningStore
from experience_engine.adapters.mq.celery_app import create_celery_app, ensure_topology
from experience_engine.learning.collector import ExecutionCompletedHandler

settings = InfraSettings.from_env()
handler = ExecutionCompletedHandler(
    SqlLearningStore(make_engine(settings.database_url)), SystemClock()
)
app = create_celery_app(settings, handler)


@worker_init.connect  # type: ignore[untyped-decorator]
def _declare_topology(**_: object) -> None:
    ensure_topology(app, settings)
