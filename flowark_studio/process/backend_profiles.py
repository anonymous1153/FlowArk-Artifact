from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from flowark.anthropic_env import (
    STUDIO_BACKEND_PROFILE_ENV,
    STUDIO_RUNTIME_AUTH_TOKEN_ENV,
    STUDIO_RUNTIME_BASE_URL_ENV,
    STUDIO_RUNTIME_MODEL_ENV,
)
from flowark.backend_transport import (
    INTERNAL_BACKEND_TRANSPORT_ACTIVE_ENV,
    INTERNAL_REUSE_EMBED_API_KEY_ENV,
    INTERNAL_REUSE_EMBED_BASE_URL_ENV,
    INTERNAL_REUSE_EMBED_MODEL_ENV,
    INTERNAL_REUSE_EMBED_VERIFY_SSL_ENV,
    INTERNAL_REUSE_RERANK_API_KEY_ENV,
    INTERNAL_REUSE_RERANK_BASE_URL_ENV,
    INTERNAL_REUSE_RERANK_MODEL_ENV,
    INTERNAL_REUSE_RERANK_TIMEOUT_SECONDS_ENV,
)
from flowark.state_paths import get_workspace_state_paths
from flowark.timeutil import now_tz8_iso

BACKEND_PROFILE_SCHEMA_VERSION = 4
WORKSPACE_ENV_BACKEND_PROFILE = "workspace_env"
LEGACY_WORKSPACE_ENV_LABEL = "legacy workspace_env"
RUNTIME_BACKEND_MODE_SINGLE = "single"
RUNTIME_BACKEND_MODE_CHOICES = (RUNTIME_BACKEND_MODE_SINGLE,)

BACKEND_SELECTION_SPECS: dict[str, dict[str, Any]] = {
    "runtime_backend": {
        "section": "runtime",
        "required": ("name", "base_url", "auth_token", "model"),
        "optional": (),
    },
    "reuse_embed_backend": {
        "section": "reuse_embed",
        "required": ("name", "base_url", "api_key", "model"),
        "optional": ("verify_ssl",),
    },
    "reuse_rerank_backend": {
        "section": "reuse_rerank",
        "required": ("name", "base_url", "api_key", "model"),
        "optional": ("timeout_seconds",),
    },
}

BACKEND_SELECTION_FIELDS = tuple(BACKEND_SELECTION_SPECS.keys())
BACKEND_SELECTION_HELP_PREFIX: dict[str, str] = {}
BACKEND_SELECTION_WORKSPACE_ENV_HINT: dict[str, str] = {
    "runtime_backend": "legacy compatibility path",
    "reuse_embed_backend": "legacy compatibility path",
    "reuse_rerank_backend": "legacy compatibility path",
}


def get_backend_profiles_path(workspace_root: Path | str) -> Path:
    return get_workspace_state_paths(workspace_root).studio_state_dir / "backend_profiles.json"


def load_backend_registry(workspace_root: Path | str) -> dict[str, Any]:
    path = get_backend_profiles_path(workspace_root)
    payload = _read_json_object(path)
    if not isinstance(payload, dict):
        return _empty_registry()
    schema_version = str(payload.get("schema_version") or "").strip()
    if schema_version == str(BACKEND_PROFILE_SCHEMA_VERSION):
        return _normalize_registry_v4(payload)
    if schema_version == "3":
        return _normalize_registry_v3(payload)
    if schema_version == "2":
        return _normalize_registry_v2_legacy(payload)
    return _normalize_legacy_profiles(payload)


def save_backend_registry(workspace_root: Path | str, registry: Mapping[str, Any]) -> Path:
    path = get_backend_profiles_path(workspace_root)
    payload = _normalize_registry_v4(dict(registry))
    payload["updated_at"] = now_tz8_iso()
    _write_json_object(path, payload)
    return path


def get_backend_selection_defaults(workspace_root: Path | str) -> dict[str, str]:
    registry = load_backend_registry(workspace_root)
    defaults: dict[str, str] = {}
    for field, spec in BACKEND_SELECTION_SPECS.items():
        section_payload = registry.get(spec["section"]) or {}
        defaults[field] = str(section_payload.get("default") or WORKSPACE_ENV_BACKEND_PROFILE)
    return defaults


def get_backend_selection_ui_defaults(workspace_root: Path | str) -> dict[str, str]:
    registry = load_backend_registry(workspace_root)
    return {
        field: _resolve_ui_default_name(registry, field)
        for field in BACKEND_SELECTION_FIELDS
    }


