"""Deterministic content controls on knowledge selected from a task-start fixture."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

import yaml

from flowark.knowledge.control_history import control_path

_NOTE_START = re.compile(r"^### (?P<kind>NOTE(?:_SUMMARY)?):[^\n]*(?:\n|$)", re.M)
_CLOSE_WRAPPER = re.compile(r"^</flowark-(?:knowledge-injection|runtime-context)>\s*$", re.M)
_MATCH_RULE_LINE = re.compile(r"^命中规则:[^\n]*(?:\n|$)", re.M)
TASK_START_CONTROL_SCHEMA = "flowark-task-start-content-preparation-v1"


@dataclass(frozen=True, slots=True)
class InjectionControlResult:
    text: str
    audit: dict[str, Any]


class InjectionControlError(ValueError):
    """Invalid frozen input or knowledge delivery."""

    def __init__(self, message: str, *, audit: dict[str, Any] | None = None):
        super().__init__(message)
        self.audit = dict(audit or {})


def _hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _instant(value: object) -> datetime:
    try:
        result = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise InjectionControlError(f"invalid control timestamp: {value!r}") from exc
    if result.tzinfo is None:
        raise InjectionControlError("control timestamps must include timezone")
    return result.astimezone(timezone.utc)


def _app_key(value: object) -> str:
    return re.sub(r"[^\w]", "", str(value or "").casefold())


def _validate_control_protocol(fixture: Mapping[str, Any]) -> None:
    if fixture.get("schema_version") != TASK_START_CONTROL_SCHEMA:
        raise InjectionControlError("unsupported knowledge control fixture schema")
    if fixture.get("execution_mode") != "task_start":
        raise InjectionControlError("task-start fixture must use task_start execution mode")
    if any(fixture.get(field) != "replace" for field in ("initial_injection", "after_tool_injection")):
        raise InjectionControlError("task-start control must replace initial and after-tool injections")
    if fixture.get("generic_knowledge_filter") is not False:
        raise InjectionControlError("task-start control must retain generic knowledge")
    if fixture.get("token_count_matching") is not False or fixture.get("length_metric") != "unicode_codepoints":
        raise InjectionControlError("task-start control must match Unicode character counts")
    if fixture.get("character_length_ratio") != [0.8, 1.25]:
        raise InjectionControlError("task-start character length ratio must be 0.8--1.25")


def load_control_fixture(path: str | Path, *, allowed_roots: Sequence[Path] | None = None) -> dict[str, Any]:
    """Read only the explicitly supplied immutable experiment fixture."""
    fixture_path = Path(path).expanduser()
    try:
        raw = fixture_path.read_text(encoding="utf-8")
        fixture = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise InjectionControlError(f"cannot load knowledge control fixture: {fixture_path}") from exc
    if not isinstance(fixture, dict):
        raise InjectionControlError("knowledge control fixture must be a JSON object")
    fixture = dict(fixture)
    _validate_control_protocol(fixture)
    fixture["_fixture_sha256"] = _hash(raw)
    fixture["_fixture_path"] = str(fixture_path.resolve())
    if fixture.get("schema_version") == TASK_START_CONTROL_SCHEMA:
        pool_value = fixture.get("donor_pool_path")
        if not isinstance(pool_value, str) or not pool_value.strip():
            raise InjectionControlError("task-start fixture requires a frozen donor_pool_path")
        try:
            pool_path = control_path(pool_value, base_dir=fixture_path.parent,
                                     allowed_roots=allowed_roots or [fixture_path.parent])
            pool_raw = pool_path.read_text(encoding="utf-8")
            pool = json.loads(pool_raw)
        except (OSError, ValueError) as exc:
            raise InjectionControlError("cannot load frozen task-start donor pool") from exc
        if not isinstance(pool, dict) or not isinstance(pool.get("donors"), list):
            raise InjectionControlError("frozen task-start donor pool must contain a donor list")
        if _instant(pool.get("cutoff_time")) != _instant(fixture.get("cutoff_time")):
            raise InjectionControlError("donor pool cutoff differs from task-start fixture")
        if pool.get("generic_knowledge_filter") is not False or pool.get("token_count_matching") is not False:
            raise InjectionControlError("donor pool does not declare the frozen character-matching protocol")
        fixture["donors"] = pool["donors"]
        fixture["_donor_pool_sha256"] = _hash(pool_raw)
    return fixture


def validate_frozen_selection(
    *,
    fixture: Mapping[str, Any],
    details: list[dict[str, Any]],
    skills_dir: str | Path | None,
    app_name: str | None = None,
) -> dict[str, Any]:
    """Verify selected snapshots against the fixture's historical inventory."""
    audit: dict[str, Any] = {"status": "verified", "selected_versions": []}
    try:
        target_app = str(fixture.get("app_name") or "")
        if not target_app or (app_name and _app_key(app_name) != _app_key(target_app)):
            raise InjectionControlError("frozen selection fixture app does not match current app")
        cutoff = _instant(fixture.get("cutoff_time"))
        frozen_path = fixture.get("skills_dir")
        if not frozen_path or skills_dir is None:
            raise InjectionControlError("frozen selection requires configured and fixture skills_dir")
        frozen = Path(str(frozen_path)).expanduser()
        if not frozen.is_absolute() and fixture.get("_fixture_path"):
            frozen = Path(str(fixture["_fixture_path"])).parent / frozen
        actual = Path(skills_dir).expanduser().resolve()
        if actual != frozen.resolve():
            raise InjectionControlError("configured skills_dir differs from frozen fixture skills_dir")
        audit["skills_dir"] = str(actual)
        inventory = fixture.get("historical_skills")
        if not isinstance(inventory, list) or not details:
            raise InjectionControlError("frozen selection requires historical inventory and original details")
        for detail in details:
            skill_id = str(detail.get("skill_id") or "")
            snapshot = detail.get("skill_file_snapshot")
            if not skill_id or not isinstance(snapshot, str) or not snapshot:
                raise InjectionControlError("selected knowledge is missing its original ID or snapshot")
            frontmatter = re.match(r"\A---[^\S\n]*\n(.*?)\n---(?:\s*\n|$)", snapshot, re.S)
            if frontmatter is None:
                raise InjectionControlError(f"selected snapshot lacks skill frontmatter: {skill_id}")
            try:
                metadata = yaml.safe_load(frontmatter.group(1))
            except yaml.YAMLError as exc:
                raise InjectionControlError(f"selected snapshot has invalid frontmatter: {skill_id}") from exc
            if not isinstance(metadata, dict) or str(metadata.get("id") or "") != skill_id:
                raise InjectionControlError(f"selected snapshot ID differs from selected skill: {skill_id}")
            if _app_key(metadata.get("app_name")) != _app_key(target_app):
                raise InjectionControlError(f"selected snapshot is not from the target app: {skill_id}")
            snapshot_hash = _hash(snapshot)
            candidates = [
                row for row in inventory if isinstance(row, dict)
                and str(row.get("id") or "") == skill_id
                and _app_key(row.get("app_name")) == _app_key(target_app)
                and row.get("source_sha256") == snapshot_hash
            ]
            if len(candidates) != 1:
                raise InjectionControlError(f"selected snapshot does not identify one frozen historical version: {skill_id}")
            selected = candidates[0]
            if not selected.get("version") or str(metadata.get("version") or "") != str(selected["version"]):
                raise InjectionControlError(f"selected snapshot version differs from frozen inventory: {skill_id}")
            if _instant(selected.get("available_at")) > cutoff:
                raise InjectionControlError(f"selected snapshot became available after historical cutoff: {skill_id}")
            audit["selected_versions"].append({
                "id": skill_id, "app_name": target_app, "version": selected["version"],
                "available_at": selected["available_at"], "source_sha256": snapshot_hash,
            })
        return audit
    except InjectionControlError as exc:
        exc.audit = {"status": "failed", "action": "failed", "error": str(exc), "frozen_selection": {**audit, "status": "failed"}}
        raise


