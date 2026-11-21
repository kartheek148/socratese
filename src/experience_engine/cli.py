"""ee-bench entrypoint: generate datasets, run the baseline, report, compare."""

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path

from experience_engine.adapters.artifacts.local_fs import LocalFsArtifactStore
from experience_engine.adapters.clock import SystemClock
from experience_engine.adapters.db.catalog import SqlExecutableCatalog
from experience_engine.adapters.db.engine import InfraSettings, make_engine
from experience_engine.adapters.db.run_lifecycle import SqlRunLifecycle
from experience_engine.adapters.llm.decisions import LlmDecisionProvider
from experience_engine.adapters.llm.fake import HeuristicDecisionProvider, HeuristicFakeLLM
from experience_engine.adapters.llm.openai_compatible import OpenAICompatibleClient
from experience_engine.adapters.llm.vertex import (
    service_account_token_provider,
    vertex_config_from_env,
)
from experience_engine.adapters.results import JsonlResultStore
from experience_engine.bench.dataset import (
    DATASETS,
    DEFAULT_SEED,
    PRIVATE_SEED_ENV,
    DatasetLoader,
    EvaluatorAccess,
    write_dataset,
    write_private_dataset,
)
from experience_engine.bench.harness import LLMFactory, run_benchmark
from experience_engine.bench.report import (
    compare_runs,
    render_comparison,
    render_report,
    summarize,
)
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.config import LLMSettings
from experience_engine.domain.benchmark import AgentConfig, BenchmarkRunRecord, ModelSettings, Split
from experience_engine.domain.capability import EXECUTABLE_STATES, CapabilityState
from experience_engine.domain.redaction import Redactor
from experience_engine.execution.capability_runtime import CapabilityRuntime
from experience_engine.execution.controller import Controller, FixedPolicy, RuleCapabilityPolicy
from experience_engine.execution.matcher import CapabilityMatcher
from experience_engine.execution.protocol import PROMPT_VERSION
from experience_engine.ports.decisions import DecisionProvider
from experience_engine.ports.llm import LLMClient

TENANT = "bench"


def _load_records(path: Path) -> list[BenchmarkRunRecord]:
    return JsonlResultStore(path).list_runs(TENANT)