def list_backend_option_names(
    workspace_root: Path | str,
    field: str,
    *,
    include_workspace_env: bool = True,
    selectable_only: bool = False,
) -> list[str]:
    registry = load_backend_registry(workspace_root)
    return list_backend_option_names_from_registry(
        registry,
        field,
        include_workspace_env=include_workspace_env,
        selectable_only=selectable_only,
    )


def list_backend_option_names_from_registry(
    registry: Mapping[str, Any],
    field: str,
    *,
    include_workspace_env: bool = True,
    selectable_only: bool = False,
) -> list[str]:
    spec = BACKEND_SELECTION_SPECS[field]
    section_payload = registry.get(spec["section"]) or {}
    options = list(section_payload.get("options") or [])
    names = [
        str(item.get("name") or "")
        for item in options
        if str(item.get("name") or "").strip()
        and (not selectable_only or _is_selectable_option(item))
    ]
    if include_workspace_env:
        return [WORKSPACE_ENV_BACKEND_PROFILE] + names
    return names


def normalize_runtime_backend_mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in RUNTIME_BACKEND_MODE_CHOICES else RUNTIME_BACKEND_MODE_SINGLE


def get_backend_option_display_labels(
    workspace_root: Path | str,
    field: str,
    *,
    include_workspace_env: bool = True,
    selectable_only: bool = False,
) -> dict[str, str]:
    registry = load_backend_registry(workspace_root)
    labels: dict[str, str] = {}
    for name in list_backend_option_names_from_registry(
        registry,
        field,
        include_workspace_env=include_workspace_env,
        selectable_only=selectable_only,
    ):
        option = (
            resolve_backend_option_from_registry(registry, field, name)
            if name != WORKSPACE_ENV_BACKEND_PROFILE
            else None
        )
        labels[name] = str(
            _build_backend_selection_display(
                field=field,
                selected_name=name,
                option=option,
            )["label"]
        )
    return labels


def get_backend_selection_help_text(
    workspace_root: Path | str,
    field: str,
    *,
    include_workspace_env: bool = False,
    selectable_only: bool = True,
) -> str:
    base = BACKEND_SELECTION_HELP_PREFIX.get(field, "")
    labels = get_backend_option_display_labels(
        workspace_root,
        field,
        include_workspace_env=include_workspace_env,
        selectable_only=selectable_only,
    )
    ordered_names = list_backend_option_names(
        workspace_root,
        field,
        include_workspace_env=include_workspace_env,
        selectable_only=selectable_only,
    )
    items = [labels[name] for name in ordered_names if labels.get(name)]
    if not items:
        return base
    return "；".join(items)


def resolve_backend_option(
    workspace_root: Path | str,
    field: str,
    selection_name: Any,
) -> dict[str, Any] | None:
    registry = load_backend_registry(workspace_root)
    return resolve_backend_option_from_registry(registry, field, selection_name)


def resolve_backend_option_from_registry(
    registry: Mapping[str, Any],
    field: str,
    selection_name: Any,
) -> dict[str, Any] | None:
    spec = BACKEND_SELECTION_SPECS[field]
    section_payload = registry.get(spec["section"]) or {}
    selected = str(selection_name or WORKSPACE_ENV_BACKEND_PROFILE).strip() or WORKSPACE_ENV_BACKEND_PROFILE
    if selected == WORKSPACE_ENV_BACKEND_PROFILE:
        return None
    for option in list(section_payload.get("options") or []):
        if str(option.get("name") or "") == selected:
            return dict(option)
    raise ValueError(f"Cannot find {field}: {selected}")


def apply_backend_selection_param_defaults(
    *,
    workspace_root: Path | str,
    params: Mapping[str, Any],
    registry: Mapping[str, Any] | None = None,
    strict_explicit: bool = True,
) -> dict[str, Any]:
    resolved_registry = load_backend_registry(workspace_root) if registry is None else dict(registry)
    updated = dict(params)
    legacy_profile = str(updated.get("backend_profile") or "").strip()
    defaults = (
        {field: WORKSPACE_ENV_BACKEND_PROFILE for field in BACKEND_SELECTION_FIELDS}
        if strict_explicit
        else (
            get_backend_selection_defaults(workspace_root)
            if registry is None
            else {
                field: str((resolved_registry.get(spec["section"]) or {}).get("default") or WORKSPACE_ENV_BACKEND_PROFILE)
                for field, spec in BACKEND_SELECTION_SPECS.items()
            }
        )
    )
    for field in BACKEND_SELECTION_FIELDS:
        updated[field] = _normalize_selected_name(
            resolved_registry,
            field,
            updated.get(field),
            legacy_profile=legacy_profile,
            default_name=defaults[field],
            strict_explicit=strict_explicit,
        )
    return updated


