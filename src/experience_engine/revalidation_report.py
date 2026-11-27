"""Pre-registered acceptance and verdict for the M6.3 revalidation experiment (design §9).

Every criterion and the verdict rule are computed here from the stored per-stream results, so the
frozen text and the code cannot drift apart silently. Nothing in this module chooses a parameter.
"""

import random
import statistics
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from experience_engine.controller.drift_eval import (
    UTILITY_LAMBDA,
    clopper_pearson_upper,
    paired_diff_ci,
    quantiles,
)
from experience_engine.controller.reval_eval import RevalResult, result_from_json

STRATEGIES = ("m63_A", "m63_B", "m63_C")
HOLD = ("S2", "S4", "R1", "R2")
SHOULD_RESTORE = ("S5", "R3", "R4")
STRUCTURAL = ("S8", "S8b")
ECON = ("S5", "R3", "R4", "S8", "S8b")
BENEFIT_PER_DECISION = (0.52 - 6500 * UTILITY_LAMBDA) - (0.50 - 9000 * UTILITY_LAMBDA)


def load(path: str) -> dict[str, dict[str, list[RevalResult]]]:
    import gzip
    import json

    if path.endswith(".gz"):
        with gzip.open(path, "rt") as fh:
            raw = fh.read()
    else:
        raw = Path(path).read_text()
    by: dict[str, dict[str, list[RevalResult]]] = defaultdict(lambda: defaultdict(list))
    for d in json.loads(raw):
        r = result_from_json(d)
        by[r.sid][r.variant].append(r)
    for sid in by:
        for v in by[sid]:
            by[sid][v].sort(key=lambda r: r.seed)
    return by


def _mark(ok: bool) -> str:
    return "**met**" if ok else "**NOT met**"


def _pct(k: int, n: int) -> str:
    return f"{k}/{n} ({100 * k / n:.1f}%)" if n else "n/a"


def _ratio_ci(
    xs: Sequence[float], ys: Sequence[float], seed: int = 20260601
) -> tuple[float, float, float]:
    """Paired bootstrap over streams of sum(xs)/sum(ys)."""
    n = len(xs)
    if n == 0 or sum(ys) == 0:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)
    vals = []
    for _ in range(2000):
        idx = [rng.randrange(n) for _ in range(n)]
        den = sum(ys[i] for i in idx)
        if den > 0:
            vals.append(sum(xs[i] for i in idx) / den)
    vals.sort()
    return sum(xs) / sum(ys), vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1]


def _mean(xs: Sequence[float]) -> float:
    return statistics.fmean(xs) if xs else 0.0


