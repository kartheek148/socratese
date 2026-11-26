"""ee-ctl end to end against real PostgreSQL with the fake model: log -> train -> offline ->
shadow -> canary -> active -> replay -> rollback. The fake model is deterministic, so this checks
the mechanism and the gates' plumbing, not controller quality."""

import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from experience_engine import controller_cli
from experience_engine.adapters.db import tables as t
from experience_engine.bench.dataset import write_private_dataset

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2] / "benchmarks" / "v2"
TENANT = "bench"


@pytest.fixture
def cli(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):  # type: ignore[no-untyped-def]
    private = tmp_path / "private"
    write_private_dataset(private, seed=424243, dataset="v2", scale=5)
    monkeypatch.setenv("EE_EVALUATOR_ROLE", "evaluator")
    monkeypatch.setenv("EE_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("EE_ARTIFACT_ROOT", str(tmp_path / "artifacts"))

    def run(*argv: str) -> tuple[int, str]:
        capsys.readouterr()
        code = controller_cli.main(list(argv))
        return code, capsys.readouterr().out

    def live(*extra: str) -> str:
        code, out = run(
            "live", "--root", str(ROOT), "--private-root", str(private), "--portfolio", "good",
            "--llm", "fake", "--workers", "4", "--families", "pool", "timeout", *extra,
        )  # fmt: skip
        assert code == 0, out
        return out

    run.live = live  # type: ignore[attr-defined]
    return run


def states(engine: Engine) -> dict[int, str]:
    with engine.connect() as conn:
        return {
            r.version: r.state
            for r in conn.execute(
                sa.select(t.controller_policies.c.version, t.controller_policies.c.state)
            )
        }


def test_policy_reaches_active_only_through_evidence_and_can_be_rolled_back(
    cli,
    engine: Engine,  # type: ignore[no-untyped-def]
) -> None:
    out = cli.live()  # no policy yet: the rule baseline acts and its decisions are logged
    assert "active=None canary=None shadow=None" in out and "rule-capability/1: n=120" in out
    code, out = cli("train", "--kind", "cost_aware", "--n-min", "30")
    assert code == 0 and "adaptive/1" in out and "CANDIDATE" in out
    # promotion without evidence is blocked (and the database would refuse it too)
    code, _ = cli("promote", "--version", "1", "--to", "shadow")
    assert code == 1 and states(engine) == {1: "CANDIDATE"}
    code, out = cli("evaluate", "offline", "--version", "1")
    assert code == 0 and '"verdict": "PASS"' in out, out
    assert cli("promote", "--version", "1", "--to", "canary")[0] == 1  # cannot skip SHADOW
    assert cli("promote", "--version", "1", "--to", "shadow")[0] == 0

    out = cli.live()  # the candidate now decides alongside the rule baseline (acted=False)
    assert "shadow=adaptive/1" in out and "rule-capability/1: n=120" in out
    with engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(sa.func.count()).where(t.policy_decisions.c.acted.is_(False))
            ).scalar_one()
            == 120
        )
    code, out = cli("evaluate", "shadow", "--version", "1")
    assert code == 0 and '"verdict": "PASS"' in out, out
    assert cli("promote", "--version", "1", "--to", "canary")[0] == 0

    out = cli.live("--canary-fraction", "0.5")
    assert "canary=adaptive/1" in out and "adaptive/1: n=" in out
    code, out = cli("evaluate", "canary", "--version", "1")
    report = json.loads(out)
    assert code == 0 and report["verdict"] == "PASS", out
    # concurrent control only: earlier rule-baseline traffic must not be in the control arm
    m = report["metrics"]
    assert m["n_candidate"] + m["n_control"] == 120 and 30 <= m["n_control"] <= 90
    assert cli("promote", "--version", "1", "--to", "active")[0] == 0
    assert states(engine) == {1: "ACTIVE"}
    # canary evidence computed after the promotion would use the candidate as its own control
    assert cli("evaluate", "canary", "--version", "1")[0] == 2

    out = cli.live()
    assert "active=adaptive/1" in out and "adaptive/1: n=120" in out
    code, out = cli("replay", "--version", "1")
    assert (
        code == 0 and "stored decisions reproduced exactly" in out and "NOT reproducible" not in out
    )

    assert cli("rollback", "--version", "1", "--reason", "drill")[0] == 0
    assert states(engine) == {1: "ROLLED_BACK"}
    assert cli("promote", "--version", "1", "--to", "shadow")[0] == 1  # terminal
    out = cli.live()
    assert "active=None" in out and "rule-capability/1: n=120" in out  # rule baseline serves again
    code, out = cli("status")
    assert code == 0 and "ROLLED_BACK" in out and "ACTIVE->ROLLED_BACK" in out
