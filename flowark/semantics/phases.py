"""Default phase policies for the first-stage adapter refactor."""

from __future__ import annotations

from dataclasses import replace

from flowark.semantics.models import Phase, PhasePolicy


DEFAULT_PHASE_POLICIES: dict[Phase, PhasePolicy] = {
    Phase.ANALYSIS: PhasePolicy(
        phase=Phase.ANALYSIS,
        allow_request_submit_augment=True,
        allow_after_tool_augment=True,
        trace_channel="analysis",
    ),
    Phase.FINAL_REPORT: PhasePolicy(
        phase=Phase.FINAL_REPORT,
        allow_request_submit_augment=False,
        allow_after_tool_augment=False,
        trace_channel="final_report",
    ),
    Phase.KNOWLEDGE_SYNTH: PhasePolicy(
        phase=Phase.KNOWLEDGE_SYNTH,
        allow_request_submit_augment=False,
        allow_after_tool_augment=False,
        trace_channel="knowledge_synth",
    ),
    Phase.KNOWLEDGE_RULE_REPAIR: PhasePolicy(
        phase=Phase.KNOWLEDGE_RULE_REPAIR,
        allow_request_submit_augment=False,
        allow_after_tool_augment=False,
        trace_channel="knowledge_rule_repair",
    ),
}


def default_phase_policy(phase: Phase) -> PhasePolicy:
    try:
        policy = DEFAULT_PHASE_POLICIES[phase]
    except KeyError as exc:  # pragma: no cover - defensive guard for future enum additions
        raise ValueError(f"unknown phase policy: {phase!r}") from exc
    return replace(policy)
