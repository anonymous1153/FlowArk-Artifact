"""Schemas and config models for benchmark_builder v2."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha1
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

BENCHMARK_SCHEMA_V1 = "flowark-benchmark-v1"
DEFAULT_SINK_CATEGORIES = ["log", "network", "icc", "file", "database", "storage"]
DEFAULT_BENCHMARK_SINK_CATEGORIES = [*DEFAULT_SINK_CATEGORIES, "others"]
DEFAULT_SORT_FIELDS = ["source_subtype", "classname", "method", "file_path", "line_number", "occurrence_id"]


class SourceKind(str, Enum):
    PLATFORM_API = "platform_api"
    UI_INPUT = "ui_input"
    ICC_PAYLOAD = "icc_payload"
    REMOTE_PAYLOAD = "remote_payload"
    PERSISTENT_STORAGE = "persistent_storage"

    def __str__(self) -> str:
        return self.value


SOURCE_KIND_ALIASES: dict[str, SourceKind] = {
    "platform_api": SourceKind.PLATFORM_API,
    "system_context_input": SourceKind.PLATFORM_API,
    "clipboard": SourceKind.PLATFORM_API,
    "location": SourceKind.PLATFORM_API,
    "sensor": SourceKind.PLATFORM_API,
    "contacts": SourceKind.PLATFORM_API,
    "telephony": SourceKind.PLATFORM_API,
    "account_settings": SourceKind.PLATFORM_API,
    "device_state": SourceKind.PLATFORM_API,
    "ui_input": SourceKind.UI_INPUT,
    "icc_payload": SourceKind.ICC_PAYLOAD,
    "app_entry_input": SourceKind.ICC_PAYLOAD,
    "intent_extra": SourceKind.ICC_PAYLOAD,
    "deeplink": SourceKind.ICC_PAYLOAD,
    "route_args": SourceKind.ICC_PAYLOAD,
    "notification_payload": SourceKind.ICC_PAYLOAD,
    "ipc_input": SourceKind.ICC_PAYLOAD,
    "remote_payload": SourceKind.REMOTE_PAYLOAD,
    "remote_input": SourceKind.REMOTE_PAYLOAD,
    "network_response": SourceKind.REMOTE_PAYLOAD,
    "push_payload": SourceKind.REMOTE_PAYLOAD,
    "remote_config": SourceKind.REMOTE_PAYLOAD,
    "persistent_storage": SourceKind.PERSISTENT_STORAGE,
    "local_io_input": SourceKind.PERSISTENT_STORAGE,
    "database_read": SourceKind.PERSISTENT_STORAGE,
    "preferences_read": SourceKind.PERSISTENT_STORAGE,
    "file_read": SourceKind.PERSISTENT_STORAGE,
    "cache_read": SourceKind.PERSISTENT_STORAGE,
    "content_provider_read": SourceKind.PERSISTENT_STORAGE,
}


def normalize_source_kind(value: str | SourceKind) -> SourceKind:
    if isinstance(value, SourceKind):
        return value
    text = str(value or "").strip().lower()
    if not text:
        raise ValueError("source_kind 不能为空")
    try:
        return SOURCE_KIND_ALIASES[text]
    except KeyError as exc:
        allowed = ", ".join(kind.value for kind in SourceKind)
        raise ValueError(f"不支持的 source_kind: {text}；允许值: {allowed}") from exc


def normalize_statement(value: str) -> str:
    return " ".join(str(value or "").strip().split())


def build_occurrence_id(
    *,
    app_name: str,
    source_kind: str | SourceKind,
    source_subtype: str,
    rule_id: str,
    file_path: str,
    line_number: int,
    statement: str,
    classname: str | None = None,
    method: str | None = None,
) -> str:
    kind = normalize_source_kind(source_kind).value
    seed = "||".join(
        [
            str(app_name).strip(),
            kind,
            str(rule_id).strip(),
            str(file_path).strip(),
            str(line_number),
            str(classname or "").strip(),
            str(method or "").strip(),
            normalize_statement(statement),
        ]
    )
    digest = sha1(seed.encode("utf-8")).hexdigest()[:8]
    human = (str(source_subtype or "").strip() or "occurrence").replace(" ", "_")
    owner = (str(classname or method or "item").strip() or "item").replace(" ", "_")
    return f"{kind}.{human}.{owner}.{digest}"


def _normalize_str_list(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        out.append(text)
    return out


class InventoryOccurrence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_name: str
    apk_name: str
    dataset: str
    source_dir: str
    source_kind: SourceKind
    source_subtype: str
    rule_id: str
    boundary_type: str
    alignment_tier: str
    literature_basis: list[str] = Field(default_factory=list)
    occurrence_id: str
    file_path: str
    line_number: int
    classname: str | None = None
    method: str | None = None
    statement: str
    description: str | None = None
    review_state: Literal["auto", "reviewed", "rejected"] = "auto"

    @field_validator(
        "app_name",
        "apk_name",
        "dataset",
        "source_dir",
        "source_subtype",
        "rule_id",
        "boundary_type",
        "alignment_tier",
        "occurrence_id",
        "file_path",
        "statement",
    )
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("字段不能为空")
        return text

    @field_validator("classname", "method", "description")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("source_kind", mode="before")
    @classmethod
    def _normalize_source_kind(cls, value: str | SourceKind) -> SourceKind:
        return normalize_source_kind(value)

    @field_validator("literature_basis")
    @classmethod
    def _normalize_literature_basis(cls, value: list[str]) -> list[str]:
        return _normalize_str_list(value)

    @field_validator("line_number")
    @classmethod
    def _validate_line_number(cls, value: int) -> int:
        number = int(value)
        if number <= 0:
            raise ValueError("line_number 必须为正整数")
        return number


class BenchmarkSource(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    file_path: str = Field(validation_alias=AliasChoices("file_path", "filePath", "file"))
    line_number: int | None = Field(default=None, validation_alias=AliasChoices("line_number", "lineNumber", "line", "line_start"))
    classname: str | None = Field(default=None, validation_alias=AliasChoices("classname", "className", "class_name"))
    method: str | None = Field(default=None, validation_alias=AliasChoices("method", "methodName", "method_name", "function", "function_name"))
    statement: str = Field(validation_alias=AliasChoices("statement", "statementgeneric", "statementfull", "targetName", "code"))
    description: str | None = Field(default=None, validation_alias=AliasChoices("description", "usage"))

    @field_validator("file_path", "statement")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("字段不能为空")
        return text

    @field_validator("classname", "method", "description")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("line_number")
    @classmethod
    def _validate_line_number(cls, value: int | None) -> int | None:
        if value is None:
            return None
        number = int(value)
        if number <= 0:
            raise ValueError("line_number 必须为正整数")
        return number


class BenchmarkDraftCase(BaseModel):
    model_config = ConfigDict(extra="ignore")

    case_id: str
    source_id: str | None = None
    source_kind: SourceKind
    source_subtype: str
    rule_id: str
    boundary_type: str
    alignment_tier: str
    literature_basis: list[str] = Field(default_factory=list)
    review_state: Literal["auto", "reviewed", "rejected"] = "auto"
    source: BenchmarkSource
    notes: list[str] = Field(default_factory=list)

    @field_validator("case_id", "source_subtype", "rule_id", "boundary_type", "alignment_tier")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("字段不能为空")
        return text

    @field_validator("source_kind", mode="before")
    @classmethod
    def _normalize_source_kind(cls, value: str | SourceKind) -> SourceKind:
        return normalize_source_kind(value)

    @field_validator("literature_basis")
    @classmethod
    def _normalize_literature_basis(cls, value: list[str]) -> list[str]:
        return _normalize_str_list(value)


class BenchmarkShardOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_kind: str
    cases: list[BenchmarkDraftCase]
    warnings: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    review_summary: str | None = None

    @field_validator("source_kind")
    @classmethod
    def _validate_source_kind(cls, value: str) -> str:
        return normalize_source_kind(value).value


class BenchmarkCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    source_id: str
    dataset: str
    app_name: str
    apk_name: str
    source_dir: str
    source_kind: SourceKind
    source_subtype: str
    rule_id: str
    boundary_type: str
    alignment_tier: str
    literature_basis: list[str] = Field(default_factory=list)
    review_state: Literal["auto", "reviewed", "rejected"] = "auto"
    source: BenchmarkSource
    benchmark_family: Literal["source_first_mixed"] = "source_first_mixed"
    target_sink_categories: list[str] = Field(default_factory=lambda: list(DEFAULT_BENCHMARK_SINK_CATEGORIES))
    notes: list[str] = Field(default_factory=list)

    @field_validator(
        "case_id",
        "source_id",
        "dataset",
        "app_name",
        "apk_name",
        "source_dir",
        "source_subtype",
        "rule_id",
        "boundary_type",
        "alignment_tier",
    )
    @classmethod
    def _validate_non_empty_text(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("字段不能为空")
        return text

    @field_validator("source_kind", mode="before")
    @classmethod
    def _normalize_source_kind(cls, value: str | SourceKind) -> SourceKind:
        return normalize_source_kind(value)

    @field_validator("literature_basis")
    @classmethod
    def _normalize_literature_basis(cls, value: list[str]) -> list[str]:
        return _normalize_str_list(value)

    @field_validator("target_sink_categories")
    @classmethod
    def _validate_target_sink_categories(cls, value: list[str]) -> list[str]:
        normalized = [str(item or "").strip().lower() for item in value if str(item or "").strip()]
        if not normalized:
            raise ValueError("target_sink_categories 不能为空")
        out: list[str] = []
        seen: set[str] = set()
        for item in normalized:
            if item in seen:
                continue
            seen.add(item)
            out.append(item)
        return out


class BenchmarkOutput(BaseModel):
    schema_version: Literal[BENCHMARK_SCHEMA_V1] = BENCHMARK_SCHEMA_V1
    benchmark_family: Literal["source_first_mixed"] = "source_first_mixed"
    default_sink_categories: list[str] = Field(default_factory=lambda: list(DEFAULT_BENCHMARK_SINK_CATEGORIES))
    cases: list[BenchmarkCase] = Field(default_factory=list)

    @field_validator("default_sink_categories")
    @classmethod
    def _validate_default_sink_categories(cls, value: list[str]) -> list[str]:
        normalized = [str(item or "").strip().lower() for item in value if str(item or "").strip()]
        if not normalized:
            raise ValueError("default_sink_categories 不能为空")
        out: list[str] = []
        seen: set[str] = set()
        for item in normalized:
            if item in seen:
                continue
            seen.add(item)
            out.append(item)
        return out


class InventoryBuildConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_root: Path
    out_dir: Path
    dataset: str = "fdroid-open-source-app"
    source_kinds: list[SourceKind] = Field(default_factory=lambda: list(SourceKind))
    excluded_dirs: list[str] = Field(default_factory=list)
    include_extensions: list[str] = Field(default_factory=lambda: [".kt", ".java", ".xml"])
    app_names: list[str] = Field(default_factory=list)
    app_limit: int | None = None

    @field_validator("source_kinds", mode="before")
    @classmethod
    def _normalize_source_kinds(cls, value: list[str | SourceKind]) -> list[SourceKind]:
        out: list[SourceKind] = []
        seen: set[SourceKind] = set()
        for item in value or []:
            kind = normalize_source_kind(item)
            if kind in seen:
                continue
            seen.add(kind)
            out.append(kind)
        if not out:
            raise ValueError("source_kinds 不能为空")
        return out

    @field_validator("excluded_dirs", "include_extensions", "app_names")
    @classmethod
    def _normalize_text_list(cls, value: list[str]) -> list[str]:
        return _normalize_str_list([str(item or "").strip() for item in value or []])

    @field_validator("app_limit")
    @classmethod
    def _validate_app_limit(cls, value: int | None) -> int | None:
        if value is None:
            return None
        number = int(value)
        if number <= 0:
            raise ValueError("app_limit 必须为正整数")
        return number


class ExportBuildConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    inventory_root: Path
    out_dir: Path
    benchmark_name: str = "benchmark_v2"
    benchmark_family: Literal["source_first_mixed"] = "source_first_mixed"
    source_kinds: list[SourceKind] = Field(default_factory=lambda: list(SourceKind))
    max_cases_per_kind: int | None = None
    sink_categories: list[str] = Field(default_factory=lambda: list(DEFAULT_BENCHMARK_SINK_CATEGORIES))
    sort_fields: list[str] = Field(default_factory=lambda: list(DEFAULT_SORT_FIELDS))

    @field_validator("benchmark_name")
    @classmethod
    def _validate_benchmark_name(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("benchmark_name 不能为空")
        return text

    @field_validator("source_kinds", mode="before")
    @classmethod
    def _normalize_source_kinds(cls, value: list[str | SourceKind]) -> list[SourceKind]:
        out: list[SourceKind] = []
        seen: set[SourceKind] = set()
        for item in value or []:
            kind = normalize_source_kind(item)
            if kind in seen:
                continue
            seen.add(kind)
            out.append(kind)
        if not out:
            raise ValueError("source_kinds 不能为空")
        return out

    @field_validator("max_cases_per_kind")
    @classmethod
    def _validate_max_cases_per_kind(cls, value: int | None) -> int | None:
        if value is None:
            return None
        number = int(value)
        if number <= 0:
            raise ValueError("max_cases_per_kind 必须为正整数")
        return number

    @field_validator("sink_categories")
    @classmethod
    def _validate_sink_categories(cls, value: list[str]) -> list[str]:
        normalized = [str(item or "").strip().lower() for item in value or [] if str(item or "").strip()]
        if not normalized:
            raise ValueError("sink_categories 不能为空")
        out: list[str] = []
        seen: set[str] = set()
        for item in normalized:
            if item in seen:
                continue
            seen.add(item)
            out.append(item)
        return out

    @field_validator("sort_fields")
    @classmethod
    def _validate_sort_fields(cls, value: list[str]) -> list[str]:
        allowed = set(DEFAULT_SORT_FIELDS)
        out: list[str] = []
        seen: set[str] = set()
        for item in value or []:
            text = str(item or "").strip()
            if not text:
                continue
            if text not in allowed:
                raise ValueError(f"不支持的 sort_fields 项: {text}")
            if text in seen:
                continue
            seen.add(text)
            out.append(text)
        if not out:
            return list(DEFAULT_SORT_FIELDS)
        return out


@dataclass(frozen=True)
class InventoryBuildResult:
    manifest_path: Path
    inventory_path: Path
    summary_path: Path
    app_names: list[str]


@dataclass(frozen=True)
class ExportBuildResult:
    manifest_path: Path
    benchmark_path: Path
    summary_path: Path
    app_names: list[str]
