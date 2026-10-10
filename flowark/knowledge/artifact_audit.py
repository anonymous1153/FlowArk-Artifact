"""Artifact-level checks for the note-only knowledge runtime."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from flowark.knowledge.manager import SKILL_SCHEMA_V4, SKILL_SCHEMA_V5
from flowark.knowledge.pipeline import lint_skills_dir


_FORBIDDEN_ARTIFACT_TERMS = (
    "flow" + "-fact",
    "Flow" + "Fact",
    "used" + "_flow" + "_fact",
    "flow" + "_fact_match",
    "session" + "_bridge",
    "knowledge" + "-session",
    "knowledge" + "_router",
)
_SKILL_LEGACY_KEYS = ("node_type", "related" + "_note_ids", "related" + "_note_hints")
_IGNORED_DIR_NAMES = {"archive", "archived", "skills_archived", "0A-archived", ".rerun_archive"}
NOTE_ONLY_ARTIFACT_AUDIT_FILENAME = "note_only_artifact_audit.json"


@dataclass(slots=True)
class _Issue:
    severity: str
    kind: str
    file: Path
    message: str
    details: dict[str, Any] | None = None

    def to_dict(self, root: Path) -> dict[str, Any]:
        try:
            rel = self.file.resolve().relative_to(root)
            file_value = str(rel)
        except Exception:
            file_value = str(self.file)
        payload: dict[str, Any] = {
            "severity": self.severity,
            "kind": self.kind,
            "file": file_value,
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        return payload


def audit_note_only_artifacts(root: Path | str, *, include_skill_lint: bool = True) -> dict[str, Any]:
    """Audit run/eval artifacts for the current note-only knowledge contract."""
    root_path = Path(root).expanduser().resolve()
    issues: list[_Issue] = []
    counts: Counter[str] = Counter()

    if not root_path.exists():
        issues.append(_Issue("error", "missing_root", root_path, "artifact root does not exist"))
        return _build_result(root_path, counts, issues)

    for path in _iter_relevant_text_files(root_path):
        _scan_forbidden_terms(path, issues)

    for path in _iter_named_files(root_path, "final_report.json"):
        counts["final_report_json"] += 1
        _audit_final_report(path, issues)

    for path in _iter_named_files(root_path, "knowledge_injection.jsonl"):
        counts["knowledge_injection_jsonl"] += 1
        _audit_injection_log(path, counts, issues)

    skills_dirs = _find_skills_dirs(root_path)
    counts["skills_dir"] = len(skills_dirs)
    for skills_dir in skills_dirs:
        _audit_skills_dir(skills_dir, counts, issues)
        if include_skill_lint:
            _collect_lint_warnings(skills_dir, issues)

    return _build_result(root_path, counts, issues)


def write_note_only_artifact_audit(
    root: Path | str,
    *,
    include_skill_lint: bool = True,
    output_path: Path | str | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Run the note-only audit and write the full JSON artifact."""
    root_path = Path(root).expanduser().resolve()
    result = audit_note_only_artifacts(root_path, include_skill_lint=include_skill_lint)
    out_path = (
        Path(output_path).expanduser().resolve()
        if output_path is not None
        else root_path / NOTE_ONLY_ARTIFACT_AUDIT_FILENAME
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path, result


def compact_artifact_audit_result(result: dict[str, Any], *, path: Path | str | None = None) -> dict[str, Any]:
    """Build a small summary suitable for run/eval metadata."""
    issues = result.get("issues") if isinstance(result.get("issues"), list) else []
    top_issues: list[dict[str, Any]] = []
    for item in issues[:10]:
        if not isinstance(item, dict):
            continue
        top_issues.append(
            {
                "severity": item.get("severity"),
                "kind": item.get("kind"),
                "file": item.get("file"),
                "message": item.get("message"),
            }
        )
    payload: dict[str, Any] = {
        "schema_version": str(result.get("schema_version") or "flowark-artifact-audit-v1"),
        "ok": bool(result.get("ok")),
        "error_count": int(result.get("error_count") or 0),
        "warning_count": int(result.get("warning_count") or 0),
        "counts": dict(result.get("counts") or {}),
    }
    if path is not None:
        payload["path"] = str(Path(path).expanduser().resolve())
    if top_issues:
        payload["top_issues"] = top_issues
    return payload


def _iter_relevant_text_files(root: Path) -> Iterable[Path]:
    names = {
        "final_report.json",
        "knowledge_injection.jsonl",
        "auto_knowledge_cycle.json",
        "knowledge_apply.json",
        "knowledge_candidates.json",
        "knowledge_catalog_filter.json",
        "knowledge_synth_response.json",
    }
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if _is_ignored_artifact_path(path, root):
            continue
        if path.name in names:
            yield path
            continue
        if _is_under_skills_dir(path) and path.suffix == ".md":
            yield path


def _iter_named_files(root: Path, name: str) -> Iterable[Path]:
    for path in sorted(root.rglob(name)):
        if not path.is_file():
            continue
        if _is_ignored_artifact_path(path, root):
            continue
        yield path


def _scan_forbidden_terms(path: Path, issues: list[_Issue]) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        issues.append(_Issue("warning", "read_error", path, f"failed to read artifact: {exc}"))
        return
    found = sorted({term for term in _FORBIDDEN_ARTIFACT_TERMS if term in text})
    if found:
        issues.append(
            _Issue(
                "error",
                "legacy_term",
                path,
                "artifact contains removed legacy terminology",
                {"terms": found},
            )
        )


def _audit_final_report(path: Path, issues: list[_Issue]) -> None:
    payload = _read_json(path, issues)
    if not isinstance(payload, dict):
        return
    knowledge_used = payload.get("knowledge_used")
    if knowledge_used is None:
        return
    if not isinstance(knowledge_used, dict):
        issues.append(_Issue("error", "invalid_knowledge_used", path, "knowledge_used must be an object"))
        return
    keys = set(str(key) for key in knowledge_used.keys())
    extra = sorted(keys - {"notes", "flow_facts"})
    if extra:
        issues.append(
            _Issue(
                "error",
                "legacy_knowledge_used_keys",
                path,
                "knowledge_used contains fields other than notes / flow_facts",
                {"keys": extra},
            )
        )
    notes = knowledge_used.get("notes", [])
    if notes is not None and not isinstance(notes, list):
        issues.append(_Issue("error", "invalid_knowledge_used_notes", path, "knowledge_used.notes must be a list"))
    flow_facts = knowledge_used.get("flow_facts", [])
    if not isinstance(flow_facts, list):
        issues.append(_Issue("error", "invalid_knowledge_used_flow_facts", path, "knowledge_used.flow_facts must be a list"))
    elif flow_facts:
        issues.append(
            _Issue(
                "error",
                "legacy_knowledge_used_flow_facts",
                path,
                "knowledge_used.flow_facts must stay empty in note-only artifacts",
            )
        )


def _legacy_injection_keys(row: dict[str, Any]) -> list[str]:
    legacy_prefix = "session" + "_bridge"
    return sorted(
        str(key)
        for key in row.keys()
        if str(key).startswith(legacy_prefix) or str(key) in {"flow_facts", "flow_fact_ids"}
    )


def _audit_injection_log(path: Path, counts: Counter[str], issues: list[_Issue]) -> None:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception as exc:
        issues.append(_Issue("warning", "read_error", path, f"failed to read injection log: {exc}"))
        return
    for lineno, line in enumerate(lines, start=1):
        text = line.strip()
        if not text:
            continue
        counts["knowledge_injection_log_row"] += 1
        try:
            row = json.loads(text)
        except Exception as exc:
            issues.append(
                _Issue(
                    "error",
                    "invalid_knowledge_injection_json",
                    path,
                    f"invalid JSONL row at line {lineno}: {exc}",
                )
            )
            continue
        if not isinstance(row, dict):
            issues.append(_Issue("error", "invalid_knowledge_injection_row", path, f"row {lineno} must be an object"))
            continue
        delivery_status = str(row.get("delivery_status") or "").strip().lower()
        if delivery_status == "skipped":
            counts["knowledge_injection_skipped_event"] += 1
        elif delivery_status == "failed":
            counts["knowledge_injection_failed_event"] += 1
        else:
            counts["knowledge_injection_event"] += 1
            counts["knowledge_injection_delivered_event"] += 1
        legacy_keys = _legacy_injection_keys(row)
        if legacy_keys:
            issues.append(
                _Issue(
                    "error",
                    "legacy_injection_keys",
                    path,
                    f"row {lineno} contains removed injection fields",
                    {"keys": legacy_keys, "line": lineno},
                )
            )
        for detail in row.get("details") or []:
            if not isinstance(detail, dict):
                continue
            legacy_detail_keys = _legacy_injection_keys(detail)
            if legacy_detail_keys:
                issues.append(
                    _Issue(
                        "error",
                        "legacy_injection_detail_keys",
                        path,
                        f"row {lineno} contains removed injection detail fields",
                        {"keys": legacy_detail_keys, "line": lineno},
                    )
                )
            dtype = str(detail.get("type") or detail.get("kind") or "").strip()
            if dtype in {"flow" + "_fact", "flow" + "-fact", "session" + "_bridge_fact"}:
                issues.append(
                    _Issue(
                        "error",
                        "legacy_injection_detail",
                        path,
                        f"row {lineno} contains removed injection detail type",
                        {"type": dtype, "line": lineno},
                    )
                )


def _find_skills_dirs(root: Path) -> list[Path]:
    dirs: set[Path] = set()
    if root.name == "skills" and root.is_dir() and not _is_ignored_artifact_path(root, root.parent):
        dirs.add(root)
    for path in root.rglob("skills"):
        if path.is_dir():
            if _is_ignored_artifact_path(path, root):
                continue
            dirs.add(path)
    return sorted(dirs)


def _audit_skills_dir(skills_dir: Path, counts: Counter[str], issues: list[_Issue]) -> None:
    for path in sorted(skills_dir.rglob("*.md")):
        if _is_ignored_artifact_path(path, skills_dir):
            continue
        counts["skill_markdown"] += 1
        parsed = _split_frontmatter(path)
        if parsed is None:
            issues.append(_Issue("error", "invalid_skill_frontmatter", path, "skill markdown must have YAML frontmatter"))
            continue
        metadata, _frontmatter, _body = parsed
        schema_version = str(metadata.get("schema_version") or "").strip()
        legacy_keys = sorted(key for key in _SKILL_LEGACY_KEYS if key in metadata)
        if schema_version == SKILL_SCHEMA_V5:
            if legacy_keys:
                issues.append(
                    _Issue(
                        "error",
                        "legacy_skill_frontmatter_keys",
                        path,
                        "active skill contains removed frontmatter keys",
                        {"keys": legacy_keys},
                    )
                )
            continue

        if schema_version == SKILL_SCHEMA_V4:
            node_type = str(metadata.get("node_type") or "note").strip().lower() or "note"
            if node_type == "note":
                issues.append(
                    _Issue(
                        "warning",
                        "legacy_v4_note_skill",
                        path,
                        "legacy v4 note is readable but should be migrated to flowark-skill-v5",
                    )
                )
                continue
            issues.append(
                _Issue(
                    "error",
                    "invalid_skill_schema",
                    path,
                    "removed legacy skill type cannot remain active",
                    {"schema_version": schema_version, "node_type": node_type},
                )
            )
            issues.append(
                _Issue(
                    "error",
                    "legacy_skill_frontmatter_keys",
                    path,
                    "active skill contains removed frontmatter keys",
                    {"keys": legacy_keys},
                )
            )
            continue

        issues.append(
            _Issue(
                "error",
                "invalid_skill_schema",
                path,
                "active skill must use flowark-skill-v5 or readable legacy v4 note",
                {"schema_version": schema_version or None},
            )
        )


def _collect_lint_warnings(skills_dir: Path, issues: list[_Issue]) -> None:
    try:
        lint = lint_skills_dir(skills_dir)
    except Exception as exc:
        issues.append(_Issue("warning", "skill_lint_error", skills_dir, f"skill lint failed: {exc}"))
        return
    for item in lint.get("issues") or []:
        if "mixed_note_jump_guidance" not in set(item.get("issue_types") or []):
            continue
        file_path = Path(str(item.get("file") or skills_dir))
        issues.append(
            _Issue(
                "warning",
                "mixed_note_jump_guidance",
                file_path,
                "skill may still contain jump-style guidance inside a note",
                {"skill_id": item.get("skill_id")},
            )
        )


def _split_frontmatter(path: Path) -> tuple[dict[str, Any], str, str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    _, frontmatter_raw, body = parts
    try:
        metadata = yaml.safe_load(frontmatter_raw) or {}
    except Exception:
        return None
    if not isinstance(metadata, dict):
        return None
    return metadata, frontmatter_raw, body


def _read_json(path: Path, issues: list[_Issue]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        issues.append(_Issue("error", "invalid_json", path, f"invalid JSON: {exc}"))
        return None


def _is_under_skills_dir(path: Path) -> bool:
    return "skills" in path.parts


def _is_ignored_artifact_path(path: Path, root: Path) -> bool:
    try:
        relative_parts = path.resolve().relative_to(root.resolve()).parts
    except Exception:
        relative_parts = path.parts
    return any(part in _IGNORED_DIR_NAMES for part in relative_parts)


def _build_result(root: Path, counts: Counter[str], issues: list[_Issue]) -> dict[str, Any]:
    issue_payload = [issue.to_dict(root) for issue in issues]
    severity_counts = Counter(str(issue.severity) for issue in issues)
    return {
        "schema_version": "flowark-artifact-audit-v1",
        "root": str(root),
        "ok": severity_counts.get("error", 0) == 0,
        "counts": dict(sorted(counts.items())),
        "error_count": int(severity_counts.get("error", 0)),
        "warning_count": int(severity_counts.get("warning", 0)),
        "issues": issue_payload,
    }
