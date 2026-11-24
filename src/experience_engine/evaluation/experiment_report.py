"""Markdown report for the capability-vs-memory experiment. Every number is computed from the
`ExperimentResult` rows; the "mechanical verdicts" only restate paired statistics, they do not
interpret them."""

from collections import Counter, defaultdict
from collections.abc import Callable, Sequence

from experience_engine.bench.report import assert_no_secrets
from experience_engine.domain.experiment import ARM_NAMES, ARMS, ExperimentResult, RunRow
from experience_engine.evaluation import paired
from experience_engine.evaluation.economics import ArmEconomics, lifecycle_table

BUDGET_CAPS = (3_000, 6_000, 12_000, 24_000, 60_000)
Where = Callable[[RunRow], bool]


def _pct(x: float) -> str:
    return f"{x:.0%}"


def _arms(result: ExperimentResult) -> list[str]:
    return [a for a in ARMS if a in result.config.arms]


def _label(a: str) -> str:
    return f"{a} {ARM_NAMES[a]}"


def _group_table(
    rows: Sequence[RunRow], arms: Sequence[str], key: Callable[[RunRow], str], title: str
) -> list[str]:
    groups = sorted({key(r) for r in rows})
    out = [
        f"| {title} | tasks | " + " | ".join(arms) + " |",
        "|---|---|" + "---|" * len(arms),
    ]
    for g in groups:
        sub = [r for r in rows if key(r) == g]
        n_tasks = len({r.task_id for r in sub})
        cells = []
        for a in arms:
            rates = paired.task_rates(sub, a)
            cells.append(_pct(sum(rates.values()) / len(rates)) if rates else "n/a")
        out.append(f"| {g} | {n_tasks} | " + " | ".join(cells) + " |")
    return out


def _ctx(rs: Sequence[RunRow], n: int) -> str:
    total = sum(r.context_tokens for r in rs)
    return f"{total / n:,.0f}" if total else "n/a (runtime arm)"


def _verdict(c: paired.PairedComparison, p: float | None = None) -> str:
    """Both tests must agree before a winner is named; a split decision is reported as such."""
    if c.n_tasks == 0:
        return "no data"
    winner = c.x if c.diff > 0 else c.y
    by_ci = c.ci_excludes_zero
    by_p = (c.p_mcnemar if p is None else p) < 0.05
    if by_ci and by_p:
        return f"{winner} better (bootstrap and McNemar)"
    if by_ci:
        return f"{winner} ahead by bootstrap only (McNemar n.s.)"
    if by_p:
        return f"{winner} ahead by McNemar only (bootstrap CI includes 0)"
    return "not distinguishable"


def _cmp_row(c: paired.PairedComparison, p_adj: float | None) -> str:
    adj = "" if p_adj is None else f"{p_adj:.3f}"
    return (
        f"| {c.x} vs {c.y} | {c.n_tasks} | {_pct(c.rate_x)} | {_pct(c.rate_y)} | "
        f"{c.diff:+.0%} [{c.ci_lo:+.0%}, {c.ci_hi:+.0%}] | {c.only_x}/{c.only_y} | "
        f"{c.p_mcnemar:.3f} | {adj} | {_verdict(c, p_adj)} |"
    )


_CMP_HEADER = [
    "| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | "
    "discordant X-only/Y-only | McNemar p | Holm-adj p | reading |",
    "|---|---|---|---|---|---|---|---|---|",
]


def _comparisons(
    rows: Sequence[RunRow], pairs: Sequence[tuple[str, str]], where: Where | None = None
) -> list[str]:
    comps = [paired.compare(rows, x, y, where=where) for x, y in pairs]
    adj = paired.holm({f"{c.x}-{c.y}": c.p_mcnemar for c in comps})
    return [*_CMP_HEADER, *[_cmp_row(c, adj[f"{c.x}-{c.y}"]) for c in comps]]