def resolve_backend_selections(
    *,
    workspace_root: Path | str,
    params: Mapping[str, Any] | None,
) -> tuple[dict[str, str], dict[str, dict[str, Any] | None]]:
    registry = load_backend_registry(workspace_root)
    normalized_params = apply_backend_selection_param_defaults(
        workspace_root=workspace_root,
        params=params or {},
        registry=registry,
    )
    selected_names: dict[str, str] = {}
    resolved_options: dict[str, dict[str, Any] | None] = {}
    for field in BACKEND_SELECTION_FIELDS:
        selected_name = str(normalized_params.get(field) or WORKSPACE_ENV_BACKEND_PROFILE).strip() or WORKSPACE_ENV_BACKEND_PROFILE
        selected_names[field] = selected_name
        resolved_options[field] = resolve_backend_option_from_registry(
            registry,
            field,
            selected_name,
        )
    return selected_names, resolved_options


def build_backend_selection_display_metadata(
    *,
    workspace_root: Path | str,
    params: Mapping[str, Any] | None,
    strict_explicit: bool = False,
) -> dict[str, dict[str, Any]]:
    registry = load_backend_registry(workspace_root)
    normalized_params = apply_backend_selection_param_defaults(
        workspace_root=workspace_root,
        params=params or {},
        registry=registry,
        strict_explicit=strict_explicit,
    )
    metadata: dict[str, dict[str, Any]] = {}
    for field in BACKEND_SELECTION_FIELDS:
        selected_name = str(normalized_params.get(field) or WORKSPACE_ENV_BACKEND_PROFILE).strip() or WORKSPACE_ENV_BACKEND_PROFILE
        option = (
            resolve_backend_option_from_registry(registry, field, selected_name)
            if selected_name != WORKSPACE_ENV_BACKEND_PROFILE
            else None
        )
        metadata[field] = _build_backend_selection_display(
            field=field,
            selected_name=selected_name,
            option=option,
        )
    return metadata


def build_backend_selection_env_overrides(
    selections: Mapping[str, dict[str, Any] | None],
    *,
    selected_names: Mapping[str, str] | None = None,
    include_runtime_backend: bool = True,
) -> dict[str, str]:
    overrides: dict[str, str] = {}
    internal_transport_used = False

    runtime_backend = selections.get("runtime_backend")
    if include_runtime_backend and isinstance(runtime_backend, dict):
        base_url = str(runtime_backend.get("base_url") or "").strip()
        auth_token = str(runtime_backend.get("auth_token") or "").strip()
        model = str(runtime_backend.get("model") or "").strip()
        profile_name = (
            str((selected_names or {}).get("runtime_backend") or "").strip()
            or str(runtime_backend.get("profile_name") or "").strip()
            or str(runtime_backend.get("name") or "").strip()
            or "runtime_backend"
        )
        if base_url and auth_token and model:
            overrides[STUDIO_BACKEND_PROFILE_ENV] = profile_name
            overrides[STUDIO_RUNTIME_BASE_URL_ENV] = base_url
            overrides[STUDIO_RUNTIME_AUTH_TOKEN_ENV] = auth_token
            overrides[STUDIO_RUNTIME_MODEL_ENV] = model

    reuse_embed = selections.get("reuse_embed_backend")
    if isinstance(reuse_embed, dict):
        internal_transport_used = True
        overrides[INTERNAL_REUSE_EMBED_BASE_URL_ENV] = str(reuse_embed["base_url"])
        overrides[INTERNAL_REUSE_EMBED_API_KEY_ENV] = str(reuse_embed["api_key"])
        if str(reuse_embed.get("model") or "").strip():
            overrides[INTERNAL_REUSE_EMBED_MODEL_ENV] = str(reuse_embed["model"])
        if "verify_ssl" in reuse_embed:
            overrides[INTERNAL_REUSE_EMBED_VERIFY_SSL_ENV] = "true" if bool(reuse_embed["verify_ssl"]) else "false"

    reuse_rerank = selections.get("reuse_rerank_backend")
    if isinstance(reuse_rerank, dict):
        internal_transport_used = True
        overrides[INTERNAL_REUSE_RERANK_BASE_URL_ENV] = str(reuse_rerank["base_url"])
        overrides[INTERNAL_REUSE_RERANK_API_KEY_ENV] = str(reuse_rerank["api_key"])
        if str(reuse_rerank.get("model") or "").strip():
            overrides[INTERNAL_REUSE_RERANK_MODEL_ENV] = str(reuse_rerank["model"])
        if str(reuse_rerank.get("timeout_seconds") or "").strip():
            overrides[INTERNAL_REUSE_RERANK_TIMEOUT_SECONDS_ENV] = str(reuse_rerank["timeout_seconds"])

    if internal_transport_used:
        overrides[INTERNAL_BACKEND_TRANSPORT_ACTIVE_ENV] = "1"
    return overrides


