"""ee-policy: export recorded evaluations and run the policy-confirmation analysis.

  ee-policy collect --tenant T --out reports.json     (EE_DATABASE_URL, EE_ARTIFACT_ROOT)
  ee-policy confirm --set A reports_a.json results_a.json --set B ... --out report.md

`collect` reads the stored evaluation reports (with their per-task rows) from the artifact store;
`confirm` re-reads nothing from the database and decides nothing: it analyses recorded data.
"""

import argparse
import json
import sys
from pathlib import Path

import sqlalchemy as sa

from experience_engine.adapters.artifacts.local_fs import LocalFsArtifactStore
from experience_engine.adapters.db import tables as t
from experience_engine.adapters.db.engine import InfraSettings, make_engine
from experience_engine.adapters.db.experience_store import SqlExperienceStore
from experience_engine.bench.dataset import EvaluatorAccess, SplitAccessError
from experience_engine.domain.evaluation import EvaluationReport
from experience_engine.domain.experiment import ExperimentResult
from experience_engine.evaluation.policy_confirmation_report import SetInputs, render_confirmation
from experience_engine.experiment_cli import load_discovery_experience


def _collect(args: argparse.Namespace) -> int:
    infra = InfraSettings.from_env()
    engine = make_engine(infra.database_url)
    artifacts = LocalFsArtifactStore(infra.artifact_root)
    reports: list[EvaluationReport] = []
    with engine.connect() as conn:
        for table, id_col in (
            (t.memory_evaluations, "memory_id"),
            (t.capability_evaluations, "capability_id"),
        ):
            latest: dict[tuple[str, int], tuple[object, str]] = {}
            for row in conn.execute(
                sa.select(
                    table.c[id_col], table.c.version, table.c.report_sha256, table.c.completed_at
                )
                .where(table.c.tenant_id == args.tenant)
                .order_by(table.c.completed_at)
            ).all():
                latest[(row[0], row[1])] = (
                    row[3],
                    row[2],
                )  # keep the newest evaluation per version
            for _key, (_when, sha) in sorted(latest.items(), key=lambda kv: kv[0]):
                reports.append(
                    EvaluationReport.model_validate_json(artifacts.get(args.tenant, sha))
                )
    trajectories = load_discovery_experience(SqlExperienceStore(engine), artifacts, args.tenant)
    payload = {
        "discovery_tokens": sum(x.tokens_used for x in trajectories),
        "reports": [r.model_dump(mode="json") for r in reports],
    }
    Path(args.out).write_text(json.dumps(payload, sort_keys=True))
    print(f"wrote {args.out}: {len(reports)} evaluation reports")
    return 0


def _confirm(args: argparse.Namespace) -> int:
    sets = []
    for name, reports_path, results_path in args.set:
        data = json.loads(Path(reports_path).read_text())
        result = ExperimentResult.model_validate_json(Path(results_path).read_text())
        sets.append(
            SetInputs(
                name=name,
                reports=[EvaluationReport.model_validate(x) for x in data["reports"]],
                rows=result.rows,
                discovery_tokens=int(data["discovery_tokens"]),
            )
        )
    Path(args.out).write_text(render_confirmation(sets, args.title))
    print(f"wrote {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ee-policy")
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--tenant", required=True)
    c.add_argument("--out", required=True)
    c.set_defaults(func=_collect)
    k = sub.add_parser("confirm")
    k.add_argument(
        "--set", nargs=3, action="append", metavar=("NAME", "REPORTS", "RESULTS"), required=True
    )
    k.add_argument("--out", required=True)
    k.add_argument("--title", default="M5.9 fresh confirmation of the candidate promotion policy")
    k.set_defaults(func=_confirm)
    args = parser.parse_args(argv)
    try:
        EvaluatorAccess.from_env()
    except SplitAccessError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
