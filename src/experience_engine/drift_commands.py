"""ee-ctl drift commands (M6.2): calibration, scenario simulation and acceptance evaluation.

Registered on the existing `ee-ctl` parser (`register`); no separate CLI. The database-backed
drift commands (evaluate / status / replay / shadow-report / evidence inspect) live in
`drift_db_commands.py`.
"""

import argparse
import json
import multiprocessing
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from experience_engine.controller.drift_eval import (
    VARIANTS,
    FamilyScore,
    GridPoint,
    StreamResult,
    calibration_task,
    point_summary,
    result_to_json,
    run_stream_variants,
    summarize,
)
from experience_engine.controller.scenarios import SCENARIOS
from experience_engine.domain.drift import DriftParams
from experience_engine.evaluation.paired import bootstrap_mean_ci


def _pool(workers: int) -> Any:
    return multiprocessing.get_context("fork").Pool(max(1, workers))


def _base_params(args: argparse.Namespace) -> DriftParams:
    update: dict[str, Any] = {}
    for name in ("detector", "confidence", "persistence"):
        v = getattr(args, name, None)
        if v is not None:
            update[name] = v
    if getattr(args, "threshold", None) is not None:
        update["cusum_threshold"] = args.threshold
    return DriftParams().model_copy(update=update)


# ---- calibration --------------------------------------------------------------------------------
def _cal_worker(
    job: tuple[str, int, list[GridPoint], str],
) -> tuple[str, dict[GridPoint, FamilyScore]]:
    sid, seed, grid, base_json = job
    return sid, calibration_task(sid, seed, grid, DriftParams.model_validate_json(base_json))


CAL_SCENARIOS = ("S1", "S6", "S9a", "S10", "S2", "S3")
FA_LIMIT = 0.03
DETECTION_FLOOR = 0.90
CUSUM_ADVANTAGE = 0.25


def _calibrate(
    seeds: Sequence[int], grid: Sequence[GridPoint], base: DriftParams, workers: int
) -> dict[tuple[str, GridPoint], dict[str, Any]]:
    jobs = [(sid, s, list(grid), base.model_dump_json()) for sid in CAL_SCENARIOS for s in seeds]
    by: dict[tuple[str, GridPoint], list[FamilyScore]] = defaultdict(list)
    with _pool(workers) as pool:
        for sid, scores in pool.imap_unordered(_cal_worker, jobs, chunksize=4):
            for g, sc in scores.items():
                by[(sid, g)].append(sc)
    return {k: point_summary(v) for k, v in by.items()}


def _feasible(table: dict[tuple[str, GridPoint], dict[str, Any]], g: GridPoint) -> bool:
    def get(sid: str) -> dict[str, Any]:
        return table[(sid, g)]

    return (
        get("S1")["false_alarm"] <= FA_LIMIT
        and get("S6")["false_alarm"] <= FA_LIMIT
        and get("S9a")["drifted"] <= 0.05
        and get("S10")["drifted"] <= 0.05
        and (get("S2")["detected"] or 0.0) >= DETECTION_FLOOR
        and (get("S3")["detected"] or 0.0) >= DETECTION_FLOOR
    )


def _score(table: dict[tuple[str, GridPoint], dict[str, Any]], g: GridPoint) -> float:
    d2, d3 = table[("S2", g)]["median_delay"], table[("S3", g)]["median_delay"]
    return (float(d2) + float(d3)) / 2.0


