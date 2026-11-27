"""ee-ctl drift-report: pre-registered acceptance and economics from `drift-sim` JSON outputs.

Reads only recorded stream results (no new randomness): the loop file (rule / M6.1 / SHADOW /
ENFORCE on identical streams) and the monitor-only file (the alternative executed at every
position). Criteria and wording follow docs/experiments/evidence-reliability-design.md section 6.3.
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from experience_engine.controller.drift_eval import (
    UTILITY_LAMBDA,
    FamilyScore,
    StreamResult,
    clopper_pearson_upper,
    quantiles,
)
from experience_engine.evaluation.paired import bootstrap_mean_ci


def _load(path: str) -> dict[str, dict[str, list[StreamResult]]]:
    by: dict[str, dict[str, list[StreamResult]]] = defaultdict(lambda: defaultdict(list))
    for d in json.loads(Path(path).read_text()):
        fams = [FamilyScore(**f) for f in d.pop("families")]
        r = StreamResult(**d, families=fams)
        by[r.sid][r.variant].append(r)
    for sid in by:
        for v in by[sid]:
            by[sid][v].sort(key=lambda r: r.seed)
    return by


def _fam(rs: list[StreamResult], family: str | None = None) -> list[FamilyScore]:
    return [f for r in rs for f in r.families if family is None or f.family == family]


def _rate(k: int, n: int) -> str:
    return f"{k}/{n} ({k / n:.1%}; exact 95% UB {clopper_pearson_upper(k, n):.1%})" if n else "n/a"


def _mark(ok: bool) -> str:
    return "**met**" if ok else "**NOT met**"


def _fa_streams(rs: list[StreamResult], family: str | None = None) -> int:
    return sum(1 for r in rs if any(f.false_alarms for f in r.families
               if family is None or f.family == family))  # fmt: skip


def _detect(rs: list[StreamResult], family: str | None = None) -> tuple[int, int, int, Any]:
    fs = [f for f in _fam(rs, family) if f.onset is not None]
    det = sum(1 for f in fs if f.first_detection is not None)
    missed = sum(1 for f in fs if f.missed)
    return len(fs), det, missed, quantiles([f.delay for f in fs if f.delay is not None])


def _u(row: list[int]) -> float:
    return row[1] - UTILITY_LAMBDA * row[2]


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def report(loop: str, monitor: str, cusum: str | None, extra: dict[str, str]) -> str:
    L = _load(loop)
    M = _load(monitor)
    out: list[str] = []
    crit: dict[str, bool] = {}

    out += ["## Pre-registered acceptance (design section 6.3)", ""]
    out += [
        "Criteria (a)-(d) are evaluated on the **monitor-only** held-out run (300 streams per "
        "scenario, seeds 7000000-7000299, the alternative executed at every position, i.e. what "
        "the monitor observes in SHADOW) because an enforcing controller stops executing a "
        "quarantined alternative and so cannot be used to measure missed detections; (e)-(f) on "
        "the controller-in-the-loop run (100 streams, seeds 7000000-7000099).",
        "",
        "| criterion | observed | result |",
        "|---|---|---|",
    ]
    s1, s6 = M["S1"]["monitor"], M["S6"]["monitor"]
    fa1, fa6 = _fa_streams(s1), _fa_streams(s6)
    crit["a"] = fa1 / len(s1) <= 0.05 and fa6 / len(s6) <= 0.05
    out.append(
        f"| (a) false alarms S1 and S6 <= 5% of streams | S1 {_rate(fa1, len(s1))}; "
        f"S6 {_rate(fa6, len(s6))} | {_mark(crit['a'])} |"
    )
    n2, d2, m2, q2 = _detect(M["S2"]["monitor"])
    n3, d3, m3, q3 = _detect(M["S3"]["monitor"])
    n4, d4, m4, q4 = _detect(M["S4"]["monitor"])
    med2 = q2[1] if q2 else float("inf")
    crit["b"] = med2 <= 100 and m2 / n2 <= 0.05 and m3 / n3 <= 0.15 and m4 / n4 <= 0.10
    out.append(
        f"| (b) S2 median delay <= 100 and missed <= 5%; S3 missed <= 15%; S4 missed <= 10% | "
        f"S2 median delay {med2:g} (IQR {q2[0]:g}-{q2[2]:g}), missed {_rate(m2, n2)}; "
        f"S3 missed {_rate(m3, n3)}, median delay {q3[1] if q3 else 'n/a'}; "
        f"S4 missed {_rate(m4, n4)}, median delay {q4[1] if q4 else 'n/a'} | {_mark(crit['b'])} |"
    )
    s7 = M["S7"]["monitor"]
    fa_pool = _fa_streams(s7, "pool")
    nt, dt, mt, qt = _detect(s7, "timeout")
    crit["c"] = fa_pool / len(s7) <= 0.05 and mt / nt <= 0.05
    out.append(
        f"| (c) S7 isolation: `pool` false alarms <= 5%, `timeout` detected (missed <= 5%) | "
        f"`pool` false alarms {_rate(fa_pool, len(s7))}; `timeout` missed {_rate(mt, nt)}, "
        f"median delay {qt[1] if qt else 'n/a'} decisions (the family sees half the traffic) | "
        f"{_mark(crit['c'])} |"
    )
    s9 = M["S9b"]["monitor"]
    s9a = M["S9a"]["monitor"]
    s10 = M["S10"]["monitor"]
    dr9 = sum(1 for r in s9 + s9a if any(f.drifted_positions for f in r.families))
    dr10 = sum(1 for r in s10 if any(f.drifted_positions for f in r.families))
    crit["d"] = dr9 == 0 and dr10 / len(s10) <= 0.05
    out.append(
        f"| (d) S9: no DRIFTED below min_samples; S10: DRIFTED <= 5% | S9 (stable + drift, "
        f"{len(s9) + len(s9a)} streams) DRIFTED in {dr9}; S10 {_rate(dr10, len(s10))} | "
        f"{_mark(crit['d'])} |"
    )
    # (e) in the loop
    s2l = L["S2"]
    d_succ = bootstrap_mean_ci(
        [a.successes - b.successes for a, b in zip(s2l["m62"], s2l["m61"], strict=True)],
        resamples=4000,
    )
    s1l = L["S1"]
    d_s1 = bootstrap_mean_ci(
        [(a.successes - b.successes) / a.n for a, b in zip(s1l["m62"], s1l["m61"], strict=True)],
        resamples=4000,
    )
    shadow_same = all(
        (a.successes, a.tokens) == (b.successes, b.tokens)
        for sid in L
        for a, b in zip(L[sid]["m62_shadow"], L[sid]["m61"], strict=True)
    )
    crit["e"] = d_succ[1] > 0 and d_s1[1] >= -0.01 and shadow_same
    out.append(
        f"| (e) ENFORCE vs M6.1: S2 verified successes CI excludes 0 (better); S1 not worse by "
        f"more than 1 point; SHADOW identical to M6.1 on every stream | S2 {d_succ[0]:+.1f} "
        f"[{d_succ[1]:+.1f}, {d_succ[2]:+.1f}] successes/stream; S1 {d_s1[0]:+.2%} "
        f"[{d_s1[1]:+.2%}, {d_s1[2]:+.2%}]; SHADOW identical in "
        f"{'every stream of every scenario' if shadow_same else 'NOT all streams'} | "
        f"{_mark(crit['e'])} |"
    )
    s5 = L["S5"]["m62"]
    f5 = [f for r in s5 for f in r.families if f.onset is not None]
    rec = sum(1 for f in f5 if f.recovered_at is not None)
    ep1 = _mean([sum(f.quarantine_episodes for f in r.families) for r in L["S1"]["m62"]])
    ep6 = _mean([sum(f.quarantine_episodes for f in r.families) for r in L["S6"]["m62"]])
    crit["f"] = rec / len(f5) >= 0.70 and ep1 <= 1 and ep6 <= 1
    out.append(
        f"| (f) S5 (temporary disturbance): >= 70% back to NORMAL by the end of the stream; "
        f"<= 1 quarantine episode per stream in S1/S6 | S5 recovered {_rate(rec, len(f5))}; "
        f"quarantine episodes per stream S1 {ep1:.2f}, S6 {ep6:.2f} | {_mark(crit['f'])} |"
    )
    out += [""]
    core = all(crit[k] for k in "abcde")
    out += [
        "Decision text as frozen: *SUPPORTED iff (a)-(e) hold; PARTIALLY SUPPORTED if (a)-(c) "
        "hold but (e) or (f) fails; NOT SUPPORTED if (a) or (b) fails.* The frozen text is "
        "ambiguous when (a)-(e) hold and only (f) fails (clause 1 says SUPPORTED, clause 2 says "
        "PARTIALLY). This was found at evaluation time; the conservative reading is adopted: a "
        "pre-registered criterion that fails prevents an unqualified SUPPORTED.",
        "",
    ]
    if not (crit["a"] and crit["b"]):
        verdict = "NOT SUPPORTED"
    elif core and crit["f"]:
        verdict = "SUPPORTED"
    elif crit["c"]:
        verdict = "PARTIALLY SUPPORTED"
    else:
        verdict = "NOT SUPPORTED"
    out += [
        f"**Hypothesis verdict: {verdict}** (criteria met: "
        f"{', '.join(k for k in 'abcdef' if crit[k]) or 'none'}; not met: "
        f"{', '.join(k for k in 'abcdef' if not crit[k]) or 'none'}).",
        "",
    ]

    # ---- non-selected detector, if provided -----------------------------------------------------
    if cusum:
        C = _load(cusum)
        out += [
            "## Non-selected comparison: CUSUM (h=10) versus the frozen WINDOW detector",
            "",
            "Monitor-only, same held-out streams. CUSUM had no feasible calibration point, so it "
            "is reported for comparison only.",
            "",
            "| scenario | WINDOW: false-alarm streams / detected / median delay | "
            "CUSUM: false-alarm streams / detected / median delay |",
            "|---|---|---|",
        ]
        for sid in ("S1", "S6", "S2", "S3", "S4", "S10"):
            w, c = M[sid]["monitor"], C[sid]["monitor"]
            nw, dw, _, qw = _detect(w)
            nc, dc, _, qc = _detect(c)
            out.append(
                f"| {sid} | {_fa_streams(w)}/{len(w)} / {dw}/{nw} / "
                f"{qw[1] if qw else 'n/a'} | {_fa_streams(c)}/{len(c)} / {dc}/{nc} / "
                f"{qc[1] if qc else 'n/a'} |"
            )
        out.append("")

    # ---- full monitor-only matrix ---------------------------------------------------------------
    out += [
        "## Monitor-only held-out detection matrix (300 streams per scenario)",
        "",
        "| scenario | false-alarm streams (exact 95% UB) | drift streams | detected | missed "
        "| detection delay median [IQR] | suspect episodes / stream | CPU per observation |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for sid in M:
        rs = M[sid]["monitor"]
        n, d, m, q = _detect(rs)
        fa = _fa_streams(rs)
        susp = _mean([float(sum(f.suspect_episodes for f in r.families)) for r in rs])
        cpu = _mean([r.cpu_s_per_observation for r in rs])
        qs = f"{q[1]:g} [{q[0]:g}, {q[2]:g}]" if q else "n/a"
        out.append(
            f"| {sid} | {_rate(fa, len(rs))} | {n} | {d} | {m} | {qs} | {susp:.1f} | "
            f"{cpu * 1e6:.0f} us |"
        )
    out.append("")

    # ---- economics ------------------------------------------------------------------------------
    lam = int(round(1 / UTILITY_LAMBDA))
    out += [
        "## Economics (controllers in the loop, per stream, mean over streams)",
        "",
        f"Utility = verified successes - tokens / {lam:,} (the raw columns are given next to it). "
        "Ground truth splits each stream's positions into *material* (the alternative's evidence "
        "is materially wrong: true success drop >= 10 points, cost rise >= 30%, or an "
        "incompatible version/age) and *non-material*. Positive costs are losses.",
        "",
        "| scenario | rule: succ / tokens | M6.1: succ / tokens | M6.2 ENFORCE: succ / tokens | "
        "fallback decisions | recovery probes (tokens) | cost of unnecessary quarantine "
        "(non-material positions, vs M6.1) | residual loss while evidence is wrong, M6.1 | "
        "residual loss while evidence is wrong, M6.2 | net utility M6.2 - M6.1 [95% CI] |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for sid, v in L.items():
        a, b = v["m62"], v["m61"]
        rule_s = _mean([r.rule_successes for r in a])
        rule_t = _mean([r.rule_tokens for r in a])
        unnec = _mean([_u(y.nonmaterial) - _u(x.nonmaterial) for x, y in zip(a, b, strict=True)])
        loss61 = _mean([_u(y.rule_material) - _u(y.material) for y in b])
        loss62 = _mean([_u(x.rule_material) - _u(x.material) for x in a])
        net = bootstrap_mean_ci(
            [
                (x.successes - x.tokens * UTILITY_LAMBDA)
                - (y.successes - y.tokens * UTILITY_LAMBDA)
                for x, y in zip(a, b, strict=True)
            ],
            resamples=2000,
        )
        out.append(
            f"| {sid} | {rule_s:.0f} / {rule_t:,.0f} | {_mean([r.successes for r in b]):.0f} / "
            f"{_mean([r.tokens for r in b]):,.0f} | {_mean([r.successes for r in a]):.0f} / "
            f"{_mean([r.tokens for r in a]):,.0f} | {_mean([r.n_fallback for r in a]):.0f} | "
            f"{_mean([r.n_probe for r in a]):.1f} ({_mean([r.probe_tokens for r in a]):,.0f}) | "
            f"{unnec:+.2f} | {loss61:+.2f} | {loss62:+.2f} | "
            f"{net[0]:+.2f} [{net[1]:+.2f}, {net[2]:+.2f}] |"
        )
    out += [
        "",
        "Cost of monitoring: **0 model tokens** (the monitor is arithmetic over stored outcomes); "
        "CPU per observation is in the detection matrix. Cost of recovery is the probe column "
        "(tokens actually spent running the alternative while it is not trusted). The residual "
        "loss columns are utility lost relative to serving the rule default throughout the "
        "positions where the evidence was materially wrong: the cost of undetected or "
        "late-detected drift (M6.1 has no monitor, so it carries the full amount).",
        "",
    ]
    out += [f"<!-- {k}: {v} -->" for k, v in extra.items()]
    return "\n".join(out) + "\n"


def register(sub: Any) -> None:
    p = sub.add_parser("drift-report", help="pre-registered acceptance and economics tables")
    p.add_argument("--loop", required=True)
    p.add_argument("--monitor", required=True)
    p.add_argument("--cusum", default=None)
    p.add_argument("--out", required=True)
    p.set_defaults(func=_cmd)


def _cmd(args: argparse.Namespace) -> int:
    text = report(args.loop, args.monitor, args.cusum, {})
    Path(args.out).write_text(text)
    print(f"wrote {args.out}")
    return 0