def _empty_registry() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": BACKEND_PROFILE_SCHEMA_VERSION,
        "updated_at": now_tz8_iso(),
    }
    for spec in BACKEND_SELECTION_SPECS.values():
        payload[spec["section"]] = {
            "default": WORKSPACE_ENV_BACKEND_PROFILE,
            "options": [],
        }
    return payload


def _normalize_registry_v4(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _empty_registry()
    updated_at = str(payload.get("updated_at") or "").strip()
    if updated_at:
        normalized["updated_at"] = updated_at
    for spec in BACKEND_SELECTION_SPECS.values():
        section = spec["section"]
        section_payload = _normalize_section(
            payload.get(section),
            required_fields=spec["required"],
            optional_fields=tuple(spec.get("optional") or ()),
        )
        normalized[section] = section_payload
    return normalized


def _normalize_registry_v3(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _empty_registry()
    updated_at = str(payload.get("updated_at") or "").strip()
    if updated_at:
        normalized["updated_at"] = updated_at
    for spec in BACKEND_SELECTION_SPECS.values():
        section = spec["section"]
        normalized[section] = _normalize_section(
            payload.get(section),
            required_fields=spec["required"],
            optional_fields=tuple(spec.get("optional") or ()),
        )
    return normalized


def _normalize_registry_v2_legacy(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _empty_registry()
    updated_at = str(payload.get("updated_at") or "").strip()
    if updated_at:
        normalized["updated_at"] = updated_at
    for spec in BACKEND_SELECTION_SPECS.values():
        section = spec["section"]
        normalized[section] = _normalize_section(
            payload.get(section),
            required_fields=spec["required"],
            optional_fields=tuple(spec.get("optional") or ()),
            allow_incomplete_required_fields=("model",),
        )
    return normalized


def _normalize_legacy_profiles(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _empty_registry()
    raw_profiles = list(payload.get("profiles") or []) if isinstance(payload, Mapping) else []
    by_section_seen: dict[str, set[str]] = {
        spec["section"]: set()
        for spec in BACKEND_SELECTION_SPECS.values()
    }
    for item in raw_profiles:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name == WORKSPACE_ENV_BACKEND_PROFILE:
            continue
        for spec in BACKEND_SELECTION_SPECS.values():
            section = spec["section"]
            legacy_section = item.get(section)
            if not isinstance(legacy_section, dict):
                continue
            option_payload = {"name": name}
            for field in spec["required"]:
                if field == "name":
                    continue
                option_payload[field] = legacy_section.get(field)
            for field in tuple(spec.get("optional") or ()):
                if field in legacy_section:
                    option_payload[field] = legacy_section.get(field)
            option = _normalize_option(
                option_payload,
                required_fields=spec["required"],
                optional_fields=tuple(spec.get("optional") or ()),
                allow_incomplete_required_fields=("model",),
            )
            if option is None:
                continue
            option_name = str(option["name"])
            if option_name in by_section_seen[section]:
                continue
            by_section_seen[section].add(option_name)
            normalized[section]["options"].append(option)
    return normalized


def _normalize_section(
    payload: Any,
    *,
    required_fields: tuple[str, ...],
    optional_fields: tuple[str, ...],
    allow_incomplete_required_fields: tuple[str, ...] = (),
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"default": WORKSPACE_ENV_BACKEND_PROFILE, "options": []}
    raw_options = list(payload.get("options") or [])
    options: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for item in raw_options:
        option = _normalize_option(
            item,
            required_fields=required_fields,
            optional_fields=optional_fields,
            allow_incomplete_required_fields=allow_incomplete_required_fields,
        )
        if option is None:
            continue
        name = str(option["name"])
        if name in seen_names:
            continue
        seen_names.add(name)
        options.append(option)
    default_name = str(payload.get("default") or WORKSPACE_ENV_BACKEND_PROFILE).strip() or WORKSPACE_ENV_BACKEND_PROFILE
    if default_name != WORKSPACE_ENV_BACKEND_PROFILE and default_name not in seen_names:
        default_name = WORKSPACE_ENV_BACKEND_PROFILE
    return {
        "default": default_name,
        "options": options,
    }


def _normalize_option(
    payload: Any,
    *,
    required_fields: tuple[str, ...],
    optional_fields: tuple[str, ...],
    allow_incomplete_required_fields: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    normalized: dict[str, Any] = {}
    incomplete = False
    for field in required_fields:
        if field not in payload:
            if field in allow_incomplete_required_fields:
                incomplete = True
                continue
            return None
        value = payload.get(field)
        text = str(value or "").strip()
        if not text:
            if field in allow_incomplete_required_fields:
                incomplete = True
                continue
            return None
        normalized[field] = text
    for field in optional_fields:
        if field == "verify_ssl":
            normalized[field] = bool(payload.get(field, False))
            continue
        if field not in payload:
            continue
        text = str(payload.get(field) or "").strip()
        if text:
            normalized[field] = text
    if incomplete:
        normalized["legacy_incomplete"] = True
    return normalized


def _normalize_selected_name(
    registry: Mapping[str, Any],
    field: str,
    raw_value: Any,
    *,
    legacy_profile: str,
    default_name: str,
    strict_explicit: bool,
) -> str:
    selected = str(raw_value or "").strip()
    valid_names = set(list_backend_option_names_from_registry(registry, field))
    if selected in valid_names:
        return selected
    if selected and strict_explicit:
        raise ValueError(f"Cannot find {field}: {selected}")
    if legacy_profile and legacy_profile in valid_names:
        return legacy_profile
    if default_name in valid_names:
        return default_name
    return WORKSPACE_ENV_BACKEND_PROFILE


def _resolve_ui_default_name(registry: Mapping[str, Any], field: str) -> str:
    selectable = list_backend_option_names_from_registry(
        registry,
        field,
        include_workspace_env=False,
        selectable_only=True,
    )
    if not selectable:
        return WORKSPACE_ENV_BACKEND_PROFILE
    spec = BACKEND_SELECTION_SPECS[field]
    section_payload = registry.get(spec["section"]) or {}
    default_name = str(section_payload.get("default") or "").strip()
    if default_name in selectable:
        return default_name
    return selectable[0]


def _build_backend_selection_display(
    *,
    field: str,
    selected_name: str,
    option: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if selected_name == WORKSPACE_ENV_BACKEND_PROFILE:
        return _build_workspace_env_display(field=field)
    data = dict(option or {})
    is_legacy = bool(data.get("legacy_incomplete"))
    display_name = f"legacy {selected_name}" if is_legacy else selected_name
    base_url = str(data.get("base_url") or "").strip()
    model = str(data.get("model") or "").strip()
    secret = _mask_secret(_secret_text_for_field(field, data))
    label_parts = [display_name]
    if base_url:
        label_parts.append(base_url)
    if model:
        label_parts.append(f"model={model}")
    elif is_legacy:
        label_parts.append("model=missing")
    if secret:
        label_parts.append(f"{_secret_label_for_field(field)}={secret}")
    short_parts = [display_name]
    host = _host_from_url(base_url)
    if host:
        short_parts.append(host)
    if model:
        short_parts.append(model)
    elif is_legacy:
        short_parts.append("model-missing")
    return {
        "name": selected_name,
        "label": " · ".join(label_parts),
        "short_label": " · ".join(short_parts),
        "base_url": base_url,
        "model": model,
        "secret_ref": secret,
        "legacy": is_legacy,
    }


def _build_workspace_env_display(*, field: str) -> dict[str, Any]:
    return {
        "name": WORKSPACE_ENV_BACKEND_PROFILE,
        "label": f"{LEGACY_WORKSPACE_ENV_LABEL} · {BACKEND_SELECTION_WORKSPACE_ENV_HINT.get(field, 'legacy compatibility path')}",
        "short_label": LEGACY_WORKSPACE_ENV_LABEL,
        "base_url": "",
        "model": "",
        "secret_ref": "",
        "legacy": True,
    }


def _is_selectable_option(option: Mapping[str, Any]) -> bool:
    return not bool(option.get("legacy_incomplete"))


def _secret_text_for_field(field: str, option: Mapping[str, Any]) -> str:
    key = "auth_token" if field == "runtime_backend" else "api_key"
    return str(option.get(key) or "").strip()


def _secret_label_for_field(field: str) -> str:
    return "token" if field == "runtime_backend" else "key"


def _mask_secret(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return f"{text[:2]}***"
    return f"{text[:8]}...{text[-5:]}"


def _host_from_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    return parsed.netloc or text


def _read_json_object(path: Path) -> dict[str, Any] | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _write_json_object(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)