def evaluate(by: dict[str, dict[str, list[RevalResult]]]) -> dict[str, Any]:
    out: dict[str, Any] = {"strategies": {}}
    n_by = {sid: len(next(iter(v.values()))) for sid, v in by.items() if v}
    m62_s5 = sum(r.restored for r in by.get("S5", {}).get("m62", []))
    for s in STRATEGIES:
        c: dict[str, Any] = {}
        hold = [r for sid in HOLD for r in by.get(sid, {}).get(s, [])]
        false = sum(1 for r in hold if r.false_restorations > 0)
        ub = clopper_pearson_upper(false, len(hold))
        r1 = by.get("R1", {}).get(s, [])
        ub_r1 = clopper_pearson_upper(sum(1 for r in r1 if r.false_restorations > 0), len(r1))
        c["a"] = {"false": false, "n": len(hold), "ub": ub, "ub_r1": ub_r1,
                  "ok": ub <= 0.05 and ub_r1 <= 0.10}  # fmt: skip
        b = {
            sid: (sum(r.restored for r in by[sid][s]), n_by[sid])
            for sid in SHOULD_RESTORE
            if s in by.get(sid, {})
        }
        c["b"] = {"by": b, "ok": all(k / n >= 0.70 for k, n in b.values()) if b else False}
        st = {
            sid: (sum(r.restored for r in by[sid][s]), n_by[sid])
            for sid in STRUCTURAL
            if s in by.get(sid, {})
        }
        c["c"] = {"by": st, "ok": all(k / n >= 0.90 for k, n in st.values()) if st else False}
        unv = sum(r.unverified_evidence_restorations for sid in by for r in by[sid][s])
        c["d"] = {"violations": unv, "ok": unv == 0}
        xs: list[float] = []
        ys: list[float] = []
        zs: list[float] = []
        for sid in ECON:
            if s in by.get(sid, {}):
                xs += [r.utility for r in by[sid][s]]
                ys += [r.utility for r in by[sid]["m62"]]
                zs += [r.utility for r in by[sid]["m62_locked"]]
        d_m62 = paired_diff_ci(xs, ys) if xs else (0.0, 0.0, 0.0)
        d_lock = paired_diff_ci(xs, zs) if xs else (0.0, 0.0, 0.0)
        c["e"] = {"vs_m62": d_m62, "vs_locked": d_lock, "ok": d_m62[1] > 0.0}
        out["strategies"][s] = c
    # (f): C against A over every scenario (revalidation tokens per stream)
    a_tok = [r.reval_tokens for sid in by for r in by[sid].get("m63_A", [])]
    c_tok = [r.reval_tokens for sid in by for r in by[sid].get("m63_C", [])]
    ratio = _ratio_ci(c_tok, a_tok)
    out["f"] = {"ratio": ratio, "ok": ratio[0] <= 0.80 and ratio[2] < 1.0
                and out["strategies"]["m63_C"]["a"]["ok"]}  # fmt: skip
    # verdict (design §9, clauses in order)
    strat = out["strategies"]
    best_s5 = max(
        (sum(r.restored for r in by.get("S5", {}).get(s, [])) for s in STRATEGIES), default=0
    )
    meets_ad = [s for s in STRATEGIES if strat[s]["a"]["ok"] and strat[s]["d"]["ok"]]
    full = [s for s in STRATEGIES if all(strat[s][k]["ok"] for k in "abcde")]
    if not any(strat[s]["a"]["ok"] for s in STRATEGIES) or best_s5 <= m62_s5:
        verdict = "NOT SUPPORTED"
    elif full:
        verdict = "SUPPORTED"
    elif any(strat[s]["b"]["ok"] or strat[s]["c"]["ok"] for s in meets_ad):
        verdict = "PARTIALLY SUPPORTED"
    else:
        verdict = "INCONCLUSIVE"
    cands = [s for s in STRATEGIES if all(strat[s][k]["ok"] for k in "abcd")]
    out["candidate"] = (
        min(cands, key=lambda s: _mean([r.reval_tokens for sid in by for r in by[sid][s]]))
        if cands
        else None
    )
    out["verdict"] = verdict
    out["m62_s5_restored"] = m62_s5
    out["best_s5_restored"] = best_s5
    return out


def _scenario_table(
    by: dict[str, dict[str, list[RevalResult]]], variants: Sequence[str]
) -> list[str]:
    cols = [
        "scenario (expect)", "variant", "restored", "false restoration", "Δ successes vs rule",
        "Δ utility vs rule [95% CI]", "revalidation tokens", "production tokens vs rule",
        "trials", "cycles started/failed", "terminal", "median delay to restoration",
    ]  # fmt: skip
    rows = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for sid in by:
        for v in variants:
            rs = by[sid].get(v, [])
            if not rs:
                continue
            n = len(rs)
            du = paired_diff_ci([r.utility for r in rs], [r.rule_utility for r in rs])
            delays = []
            for r in rs:
                ref = r.degraded_end if r.degraded_end is not None else r.first_quarantine
                if r.restorations and ref is not None:
                    delays.append(r.restorations[0] - ref)
            q = quantiles(delays)
            term: dict[str, int] = defaultdict(int)
            for r in rs:
                if r.terminal:
                    term[r.terminal] += 1
            cells = [
                f"{sid} ({rs[0].expect or '-'})",
                v,
                _pct(sum(r.restored for r in rs), n),
                _pct(sum(1 for r in rs if r.false_restorations > 0), n),
                f"{_mean([r.successes - r.rule_successes for r in rs]):+.1f}",
                f"{du[0]:+.1f} [{du[1]:+.1f}, {du[2]:+.1f}]",
                f"{_mean([r.reval_tokens for r in rs]) / 1000:,.0f}k",
                f"{_mean([r.tokens - r.rule_tokens for r in rs]) / 1000:+,.0f}k",
                f"{_mean([r.reval_trials for r in rs]):.0f}",
                f"{_mean([r.cycles_started for r in rs]):.1f}/"
                f"{_mean([r.cycles_failed for r in rs]):.1f}",
                str(dict(term) or "-"),
                f"{q[1]:.0f} [{q[0]:.0f}, {q[2]:.0f}]" if q else "-",
            ]
            rows.append("| " + " | ".join(cells) + " |")
    return rows


def _fmt_a(c: dict[str, Any]) -> str:
    a = c["a"]
    return (
        f"{a['false']}/{a['n']}, UB {100 * a['ub']:.1f}% (R1 {100 * a['ub_r1']:.1f}%) "
        f"{_mark(a['ok'])}"
    )