def _cmd_calibrate(args: argparse.Namespace) -> int:
    seeds = list(range(args.seed_start, args.seed_start + args.seeds))
    base = DriftParams()
    window_grid = [GridPoint("WINDOW", c, p) for c in args.confidences for p in args.persistences]
    table = _calibrate(seeds, window_grid, base, args.workers)
    feasible_w = [g for g in window_grid if _feasible(table, g)]
    if feasible_w:
        best_w = min(feasible_w, key=lambda g: _score(table, g))
        note_w = "selected: the feasible point with the smallest mean median delay on S2 and S3"
    else:
        best_w = GridPoint("WINDOW", max(args.confidences), max(args.persistences))
        note_w = "NO feasible point: the most conservative grid point is frozen (errata E4)"
    cusum_grid = [
        GridPoint("CUSUM", best_w.confidence, best_w.persistence, h) for h in args.thresholds
    ]
    table.update(_calibrate(seeds, cusum_grid, base, args.workers))
    feasible_c = [g for g in cusum_grid if _feasible(table, g)]
    best_c = min(feasible_c, key=lambda g: _score(table, g)) if feasible_c else None
    chosen, why = best_w, "WINDOW (simplest) unless CUSUM is feasible and clearly faster"
    if best_c is not None and feasible_w:
        w2, w3 = table[("S2", best_w)]["median_delay"], table[("S3", best_w)]["median_delay"]
        c2, c3 = table[("S2", best_c)]["median_delay"], table[("S3", best_c)]["median_delay"]
        if c2 <= (1 - CUSUM_ADVANTAGE) * w2 and c3 <= (1 - CUSUM_ADVANTAGE) * w3:
            chosen, why = best_c, "CUSUM: at least 25% shorter median delay on both S2 and S3"
    lines = [
        "# M6.2 calibration on development streams (generated)",
        "",
        f"Development seeds {seeds[0]}..{seeds[-1]} ({len(seeds)} streams per scenario), "
        "monitor-only (the alternative executes at every position), first family. Feasible = "
        f"false alarm ≤ {FA_LIMIT:.0%} in S1 and S6, DRIFTED in ≤ 5% of S9a and S10 streams, "
        f"S2 and S3 detected in ≥ {DETECTION_FLOOR:.0%}.",
        "",
        "| detector | S1 FA | S6 FA | S9a DRIFTED | S10 DRIFTED | S2 detected | S2 median delay "
        "| S3 detected | S3 median delay | feasible |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]

    def fmt(x: Any, pct: bool = True) -> str:
        if x is None:
            return "n/a"
        return f"{x:.1%}" if pct else f"{x:g}"

    for g in [*window_grid, *cusum_grid]:
        t = {sid: table[(sid, g)] for sid in CAL_SCENARIOS}
        lines.append(
            f"| {g.label()} | {fmt(t['S1']['false_alarm'])} | {fmt(t['S6']['false_alarm'])} | "
            f"{fmt(t['S9a']['drifted'])} | {fmt(t['S10']['drifted'])} | "
            f"{fmt(t['S2']['detected'])} | "
            f"{fmt(t['S2']['median_delay'], False)} | {fmt(t['S3']['detected'])} | "
            f"{fmt(t['S3']['median_delay'], False)} | {'yes' if _feasible(table, g) else 'no'} |"
        )
    lines += [
        "",
        f"WINDOW: {best_w.label()} - {note_w}.",
        f"CUSUM: {best_c.label() if best_c else 'no feasible point'}.",
        f"**Frozen detector: {chosen.label()}** ({why}).",
        "",
    ]
    Path(args.out).write_text("\n".join(lines))
    print("\n".join(lines[-4:]))
    return 0


# ---- scenario simulation ------------------------------------------------------------------------
def _sim_worker(job: tuple[str, int, str, list[str]]) -> list[dict[str, Any]]:
    sid, seed, base_json, variants = job
    base = DriftParams.model_validate_json(base_json)
    return [result_to_json(r) for r in run_stream_variants(SCENARIOS[sid], seed, base, variants)]


def _from_json(d: dict[str, Any]) -> StreamResult:
    fams = [FamilyScore(**f) for f in d.pop("families")]
    return StreamResult(**d, families=fams)


def _fmt_q(q: tuple[float, float, float] | None) -> str:
    return "n/a" if q is None else f"{q[1]:g} [{q[0]:g}, {q[2]:g}]"


def _num(v: float, kind: str) -> str:
    if kind == "int":
        return f"{v:+,.0f}"
    if kind == "dec":
        return f"{v:+.4f}"
    return f"{v:+.1%}"


def _ci(t: tuple[float, float, float], kind: str = "int") -> str:
    return f"{_num(t[0], kind)} [{_num(t[1], kind)}, {_num(t[2], kind)}]"


def render_sim(by: dict[str, dict[str, list[StreamResult]]], title: str, preamble: str) -> str:
    out = [f"# {title}", "", preamble, ""]
    for sid, variants in by.items():
        sc = SCENARIOS[sid]
        out += [f"## {sid} - {sc.title}", ""]
        out += [
            f"{len(next(iter(variants.values())))} streams of {sc.n} decisions "
            f"({len(sc.families)} famil{'y' if len(sc.families) == 1 else 'ies'}).",
            "",
            "| variant | success [95% CI] | tokens / verified success | false-alarm streams "
            "(exact 95% UB) | detected / drift | missed | detection delay median [IQR] "
            "| recovered | recovery delay median [IQR] | suspect episodes / stream |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for v, rs in variants.items():
            s = summarize(rs, sc)
            ci = s["success_rate_ci"]
            out.append(
                f"| {v} | {s['success_rate']:.1%} [{ci[1]:.1%}, {ci[2]:.1%}] | "
                f"{s['tokens_per_success']:,.0f} | {s['false_alarm_streams']}/{s['n_streams']} "
                f"({s['false_alarm_ub95']:.1%}) | {s['detected']}/{s['drift_streams']} | "
                f"{s['missed']} | {_fmt_q(s['delay_quartiles'])} | "
                f"{s['recovered']} | {_fmt_q(s['recovery_delay_quartiles'])} | "
                f"{s['suspect_episodes_per_stream']:.1f} |"
            )
        out.append("")
        if "m62" in variants and "m61" in variants:
            a, b = variants["m62"], variants["m61"]
            out += [
                "Controller comparison on identical streams (paired by seed, 95% bootstrap CI of "
                "the mean per-stream difference):",
                "",
                "| comparison | verified successes / stream | tokens / stream | utility / task "
                "(success - tokens / 20,000) |",
                "|---|---|---|---|",
            ]
            for name, x, y in (("M6.2 ENFORCE - M6.1", a, b), ("M6.2 ENFORCE - rule", a, None)):
                if y is None:
                    ys_s = [r.rule_successes for r in x]
                    ys_t = [r.rule_tokens for r in x]
                else:
                    ys_s = [r.successes for r in y]
                    ys_t = [r.tokens for r in y]
                ds = bootstrap_mean_ci(
                    [p.successes - q for p, q in zip(x, ys_s, strict=True)], resamples=2000
                )
                dt = bootstrap_mean_ci(
                    [p.tokens - q for p, q in zip(x, ys_t, strict=True)], resamples=2000
                )
                du = bootstrap_mean_ci(
                    [
                        (p.successes - qs) / p.n - (p.tokens - qt) / (20000.0 * p.n)
                        for p, qs, qt in zip(x, ys_s, ys_t, strict=True)
                    ],
                    resamples=2000,
                )
                out.append(f"| {name} | {_ci(ds)} | {_ci(dt)} | {_ci(du, 'dec')} |")
            shadow = variants.get("m62_shadow", [])
            same = sum(
                1
                for p, q in zip(shadow, b, strict=True)
                if (p.successes, p.tokens) == (q.successes, q.tokens)
            )
            out += [
                "",
                f"SHADOW equals M6.1 (same successes and tokens) in {same}/{len(shadow)} streams. "
                f"M6.2 ENFORCE per stream: {sum(r.n_fallback for r in a) / len(a):.0f} fallback "
                f"decisions, {sum(r.n_probe for r in a) / len(a):.1f} recovery probes "
                f"({sum(r.probe_tokens for r in a) / len(a):,.0f} probe tokens), "
                f"{sum(r.transitions for r in a) / len(a):.1f} transitions.",
                "",
            ]
    return "\n".join(out) + "\n"


def _cmd_sim(args: argparse.Namespace) -> int:
    seeds = list(range(args.seed_start, args.seed_start + args.seeds))
    base = _base_params(args)
    variants = list(args.variants)
    jobs = [(sid, s, base.model_dump_json(), variants) for sid in args.scenarios for s in seeds]
    by: dict[str, dict[str, list[StreamResult]]] = {
        sid: {v: [] for v in variants} for sid in args.scenarios
    }
    raw: list[dict[str, Any]] = []
    with _pool(args.workers) as pool:
        for batch in pool.imap(_sim_worker, jobs, chunksize=2):
            for d in batch:
                raw.append(d)
                r = _from_json(json.loads(json.dumps(d)))
                by[r.sid][r.variant].append(r)
    for sid in by:  # the rule baseline is the default's recorded outcomes on the same stream
        for v in by[sid]:
            by[sid][v].sort(key=lambda r: r.seed)
    text = render_sim(by, args.title, args.preamble or "")
    Path(args.out).write_text(text)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(raw))
    print(f"wrote {args.out} ({len(raw)} stream results)")
    return 0


def register(sub: Any) -> None:
    cal = sub.add_parser(
        "drift-calibrate", help="calibrate the drift monitor on development streams"
    )
    cal.add_argument("--seed-start", type=int, default=100000)
    cal.add_argument("--seeds", type=int, default=200)
    cal.add_argument("--confidences", nargs="+", type=float, default=[0.95, 0.99, 0.995, 0.999])
    cal.add_argument("--persistences", nargs="+", type=int, default=[3, 5, 10])
    cal.add_argument("--thresholds", nargs="+", type=float, default=[4.0, 6.0, 8.0, 10.0])
    cal.add_argument("--workers", type=int, default=8)
    cal.add_argument("--out", required=True)
    cal.set_defaults(func=_cmd_calibrate)

    sim = sub.add_parser(
        "drift-sim", help="frozen drift scenarios (synthetic), controllers in the loop"
    )
    sim.add_argument("--scenarios", nargs="+", default=list(SCENARIOS), choices=list(SCENARIOS))
    sim.add_argument("--seed-start", type=int, default=7000000)
    sim.add_argument("--seeds", type=int, default=100)
    sim.add_argument("--variants", nargs="+", default=["m61", "m62_shadow", "m62"],
                     choices=[*VARIANTS, "monitor"])  # fmt: skip
    sim.add_argument("--detector", choices=["WINDOW", "CUSUM"], default=None)
    sim.add_argument("--confidence", type=float, default=None)
    sim.add_argument("--persistence", type=int, default=None)
    sim.add_argument("--threshold", type=float, default=None)
    sim.add_argument("--workers", type=int, default=8)
    sim.add_argument("--title", default="M6.2 drift scenarios (generated, synthetic)")
    sim.add_argument("--preamble", default="")
    sim.add_argument("--out", required=True)
    sim.add_argument("--json-out", default=None)
    sim.set_defaults(func=_cmd_sim)
