"""LLM judge helpers for evaluation harness."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from flowark.openai_compat import (
    build_chat_completions_endpoint,
    normalize_chat_completions_base_url,
)
from flowark.reporting import ReportNormalizer

from .common import (
    DEFAULT_SINK_CATEGORIES,
    _extract_first_json_object,
    _json_dump,
    _json_load,
    _safe_float,
    _safe_int,
)
from .models import EvalCase, EvalTask, EvaluationConfig


def _normalize_openai_base_url(base_url: str) -> str:
    resolved = str(base_url or "").strip()
    if not resolved:
        raise ValueError("llm_judge_base_url 不能为空")
    return normalize_chat_completions_base_url(resolved, default_base_url=resolved)


def _load_final_report_for_judge(run_dir: Path | None) -> dict[str, Any]:
    report_json_compact: dict[str, Any] = {}
    report_markdown = ""
    if run_dir is None:
        return {"report_json_compact": report_json_compact, "report_markdown": report_markdown}

    normalizer = ReportNormalizer()
    report_json_path = run_dir / "final_report.json"
    if report_json_path.exists():
        try:
            raw = _json_load(report_json_path)
            if isinstance(raw, dict):
                report = normalizer.normalize_structured_payload(
                    raw_payload=raw,
                    query=str(raw.get("query") or ""),
                    source_description=(
                        str(raw.get("source_description") or "")
                        if raw.get("source_description") is not None
                        else None
                    ),
                    sink_types=(
                        raw.get("sink_types")
                        if isinstance(raw.get("sink_types"), list)
                        else None
                    ),
                    knowledge_usage=(
                        raw.get("knowledge_usage")
                        if isinstance(raw.get("knowledge_usage"), list)
                        else None
                    ),
                    run_summary=None,
                    parse_error=(
                        str(raw.get("parse_error"))
                        if raw.get("parse_error") is not None and str(raw.get("parse_error")).strip()
                        else None
                    ),
                )
                report_json_compact = report.to_payload()
                report_json_compact["source_description"] = report.source.description
                report_json_compact["sink_types"] = report.sink_types
        except Exception:
            pass

    if not report_markdown:
        report_md_path = run_dir / "final_report.md"
        if report_md_path.exists():
            try:
                report_markdown = report_md_path.read_text(encoding="utf-8")
            except Exception:
                report_markdown = ""
    if not report_markdown and report_json_compact:
        try:
            report = normalizer.normalize_structured_payload(
                raw_payload=report_json_compact,
                query=str(report_json_compact.get("query") or ""),
                source_description=None,
                sink_types=None,
                knowledge_usage=None,
                run_summary=None,
                parse_error=None,
            )
            report_markdown = normalizer.render_markdown(report)
        except Exception:
            report_markdown = ""

    if len(report_markdown) > 18000:
        report_markdown = report_markdown[:18000] + "\n...[TRUNCATED]..."
    return {"report_json_compact": report_json_compact, "report_markdown": report_markdown}


def _source_payload_for_case(case: EvalCase) -> dict[str, Any]:
    raw = case.raw if isinstance(case.raw, dict) else {}
    source_obj = raw.get("source") if isinstance(raw.get("source"), dict) else {}
    if not source_obj:
        return {
            "method": case.source_method,
            "classname": case.source_classname,
            "statement": case.source_statement,
        }
    return source_obj


def _build_ground_truth_for_exact_judge(case: EvalCase) -> dict[str, Any]:
    source_obj = _source_payload_for_case(case)
    sinks_payload: list[dict[str, Any]] = []
    for item in (case.sink_entries or []):
        if not isinstance(item, dict):
            continue
        sink_obj = item.get("sink") if isinstance(item.get("sink"), dict) else {}
        entry = {
            "flow_id": item.get("flow_id"),
            "classification": item.get("classification"),
            "sink": sink_obj,
            "intermediate_flows": (
                item.get("intermediate_flows")
                if isinstance(item.get("intermediate_flows"), list)
                else []
            ),
        }
        sinks_payload.append(entry)

    return {
        "flow_id": case.flow_id,
        "source_id": case.source_id,
        "dataset": case.dataset,
        "app_name": case.app_name,
        "apk_name": case.apk_name,
        "classification": case.classification,
        "source": source_obj,
        "sinks": sinks_payload,
        "ground_truth_notes": {
            "classification_meaning": "TRUE 表示真实存在该 source->sink 流；FALSE 表示不存在。",
            "evaluation_focus": "对比最终报告与 ground truth 在可达 sink、不可达 sink、关键传播链上的一致性。",
        },
    }


def _build_ground_truth_for_judge(case: EvalCase) -> dict[str, Any]:
    return _build_ground_truth_for_exact_judge(case)


def _openai_chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout_seconds: int,
) -> tuple[dict[str, Any], int]:
    try:
        from openai import OpenAI
    except Exception as exc:  # pragma: no cover - environment issue
        raise RuntimeError("openai client unavailable, please install `openai` dependency") from exc

    client = OpenAI(
        base_url=_normalize_openai_base_url(base_url),
        api_key=api_key,
        timeout=max(1, int(timeout_seconds)),
    )
    raw = client.chat.completions.with_raw_response.create(
        model=model,
        temperature=0,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    status_code = int(getattr(raw, "status_code", 200))
    parsed = raw.parse()
    data = parsed.model_dump() if hasattr(parsed, "model_dump") else {}
    if not isinstance(data, dict) or not data:
        raise RuntimeError("openai response parse failed")
    return data, status_code


async def _run_llm_judge(
    config: EvaluationConfig,
    *,
    case: EvalCase,
    task: EvalTask,
    run_dir: Path | None,
    repeat_dir: Path,
    query: str,
    source_desc: str,
    sink_types: list[str],
) -> dict[str, Any]:
    judge_input_path = repeat_dir / "llm_judge_input.json"
    judge_raw_response_path = repeat_dir / "llm_judge_raw_response.json"
    judge_result_path = repeat_dir / "llm_judge_result.json"

    base_result: dict[str, Any] = {
        "enabled": bool(config.llm_judge_enabled),
        "eligible": False,
        "skipped": False,
        "status": "disabled",
        "model": config.llm_judge_model,
        "endpoint": (
            build_chat_completions_endpoint(
                config.llm_judge_base_url,
                default_base_url=config.llm_judge_base_url,
            )
            if str(config.llm_judge_base_url or "").strip()
            else ""
        ),
        "input_path": str(judge_input_path),
        "raw_response_path": str(judge_raw_response_path),
        "result_path": str(judge_result_path),
        "verdict": "unknown",
        "is_correct": None,
        "confidence": None,
        "score": None,
        "summary": "",
        "reasons": [],
        "matched_true_flow_ids": [],
        "missed_true_flow_ids": [],
        "false_positive_flow_ids": [],
        "usage": {},
        "error": None,
    }
    if not config.llm_judge_enabled:
        base_result["skipped"] = True
        _json_dump(judge_result_path, base_result)
        return base_result
    if config.dummy_run:
        base_result["status"] = "skipped_dummy_run"
        base_result["skipped"] = True
        _json_dump(judge_result_path, base_result)
        return base_result
    has_ground_truth = any(
        isinstance(item.get("sink"), dict)
        for item in (case.sink_entries or [])
        if isinstance(item, dict)
    )
    if not has_ground_truth:
        base_result["status"] = "skipped_no_ground_truth"
        base_result["skipped"] = True
        _json_dump(judge_result_path, base_result)
        return base_result
    base_result["eligible"] = True
    if run_dir is None:
        base_result["status"] = "skipped_no_run_dir"
        base_result["skipped"] = True
        _json_dump(judge_result_path, base_result)
        return base_result

    report_payload = _load_final_report_for_judge(run_dir)
    if not report_payload.get("report_json_compact") and not report_payload.get("report_markdown"):
        base_result["status"] = "skipped_no_report"
        base_result["skipped"] = True
        _json_dump(judge_result_path, base_result)
        return base_result

    gt_payload = _build_ground_truth_for_judge(case)
    judge_input = {
        "task_meta": {
            "flow_id": case.flow_id,
            "source_id": case.source_id,
            "benchmark_family": case.benchmark_family,
            "mode": task.mode,
            "repeat_idx": task.repeat_idx,
            "query": query,
            "source_description": source_desc,
            "target_sink_categories": list(sink_types or config.sink_categories or DEFAULT_SINK_CATEGORIES),
        },
        "ground_truth": gt_payload,
        "final_report": report_payload,
    }
    _json_dump(judge_input_path, judge_input)

    system_prompt = (
        "你是严格的数据流分析评测裁判。"
        "你会拿到 ground truth（真实 source/sink/classification）和候选报告。"
        "你的任务是判断候选报告的最终结论是否与 ground truth 一致。"
        "必须严格输出 JSON，不要输出任何额外文本。"
    )
    user_prompt = (
        "请对比下面的 final_report 与 ground_truth，并输出固定 JSON。\n"
        "判定标准：\n"
        "1) 若报告正确识别了真实存在的关键流向，且没有把明显不存在的流向当成存在，可判 correct。\n"
        "2) 若只命中一部分或有明显遗漏，判 partially_correct。\n"
        "3) 若核心结论与 GT 相悖，判 incorrect。\n"
        "4) 若信息不足无法判断，判 unknown。\n\n"
        "输出 schema（字段必须齐全）:\n"
        "{\n"
        '  "schema_version": "flowark-llm-judge-v1",\n'
        '  "verdict": "correct|partially_correct|incorrect|unknown",\n'
        '  "is_correct": true|false|null,\n'
        '  "confidence": 0.0,\n'
        '  "score": 0.0,\n'
        '  "summary": "string",\n'
        '  "reasons": ["string"],\n'
        '  "matched_true_flow_ids": ["flow_id"],\n'
        '  "missed_true_flow_ids": ["flow_id"],\n'
        '  "false_positive_flow_ids": ["flow_id"]\n'
        "}\n\n"
        "待评估输入如下（JSON）:\n"
        f"{json.dumps(judge_input, ensure_ascii=False)}"
    )

    last_error: str | None = None
    raw_response: dict[str, Any] | None = None
    status_code: int | None = None
    started = time.time()
    max_attempts = max(1, int(config.llm_judge_max_retries) + 1)
    for attempt in range(max_attempts):
        try:
            raw_response, status_code = await asyncio.to_thread(
                _openai_chat_completion,
                base_url=config.llm_judge_base_url,
                api_key=config.llm_judge_api_key,
                model=config.llm_judge_model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                timeout_seconds=int(config.llm_judge_timeout_seconds),
            )
            break
        except Exception as exc:  # pragma: no cover - defensive
            last_error = str(exc)
            if attempt + 1 >= max_attempts:
                break
            await asyncio.sleep(min(2.0 * (attempt + 1), 6.0))

    if raw_response is None:
        base_result["status"] = "api_error"
        base_result["error"] = last_error
        base_result["latency_ms"] = int((time.time() - started) * 1000)
        _json_dump(judge_result_path, base_result)
        return base_result

    _json_dump(
        judge_raw_response_path,
        {
            "status_code": status_code,
            "response": raw_response,
        },
    )

    usage = raw_response.get("usage") if isinstance(raw_response.get("usage"), dict) else {}
    choices = raw_response.get("choices") if isinstance(raw_response.get("choices"), list) else []
    content_text = ""
    if choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                content_text = content
            elif isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    if isinstance(block, dict):
                        text = block.get("text")
                        if isinstance(text, str) and text.strip():
                            parts.append(text)
                content_text = "\n".join(parts)

    parsed_obj: dict[str, Any] | None = None
    parse_error: str | None = None
    if content_text.strip():
        try:
            parsed_obj = json.loads(content_text)
        except Exception:
            obj_text = _extract_first_json_object(content_text)
            if obj_text:
                try:
                    parsed_obj = json.loads(obj_text)
                except Exception as exc:
                    parse_error = f"json decode failed: {exc}"
            else:
                parse_error = "no json object found in model content"
    else:
        parse_error = "empty model content"

    if not isinstance(parsed_obj, dict):
        base_result["status"] = "parse_error"
        base_result["error"] = parse_error
        base_result["usage"] = {
            "input_tokens": _safe_int(usage.get("prompt_tokens") or usage.get("input_tokens")),
            "output_tokens": _safe_int(usage.get("completion_tokens") or usage.get("output_tokens")),
            "total_tokens": _safe_int(usage.get("total_tokens")),
        }
        base_result["latency_ms"] = int((time.time() - started) * 1000)
        _json_dump(judge_result_path, base_result)
        return base_result

    verdict = str(parsed_obj.get("verdict") or "unknown").strip().lower()
    if verdict not in {"correct", "partially_correct", "incorrect", "unknown"}:
        verdict = "unknown"
    is_correct_raw = parsed_obj.get("is_correct")
    is_correct = is_correct_raw if isinstance(is_correct_raw, bool) else None
    confidence = _safe_float(parsed_obj.get("confidence"))
    if confidence is not None:
        confidence = max(0.0, min(1.0, confidence))
    score = _safe_float(parsed_obj.get("score"))
    reasons = (
        [str(v) for v in (parsed_obj.get("reasons") or []) if str(v).strip()]
        if isinstance(parsed_obj.get("reasons"), list)
        else []
    )
    matched = (
        [str(v) for v in (parsed_obj.get("matched_true_flow_ids") or []) if str(v).strip()]
        if isinstance(parsed_obj.get("matched_true_flow_ids"), list)
        else []
    )
    missed = (
        [str(v) for v in (parsed_obj.get("missed_true_flow_ids") or []) if str(v).strip()]
        if isinstance(parsed_obj.get("missed_true_flow_ids"), list)
        else []
    )
    false_pos = (
        [str(v) for v in (parsed_obj.get("false_positive_flow_ids") or []) if str(v).strip()]
        if isinstance(parsed_obj.get("false_positive_flow_ids"), list)
        else []
    )

    result = {
        **base_result,
        "status": "ok",
        "skipped": False,
        "verdict": verdict,
        "is_correct": is_correct,
        "confidence": confidence,
        "score": score,
        "summary": str(parsed_obj.get("summary") or ""),
        "reasons": reasons,
        "matched_true_flow_ids": matched,
        "missed_true_flow_ids": missed,
        "false_positive_flow_ids": false_pos,
        "usage": {
            "input_tokens": _safe_int(usage.get("prompt_tokens") or usage.get("input_tokens")),
            "output_tokens": _safe_int(usage.get("completion_tokens") or usage.get("output_tokens")),
            "total_tokens": _safe_int(usage.get("total_tokens")),
        },
        "latency_ms": int((time.time() - started) * 1000),
        "raw_content": content_text,
    }
    _json_dump(judge_result_path, result)
    return result
