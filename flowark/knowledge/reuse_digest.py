from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Sequence

LOCATION_RE = re.compile(r"(?P<path>.*?)(?::|@)(?P<line>\d+)(?:[-:]?(?P<end>\d+))?$")
STRING_LITERAL_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
NUMBER_LITERAL_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
BOOL_LITERAL_RE = re.compile(r"\b(?:true|false)\b", re.IGNORECASE)
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|<str>|<num>|<bool>|<slot>|\.\.\.|\S")
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SIMPLE_ATOM_RE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*|<str>|<num>|<bool>|<slot>|\.\.\.)$"
)
SIMPLE_CALL_RE = re.compile(r"(?P<head>[A-Za-z_][A-Za-z0-9_.]*)\((?P<args>[^()]*)\)")
METHOD_CALL_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)+\s*\([^()]*\)")
SIGNATURE_METHOD_RE = re.compile(r"(?P<name>[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)?)\s*\([^()]*\)")
CALLBACK_GLUE_MARKERS = ("onvaluechange", "ontimechange", "onclick")
ACTIVITY_GLUE_MARKERS = ("startactivity", "startforegroundservice", "context.startactivity")
LOG_GLUE_MARKERS = ("log.",)
CONCRETE_BOUNDARY_PREFIXES = ("network:", "database:", "storage:", "file:", "icc:")
LIVE_DIGEST_MIN_COMPLETED_REPORTS = 5
LIVE_DIGEST_TOP_K = 3


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    text = value.replace("`", "")
    text = re.sub(r"@\d+(?::\d+)?", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().casefold()



def _is_simple_call_args(arg_text: str) -> bool:
    args = [part.strip() for part in str(arg_text or "").split(",")]
    return bool(args) and all(part and SIMPLE_ATOM_RE.match(part) for part in args)



def _collapse_simple_call_args(value: str) -> str:
    text = value
    while True:
        changed = False

        def _replace(match: re.Match[str]) -> str:
            nonlocal changed
            head = match.group("head")
            args = match.group("args").strip()
            if not args:
                return match.group(0)
            if not _is_simple_call_args(args):
                return match.group(0)
            replacement = f"{head}(...)"
            if replacement != match.group(0):
                changed = True
            return replacement

        updated = SIMPLE_CALL_RE.sub(_replace, text)
        if not changed:
            return updated
        text = updated



def normalize_step_for_overlap(value: str | None) -> str:
    text = normalize_text(value)
    if not text:
        return ""
    text = re.sub(r":\d+(?:-\d+)?", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()



def normalize_literal_tokens(value: str) -> str:
    text = value
    text = STRING_LITERAL_RE.sub("<str>", text)
    text = NUMBER_LITERAL_RE.sub("<num>", text)
    text = BOOL_LITERAL_RE.sub("<bool>", text)
    text = _collapse_simple_call_args(text)
    text = re.sub(r"\b([A-Za-z_][A-Za-z0-9_.]*)\(\)", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()



def tokenize_for_family(value: str) -> list[str]:
    return TOKEN_RE.findall(value)



def detokenize_tokens(tokens: Sequence[str]) -> str:
    text = " ".join(tokens)
    text = re.sub(r"\s+([.,)\]}>])", r"\1", text)
    text = re.sub(r"([(<\[{])\s+", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()



def _identifier_token_count(tokens: Sequence[str]) -> int:
    return sum(1 for token in tokens if IDENTIFIER_RE.match(token))



def _single_span_abstract(tokens_a: Sequence[str], tokens_b: Sequence[str]) -> tuple[str, int] | None:
    if tuple(tokens_a) == tuple(tokens_b):
        return None
    if len(tokens_a) != len(tokens_b):
        return None
    differing = [idx for idx, (a, b) in enumerate(zip(tokens_a, tokens_b)) if a != b]
    if not differing:
        return None
    if differing[-1] - differing[0] + 1 != len(differing):
        return None
    start, end = differing[0], differing[-1]
    prefix = list(tokens_a[:start])
    suffix = list(tokens_a[end + 1 :])
    if _identifier_token_count(prefix) + _identifier_token_count(suffix) < 2:
        return None
    abstract = prefix + ["<slot>"] + suffix
    return detokenize_tokens(abstract), len(differing)



def location_to_path(location: str | None) -> str:
    if not location:
        return ""
    match = LOCATION_RE.match(location.strip())
    if not match:
        return location.strip()
    return match.group("path").strip()



def simplify_step(step: dict[str, Any]) -> str:
    src = normalize_text(step.get("from"))
    dst = normalize_text(step.get("to"))
    desc = normalize_text(step.get("description"))
    if src and dst:
        return f"{src} -> {dst}"
    return src or dst or desc



def _ordered_unique_text(values: Iterable[str]) -> list[str]:
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
    return out



def _dedupe_adjacent(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    previous = ""
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key == previous:
            continue
        previous = key
        out.append(text)
    return out



def normalize_method_call_for_overlap(value: str | None, *, allow_bare: bool = False) -> str:
    text = normalize_step_for_overlap(value)
    if not text:
        return ""
    match = SIGNATURE_METHOD_RE.search(text)
    if match:
        text = f"{match.group('name')}(...)"
    else:
        if not allow_bare and "." not in text:
            return ""
        text = text.split()[-1] if " " in text else text
    text = normalize_literal_tokens(text)
    text = re.sub(r"\(\s*\)", "(...)", text)
    return text.strip()



def _method_calls_from_text(value: str | None) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    return _ordered_unique_text(
        normalize_method_call_for_overlap(match.group(0), allow_bare=False)
        for match in METHOD_CALL_RE.finditer(text)
    )



def _method_field_node(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "." not in raw and "(" not in raw:
        return ""
    if "." in raw and "(" not in raw:
        raw = f"{raw}(...)"
    return normalize_method_call_for_overlap(raw, allow_bare=True)



def _explicit_method_chain_nodes(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    raw_items: list[str] = []
    for item in value:
        if isinstance(item, dict):
            raw = item.get("method") or item.get("call") or item.get("statement") or item.get("name")
        else:
            raw = item
        text = str(raw or "").strip()
        if text:
            raw_items.append(text)
    return _dedupe_adjacent(
        normalize_method_call_for_overlap(item, allow_bare=True)
        for item in raw_items
    )



def _dataflow_method_call_nodes(
    dataflow: dict[str, Any],
    *,
    source_method: str | None = None,
) -> list[str]:
    explicit = _explicit_method_chain_nodes(dataflow.get("method_call_chain"))
    if len(explicit) >= 2:
        return explicit

    nodes: list[str] = []
    source_node = _method_field_node(source_method)
    if source_node:
        nodes.append(source_node)
    for step in dataflow.get("path") or []:
        if not isinstance(step, dict):
            continue
        step_method = _method_field_node(step.get("method"))
        if step_method:
            nodes.append(step_method)
        nodes.extend(_method_calls_from_text(step.get("from")))
        nodes.extend(_method_calls_from_text(step.get("to")))
    sink = dataflow.get("sink") or {}
    if isinstance(sink, dict):
        nodes.extend(_method_calls_from_text(sink.get("statement")))
        sink_method = _method_field_node(sink.get("method"))
        if sink_method:
            nodes.append(sink_method)
    return _dedupe_adjacent(nodes)



def _preferred_overlap_path(record: dict[str, Any]) -> list[str]:
    method_path = [
        str(node or "").strip()
        for node in (record.get("method_call_path") or [])
        if str(node or "").strip()
    ]
    if len(method_path) >= 2:
        return method_path
    return [
        str(node or "").strip()
        for node in (record.get("exact_path") or [])
        if str(node or "").strip()
    ]



def _dataflow_file_refs(dataflow: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    source = dataflow.get("source") or {}
    sink = dataflow.get("sink") or {}
    source_loc = location_to_path(source.get("location"))
    sink_loc = location_to_path(sink.get("location"))
    if source_loc:
        refs.add(source_loc)
    if sink_loc:
        refs.add(sink_loc)
    for step in dataflow.get("path") or []:
        loc = location_to_path(step.get("location"))
        if loc:
            refs.add(loc)
    return refs



def _dataflow_path_nodes(dataflow: dict[str, Any]) -> list[str]:
    path = dataflow.get("path") or []
    if not path:
        return []
    nodes: list[str] = []
    first_from = normalize_step_for_overlap(path[0].get("from"))
    if first_from:
        nodes.append(first_from)
    for step in path:
        dst = normalize_step_for_overlap(step.get("to"))
        if dst:
            nodes.append(dst)
    return [node for node in nodes if node]



def infer_case_name_from_report_path(report_path: Path) -> str:
    try:
        return report_path.parents[3].name
    except IndexError:
        return report_path.parent.parent.parent.name if len(report_path.parents) >= 3 else report_path.stem



def _load_eval_root_metadata(eval_root: Path) -> tuple[dict[str, str], str]:
    case_to_app: dict[str, str] = {}
    default_app = "unknown"

    manifest_path = eval_root / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}
        cases = manifest.get("cases") or []
        for item in cases:
            if not isinstance(item, dict):
                continue
            flow_id = item.get("flow_id")
            app_name = item.get("app_name")
            if flow_id and app_name:
                case_to_app[str(flow_id)] = normalize_text(str(app_name)) or str(app_name)
        manifest_apps = sorted({value for value in case_to_app.values() if value and value != "unknown"})
        if len(manifest_apps) == 1:
            default_app = manifest_apps[0]

    config_path = eval_root / "config.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            config = {}
        config_apps = [normalize_text(str(app)) for app in (config.get("app_names") or []) if normalize_text(str(app))]
        if len(config_apps) == 1:
            default_app = config_apps[0]

    if default_app == "unknown":
        skills_dir = eval_root / "knowledge_scope" / "skills"
        if skills_dir.exists():
            skill_apps = sorted(path.name for path in skills_dir.iterdir() if path.is_dir())
            if len(skill_apps) == 1:
                default_app = normalize_text(skill_apps[0]) or skill_apps[0]

    return case_to_app, default_app



def iter_final_reports(eval_roots: Sequence[Path]) -> Iterable[tuple[Path, str, Path]]:
    for eval_root in eval_roots:
        for report_path in sorted(eval_root.rglob("final_report.json")):
            yield eval_root, infer_case_name_from_report_path(report_path), report_path



def load_final_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))



def classify_sink_signature(dataflow: dict[str, Any]) -> str:
    sink = dataflow.get("sink") or {}
    sink_type = normalize_text(sink.get("sink_type")) or normalize_text(sink.get("type")) or "unknown"
    sink_method = normalize_text(sink.get("method"))
    sink_desc = normalize_text(sink.get("statement")) or normalize_text(sink.get("description"))
    detail = sink_method or sink_desc or "sink"
    return f"{sink_type}:{detail}"



def build_case_summary(
    case_name: str,
    report: dict[str, Any],
    *,
    session_name: str,
    app_name_override: str | None = None,
) -> dict[str, Any]:
    dataflows = report.get("dataflows") or []
    knowledge_used = report.get("knowledge_used") or {}
    note_ids = [
        note.get("note_id")
        for note in knowledge_used.get("notes") or []
        if isinstance(note, dict) and note.get("note_id")
    ]
    app_name = normalize_text(report.get("app_name")) or normalize_text(app_name_override) or "unknown"
    source_desc = normalize_text((report.get("source") or {}).get("description"))
    source_method = (report.get("source") or {}).get("method") if isinstance(report.get("source"), dict) else None
    sink_signatures: list[str] = []
    file_refs: set[str] = set()
    path_nodes: list[list[str]] = []
    descriptions: list[str] = []
    path_records: list[dict[str, Any]] = []

    for dataflow in dataflows:
        sink_signatures.append(classify_sink_signature(dataflow))
        file_refs.update(_dataflow_file_refs(dataflow))
        nodes = _dataflow_path_nodes(dataflow)
        method_nodes = _dataflow_method_call_nodes(dataflow, source_method=source_method)
        overlap_nodes = method_nodes if len(method_nodes) >= 2 else nodes
        if overlap_nodes:
            path_nodes.append(nodes)
            descriptions.extend(simplify_step(step) for step in (dataflow.get("path") or []))
            path_records.append(
                {
                    "exact_path": nodes,
                    "method_call_path": method_nodes,
                    "overlap_path": overlap_nodes,
                    "path_granularity": "method_call" if len(method_nodes) >= 2 else "atomic_hop_fallback",
                    "file_refs": sorted(_dataflow_file_refs(dataflow)),
                    "sink_signatures": [classify_sink_signature(dataflow)],
                }
            )

    return {
        "case_name": case_name,
        "session_name": session_name,
        "app_name": app_name,
        "source_description": source_desc,
        "dataflow_count": len(dataflows),
        "sink_signatures": sorted(set(sink_signatures)),
        "file_refs": sorted(file_refs),
        "path_nodes": path_nodes,
        "path_records": path_records,
        "knowledge_note_ids": note_ids,
        "descriptions": sorted({desc for desc in descriptions if desc}),
    }



def summarize_eval_roots(eval_roots: Sequence[Path]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    metadata_cache: dict[Path, tuple[dict[str, str], str]] = {}
    for eval_root, case_name, report_path in iter_final_reports(eval_roots):
        session_name = eval_root.name
        if eval_root not in metadata_cache:
            metadata_cache[eval_root] = _load_eval_root_metadata(eval_root)
        case_to_app, default_app = metadata_cache[eval_root]
        report = load_final_report(report_path)
        app_name_override = case_to_app.get(case_name, default_app)
        summaries.append(
            build_case_summary(
                case_name,
                report,
                session_name=session_name,
                app_name_override=app_name_override,
            )
        )
    return summaries



def summarize_report_paths(
    report_paths: Sequence[Path],
    *,
    session_name: str,
    app_name_override: str | None = None,
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for report_path in sorted(report_paths):
        try:
            report = load_final_report(report_path)
        except Exception:
            continue
        case_name = infer_case_name_from_report_path(report_path)
        summary = build_case_summary(
            case_name,
            report,
            session_name=session_name,
            app_name_override=app_name_override,
        )
        if summary.get("path_records"):
            summaries.append(summary)
    return summaries



def group_by_app(cases: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        grouped[case.get("app_name") or "unknown"].append(case)
    return dict(grouped)



def _sliding_windows(nodes: Sequence[str], size: int) -> Iterable[tuple[str, ...]]:
    if len(nodes) < size:
        return
    for idx in range(len(nodes) - size + 1):
        yield tuple(nodes[idx : idx + size])



def _canonical_subpath(subpath: Sequence[str]) -> str:
    return " -> ".join(subpath)



def _family_variant_stats(path_records: Sequence[dict[str, Any]]) -> dict[tuple[str, ...], set[tuple[str, ...]]]:
    sibling_groups: dict[tuple[str | None, str | None], list[str]] = defaultdict(list)
    for record in path_records:
        nodes = record["exact_path"]
        literal_nodes = [normalize_literal_tokens(node) for node in nodes]
        for idx, node in enumerate(literal_nodes):
            prev_node = literal_nodes[idx - 1] if idx > 0 else None
            next_node = literal_nodes[idx + 1] if idx + 1 < len(literal_nodes) else None
            sibling_groups[(prev_node, next_node)].append(node)

    family_map: dict[tuple[str | None, str | None, str], str] = {}
    for (prev_node, next_node), values in sibling_groups.items():
        unique_values = sorted(set(values))
        if len(unique_values) < 2:
            continue
        tokenized = {value: tokenize_for_family(value) for value in unique_values}
        value_to_best: dict[str, tuple[str, int]] = {}
        for idx, left in enumerate(unique_values):
            for right in unique_values[idx + 1 :]:
                abstract = _single_span_abstract(tokenized[left], tokenized[right])
                if abstract is None:
                    continue
                abstract_value, diff_span = abstract
                for current in (left, right):
                    best = value_to_best.get(current)
                    if best is None or diff_span < best[1]:
                        value_to_best[current] = (abstract_value, diff_span)
        group_by_abstract: dict[str, set[str]] = defaultdict(set)
        for value, (abstract_value, _span) in value_to_best.items():
            group_by_abstract[abstract_value].add(value)
        for abstract_value, members in group_by_abstract.items():
            if len(members) < 2:
                continue
            for member in members:
                family_map[(prev_node, next_node, member)] = abstract_value

    family_variants: dict[tuple[str, ...], set[tuple[str, ...]]] = defaultdict(set)
    for record in path_records:
        exact_nodes = tuple(record["exact_path"])
        literal_nodes = [normalize_literal_tokens(node) for node in exact_nodes]
        family_nodes: list[str] = []
        for idx, node in enumerate(literal_nodes):
            prev_node = literal_nodes[idx - 1] if idx > 0 else None
            next_node = literal_nodes[idx + 1] if idx + 1 < len(literal_nodes) else None
            family_nodes.append(family_map.get((prev_node, next_node, node), node))
        record["family_path"] = family_nodes
        for start in range(len(exact_nodes)):
            for end in range(start + 2, len(exact_nodes) + 1):
                family_subpath = tuple(family_nodes[start:end])
                family_variants[family_subpath].add(tuple(exact_nodes[start:end]))
    return family_variants



def _make_corridor_store() -> dict[tuple[str, ...], dict[str, Any]]:
    return {}



def _ensure_corridor(store: dict[tuple[str, ...], dict[str, Any]], subpath: tuple[str, ...]) -> dict[str, Any]:
    item = store.get(subpath)
    if item is None:
        item = {
            "subpath": subpath,
            "support_cases": set(),
            "support_sessions": set(),
            "file_refs": set(),
            "sink_signatures": set(),
            "example_flows": [],
            "_example_seen": set(),
            "subpath_length": len(subpath),
            "left_extensions": Counter(),
            "right_extensions": Counter(),
            "exact_variants": set(),
            "path_granularity_counts": Counter(),
        }
        store[subpath] = item
    return item



def _record_example(item: dict[str, Any], *, case_name: str, session_name: str, limit: int) -> None:
    key = (case_name, session_name)
    if key in item["_example_seen"] or len(item["example_flows"]) >= limit:
        return
    item["_example_seen"].add(key)
    item["example_flows"].append({"case_name": case_name, "session_name": session_name})



def _collect_corridors(
    path_records: Sequence[dict[str, Any]],
    *,
    example_limit: int,
    view: str,
    family_variants: dict[tuple[str, ...], set[tuple[str, ...]]] | None = None,
) -> dict[tuple[str, ...], dict[str, Any]]:
    store = _make_corridor_store()
    for record in path_records:
        nodes = tuple(record[f"{view}_path"])
        if len(nodes) < 2:
            continue
        case_name = record["case_name"]
        session_name = record["session_name"]
        file_refs = record["file_refs"]
        sink_signatures = record["sink_signatures"]
        for start in range(len(nodes)):
            for end in range(start + 2, len(nodes) + 1):
                subpath = nodes[start:end]
                item = _ensure_corridor(store, subpath)
                item["support_cases"].add(case_name)
                item["support_sessions"].add(session_name)
                item["file_refs"].update(file_refs)
                item["sink_signatures"].update(sink_signatures)
                item["path_granularity_counts"][str(record.get("path_granularity") or "unknown")] += 1
                _record_example(item, case_name=case_name, session_name=session_name, limit=example_limit)
                if view == "family" and family_variants is not None:
                    item["exact_variants"].update(family_variants.get(subpath, set()))
                else:
                    item["exact_variants"].add(tuple(record["exact_path"][start:end]))
                if start > 0:
                    item["left_extensions"][nodes[start - 1]] += 1
                if end < len(nodes):
                    item["right_extensions"][nodes[end]] += 1
    return store



def _finalize_corridor_metrics(store: dict[tuple[str, ...], dict[str, Any]]) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    support_lookup = {subpath: len(item["support_cases"]) for subpath, item in store.items()}
    for subpath, item in store.items():
        shrink_supports: list[int] = []
        if len(subpath) > 2:
            left = subpath[1:]
            right = subpath[:-1]
            if left in support_lookup:
                shrink_supports.append(support_lookup[left])
            if right in support_lookup:
                shrink_supports.append(support_lookup[right])
        best_shrink = max(shrink_supports) if shrink_supports else len(item["support_cases"])
        left_extend = max(item["left_extensions"].values()) if item["left_extensions"] else 0
        right_extend = max(item["right_extensions"].values()) if item["right_extensions"] else 0
        best_extend = max(left_extend, right_extend)
        support_cases = len(item["support_cases"])
        shrink_retention = support_cases / best_shrink if best_shrink else 1.0
        branch_drop = 1.0 if best_extend == 0 else 1.0 - (best_extend / support_cases)
        metrics.append(
            {
                "subpath": _canonical_subpath(subpath),
                "subpath_nodes": list(subpath),
                "support_cases": support_cases,
                "support_sessions": len(item["support_sessions"]),
                "file_span": len(item["file_refs"]),
                "file_refs": sorted(item["file_refs"]),
                "sink_signatures": sorted(item["sink_signatures"]),
                "example_flows": item["example_flows"],
                "subpath_length": len(subpath),
                "compression_gain": (len(subpath) - 1) * support_cases,
                "best_shrink_support": best_shrink,
                "best_extend_support": best_extend,
                "shrink_retention": round(shrink_retention, 6),
                "branch_drop": round(branch_drop, 6),
                "exact_variant_count": len(item["exact_variants"]),
                "path_granularity_counts": dict(sorted(item["path_granularity_counts"].items())),
                "primary_path_granularity": (
                    item["path_granularity_counts"].most_common(1)[0][0]
                    if item["path_granularity_counts"]
                    else "unknown"
                ),
                "collapsed_examples": [
                    _canonical_subpath(variant)
                    for variant in sorted(item["exact_variants"])[:3]
                ],
            }
        )
    metrics.sort(
        key=lambda item: (
            item["support_sessions"],
            item["support_cases"],
            item["subpath_length"],
            item["file_span"],
            item["subpath"],
        ),
        reverse=True,
    )
    return metrics



def build_app_overlap_graph(cases: Sequence[dict[str, Any]], *, example_limit: int = 5) -> dict[str, Any]:
    node_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "support_cases": set(),
            "support_sessions": set(),
            "file_refs": set(),
            "sink_signatures": set(),
            "example_flows": [],
            "_example_seen": set(),
            "predecessors": set(),
            "successors": set(),
            "internal_cases": set(),
            "path_granularity_counts": Counter(),
        }
    )
    edge_stats: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "support_cases": set(),
            "support_sessions": set(),
            "file_refs": set(),
            "sink_signatures": set(),
            "example_flows": [],
            "_example_seen": set(),
        }
    )

    path_records: list[dict[str, Any]] = []
    path_granularity_counts: Counter[str] = Counter()
    for case in cases:
        case_name = case["case_name"]
        session_name = case["session_name"]
        for record in case.get("path_records") or []:
            exact_nodes = _preferred_overlap_path(record)
            if not exact_nodes:
                continue
            path_granularity = (
                str(record.get("path_granularity") or "").strip()
                or ("method_call" if len(record.get("method_call_path") or []) >= 2 else "atomic_hop_fallback")
            )
            path_granularity_counts[path_granularity] += 1
            normalized_record = {
                "case_name": case_name,
                "session_name": session_name,
                "exact_path": exact_nodes,
                "path_granularity": path_granularity,
                "file_refs": list(record["file_refs"]),
                "sink_signatures": list(record["sink_signatures"]),
            }
            path_records.append(normalized_record)
            for idx, node in enumerate(exact_nodes):
                node_item = node_stats[node]
                node_item["support_cases"].add(case_name)
                node_item["support_sessions"].add(session_name)
                node_item["file_refs"].update(record["file_refs"])
                node_item["sink_signatures"].update(record["sink_signatures"])
                node_item["path_granularity_counts"][path_granularity] += 1
                _record_example(node_item, case_name=case_name, session_name=session_name, limit=example_limit)
                if idx > 0:
                    node_item["predecessors"].add(exact_nodes[idx - 1])
                if idx + 1 < len(exact_nodes):
                    node_item["successors"].add(exact_nodes[idx + 1])
                if 0 < idx < len(exact_nodes) - 1:
                    node_item["internal_cases"].add(case_name)
            for left, right in zip(exact_nodes, exact_nodes[1:]):
                edge_item = edge_stats[(left, right)]
                edge_item["support_cases"].add(case_name)
                edge_item["support_sessions"].add(session_name)
                edge_item["file_refs"].update(record["file_refs"])
                edge_item["sink_signatures"].update(record["sink_signatures"])
                _record_example(edge_item, case_name=case_name, session_name=session_name, limit=example_limit)

    family_variants = _family_variant_stats(path_records)
    exact_corridors = _finalize_corridor_metrics(
        _collect_corridors(path_records, example_limit=example_limit, view="exact")
    )
    family_corridors = _finalize_corridor_metrics(
        _collect_corridors(
            path_records,
            example_limit=example_limit,
            view="family",
            family_variants=family_variants,
        )
    )

    finalized_nodes = []
    for node, item in node_stats.items():
        finalized_nodes.append(
            {
                "node": node,
                "support_cases": len(item["support_cases"]),
                "support_sessions": len(item["support_sessions"]),
                "file_span": len(item["file_refs"]),
                "file_refs": sorted(item["file_refs"]),
                "sink_signatures": sorted(item["sink_signatures"]),
                "example_flows": item["example_flows"],
                "predecessor_count": len(item["predecessors"]),
                "successor_count": len(item["successors"]),
                "internal_support_cases": len(item["internal_cases"]),
                "path_granularity_counts": dict(sorted(item["path_granularity_counts"].items())),
                "primary_path_granularity": (
                    item["path_granularity_counts"].most_common(1)[0][0]
                    if item["path_granularity_counts"]
                    else "unknown"
                ),
            }
        )
    finalized_nodes.sort(
        key=lambda item: (
            item["support_sessions"],
            item["support_cases"],
            item["file_span"],
            item["node"],
        ),
        reverse=True,
    )

    finalized_edges = []
    for (left, right), item in edge_stats.items():
        finalized_edges.append(
            {
                "edge": f"{left} -> {right}",
                "source": left,
                "target": right,
                "support_cases": len(item["support_cases"]),
                "support_sessions": len(item["support_sessions"]),
                "file_span": len(item["file_refs"]),
                "file_refs": sorted(item["file_refs"]),
                "sink_signatures": sorted(item["sink_signatures"]),
                "example_flows": item["example_flows"],
            }
        )
    finalized_edges.sort(
        key=lambda item: (
            item["support_sessions"],
            item["support_cases"],
            item["file_span"],
            item["edge"],
        ),
        reverse=True,
    )

    return {
        "schema_version": "flowark-path-overlap-graph-v2",
        "app_name": cases[0].get("app_name") if cases else "unknown",
        "case_count": len(cases),
        "node_count": len(finalized_nodes),
        "edge_count": len(finalized_edges),
        "exact_corridor_count": len(exact_corridors),
        "family_corridor_count": len(family_corridors),
        "path_granularity": "method_call_preferred",
        "path_granularity_counts": dict(sorted(path_granularity_counts.items())),
        "nodes": finalized_nodes,
        "edges": finalized_edges,
        "corridors": {"exact": exact_corridors, "family": family_corridors},
    }



def _score_multiplier(item: dict[str, Any]) -> float:
    multiplier = 1.0
    sinks = [str(sink).strip().casefold() for sink in (item.get("sink_signatures") or []) if str(sink).strip()]
    nodes = [str(node).strip().casefold() for node in (item.get("subpath_nodes") or []) if str(node).strip()]
    granularity = str(item.get("primary_path_granularity") or "").strip().casefold()
    if granularity == "method_call":
        multiplier *= 1.15
    elif granularity == "atomic_hop_fallback":
        multiplier *= 0.45
    if item.get("file_span", 0) >= 2:
        multiplier *= 1.10
    if len(set(sinks)) >= 2:
        multiplier *= 1.10
    if any(any(sink.startswith(prefix) for prefix in CONCRETE_BOUNDARY_PREFIXES) for sink in sinks):
        multiplier *= 1.10
    if sinks and all(sink.startswith("log:") for sink in sinks):
        multiplier *= 0.75
    if nodes and any(marker in nodes[-1] for marker in ACTIVITY_GLUE_MARKERS):
        multiplier *= 0.85
    has_boundary = any(any(sink.startswith(prefix) for prefix in CONCRETE_BOUNDARY_PREFIXES) for sink in sinks)
    if nodes and any(any(marker in node for marker in CALLBACK_GLUE_MARKERS) for node in nodes) and not has_boundary:
        multiplier *= 0.85
    return multiplier



def _candidate_score(item: dict[str, Any], *, is_family: bool) -> float:
    family_bonus = 1.25 if is_family and item.get("exact_variant_count", 0) >= 2 else 1.0
    base = (
        item["compression_gain"]
        * (1 + 0.2 * (item["support_sessions"] - 1))
        * (1 + 0.1 * min(item["file_span"], 5))
        * (1 + 0.5 * item["branch_drop"])
        * item["shrink_retention"]
        * family_bonus
    )
    return base * _score_multiplier(item)



def _eligible_corridor(item: dict[str, Any], *, is_family: bool) -> bool:
    if item["support_cases"] < 2:
        return False
    if item["subpath_length"] < 2:
        return False
    if item["compression_gain"] < 2:
        return False
    if item["shrink_retention"] < 0.7:
        return False
    if item["branch_drop"] < 0.25 and item["best_extend_support"] > 0:
        return False
    if is_family and not (
        item.get("exact_variant_count", 0) >= 2 or item["support_sessions"] >= 2
    ):
        return False
    return True



def _corridor_contains(container: Sequence[str], child: Sequence[str]) -> bool:
    if len(container) < len(child):
        return False
    child_tuple = tuple(child)
    for idx in range(len(container) - len(child) + 1):
        if tuple(container[idx : idx + len(child)]) == child_tuple:
            return True
    return False



def _node_covered_by_corridor(node: str, corridors: Sequence[dict[str, Any]]) -> bool:
    return any(node in corridor["supporting_subpath"].split(" -> ") for corridor in corridors)



def build_reuse_digest(graph: dict[str, Any], *, top_k: int = 5) -> dict[str, Any]:
    digests: list[dict[str, Any]] = []
    selected_corridors: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []

    for view_name in ("family", "exact"):
        for item in graph.get("corridors", {}).get(view_name, []):
            is_family = view_name == "family"
            if not _eligible_corridor(item, is_family=is_family):
                continue
            candidate = {
                "digest_kind": f"{view_name}_corridor",
                "view": view_name,
                "summary": (
                    f"Repeated family corridor: {item['subpath']}"
                    if is_family
                    else f"Repeated corridor: {item['subpath']}"
                ),
                "support_cases": item["support_cases"],
                "support_sessions": item["support_sessions"],
                "file_span": item["file_span"],
                "supporting_subpath": item["subpath"],
                "sink_signatures": item["sink_signatures"],
                "reuse_hint": "Treat this corridor as a stable overlap anchor; inspect its adjacent hops and sinks before expanding more code.",
                "example_flows": item["example_flows"],
                "score": round(_candidate_score(item, is_family=is_family), 6),
                "compression_gain": item["compression_gain"],
                "shrink_retention": item["shrink_retention"],
                "branch_drop": item["branch_drop"],
                "exact_variant_count": item.get("exact_variant_count", 0),
                "primary_path_granularity": item.get("primary_path_granularity", "unknown"),
                "path_granularity_counts": dict(item.get("path_granularity_counts") or {}),
                "collapsed_examples": item.get("collapsed_examples", []) if is_family else [],
                "subpath_nodes": item["subpath_nodes"],
            }
            candidates.append(candidate)

    candidates.sort(
        key=lambda item: (
            item["score"],
            1 if item["digest_kind"] == "family_corridor" else 0,
            item["support_cases"],
            item["support_sessions"],
            item["file_span"],
            len(item["subpath_nodes"]),
        ),
        reverse=True,
    )

    for candidate in candidates:
        nodes = candidate["subpath_nodes"]
        redundant = False
        for selected in selected_corridors:
            selected_nodes = selected["subpath_nodes"]
            support_gap = abs(selected["support_cases"] - candidate["support_cases"]) / max(
                selected["support_cases"], candidate["support_cases"], 1
            )
            if support_gap <= 0.1 and _corridor_contains(selected_nodes, nodes):
                redundant = True
                break
        if redundant:
            continue
        digests.append({k: v for k, v in candidate.items() if k != "subpath_nodes"})
        selected_corridors.append(candidate)
        if len(digests) >= top_k:
            break

    if len(digests) < top_k:
        for node in graph.get("nodes", []):
            if node["support_cases"] < 2:
                continue
            if node["internal_support_cases"] < 2:
                continue
            if node["predecessor_count"] <= 0 or node["successor_count"] <= 0:
                continue
            if _node_covered_by_corridor(node["node"], digests):
                continue
            digests.append(
                {
                    "digest_kind": "internal_node",
                    "view": "exact",
                    "summary": f"Repeated internal node: {node['node']}",
                    "support_cases": node["support_cases"],
                    "support_sessions": node["support_sessions"],
                    "file_span": node["file_span"],
                    "supporting_subpath": node["node"],
                    "sink_signatures": node["sink_signatures"],
                    "reuse_hint": "Treat this internal node as a stable overlap anchor; inspect its adjacent hops and sinks before expanding more code.",
                    "example_flows": node["example_flows"],
                    "score": round(
                        node["support_cases"]
                        * (1 + 0.2 * (node["support_sessions"] - 1))
                        * (1 + 0.1 * min(node["file_span"], 5)),
                        6,
                    ),
                    "compression_gain": node["support_cases"],
                    "shrink_retention": 1.0,
                    "branch_drop": 0.0,
                    "exact_variant_count": 1,
                    "primary_path_granularity": node.get("primary_path_granularity", "unknown"),
                    "path_granularity_counts": dict(node.get("path_granularity_counts") or {}),
                    "collapsed_examples": [],
                }
            )
            if len(digests) >= top_k:
                break

    return {
        "schema_version": "flowark-reuse-digest-v2",
        "app_name": graph.get("app_name", "unknown"),
        "path_granularity": graph.get("path_granularity", "method_call_preferred"),
        "path_granularity_counts": dict(graph.get("path_granularity_counts") or {}),
        "digest_count": len(digests),
        "digests": digests,
    }



def render_reuse_digest_markdown(digest: dict[str, Any]) -> str:
    lines = [f"## Reuse digest for {digest.get('app_name', 'unknown')}", ""]
    for idx, item in enumerate(digest.get("digests", []), start=1):
        lines.append(f"### {idx}. {item['summary']}")
        lines.append(f"- digest_kind: {item.get('digest_kind', 'unknown')}")
        lines.append(f"- view: {item.get('view', 'exact')}")
        lines.append(f"- score: {item.get('score', 0)}")
        lines.append(f"- support_cases: {item['support_cases']}")
        lines.append(f"- support_sessions: {item['support_sessions']}")
        lines.append(f"- file_span: {item['file_span']}")
        lines.append(f"- supporting_subpath: {item['supporting_subpath']}")
        lines.append(f"- primary_path_granularity: {item.get('primary_path_granularity', 'unknown')}")
        sinks = ", ".join(item.get("sink_signatures") or []) or "(none)"
        lines.append(f"- sink_signatures: {sinks}")
        lines.append(f"- reuse_hint: {item['reuse_hint']}")
        collapsed = item.get("collapsed_examples") or []
        if collapsed:
            lines.append(f"- collapsed_examples: {'; '.join(collapsed)}")
        examples = item.get("example_flows") or []
        if examples:
            lines.append("- example_flows:")
            for example in examples:
                lines.append(f"  - {example['session_name']} / {example['case_name']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"



def render_compact_reuse_digest_block(digest: dict[str, Any], *, limit: int = LIVE_DIGEST_TOP_K) -> str:
    digests = [item for item in (digest.get("digests") or []) if isinstance(item, dict)]
    if not digests:
        return ""
    lines = ["历史路径重叠摘要（仅用于补充跨 case 复用视角；不要直接照抄成知识正文）:"]
    count = 0
    for idx, item in enumerate(digests, start=1):
        summary = str(item.get("summary") or "").strip()
        if not summary:
            continue
        lines.append(
            f"{idx}. subpath={str(item.get('supporting_subpath') or '-').strip() or '-'}; "
            f"support_cases={int(item.get('support_cases') or 0)}; "
            f"support_sessions={int(item.get('support_sessions') or 0)}"
        )
        count += 1
        if count >= max(1, int(limit or LIVE_DIGEST_TOP_K)):
            break
    if count <= 0:
        return ""
    return "\n".join(lines) + "\n\n"



def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
