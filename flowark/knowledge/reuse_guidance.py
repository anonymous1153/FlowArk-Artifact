"""Render guidance from the candidates selected by knowledge recall."""

from __future__ import annotations

from typing import Any


def _card_payload(entry: dict[str, Any]) -> dict[str, Any]:
    payload = entry.get("card")
    return payload if isinstance(payload, dict) else {}


def render_historical_reuse_guidance_block(
    historical_recall: dict[str, Any] | None,
) -> str:
    payload = historical_recall if isinstance(historical_recall, dict) else {}
    selected = list(payload.get("selected") or [])
    if not selected:
        return ""
    lines = ["相关历史复用模式（仅供参考，不代表已覆盖）:"]
    for idx, entry in enumerate(selected, start=1):
        card = _card_payload(entry)
        family = str(card.get("family") or "").strip()
        corridor = str(card.get("corridor") or "").strip()
        support_cases = int(card.get("support_cases") or 0)
        if family:
            lines.append(f"{idx}. family={family}")
            if corridor:
                lines.append(f"   corridor={corridor}")
        elif corridor:
            lines.append(f"{idx}. corridor={corridor}")
        else:
            raise ValueError("Historical recall candidate has no family or corridor")
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
