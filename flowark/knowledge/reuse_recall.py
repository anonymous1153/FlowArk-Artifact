from __future__ import annotations

from dataclasses import dataclass
import math
import re
import time
from typing import Any, Mapping, Sequence

import httpx
from openai import APIConnectionError, APIStatusError, OpenAI

from flowark.openai_compat import normalize_chat_completions_base_url
from flowark.knowledge.reuse_digest import normalize_text

_QUERY_INSTRUCTION = (
    "Retrieve historical reusable dataflow corridors and existing knowledge "
    "that are semantically related to the current case's main analysis paths."
)
_EMBEDDING_TOP_K = 30
_FINAL_TOP_K = 20
_EMBEDDING_RETRY_COUNT = 3
_EMBEDDING_RETRY_BASE_DELAY_SECONDS = 1.0
_EMBEDDING_RETRY_STATUS_CODES = {408, 409, 429}
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.]+")


@dataclass(frozen=True)
class ReuseEmbeddingConfig:
    base_url: str
    api_key: str
    model: str = "qwen3-embedding-0.6b"
    verify_ssl: bool = False


def _env_bool(value: str | None, *, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def resolve_reuse_embedding_config(
    environ: Mapping[str, str] | None = None,
) -> ReuseEmbeddingConfig | None:
    if environ is None:
        return None
    env = environ
    base_url = str(env.get("FLOWARK_REUSE_EMBED_BASE_URL") or "").strip()
    api_key = str(env.get("FLOWARK_REUSE_EMBED_API_KEY") or "").strip()
    if not base_url or not api_key:
        return None
    model = str(env.get("FLOWARK_REUSE_EMBED_MODEL") or "qwen3-embedding-0.6b").strip() or "qwen3-embedding-0.6b"
    verify_ssl = _env_bool(env.get("FLOWARK_REUSE_EMBED_VERIFY_SSL"), default=False)
    return ReuseEmbeddingConfig(
        base_url=normalize_chat_completions_base_url(base_url, default_base_url=base_url),
        api_key=api_key,
        model=model,
        verify_ssl=verify_ssl,
    )


def build_reuse_embedding_config(
    *,
    base_url: str | None,
    api_key: str | None,
    model: str | None = None,
    verify_ssl: bool | None = None,
) -> ReuseEmbeddingConfig | None:
    resolved_base_url = str(base_url or "").strip()
    resolved_api_key = str(api_key or "").strip()
    if not resolved_base_url or not resolved_api_key:
        return None
    resolved_model = str(model or "qwen3-embedding-0.6b").strip() or "qwen3-embedding-0.6b"
    return ReuseEmbeddingConfig(
        base_url=normalize_chat_completions_base_url(
            resolved_base_url,
            default_base_url=resolved_base_url,
        ),
        api_key=resolved_api_key,
        model=resolved_model,
        verify_ssl=bool(verify_ssl),
    )


def build_query_embedding_text(summary_text: str) -> str:
    body = str(summary_text or "").strip()
    if not body:
        return ""
    return f"Instruct: {_QUERY_INSTRUCTION}\nQuery:\n{body}"


def _is_retryable_embedding_error(exc: Exception) -> bool:
    if isinstance(exc, APIConnectionError):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in _EMBEDDING_RETRY_STATUS_CODES or exc.status_code >= 500
    return False


def embed_texts(texts: Sequence[str], config: ReuseEmbeddingConfig) -> list[list[float]]:
    payload = [str(text or "").strip() for text in texts if str(text or "").strip()]
    if not payload:
        return []
    http_client = httpx.Client(verify=config.verify_ssl)
    try:
        client = OpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
            http_client=http_client,
        )
        for attempt_idx in range(_EMBEDDING_RETRY_COUNT + 1):
            try:
                response = client.embeddings.create(model=config.model, input=payload)
            except Exception as exc:
                if not _is_retryable_embedding_error(exc) or attempt_idx >= _EMBEDDING_RETRY_COUNT:
                    raise
                time.sleep(_EMBEDDING_RETRY_BASE_DELAY_SECONDS * (2**attempt_idx))
                continue
            return [list(item.embedding) for item in response.data]
    finally:
        http_client.close()


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(value) * float(value) for value in left))
    right_norm = math.sqrt(sum(float(value) * float(value) for value in right))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _token_set(*texts: str) -> set[str]:
    out: set[str] = set()
    for text in texts:
        for token in _TOKEN_RE.findall(str(text or "").lower()):
            if token:
                out.add(token)
    return out


