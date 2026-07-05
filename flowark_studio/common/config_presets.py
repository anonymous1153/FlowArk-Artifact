from __future__ import annotations

from pathlib import Path
from typing import Any

from flowark.config import load_eval_config_defaults
from flowark.state_paths import resolve_eval_config_out_dir


def _field(
    name: str,
    label: str,
    field_type: str,
    *,
    default: Any = None,
    required: bool = False,
    enum: list[str] | None = None,
    enum_labels: dict[str, str] | None = None,
    help_text: str = "",
    placeholder: str | None = None,
    section: str | None = None,
    advanced: bool = False,
    default_open: bool | None = None,
    ui_variant: str | None = None,
    allow_custom: bool = False,
) -> dict[str, Any]:
    resolved_section = section or _default_field_section(name)
    resolved_default_open = default_open
    if resolved_default_open is None and resolved_section:
        resolved_default_open = _default_section_open(resolved_section)
    data: dict[str, Any] = {
        "name": name,
        "label": label,
        "type": field_type,
        "required": required,
        "default": default,
        "help": help_text,
    }
    if enum:
        data["enum"] = enum
    if enum_labels:
        data["enum_labels"] = enum_labels
    if placeholder:
        data["placeholder"] = placeholder
    if resolved_section:
        data["section"] = resolved_section
    if advanced:
        data["advanced"] = True
    if resolved_default_open is not None:
        data["default_open"] = bool(resolved_default_open)
    if ui_variant:
        data["ui_variant"] = ui_variant
    if allow_custom:
        data["allow_custom"] = True
    return data


EXPERIMENT_PRESET_VALUES = [
    "naive",
    "flowark_full",
    "m1_generic",
    "m2_embedding",
    "m3_start_only",
    "mem0_enabled_opencode",
    "analysis_log_rag",
]

EXPERIMENT_PRESET_LABELS = {
    "naive": "Standard opencode",
    "flowark_full": "FlowArk-enabled opencode",
    "m1_generic": "M1 Generic",
    "m2_embedding": "M2 Embedding",
    "m3_start_only": "M3 Start-only",
    "mem0_enabled_opencode": "Mem0-enabled opencode",
    "analysis_log_rag": "Analysis-Log RAG Baseline",
}

DATASET_PRESET_VALUES = ["strat15", "main50"]

DATASET_PRESET_LABELS = {
    "strat15": "Strat15",
    "main50": "Main50",
}

DATASET_PRESET_RELATIVE_PATHS = {
    "strat15": Path("artifact-data/benchmarks/source-first-v3.2-strat15.json"),
    "main50": Path("artifact-data/benchmarks/source-first-v3.2-main50.json"),
}

MODEL_BACKEND_PRESET_VALUES = ["custom"]

MODEL_BACKEND_PRESET_LABELS = {"custom": "Custom"}

MODEL_BACKEND_PRESET_SPECS: dict[str, dict[str, Any]] = {}

PUBLIC_OPENCODE_PROVIDERS = {"anthropic", "openai"}


def _public_opencode_provider(value: Any, *, default: str = "anthropic") -> str:
    provider = str(value or default).strip().lower()
    return provider if provider in PUBLIC_OPENCODE_PROVIDERS else default

FIELD_SECTION_BY_NAME = {
    "experiment_preset": "Experiment preset",
    "model_backend_preset": "Experiment preset",
    "dataset_preset": "Input and sampling",
    "modes": "Experiment preset",
    "input_path": "Input and sampling",
    "app_names": "Input and sampling",
    "out_dir": "Input and sampling",
    "parallel": "Input and sampling",
    "serialize_within_app": "Input and sampling",
    "max_cases": "Input and sampling",
    "max_apps": "Input and sampling",
    "max_sources": "Input and sampling",
    "classification_filter": "Input and sampling",
    "agent_adapter": "Agent / OpenCode",
    "opencode_binary": "Agent / OpenCode",
    "opencode_model": "Model backend",
    "opencode_provider": "Model backend",
    "runtime_backend_base_url": "Model backend",
    "runtime_backend_auth_token": "Model backend",
    "opencode_after_tool_delivery": "Agent / OpenCode",
    "opencode_bash_policy": "Agent / OpenCode",
    "opencode_post_phase_mode": "Agent / OpenCode",
    "opencode_structured_output": "Agent / OpenCode",
    "runtime_backend": "Backends",
    "runtime_backend_mode": "Backends",
    "reuse_embed_backend": "Backends",
    "reuse_rerank_backend": "Backends",
    "dummy_run": "Debug and compatibility",
    "timeout_seconds": "Debug and compatibility",
}

