"""Convenience Python entrypoint for batch evaluation.

Edit the parameters in `flowark/config/eval.yaml` and run:

    uv run python eval_main.py
"""

from __future__ import annotations

import sys
import asyncio
from pathlib import Path

from flowark.config import EVAL_CONFIG_RELATIVE_PATH, load_eval_config_defaults, resolve_eval_env_config
from flowark.eval.harness import EvaluationConfig, run_evaluation
from flowark.eval.harness.models import validate_embedding_packaging_backend_config
from flowark.knowledge_packaging import normalize_knowledge_packaging_mode
from flowark.runtime.config import (
    KNOWLEDGE_DISTILLATION_GENERIC,
    normalize_auto_knowledge_validate_mode,
    normalize_knowledge_distillation_mode,
    normalize_knowledge_reuse_digest_mode,
    normalize_knowledge_runtime_modes,
    normalize_runtime_injection_mode,
)
from flowark.state_paths import format_legacy_state_dirs_warning, resolve_eval_config_out_dir

# 配置文件路径
CONFIG_FILE = Path(__file__).parent / EVAL_CONFIG_RELATIVE_PATH


def _env_bool(value: object, *, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def load_config() -> dict:
    """加载 YAML 配置文件。"""
    return load_eval_config_defaults(Path(__file__).parent.resolve())


def _reject_removed_router_config(cfg: dict[str, object], *, context_label: str) -> None:
    legacy_prefix = "knowledge" + "_router_"
    removed = sorted(str(key) for key in cfg.keys() if str(key).startswith(legacy_prefix))
    if removed:
        raise ValueError(
            f"{context_label} 配置仍包含已移除的 {legacy_prefix}* 字段，请改用 "
            f"knowledge_recall_top_m / knowledge_top_k: {', '.join(removed)}"
        )


def _resolve_entry_auto_knowledge_validate_mode(cfg: dict[str, object]) -> str:
    mode = str(cfg.get("auto_knowledge_validate_mode") or "static").strip().lower()
    if mode == "full":
        raise ValueError(
            "eval 配置不再支持 auto_knowledge_validate_mode=full，请改用 static 或 off"
        )
    if mode not in {"off", "static"}:
        raise ValueError("eval 配置 auto_knowledge_validate_mode 必须是 off 或 static")
    return mode


def main() -> int:
    workspace_root = Path(__file__).parent.resolve()
    warning = format_legacy_state_dirs_warning(workspace_root)
    if warning:
        print(f"[eval_main] {warning}", file=sys.stderr, flush=True)

    # 从配置文件加载参数
    cfg = load_config()
    _reject_removed_router_config(cfg, context_label="eval")
    llm_judge_enabled = bool(cfg.get("llm_judge_enabled", True))
    env_cfg = resolve_eval_env_config(workspace_root)
    llm_judge_base_url = str(env_cfg.get("llm_judge_base_url") or "").strip() if llm_judge_enabled else ""
    llm_judge_api_key = str(env_cfg.get("llm_judge_api_key") or "").strip() if llm_judge_enabled else ""
    llm_judge_model = str(cfg.get("llm_judge_model") or "glm-4.7").strip()
    if llm_judge_enabled:
        missing: list[str] = []
        if not llm_judge_base_url:
            missing.append("FLOWARK_LLM_JUDGE_BASE_URL")
        if not llm_judge_api_key:
            missing.append("FLOWARK_LLM_JUDGE_API_KEY")
        if not llm_judge_model:
            missing.append("llm_judge_model")
        if missing:
            raise ValueError(f".env / eval 配置缺少字段: {', '.join(missing)}")

    distillation_mode = normalize_knowledge_distillation_mode(
        str(cfg.get("knowledge_distillation_mode") or "with_selection_rules")
    )
    validate_mode = normalize_auto_knowledge_validate_mode(
        _resolve_entry_auto_knowledge_validate_mode(cfg)
    )
    digest_mode = normalize_knowledge_reuse_digest_mode(
        str(cfg.get("knowledge_reuse_digest_mode") or "off")
    )
    if distillation_mode == KNOWLEDGE_DISTILLATION_GENERIC:
        overridden: list[str] = []
        if validate_mode != "off":
            overridden.append(f"auto_knowledge_validate_mode={validate_mode}->off")
        if digest_mode != "off":
            overridden.append(f"knowledge_reuse_digest_mode={digest_mode}->off")
        if overridden:
            print(
                "[eval_main] knowledge_distillation_mode=generic disables "
                + ", ".join(overridden),
                file=sys.stderr,
                flush=True,
            )
    distillation_mode, validate_mode, digest_mode = normalize_knowledge_runtime_modes(
        knowledge_distillation_mode=distillation_mode,
        auto_knowledge_validate_mode=validate_mode,
        knowledge_reuse_digest_mode=digest_mode,
    )
    knowledge_packaging_mode = normalize_knowledge_packaging_mode(
        str(cfg.get("knowledge_packaging_mode") or "dsl_rule")
    )
    reuse_embed_base_url = str(env_cfg.get("reuse_embed_base_url") or "").strip() or None
    reuse_embed_api_key = str(env_cfg.get("reuse_embed_api_key") or "").strip() or None
    reuse_embed_model = str(env_cfg.get("reuse_embed_model") or "").strip() or None
    reuse_embed_verify_ssl = _env_bool(env_cfg.get("reuse_embed_verify_ssl"), default=False)

    config = EvaluationConfig(
        input_path=Path(cfg["input_path"]).expanduser().resolve(),
        out_dir=resolve_eval_config_out_dir(cfg.get("out_dir"), workspace_root=workspace_root),
        agent_adapter=str(cfg.get("agent_adapter") or "opencode"),
        opencode_binary=cfg.get("opencode_binary"),
        opencode_model=str(cfg.get("opencode_model") or "glm-4.7"),
        opencode_provider=str(cfg.get("opencode_provider") or "anthropic"),
        opencode_after_tool_delivery=str(
            cfg.get("opencode_after_tool_delivery") or "no_reply_context"
        ),
        opencode_bash_policy=str(cfg.get("opencode_bash_policy") or "read_only_guarded"),
        opencode_post_phase_mode=str(
            cfg.get("opencode_post_phase_mode") or "plain_json_same_surface"
        ),
        opencode_structured_output=bool(cfg.get("opencode_structured_output", True)),
        modes=list(cfg["modes"]),
        parallel=int(cfg["parallel"]),
        serialize_within_app=bool(cfg.get("serialize_within_app", True)),
        repeats=int(cfg["repeats"]),
        max_apps=(int(cfg["max_apps"]) if cfg["max_apps"] is not None else None),
        max_sources=(int(cfg["max_sources"]) if cfg["max_sources"] is not None else None),
        app_names=list(cfg.get("app_names") or []),
        classification_filter=str(cfg["classification_filter"]),
        dummy_run=bool(cfg["dummy_run"]),
        knowledge_mode=str(cfg["knowledge_mode"]),
        knowledge_allow_repeat_injection_within_session=bool(
            cfg.get("knowledge_allow_repeat_injection_within_session", True)
        ),
        auto_knowledge_cycle=bool(cfg["auto_knowledge_cycle"]),
        runtime_injection_mode=normalize_runtime_injection_mode(
            str(cfg.get("runtime_injection_mode") or "context_aware")
        ),
        knowledge_distillation_mode=distillation_mode,
        knowledge_packaging_mode=knowledge_packaging_mode,
        auto_knowledge_validate_mode=validate_mode,
        knowledge_reuse_digest_mode=digest_mode,
        knowledge_top_k=int(cfg.get("knowledge_top_k") or 3),
        knowledge_recall_top_m=int(cfg.get("knowledge_recall_top_m") or 8),
        timeout_seconds=(int(cfg["timeout_seconds"]) if cfg["timeout_seconds"] is not None else 1800),
        llm_judge_enabled=llm_judge_enabled,
        llm_judge_base_url=llm_judge_base_url,
        llm_judge_api_key=llm_judge_api_key,
        llm_judge_model=llm_judge_model,
        llm_judge_timeout_seconds=int(cfg.get("llm_judge_timeout_seconds", 120)),
        llm_judge_max_retries=int(cfg.get("llm_judge_max_retries", 2)),
        reuse_embed_base_url=reuse_embed_base_url,
        reuse_embed_api_key=reuse_embed_api_key,
        reuse_embed_model=reuse_embed_model,
        reuse_embed_verify_ssl=reuse_embed_verify_ssl,
    )
    validate_embedding_packaging_backend_config(config)

    print(f"[eval_main] config: {CONFIG_FILE}")
    print(f"[eval_main] input: {config.input_path}")
    print(
        f"[eval_main] modes: {','.join(config.modes)} "
        f"parallel={config.parallel} repeats={config.repeats} "
        f"classification_filter={config.normalized_classification_filter()} "
        f"app_names={config.normalized_app_names()} "
        f"max_apps={config.normalized_max_apps()} "
        f"max_sources={config.effective_max_sources()} "
        f"dummy_run={config.dummy_run} "
        f"llm_judge={config.llm_judge_enabled} "
        f"judge_model={config.llm_judge_model}"
    )

    try:
        result = asyncio.run(run_evaluation(config, workspace_root=workspace_root))
    except KeyboardInterrupt:
        print("\n用户中断。")
        return 130

    print(f"评估目录: {result['eval_root']}")
    print(f"汇总文件: {result['summary_path']}")
    print(f"对比文件: {result['comparison_path']}")
    print(f"结果明细: {result['results_path']}")
    print(
        "任务状态: "
        f"success={int(result.get('success_count') or 0)} "
        f"warning={int(result.get('warning_count') or 0)} "
        f"error={int(result.get('error_count') or 0)}"
    )
    artifact_audit = result.get("artifact_audit") if isinstance(result.get("artifact_audit"), dict) else None
    if artifact_audit is not None:
        print(f"产物审计: {artifact_audit.get('path')}")
        print(
            "产物审计结果: "
            f"{'ok' if artifact_audit.get('ok') else 'failed'} "
            f"(errors={artifact_audit.get('error_count', 0)}, warnings={artifact_audit.get('warning_count', 0)})"
        )
    return 0 if artifact_audit is None or bool(artifact_audit.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
