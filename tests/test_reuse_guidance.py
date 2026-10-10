from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from flowark.knowledge.reuse_guidance import render_historical_reuse_guidance_block
from flowark.runtime.config import AnalysisRequest, RunConfig
from flowark.runtime.runner import FlowArkRunner
from flowark.runtime.runner import knowledge_synth


def recalled_paths() -> dict:
    families = ["storage", "storage", "network", "database", "intent"]
    corridors = ["read → parse", "read → copy", "request → send", "query → cursor", "extra → launch"]
    return {
        "query_summary_text": "Trace the selected source to its sinks.",
        "candidate_count": 9,
        "reason": "ok",
        "selected": [
            {"metadata": {"case_id": f"case-{index}"},
             "card": {"family": family, "corridor": corridor, "support_cases": index},
             "embedding_similarity": index / 10, "final_rank": index}
            for index, (family, corridor) in enumerate(zip(families, corridors), 1)
        ],
    }


EXPECTED_GUIDANCE = """相关历史复用模式（仅供参考，不代表已覆盖）:
1. family=storage
   corridor=read → parse
   support_cases=1
2. family=storage
   corridor=read → copy
   support_cases=2
3. family=network
   corridor=request → send
   support_cases=3
4. family=database
   corridor=query → cursor
   support_cases=4
5. family=intent
   corridor=extra → launch
   support_cases=5"""


def test_guidance_preserves_complete_recall_order_and_membership() -> None:
    recalled = recalled_paths()
    original = deepcopy(recalled)
    assert render_historical_reuse_guidance_block(recalled) == EXPECTED_GUIDANCE
    assert recalled == original
    assert render_historical_reuse_guidance_block({"selected": []}) == ""


def test_guidance_keeps_corridor_only_candidate_and_rejects_missing_content() -> None:
    assert render_historical_reuse_guidance_block({"selected": [
        {"card": {"corridor": "source → sink", "support_cases": 2}}
    ]}) == "相关历史复用模式（仅供参考，不代表已覆盖）:\n1. corridor=source → sink\n   support_cases=2"
    with pytest.raises(ValueError, match="no family or corridor"):
        render_historical_reuse_guidance_block({"selected": [{"card": {}}]})


def test_runtime_guidance_persists_recalled_evidence_without_model_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    recalled = recalled_paths()
    before = deepcopy(recalled)
    history = Mock(return_value=recalled)
    knowledge = Mock(return_value={"selected": [], "reason": "no_candidates"})
    monkeypatch.setattr(knowledge_synth, "build_historical_recall_candidates", history)
    monkeypatch.setattr(knowledge_synth, "build_knowledge_recall_candidates", knowledge)
    monkeypatch.setattr(knowledge_synth, "_load_eval_root_metadata", lambda _: ({}, "example.app"))
    monkeypatch.setattr(FlowArkRunner, "_resolve_live_reuse_eval_root", lambda *_: None)

    def unexpected_request(*args, **kwargs):
        raise AssertionError("Guidance construction made an unexpected network request")

    monkeypatch.setattr(httpx.Client, "send", unexpected_request)
    monkeypatch.setattr(httpx.AsyncClient, "send", unexpected_request)
    runner = FlowArkRunner(RunConfig(
        cwd=tmp_path, skills_dir=tmp_path / "skills", knowledge_reuse_digest_mode="live_corridor_v2",
    ))
    run_dir = tmp_path / "case" / "repeat-01" / "runs" / "current"
    block, metadata = runner._build_live_reuse_digest_context(
        AnalysisRequest(query="Trace the source", app_name="example.app"),
        run_dir=run_dir,
        current_case_profile={"metadata": {"case_id": "current", "run_id": "current"},
                              "profile": {"summary_text": "Trace the selected source to its sinks."}},
    )
    assert history.call_count == knowledge.call_count == 1
    assert recalled == before
    assert block == EXPECTED_GUIDANCE
    assert metadata["historical_recall_selected_count"] == 5
    assert metadata["reason"] == "ok"
    assert (run_dir / "historical_reuse_guidance.md").read_text() == EXPECTED_GUIDANCE
    assert json.loads((run_dir / "historical_recall_candidates.json").read_text()) == recalled
    digest = json.loads((run_dir / "historical_reuse_digest.json").read_text())
    assert digest["historical_reuse_guidance"] == EXPECTED_GUIDANCE
    assert digest["guidance_block"] == EXPECTED_GUIDANCE


def test_workflow_summary_keeps_analysis_reporting_and_knowledge_costs(tmp_path: Path) -> None:
    runner = FlowArkRunner(RunConfig(cwd=tmp_path))
    metrics = [
        {"name": name, "adapter": "opencode", "result": {
            "total_cost_usd": cost, "num_turns": 1,
            "usage": {"input_tokens": tokens, "output_tokens": 2,
                      "cache_read_input_tokens": tokens * 3, "cache_creation_input_tokens": 0},
        }}
        for name, cost, tokens in [
            ("analysis", 1.0, 100), ("final_report", 0.2, 20),
            ("knowledge_synth", 0.3, 30), ("knowledge_synth_fix_parse", 0.04, 4),
            ("knowledge_rule_repair", 0.06, 6),
        ]
    ]
    summary = runner._build_run_summary(
        request=AnalysisRequest(query="Trace the source"), analysis_messages=[], turn_metrics=metrics,
    )
    assert [phase["name"] for phase in summary["phases"]] == [row["name"] for row in metrics]
    main = summary["aggregated_metrics"]["main_agent"]
    assert main["total_cost_usd_sum"] == 1.6
    assert main["usage"]["input_tokens"] == 160
    assert main["usage"]["output_tokens"] == 10
    assert main["usage"]["cache_read_input_tokens"] == 480
    assert main["num_turns_sum"] == 5
    assert set(summary["aggregated_metrics"]) == {"main_agent", "subagents", "combined", "opencode"}
