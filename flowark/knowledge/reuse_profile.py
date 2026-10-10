from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Sequence

from flowark.knowledge.reuse_digest import normalize_text

_MAX_MAIN_CORRIDORS = 5
_MAX_ANCHOR_NODES = 8
_MAX_BOUNDARY_NODES = 8


def _ordered_unique(values: Iterable[str], *, limit: int | None = None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if limit is not None and len(out) >= limit:
            break
    return out


def _corridor_text(nodes: Sequence[str]) -> str:
    cleaned = [str(node or "").strip() for node in nodes if str(node or "").strip()]
    return " -> ".join(cleaned)


def _profile_path_for_record(record: dict[str, Any]) -> list[str]:
    method_path = [
        str(node or "").strip()
        for node in (record.get("method_call_path") or [])
        if str(node or "").strip()
    ]
    if len(method_path) >= 2:
        return method_path
    return [
        str(node or "").strip()
        for node in (record.get("overlap_path") or record.get("exact_path") or [])
        if str(node or "").strip()
    ]


def _anchor_nodes_for_path(nodes: Sequence[str]) -> list[str]:
    if len(nodes) <= 2:
        return []
    return _ordered_unique(nodes[1:-1], limit=_MAX_ANCHOR_NODES)


def _boundary_nodes_for_path(nodes: Sequence[str]) -> list[str]:
    if not nodes:
        return []
    return _ordered_unique([nodes[-1]], limit=_MAX_BOUNDARY_NODES)


def build_summary_text(
    *,
    source_summary: str = "",
    corridors: Sequence[str] | None = None,
    anchor_nodes: Sequence[str] | None = None,
    boundary_nodes: Sequence[str] | None = None,
) -> str:
    lines: list[str] = []
    source = str(source_summary or "").strip()
    corridor_items = [str(item or "").strip() for item in (corridors or []) if str(item or "").strip()]
    anchor_items = [str(item or "").strip() for item in (anchor_nodes or []) if str(item or "").strip()]
    boundary_items = [str(item or "").strip() for item in (boundary_nodes or []) if str(item or "").strip()]
    if source:
        lines.append(f"source={source}")
    if corridor_items:
        lines.append("corridors:")
        lines.extend(f"- {item}" for item in corridor_items)
    if anchor_items:
        lines.append(f"anchors={', '.join(anchor_items)}")
    if boundary_items:
        lines.append(f"boundaries={', '.join(boundary_items)}")
    return "\n".join(lines).strip()


def build_current_case_profile(
    *,
    case_summary: dict[str, Any],
    report_path: Path,
    app_name: str | None = None,
) -> dict[str, Any]:
    path_records = list(case_summary.get("path_records") or [])
    main_corridors = _ordered_unique(
        (_corridor_text(_profile_path_for_record(record)) for record in path_records),
        limit=_MAX_MAIN_CORRIDORS,
    )
    anchor_nodes = _ordered_unique(
        (
            node
            for record in path_records[:_MAX_MAIN_CORRIDORS]
            for node in _anchor_nodes_for_path(_profile_path_for_record(record))
        ),
        limit=_MAX_ANCHOR_NODES,
    )
    boundary_nodes = _ordered_unique(
        (
            node
            for record in path_records[:_MAX_MAIN_CORRIDORS]
            for node in _boundary_nodes_for_path(_profile_path_for_record(record))
        ),
        limit=_MAX_BOUNDARY_NODES,
    )
    source_summary = str(case_summary.get("source_description") or "").strip()
    profile = {
        "source_summary": source_summary,
        "main_corridors": main_corridors,
        "anchor_nodes": anchor_nodes,
        "boundary_nodes": boundary_nodes,
        "summary_text": build_summary_text(
            source_summary=source_summary,
            corridors=main_corridors,
            anchor_nodes=anchor_nodes,
            boundary_nodes=boundary_nodes,
        ),
    }
    metadata = {
        "app_name": str(app_name or case_summary.get("app_name") or "").strip(),
        "case_id": str(case_summary.get("case_name") or "").strip(),
        "run_id": report_path.parent.name,
        "report_path": str(report_path),
    }
    return {"metadata": metadata, "profile": profile}


def build_current_case_profile_from_report(
    *,
    report_payload: dict[str, Any],
    case_name: str,
    session_name: str,
    report_path: Path,
    app_name: str | None = None,
) -> dict[str, Any]:
    case_summary = summarize_case_report_for_profile(
        report_payload,
        case_name=case_name,
        session_name=session_name,
        app_name_override=app_name,
    )
    return build_current_case_profile(
        case_summary=case_summary,
        report_path=report_path,
        app_name=app_name,
    )


def build_historical_profile_cards(
    *,
    report_paths: Sequence[Path],
    case_summaries: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for report_path, case_summary in zip(report_paths, case_summaries, strict=False):
        source_summary = str(case_summary.get("source_description") or "").strip()
        for idx, record in enumerate(list(case_summary.get("path_records") or []), start=1):
            nodes = _profile_path_for_record(record)
            corridor = _corridor_text(nodes)
            if not corridor:
                continue
            anchor_nodes = _anchor_nodes_for_path(nodes)
            boundary_nodes = _boundary_nodes_for_path(nodes)
            card = {
                "card_id": f"{str(case_summary.get('case_name') or 'case').strip()}:{idx}",
                "family": corridor,
                "corridor": corridor,
                "support_cases": 1,
                "anchor_nodes": anchor_nodes,
                "boundary_nodes": boundary_nodes,
                "summary_text": build_summary_text(
                    source_summary=source_summary,
                    corridors=[corridor],
                    anchor_nodes=anchor_nodes,
                    boundary_nodes=boundary_nodes,
                ),
            }
            metadata = {
                "app_name": str(case_summary.get("app_name") or "").strip(),
                "case_id": str(case_summary.get("case_name") or "").strip(),
                "run_id": report_path.parent.name,
                "report_path": str(report_path),
            }
            cards.append({"metadata": metadata, "card": card})
    return cards


def build_knowledge_profile_cards(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for item in items:
        summary = str(item.get("summary") or "").strip()
        boundary_summary = str(item.get("boundary_summary") or "").strip()
        if not summary and not boundary_summary:
            continue
        card = {
            "id": str(item.get("id") or "").strip(),
            "summary": summary,
            "status": str(item.get("validation_status") or "").strip(),
            "boundary_summary": boundary_summary,
            "summary_text": build_summary_text(
                source_summary=summary,
                boundary_nodes=_ordered_unique([boundary_summary]) if boundary_summary else [],
            ),
        }
        metadata = {
            "app_name": str(item.get("app_name") or "").strip(),
            "skill_path": str(item.get("skill_path") or "").strip(),
        }
        cards.append({"metadata": metadata, "card": card})
    return cards


def summarize_case_report_for_profile(
    report: dict[str, Any],
    *,
    case_name: str,
    session_name: str,
    app_name_override: str | None = None,
) -> dict[str, Any]:
    from flowark.knowledge.reuse_digest import build_case_summary

    return build_case_summary(
        case_name,
        report,
        session_name=session_name,
        app_name_override=normalize_text(app_name_override) or app_name_override,
    )