def _econ_table(table: dict[str, ArmEconomics]) -> list[str]:
    out = [
        "| Arm | runs | success | tokens/run | tokens/success | tool calls/run | latency s/run | "
        "retrieved tok/run | retrieval ms | learning tokens L | break-even (verified successes) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for a, e in table.items():
        c = "never" if e.tokens_per_success is None else f"{e.tokens_per_success:,.0f}"
        if a == "A":
            be = "reference"
        elif e.breakeven_successes is None:
            be = f"never ({e.breakeven_note})" if e.breakeven_note else "never"
        else:
            be = f"{e.breakeven_successes:,.0f}"
        out.append(
            f"| {_label(a)} | {e.n_runs} | {_pct(e.success_rate)} | {e.tokens_per_run:,.0f} | {c} "
            f"| {e.tool_calls_per_run:.1f} | {e.latency_ms_per_run / 1000:.0f} | "
            f"{e.retrieved_tokens_per_run:,.0f} | {e.retrieval_ms_per_run:.1f} | "
            f"{e.learning_tokens:,} | {be} |"
        )
    return out


def render_experiment(result: ExperimentResult, *, title: str, maintenance_tokens: int = 0) -> str:
    cfg, rows, arms = result.config, result.rows, _arms(result)
    known = [r for r in rows if r.status != "UNKNOWN"]
    n_unknown = len(rows) - len(known)
    tasks = sorted({r.task_id for r in rows})
    lines: list[str] = [
        f"# {title}",
        "",
        "Research question: with equivalent historical experience, does a compiled, validated, "
        "executable capability (E) outperform memory-based reuse (B episodic, C consolidated, "
        "D instructions) and the stateless baseline (A)? The design is "
        "[capability-vs-memory-design.md](../experiments/capability-vs-memory-design.md); "
        "the experiment is built to be able to falsify the hypothesis.",
        "",
        "## 1. Configuration (reproducibility)",
        "",
        f"- Model: `{cfg.model.get('provider')}` / `{cfg.model.get('model')}` (temperature "
        f"{cfg.model.get('temperature')}); repetitions per task: {cfg.repetitions}; workers: "
        f"{cfg.workers}. Repetitions are repeated draws from a non-deterministic model (the "
        "provider ignores seeds), not independent samples.",
        f"- Arms: {', '.join(_label(a) for a in arms)}.",
        "- Datasets: "
        + "; ".join(
            f"{s}: `{v['name']}` v{v['version']} `{v['content_hash'][:12]}…`"
            for s, v in sorted(cfg.datasets.items())
        ),
        f"- Discovery experience: {cfg.discovery_trajectories} eligible trajectories "
        f"(DISCOVERY split only), families {cfg.discovery_families}; the same set feeds B, C, D "
        "and the compiler.",
        f"- Capabilities available to D and E: {', '.join(cfg.capabilities) or 'none'} "
        f"(catalog states {cfg.capability_catalog_states}). "
        + (
            "**Experimental catalog: not every version is ACTIVE; this is a research run, "
            "not promotion evidence.**"
            if cfg.capability_catalog_states != ["ACTIVE"]
            else ""
        ),
        f"- Memory preamble sha256 `{cfg.memory_preamble_sha256[:16]}…`; up to "
        f"{cfg.max_episodes} episodes; token estimator `{cfg.token_estimator}`; verifier "
        f"`{cfg.verifier_version}`; runtime `{cfg.runtime_version}`; evaluator "
        f"`{cfg.evaluator_version}`.",
        f"- Runs: {len(rows)} ({n_unknown} UNKNOWN, excluded as missing data); distinct tasks: "
        f"{len(tasks)}.",
        *[f"- Note: {n}" for n in cfg.notes],
        "",
        "## 2. Success by arm",
        "",
        "Cells are the mean of per-task success rates (each task weighted equally); the number of "
        "distinct tasks is shown next to the group.",
        "",
        *_group_table(known, arms, lambda r: "all tasks", "Group"),
        "",
        "### By task family",
        "",
        *_group_table(known, arms, lambda r: r.family, "Family"),
        "",
        "### By transfer level",
        "",
        "`intra_variant`: seen libraries, new numbers. `cross_variant`: a learned family with an "
        "unseen library/layout. `cross_family`: a family never seen in discovery.",
        "",
        *_group_table(known, arms, lambda r: r.level, "Level"),
        "",
        "### By variant",
        "",
        *_group_table(known, arms, lambda r: f"{r.family}/{r.variant}", "Variant"),
        "",
        "## 3. Paired statistical comparisons",
        "",
        "Unit = distinct task; repetitions are averaged inside a task. Paired bootstrap over tasks "
        f"({paired.BOOTSTRAP_RESAMPLES:,} resamples, fixed seed); exact McNemar on tasks "
        f'"solved" in >= {paired.SOLVED_THRESHOLD:.0%} of repetitions; Holm correction within '
        "each table. Wilson intervals are not used for inference here.",
        "",
        "### 3.1 Primary: compiled capability (E) against every alternative",
        "",
        *_comparisons(known, [("E", a) for a in arms if a != "E"]),
    ]
    memory = [a for a in ("B", "C", "D") if a in arms]
    if "E" in arms and memory:
        best = max(memory, key=lambda a: sum(paired.task_rates(known, a).values()))
        lines += [
            "",
            f"E vs the best memory arm (**{best}**, chosen *post hoc* by overall rate, so this "
            "comparison favours memory):",
            "",
            *_comparisons(known, [("E", best)]),
        ]
    ladder = [
        p
        for p in (("B", "A"), ("C", "A"), ("C", "B"), ("D", "C"), ("E", "D"))
        if all(x in arms for x in p)
    ]
    lines += [
        "",
        "### 3.2 Attribution ladder (where does any improvement come from?)",
        "",
        "A->B/C: value of *information*. C->D: value of a procedure stated as *instructions*. "
        "D->E: value of *executing* the procedure instead of asking the model to follow it.",
        "",
        *_comparisons(known, ladder),
    ]
    for lvl in ("intra_variant", "cross_variant", "cross_family"):
        sub = [r for r in known if r.level == lvl]
        if not sub:
            continue
        lines += [
            "",
            f"### 3.3 E vs alternatives, {lvl} tasks only",
            "",
            *_comparisons(sub, [("E", a) for a in arms if a != "E"]),
        ]

    for fam in sorted({r.family for r in known}):
        sub = [r for r in known if r.family == fam]
        lines += [
            "",
            f"### 3.4 E vs alternatives, family `{fam}` only",
            "",
            *_comparisons(sub, [("E", a) for a in arms if a != "E"]),
        ]

    # efficiency and equal-budget success
    econ = lifecycle_table(known, result.ledger, arms=arms, maintenance_tokens=maintenance_tokens)
    lines += [
        "",
        "## 4. Execution efficiency and equal-budget success",
        "",
        "Tokens include the retrieved memory, which sits inside every prompt of the run.",
        "",
        "| Arm | context tokens (est., first call) | retrieved tokens | tokens/run | "
        "tokens/verified success |",
        "|---|---|---|---|---|",
    ]
    for a in arms:
        rs = [r for r in known if r.arm == a]
        n = max(1, len(rs))
        e = econ[a]
        per_success = "never" if e.tokens_per_success is None else f"{e.tokens_per_success:,.0f}"
        lines.append(
            f"| {_label(a)} | {_ctx(rs, n)} | "
            f"{sum(r.retrieved_tokens for r in rs) / n:,.0f} | {e.tokens_per_run:,.0f} | "
            f"{per_success} |"
        )
    lines += [
        "",
        "Success rate when a run only counts if it verified within a token cap "
        "(equal resource budget):",
        "",
        "| Arm | " + " | ".join(f"<= {c:,}" for c in BUDGET_CAPS) + " |",
        "|---|" + "---|" * len(BUDGET_CAPS),
    ]
    for a in arms:
        curve = paired.budget_curve(known, a, BUDGET_CAPS)
        lines.append(f"| {_label(a)} | " + " | ".join(_pct(curve[c]) for c in BUDGET_CAPS) + " |")

    # lifecycle
    lg = result.ledger
    lines += [
        "",
        "## 5. Lifecycle cost and break-even",
        "",
        "`lifecycle_cost(N) = L + N * e + M`; break-even is the number of future *verified "
        "successes* after which the learning cost is recovered relative to the stateless "
        "baseline (`N* = L / (c_A - c_X)`, `c` = tokens per verified success, failures' cost "
        "included). Maintenance M = "
        f"{maintenance_tokens:,} tokens (**not measured**, which flatters every learning arm).",
        "",
        f"Learning ledger: discovery {lg.discovery_tokens:,} tokens over {lg.discovery_runs} "
        f"runs ({lg.discovery_tool_calls} tool calls); analysis {lg.analysis_tokens:,}; "
        f"consolidation {lg.consolidation_tokens:,} ({lg.wall_ms_consolidation} ms); "
        f"compilation {lg.compilation_tokens:,} ({lg.wall_ms_compilation} ms); validation "
        f"{lg.validation_tokens:,}.",
        *[f"- {n}" for n in lg.notes],
        "",
        "### 5.1 As built (only the capability pays for validation)",
        "",
        *_econ_table(econ),
        "",
        "### 5.2 Sensitivity: every learning arm pays the same validation cost",
        "",
        *_econ_table(
            lifecycle_table(
                known,
                lg,
                arms=arms,
                maintenance_tokens=maintenance_tokens,
                charge_validation_to_all=True,
            )
        ),
    ]
    for fam in sorted({r.family for r in known}):
        fl = result.ledger_by_family.get(fam)
        if fl is None:
            continue
        lines += [
            "",
            f"### 5.3 Family `{fam}` (family-specific learning ledger)",
            "",
            *_econ_table(
                lifecycle_table(
                    known,
                    fl,
                    arms=arms,
                    maintenance_tokens=maintenance_tokens,
                    where=lambda r, f=fam: r.family == f,  # type: ignore[misc]
                )
            ),
        ]

    # capability behaviour
    e_rows = [r for r in known if r.arm == "E"]
    if e_rows:
        lines += [
            "",
            "## 6. Compiled capability: applicability, fallback and boundaries",
            "",
            "| Family | tasks | eligible capability found | used | fell back to planning |",
            "|---|---|---|---|---|",
        ]
        for fam in sorted({r.family for r in e_rows}):
            sub = [r for r in e_rows if r.family == fam]
            n = len(sub)
            lines.append(
                f"| {fam} | {len({r.task_id for r in sub})} | "
                f"{_pct(sum(bool(r.applicable) for r in sub) / n)} | "
                f"{_pct(sum(bool(r.capability_used) for r in sub) / n)} | "
                f"{_pct(sum(bool(r.fell_back) for r in sub) / n)} |"
            )
        if result.applicability:
            lines += [
                "",
                "Applicability gate (deterministic, no model): tasks admitted per family and "
                "synthetic precondition violations admitted (must be 0).",
                "",
                "| Capability | learned family | admitted/total by family | synthetic violations "
                "admitted |",
                "|---|---|---|---|",
            ]
            for ap in result.applicability:
                by = ap["admitted_by_family"]
                assert isinstance(by, dict)
                cells = ", ".join(f"{f}: {v[0]}/{v[1]}" for f, v in by.items())
                syn = ap["synthetic_violations_admitted"]
                assert isinstance(syn, list)
                lines.append(
                    f"| {ap['capability']} | {ap['family']} | {cells} | {syn[0]}/{syn[1]} |"
                )

    # failures and regressions
    lines += ["", "## 7. Failure and regression analysis", "", "Non-verified runs by termination:"]
    lines += ["", "| Arm | non-verified runs | termination counts |", "|---|---|---|"]
    for a in arms:
        bad = [r for r in known if r.arm == a and not r.verified]
        term = Counter(r.termination or "?" for r in bad)
        lines.append(f"| {_label(a)} | {len(bad)} | {dict(term.most_common(5))} |")
    lines += [
        "",
        "Regressions against the baseline: tasks A solved (>= half of repetitions) that the arm "
        "did not.",
        "",
        "| Arm | regressed tasks | improved tasks | task ids that regressed |",
        "|---|---|---|---|",
    ]
    for a in arms:
        if a == "A":
            continue
        cmp_a = paired.compare(known, a, "A")
        rx, ry = paired.task_rates(known, a), paired.task_rates(known, "A")
        reg = sorted(
            t
            for t in rx.keys() & ry.keys()
            if ry[t] >= paired.SOLVED_THRESHOLD and rx[t] < paired.SOLVED_THRESHOLD
        )
        lines.append(
            f"| {_label(a)} | {cmp_a.only_y} | {cmp_a.only_x} | {', '.join(reg[:8]) or '-'} |"
        )

    # mechanical verdicts
    lines += [
        "",
        "## 8. Mechanical verdicts (restating the paired statistics, not interpreting them)",
        "",
    ]
    for x, y, q in (
        ("E", "B", "Does E outperform episodic memory?"),
        ("E", "C", "Does E outperform consolidated memory?"),
        ("E", "D", "Does E outperform instruction-based reuse?"),
        ("E", "A", "Does E outperform the stateless baseline?"),
    ):
        if x in arms and y in arms:
            vc = paired.compare(known, x, y)
            lines.append(
                f"- {q} Paired difference {vc.diff:+.0%} [{vc.ci_lo:+.0%}, {vc.ci_hi:+.0%}] over "
                f"{vc.n_tasks} tasks; McNemar p={vc.p_mcnemar:.3f}; reading: **{_verdict(vc)}**."
            )
    lines += [
        "",
        "## 9. Statistical assumptions and limitations",
        "",
        "- Tasks inside a family come from one generator and are treated as exchangeable draws; "
        "results do not generalise beyond these families, this simulated environment and the "
        "model(s) tested.",
        "- Repetitions are collapsed inside each task, so they sharpen the per-task rate but add "
        "no tasks; with a few dozen tasks the intervals are wide.",
        "- McNemar binarises each task and discards the size of the difference; the bootstrap "
        "keeps it. They can disagree at small n.",
        "- Holm adjusts within each table only; across the whole report many comparisons are made "
        "and isolated small p-values should not be read as findings.",
        "- Token counts include retrieval in the prompt but no price list is applied; maintenance "
        "cost is an input, not a measurement.",
        "- Memory and instruction texts were fixed before any evaluation and never tuned on "
        "evaluation data; a better-engineered memory could do better.",
        "",
    ]
    text = "\n".join(lines)
    assert_no_secrets(text)
    return text


def group_by_task(rows: Sequence[RunRow]) -> dict[str, list[RunRow]]:
    out: dict[str, list[RunRow]] = defaultdict(list)
    for r in rows:
        out[r.task_id].append(r)
    return out
