"""Memory representations built from *the same* discovery trajectories the compiler sees.

Three ways to reuse experience without executing a compiled capability:
  episodic     - a few concrete past episodes with their outcomes (successes and failures);
  consolidated - a per-family summary: applicability, consistent procedure, failure patterns;
  instructions - the compiled capability's procedure rendered as natural-language instructions.

All inputs are `Trajectory` / `CapabilityDefinition` objects: no task, dataset or evaluation access
is possible from here (architecture test), so hidden data cannot leak into memory. Raw values and
paths are not in a Trajectory, so no memory form can carry answers a capability cannot.
"""

import math
from collections.abc import Sequence

from experience_engine.domain.capability import (
    CapabilityDefinition,
    FileRolePresent,
    LlmDecision,
    RiskLevelAtMost,
    SelectPath,
    TaskFamilyIs,
    ToolCall,
    ToolsAvailable,
    Verify,
)
from experience_engine.domain.experience import Trajectory
from experience_engine.domain.memory import EpisodeEntry
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.learning.contrast import contrast

PREAMBLE = (
    "Experience from earlier, similar tasks. It may not apply to this task: verify everything "
    "against the actual files before acting."
)
_ROLE_HINT = {
    "log": "the log file (under logs/)",
    "infra": "the infrastructure file (under infra/; read-only, owned by another team)",
    "config": "the application configuration file",
}


def estimate_tokens(text: str) -> int:
    """Provider-independent size estimate (~4 characters per token). The real prompt tokens are
    charged by the provider on every call; this is only used to report what was retrieved."""
    return math.ceil(len(text) / 4)


def _is_success(t: Trajectory) -> bool:
    return t.outcome_status == OutcomeStatus.VERIFIED_SUCCESS


def _pretty(token: str) -> str:
    tool, _, role = token.partition(":")
    return f"{tool}({role})" if role else tool


def _steps(t: Trajectory) -> str:
    return " -> ".join(_pretty(x) for x in t.tool_tokens) or "(no tool calls)"


def render_episode(t: Trajectory, number: int) -> str:
    family = t.task_family or "unknown"
    if _is_success(t):
        verdict = "VERIFIED SUCCESS (independently verified)"
    else:
        verdict = "FAILED independent verification" + (
            " although the agent had claimed it was done" if t.claimed_done else ""
        )
    return (
        f"Episode {number} (task family {family}): {verdict}. "
        f"Steps: {_steps(t)}. Cost: {t.tokens_used} tokens, {t.tool_calls} tool calls."
    )


def render_episode_entry(e: EpisodeEntry, number: int) -> str:
    """Render one stored bank entry exactly as `render_episode` rendered the live trajectory."""
    return e.text.replace("Episode 0 ", f"Episode {number} ", 1)


def build_episodic(episodes: Sequence[Trajectory]) -> str:
    if not episodes:
        return ""
    body = "\n".join(render_episode(t, i + 1) for i, t in enumerate(episodes))
    return f"{PREAMBLE}\nPast episodes:\n{body}"


def _ordered_common_steps(successes: Sequence[Trajectory], support: float = 0.6) -> list[str]:
    positions: dict[str, list[int]] = {}
    for t in successes:
        seen: set[str] = set()
        for i, tok in enumerate(t.tool_tokens):
            if tok not in seen:
                seen.add(tok)
                positions.setdefault(tok, []).append(i)
    kept = [
        (sum(p) / len(p), tok)
        for tok, p in positions.items()
        if len(p) / max(1, len(successes)) >= support
    ]
    return [tok for _, tok in sorted(kept)]


