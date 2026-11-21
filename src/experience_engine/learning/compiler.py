"""Deterministic capability compiler (milestone 3).

Turns eligible experience (verified successes, with failures as contrast) into immutable
capability *candidates*. No model is called: the same inputs always produce byte-identical
output, so compiler versions are comparable. Every candidate is validated by the static validator
before it is returned, and is only ever a CANDIDATE - activation belongs to milestone 4.

Method (documented limits in docs/capabilities.md):
  1. keep tool tokens present in at least `skeleton_support` of the verified successes;
  2. order them by mean first position in those successes;
  3. abstract file arguments to path *roles* and delegate value choice to a bounded, typed
     LLM_DECISION (the DSL has no arithmetic and no free text);
  4. end with an independent VERIFY step;
  5. derive preconditions, budgets, limitations and data-only tests from the evidence.
"""

import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from experience_engine.capabilities.tools import APPROVED_TOOLS
from experience_engine.capabilities.validator import (
    CapabilityValidationError,
    validate_definition,
)
from experience_engine.domain.capability import (
    Budgets,
    CandidateBundle,
    CandidateTest,
    CapabilityDefinition,
    FailureCondition,
    FieldType,
    FileRolePresent,
    LlmDecision,
    OutType,
    PathRole,
    Provenance,
    RecoveryAction,
    RiskLevelAtMost,
    SelectPath,
    SelectPathSpec,
    Step,
    TaskFamilyIs,
    TerminationRule,
    ToolCall,
    ToolsAvailable,
    VarRef,
    Verify,
    procedure_hash,
)
from experience_engine.domain.experience import (
    PIPELINE_VERSION,
    FindingKind,
    Trajectory,
)
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.learning.contrast import contrast

# v2: LLM_DECISION step cap 2000 -> 4000 (thinking models spend hidden tokens against the cap)
# v3: observation steps that discriminate successes from failures join the skeleton even when
#     fewer than `skeleton_support` of the successes performed them (see `_discriminative_reads`)
COMPILER_VERSION = "rule-compiler/3"
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
_FAMILY_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class CompilerConfig:
    compiler_version: str = COMPILER_VERSION
    min_support: int = 3
    skeleton_support: float = 0.6
    # contrast-added read steps: needs real counterexamples, and the read must be clearly more
    # common in successes than in failures (never applied to mutating steps)
    contrast_min_failures: int = 3
    contrast_min_success_support: float = 0.3
    contrast_max_failure_support: float = 0.2
    contrast_min_gap: float = 0.3
    max_tests: int = 5
    max_listed_runs: int = 50
    decision_max_tokens: int = 4000
    dataset_name: str | None = None
    dataset_version: str | None = None


@dataclass(frozen=True)
class Rejection:
    family: str
    reason: str


@dataclass(frozen=True)
class CompileResult:
    candidates: list[CandidateBundle]
    rejections: list[Rejection]


def _p90(values: Sequence[int]) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)]


def _family_key(family: str) -> str:
    return _FAMILY_RE.sub("_", family.lower()).strip("_")[:40]


def _discriminative_reads(
    successes: Sequence[Trajectory], failures: Sequence[Trajectory], cfg: CompilerConfig
) -> set[str]:
    """Read-only observation steps that separate successes from failures.

    The common core of the successes can miss exactly the step that matters: if failed runs
    skipped reading a source of evidence that only some successful runs consulted, that read
    sits below the support threshold. Adding a read cannot change the environment, so it is the
    only kind of step the contrast may add; it is still a hypothesis the evaluator must confirm.
    """
    if len(failures) < cfg.contrast_min_failures or not successes:
        return set()
    out: set[str] = set()
    for f in contrast(successes, failures, critical_success_min=0.0, gap=0.0):
        tool = _tool_of(f.token)
        if tool not in APPROVED_TOOLS or APPROVED_TOOLS[tool].mutating or ":" not in f.token:
            continue
        if (
            f.success_support >= cfg.contrast_min_success_support
            and f.failure_support <= cfg.contrast_max_failure_support
            and f.success_support - f.failure_support >= cfg.contrast_min_gap
        ):
            out.add(f.token)
    return out


