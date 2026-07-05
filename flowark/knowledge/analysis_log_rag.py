"""Analysis-log RAG baseline corpus and router."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Sequence

from flowark.knowledge.reuse_recall import ReuseEmbeddingConfig, embed_texts
from flowark.timeutil import now_tz8_iso

ANALYSIS_LOG_RAG_DIR_NAME = "analysis_log_rag"
ANALYSIS_LOG_RAG_CHUNK_SCHEMA = "flowark-analysis-log-rag-chunk-v1"
ANALYSIS_LOG_RAG_MANIFEST_SCHEMA = "flowark-analysis-log-rag-manifest-v1"
TARGET_CHUNK_CHARS = 2200
SOFT_MIN_CHUNK_CHARS = 1000
HARD_MAX_CHUNK_CHARS = 3500
CHUNK_OVERLAP_CHARS = 250
MAX_TRANSCRIPT_LINE_CHARS = 700
_QUERY_INSTRUCTION = (
    "Retrieve historical analysis-log snippets from prior same-app sessions "
    "that are semantically relevant to the current code/data-flow analysis context."
)
_FLOWARK_INJECTED_CONTEXT_BLOCK_RE = re.compile(
    r"<(?P<tag>flowark-(?:runtime-context|knowledge-injection))\b[^>]*>.*?</(?P=tag)>",
    flags=re.S | re.I,
)
_ANALYSIS_LOG_RAG_TITLE = (
    "Retrieved historical analysis snippets from prior same-app sessions"
)
_TRANSCRIPT_BOUNDARY_RE = re.compile(
    r"^(?:\[?(?:assistant|user|tool|system|runner|phase:[^\]]+)\]?[:\s]|"
    r"(?:Assistant|User|Tool|System)Message\b|"
    r"(?:ToolUse|ToolResult)Block\b)",
    flags=re.I,
)
_ALRAG_ID_RE = re.compile(r"\balrag-[0-9a-f]{8,64}\b", flags=re.I)
_FINAL_REPORT_PHASE_LINE_RE = re.compile(
    r"^(?:\[\s*phase:final_report\s*\]|phase:final_report(?:[:\s]|$))",
    flags=re.I,
)
_LINE_TRANSCRIPT_BOUNDARY_RE = re.compile(
    r"^(?:OpenCode\s+(?:assistant|user)\b|"
    r"\[tool:(?P<tool>[^\]]+)\]|"
    r"\[phase:[^\]]+\]|"
    r"\[(?:tool-input|tool-output|step-finish)\]|"
    r"(?:assistant|user|system|tool_result):\s*|"
    r"(?:Assistant|User|Tool|System)Message\b|"
    r"(?:ToolUse|ToolResult)Block\b)",
    flags=re.I,
)


@dataclass(frozen=True, slots=True)
class AnalysisLogRagChunk:
    chunk_id: str
    app_name: str
    source_id: str | None
    case_id: str | None
    run_id: str | None
    origin: str
    text: str
    artifact_path: str | None
    chunk_index: int
    created_at: str
    sink_types: tuple[str, ...] = ()
    run_order: int | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": ANALYSIS_LOG_RAG_CHUNK_SCHEMA,
            "chunk_id": self.chunk_id,
            "app_name": self.app_name,
            "source_id": self.source_id,
            "case_id": self.case_id,
            "run_id": self.run_id,
            "origin": self.origin,
            "text": self.text,
            "artifact_path": self.artifact_path,
            "chunk_index": self.chunk_index,
            "created_at": self.created_at,
            "sink_types": list(self.sink_types),
            "run_order": self.run_order,
            "chars": len(self.text),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "AnalysisLogRagChunk | None":
        if not isinstance(payload, dict):
            return None
        text = str(payload.get("text") or "").strip()
        chunk_id = str(payload.get("chunk_id") or "").strip()
        app_name = str(payload.get("app_name") or "").strip()
        origin = str(payload.get("origin") or "").strip()
        if not text or not chunk_id or not app_name or not origin:
            return None
        return cls(
            chunk_id=chunk_id,
            app_name=app_name,
            source_id=_optional_text(payload.get("source_id")),
            case_id=_optional_text(payload.get("case_id")),
            run_id=_optional_text(payload.get("run_id")),
            origin=origin,
            text=text,
            artifact_path=_optional_text(payload.get("artifact_path")),
            chunk_index=_safe_int(payload.get("chunk_index"), default=0),
            created_at=str(payload.get("created_at") or ""),
            sink_types=tuple(_normalize_sink_types(payload.get("sink_types"))),
            run_order=_optional_int(payload.get("run_order")),
        )


@dataclass(frozen=True, slots=True)
class AnalysisLogRagMatch:
    chunk_id: str
    score: float
    chunk: AnalysisLogRagChunk
    reasons: list[str]
    match_stage: str = "analysis_log_rag_embedding"


def analysis_log_rag_root_for_skills_dir(skills_dir: Path) -> Path:
    return Path(skills_dir).expanduser().resolve().parent / ANALYSIS_LOG_RAG_DIR_NAME


def build_analysis_log_rag_query_text(match_text: str) -> str:
    body = str(match_text or "").strip()
    if not body:
        return ""
    return f"Instruct: {_QUERY_INSTRUCTION}\nQuery:\n{body}"


def build_trimmed_transcript(
    raw_transcript_path: Path,
    *,
    max_tool_output_chars: int = MAX_TRANSCRIPT_LINE_CHARS,
) -> str:
    path = Path(raw_transcript_path)
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    jsonl_text = _extract_jsonl_transcript_text(text, max_non_tool_line_chars=max_tool_output_chars)
    if jsonl_text.strip():
        text = jsonl_text
    text = _strip_injected_context_blocks(text)
    text = _compact_transcript_text(text, max_non_tool_line_chars=max_tool_output_chars)
    return _strip_rag_chunk_ids(text)


def build_chunks_from_text(
    *,
    text: str,
    app_name: str,
    source_id: str | None,
    case_id: str | None,
    run_id: str | None,
    origin: str,
    artifact_path: str | None,
    created_at: str | None = None,
    sink_types: Sequence[str] | None = None,
    run_order: int | None = None,
) -> list[AnalysisLogRagChunk]:
    normalized = _normalize_body_text(text)
    if not normalized:
        return []
    parts = _chunk_text(normalized)
    timestamp = str(created_at or now_tz8_iso())
    normalized_sink_types = tuple(_normalize_sink_types(sink_types))
    chunks: list[AnalysisLogRagChunk] = []
    for index, part in enumerate(parts):
        chunk_id = _stable_chunk_id(
            app_name=app_name,
            source_id=source_id,
            case_id=case_id,
            run_id=run_id,
            origin=origin,
            chunk_index=index,
            text=part,
        )
        chunks.append(
            AnalysisLogRagChunk(
                chunk_id=chunk_id,
                app_name=app_name,
                source_id=source_id,
                case_id=case_id,
                run_id=run_id,
                origin=origin,
                text=part,
                artifact_path=artifact_path,
                chunk_index=index,
                created_at=timestamp,
                sink_types=normalized_sink_types,
                run_order=run_order,
            )
        )
    return chunks


def build_chunks_from_final_report(
    final_report_json_path: Path,
    *,
    app_name: str,
    source_id: str | None,
    case_id: str | None,
    run_id: str | None,
    created_at: str | None = None,
    sink_types: Sequence[str] | None = None,
    run_order: int | None = None,
) -> list[AnalysisLogRagChunk]:
    path = Path(final_report_json_path)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    report_text = _render_final_report_text(payload)
    return build_chunks_from_text(
        text=report_text,
        app_name=app_name,
        source_id=source_id,
        case_id=case_id,
        run_id=run_id,
        origin="final_report",
        artifact_path=str(path),
        created_at=created_at,
        sink_types=sink_types,
        run_order=run_order,
    )


def append_run_to_analysis_log_rag_corpus(
    *,
    run_dir: Path,
    skills_dir: Path,
    app_name: str,
    source_id: str | None,
    case_id: str | None,
    run_id: str | None = None,
    sink_types: Sequence[str] | None = None,
    run_order: int | None = None,
) -> dict[str, Any]:
    run_path = Path(run_dir)
    final_report_json = run_path / "final_report.json"
    raw_transcript = run_path / "raw_transcript.txt"
    if not final_report_json.exists():
        return {
            "enabled": True,
            "executed": False,
            "skipped": True,
            "reason": "missing_final_report_json",
        }
    if not raw_transcript.exists():
        return {
            "enabled": True,
            "executed": False,
            "skipped": True,
            "reason": "missing_raw_transcript",
        }
    app_text = str(app_name or "").strip()
    if not app_text:
        return {
            "enabled": True,
            "executed": False,
            "skipped": True,
            "reason": "missing_app_name",
        }

    created_at = now_tz8_iso()
    run_artifact_dir = run_path / ANALYSIS_LOG_RAG_DIR_NAME
    run_artifact_dir.mkdir(parents=True, exist_ok=True)
    trimmed_transcript = build_trimmed_transcript(raw_transcript)
    trimmed_path = run_artifact_dir / "trimmed_transcript.txt"
    trimmed_path.write_text(trimmed_transcript, encoding="utf-8")

    resolved_run_id = str(run_id or run_path.name).strip() or run_path.name
    run_chunks_path = run_artifact_dir / "chunks.jsonl"

    corpus_root = analysis_log_rag_root_for_skills_dir(skills_dir)
    corpus_root.mkdir(parents=True, exist_ok=True)
    corpus_path = corpus_root / "chunks.jsonl"
    manifest_path = corpus_root / "index_manifest.json"
    with _corpus_write_lock(corpus_root):
        existing = _load_chunks(corpus_path)
        effective_run_order = (
            int(run_order)
            if run_order is not None
            else _next_run_order(existing, run_id=resolved_run_id)
        )
        transcript_chunks = build_chunks_from_text(
            text=trimmed_transcript,
            app_name=app_text,
            source_id=source_id,
            case_id=case_id,
            run_id=resolved_run_id,
            origin="transcript",
            artifact_path=str(trimmed_path),
            created_at=created_at,
            sink_types=sink_types,
            run_order=effective_run_order,
        )
        report_chunks = build_chunks_from_final_report(
            final_report_json,
            app_name=app_text,
            source_id=source_id,
            case_id=case_id,
            run_id=resolved_run_id,
            created_at=created_at,
            sink_types=sink_types,
            run_order=effective_run_order,
        )
        chunks = transcript_chunks + report_chunks
        _write_chunks_jsonl(run_chunks_path, chunks)
        merged = _merge_chunks(existing, chunks)
        _write_chunks_jsonl(corpus_path, merged)
        manifest = _build_manifest(corpus_root=corpus_root, chunks=merged, updated_at=created_at)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "enabled": True,
        "executed": True,
        "skipped": False,
        "corpus_dir": str(corpus_root),
        "corpus_chunks_path": str(corpus_path),
        "manifest_path": str(manifest_path),
        "run_artifact_dir": str(run_artifact_dir),
        "trimmed_transcript_path": str(trimmed_path),
        "run_chunks_path": str(run_chunks_path),
        "chunk_count": len(chunks),
        "transcript_chunk_count": len(transcript_chunks),
        "final_report_chunk_count": len(report_chunks),
    }


class AnalysisLogRagRouter:
    """Embedding router over prior same-app transcript/report chunks."""

    def __init__(
        self,
        *,
        skills_dir: Path,
        embedding_config: ReuseEmbeddingConfig,
        corpus_root: Path | None = None,
    ) -> None:
        self.skills_dir = Path(skills_dir).expanduser().resolve()
        self.embedding_config = embedding_config
        self.corpus_root = (
            Path(corpus_root).expanduser().resolve()
            if corpus_root is not None
            else analysis_log_rag_root_for_skills_dir(self.skills_dir)
        )
        self.backend_fingerprint = _backend_fingerprint(self.embedding_config)
        self.cache_dir = (
            self.corpus_root
            / "embedding_cache"
            / _slug(str(self.embedding_config.model or "default"))
            / self.backend_fingerprint
        )

    def _corpus_path(self) -> Path:
        return self.corpus_root / "chunks.jsonl"

    def chunks(
        self,
        *,
        current_app_name: str | None,
        current_run_id: str | None = None,
        before_run_order: int | None = None,
    ) -> list[AnalysisLogRagChunk]:
        current_app = str(current_app_name or "").strip()
        if not current_app:
            return []
        run_id = str(current_run_id or "").strip()
        chunks = _load_chunks(self._corpus_path())
        current_order = before_run_order
        if current_order is None and run_id:
            current_order = _run_order_for_run(chunks, run_id=run_id)
        return [
            chunk
            for chunk in chunks
            if chunk.app_name.strip().casefold() == current_app.casefold()
            and str(chunk.text or "").strip()
            and (not run_id or str(chunk.run_id or "").strip() != run_id)
            and _is_prior_chunk(chunk, before_run_order=current_order)
        ]

    def recall(
        self,
        *,
        text: str,
        limit: int = 3,
        current_app_name: str | None = None,
        current_run_id: str | None = None,
        before_run_order: int | None = None,
    ) -> list[AnalysisLogRagMatch]:
        query_text = build_analysis_log_rag_query_text(text)
        if limit <= 0 or not query_text:
            return []
        chunks = self.chunks(
            current_app_name=current_app_name,
            current_run_id=current_run_id,
            before_run_order=before_run_order,
        )
        if not chunks:
            return []
        query_vectors = embed_texts([query_text], self.embedding_config)
        if len(query_vectors) != 1:
            raise RuntimeError("embedding 后端未返回 query embedding")
        query_vector = query_vectors[0]
        scored: list[tuple[float, AnalysisLogRagChunk]] = []
        for chunk, vector in self._chunk_embeddings(chunks):
            scored.append((_cosine_similarity(query_vector, vector), chunk))
        scored.sort(key=lambda item: item[0], reverse=True)
        matches: list[AnalysisLogRagMatch] = []
        for score, chunk in scored[: max(1, int(limit or 3))]:
            matches.append(
                AnalysisLogRagMatch(
                    chunk_id=chunk.chunk_id,
                    score=float(score),
                    chunk=chunk,
                    reasons=[f"embedding_similarity={score:.4f}"],
                )
            )
        return matches

    def _chunk_embeddings(
        self,
        chunks: list[AnalysisLogRagChunk],
    ) -> list[tuple[AnalysisLogRagChunk, list[float]]]:
        resolved: list[tuple[AnalysisLogRagChunk, list[float]]] = []
        missing: list[tuple[AnalysisLogRagChunk, str, str]] = []
        for chunk in chunks:
            input_text = _chunk_embedding_text(chunk)
            input_hash = hashlib.sha256(input_text.encode("utf-8")).hexdigest()
            cached = self._read_cached_embedding(chunk, input_hash=input_hash)
            if cached is not None:
                resolved.append((chunk, cached))
            else:
                missing.append((chunk, input_text, input_hash))
        if missing:
            vectors = embed_texts([input_text for _chunk, input_text, _hash in missing], self.embedding_config)
            if len(vectors) != len(missing):
                raise RuntimeError("embedding 后端返回数量与请求数量不一致")
            for (chunk, input_text, input_hash), vector in zip(missing, vectors, strict=False):
                self._write_cached_embedding(chunk, input_text=input_text, input_hash=input_hash, embedding=vector)
                resolved.append((chunk, [float(value) for value in vector]))
        return resolved

    def _cache_path(self, chunk: AnalysisLogRagChunk, *, input_hash: str) -> Path:
        key = "\0".join(
            [
                str(self.embedding_config.model or ""),
                self.backend_fingerprint,
                str(chunk.chunk_id),
                input_hash,
            ]
        )
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _read_cached_embedding(
        self,
        chunk: AnalysisLogRagChunk,
        *,
        input_hash: str,
    ) -> list[float] | None:
        path = self._cache_path(chunk, input_hash=input_hash)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if str(payload.get("model") or "") != str(self.embedding_config.model or ""):
            return None
        if str(payload.get("backend_fingerprint") or "") != self.backend_fingerprint:
            return None
        if str(payload.get("input_hash") or "") != input_hash:
            return None
        embedding = payload.get("embedding")
        if not isinstance(embedding, list):
            return None
        try:
            return [float(value) for value in embedding]
        except Exception:
            return None

    def _write_cached_embedding(
        self,
        chunk: AnalysisLogRagChunk,
        *,
        input_text: str,
        input_hash: str,
        embedding: Sequence[float],
    ) -> None:
        path = self._cache_path(chunk, input_hash=input_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "schema_version": "flowark-analysis-log-rag-embedding-cache-v1",
            "model": str(self.embedding_config.model or ""),
            "backend_fingerprint": self.backend_fingerprint,
            "base_url": str(self.embedding_config.base_url or "").strip(),
            "verify_ssl": bool(self.embedding_config.verify_ssl),
            "chunk_id": chunk.chunk_id,
            "origin": chunk.origin,
            "app_name": chunk.app_name,
            "input_hash": input_hash,
            "input_chars": len(input_text),
            "embedding": [float(value) for value in embedding],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def _normalize_sink_types(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_values: Sequence[Any] = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_values = value
    else:
        raw_values = [value]
    seen: set[str] = set()
    normalized: list[str] = []
    for item in raw_values:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


def _normalize_body_text(text: str) -> str:
    lines = [line.rstrip() for line in str(text or "").replace("\r\n", "\n").splitlines()]
    compact: list[str] = []
    previous_blank = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if not previous_blank and compact:
                compact.append("")
            previous_blank = True
            continue
        compact.append(stripped)
        previous_blank = False
    return "\n".join(compact).strip()


def _compact_transcript_text(text: str, *, max_non_tool_line_chars: int) -> str:
    normalized = str(text or "").replace("\r\n", "\n")
    raw_lines = normalized.splitlines()
    lines: list[str] = []
    previous_blank = False
    index = 0
    while index < len(raw_lines):
        raw_line = raw_lines[index]
        index += 1
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            if not previous_blank and lines:
                lines.append("")
            previous_blank = True
            continue
        previous_blank = False
        lowered = stripped.lower()
        if lowered.startswith("[step-finish]"):
            continue
        if _is_final_report_boundary_line(stripped):
            break
        if stripped.startswith("OpenCode assistant"):
            lines.append("[assistant]")
            continue
        if stripped.startswith("OpenCode user"):
            lines.append("[user]")
            continue
        if lowered.startswith("[tool-input]"):
            payload = stripped[len("[tool-input]") :].strip()
            lines.append(_summarize_tool_input(payload, max_chars=500))
            continue
        if lowered.startswith("[tool-output]"):
            payload = stripped[len("[tool-output]") :].strip()
            lines.append(_format_tool_output_line(payload))
            while index < len(raw_lines):
                next_line = raw_lines[index]
                next_stripped = next_line.strip()
                if next_stripped and _is_tool_output_end_boundary(next_stripped, raw_lines, index):
                    break
                index += 1
                if next_stripped:
                    lines.append(next_line.rstrip())
                elif lines and lines[-1] != "":
                    lines.append("")
            continue
        if _is_tool_result_line(stripped):
            lines.append(_format_tool_output_line(_tool_result_payload(stripped)))
            while index < len(raw_lines):
                next_line = raw_lines[index]
                next_stripped = next_line.strip()
                if next_stripped and _is_tool_output_end_boundary(next_stripped, raw_lines, index):
                    break
                index += 1
                if next_stripped:
                    lines.append(next_line.rstrip())
                elif lines and lines[-1] != "":
                    lines.append("")
            continue
        lines.append(_trim_leaf_text(stripped, max_chars=max_non_tool_line_chars))
    return _normalize_body_text(_strip_rag_chunk_ids("\n".join(lines)))


def _summarize_tool_input(payload: str, *, max_chars: int) -> str:
    text = str(payload or "").strip()
    if not text:
        return "[tool-input] <empty>"
    try:
        parsed = json.loads(text)
    except Exception:
        return "[tool-input] " + _trim_leaf_text(text, max_chars=max_chars)
    if isinstance(parsed, dict):
        if isinstance(parsed.get("todos"), list):
            todos = parsed.get("todos") or []
            rendered = []
            for item in todos[:6]:
                if not isinstance(item, dict):
                    continue
                content = str(item.get("content") or "").strip()
                status = str(item.get("status") or "").strip()
                priority = str(item.get("priority") or "").strip()
                rendered.append(
                    " | ".join(part for part in (status, priority, content) if part)
                )
            suffix = f"; +{len(todos) - len(rendered)} more" if len(todos) > len(rendered) else ""
            return "[tool-input] todos: " + _trim_leaf_text("; ".join(rendered) + suffix, max_chars=max_chars)
        compact_keys = [
            "filePath",
            "path",
            "pattern",
            "include",
            "offset",
            "limit",
            "query",
            "command",
        ]
        parts = [
            f"{key}={_trim_leaf_text(str(parsed.get(key) or ''), max_chars=180)}"
            for key in compact_keys
            if key in parsed and str(parsed.get(key) or "").strip()
        ]
        if parts:
            return "[tool-input] " + _trim_leaf_text("; ".join(parts), max_chars=max_chars)
    return "[tool-input] " + _trim_leaf_text(json.dumps(parsed, ensure_ascii=False), max_chars=max_chars)


def _format_tool_output_line(payload: str) -> str:
    text = str(payload or "").rstrip()
    return f"[tool-output] {text}" if text else "[tool-output]"


def _is_tool_result_line(stripped: str) -> bool:
    lowered = str(stripped or "").strip().lower()
    return (
        lowered.startswith("tool_result:")
        or lowered.startswith("tool result:")
        or lowered.startswith("toolresultblock")
    )


def _tool_result_payload(stripped: str) -> str:
    text = str(stripped or "").strip()
    if ":" not in text:
        match = re.search(r"\bcontent=(['\"])(?P<content>.*?)(?<!\\)\1", text)
        if match:
            return match.group("content").strip()
        return text if text.lower().startswith("toolresultblock") else ""
    return text.split(":", 1)[1].strip()


def _is_final_report_boundary_line(stripped: str) -> bool:
    text = str(stripped or "").strip()
    if not text:
        return False
    return bool(_FINAL_REPORT_PHASE_LINE_RE.search(text)) or _looks_like_final_report_prompt(text)


def _is_tool_output_end_boundary(stripped: str, raw_lines: Sequence[str], index: int) -> bool:
    text = str(stripped or "").strip()
    if not text:
        return False
    lower = text.lower()
    if lower.startswith("[step-finish]"):
        return True
    if lower.startswith("[tool-input]") or lower.startswith("[tool-output]"):
        return True
    if re.match(r"^\[tool:[^\]]+\]", text, flags=re.I):
        return True
    if _is_tool_result_line(text):
        return True
    if _is_final_report_boundary_line(text):
        return _looks_like_current_final_report_boundary(raw_lines, index)
    return False


def _looks_like_current_final_report_boundary(raw_lines: Sequence[str], index: int) -> bool:
    stripped = str(raw_lines[index] or "").strip()
    lowered = stripped.lower()
    if "msg_report" in lowered:
        return True
    saw_final_report_prompt = _looks_like_final_report_prompt(stripped)
    for lookahead in raw_lines[index + 1 : index + 10]:
        text = str(lookahead or "").strip()
        if not text:
            continue
        lower = text.lower()
        if _looks_like_final_report_prompt(text):
            saw_final_report_prompt = True
            continue
        if lower.startswith("opencode assistant"):
            return "msg_report" in lower
        if _LINE_TRANSCRIPT_BOUNDARY_RE.match(text):
            return saw_final_report_prompt
    return True


def _strip_injected_context_blocks(text: str) -> str:
    value = str(text or "")
    if not value.strip():
        return ""
    stripped = _FLOWARK_INJECTED_CONTEXT_BLOCK_RE.sub("\n", value)
    if _ANALYSIS_LOG_RAG_TITLE not in stripped:
        return stripped.strip()

    lines: list[str] = []
    skipping_rag_block = False
    for line in stripped.splitlines():
        if _ANALYSIS_LOG_RAG_TITLE in line:
            skipping_rag_block = True
            continue
        if skipping_rag_block and _TRANSCRIPT_BOUNDARY_RE.match(line.strip()):
            skipping_rag_block = False
        if skipping_rag_block:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _strip_rag_chunk_ids(text: str) -> str:
    return _ALRAG_ID_RE.sub("[historical_chunk_id]", str(text or ""))


def _looks_like_final_report_prompt(line: str) -> bool:
    text = str(line or "").strip()
    if not text:
        return False
    return (
        ("严格输出" in text and "只输出 JSON" in text and "不要调用工具" in text)
        or text.startswith("直接复用刚才的结论与证据")
    )


def _extract_jsonl_transcript_text(text: str, *, max_non_tool_line_chars: int) -> str:
    lines: list[str] = []
    parsed_count = 0
    for raw_line in str(text or "").splitlines():
        stripped = raw_line.strip()
        if not stripped or not stripped.startswith(("{", "[")):
            continue
        try:
            payload = json.loads(stripped)
        except Exception:
            continue
        parsed_count += 1
        if _is_json_final_report_phase_payload(payload):
            lines.append("[phase:final_report]")
            break
        extracted = _extract_json_text(payload, max_non_tool_line_chars=max_non_tool_line_chars)
        if extracted:
            lines.append(extracted)
    if parsed_count == 0:
        return ""
    return "\n\n".join(lines)


def _is_json_final_report_phase_payload(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if _is_json_explicit_tool_result_payload(value):
        return False
    for key in ("current_phase", "phase", "turn_name", "trace_channel", "transcript_prefix", "session_role"):
        text = str(value.get(key) or "").strip().lower().replace("-", "_")
        if not text:
            continue
        if (
            text.startswith("final_report")
            or text.startswith("phase:final_report")
            or text.startswith("phase_final_report")
        ):
            return True
    return False


def _extract_json_text(value: Any, *, max_non_tool_line_chars: int, path: str = "") -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _trim_leaf_text(value, max_chars=4000)
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        list_items = value if _should_preserve_full_json_list(value, path=path) else value[:50]
        parts = [
            _extract_json_text(item, max_non_tool_line_chars=max_non_tool_line_chars, path=path)
            for item in list_items
        ]
        return "\n".join(part for part in parts if part.strip())
    if isinstance(value, dict):
        if _is_json_explicit_tool_result_payload(value):
            return _render_json_tool_result_payload(value, max_non_tool_line_chars=max_non_tool_line_chars)
        preferred_keys = [
            "role",
            "type",
            "name",
            "tool",
            "tool_name",
            "message",
            "text",
            "content",
            "input",
            "parts",
            "result",
            "output",
            "stdout",
            "stderr",
            "summary",
            "error",
        ]
        parts: list[str] = []
        for key in preferred_keys:
            if key not in value:
                continue
            if key == "input":
                part = _json_tool_input_summary(value.get(key))
            elif key in {"result", "output", "stdout", "stderr"}:
                part = _format_json_tool_output(value.get(key))
            else:
                part = _extract_json_text(
                    value.get(key),
                    max_non_tool_line_chars=max_non_tool_line_chars,
                    path=f"{path}.{key}" if path else key,
                )
            if not part.strip():
                continue
            if key in {"role", "type", "name", "tool", "tool_name"}:
                parts.append(f"[{key}] {part}")
            else:
                parts.append(part)
        state = value.get("state")
        if isinstance(state, dict):
            input_part = _json_tool_input_summary(state.get("input")) if "input" in state else ""
            if input_part.strip():
                parts.append(input_part)
            if "output" in state:
                parts.append(_format_json_tool_output(state.get("output")))
        return "\n".join(parts)
    return ""


def _render_json_tool_result_payload(
    value: dict[Any, Any],
    *,
    max_non_tool_line_chars: int,
) -> str:
    parts: list[str] = []
    for key in ("role", "type", "name", "tool", "tool_name"):
        if key not in value:
            continue
        part = _extract_json_text(
            value.get(key),
            max_non_tool_line_chars=max_non_tool_line_chars,
            path=key,
        )
        if part.strip():
            parts.append(f"[{key}] {part}")
    if "input" in value:
        input_part = _json_tool_input_summary(value.get("input"))
        if input_part.strip():
            parts.append(input_part)
    output_parts: list[str] = []
    for key in ("content", "result", "output", "stdout", "stderr", "value"):
        if key in value:
            rendered = _render_json_tool_output_value(value.get(key))
            if rendered.strip():
                output_parts.append(rendered)
    state = value.get("state")
    if isinstance(state, dict):
        if "input" in state:
            input_part = _json_tool_input_summary(state.get("input"))
            if input_part.strip():
                parts.append(input_part)
        if "output" in state:
            rendered = _render_json_tool_output_value(state.get("output"))
            if rendered.strip():
                output_parts.append(rendered)
    if output_parts:
        parts.append(_format_json_tool_output("\n".join(output_parts)))
    return "\n".join(parts)


def _should_preserve_full_json_list(value: list[Any], *, path: str) -> bool:
    key = path.rsplit(".", 1)[-1].lower()
    if key == "parts":
        return True
    for item in value:
        if not isinstance(item, dict):
            continue
        if _is_json_explicit_tool_result_payload(item):
            return True
        state = item.get("state")
        if isinstance(state, dict) and "output" in state:
            return True
    return False


def _format_json_tool_output(value: Any) -> str:
    text = _render_json_tool_output_value(value).strip()
    if not text:
        return "[tool-output]"
    if "\n" in text:
        return "[tool-output]\n" + text
    return f"[tool-output] {text}"


def _render_json_tool_output_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        return str(value).strip()


def _is_json_explicit_tool_result_payload(value: dict[Any, Any]) -> bool:
    type_text = str(value.get("type") or "").strip().lower()
    role_text = str(value.get("role") or "").strip().lower()
    if role_text in {"tool", "tool_result", "tool-result", "tool_output", "tool-output"}:
        return True
    if type_text in {"tool_result", "tool-result", "tool_output", "tool-output"}:
        return True
    if type_text == "tool" and "content" in value and "state" not in value and "input" not in value:
        return True
    return False


def _json_tool_input_summary(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        payload = value
    else:
        payload = json.dumps(value, ensure_ascii=False)
    return _summarize_tool_input(payload, max_chars=500)


def _trim_leaf_text(value: str, *, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    head = max(1, int(max_chars * 0.65))
    tail = max(1, max_chars - head - 80)
    return (
        text[:head].rstrip()
        + f"\n...[trimmed {len(text) - head - tail} chars of verbose output]...\n"
        + text[-tail:].lstrip()
    )


def _chunk_text(text: str) -> list[str]:
    paragraphs = re.split(r"\n{2,}", str(text or "").strip())
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        part = paragraph.strip()
        if not part:
            continue
        if len(part) > HARD_MAX_CHUNK_CHARS:
            if current.strip():
                chunks.append(current.strip())
                current = ""
            chunks.extend(_split_long_text(part))
            continue
        candidate = f"{current}\n\n{part}".strip() if current else part
        if len(candidate) <= TARGET_CHUNK_CHARS or len(current) < SOFT_MIN_CHUNK_CHARS:
            current = candidate
            continue
        chunks.append(current.strip())
        current = _with_overlap(current, part)
    if current.strip():
        chunks.append(current.strip())
    normalized: list[str] = []
    for chunk in chunks:
        if len(chunk) <= HARD_MAX_CHUNK_CHARS:
            normalized.append(chunk)
        else:
            normalized.extend(_split_long_text(chunk))
    return [chunk for chunk in normalized if chunk.strip()]


def _split_long_text(text: str) -> list[str]:
    body = str(text or "").strip()
    chunks: list[str] = []
    start = 0
    step = max(1, HARD_MAX_CHUNK_CHARS - CHUNK_OVERLAP_CHARS)
    while start < len(body):
        end = min(len(body), start + HARD_MAX_CHUNK_CHARS)
        chunks.append(body[start:end].strip())
        if end >= len(body):
            break
        start += step
    return chunks


def _with_overlap(previous: str, next_part: str) -> str:
    overlap = str(previous or "")[-CHUNK_OVERLAP_CHARS:].strip()
    if overlap:
        return f"{overlap}\n\n{next_part}".strip()
    return str(next_part or "").strip()


def _render_final_report_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return _normalize_body_text(str(payload or ""))
    lines: list[str] = ["# Final Report"]
    query = str(payload.get("query") or "").strip()
    if query:
        lines.append(f"Query: {query}")
    source = payload.get("source")
    if isinstance(source, dict):
        source_parts = [
            str(source.get("description") or "").strip(),
            str(source.get("method") or "").strip(),
            str(source.get("location") or "").strip(),
        ]
        source_text = " | ".join(part for part in source_parts if part)
        if source_text:
            lines.append(f"Source: {source_text}")
    dataflows = payload.get("dataflows")
    if isinstance(dataflows, list):
        for idx, item in enumerate(dataflows, start=1):
            if not isinstance(item, dict):
                continue
            lines.append(f"\n## Finding {idx}")
            explain = str(item.get("explain") or "").strip()
            if explain:
                lines.append(f"Explain: {explain}")
            confidence = str(item.get("confidence") or "").strip()
            if confidence:
                lines.append(f"Confidence: {confidence}")
            sink = item.get("sink")
            if isinstance(sink, dict):
                sink_parts = [
                    str(sink.get("sink_type") or "").strip(),
                    str(sink.get("method") or "").strip(),
                    str(sink.get("statement") or "").strip(),
                    str(sink.get("location") or "").strip(),
                ]
                sink_text = " | ".join(part for part in sink_parts if part)
                if sink_text:
                    lines.append(f"Sink: {sink_text}")
            chain = item.get("method_call_chain")
            if isinstance(chain, list) and chain:
                lines.append("Method call chain: " + " -> ".join(str(v).strip() for v in chain if str(v).strip()))
            path = item.get("path")
            if isinstance(path, list) and path:
                rendered_path: list[str] = []
                for node in path[:20]:
                    if isinstance(node, dict):
                        rendered_path.append(
                            " | ".join(
                                str(node.get(key) or "").strip()
                                for key in ("description", "from", "to", "method", "statement", "location")
                                if str(node.get(key) or "").strip()
                            )
                        )
                    else:
                        rendered_path.append(str(node or "").strip())
                path_text = " -> ".join(part for part in rendered_path if part)
                if path_text:
                    lines.append(f"Path: {path_text}")
    for key, title in (
        ("uncertainties", "Uncertainties"),
        ("skipped_branches", "Skipped branches"),
    ):
        values = payload.get(key)
        if isinstance(values, list) and values:
            lines.append(f"\n## {title}")
            lines.extend(f"- {str(item).strip()}" for item in values if str(item).strip())
    return _normalize_body_text("\n".join(lines))


def _stable_chunk_id(
    *,
    app_name: str,
    source_id: str | None,
    case_id: str | None,
    run_id: str | None,
    origin: str,
    chunk_index: int,
    text: str,
) -> str:
    payload = "\0".join(
        [
            str(app_name or ""),
            str(source_id or ""),
            str(case_id or ""),
            str(run_id or ""),
            str(origin or ""),
            str(chunk_index),
            hashlib.sha256(str(text or "").encode("utf-8")).hexdigest(),
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"alrag-{digest}"


def _write_chunks_jsonl(path: Path, chunks: list[AnalysisLogRagChunk]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        for chunk in chunks:
            fp.write(json.dumps(chunk.to_payload(), ensure_ascii=False) + "\n")


def _load_chunks(path: Path) -> list[AnalysisLogRagChunk]:
    if not Path(path).exists():
        return []
    chunks: list[AnalysisLogRagChunk] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        chunk = AnalysisLogRagChunk.from_payload(payload)
        if chunk is not None:
            chunks.append(chunk)
    return chunks


def _merge_chunks(
    existing: list[AnalysisLogRagChunk],
    new_chunks: list[AnalysisLogRagChunk],
) -> list[AnalysisLogRagChunk]:
    by_id: dict[str, AnalysisLogRagChunk] = {}
    for chunk in existing + new_chunks:
        by_id[chunk.chunk_id] = chunk
    return list(by_id.values())


def _next_run_order(existing: list[AnalysisLogRagChunk], *, run_id: str) -> int:
    current_run = str(run_id or "").strip()
    existing_orders = [
        int(chunk.run_order)
        for chunk in existing
        if chunk.run_order is not None
    ]
    if current_run:
        for chunk in existing:
            if str(chunk.run_id or "").strip() == current_run and chunk.run_order is not None:
                return int(chunk.run_order)
    if existing_orders:
        return max(existing_orders) + 1
    seen_runs: set[str] = {
        str(chunk.run_id or "").strip()
        for chunk in existing
        if str(chunk.run_id or "").strip()
    }
    return len(seen_runs) + 1


def _run_order_for_run(chunks: list[AnalysisLogRagChunk], *, run_id: str) -> int | None:
    current_run = str(run_id or "").strip()
    if not current_run:
        return None
    orders = [
        int(chunk.run_order)
        for chunk in chunks
        if str(chunk.run_id or "").strip() == current_run and chunk.run_order is not None
    ]
    return min(orders) if orders else None


def _is_prior_chunk(chunk: AnalysisLogRagChunk, *, before_run_order: int | None) -> bool:
    if before_run_order is None or chunk.run_order is None:
        return True
    return int(chunk.run_order) < int(before_run_order)


@contextmanager
def _corpus_write_lock(corpus_root: Path):
    lock_path = Path(corpus_root) / ".chunks.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as fp:
        try:
            import fcntl

            fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        except Exception:
            fcntl = None  # type: ignore[assignment]
        try:
            yield
        finally:
            try:
                if fcntl is not None:  # type: ignore[name-defined]
                    fcntl.flock(fp.fileno(), fcntl.LOCK_UN)  # type: ignore[union-attr]
            except Exception:
                pass


def _build_manifest(
    *,
    corpus_root: Path,
    chunks: list[AnalysisLogRagChunk],
    updated_at: str,
) -> dict[str, Any]:
    by_app: dict[str, int] = {}
    by_origin: dict[str, int] = {}
    for chunk in chunks:
        by_app[chunk.app_name] = int(by_app.get(chunk.app_name, 0) or 0) + 1
        by_origin[chunk.origin] = int(by_origin.get(chunk.origin, 0) or 0) + 1
    return {
        "schema_version": ANALYSIS_LOG_RAG_MANIFEST_SCHEMA,
        "corpus_dir": str(corpus_root),
        "chunks_path": str(corpus_root / "chunks.jsonl"),
        "updated_at": updated_at,
        "chunk_count": len(chunks),
        "by_app": by_app,
        "by_origin": by_origin,
        "chunking": {
            "target_chars": TARGET_CHUNK_CHARS,
            "soft_min_chars": SOFT_MIN_CHUNK_CHARS,
            "hard_max_chars": HARD_MAX_CHUNK_CHARS,
            "overlap_chars": CHUNK_OVERLAP_CHARS,
            "max_tool_output_chars": None,
            "max_non_tool_line_chars": MAX_TRANSCRIPT_LINE_CHARS,
            "tool_output_policy": "preserve_full",
        },
    }


def _chunk_embedding_text(chunk: AnalysisLogRagChunk) -> str:
    source = chunk.source_id or chunk.case_id or ""
    parts = [
        f"app: {chunk.app_name}",
        f"source: {source}" if source else "",
        f"origin: {chunk.origin}",
        chunk.text,
    ]
    return "\n".join(part for part in parts if str(part or "").strip()).strip()


def _slug(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-z._-]+", "_", str(value or "").strip()).strip("._-")
    return text or "default"


def _backend_fingerprint(config: ReuseEmbeddingConfig) -> str:
    payload = {
        "base_url": str(config.base_url or "").strip().rstrip("/"),
        "model": str(config.model or "").strip(),
        "verify_ssl": bool(config.verify_ssl),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:16]


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(float(a) * float(b) for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(float(value) * float(value) for value in left))
    right_norm = math.sqrt(sum(float(value) * float(value) for value in right))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return dot / (left_norm * right_norm)