FLOWARK_FIELD_PREFIXES = ("knowledge_",)
FLOWARK_FIELD_NAMES = {
    "auto_knowledge_cycle",
    "runtime_injection_mode",
    "auto_knowledge_validate_mode",
}

SECTION_DEFAULT_OPEN = {
    "Experiment preset": True,
    "Input and sampling": True,
    "Model backend": True,
    "FlowArk parameters": True,
    "Agent / OpenCode": False,
    "Backends": False,
    "Debug and compatibility": False,
}


def _default_field_section(name: str) -> str:
    field_name = str(name or "").strip()
    if field_name in FIELD_SECTION_BY_NAME:
        return FIELD_SECTION_BY_NAME[field_name]
    if field_name.startswith(FLOWARK_FIELD_PREFIXES) or field_name in FLOWARK_FIELD_NAMES:
        return "FlowArk parameters"
    return "Debug and compatibility"


def _default_section_open(section: str) -> bool:
    return SECTION_DEFAULT_OPEN.get(str(section or ""), False)


def _apply_flowark_full_preset_defaults(defaults: dict[str, Any]) -> dict[str, Any]:
    next_defaults = dict(defaults)
    next_defaults["experiment_preset"] = "flowark_full"
    next_defaults["agent_adapter"] = "opencode"
    next_defaults["knowledge_mode"] = "warm"
    next_defaults["knowledge_allow_repeat_injection_within_session"] = True
    next_defaults["auto_knowledge_cycle"] = True
    next_defaults["runtime_injection_mode"] = "context_aware"
    next_defaults["knowledge_distillation_mode"] = "with_selection_rules"
    next_defaults["knowledge_packaging_mode"] = "dsl_rule"
    next_defaults["auto_knowledge_validate_mode"] = "static"
    next_defaults["knowledge_reuse_digest_mode"] = "live_corridor_v2"
    next_defaults["modes"] = "flowark"
    return next_defaults


def apply_experiment_preset_to_params(*, params: dict[str, Any], kind: str) -> dict[str, Any]:
    """Apply the public Studio preset contract before command construction."""
    updated = dict(params or {})
    preset = str(updated.get("experiment_preset") or "").strip().lower()
    if preset not in set(EXPERIMENT_PRESET_VALUES):
        return updated

    updated["experiment_preset"] = preset
    updated["agent_adapter"] = "opencode"

    def _set_mode(value: str) -> None:
        updated["modes"] = value

    def _set_naive_defaults() -> None:
        _set_mode("naive")
        updated["runtime_injection_mode"] = "context_aware"
        updated["knowledge_distillation_mode"] = "with_selection_rules"
        updated["knowledge_packaging_mode"] = "dsl_rule"
        updated["auto_knowledge_validate_mode"] = "static"
        updated["knowledge_reuse_digest_mode"] = "off"
        updated["auto_knowledge_cycle"] = True

    def _set_flowark_defaults() -> None:
        _set_mode("flowark")
        updated["knowledge_mode"] = "warm"
        updated["knowledge_allow_repeat_injection_within_session"] = True
        updated["auto_knowledge_cycle"] = True
        updated["runtime_injection_mode"] = "context_aware"
        updated["knowledge_distillation_mode"] = "with_selection_rules"
        updated["knowledge_packaging_mode"] = "dsl_rule"
        updated["auto_knowledge_validate_mode"] = "static"
        updated["knowledge_reuse_digest_mode"] = "live_corridor_v2"

    if preset in {"naive", "mem0_enabled_opencode"}:
        _set_naive_defaults()
        return updated

    _set_flowark_defaults()
    if preset == "m1_generic":
        updated["knowledge_distillation_mode"] = "generic"
        updated["auto_knowledge_validate_mode"] = "off"
        updated["knowledge_reuse_digest_mode"] = "off"
    elif preset == "m2_embedding":
        updated["knowledge_packaging_mode"] = "embedding"
        updated["auto_knowledge_validate_mode"] = "off"
    elif preset == "m3_start_only":
        updated["runtime_injection_mode"] = "start_only"
    elif preset == "analysis_log_rag":
        updated["auto_knowledge_cycle"] = False
        updated["knowledge_packaging_mode"] = "analysis_log_rag"
        updated["auto_knowledge_validate_mode"] = "off"
        updated["knowledge_reuse_digest_mode"] = "off"
        updated["knowledge_top_k"] = 3
        updated["repeats"] = 1
    return updated