def _skeleton(
    successes: Sequence[Trajectory], threshold: float, extra: frozenset[str] = frozenset()
) -> list[str]:
    n = len(successes)
    positions: dict[str, list[int]] = {}
    for t in successes:
        seen: set[str] = set()
        for i, token in enumerate(t.tool_tokens):
            if token not in seen:
                seen.add(token)
                positions.setdefault(token, []).append(i)
    kept = [
        (sum(p) / len(p), tok)
        for tok, p in positions.items()
        if len(p) / n >= threshold or tok in extra
    ]
    return [tok for _, tok in sorted(kept)]


def _tool_of(token: str) -> str:
    return token.split(":", 1)[0]


def _majority_value_type(successes: Sequence[Trajectory]) -> OutType:
    counts: dict[str, int] = {}
    for t in successes:
        for s in t.steps:
            if s.tool == "set_json_value" and s.value_type:
                counts[s.value_type] = counts.get(s.value_type, 0) + 1
    if not counts:
        return OutType.NUMBER
    return OutType(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0])


def _build_steps(skeleton: list[str], value_type: OutType, decision_max_tokens: int) -> list[Step]:
    steps: list[Step] = []
    bound_reads: list[str] = []
    needs_files = any(":" in tok for tok in skeleton)
    if needs_files and "list_files" not in skeleton:
        skeleton = ["list_files", *skeleton]
    used_ids: set[str] = set()

    def uid(base: str) -> str:
        cand, i = base, 2
        while cand in used_ids:
            cand, i = f"{base}_{i}", i + 1
        used_ids.add(cand)
        return cand

    for token in skeleton:
        tool = _tool_of(token)
        role = PathRole(token.split(":", 1)[1]) if ":" in token else None
        if tool == "list_files":
            steps.append(ToolCall(id=uid("list_files"), tool="list_files", bind="files"))
        elif tool == "read_file" and role is not None:
            var = f"{role.value}_text"
            steps.append(
                ToolCall(
                    id=uid(f"read_{role.value}"),
                    tool="read_file",
                    args={
                        "path": SelectPath(select_path=SelectPathSpec(from_var="files", role=role))
                    },
                    bind=var,
                )
            )
            bound_reads.append(var)
        elif tool == "set_json_value" and role is not None:
            n_fix = sum(1 for s in steps if isinstance(s, LlmDecision))
            fix_var = "fix" if n_fix == 0 else f"fix_{n_fix + 1}"
            steps.append(
                LlmDecision(
                    id=uid("decide_fix"),
                    template_id="select_fix_parameters/1",
                    inputs=list(bound_reads),
                    output_schema={"key": OutType.STRING, "value": value_type},
                    bind=fix_var,
                    max_tokens=decision_max_tokens,
                )
            )
            steps.append(
                ToolCall(
                    id=uid("apply_fix"),
                    tool="set_json_value",
                    args={
                        "path": SelectPath(select_path=SelectPathSpec(from_var="files", role=role)),
                        "key": VarRef(var=f"{fix_var}.key"),
                        "value": VarRef(var=f"{fix_var}.value"),
                    },
                    bind="applied",
                )
            )
    steps.append(Verify(id="verify_outcome", verifier="outcome_verifier"))
    return steps


def _roles_in(steps: Sequence[Step]) -> list[PathRole]:
    roles: set[PathRole] = set()
    for s in steps:
        if isinstance(s, ToolCall):
            for e in s.args.values():
                if isinstance(e, SelectPath):
                    roles.add(e.select_path.role)
    return sorted(roles, key=lambda r: r.value)


def _tools_in(steps: Sequence[Step]) -> list[str]:
    return sorted({s.tool for s in steps if isinstance(s, ToolCall)})


