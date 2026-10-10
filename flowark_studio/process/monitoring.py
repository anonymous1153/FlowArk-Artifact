from pathlib import Path
from typing import Any

from flowark_studio.common.models import StudioTask


class StudioProcessMonitoring:
    def __init__(self) -> None:
        pass

    @staticmethod
    def _normalize_eval_modes(value: Any) -> list[str]:
        raw_items = value if isinstance(value, list) else str(value or "").split(",")
        modes: list[str] = []
        seen: set[str] = set()
        for item in raw_items:
            text = str(item or "").strip().lower()
            if not text:
                continue
            if text == "native":
                text = "naive"
            if text in seen:
                continue
            seen.add(text)
            modes.append(text)
        return modes

    @staticmethod
    def _as_bool(value: Any, *, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return default

    def _infer_experiment_preset(self, params: dict[str, Any], modes: list[str]) -> str:
        if "flowark" not in set(modes):
            return "naive"
        explicit = str(params.get("experiment_preset") or "").strip()
        if explicit and explicit != "custom":
            return explicit
        adapter = str(params.get("agent_adapter") or "opencode").strip().lower().replace("-", "_")
        knowledge_mode = str(params.get("knowledge_mode") or "warm").strip().lower()
        cycle = self._as_bool(params.get("auto_knowledge_cycle"), default=True)
        runtime_mode = str(params.get("runtime_injection_mode") or "context_aware").strip().lower()
        distill_mode = str(params.get("knowledge_distillation_mode") or "with_selection_rules").strip().lower()
        packaging_mode = str(params.get("knowledge_packaging_mode") or "dsl_rule").strip().lower()
        validate_mode = str(params.get("auto_knowledge_validate_mode") or "static").strip().lower()
        digest_mode = str(params.get("knowledge_reuse_digest_mode") or "live_corridor_v2").strip().lower()
        if adapter != "opencode" or knowledge_mode != "warm":
            return "custom"
        if (
            distill_mode == "with_selection_rules"
            and packaging_mode == "dsl_rule"
            and runtime_mode == "context_aware"
            and cycle
            and validate_mode == "static"
            and digest_mode == "live_corridor_v2"
        ):
            return "flowark_full"
        if (
            distill_mode == "generic"
            and packaging_mode == "dsl_rule"
            and runtime_mode == "context_aware"
            and cycle
            and validate_mode == "off"
            and digest_mode == "off"
        ):
            return "m1_generic"
        if (
            distill_mode == "with_selection_rules"
            and packaging_mode == "embedding"
            and runtime_mode == "context_aware"
            and cycle
            and validate_mode == "off"
            and digest_mode == "live_corridor_v2"
        ):
            return "m2_embedding"
        if (
            distill_mode == "with_selection_rules"
            and packaging_mode == "dsl_rule"
            and runtime_mode == "start_only"
            and cycle
            and validate_mode == "static"
            and digest_mode == "live_corridor_v2"
        ):
            return "m3_start_only"
        if (
            distill_mode == "with_selection_rules"
            and packaging_mode == "analysis_log_rag"
            and runtime_mode == "context_aware"
            and not cycle
            and validate_mode == "off"
            and digest_mode == "off"
        ):
            return "analysis_log_rag"
        if (
            distill_mode == "with_selection_rules"
            and packaging_mode == "analysis_log_rag_initial"
            and runtime_mode == "start_only"
            and not cycle
            and validate_mode == "off"
            and digest_mode == "off"
        ):
            return "analysis_log_rag_initial"
        return "custom"

    @staticmethod
    def _normalize_eval_validate_mode(value: Any) -> str:
        raw = str(value or "").strip().lower()
        if not raw:
            return "static"
        mode = raw
        if mode not in {"off", "static"}:
            return ""
        return mode

    def _build_eval_task_label(self, task: StudioTask) -> str:
        params = task.params or {}
        modes = self._normalize_eval_modes(params.get("modes"))
        mode_text = "+".join(modes) if modes else "unknown"
        preset_text = self._infer_experiment_preset(params, modes)
        runtime_text = ""
        distill_text = ""
        packaging_text = ""
        validate_text = ""
        digest_text = ""
        if "flowark" in set(modes):
            runtime_mode = str(params.get("runtime_injection_mode") or "context_aware").strip() or "context_aware"
            runtime_text = f"runtime={runtime_mode}"
            distill_mode = str(
                params.get("knowledge_distillation_mode") or "with_selection_rules"
            ).strip() or "with_selection_rules"
            distill_text = f"distill={distill_mode}"
            packaging_mode = str(
                params.get("knowledge_packaging_mode") or "dsl_rule"
            ).strip() or "dsl_rule"
            packaging_text = f"packaging={packaging_mode}"
            validate_mode = (
                "off"
                if distill_mode == "generic" or packaging_mode in {"embedding", "analysis_log_rag", "analysis_log_rag_initial"}
                else self._normalize_eval_validate_mode(params.get("auto_knowledge_validate_mode"))
            )
            validate_text = f"validate={validate_mode}" if validate_mode else ""
            digest_mode = (
                "off"
                if distill_mode == "generic" or packaging_mode in {"analysis_log_rag", "analysis_log_rag_initial"}
                else str(params.get("knowledge_reuse_digest_mode") or "off").strip() or "off"
            )
            digest_text = f"digest={digest_mode}"
        eval_root = str(
            task.paths.get("eval_root")
            or task.paths.get("eval_dir")
            or task.metadata.get("eval_dir")
            or ""
        ).strip()
        eval_label = Path(eval_root).name if eval_root else ""
        input_path = str(params.get("input_path") or params.get("input") or "").strip()
        input_name = Path(input_path).name if input_path else ""
        suffix = eval_label or input_name or str(task.created_at or "").strip() or task.task_id
        parts = [preset_text, mode_text]
        if runtime_text:
            parts.append(runtime_text)
        if distill_text:
            parts.append(distill_text)
        if packaging_text:
            parts.append(packaging_text)
        if validate_text:
            parts.append(validate_text)
        if digest_text:
            parts.append(digest_text)
        parts.append(suffix)
        return "eval · " + " · ".join(part for part in parts if part)