def _fmt_by(c: dict[str, Any], key: str) -> str:
    return (
        ", ".join(f"{k}: {v[0]}/{v[1]}" for k, v in c[key]["by"].items())
        + f" {_mark(c[key]['ok'])}"
    )


def _fmt_ci(t: tuple[float, float, float]) -> str:
    return f"{t[0]:+.1f} [{t[1]:+.1f}, {t[2]:+.1f}]"


def report(path: str, repro: str | None = None, title: str = "M6.3 revalidation") -> str:
    by = load(path)
    ev = evaluate(by)
    s = ev["strategies"]
    n_streams = {sid: len(v["m62"]) for sid, v in by.items()}
    lines = [
        f"# {title} (generated, synthetic)",
        "",
        f"Streams per scenario: {n_streams}. Source: `{Path(path).name}`.",
        "",
        "## Pre-registered acceptance (design §9; computed from the stored results)",
        "",
        "| criterion | " + " | ".join(STRATEGIES) + " |",
        "|---|---|---|---|",
    ]

    def row(name: str, f: Any) -> str:
        return f"| {name} | " + " | ".join(f(s[x]) for x in STRATEGIES) + " |"

    lines += [
        row("(a) false restoration in S2/S4/R1/R2: exact 95% UB ≤ 5% (R1 ≤ 10%)", _fmt_a),
        row("(b) restored by the end, S5/R3/R4 ≥ 70%", lambda c: _fmt_by(c, "b")),
        row("(c) restored by the end, S8/S8b ≥ 90%", lambda c: _fmt_by(c, "c")),
        row(
            "(d) restorations resting on > 20% unverified trials (must be 0)",
            lambda c: f"{c['d']['violations']} {_mark(c['d']['ok'])}",
        ),
        row(
            "(e) net utility vs m62, pooled S5/R3/R4/S8/S8b, CI lower bound > 0",
            lambda c: f"{_fmt_ci(c['e']['vs_m62'])} {_mark(c['e']['ok'])}",
        ),
        row(
            "    … against m62_locked (permanent quarantine), reported",
            lambda c: _fmt_ci(c["e"]["vs_locked"]),
        ),  # fmt: skip
    ]
    r = ev["f"]["ratio"]
    lines += [
        "",
        f"(f) sequential saving, C / A revalidation tokens over all scenarios: {r[0]:.2f} "
        f"[{r[1]:.2f}, {r[2]:.2f}] (needs ≤ 0.80, upper bound < 1.0, and (a) for C): "
        f"{_mark(ev['f']['ok'])}",
        "",
        "(g) reproducibility: " + ("see below" if repro else "no second run supplied"),
        "(h) backward compatibility: pinned by the test suite (M6/M6.1/M6.2 hashes and digests).",
        "",
    ]
    if repro:
        same = Path(path).read_bytes() == Path(repro).read_bytes()
        lines += [
            f"Second run `{Path(repro).name}` byte-identical: **{'yes' if same else 'NO'}**.",
            "",
        ]
    lines += [
        f"**Verdict (rule applied in order): {ev['verdict']}.** Candidate strategy (lowest "
        f"revalidation tokens among those meeting (a)–(d)): {ev['candidate'] or 'none'}. "
        f"S5 restorations: m62 {ev['m62_s5_restored']}, best m63 {ev['best_s5_restored']}.",
        "",
        "## Per-scenario results",
        "",
        "Δ is against the rule baseline on the same stream; utility = successes − (production + "
        "revalidation tokens)/20,000. Delay is measured from the end of the degradation (or, for "
        "structural/stale cases, from the quarantine).",
        "",
        *_scenario_table(by, ("m61", "m62", "m62_locked", *STRATEGIES)),
        "",
        "## Break-even horizon (arithmetic on the measured costs)",
        "",
        f"A restored alternative earns ≈ {BENEFIT_PER_DECISION:.3f} utility per decision in this "
        "world (0.52 − 6,500 tokens/20,000 for the alternative, 0.50 − 9,000/20,000 for the rule "
        "default). Revalidation pays back only after the number of *restored* decisions below.",
        "",
        "| scenario | variant | mean revalidation tokens | utility cost | break-even decisions |",
        "|---|---|---|---|---|",
    ]
    for sid in by:
        for v in STRATEGIES:
            tok = _mean([x.reval_tokens for x in by[sid][v]])
            cost = tok * UTILITY_LAMBDA
            be = cost / BENEFIT_PER_DECISION
            lines.append(f"| {sid} | {v} | {tok / 1000:,.0f}k | {cost:.1f} | {be:,.0f} |")
    return "\n".join(lines) + "\n"