def _compile_family(
    family: str, trajs: Sequence[Trajectory], others: Sequence[Trajectory], cfg: CompilerConfig
) -> CandidateBundle | Rejection:
    successes = sorted(
        (t for t in trajs if t.outcome_status == OutcomeStatus.VERIFIED_SUCCESS),
        key=lambda t: t.run_id,
    )
    failures = sorted(
        (t for t in trajs if t.outcome_status == OutcomeStatus.FAILURE), key=lambda t: t.run_id
    )
    key = _family_key(family)
    if not key or not key[0].isalpha():
        return Rejection(family, "invalid_family_name")
    if len(successes) < cfg.min_support:
        return Rejection(
            family, f"insufficient_support: {len(successes)} verified successes < {cfg.min_support}"
        )
    added = _discriminative_reads(successes, failures, cfg)
    skeleton = _skeleton(successes, cfg.skeleton_support, frozenset(added))
    unapproved = sorted({_tool_of(t) for t in skeleton} - set(APPROVED_TOOLS))
    if unapproved:
        return Rejection(family, f"unapproved_tool: {','.join(unapproved)}")
    if not any(APPROVED_TOOLS[_tool_of(t)].mutating for t in skeleton):
        return Rejection(family, "no_mutating_step: nothing to automate beyond reading")

    signature = hashlib.sha256(
        json.dumps({"family": key, "skeleton": skeleton}, sort_keys=True).encode()
    ).hexdigest()
    steps = _build_steps(skeleton, _majority_value_type(successes), cfg.decision_max_tokens)
    findings = contrast(successes, failures)
    critical = [f for f in findings if f.kind == FindingKind.CRITICAL and f.token in skeleton]
    shapes = sorted({t.config_shape for t in successes if t.config_shape})
    claimed_false = sum(1 for t in failures if t.claimed_done)
    risk = max((t.risk_level for t in successes), key=lambda r: _RISK_ORDER.get(r, 0))
    n_steps = len(steps)
    max_tool_calls = max(t.tool_calls for t in successes)
    max_steps = min(100, max(n_steps + 2, math.ceil(1.5 * max_tool_calls) + 2))
    max_tokens = min(50_000, max(1_000, math.ceil(1.5 * _p90([t.tokens_used for t in successes]))))

    limitations = [
        f"Derived from {len(successes)} verified successes of task family {key}; "
        f"{len(shapes)} distinct config layouts observed.",
        "Steps are association-based hypotheses; effectiveness is unproven until evaluated.",
    ]
    for token in sorted(added):
        found = next(f for f in contrast(successes, failures) if f.token == token)
        limitations.append(
            f"Step {token} added by contrast although only {found.success_support:.0%} of "
            f"successes performed it: failed runs did so in {found.failure_support:.0%}."
        )
    if failures:
        limitations.append(
            f"{len(failures)} contrasting failed run(s); {claimed_false} of them claimed success "
            "without independent verification."
        )
        for f in critical[:5]:
            limitations.append(
                f"Hypothesis: step {f.token} is common in successes ({f.success_support:.0%}) "
                f"and rare in failures ({f.failure_support:.0%})."
            )
    else:
        limitations.append("No failed runs observed for this family; contrast unavailable.")

    preconds = [
        TaskFamilyIs(family=key),
        ToolsAvailable(tools=_tools_in(steps)),
        *[FileRolePresent(role=r) for r in _roles_in(steps)],
        RiskLevelAtMost(level=risk),
    ]
    definition = CapabilityDefinition(
        id=f"{key}.{signature[:10]}",
        version=1,
        objective=f"Resolve {key} problems using the procedure verified in discovery runs.",
        inputs_schema={},
        outputs_schema={"verified": FieldType.BOOLEAN},
        preconditions=preconds,
        steps=steps,
        termination=[
            TerminationRule(when="verify_passed", outcome="SUCCESS"),
            TerminationRule(when="budget_exhausted", outcome="FALLBACK"),
            TerminationRule(when="precondition_violated", outcome="FALLBACK"),
            TerminationRule(when="step_failed", outcome="FALLBACK"),
        ],
        failure_conditions=[
            FailureCondition(kind="verification_failed", description_id="outcome_not_verified"),
            FailureCondition(kind="tool_error", description_id="tool_returned_error"),
            FailureCondition(kind="budget_exceeded", description_id="budget_exhausted"),
            FailureCondition(kind="uncertain_observation", description_id="unexpected_layout"),
        ],
        recovery=[
            RecoveryAction(on="verification_failed", action="FALLBACK_GENERAL_PLANNING"),
            RecoveryAction(on="tool_error", action="FALLBACK_GENERAL_PLANNING"),
            RecoveryAction(on="budget_exceeded", action="FALLBACK_GENERAL_PLANNING"),
            RecoveryAction(on="uncertain_observation", action="FALLBACK_GENERAL_PLANNING"),
        ],
        permissions=_tools_in(steps),
        budgets=Budgets(max_steps=max_steps, max_tokens=max_tokens, timeout_ms=60_000),
        known_limitations=limitations,
        provenance=Provenance(
            source_run_ids=[t.run_id for t in successes][: cfg.max_listed_runs],
            counterexample_run_ids=[t.run_id for t in failures][: cfg.max_listed_runs],
            compiler_version=cfg.compiler_version,
            pipeline_version=PIPELINE_VERSION,
            dataset_name=cfg.dataset_name,
            dataset_version=cfg.dataset_version,
        ),
    )
    try:
        validated = validate_definition(definition)
    except CapabilityValidationError as exc:  # compiler bug or unsafe evidence: never persisted
        return Rejection(family, f"validation_failed: {exc}")

    tests = [
        CandidateTest(
            kind="SOURCE_REPLAY",
            task_id=t.task_id,
            run_id=t.run_id,
            expected="VERIFIED_SUCCESS",
            note="necessary but not sufficient",
        )
        for t in successes[: cfg.max_tests]
    ]
    tests += [
        CandidateTest(
            kind="COUNTEREXAMPLE",
            task_id=t.task_id,
            run_id=t.run_id,
            expected="FAILURE",
            note="baseline failed here; capability must not do worse",
        )
        for t in failures[: cfg.max_tests]
    ]
    other_families = sorted(
        {o.task_family for o in others if o.task_family and o.task_family != family}
    )
    for other in other_families[: cfg.max_tests]:
        sample = sorted((o for o in others if o.task_family == other), key=lambda o: o.run_id)[0]
        tests.append(
            CandidateTest(
                kind="NEGATIVE_APPLICABILITY",
                task_id=sample.task_id,
                run_id=sample.run_id,
                expected="NOT_APPLICABLE",
                note=f"task family {other} is outside the boundary",
            )
        )
    tests.append(
        CandidateTest(
            kind="NEGATIVE_APPLICABILITY",
            expected="NOT_APPLICABLE",
            note="environment without the required tools or file roles",
        )
    )
    return CandidateBundle(
        definition=validated,
        tests=tests,
        procedure_hash=procedure_hash(validated),
        skeleton_signature=signature,
        task_family=key,
    )


def compile_candidates(
    trajectories: Sequence[Trajectory], config: CompilerConfig | None = None
) -> CompileResult:
    """Compile candidates from already-eligible trajectories of ONE tenant."""
    cfg = config or CompilerConfig()
    if len({t.tenant_id for t in trajectories}) > 1:
        raise ValueError("compile_candidates must be called per tenant (tenant isolation)")
    usable = [
        t
        for t in trajectories
        if t.task_family
        and t.outcome_status in (OutcomeStatus.VERIFIED_SUCCESS, OutcomeStatus.FAILURE)
    ]
    families = sorted({t.task_family for t in usable if t.task_family})
    candidates: list[CandidateBundle] = []
    rejections: list[Rejection] = []
    for family in families:
        mine = [t for t in usable if t.task_family == family]
        others = [t for t in usable if t.task_family != family]
        result = _compile_family(family, mine, others, cfg)
        if isinstance(result, Rejection):
            rejections.append(result)
        else:
            candidates.append(result)
    return CompileResult(candidates=candidates, rejections=rejections)