def _cmd_generate(args: argparse.Namespace) -> int:
    if args.private:
        seed_text = os.environ.get(PRIVATE_SEED_ENV)
        if not seed_text:
            print(f"set {PRIVATE_SEED_ENV} to a secret integer seed", file=sys.stderr)
            return 2
        root = Path(args.root)
        repo = Path(__file__).resolve().parents[2]
        if repo in (root.resolve(), *root.resolve().parents):
            print(
                "refusing to write private evaluation data inside the repository", file=sys.stderr
            )
            return 2
        try:
            manifest = write_private_dataset(root, int(seed_text), args.dataset)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"wrote private splits {manifest['dataset']} v{manifest['version']} to {root}")
        return 0
    manifest = write_dataset(Path(args.root), args.seed, args.dataset)
    print(f"wrote dataset {manifest['dataset']} v{manifest['version']} to {args.root}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if out.exists():
        print(f"refusing to append to existing {out}; choose a new --out", file=sys.stderr)
        return 2
    splits = list(Split) if args.split == "all" else [Split(args.split.upper())]
    loader = DatasetLoader(
        Path(args.root),
        EvaluatorAccess.from_env(),
        private_root=Path(args.private_root) if args.private_root else None,
    )

    llm_factory: LLMFactory
    known_secrets: list[str] = []
    if args.llm == "fake":
        model = ModelSettings(provider="fake", model="heuristic-fake/1", seed=args.seed)
        llm_factory = lambda _task, _seed: HeuristicFakeLLM()  # noqa: E731
    else:
        provider = ""
        try:
            if args.llm == "vertex":
                vertex = vertex_config_from_env(dict(os.environ))
                settings = vertex.settings(
                    temperature=float(os.environ.get("EE_LLM_TEMPERATURE", "0")),
                    max_output_tokens=int(os.environ.get("EE_LLM_MAX_OUTPUT_TOKENS", "0")) or None,
                )
                client = OpenAICompatibleClient(
                    settings, token_provider=service_account_token_provider(vertex.credentials_file)
                )
                provider = vertex.provider_label  # no project id in reports
            else:
                settings = LLMSettings.from_env()
                client = OpenAICompatibleClient(settings)
                provider = settings.base_url
                if settings.api_key is not None:
                    known_secrets.append(settings.api_key.get_secret_value())
        except RuntimeError as exc:
            print(f"{exc} (see docs/benchmark.md)", file=sys.stderr)
            return 2
        model = ModelSettings(
            provider=provider,
            model=settings.model,
            temperature=settings.temperature,
            seed=args.seed,
            max_output_tokens=settings.max_output_tokens,
        )
        llm_factory = lambda _task, _seed: client  # noqa: E731  (client is stateless)
    controller: Controller = FixedPolicy()
    runtime: CapabilityRuntime | None = None
    if args.capabilities:
        try:
            states = (
                {CapabilityState(x.strip().upper()) for x in args.experimental_capability_states}
                if args.experimental_capability_states
                else set(EXECUTABLE_STATES)
            )
        except ValueError as exc:
            print(f"bad capability state: {exc}", file=sys.stderr)
            return 2
        catalog = SqlExecutableCatalog(make_engine(InfraSettings.from_env().database_url), states)
        decisions_for: Callable[[LLMClient], DecisionProvider] = (
            (lambda _llm: HeuristicDecisionProvider())
            if args.llm == "fake"
            else (lambda llm: LlmDecisionProvider(llm, model))
        )
        runtime = CapabilityRuntime(
            catalog=catalog,
            matcher=CapabilityMatcher(catalog),
            decisions_for=decisions_for,
            experimental=catalog.experimental,
        )
        controller = RuleCapabilityPolicy()
        if runtime.experimental:
            print(
                "WARNING: EXPERIMENTAL capability catalog "
                f"({sorted(s.value for s in states)}): runs are tagged and are not promotion "
                "evidence",
                file=sys.stderr,
            )
    config = AgentConfig(
        config_id="C_CAPABILITIES" if runtime else "A_BASELINE",
        prompt_version=PROMPT_VERSION,
        model=model,
    )

    store = JsonlResultStore(out)
    lifecycle = artifacts = redactor = None
    if args.persist:
        infra = InfraSettings.from_env()
        lifecycle = SqlRunLifecycle(make_engine(infra.database_url))
        artifacts = LocalFsArtifactStore(infra.artifact_root)
        redactor = Redactor(known_secrets=known_secrets)
    records: list[BenchmarkRunRecord] = []
    for split in splits:
        loaded = loader.load(split)
        records += run_benchmark(
            loaded.tasks,
            dataset=loaded.version,
            seed=args.seed,
            config=config,
            llm_factory=llm_factory,
            controller=controller,
            verifier=SimulationVerifier(),
            clock=SystemClock(),
            store=store,
            lifecycle=lifecycle,
            artifacts=artifacts,
            redactor=redactor,
            capabilities=runtime,
        )
    s = summarize(records)
    print(f"{s.verified}/{s.n} verified successes; statuses={s.statuses}; results in {out}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    text = render_report(_load_records(Path(args.runs)), title=args.title)
    Path(args.out).write_text(text)
    print(f"wrote {args.out}")
    return 0


def _cmd_report_compare(args: argparse.Namespace) -> int:
    text = render_comparison(
        _load_records(Path(args.baseline)), _load_records(Path(args.candidate)), title=args.title
    )
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
    gen.add_argument(
        "--dataset",
        choices=sorted(DATASETS),
        default="v1",
        help="v1 is frozen; v2 is the harder shared-evidence benchmark (use --root benchmarks/v2)",
    )
    gen.add_argument("--seed", type=int, default=DEFAULT_SEED)
    gen.add_argument(
        "--private",
        action="store_true",
        help=f"write validation/hidden splits from the secret seed in ${PRIVATE_SEED_ENV}",
    )
    gen.set_defaults(func=_cmd_generate)

    run = sub.add_parser("run", help="run the baseline (needs EE_EVALUATOR_ROLE=evaluator)")
    run.add_argument("--root", default="benchmarks/v1")
    run.add_argument(
        "--private-root",
        default=os.environ.get("EE_PRIVATE_DATASET_ROOT"),
        help="directory holding private validation/hidden splits (outside the repository)",
    )
    run.add_argument(
        "--split", choices=["all", "discovery", "validation", "hidden_transfer"], default="all"
    )
    run.add_argument("--llm", choices=["fake", "openai", "vertex"], default="fake")
    run.add_argument("--seed", type=int, default=0)
    run.add_argument("--out", required=True)
    run.add_argument(
        "--persist",
        action="store_true",
        help="also persist traces, outcomes and outbox events (EE_DATABASE_URL, EE_ARTIFACT_ROOT)",
    )
    run.add_argument(
        "--capabilities",
        action="store_true",
        help="serve tasks from the capability catalog (EE_DATABASE_URL); ACTIVE versions only",
    )
    run.add_argument(
        "--experimental-capability-states",
        nargs="+",
        metavar="STATE",
        help="EXPERIMENT ONLY: also allow capabilities in these states (e.g. REJECTED); the runs "
        "are tagged and the report carries a warning",
    )
    run.set_defaults(func=_cmd_run)

    rep = sub.add_parser("report", help="render a markdown report from a run file")
    rep.add_argument("--runs", required=True)
    rep.add_argument("--out", required=True)
    rep.add_argument("--title", default="Baseline report")
    rep.set_defaults(func=_cmd_report)

    rc = sub.add_parser("report-compare", help="paired report: baseline vs capability runtime")
    rc.add_argument("--baseline", required=True)
    rc.add_argument("--candidate", required=True)
    rc.add_argument("--out", required=True)
    rc.add_argument("--title", default="Capability runtime comparison")
    rc.set_defaults(func=_cmd_report_compare)

    cmp = sub.add_parser("compare", help="compare two run files (exit 1 if they differ)")
    cmp.add_argument("a")
    cmp.add_argument("b")
    cmp.set_defaults(func=_cmd_compare)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