def _spans(text: str) -> list[tuple[int, int, str]]:
    starts = list(_NOTE_START.finditer(text))
    spans: list[tuple[int, int, str]] = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        close = _CLOSE_WRAPPER.search(text, start.end(), end)
        if close:
            end = close.start()
        # Preserve separators and closing wrapper verbatim outside the block.
        while end > start.start() and text[end - 1].isspace():
            end -= 1
        spans.append((start.start(), end, start.group("kind")))
    return spans


def _audit(original: str, actual: str, *, mode: str, **extra: Any) -> dict[str, Any]:
    return {
        "mode": mode,
        "status": "applied",
        "expected_action": {"off": "identity", "matched": "identity", "irrelevant": "replace"}[mode],
        "action": {"off": "identity", "matched": "identity", "irrelevant": "replace"}[mode],
        "length_metric": "unicode_codepoints",
        "token_count_measured": False,
        "original_payload": original,
        "actual_payload": actual,
        "original_chars": len(original),
        "actual_chars": len(actual),
        "original_utf8_bytes": len(original.encode("utf-8")),
        "actual_utf8_bytes": len(actual.encode("utf-8")),
        "original_sha256": _hash(original),
        "actual_sha256": _hash(actual),
        **extra,
    }


def transform_injection_text(
    text: str,
    *,
    mode: str,
    fixture: Mapping[str, Any] | None = None,
    app_name: str | None = None,
    max_chars: int | None = None,
) -> InjectionControlResult:
    """Preserve matched blocks or replace them with historical cross-app donors.

    Donors are selected by character-length difference and then donor ID, within
    the frozen 0.8--1.25 ratio and the supplied delivery budget.
    """
    original = str(text or "")
    normalized_mode = str(mode or "off").strip().lower()
    if normalized_mode == "off":
        return InjectionControlResult(original, _audit(original, original, mode="off"))
    if normalized_mode not in {"matched", "irrelevant"}:
        raise InjectionControlError(f"unsupported knowledge control mode: {mode}")
    fixture = dict(fixture or {})
    base = {"fixture_sha256": fixture.get("_fixture_sha256"), "donor_pool_sha256": fixture.get("_donor_pool_sha256"),
            "execution_mode": fixture.get("execution_mode"),
            "donor_mappings": [], "rejected_candidates": []}
    try:
        _validate_control_protocol(fixture)
        target_app = str(fixture.get("app_name") or "").strip()
        if not target_app or (app_name and _app_key(app_name) != _app_key(target_app)):
            raise InjectionControlError("knowledge control fixture app does not match current app")
        cutoff = _instant(fixture.get("cutoff_time"))
        base.update({"app_name": target_app, "cutoff_time": fixture["cutoff_time"]})
        spans = _spans(original)
        if original.strip() and not spans:
            raise InjectionControlError("control payload contains no supported NOTE or NOTE_SUMMARY block")
        budget = len(original) if max_chars is None else max(0, int(max_chars))
        if normalized_mode == "matched":
            if len(original) > budget:
                raise InjectionControlError("matched knowledge exceeds the character budget")
            return InjectionControlResult(original, _audit(original, original, mode=normalized_mode, **base, max_chars=budget))
        forbidden = [str(a).casefold() for a in fixture.get("forbidden_anchors", []) if str(a).strip()]
        donors = fixture.get("donors")
        if not isinstance(donors, list):
            raise InjectionControlError("knowledge control fixture has no donor list")
        # Match reasons describe the real same-app selection; keep them only in
        # audit, since they are knowledge-specific rather than common guidance.
        rule_matches = [
            match for match in _MATCH_RULE_LINE.finditer(original)
            if not any(start <= match.start() < end for start, end, _ in spans)
        ]
        removed_rules = [match.group(0) for match in rule_matches]
        current_length = len(original) - sum(len(line) for line in removed_rules)
        replacements: list[tuple[int, int, str]] = [
            (match.start(), match.end(), "") for match in rule_matches
        ]
        used_ids: set[str] = set()
        for block_index, (start, end, kind) in enumerate(spans):
            source = original[start:end]
            field = "summary_text" if kind == "NOTE_SUMMARY" else "note_text"
            hash_field = "summary_sha256" if kind == "NOTE_SUMMARY" else "note_sha256"
            candidates: list[tuple[int, str, str, Mapping[str, Any]]] = []
            for donor in donors:
                donor = donor if isinstance(donor, dict) else {}
                donor_id = str(donor.get("id") or "")
                donor_text = str(donor.get(field) or "").strip()
                reasons: list[str] = []
                if not donor_id or not donor.get("version"):
                    reasons.append("missing_identity_or_historical_version")
                if not donor.get("app_name") or _app_key(donor.get("app_name")) == _app_key(target_app):
                    reasons.append("not_other_app")
                try:
                    if _instant(donor.get("available_at")) > cutoff:
                        reasons.append("available_after_cutoff")
                except InjectionControlError:
                    reasons.append("invalid_availability_timestamp")
                snapshot = donor.get("skill_file_snapshot")
                if not isinstance(snapshot, str) or not snapshot or _hash(snapshot) != donor.get("source_sha256"):
                    reasons.append("source_hash_mismatch_or_missing_snapshot")
                if not donor_text.startswith(f"### {kind}: ") or len(_spans(donor_text)) != 1:
                    reasons.append("missing_or_wrong_render_structure")
                if _hash(str(donor.get(field) or "")) != donor.get(hash_field):
                    reasons.append("render_hash_mismatch")
                anchor_corpus = donor_text.casefold() + "\n" + "\n".join(str(a).casefold() for a in donor.get("anchors", []))
                overlap = [a for a in forbidden if a in anchor_corpus]
                if overlap:
                    reasons.append("shared_target_anchors")
                ratio = len(donor_text) / max(1, len(source))
                if not 0.8 <= ratio <= 1.25:
                    reasons.append("not_similar_character_length")
                if current_length - len(source) + len(donor_text) > budget:
                    reasons.append("character_budget_exceeded")
                if donor_id in used_ids:
                    reasons.append("donor_already_used_in_payload")
                if reasons:
                    base["rejected_candidates"].append({"block_index": block_index, "donor_id": donor_id, "reasons": reasons, "shared_anchors": overlap})
                else:
                    candidates.append((abs(len(donor_text) - len(source)), donor_id, donor_text, donor))
            if not candidates:
                raise InjectionControlError(f"no eligible unrelated historical donor for block {block_index} ({kind})")
            _, donor_id, actual, selected = min(candidates, key=lambda item: (item[0], item[1]))
            used_ids.add(donor_id)
            current_length += len(actual) - len(source)
            replacements.append((start, end, actual))
            base["donor_mappings"].append(_audit(source, actual, mode=normalized_mode, block_index=block_index, block_kind=kind, donor_id=donor_id, donor_app=selected["app_name"], donor_available_at=selected["available_at"], donor_version=selected["version"], donor_source_sha256=selected["source_sha256"], character_length_ratio=len(actual) / max(1, len(source)), selection="nearest_eligible_character_length", fallback=None))
        actual = original
        for start, end, replacement in sorted(replacements, reverse=True):
            actual = actual[:start] + replacement + actual[end:]
        return InjectionControlResult(actual, _audit(original, actual, mode=normalized_mode, **base, removed_match_rule_lines=removed_rules, max_chars=budget))
    except InjectionControlError as exc:
        exc.audit = _audit(original, "", mode=normalized_mode, **base, status="failed", action="failed", error=str(exc))
        raise
