from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Any, Mapping, Sequence

from openai import OpenAI

from flowark.eval.harness.common import _extract_first_json_object
from flowark.openai_compat import disabled_thinking_extra_body, normalize_chat_completions_base_url
from flowark.prompt_loader import get_prompt, render_prompt

_DEFAULT_MODEL = ""
_DEFAULT_TIMEOUT_SECONDS = 60
_MAX_SELECTED = 3
_MAX_RERANK_ATTEMPTS = 2


@dataclass(frozen=True)
class ReuseRerankConfig:
    base_url: str
    api_key: str
    model: str = _DEFAULT_MODEL
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS


def resolve_reuse_rerank_config(
    environ: Mapping[str, str] | None = None,
) -> ReuseRerankConfig | None:
    if environ is None:
        return None
    env = environ
    base_url = str(env.get("FLOWARK_REUSE_RERANK_BASE_URL") or "").strip()
    api_key = str(env.get("FLOWARK_REUSE_RERANK_API_KEY") or "").strip()
    if not base_url or not api_key:
        return None
    model = str(env.get("FLOWARK_REUSE_RERANK_MODEL") or _DEFAULT_MODEL).strip()
    if not model:
        return None
    timeout_raw = str(env.get("FLOWARK_REUSE_RERANK_TIMEOUT_SECONDS") or "").strip()
    timeout_seconds = _DEFAULT_TIMEOUT_SECONDS
    if timeout_raw:
        try:
            timeout_seconds = max(1, int(timeout_raw))
        except Exception:
            timeout_seconds = _DEFAULT_TIMEOUT_SECONDS
    return ReuseRerankConfig(
        base_url=normalize_chat_completions_base_url(base_url, default_base_url=base_url),
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
    )


def build_reuse_rerank_config(
    *,
    base_url: str | None,
    api_key: str | None,
    model: str | None = None,
    timeout_seconds: int | None = None,
) -> ReuseRerankConfig | None:
    resolved_base_url = str(base_url or "").strip()
    resolved_api_key = str(api_key or "").strip()
    resolved_model = str(model or _DEFAULT_MODEL).strip()
    if not resolved_base_url or not resolved_api_key or not resolved_model:
        return None
    return ReuseRerankConfig(
        base_url=normalize_chat_completions_base_url(
            resolved_base_url,
            default_base_url=resolved_base_url,
        ),
        api_key=resolved_api_key,
        model=resolved_model,
        timeout_seconds=max(1, int(timeout_seconds or _DEFAULT_TIMEOUT_SECONDS)),
    )