def _count_overlap(left: Sequence[str], right: Sequence[str]) -> int:
    return len({str(item or "").strip().casefold() for item in left if str(item or "").strip()} & {str(item or "").strip().casefold() for item in right if str(item or "").strip()})


def _current_profile_payload(current_case_profile: dict[str, Any] | None) -> dict[str, Any]:
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


def _boundary_nodes_for_card(card: dict[str, Any]) -> list[str]:
    boundary_nodes = list(card.get("boundary_nodes") or [])
    if boundary_nodes:
        return [str(item or "").strip() for item in boundary_nodes if str(item or "").strip()]
    boundary_summary = str(card.get("boundary_summary") or "").strip()
    return [boundary_summary] if boundary_summary else []


def _anchor_nodes_for_card(card: dict[str, Any]) -> list[str]:
    return [str(item or "").strip() for item in list(card.get("anchor_nodes") or []) if str(item or "").strip()]


def _corridor_text_for_card(card: dict[str, Any]) -> str:
    return str(card.get("corridor") or card.get("summary_text") or "").strip()


def hard_filter_historical_cards(
    cards: Sequence[dict[str, Any]],
    *,
    app_name: str,
    current_case_id: str,
    current_run_id: str,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    target_app_name = normalize_text(app_name)
    for entry in cards:
        metadata = _metadata_payload(entry)
        card = _card_payload(entry)
        if normalize_text(metadata.get("app_name")) != target_app_name:
            continue
        if str(metadata.get("case_id") or "").strip() == current_case_id:
            continue
        if str(metadata.get("run_id") or "").strip() == current_run_id:
            continue
        if not str(card.get("summary_text") or "").strip():
            continue
        filtered.append(entry)
    return filtered


def hard_filter_knowledge_cards(
    cards: Sequence[dict[str, Any]],
    *,
    app_name: str,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    target_app_name = normalize_text(app_name)
    for entry in cards:
        metadata = _metadata_payload(entry)
        card = _card_payload(entry)
        entry_app_name = normalize_text(metadata.get("app_name"))
        if entry_app_name and entry_app_name != target_app_name:
            continue
        if str(card.get("status") or "").strip().lower() != "validated":
            continue
        if not str(card.get("summary_text") or "").strip():
            continue
        filtered.append(entry)
    return filtered


def _empty_recall_artifact(
    *,
    query_summary_text: str,
    candidate_count: int,
    reason: str,
    error: BaseException | None = None,
) -> dict[str, Any]:
    payload = {
        "query_summary_text": str(query_summary_text or "").strip(),
        "candidate_count": int(candidate_count),
        "selected": [],
        "reason": reason,
    }
    if error is not None:
        payload["error_type"] = type(error).__name__
        payload["error_message"] = str(error)[:500]
    return payload


def _build_recall_candidates(
    *,
    current_case_profile: dict[str, Any] | None,
    candidate_cards: Sequence[dict[str, Any]],
    config: ReuseEmbeddingConfig | None,
) -> dict[str, Any]:
    profile = _current_profile_payload(current_case_profile)
    query_summary_text = str(profile.get("summary_text") or "").strip()
    if not query_summary_text:
        return _empty_recall_artifact(
            query_summary_text="",
            candidate_count=len(candidate_cards),
            reason="missing_query_profile",
        )
    if not candidate_cards:
        return _empty_recall_artifact(
            query_summary_text=query_summary_text,
            candidate_count=0,
            reason="no_candidates",
        )
    if config is None:
        return _empty_recall_artifact(
            query_summary_text=query_summary_text,
            candidate_count=len(candidate_cards),
            reason="embedding_config_missing",
        )
    try:
        query_embedding = embed_texts([build_query_embedding_text(query_summary_text)], config)
        card_embeddings = embed_texts(
            [str(_card_payload(entry).get("summary_text") or "").strip() for entry in candidate_cards],
            config,
        )
    except Exception as exc:
        return _empty_recall_artifact(
            query_summary_text=query_summary_text,
            candidate_count=len(candidate_cards),
            reason="embed_error",
            error=exc,
        )
    if not query_embedding or len(card_embeddings) != len(candidate_cards):
        return _empty_recall_artifact(
            query_summary_text=query_summary_text,
            candidate_count=len(candidate_cards),
            reason="embed_error",
        )

    current_boundaries = list(profile.get("boundary_nodes") or [])
    current_anchors = list(profile.get("anchor_nodes") or [])
    current_corridor_tokens = _token_set(
        *[str(item or "").strip() for item in list(profile.get("main_corridors") or [])]
    )
    if not current_corridor_tokens:
        current_corridor_tokens = _token_set(query_summary_text)

    by_similarity: list[tuple[float, dict[str, Any]]] = []
    query_vector = query_embedding[0]
    for entry, card_vector in zip(candidate_cards, card_embeddings, strict=False):
        similarity = _cosine_similarity(query_vector, card_vector)
        by_similarity.append((similarity, entry))
    by_similarity.sort(key=lambda item: item[0], reverse=True)
    shortlisted = by_similarity[:_EMBEDDING_TOP_K]

    scored: list[dict[str, Any]] = []
    for embedding_similarity, entry in shortlisted:
        card = _card_payload(entry)
        boundary_overlap_count = _count_overlap(current_boundaries, _boundary_nodes_for_card(card))
        anchor_overlap_count = _count_overlap(current_anchors, _anchor_nodes_for_card(card))
        corridor_token_overlap = len(current_corridor_tokens & _token_set(_corridor_text_for_card(card)))
        final_score = (
            float(embedding_similarity)
            + (0.05 * boundary_overlap_count)
            + (0.03 * anchor_overlap_count)
            + (0.01 * corridor_token_overlap)
        )
        scored.append(
            {
                "metadata": _metadata_payload(entry),
                "card": card,
                "embedding_similarity": float(embedding_similarity),
                "boundary_overlap_count": int(boundary_overlap_count),
                "anchor_overlap_count": int(anchor_overlap_count),
                "corridor_token_overlap": int(corridor_token_overlap),
                "_final_score": final_score,
            }
        )
    scored.sort(
        key=lambda item: (
            float(item["_final_score"]),
            float(item["embedding_similarity"]),
            int(item["boundary_overlap_count"]),
            int(item["anchor_overlap_count"]),
            int(item["corridor_token_overlap"]),
        ),
        reverse=True,
    )

    selected: list[dict[str, Any]] = []
    seen_corridors: set[str] = set()
    seen_summaries: set[str] = set()
    for item in scored:
        card = item["card"]
        corridor_key = str(card.get("corridor") or "").strip().casefold()
        summary_key = str(card.get("summary_text") or "").strip().casefold()
        if corridor_key and corridor_key in seen_corridors:
            continue
        if summary_key and summary_key in seen_summaries:
            continue
        if corridor_key:
            seen_corridors.add(corridor_key)
        if summary_key:
            seen_summaries.add(summary_key)
        selected.append(
            {
                "metadata": item["metadata"],
                "card": card,
                "embedding_similarity": item["embedding_similarity"],
                "boundary_overlap_count": item["boundary_overlap_count"],
                "anchor_overlap_count": item["anchor_overlap_count"],
                "corridor_token_overlap": item["corridor_token_overlap"],
                "final_rank": len(selected) + 1,
            }
        )
        if len(selected) >= _FINAL_TOP_K:
            break

    return {
        "query_summary_text": query_summary_text,
        "candidate_count": len(candidate_cards),
        "selected": selected,
        "reason": "ok",
    }


def build_historical_recall_candidates(
    *,
    current_case_profile: dict[str, Any] | None,
    cards: Sequence[dict[str, Any]],
    app_name: str,
    current_case_id: str,
    current_run_id: str,
    config: ReuseEmbeddingConfig | None,
) -> dict[str, Any]:
    filtered_cards = hard_filter_historical_cards(
        cards,
        app_name=app_name,
        current_case_id=current_case_id,
        current_run_id=current_run_id,
    )
    return _build_recall_candidates(
        current_case_profile=current_case_profile,
        candidate_cards=filtered_cards,
        config=config,
    )


def build_knowledge_recall_candidates(
    *,
    current_case_profile: dict[str, Any] | None,
    cards: Sequence[dict[str, Any]],
    app_name: str,
    config: ReuseEmbeddingConfig | None,
) -> dict[str, Any]:
    filtered_cards = hard_filter_knowledge_cards(cards, app_name=app_name)
    return _build_recall_candidates(
        current_case_profile=current_case_profile,
        candidate_cards=filtered_cards,
        config=config,
    )
