"""ee-bench entrypoint: generate datasets, run the baseline, report, compare."""

import argparse
import sys
from pathlib import Path

from experience_engine.adapters.artifacts.local_fs import LocalFsArtifactStore
from experience_engine.adapters.clock import SystemClock
from experience_engine.adapters.db.engine import InfraSettings, make_engine
from experience_engine.adapters.db.run_lifecycle import SqlRunLifecycle
from experience_engine.adapters.llm.fake import HeuristicFakeLLM
from experience_engine.adapters.llm.openai_compatible import OpenAICompatibleClient
from experience_engine.adapters.results import JsonlResultStore
from experience_engine.bench.dataset import (
    DEFAULT_SEED,
    DatasetLoader,
    EvaluatorAccess,
    write_dataset,
)
from experience_engine.bench.harness import LLMFactory, run_benchmark
from experience_engine.bench.report import compare_runs, render_report, summarize
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.config import LLMSettings
from experience_engine.domain.benchmark import AgentConfig, BenchmarkRunRecord, ModelSettings, Split
from experience_engine.domain.redaction import Redactor
from experience_engine.execution.controller import FixedPolicy
from experience_engine.execution.protocol import PROMPT_VERSION

TENANT = "bench"


def _load_records(path: Path) -> list[BenchmarkRunRecord]:
    return JsonlResultStore(path).list_runs(TENANT)


def _cmd_generate(args: argparse.Namespace) -> int:
    manifest = write_dataset(Path(args.root), args.seed)
    print(f"wrote dataset {manifest['dataset']} v{manifest['version']} to {args.root}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if out.exists():
        print(f"refusing to append to existing {out}; choose a new --out", file=sys.stderr)
        return 2
    splits = list(Split) if args.split == "all" else [Split(args.split.upper())]
    loader = DatasetLoader(Path(args.root), EvaluatorAccess.from_env())

    llm_factory: LLMFactory
    if args.llm == "fake":
        model = ModelSettings(provider="fake", model="heuristic-fake/1", seed=args.seed)
        llm_factory = lambda _task, _seed: HeuristicFakeLLM()  # noqa: E731
    else:
        try:
            settings = LLMSettings.from_env()
        except RuntimeError as exc:
            print(f"{exc} (see docs/benchmark.md)", file=sys.stderr)
            return 2
        client = OpenAICompatibleClient(settings)
        model = ModelSettings(
            provider=settings.base_url,
            model=settings.model,
            temperature=settings.temperature,
            seed=args.seed,
            max_output_tokens=settings.max_output_tokens,
        )
        llm_factory = lambda _task, _seed: client  # noqa: E731  (client is stateless)
    config = AgentConfig(prompt_version=PROMPT_VERSION, model=model)

    store = JsonlResultStore(out)
    lifecycle = artifacts = redactor = None
    if args.persist:
        infra = InfraSettings.from_env()
        lifecycle = SqlRunLifecycle(make_engine(infra.database_url))
        artifacts = LocalFsArtifactStore(infra.artifact_root)
        known = [settings.api_key.get_secret_value()] if args.llm == "openai" else []
        redactor = Redactor(known_secrets=known)
    records: list[BenchmarkRunRecord] = []
    for split in splits:
        loaded = loader.load(split)
        records += run_benchmark(
            loaded.tasks,
            dataset=loaded.version,
            seed=args.seed,
            config=config,
            llm_factory=llm_factory,
            controller=FixedPolicy(),
            verifier=SimulationVerifier(),
            clock=SystemClock(),
            store=store,
            lifecycle=lifecycle,
            artifacts=artifacts,
            redactor=redactor,
        )
    s = summarize(records)
    print(f"{s.verified}/{s.n} verified successes; statuses={s.statuses}; results in {out}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    text = render_report(_load_records(Path(args.runs)), title=args.title)
    Path(args.out).write_text(text)
    print(f"wrote {args.out}")
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    cmp = compare_runs(_load_records(Path(args.a)), _load_records(Path(args.b)))
    print(
        f"identical={cmp.identical} changed={cmp.changed} "
        f"only_a={cmp.only_in_a} only_b={cmp.only_in_b}"
    )
    print(
        f"A: {cmp.summary_a.verified}/{cmp.summary_a.n} verified; "
        f"B: {cmp.summary_b.verified}/{cmp.summary_b.n} verified"
    )
    return 0 if cmp.identical else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ee-bench")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="(re)generate the versioned dataset")
    gen.add_argument("--root", default="benchmarks/v1")
    gen.add_argument("--seed", type=int, default=DEFAULT_SEED)
    gen.set_defaults(func=_cmd_generate)

    run = sub.add_parser("run", help="run the baseline (needs EE_EVALUATOR_ROLE=evaluator)")
    run.add_argument("--root", default="benchmarks/v1")
    run.add_argument(
        "--split", choices=["all", "discovery", "validation", "hidden_transfer"], default="all"
    )
    run.add_argument("--llm", choices=["fake", "openai"], default="fake")
    run.add_argument("--seed", type=int, default=0)
    run.add_argument("--out", required=True)
    run.add_argument(
        "--persist",
        action="store_true",
        help="also persist traces, outcomes and outbox events (EE_DATABASE_URL, EE_ARTIFACT_ROOT)",
    )
    run.set_defaults(func=_cmd_run)

    rep = sub.add_parser("report", help="render a markdown report from a run file")
    rep.add_argument("--runs", required=True)
    rep.add_argument("--out", required=True)
    rep.add_argument("--title", default="Baseline report")
    rep.set_defaults(func=_cmd_report)

    cmp = sub.add_parser("compare", help="compare two run files (exit 1 if they differ)")
    cmp.add_argument("a")
    cmp.add_argument("b")
    cmp.set_defaults(func=_cmd_compare)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