def consolidate_family(family: str, trajs: Sequence[Trajectory]) -> str:
    """Generalised experience for one family: applicability, procedure, known failure patterns."""
    ok = [t for t in trajs if _is_success(t)]
    bad = [t for t in trajs if t.outcome_status == OutcomeStatus.FAILURE]
    lines = [
        f"Task family '{family}': {len(ok)} independently verified successes and {len(bad)} "
        "failures in earlier runs."
    ]
    if trajs:
        roles = sorted({r for t in trajs for r in t.file_roles})
        tools = sorted({x for t in trajs for x in t.allowed_tools})
        lines.append(
            f"Applies when: tools {', '.join(tools)} are available and the environment has "
            f"files in these roles: {', '.join(roles)}."
        )
    common = _ordered_common_steps(ok)
    if common:
        lines.append("Procedure shared by most successes: " + " -> ".join(map(_pretty, common)))
    if ok and bad:
        # same discriminative threshold the compiler uses, so memory is never poorer than it
        for f in contrast(ok, bad, critical_success_min=0.3, gap=0.3):
            if f.success_support - f.failure_support >= 0.3 and f.success_support >= 0.3:
                lines.append(
                    f"Failure pattern: {_pretty(f.token)} was done in {f.success_support:.0%} of "
                    f"successes but only {f.failure_support:.0%} of failures - failed runs "
                    "typically skipped it."
                )
            elif f.failure_support - f.success_support >= 0.3 and f.failure_support >= 0.5:
                lines.append(
                    f"Failure pattern: {_pretty(f.token)} was done in {f.failure_support:.0%} of "
                    f"failures but {f.success_support:.0%} of successes."
                )
    if bad:
        claimed = sum(1 for t in bad if t.claimed_done)
        lines.append(
            f"{claimed} of {len(bad)} failed runs claimed success although independent "
            "verification showed the result was wrong: do not stop at a plausible change."
        )
    if ok:
        mean_calls = sum(t.tool_calls for t in ok) / len(ok)
        lines.append(f"Successful runs used about {mean_calls:.0f} tool calls.")
    return "\n".join(lines)


def build_consolidated(summaries: Sequence[str]) -> str:
    if not summaries:
        return ""
    return f"{PREAMBLE}\n" + "\n\n".join(summaries)


def render_instructions(d: CapabilityDefinition) -> str:
    """Natural-language rendering of a compiled procedure (what arm D hands to the model)."""
    family = next((p.family for p in d.preconditions if isinstance(p, TaskFamilyIs)), "?")
    lines = [f"Procedure for task family '{family}' (derived from verified successful runs):"]
    for n, step in enumerate(d.steps, start=1):
        if isinstance(step, ToolCall):
            path = step.args.get("path")
            role = path.select_path.role.value if isinstance(path, SelectPath) else None
            if step.tool == "list_files":
                text = "List the files in the environment."
            elif step.tool == "read_file" and role:
                text = f"Read {_ROLE_HINT.get(role, role)}."
            elif step.tool == "set_json_value" and role:
                text = f"Apply the change with set_json_value on {_ROLE_HINT.get(role, role)}."
            else:
                text = f"Call {step.tool}."
        elif isinstance(step, LlmDecision):
            keys = ", ".join(f"{k} ({v.value})" for k, v in sorted(step.output_schema.items()))
            text = (
                "Decide the single configuration key to change and its new value, using only "
                f"what you have read so far (answer: {keys})."
            )
        elif isinstance(step, Verify):
            text = "Check the result against every constraint in the task before you finish."
        else:
            text = f"Perform step '{step.id}' ({step.op})."
        lines.append(f"{n}. {text}")
    pre = []
    for p in d.preconditions:
        if isinstance(p, ToolsAvailable):
            pre.append(f"tools {', '.join(p.tools)} available")
        elif isinstance(p, FileRolePresent):
            pre.append(f"a {p.role.value} file present")
        elif isinstance(p, RiskLevelAtMost):
            pre.append(f"risk level at most {p.level}")
    if pre:
        lines.append("Applies when: " + "; ".join(pre) + ".")
    lines += [f"Limitation: {x}" for x in d.known_limitations]
    return "\n".join(lines)


def build_instructions(rendered: Sequence[str]) -> str:
    if not rendered:
        return ""
    return f"{PREAMBLE}\n" + "\n\n".join(rendered)