def normalize_model_backend_preset(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in MODEL_BACKEND_PRESET_VALUES else "custom"


def infer_model_backend_preset_from_params(params: dict[str, Any] | None) -> str:
    return "custom"


def infer_model_backend_preset_from_effective_params(params: dict[str, Any] | None) -> str:
    return "custom"


def apply_model_backend_preset_to_params(
    *,
    workspace_root: Path,
    params: dict[str, Any],
    defaults: dict[str, Any],
    strict_runtime_profile: bool = False,
) -> dict[str, Any]:
    updated = dict(params or {})
    updated.pop("runtime_backend_pool", None)
    updated["model_backend_preset"] = "custom"
    updated["agent_adapter"] = "opencode"
    provider = str(updated.get("opencode_provider") or defaults.get("opencode_provider") or "anthropic").strip().lower()
    if provider not in PUBLIC_OPENCODE_PROVIDERS:
        raise ValueError("The public Studio only supports opencode_provider=anthropic or openai")
    updated["opencode_provider"] = provider
    opencode_model = str(updated.get("opencode_model") or defaults.get("opencode_model") or "").strip()
    if opencode_model:
        updated["opencode_model"] = opencode_model
    else:
        updated.pop("opencode_model", None)
    updated["runtime_backend_mode"] = "single"
    if str(updated.get("runtime_backend") or "").strip():
        updated["runtime_backend"] = str(updated.get("runtime_backend") or "").strip()
    else:
        updated.pop("runtime_backend", None)
    return updated


def _nonnegative_int(value: Any, default: int) -> int:
    if value in {None, ""}:
        return max(0, int(default))
    try:
        return max(0, int(value))
    except Exception:
        return max(0, int(default))


def _entry_auto_knowledge_validate_mode(value: Any) -> str:
    mode = str(value or "static").strip().lower()
    if mode == "full":
        raise ValueError("Studio configuration no longer supports auto_knowledge_validate_mode=full; use static or off")
    if mode not in {"off", "static"}:
        raise ValueError("Studio configuration auto_knowledge_validate_mode must be off or static")
    return mode


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _config_workspace_root(workspace_root: Path) -> Path:
    runtime_path = workspace_root / "flowark" / "config" / "runtime.yaml"
    eval_path = workspace_root / "flowark" / "config" / "eval.yaml"
    if runtime_path.exists() and eval_path.exists():
        return workspace_root
    return _repo_root()


def normalize_dataset_preset(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in DATASET_PRESET_RELATIVE_PATHS else "strat15"


def resolve_dataset_preset_path(*, workspace_root: Path, dataset_preset: Any) -> Path:
    preset = normalize_dataset_preset(dataset_preset)
    return (workspace_root / DATASET_PRESET_RELATIVE_PATHS[preset]).expanduser().resolve()


def get_eval_defaults(*, workspace_root: Path) -> dict[str, Any]:
    cfg = load_eval_config_defaults(_config_workspace_root(workspace_root))
    app_names = cfg.get("app_names") or []
    if isinstance(app_names, list):
        app_names_text = ",".join(str(item).strip() for item in app_names if str(item).strip())
    else:
        app_names_text = str(app_names or "").strip()
    defaults = {
        "agent_adapter": str(cfg.get("agent_adapter") or "opencode"),
        "opencode_binary": str(cfg.get("opencode_binary") or ""),
        "opencode_model": str(cfg.get("opencode_model") or ""),
        "opencode_provider": _public_opencode_provider(cfg.get("opencode_provider")),
        "runtime_backend_base_url": "",
        "runtime_backend_auth_token": "",
        "opencode_after_tool_delivery": str(cfg.get("opencode_after_tool_delivery") or "no_reply_context"),
        "opencode_bash_policy": str(cfg.get("opencode_bash_policy") or "read_only_guarded"),
        "opencode_post_phase_mode": str(cfg.get("opencode_post_phase_mode") or "plain_json_same_surface"),
        "opencode_structured_output": bool(cfg.get("opencode_structured_output", True)),
        "modes": [str(item) for item in (cfg.get("modes") or ["naive", "flowark"]) if str(item).strip()],
        "app_names": app_names_text,
        "parallel": int(cfg.get("parallel") or 1),
        "serialize_within_app": False,
        "out_dir": str(resolve_eval_config_out_dir(cfg.get("out_dir"), workspace_root=workspace_root)),
        "knowledge_mode": str(cfg.get("knowledge_mode") or "warm"),
        "knowledge_allow_repeat_injection_within_session": bool(
            cfg.get("knowledge_allow_repeat_injection_within_session", True)
        ),
        "auto_knowledge_cycle": bool(cfg.get("auto_knowledge_cycle", True)),
        "runtime_injection_mode": str(cfg.get("runtime_injection_mode") or "context_aware"),
        "knowledge_distillation_mode": str(
            cfg.get("knowledge_distillation_mode") or "with_selection_rules"
        ),
        "knowledge_packaging_mode": str(cfg.get("knowledge_packaging_mode") or "dsl_rule"),
        "auto_knowledge_validate_mode": _entry_auto_knowledge_validate_mode(
            cfg.get("auto_knowledge_validate_mode")
        ),
        "knowledge_reuse_digest_mode": str(cfg.get("knowledge_reuse_digest_mode") or "off"),
        "knowledge_repeat_summary_react_gap": _nonnegative_int(
            cfg.get("knowledge_repeat_summary_react_gap"),
            0,
        ),
        "knowledge_repeat_full_react_gap": _nonnegative_int(
            cfg.get("knowledge_repeat_full_react_gap"),
            1,
        ),
        "knowledge_top_k": int(cfg.get("knowledge_top_k") or 3),
        "knowledge_recall_top_m": int(cfg.get("knowledge_recall_top_m") or 8),
        "dummy_run": bool(cfg.get("dummy_run", False)),
        "classification_filter": str(cfg.get("classification_filter") or "all"),
        "timeout_seconds": int(cfg.get("timeout_seconds") or 1800),
        "runtime_backend_mode": "single",
        "dataset_preset": "strat15",
    }
    defaults["model_backend_preset"] = "custom"
    return _apply_flowark_full_preset_defaults(defaults)


def get_eval_schema(*, workspace_root: Path) -> dict[str, Any]:
    defaults = get_eval_defaults(workspace_root=workspace_root)
    fields = [
        _field(
            "experiment_preset",
            "Experiment preset",
            "enum",
            default=defaults["experiment_preset"],
            enum=EXPERIMENT_PRESET_VALUES,
            enum_labels=EXPERIMENT_PRESET_LABELS,
            help_text="Studio shortcut; each eval starts one clean experimental condition.",
            ui_variant="preset",
        ),
        _field(
            "dataset_preset",
            "Dataset preset",
            "enum",
            default=defaults["dataset_preset"],
            enum=DATASET_PRESET_VALUES,
            enum_labels=DATASET_PRESET_LABELS,
            help_text="Selects the repository-local benchmark JSON; no manual input path is required.",
        ),
        _field(
            "opencode_provider",
            "API format",
            "enum",
            default=defaults["opencode_provider"],
            enum=["anthropic", "openai"],
            enum_labels={"anthropic": "Anthropic-compatible", "openai": "OpenAI-compatible"},
            help_text="Select the API format exposed by your model gateway.",
        ),
        _field(
            "runtime_backend_base_url",
            "Base URL",
            "text",
            default=defaults["runtime_backend_base_url"],
            required=True,
            placeholder="https://example.com/api/anthropic",
            help_text="Model gateway base URL.",
        ),
        _field(
            "runtime_backend_auth_token",
            "API key",
            "password",
            default=defaults["runtime_backend_auth_token"],
            required=True,
            help_text="Used only to start the eval process; it is not stored in task parameters.",
        ),
        _field(
            "opencode_model",
            "Model",
            "text",
            default=defaults["opencode_model"],
            required=True,
            placeholder="glm-4.7",
            help_text="Model id passed to OpenCode.",
        ),
        _field(
            "max_cases",
            "Max cases",
            "int",
            default="",
            help_text="Optional smoke-test limit. Leave empty to run the full selected dataset.",
        ),
    ]
    public_defaults = {
        str(field["name"]): defaults.get(str(field["name"]))
        for field in fields
        if str(field.get("name") or "")
    }
    return {
        "kind": "eval",
        "defaults": public_defaults,
        "fields": fields,
    }
