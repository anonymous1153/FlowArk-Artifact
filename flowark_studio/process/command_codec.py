from __future__ import annotations

from pathlib import Path
from typing import Any

from flowark.state_paths import ensure_indexable_eval_out_dir
from flowark_studio.common.config_presets import (
    apply_experiment_preset_to_params,
    apply_model_backend_preset_to_params,
    get_eval_defaults,
    normalize_dataset_preset,
    resolve_dataset_preset_path,
)
from flowark_studio.common.models import TaskKind


class StudioCommandCodec:
    def __init__(self, *, workspace_root: Path, state_paths: Any) -> None:
        self.workspace_root = workspace_root
        self._state_paths = state_paths

    def build_command(self, kind: TaskKind, params: dict[str, Any]) -> tuple[list[str], Path, dict[str, Any]]:
        if kind != "eval":
            raise ValueError("The public Studio only supports eval tasks")
        return self.build_eval_command(params)

    def normalize_effective_params(
        self,
        kind: TaskKind,
        params: dict[str, Any],
        *,
        strict_model_backend_profile: bool = True,
    ) -> tuple[dict[str, Any], list[str]]:
        if kind != "eval":
            raise ValueError("The public Studio only supports eval tasks")
        defaults = get_eval_defaults(workspace_root=self.workspace_root)
        next_params = apply_experiment_preset_to_params(params=dict(params or {}), kind=kind)
        next_params.pop("runtime_backend_pool", None)
        should_normalize = self._modes_include_flowark(
            next_params.get("modes"),
            defaults.get("modes") or ["naive", "flowark"],
        )
        warnings: list[str] = []
        experiment_preset = str(next_params.get("experiment_preset") or "").strip().lower()
        if experiment_preset in {"analysis_log_rag", "analysis_log_rag_initial"}:
            next_params["model_backend_preset"] = "custom"
            previous_provider = str(next_params.get("opencode_provider") or "").strip().lower()
            if previous_provider and previous_provider not in {"anthropic", "openai"}:
                next_params.pop("opencode_provider", None)
                next_params.pop("opencode_model", None)
            for stale_key in (
                "runtime_backend_mode",
                "runtime_backend",
            ):
                next_params.pop(stale_key, None)
        next_params = apply_model_backend_preset_to_params(
            workspace_root=self.workspace_root,
            params=next_params,
            defaults=defaults,
            strict_runtime_profile=strict_model_backend_profile,
        )
        self._reject_removed_auto_validate_repair_param(next_params)
        self._validate_entry_auto_knowledge_validate_param(next_params.get("auto_knowledge_validate_mode"))
        agent_adapter = str(
            next_params.get("agent_adapter") or defaults.get("agent_adapter") or "opencode"
        ).strip().lower().replace("-", "_")
        next_params["runtime_backend_mode"] = "single"
        if not should_normalize:
            self._reset_flowark_only_params(next_params, defaults)
            return next_params, warnings
        distillation_mode, packaging_mode, validate_mode, digest_mode, knowledge_warnings = self._knowledge_mode_values(
            next_params,
            defaults,
        )
        warnings.extend(knowledge_warnings)
        next_params["knowledge_distillation_mode"] = distillation_mode
        next_params["knowledge_packaging_mode"] = packaging_mode
        next_params["auto_knowledge_validate_mode"] = validate_mode
        next_params["knowledge_reuse_digest_mode"] = digest_mode
        if packaging_mode in {"analysis_log_rag", "analysis_log_rag_initial"}:
            if self._as_bool(
                next_params.get("auto_knowledge_cycle"),
                default=bool(defaults.get("auto_knowledge_cycle", True)),
            ):
                warnings.append(
                    f"knowledge_packaging_mode={packaging_mode} disables auto_knowledge_cycle=on->off"
                )
            next_params["auto_knowledge_cycle"] = False
            if kind == "eval":
                repeats = self._safe_positive_int(
                    next_params.get("repeats"),
                    default=int(defaults.get("repeats") or 1),
                )
                if repeats != 1:
                    warnings.append(
                        f"knowledge_packaging_mode={packaging_mode} requires repeats={repeats}->1"
                    )
                    next_params["repeats"] = 1
        runtime_injection_mode = self._one_of(
            next_params.get("runtime_injection_mode"),
            ["context_aware", "start_only"],
            str(defaults.get("runtime_injection_mode") or "context_aware"),
        )
        next_params["runtime_injection_mode"] = runtime_injection_mode
        if packaging_mode == "analysis_log_rag_initial":
            if runtime_injection_mode != "start_only":
                warnings.append(
                    "knowledge_packaging_mode=analysis_log_rag_initial requires runtime_injection_mode=context_aware->start_only"
                )
            next_params["runtime_injection_mode"] = "start_only"
            runtime_injection_mode = "start_only"
        return next_params, warnings

    def _reset_flowark_only_params(self, params: dict[str, Any], defaults: dict[str, Any]) -> None:
        for key, fallback in (
            ("runtime_injection_mode", "context_aware"),
            ("knowledge_distillation_mode", "with_selection_rules"),
            ("knowledge_packaging_mode", "dsl_rule"),
            ("auto_knowledge_validate_mode", "static"),
            ("knowledge_reuse_digest_mode", "off"),
            ("auto_knowledge_cycle", True),
        ):
            params[key] = fallback

    def build_eval_command(self, params: dict[str, Any]) -> tuple[list[str], Path, dict[str, Any]]:
        defaults = get_eval_defaults(workspace_root=self.workspace_root)
        params, parameter_warnings = self.normalize_effective_params("eval", params)
        dataset_preset = normalize_dataset_preset(params.get("dataset_preset") or defaults.get("dataset_preset"))
        input_path = resolve_dataset_preset_path(
            workspace_root=self.workspace_root,
            dataset_preset=dataset_preset,
        )
        input_path_raw = str(params.get("input_path") or params.get("input") or "").strip()
        if not input_path.exists() and input_path_raw:
            input_path = Path(input_path_raw).expanduser().resolve()
        if not input_path.exists():
            raise ValueError(
                f"Dataset preset {dataset_preset!r} is not materialized at {input_path}; "
                "run scripts/fetch_artifact_data.py --benchmarks first"
            )
        out_dir = ensure_indexable_eval_out_dir(
            params.get("out_dir") or defaults.get("out_dir") or self._state_paths.evals_dir,
            workspace_root=self.workspace_root,
        )
        cmd = ["uv", "run", "python", "main.py", "eval", "run", "--input", str(input_path)]
        modes = params.get("modes")
        if isinstance(modes, list):
            mode_str = ",".join(str(x).strip() for x in modes if str(x).strip())
        else:
            default_modes = defaults.get("modes") or "flowark"
            if isinstance(default_modes, list):
                default_mode_str = ",".join(str(x).strip() for x in default_modes if str(x).strip())
            else:
                default_mode_str = str(default_modes or "").strip()
            mode_str = str(modes or default_mode_str).strip() or "flowark"
        cmd += ["--modes", mode_str]
        has_explicit_app_names = "app_names" in params and params.get("app_names") is not None
        raw_app_names = params.get("app_names") if has_explicit_app_names else defaults.get("app_names")
        if isinstance(raw_app_names, list):
            app_names = ",".join(str(item).strip() for item in raw_app_names if str(item).strip())
        else:
            app_names = str(raw_app_names or "").strip()
        if has_explicit_app_names or app_names:
            cmd += ["--app-names", app_names]
        cmd += ["--parallel", str(max(1, int(params.get("parallel") or defaults.get("parallel") or 1)))]
        cmd += [
            "--serialize-within-app",
            "on"
            if self._as_bool(
                params.get("serialize_within_app"),
                default=bool(defaults.get("serialize_within_app", True)),
            )
            else "off",
        ]
        cmd += ["--repeats", str(max(1, int(params.get("repeats") or defaults.get("repeats") or 1)))]
        cmd += ["--out-dir", str(out_dir)]
        agent_adapter = str(
            params.get("agent_adapter") or defaults.get("agent_adapter") or "opencode"
        ).strip() or "opencode"
        cmd += ["--agent-adapter", agent_adapter]
        runtime_backend_mode = "single"
        cmd += ["--runtime-backend-mode", "single"]
        if params.get("opencode_binary") or defaults.get("opencode_binary"):
            cmd += ["--opencode-binary", str(params.get("opencode_binary") or defaults.get("opencode_binary"))]
        opencode_model = str(params.get("opencode_model") or defaults.get("opencode_model") or "").strip()
        if opencode_model:
            cmd += ["--opencode-model", opencode_model]
        cmd += [
            "--opencode-provider",
            str(params.get("opencode_provider") or defaults.get("opencode_provider") or "anthropic"),
            "--opencode-after-tool-delivery",
            self._one_of(
                params.get("opencode_after_tool_delivery"),
                ["no_reply_context", "tool_output_append"],
                str(defaults.get("opencode_after_tool_delivery") or "no_reply_context"),
            ),
            "--opencode-bash-policy",
            self._one_of(
                params.get("opencode_bash_policy"),
                ["read_only_guarded"],
                str(defaults.get("opencode_bash_policy") or "read_only_guarded"),
            ),
            "--opencode-post-phase-mode",
            self._one_of(
                params.get("opencode_post_phase_mode"),
                ["plain_json_same_surface"],
                str(defaults.get("opencode_post_phase_mode") or "plain_json_same_surface"),
            ),
            "--opencode-structured-output",
            "on"
            if self._as_bool(
                params.get("opencode_structured_output"),
                default=bool(defaults.get("opencode_structured_output", True)),
            )
            else "off",
        ]

        for key, cli in (("max_cases", "--max-cases"), ("max_apps", "--max-apps"), ("max_sources", "--max-sources")):
            value = params.get(key)
            if value not in {None, ""}:
                cmd += [cli, str(int(value))]
        cmd += [
            "--timeout-seconds",
            str(
                self._safe_positive_int(
                    params.get("timeout_seconds"),
                    default=int(defaults.get("timeout_seconds") or 900),
                )
            ),
        ]

        classification_filter = self._normalize_classification_filter_cli(
            params.get("classification_filter") or defaults.get("classification_filter")
        )
        if classification_filter:
            cmd += ["--classification-filter", classification_filter]

        cmd += [
            "--dummy-run",
            "on" if self._as_bool(params.get("dummy_run"), default=bool(defaults.get("dummy_run", False))) else "off",
        ]
        cmd += [
            "--knowledge-mode",
            self._one_of(
                params.get("knowledge_mode"),
                ["off", "cold", "warm"],
                str(defaults.get("knowledge_mode") or "warm"),
            ),
        ]
        cmd += [
            "--knowledge-allow-repeat-injection-within-session",
            "on"
            if self._as_bool(
                params.get("knowledge_allow_repeat_injection_within_session"),
                default=bool(defaults.get("knowledge_allow_repeat_injection_within_session", True)),
            )
            else "off",
        ]
        cmd += [
            "--knowledge-repeat-summary-react-gap",
            str(
                self._safe_nonnegative_int(
                    params.get("knowledge_repeat_summary_react_gap"),
                    default=self._safe_nonnegative_int(
                        defaults.get("knowledge_repeat_summary_react_gap"),
                        default=0,
                    ),
                )
            ),
        ]
        cmd += [
            "--knowledge-repeat-full-react-gap",
            str(
                self._safe_nonnegative_int(
                    params.get("knowledge_repeat_full_react_gap"),
                    default=self._safe_nonnegative_int(
                        defaults.get("knowledge_repeat_full_react_gap"),
                        default=1,
                    ),
                )
            ),
        ]
        cmd += [
            "--auto-knowledge-cycle",
            "on"
            if self._as_bool(
                params.get("auto_knowledge_cycle"),
                default=bool(defaults.get("auto_knowledge_cycle", True)),
            )
            else "off",
        ]
        cmd += [
            "--runtime-injection-mode",
            self._one_of(
                params.get("runtime_injection_mode"),
                ["context_aware", "start_only"],
                str(defaults.get("runtime_injection_mode") or "context_aware"),
            ),
        ]
        cmd += [
            "--knowledge-distillation-mode",
            str(params.get("knowledge_distillation_mode") or "with_selection_rules"),
        ]
        cmd += [
            "--knowledge-packaging-mode",
            str(params.get("knowledge_packaging_mode") or "dsl_rule"),
        ]
        cmd += [
            "--auto-knowledge-validate-mode",
            str(params.get("auto_knowledge_validate_mode") or "static"),
        ]
        cmd += [
            "--knowledge-reuse-digest-mode",
            str(params.get("knowledge_reuse_digest_mode") or "off"),
        ]
        cmd += [
            "--knowledge-recall-top-m",
            str(
                self._safe_positive_int(
                    params.get("knowledge_recall_top_m"),
                    default=int(defaults.get("knowledge_recall_top_m") or 8),
                )
            ),
        ]
        cmd += [
            "--knowledge-top-k",
            str(
                self._safe_positive_int(
                    params.get("knowledge_top_k"),
                    default=int(defaults.get("knowledge_top_k") or 3),
                )
            ),
        ]
        cmd += ["--llm-judge", "off"]
        meta = {
            "out_dir": str(out_dir),
            "input_path": str(input_path),
            "dataset_preset": dataset_preset,
            "modes": mode_str,
            "agent_adapter": agent_adapter,
            "runtime_backend_mode": runtime_backend_mode,
        }
        if parameter_warnings:
            meta["parameter_warnings"] = parameter_warnings
        return cmd, self.workspace_root, meta

    @staticmethod
    def _one_of(value: Any, choices: list[str], default: str) -> str:
        s = str(value or "").strip().lower()
        return s if s in set(choices) else default

    @staticmethod
    def _modes_include_flowark(value: Any, default: Any) -> bool:
        raw = default if value is None or value == "" else value
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, tuple):
            items = list(raw)
        else:
            items = str(raw or "").split(",")
        normalized = {
            str(item or "").strip().lower()
            for item in items
            if str(item or "").strip()
        }
        return "flowark" in normalized

    def _knowledge_mode_values(
        self,
        params: dict[str, Any],
        defaults: dict[str, Any],
    ) -> tuple[str, str, str, str, list[str]]:
        distillation_mode = self._one_of(
            params.get("knowledge_distillation_mode"),
            ["with_selection_rules", "generic"],
            str(defaults.get("knowledge_distillation_mode") or "with_selection_rules"),
        )
        packaging_mode = self._one_of(
            params.get("knowledge_packaging_mode"),
            ["dsl_rule", "embedding", "analysis_log_rag", "analysis_log_rag_initial"],
            str(defaults.get("knowledge_packaging_mode") or "dsl_rule"),
        )
        validate_mode = self._entry_auto_knowledge_validate_mode(
            params.get("auto_knowledge_validate_mode")
            if "auto_knowledge_validate_mode" in params
            else defaults.get("auto_knowledge_validate_mode")
        )
        digest_mode = self._one_of(
            params.get("knowledge_reuse_digest_mode"),
            ["off", "live_corridor", "live_corridor_v2"],
            str(defaults.get("knowledge_reuse_digest_mode") or "off"),
        )
        warnings: list[str] = []
        if distillation_mode == "generic" and packaging_mode == "embedding":
            warnings.append(
                "knowledge_distillation_mode=generic does not support "
                "knowledge_packaging_mode=embedding; "
                "knowledge_packaging_mode=embedding->dsl_rule"
            )
            packaging_mode = "dsl_rule"
        if distillation_mode == "generic":
            overridden: list[str] = []
            if validate_mode != "off":
                overridden.append(f"auto_knowledge_validate_mode={validate_mode}->off")
            if digest_mode != "off":
                overridden.append(f"knowledge_reuse_digest_mode={digest_mode}->off")
            if overridden:
                warnings.append(
                    "knowledge_distillation_mode=generic disables "
                    + ", ".join(overridden)
                )
            validate_mode = "off"
            digest_mode = "off"
        if packaging_mode == "embedding" and validate_mode != "off":
            warnings.append(
                f"knowledge_packaging_mode=embedding disables auto_knowledge_validate_mode={validate_mode}->off"
            )
            validate_mode = "off"
        if packaging_mode in {"analysis_log_rag", "analysis_log_rag_initial"}:
            overridden: list[str] = []
            if validate_mode != "off":
                overridden.append(f"auto_knowledge_validate_mode={validate_mode}->off")
            if digest_mode != "off":
                overridden.append(f"knowledge_reuse_digest_mode={digest_mode}->off")
            if overridden:
                warnings.append(
                    f"knowledge_packaging_mode={packaging_mode} disables "
                    + ", ".join(overridden)
                )
            validate_mode = "off"
            digest_mode = "off"
        return distillation_mode, packaging_mode, validate_mode, digest_mode, warnings

    @staticmethod
    def _entry_auto_knowledge_validate_mode(value: Any) -> str:
        mode = str(value or "static").strip().lower()
        if mode == "full":
            raise ValueError("auto_knowledge_validate_mode=full is deprecated and no longer supported; use static or off")
        if mode not in {"off", "static"}:
            raise ValueError("auto_knowledge_validate_mode must be off or static")
        return mode

    @classmethod
    def _validate_entry_auto_knowledge_validate_param(cls, value: Any) -> None:
        if value in {None, ""}:
            return
        cls._entry_auto_knowledge_validate_mode(value)

    @staticmethod
    def _reject_removed_auto_validate_repair_param(params: dict[str, Any]) -> None:
        if "auto_knowledge_validate_repair_mode" in params:
            raise ValueError("auto_knowledge_validate_repair_mode was removed with full validation")

    @staticmethod
    def _as_bool(value: Any, *, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in {"1", "true", "yes", "on"}:
            return True
        if s in {"0", "false", "no", "off"}:
            return False
        return default

    @staticmethod
    def _safe_positive_int(value: Any, *, default: int) -> int:
        try:
            parsed = int(value)
            if parsed > 0:
                return parsed
        except Exception:
            pass
        return default

    @staticmethod
    def _safe_nonnegative_int(value: Any, *, default: int) -> int:
        if value in {None, ""}:
            return max(0, int(default))
        try:
            return max(0, int(value))
        except Exception:
            return max(0, int(default))

    @staticmethod
    def _normalize_classification_filter_cli(value: Any) -> str:
        raw = str(value or "").strip().lower()
        if not raw:
            return "all"
        aliases = {
            "source_true_only": "source_has_true_flow",
            "true_source_only": "source_has_true_flow",
            "has_true_flow": "source_has_true_flow",
            "only_true_flow_sources": "source_has_true_flow",
            "true": "source_has_true_flow",
            "false": "all",
        }
        normalized = aliases.get(raw, raw)
        if normalized in {"all", "source_has_true_flow"}:
            return normalized
        return "all"
