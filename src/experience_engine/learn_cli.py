"""ee-learn: learning-plane worker commands (never part of task execution).

  ee-learn process-jobs [--once]   build experience cases from pending learning jobs
  ee-learn compile --tenant T      compile capability CANDIDATES from eligible cases
  ee-learn candidates --tenant T   list registered candidates (read-only)

The learning plane must not hold evaluator access: it refuses to start when EE_EVALUATOR_ROLE is
set, so a process that can read validation/hidden data can never compile capabilities.
"""

import argparse
import logging
import os
import sys
import time

from experience_engine.adapters.artifacts.local_fs import LocalFsArtifactStore
from experience_engine.adapters.clock import SystemClock
from experience_engine.adapters.db.capability_store import SqlCapabilityStore
from experience_engine.adapters.db.engine import InfraSettings, make_engine
from experience_engine.adapters.db.experience_store import SqlExperienceStore
from experience_engine.adapters.db.job_queue import SqlLearningJobQueue
from experience_engine.adapters.db.reader import SqlRunReader
from experience_engine.learning.compiler import CompilerConfig
from experience_engine.learning.pipeline import (
    CandidateCompilerService,
    ExperiencePipeline,
    LearningJobProcessor,
)


class EvaluatorAccessError(RuntimeError):
    pass


def assert_no_evaluator_access(env: dict[str, str] | None = None) -> None:
    e = env if env is not None else dict(os.environ)
    if e.get("EE_EVALUATOR_ROLE"):
        raise EvaluatorAccessError(
            "the learning plane must not run with evaluator access (EE_EVALUATOR_ROLE is set)"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ee-learn")
    sub = parser.add_subparsers(dest="command", required=True)
    pj = sub.add_parser("process-jobs")
    pj.add_argument("--once", action="store_true")
    pj.add_argument("--interval", type=float, default=2.0)
    pj.add_argument("--batch", type=int, default=20)
    cp = sub.add_parser("compile")
    cp.add_argument("--tenant", required=True)
    cp.add_argument("--min-support", type=int, default=3)
    ls = sub.add_parser("candidates")
    ls.add_argument("--tenant", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    try:
        assert_no_evaluator_access()
    except EvaluatorAccessError as exc:
        print(str(exc), file=sys.stderr)
        return 3

    infra = InfraSettings.from_env()
    engine = make_engine(infra.database_url)
    artifacts = LocalFsArtifactStore(infra.artifact_root)
    store = SqlExperienceStore(engine)
    capabilities = SqlCapabilityStore(engine)
    clock = SystemClock()

    if args.command == "process-jobs":
        pipeline = ExperiencePipeline(
            reader=SqlRunReader(engine), artifacts=artifacts, store=store, clock=clock
        )
        processor = LearningJobProcessor(
            queue=SqlLearningJobQueue(engine), pipeline=pipeline, clock=clock
        )
        while True:
            res = processor.run_once(args.batch)
            print(f"succeeded={res.succeeded} retried={res.retried} failed={res.failed}")
            if args.once:
                return 0 if res.failed == 0 else 1
            time.sleep(args.interval)
    if args.command == "compile":
        service = CandidateCompilerService(
            artifacts=artifacts,
            store=store,
            capabilities=capabilities,
            clock=clock,
            config=CompilerConfig(min_support=args.min_support),
        )
        summary = service.compile(args.tenant)
        print(f"eligible_cases={summary.n_eligible_cases}")
        for r in summary.registered:
            print(
                f"{r.kind.value} {r.capability_id} v{r.version} evidence_added={r.evidence_added}"
            )
        for rej in summary.rejections:
            print(f"REJECTED family={rej.family} reason={rej.reason}")
        return 0
    for definition, state in capabilities.list_versions(args.tenant):
        print(f"{definition.id} v{definition.version} {state.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