def _profile_payload(current_case_profile: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(current_case_profile, dict):
        return {}
    payload = current_case_profile.get("profile")
    return payload if isinstance(payload, dict) else {}


def _card_payload(entry: dict[str, Any]) -> dict[str, Any]:
    payload = entry.get("card")
    return payload if isinstance(payload, dict) else {}


def _metadata_payload(entry: dict[str, Any]) -> dict[str, Any]:
    payload = entry.get("metadata")
    return payload if isinstance(payload, dict) else {}


def _candidate_id(index: int) -> str:
    return f"c{index}"


def _historical_prompt_candidates(selected: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for idx, entry in enumerate(selected, start=1):
        card = _card_payload(entry)
        candidates.append(
            {
                "candidate_id": _candidate_id(idx),
                "family": str(card.get("family") or card.get("corridor") or "").strip(),
                "corridor": str(card.get("corridor") or "").strip(),
                "support_cases": int(card.get("support_cases") or 0),
                "anchor_nodes": list(card.get("anchor_nodes") or []),
                "boundary_nodes": list(card.get("boundary_nodes") or []),
                "summary_text": str(card.get("summary_text") or "").strip(),
            }
        )
    return candidates


def _build_historical_rerank_prompt(
    *,
    current_case_profile: dict[str, Any] | None,
    selected: Sequence[dict[str, Any]],
) -> str:
    profile = _profile_payload(current_case_profile)
    payload = {
        "current_case": {
            "source_summary": str(profile.get("source_summary") or "").strip(),
            "main_corridors": list(profile.get("main_corridors") or []),
            "anchor_nodes": list(profile.get("anchor_nodes") or []),
            "boundary_nodes": list(profile.get("boundary_nodes") or []),
            "summary_text": str(profile.get("summary_text") or "").strip(),
        },
        "candidates": _historical_prompt_candidates(selected),
    }
    schema = {
        "schema_version": "flowark-reuse-rerank-v1",
        "merge_groups": [
            {
                "group_id": "g1",
                "candidate_ids": ["c1", "c2"],
                "family": "shared family name",
            }
        ],
        "selected_order": ["g1", "c3"],
        "drop_ids": ["c4"],
    }
    return render_prompt(
        "historical_reuse_rerank",
        schema_example_json=json.dumps(schema, ensure_ascii=False),
        input_json=json.dumps(payload, ensure_ascii=False),
    )


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(part for part in parts if part).strip()
    return ""


def _usage_value(payload: Any, *keys: str) -> int | None:
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                return int(value)
    for key in keys:
        value = getattr(payload, key, None)
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
    return None


def _cost_value(payload: Any, *keys: str) -> float | None:
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, bool):
                return float(int(value))
            if isinstance(value, (int, float)):
                return float(value)
    for key in keys:
        value = getattr(payload, key, None)
        if isinstance(value, bool):
            return float(int(value))
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _normalize_llm_usage(payload: Any) -> dict[str, Any]:
    usage: dict[str, Any] = {}
    input_tokens = _usage_value(payload, "prompt_tokens", "input_tokens")
    output_tokens = _usage_value(payload, "completion_tokens", "output_tokens")
    total_tokens = _usage_value(payload, "total_tokens")
    if input_tokens is not None:
        usage["input_tokens"] = input_tokens
    if output_tokens is not None:
        usage["output_tokens"] = output_tokens
    if total_tokens is not None:
        usage["total_tokens"] = total_tokens
    return usage


def _openai_rerank_response(
    *,
    config: ReuseRerankConfig,
    prompt: str,
) -> dict[str, Any]:
    base_url = normalize_chat_completions_base_url(
        config.base_url,
        default_base_url=config.base_url,
    )
    client = OpenAI(
        base_url=base_url,
        api_key=config.api_key,
        timeout=max(1, int(config.timeout_seconds)),
    )
    started_at = time.perf_counter()
    raw = client.chat.completions.with_raw_response.create(
        model=config.model,
        temperature=0,
        messages=[
            {"role": "system", "content": get_prompt("historical_reuse_rerank_system").strip()},
            {"role": "user", "content": prompt},
        ],
        extra_body=disabled_thinking_extra_body(),
    )
    latency_ms = max(0, int((time.perf_counter() - started_at) * 1000))
    response = raw.parse()
    response_payload = response.model_dump() if hasattr(response, "model_dump") else {}
    choices = list(getattr(response, "choices", None) or [])
    message_text = ""
    if choices:
        message = getattr(choices[0], "message", None)
        message_text = _message_text(getattr(message, "content", None)).strip()
    usage_payload = (
        response_payload.get("usage")
        if isinstance(response_payload, dict)
        else None
    )
    if usage_payload is None:
        usage_payload = getattr(response, "usage", None)
    total_cost_usd = _cost_value(
        usage_payload,
        "total_cost_usd",
        "cost_usd",
        "costUSD",
        "cost",
    )
    if total_cost_usd is None and isinstance(response_payload, dict):
        total_cost_usd = _cost_value(
            response_payload,
            "total_cost_usd",
            "cost_usd",
            "costUSD",
            "cost",
        )
    return {
        "text": message_text,
        "llm_metrics": {
            "model": str(
                (response_payload.get("model") if isinstance(response_payload, dict) else None)
                or getattr(response, "model", None)
                or config.model
            ).strip()
            or config.model,
            "latency_ms": latency_ms,
            "usage": _normalize_llm_usage(usage_payload),
            "total_cost_usd": total_cost_usd,
        },
    }


def _normalize_id_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _normalize_merge_groups(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    groups: list[dict[str, Any]] = []
    seen_group_ids: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        group_id = str(raw.get("group_id") or "").strip()
        if not group_id or group_id in seen_group_ids:
            continue
        candidate_ids = _normalize_id_list(raw.get("candidate_ids"))
        if not candidate_ids:
            continue
        seen_group_ids.add(group_id)
        groups.append(
            {
                "group_id": group_id,
                "candidate_ids": candidate_ids,
                "family": str(raw.get("family") or "").strip(),
            }
        )
    return groups


def _resolved_selected_item(
    *,
    selected_id: str,
    candidate_map: Mapping[str, dict[str, Any]],
    merge_groups: Mapping[str, dict[str, Any]],
    used_candidate_ids: set[str],
) -> dict[str, Any] | None:
    if selected_id in merge_groups:
        group = merge_groups[selected_id]
        members: list[tuple[str, dict[str, Any]]] = []
        for candidate_id in group["candidate_ids"]:
            entry = candidate_map.get(candidate_id)
            if entry is None or candidate_id in used_candidate_ids:
                continue
            members.append((candidate_id, entry))
        if not members:
            return None
        for candidate_id, _ in members:
            used_candidate_ids.add(candidate_id)
        representative_id, representative_entry = members[0]
        representative_card = _card_payload(representative_entry)
        support_cases = 0
        for _, entry in members:
            card = _card_payload(entry)
            support_cases += max(1, int(card.get("support_cases") or 0))
        return {
            "group_id": selected_id,
            "family": str(group.get("family") or representative_card.get("family") or representative_card.get("corridor") or "").strip(),
            "corridor": str(representative_card.get("corridor") or "").strip(),
            "support_cases": int(support_cases),
            "member_candidate_ids": [candidate_id for candidate_id, _ in members],
            "metadata": _metadata_payload(representative_entry),
            "card": representative_card,
            "representative_candidate_id": representative_id,
        }
    entry = candidate_map.get(selected_id)
    if entry is None or selected_id in used_candidate_ids:
        return None
    used_candidate_ids.add(selected_id)
    card = _card_payload(entry)
    return {
        "group_id": selected_id,
        "family": str(card.get("family") or card.get("corridor") or "").strip(),
        "corridor": str(card.get("corridor") or "").strip(),
        "support_cases": max(1, int(card.get("support_cases") or 0)),
        "member_candidate_ids": [selected_id],
        "metadata": _metadata_payload(entry),
        "card": card,
        "representative_candidate_id": selected_id,
    }


def _has_strong_candidate_evidence(candidates: Sequence[dict[str, Any]]) -> bool:
    for entry in candidates:
        embedding_similarity = float(entry.get("embedding_similarity") or 0.0)
        boundary_overlap_count = int(entry.get("boundary_overlap_count") or 0)
        anchor_overlap_count = int(entry.get("anchor_overlap_count") or 0)
        corridor_token_overlap = int(entry.get("corridor_token_overlap") or 0)
        if boundary_overlap_count > 0 or anchor_overlap_count > 0:
            return True
        if corridor_token_overlap >= 5:
            return True
        if embedding_similarity >= 0.72 and corridor_token_overlap >= 2:
            return True
    return False


def _parse_historical_rerank_response(
    *,
    raw_text: str,
    candidates: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    obj_text = _extract_first_json_object(raw_text)
    if not obj_text:
        raise ValueError("historical rerank returned invalid JSON")
    try:
        payload = json.loads(obj_text)
    except Exception as exc:
        raise ValueError("historical rerank returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("historical rerank returned non-object JSON")

    merge_groups_list = _normalize_merge_groups(payload.get("merge_groups"))
    selected_order = _normalize_id_list(payload.get("selected_order"))[:_MAX_SELECTED]
    drop_ids = _normalize_id_list(payload.get("drop_ids"))
    candidate_map = {
        _candidate_id(idx): entry
        for idx, entry in enumerate(candidates, start=1)
    }
    merge_groups = {group["group_id"]: group for group in merge_groups_list}
    used_candidate_ids: set[str] = set()
    selected: list[dict[str, Any]] = []
    for selected_id in selected_order:
        item = _resolved_selected_item(
            selected_id=selected_id,
            candidate_map=candidate_map,
            merge_groups=merge_groups,
            used_candidate_ids=used_candidate_ids,
        )
        if item is None:
            continue
        item["final_rank"] = len(selected) + 1
        selected.append(item)
        if len(selected) >= _MAX_SELECTED:
            break
    if not selected:
        if selected_order:
            raise ValueError("historical rerank returned no usable selected items")
        if _has_strong_candidate_evidence(candidates):
            raise ValueError("historical rerank returned empty result despite strong candidates")

    return {
        "merge_groups": merge_groups_list,
        "selected_order": selected_order,
        "drop_ids": drop_ids,
        "selected": selected,
    }


def build_historical_rerank_result(
    *,
    current_case_profile: dict[str, Any] | None,
    historical_recall: dict[str, Any] | None,
    config: ReuseRerankConfig | None,
) -> dict[str, Any]:
    recall = historical_recall if isinstance(historical_recall, dict) else {}
    query_summary_text = str(recall.get("query_summary_text") or "").strip()
    candidates = list(recall.get("selected") or [])
    if not query_summary_text:
        return {
            "schema_version": "flowark-reuse-rerank-v1",
            "query_summary_text": "",
            "candidate_count": len(candidates),
            "merge_groups": [],
            "selected_order": [],
            "drop_ids": [],
            "selected": [],
            "used_fallback": False,
            "reason": "missing_query_profile",
            "llm_metrics": {},
        }
    if not candidates:
        return {
            "schema_version": "flowark-reuse-rerank-v1",
            "query_summary_text": query_summary_text,
            "candidate_count": 0,
            "merge_groups": [],
            "selected_order": [],
            "drop_ids": [],
            "selected": [],
            "used_fallback": False,
            "reason": str(recall.get("reason") or "no_candidates"),
            "llm_metrics": {},
        }
    if config is None:
        raise RuntimeError("historical rerank config missing")

    prompt = _build_historical_rerank_prompt(
        current_case_profile=current_case_profile,
        selected=candidates,
    )
    response_payload: dict[str, Any] | None = None
    parsed_payload: dict[str, Any] | None = None
    last_parse_error: Exception | None = None
    for attempt_idx in range(_MAX_RERANK_ATTEMPTS):
        try:
            response_payload = _openai_rerank_response(config=config, prompt=prompt)
        except Exception as exc:
            raise RuntimeError(
                f"historical rerank API failed: {type(exc).__name__}: {exc}"
            ) from exc
        raw_text = str(response_payload.get("text") or "").strip()
        try:
            parsed_payload = _parse_historical_rerank_response(
                raw_text=raw_text,
                candidates=candidates,
            )
            break
        except ValueError as exc:
            last_parse_error = exc
            if attempt_idx + 1 >= _MAX_RERANK_ATTEMPTS:
                raise
    if parsed_payload is None:
        if last_parse_error is not None:
            raise last_parse_error
        raise ValueError("historical rerank returned no usable selected items")

    return {
        "schema_version": "flowark-reuse-rerank-v1",
        "query_summary_text": query_summary_text,
        "candidate_count": len(candidates),
        "merge_groups": list(parsed_payload.get("merge_groups") or []),
        "selected_order": list(parsed_payload.get("selected_order") or []),
        "drop_ids": list(parsed_payload.get("drop_ids") or []),
        "selected": list(parsed_payload.get("selected") or []),
        "used_fallback": False,
        "reason": "ok",
        "llm_metrics": dict(response_payload.get("llm_metrics") or {}),
    }


def render_historical_reuse_guidance_block(
    rerank_result: dict[str, Any] | None,
    *,
    limit: int = _MAX_SELECTED,
) -> str:
    payload = rerank_result if isinstance(rerank_result, dict) else {}
    selected = list(payload.get("selected") or [])[: max(1, int(limit or _MAX_SELECTED))]
    if not selected:
        return ""
    lines = ["相关历史复用模式（仅供参考，不代表已覆盖）:"]
    for idx, item in enumerate(selected, start=1):
        family = str(item.get("family") or "").strip()
        corridor = str(item.get("corridor") or "").strip()
        support_cases = int(item.get("support_cases") or 0)
        if family:
            lines.append(f"{idx}. family={family}")
            if corridor:
                lines.append(f"   corridor={corridor}")
        elif corridor:
            lines.append(f"{idx}. corridor={corridor}")
        else:
            continue
        if support_cases > 0:
            lines.append(f"   support_cases={support_cases}")
    return "\n".join(lines).strip()


def render_similar_existing_knowledge_block(
    knowledge_recall: dict[str, Any] | None,
    *,
    limit: int = 2,
) -> str:
    payload = knowledge_recall if isinstance(knowledge_recall, dict) else {}
    selected = list(payload.get("selected") or [])[: max(1, int(limit or 2))]
    if not selected:
        return ""
    lines = ["相似已有知识（仅供参考，不代表必须复用）:"]
    for idx, entry in enumerate(selected, start=1):
        card = _card_payload(entry)
        skill_id = str(card.get("id") or "").strip()
        summary = str(card.get("summary") or "").strip()
        status = str(card.get("status") or "").strip()
        if not skill_id and not summary:
            continue
        if skill_id:
            lines.append(f"{idx}. id={skill_id}")
        else:
            lines.append(f"{idx}. summary={summary}")
            continue
        if summary:
            lines.append(f"   summary={summary}")
        if status:
            lines.append(f"   status={status}")
    return "\n".join(lines).strip()


def compose_reuse_guidance_block(*blocks: str) -> str:
    parts = [str(block or "").strip() for block in blocks if str(block or "").strip()]
    return "\n\n".join(parts).strip()
