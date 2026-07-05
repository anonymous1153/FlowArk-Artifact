"""FlowArk CLI 主入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

try:
    import yaml
except Exception:  # pragma: no cover - 兼容最小环境
    yaml = None  # type: ignore[assignment]

from flowark.anthropic_env import (
    STUDIO_BACKEND_PROFILE_ENV,
    STUDIO_RUNTIME_AUTH_TOKEN_ENV,
    STUDIO_RUNTIME_BASE_URL_ENV,
    STUDIO_RUNTIME_MODEL_ENV,
)
from flowark.backend_transport import (
    INTERNAL_BACKEND_TRANSPORT_ACTIVE_ENV,
    INTERNAL_LLM_JUDGE_API_KEY_ENV,
    INTERNAL_LLM_JUDGE_BASE_URL_ENV,
    INTERNAL_LLM_JUDGE_MAX_RETRIES_ENV,
    INTERNAL_LLM_JUDGE_MODEL_ENV,
    INTERNAL_LLM_JUDGE_TIMEOUT_SECONDS_ENV,
    INTERNAL_REUSE_EMBED_API_KEY_ENV,
    INTERNAL_REUSE_EMBED_BASE_URL_ENV,
    INTERNAL_REUSE_EMBED_MODEL_ENV,
    INTERNAL_REUSE_EMBED_VERIFY_SSL_ENV,
    INTERNAL_REUSE_RERANK_API_KEY_ENV,
    INTERNAL_REUSE_RERANK_BASE_URL_ENV,
    INTERNAL_REUSE_RERANK_MODEL_ENV,
    INTERNAL_REUSE_RERANK_TIMEOUT_SECONDS_ENV,
)
from flowark.adapters.opencode import (
    DEFAULT_AFTER_TOOL_DELIVERY,
    DEFAULT_BASH_POLICY,
    DEFAULT_OPENCODE_PROVIDER,
    DEFAULT_POST_PHASE_MODE,
    DEFAULT_REAL_SMOKE_DELIVERIES,
)
from flowark.config import (
    load_eval_config_defaults,
    load_runtime_config_defaults,
    resolve_eval_env_config,
    resolve_repo_env_config,
    resolve_run_env_config,
)
from flowark.knowledge.artifact_audit import (
    audit_note_only_artifacts,
    compact_artifact_audit_result,
    write_note_only_artifact_audit,
)
from flowark.knowledge.pipeline import (
    KnowledgeStore,
    KnowledgeSynthesizer,
    KnowledgeValidator,
    load_candidates,
    lint_skills_dir,
    migrate_skills_to_archive,
    save_candidates,
    save_validation_results,
)
from flowark.state_paths import (
    format_legacy_state_dirs_warning,
    get_workspace_state_paths,
    resolve_eval_config_out_dir,
    resolve_run_config_out_dir,
)
from flowark.runtime.config import (
    KNOWLEDGE_DISTILLATION_GENERIC,
    AnalysisRequest,
    RunConfig,
    normalize_knowledge_distillation_mode,
    normalize_knowledge_reuse_digest_mode,
    normalize_knowledge_runtime_modes,
    normalize_runtime_injection_mode,
)
from flowark.knowledge_packaging import (
    is_analysis_log_rag_packaging_mode,
    normalize_knowledge_packaging_mode,
)
from flowark.types import ValidationResult, knowledge_candidate_from_dict

PUBLIC_OPENCODE_PROVIDERS = ("anthropic", "openai")


ENTRY_AUTO_KNOWLEDGE_VALIDATE_MODES = {"off", "static"}


def _parse_entry_auto_knowledge_validate_mode(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode == "full":
        raise argparse.ArgumentTypeError(
            "auto_knowledge_validate_mode=full 已废弃且不再支持，请改用 static 或 off"
        )
    if mode not in ENTRY_AUTO_KNOWLEDGE_VALIDATE_MODES:
        raise argparse.ArgumentTypeError("自动知识验证模式必须是 off 或 static")
    return mode


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器。"""
    workspace_root = Path(__file__).parent.resolve()
    state_paths = get_workspace_state_paths(workspace_root)
    parser = argparse.ArgumentParser(description="FlowArk - OpenCode-based data-flow analysis")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="运行一次数据流分析")
    run_parser.add_argument("--query", help="自然语言分析任务描述")
    run_parser.add_argument("--source", help="source 描述（可选）")
    run_parser.add_argument("--app-name", help="应用名称（可选，用于 app 级知识作用域过滤）")
    run_parser.add_argument(
        "--sink-types",
        default="",
        help="sink 类型列表（逗号分隔），如 network,log,file",
    )
    run_parser.add_argument(
        "--cwd",
        default=str(Path.cwd()),
        help="被分析项目路径（默认当前目录）",
    )
    run_parser.add_argument(
        "--out-dir",
        default=None,
        help="运行产物输出目录（默认读取 flowark/config/runtime.yaml，再回退到外部状态根 runs/）",
    )
    run_parser.add_argument(
        "--agent-adapter",
        choices=["opencode"],
        default="opencode",
        help=argparse.SUPPRESS,
    )
    run_parser.add_argument(
        "--runtime-backend-mode",
        choices=["single"],
        default=None,
        help="Runtime 后端选择模式（公开版仅支持 single）",
    )
    run_parser.add_argument("--opencode-binary", default=None, help="OpenCode CLI 路径或命令（默认优先 PATH，再回退 npx pin）")
    run_parser.add_argument("--opencode-model", default=None, help="OpenCode 模型；未传时读取 backend/profile/env 配置")
    run_parser.add_argument(
        "--opencode-provider",
        choices=PUBLIC_OPENCODE_PROVIDERS,
        default=None,
        help=f"OpenCode provider（公开版仅支持 anthropic/openai，默认 {DEFAULT_OPENCODE_PROVIDER}）",
    )
    run_parser.add_argument(
        "--opencode-after-tool-delivery",
        choices=["no_reply_context", "tool_output_append"],
        default=None,
        help=f"OpenCode PostToolUse 注入策略（默认 {DEFAULT_AFTER_TOOL_DELIVERY}）",
    )
    run_parser.add_argument(
        "--opencode-bash-policy",
        choices=["read_only_guarded"],
        default=None,
        help=f"OpenCode Bash 工具策略（默认 {DEFAULT_BASH_POLICY}）",
    )
    run_parser.add_argument(
        "--opencode-post-phase-mode",
        choices=[DEFAULT_POST_PHASE_MODE],
        default=None,
        help=f"OpenCode post phase JSON 模式（默认 {DEFAULT_POST_PHASE_MODE}）",
    )
    run_parser.add_argument(
        "--opencode-structured-output",
        nargs="?",
        const="on",
        choices=["on", "off"],
        default=None,
        help="legacy 开关：OpenCode post phase 现默认走 FlowArk plain JSON parser，不再启用 native StructuredOutput",
    )
    run_parser.add_argument(
        "--enable-mcp",
        nargs="?",
        const="on",
        choices=["on", "off"],
        default=None,
        help=argparse.SUPPRESS,
    )
    run_parser.add_argument(
        "--agent-mode",
        choices=["flowark", "naive"],
        default=None,
        help="agent 运行模式：flowark=增强模式，naive=朴素基线模式（单轮，无知识/额外 fork）（默认 flowark）",
    )
    run_parser.add_argument(
        "--knowledge-mode",
        choices=["off", "cold", "warm"],
        default=None,
        help="知识库模式：off/cold/warm（默认 warm）",
    )
    run_parser.add_argument(
        "--knowledge-allow-repeat-injection-within-session",
        choices=["on", "off"],
        default=None,
        help="是否允许同一知识在同一会话不同阶段重复注入（默认 on）",
    )
    run_parser.add_argument(
        "--knowledge-repeat-summary-react-gap",
        type=int,
        default=None,
        help="重复命中同一知识时，summary 重新注入需间隔的完整 ReAct turn 数（默认 0）",
    )
    run_parser.add_argument(
        "--knowledge-repeat-full-react-gap",
        type=int,
        default=None,
        help="重复命中同一知识时，full 重新注入需间隔的完整 ReAct turn 数（默认 1）",
    )
    opencode_smoke = subparsers.add_parser("opencode-smoke", help="验证 OpenCode server/session 隔离与基础观测")
    opencode_smoke.add_argument(
        "--cwd",
        default=str(Path.cwd()),
        help="OpenCode smoke 的工作目录（默认当前目录）",
    )
    opencode_smoke.add_argument(
        "--out-dir",
        default=None,
        help="smoke 产物输出目录（默认读取 runtime.yaml，再回退到外部状态根 runs/opencode-smoke）",
    )
    opencode_smoke.add_argument("--opencode-binary", default=None, help="OpenCode CLI 路径或命令")
    opencode_smoke.add_argument("--opencode-model", default=None, help="OpenCode 模型；未传时读取 backend/profile/env 配置")
    opencode_smoke.add_argument(
        "--opencode-provider",
        choices=PUBLIC_OPENCODE_PROVIDERS,
        default=None,
        help=f"OpenCode provider（公开版仅支持 anthropic/openai，默认 {DEFAULT_OPENCODE_PROVIDER}）",
    )
    opencode_smoke.add_argument(
        "--opencode-after-tool-delivery",
        choices=["no_reply_context", "tool_output_append"],
        default=None,
        help=f"OpenCode PostToolUse 策略（默认 {DEFAULT_AFTER_TOOL_DELIVERY}，smoke 不激活注入）",
    )
    opencode_smoke.add_argument(
        "--opencode-bash-policy",
        choices=["read_only_guarded"],
        default=None,
        help=f"OpenCode Bash 工具策略（默认 {DEFAULT_BASH_POLICY}）",
    )
    opencode_smoke.add_argument(
        "--send-prompt",
        action="store_true",
        help="发送一个最小模型 prompt；默认只启动 server 并创建 session，不调用 LLM",
    )
    opencode_smoke.add_argument(
        "--prompt",
        default="Summarize the current working directory in one sentence.",
        help="--send-prompt 时使用的 smoke prompt",
    )

    opencode_real_smoke = subparsers.add_parser(
        "opencode-real-smoke",
        help="运行 OpenCode warm reuse 真实 smoke，并对比 noReply/tool-output delivery",
    )
    opencode_real_smoke.add_argument(
        "--out-dir",
        default=None,
        help="real smoke 输出根目录（默认读取 runtime.yaml，再回退到外部状态根 runs/）",
    )
    opencode_real_smoke.add_argument("--opencode-binary", default=None, help="OpenCode CLI 路径或命令")
    opencode_real_smoke.add_argument("--opencode-model", default=None, help="OpenCode 模型；未传时读取 backend/profile/env 配置")
    opencode_real_smoke.add_argument(
        "--opencode-provider",
        choices=PUBLIC_OPENCODE_PROVIDERS,
        default=None,
        help=f"OpenCode provider（公开版仅支持 anthropic/openai，默认 {DEFAULT_OPENCODE_PROVIDER}）",
    )
    opencode_real_smoke.add_argument(
        "--delivery",
        action="append",
        choices=list(DEFAULT_REAL_SMOKE_DELIVERIES),
        default=None,
        help="要测试的 PostToolUse delivery；可重复传入，默认两种都跑",
    )
    opencode_real_smoke.add_argument(
        "--opencode-bash-policy",
        choices=["read_only_guarded"],
        default=None,
        help=f"OpenCode Bash 工具策略（默认 {DEFAULT_BASH_POLICY}）",
    )
    opencode_real_smoke.add_argument(
        "--opencode-post-phase-mode",
        choices=[DEFAULT_POST_PHASE_MODE],
        default=None,
        help=f"OpenCode post phase JSON 模式（默认 {DEFAULT_POST_PHASE_MODE}）",
    )
    opencode_real_smoke.add_argument(
        "--opencode-structured-output",
        nargs="?",
        const="on",
        choices=["on", "off"],
        default=None,
        help="legacy 开关：real smoke 现默认验证 plain JSON parser，不再启用 native StructuredOutput",
    )
    run_parser.add_argument(
        "--auto-knowledge-cycle",
        choices=["on", "off"],
        default=None,
        help="run 完成后是否自动执行 synth->validate->apply（默认 on）",
    )
    run_parser.add_argument(
        "--runtime-injection-mode",
        choices=["context_aware", "start_only"],
        default=None,
        help="运行时知识注入模式：context_aware=初始+工具后注入，start_only=仅初始注入（默认 context_aware）",
    )
    run_parser.add_argument(
        "--knowledge-distillation-mode",
        choices=["with_selection_rules", "generic"],
        default=None,
        help="知识提炼策略：with_selection_rules=当前 FlowArk 规则约束，generic=通用知识提取消融（默认 with_selection_rules）",
    )
    run_parser.add_argument(
        "--knowledge-packaging-mode",
        choices=["dsl_rule", "embedding", "analysis_log_rag", "analysis_log_rag_initial"],
        default=None,
        help="知识封装/召回策略：dsl_rule=DSL 规则封装与召回，embedding=模块二 embedding 召回消融，analysis_log_rag=上下文感知分析日志 RAG，analysis_log_rag_initial=仅初始召回的分析日志 RAG（默认 dsl_rule）",
    )
    run_parser.add_argument(
        "--auto-knowledge-validate-mode",
        type=_parse_entry_auto_knowledge_validate_mode,
        default=None,
        help="自动知识循环的验证模式：off=不验证，static=仅静态验证（flowark 模式必须显式提供）",
    )
    run_parser.add_argument(
        "--knowledge-reuse-digest-mode",
        choices=["off", "live_corridor", "live_corridor_v2"],
        default=None,
        help="知识总结前的热点链路提示模式：off/live_corridor/live_corridor_v2（默认 off）",
    )
    run_parser.add_argument(
        "--knowledge-reuse-digest-dir",
        default=None,
        help=argparse.SUPPRESS,
    )
    run_parser.add_argument(
        "--eval-summary-json",
        choices=["on", "off"],
        default=None,
        help=argparse.SUPPRESS,
    )
    run_parser.add_argument(
        "--knowledge-top-k",
        type=int,
        default=None,
        help="单次知识注入最终保留 note 数量 TopK（默认 3）",
    )
    run_parser.add_argument(
        "--knowledge-recall-top-m",
        type=int,
        default=None,
        help="知识匹配阶段的规则候选 note 数量 TopM（默认 8）",
    )
    run_parser.add_argument(
        "--skills-dir",
        help="知识 scope 的 skills/ 目录路径（agent-mode=flowark 时必填）",
    )
    run_parser.add_argument(
        "--interactive",
        action="store_true",
        help="若未提供 --query，则进入交互式输入",
    )

    kb_parser = subparsers.add_parser("kb", help="知识库工具")
    kb_subparsers = kb_parser.add_subparsers(dest="kb_command")

    kb_synth = kb_subparsers.add_parser("synth", help="从 run 产物生成知识候选")
    kb_synth.add_argument("--run-dir", required=True, help="run 产物目录")
    kb_synth.add_argument(
        "--out-dir",
        help="候选输出目录（默认 run-dir）",
    )

    kb_validate = kb_subparsers.add_parser("validate", help="验证知识候选")
    kb_validate.add_argument("--candidate", required=True, help="候选 JSON 文件路径")
    kb_validate.add_argument(
        "--cwd",
        default=str(Path.cwd()),
        help="用于验证代码证据的项目目录（默认当前目录）",
    )
    kb_validate.add_argument("--out", help="验证结果输出文件（默认同目录 knowledge_validation.json）")

    kb_apply = kb_subparsers.add_parser("apply", help="将 PASS 候选写入 skills 库")
    kb_apply.add_argument("--candidate", required=True, help="候选或验证结果 JSON 文件路径")
    kb_apply.add_argument("--skills-dir", required=True, help="知识 scope 的 skills/ 目录路径")
    kb_apply.add_argument(
        "--cwd",
        default=str(Path.cwd()),
        help="当输入为原始候选文件时，用于本地验证的项目目录",
    )

    kb_lint = kb_subparsers.add_parser("lint", help="扫描 skills 中的任务特异/链路型知识问题")
    kb_lint.add_argument("--skills-dir", required=True, help="知识 scope 的 skills/ 目录路径")
    kb_lint.add_argument("--out", help="输出 JSON 文件路径（默认仅打印摘要）")
    kb_lint.add_argument(
        "--max-frontmatter-chars",
        type=int,
        default=2400,
        help="frontmatter 字符数阈值（默认 2400）",
    )

    kb_migrate = kb_subparsers.add_parser("migrate", help="预览/执行 v4 note 到 v5 的迁移，并归档旧链路型知识")
    kb_migrate.add_argument("--skills-dir", required=True, help="知识 scope 的 skills/ 目录路径")
    kb_migrate.add_argument(
        "--archive-dir",
        help="归档目录（默认 skills 同级 skills_archived）",
    )
    kb_migrate.add_argument("--out", help="输出 JSON 文件路径（默认仅打印摘要）")
    kb_migrate.add_argument(
        "--max-frontmatter-chars",
        type=int,
        default=2400,
        help="frontmatter 字符数阈值（默认 2400）",
    )
    kb_migrate.add_argument(
        "--apply",
        action="store_true",
        help="实际执行迁移（默认 dry-run，仅预览）",
    )

    kb_audit = kb_subparsers.add_parser("audit-artifacts", help="检查 run/evaluation 产物是否符合 note-only 知识契约")
    kb_audit.add_argument("--path", required=True, help="run/evaluation/knowledge_scope 产物根目录")
    kb_audit.add_argument("--out", help="输出 JSON 文件路径（默认仅打印摘要）")
    kb_audit.add_argument(
        "--skip-skill-lint",
        action="store_true",
        help="跳过 mixed_note_jump_guidance 等 skill 内容质量 warning",
    )

    eval_parser = subparsers.add_parser("evaluation", help="批量评估框架（naive/flowark 模式对比）")
    eval_subparsers = eval_parser.add_subparsers(dest="eval_command")

    eval_run = eval_subparsers.add_parser("run", help="运行一轮批量评估")
    eval_run.add_argument("--input", required=True, help="评估样本 JSON/JSONL 文件或目录路径")
    eval_run.add_argument(
        "--modes",
        default=None,
        help="评估模式列表（逗号分隔），支持 naive/native,flowark（默认 naive,flowark）",
    )
    eval_run.add_argument(
        "--parallel",
        type=int,
        default=None,
        help="并发任务数（默认 2）",
    )
    eval_run.add_argument(
        "--serialize-within-app",
        choices=["on", "off"],
        default=None,
        help="是否限制同一 app 的任务串行执行（默认 on；不同 app 仍可并发）",
    )
    eval_run.add_argument(
        "--repeats",
        type=int,
        default=None,
        help="每个样本每种模式重复次数（默认 1）",
    )
    eval_run.add_argument(
        "--max-cases",
        type=int,
        help="最多评估多少个样本（调试用）",
    )
    eval_run.add_argument(
        "--app-names",
        default=None,
        help="仅评估指定 app_name（逗号分隔，精确匹配，大小写不敏感）",
    )
    eval_run.add_argument(
        "--max-apps",
        type=int,
        help="最多评估多少个 app（按输入顺序）",
    )
    eval_run.add_argument(
        "--max-sources",
        type=int,
        help="最多评估多少个 source（按输入顺序；优先于 --max-cases）",
    )
    eval_run.add_argument(
        "--classification-filter",
        choices=["all", "source_has_true_flow", "true", "false"],
        default=None,
        help="按 classification 过滤样本（默认 all；推荐 source_has_true_flow，true/false 为兼容旧值）",
    )
    eval_run.add_argument(
        "--dummy-run",
        nargs="?",
        const="on",
        choices=["on", "off"],
        default=None,
        help="不调用 agent，生成模拟产物（传 on/off；不带值等价于 on）",
    )
    eval_run.add_argument(
        "--out-dir",
        default=None,
        help="评估输出根目录（默认读取配置，再回退到外部状态根）",
    )
    eval_run.add_argument(
        "--agent-adapter",
        choices=["opencode"],
        default="opencode",
        help=argparse.SUPPRESS,
    )
    eval_run.add_argument(
        "--runtime-backend-mode",
        choices=["single"],
        default=None,
        help="Runtime 后端选择模式（公开版仅支持 single）",
    )
    eval_run.add_argument("--opencode-binary", default=None, help="传递给 run 子命令的 OpenCode CLI 路径或命令")
    eval_run.add_argument("--opencode-model", default=None, help="传递给 run 子命令的 OpenCode 模型；未传时读取 backend/profile/env 配置")
    eval_run.add_argument(
        "--opencode-provider",
        choices=PUBLIC_OPENCODE_PROVIDERS,
        default=None,
        help=f"传递给 run 子命令的 OpenCode provider（公开版仅支持 anthropic/openai，默认 {DEFAULT_OPENCODE_PROVIDER}）",
    )
    eval_run.add_argument(
        "--opencode-after-tool-delivery",
        choices=["no_reply_context", "tool_output_append"],
        default=None,
        help=f"传递给 run 子命令的 OpenCode PostToolUse 策略（默认 {DEFAULT_AFTER_TOOL_DELIVERY}）",
    )
    eval_run.add_argument(
        "--opencode-bash-policy",
        choices=["read_only_guarded"],
        default=None,
        help=f"传递给 run 子命令的 OpenCode Bash 工具策略（默认 {DEFAULT_BASH_POLICY}）",
    )
    eval_run.add_argument(
        "--opencode-post-phase-mode",
        choices=[DEFAULT_POST_PHASE_MODE],
        default=None,
        help=f"传递给 run 子命令的 OpenCode post phase JSON 模式（默认 {DEFAULT_POST_PHASE_MODE}）",
    )
    eval_run.add_argument(
        "--opencode-structured-output",
        nargs="?",
        const="on",
        choices=["on", "off"],
        default=None,
        help="legacy 开关：OpenCode post phase 现默认走 FlowArk plain JSON parser，不再启用 native StructuredOutput",
    )
    eval_run.add_argument(
        "--knowledge-mode",
        choices=["off", "cold", "warm"],
        default=None,
        help="传递给 flowark 模式的知识模式（naive 模式会自动忽略）",
    )
    eval_run.add_argument(
        "--knowledge-allow-repeat-injection-within-session",
        choices=["on", "off"],
        default=None,
        help="传递给 flowark 模式：是否允许同一知识在同一会话不同阶段重复注入（默认 on）",
    )
    eval_run.add_argument(
        "--knowledge-repeat-summary-react-gap",
        type=int,
        default=None,
        help="传递给 flowark 模式：summary 重新注入需间隔的完整 ReAct turn 数（默认 0）",
    )
    eval_run.add_argument(
        "--knowledge-repeat-full-react-gap",
        type=int,
        default=None,
        help="传递给 flowark 模式：full 重新注入需间隔的完整 ReAct turn 数（默认 1）",
    )
    eval_run.add_argument(
        "--auto-knowledge-cycle",
        choices=["on", "off"],
        default=None,
        help="传递给 flowark 模式：是否自动知识循环（naive 模式会自动忽略）",
    )
    eval_run.add_argument(
        "--runtime-injection-mode",
        choices=["context_aware", "start_only"],
        default=None,
        help="传递给 flowark 模式：运行时知识注入模式（默认 context_aware）",
    )
    eval_run.add_argument(
        "--knowledge-distillation-mode",
        choices=["with_selection_rules", "generic"],
        default=None,
        help="传递给 flowark 模式：知识提炼策略（默认 with_selection_rules）",
    )
    eval_run.add_argument(
        "--knowledge-packaging-mode",
        choices=["dsl_rule", "embedding", "analysis_log_rag", "analysis_log_rag_initial"],
        default=None,
        help="传递给 flowark 模式：知识封装/召回策略；analysis_log_rag=上下文感知分析日志 RAG，analysis_log_rag_initial=仅初始召回的分析日志 RAG（默认 dsl_rule）",
    )
    eval_run.add_argument(
        "--auto-knowledge-validate-mode",
        type=_parse_entry_auto_knowledge_validate_mode,
        default=None,
        help="传递给 flowark 模式：自动知识循环的验证模式 off/static（包含 flowark 时必须显式提供）",
    )
    eval_run.add_argument(
        "--knowledge-reuse-digest-mode",
        choices=["off", "live_corridor", "live_corridor_v2"],
        default=None,
        help="传递给 flowark 模式：知识总结前的热点链路提示模式（默认 off）",
    )
    eval_run.add_argument(
        "--knowledge-reuse-digest-dir",
        default=None,
        help=argparse.SUPPRESS,
    )
    eval_run.add_argument(
        "--eval-summary-json-flowark",
        choices=["on", "off"],
        default=None,
        help=argparse.SUPPRESS,
    )
    eval_run.add_argument(
        "--eval-summary-json-naive",
        choices=["on", "off"],
        default=None,
        help=argparse.SUPPRESS,
    )
    eval_run.add_argument("--eval-summary-json", choices=["on", "off"], help=argparse.SUPPRESS)
    eval_run.add_argument(
        "--knowledge-top-k",
        type=int,
        default=None,
        help="传递给 flowark 模式：单次知识注入最终保留 note 数量 TopK（默认 3）",
    )
    eval_run.add_argument(
        "--knowledge-recall-top-m",
        type=int,
        default=None,
        help="传递给 flowark 模式：知识匹配阶段的规则候选 note 数量 TopM（默认 8）",
    )
    eval_run.add_argument(
        "--timeout-seconds",
        type=int,
        help="单个样本单次运行超时秒数（默认 900，即 15 分钟）",
    )
    eval_run.add_argument(
        "--llm-judge",
        choices=["on", "off"],
        default=None,
        help="是否启用 LLM judge 进行最终结论判定（默认 on）",
    )
    eval_run.add_argument(
        "--llm-judge-model",
        default=None,
        help="LLM judge 使用的模型名（默认读取配置）",
    )
    eval_run.add_argument(
        "--llm-judge-timeout-seconds",
        type=int,
        default=None,
        help="单次 LLM judge 请求超时秒数（默认 120）",
    )
    eval_run.add_argument(
        "--llm-judge-max-retries",
        type=int,
        default=None,
        help="单次 LLM judge 最大重试次数（默认 2）",
    )
    eval_run.add_argument(
        "--enable-mcp",
        nargs="?",
        const="on",
        choices=["on", "off"],
        default=None,
        help=argparse.SUPPRESS,
    )

    eval_resume = eval_subparsers.add_parser("resume", help="恢复一轮已暂停的批量评估")
    eval_resume.add_argument("--evaluation-root", dest="eval_root", help="待恢复的评估目录")
    eval_resume.add_argument("--eval-root", dest="eval_root", help=argparse.SUPPRESS)

    return parser


def _prompt_query() -> str:
    query = input("请输入分析查询（或输入 'quit' 退出）: ").strip()
    if query.lower() in {"quit", "exit", "q"}:
        return ""
    return query


def _parse_sink_types(raw: str) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_modes(raw: str) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_csv_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _first_non_none(*values: object) -> object:
    for value in values:
        if value is not None:
            return value
    return None


def _resolve_choice(cli_value: str | None, config_value: object, default: str) -> str:
    text = str(_first_non_none(cli_value, config_value, default) or default).strip()
    return text or default


def _resolve_public_opencode_provider(cli_value: str | None, config_value: object) -> str:
    provider = _resolve_choice(cli_value, config_value, DEFAULT_OPENCODE_PROVIDER).strip().lower()
    if provider not in PUBLIC_OPENCODE_PROVIDERS:
        raise ValueError("公开版 CLI 仅支持 opencode_provider=anthropic 或 openai")
    return provider


def _resolve_cli_auto_knowledge_validate_mode(
    args: argparse.Namespace,
    *,
    context_label: str,
    require_explicit: bool,
) -> str:
    raw = getattr(args, "auto_knowledge_validate_mode", None)
    if raw is None:
        if require_explicit:
            raise ValueError(
                f"{context_label} 使用 flowark 时必须显式提供 "
                "--auto-knowledge-validate-mode off|static"
            )
        return "static"
    mode = str(raw or "").strip().lower()
    if mode == "full":
        raise ValueError(
            f"{context_label} 不再支持 auto_knowledge_validate_mode=full，"
            "请改用 static 或 off"
        )
    if mode not in ENTRY_AUTO_KNOWLEDGE_VALIDATE_MODES:
        raise ValueError(f"{context_label} 的自动知识验证模式必须是 off 或 static")
    return mode


def _resolve_knowledge_runtime_modes_for_cli(
    args: argparse.Namespace,
    cfg: dict[str, object],
    *,
    context_label: str,
    require_validate_mode: bool,
) -> tuple[str, str, str]:
    distillation_mode = normalize_knowledge_distillation_mode(
        _resolve_choice(
            getattr(args, "knowledge_distillation_mode", None),
            cfg.get("knowledge_distillation_mode"),
            "with_selection_rules",
        )
    )
    validate_mode = _resolve_cli_auto_knowledge_validate_mode(
        args,
        context_label=context_label,
        require_explicit=require_validate_mode,
    )
    digest_mode = normalize_knowledge_reuse_digest_mode(
        _resolve_choice(
            getattr(args, "knowledge_reuse_digest_mode", None),
            cfg.get("knowledge_reuse_digest_mode"),
            "off",
        )
    )
    if distillation_mode == KNOWLEDGE_DISTILLATION_GENERIC:
        overridden: list[str] = []
        if validate_mode != "off":
            overridden.append(f"auto_knowledge_validate_mode={validate_mode}->off")
        if digest_mode != "off":
            overridden.append(f"knowledge_reuse_digest_mode={digest_mode}->off")
        if overridden:
            print(
                f"[{context_label}] knowledge_distillation_mode=generic disables "
                + ", ".join(overridden),
                file=sys.stderr,
                flush=True,
            )
    return normalize_knowledge_runtime_modes(
        knowledge_distillation_mode=distillation_mode,
        auto_knowledge_validate_mode=validate_mode,
        knowledge_reuse_digest_mode=digest_mode,
    )


def _resolve_optional_text(cli_value: str | None, config_value: object) -> str | None:
    value = _first_non_none(cli_value, config_value)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resolve_toggle(cli_value: object, config_value: object, default: bool) -> bool:
    if isinstance(cli_value, bool):
        return cli_value
    if cli_value in {"on", "off"}:
        return cli_value == "on"
    if cli_value is not None:
        text = str(cli_value).strip().lower()
        if text in {"on", "true", "1", "yes"}:
            return True
        if text in {"off", "false", "0", "no"}:
            return False
    if isinstance(config_value, bool):
        return config_value
    if config_value is None:
        return default
    text = str(config_value).strip().lower()
    if text in {"on", "true", "1", "yes"}:
        return True
    if text in {"off", "false", "0", "no"}:
        return False
    return default


def _resolve_nonnegative_int(cli_value: object, config_value: object, *, default: int) -> int:
    value = _first_non_none(cli_value, config_value, default)
    if value in {None, ""}:
        return max(0, int(default))
    return max(0, int(value))


def _resolve_explicit_skills_dir(raw_value: object, *, context_label: str) -> Path:
    raw_text = str(raw_value or "").strip()
    if not raw_text:
        raise ValueError(f"{context_label}必须提供知识 scope 的 skills/ 目录路径（--skills-dir）")
    return Path(raw_text).expanduser().resolve()


def _env_text(key: str) -> str | None:
    value = str(os.environ.get(key) or "").strip()
    return value or None


def _env_json_object(key: str) -> dict[str, object]:
    value = _env_text(key)
    if value is None:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _env_json_list(key: str) -> list[str]:
    value = _env_text(key)
    if value is None:
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [str(item) for item in payload if str(item).strip()]


def _internal_transport_text(key: str) -> str | None:
    active = str(os.environ.get(INTERNAL_BACKEND_TRANSPORT_ACTIVE_ENV) or "").strip()
    if active != "1":
        return None
    return _env_text(key)


def _env_bool(key: str) -> bool | None:
    value = _env_text(key)
    if value is None:
        return None
    text = value.lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def _resolve_runtime_backend_mode_for_cli(
    *,
    workspace_root: Path,
    runtime_backend_mode: str | None,
) -> str:
    mode = str(runtime_backend_mode or "").strip().lower()
    if not mode or mode == "single":
        return "single"
    raise ValueError("公开版仅支持 runtime_backend_mode=single")


def _reject_removed_router_config(cfg: dict[str, object], *, context_label: str) -> None:
    legacy_prefix = "knowledge" + "_router_"
    removed = sorted(str(key) for key in cfg.keys() if str(key).startswith(legacy_prefix))
    if removed:
        raise ValueError(
            f"{context_label} 配置仍包含已移除的 {legacy_prefix}* 字段，请改用 "
            f"knowledge_recall_top_m / knowledge_top_k: {', '.join(removed)}"
        )


def _resolve_runtime_run_config(args: argparse.Namespace, *, workspace_root: Path) -> RunConfig:
    cfg = load_runtime_config_defaults(workspace_root)
    _reject_removed_router_config(cfg, context_label="run")
    env_cfg = resolve_run_env_config(workspace_root)
    if args.enable_mcp is not None:
        raise ValueError("参数 --enable-mcp 已废弃且不再支持，请移除该参数")
    reuse_embed_base_url = _internal_transport_text(INTERNAL_REUSE_EMBED_BASE_URL_ENV) or str(env_cfg.get("reuse_embed_base_url") or "").strip() or None
    reuse_embed_api_key = _internal_transport_text(INTERNAL_REUSE_EMBED_API_KEY_ENV) or str(env_cfg.get("reuse_embed_api_key") or "").strip() or None
    reuse_embed_model = _internal_transport_text(INTERNAL_REUSE_EMBED_MODEL_ENV) or str(env_cfg.get("reuse_embed_model") or "").strip() or None
    reuse_embed_verify_ssl = _env_bool(INTERNAL_REUSE_EMBED_VERIFY_SSL_ENV) if _env_text(INTERNAL_BACKEND_TRANSPORT_ACTIVE_ENV) == "1" else None
    if reuse_embed_verify_ssl is None:
        reuse_embed_verify_ssl = str(env_cfg.get("reuse_embed_verify_ssl") or "").strip().lower() in {"1", "true", "yes", "on"}
    reuse_rerank_base_url = _internal_transport_text(INTERNAL_REUSE_RERANK_BASE_URL_ENV) or str(env_cfg.get("reuse_rerank_base_url") or "").strip() or None
    reuse_rerank_api_key = _internal_transport_text(INTERNAL_REUSE_RERANK_API_KEY_ENV) or str(env_cfg.get("reuse_rerank_api_key") or "").strip() or None
    reuse_rerank_model = _internal_transport_text(INTERNAL_REUSE_RERANK_MODEL_ENV) or str(env_cfg.get("reuse_rerank_model") or "").strip() or None
    reuse_rerank_timeout_raw = _internal_transport_text(INTERNAL_REUSE_RERANK_TIMEOUT_SECONDS_ENV) or str(env_cfg.get("reuse_rerank_timeout_seconds") or "").strip()
    reuse_rerank_timeout_seconds = max(1, int(reuse_rerank_timeout_raw or 60))
    cwd_value = str(_first_non_none(args.cwd, str(Path.cwd())) or Path.cwd())
    out_dir_value = _first_non_none(args.out_dir, cfg.get("out_dir"))
    agent_mode = _resolve_choice(args.agent_mode, cfg.get("agent_mode"), "flowark").strip().lower()
    agent_mode = "naive" if agent_mode == "naive" else "flowark"
    skills_dir = None
    if agent_mode == "flowark":
        skills_dir = _resolve_explicit_skills_dir(args.skills_dir, context_label="flowark 模式")
    knowledge_packaging_mode = normalize_knowledge_packaging_mode(
        _resolve_choice(
            getattr(args, "knowledge_packaging_mode", None),
            cfg.get("knowledge_packaging_mode"),
            "dsl_rule",
        )
    )
    knowledge_distillation_mode, auto_knowledge_validate_mode, knowledge_reuse_digest_mode = (
        _resolve_knowledge_runtime_modes_for_cli(
            args,
            cfg,
            context_label="run",
            require_validate_mode=(
                agent_mode == "flowark"
                and not is_analysis_log_rag_packaging_mode(knowledge_packaging_mode)
            ),
        )
    )
    return RunConfig(
        cwd=Path(cwd_value).expanduser().resolve(),
        out_dir=resolve_run_config_out_dir(out_dir_value, workspace_root=workspace_root),
        agent_adapter="opencode",
        opencode_binary=_resolve_optional_text(args.opencode_binary, cfg.get("opencode_binary")),
        opencode_model=_resolve_optional_text(args.opencode_model, cfg.get("opencode_model")) or "",
        opencode_provider=_resolve_public_opencode_provider(args.opencode_provider, cfg.get("opencode_provider")),
        opencode_after_tool_delivery=_resolve_choice(
            args.opencode_after_tool_delivery,
            cfg.get("opencode_after_tool_delivery"),
            DEFAULT_AFTER_TOOL_DELIVERY,
        ),
        opencode_bash_policy=_resolve_choice(
            args.opencode_bash_policy,
            cfg.get("opencode_bash_policy"),
            DEFAULT_BASH_POLICY,
        ),
        opencode_post_phase_mode=_resolve_choice(
            args.opencode_post_phase_mode,
            cfg.get("opencode_post_phase_mode"),
            DEFAULT_POST_PHASE_MODE,
        ),
        opencode_structured_output=_resolve_toggle(
            args.opencode_structured_output,
            cfg.get("opencode_structured_output"),
            True,
        ),
        agent_mode=agent_mode,
        knowledge_mode=_resolve_choice(args.knowledge_mode, cfg.get("knowledge_mode"), "warm"),
        knowledge_allow_repeat_injection_within_session=_resolve_toggle(
            args.knowledge_allow_repeat_injection_within_session,
            cfg.get("knowledge_allow_repeat_injection_within_session"),
            True,
        ),
        auto_knowledge_cycle=_resolve_toggle(
            args.auto_knowledge_cycle,
            cfg.get("auto_knowledge_cycle"),
            True,
        ),
        runtime_injection_mode=normalize_runtime_injection_mode(
            _resolve_choice(
                getattr(args, "runtime_injection_mode", None),
                cfg.get("runtime_injection_mode"),
                "context_aware",
            )
        ),
        knowledge_distillation_mode=knowledge_distillation_mode,
        knowledge_packaging_mode=knowledge_packaging_mode,
        auto_knowledge_validate_mode=auto_knowledge_validate_mode,
        knowledge_reuse_digest_mode=knowledge_reuse_digest_mode,
        knowledge_top_k=max(
            1,
            int(
                _first_non_none(
                    args.knowledge_top_k,
                    cfg.get("knowledge_top_k"),
                    3,
                )
                or 3
            ),
        ),
        knowledge_recall_top_m=max(
            1,
            int(
                _first_non_none(
                    args.knowledge_recall_top_m,
                    cfg.get("knowledge_recall_top_m"),
                    8,
                )
                or 8
            ),
        ),
        knowledge_min_score=float(_first_non_none(cfg.get("knowledge_min_score"), 1.0) or 1.0),
        knowledge_injection_char_budget=max(
            256,
            int(_first_non_none(cfg.get("knowledge_injection_char_budget"), 4000) or 4000),
        ),
        knowledge_delta_char_budget=(
            int(cfg["knowledge_delta_char_budget"])
            if cfg.get("knowledge_delta_char_budget") not in {None, ""}
            else None
        ),
        knowledge_realtime_min_interval_ms=max(
            0,
            int(_first_non_none(cfg.get("knowledge_realtime_min_interval_ms"), 1500) or 1500),
        ),
        knowledge_repeat_summary_hook_gap=max(
            1,
            int(_first_non_none(cfg.get("knowledge_repeat_summary_hook_gap"), 3) or 3),
        ),
        knowledge_repeat_full_hook_gap=max(
            2,
            int(_first_non_none(cfg.get("knowledge_repeat_full_hook_gap"), 10) or 10),
        ),
        knowledge_repeat_summary_react_gap=_resolve_nonnegative_int(
            args.knowledge_repeat_summary_react_gap,
            cfg.get("knowledge_repeat_summary_react_gap"),
            default=0,
        ),
        knowledge_repeat_full_react_gap=_resolve_nonnegative_int(
            args.knowledge_repeat_full_react_gap,
            cfg.get("knowledge_repeat_full_react_gap"),
            default=1,
        ),
        reuse_embed_base_url=reuse_embed_base_url,
        reuse_embed_api_key=reuse_embed_api_key,
        reuse_embed_model=reuse_embed_model,
        reuse_embed_verify_ssl=bool(reuse_embed_verify_ssl),
        reuse_rerank_base_url=reuse_rerank_base_url,
        reuse_rerank_api_key=reuse_rerank_api_key,
        reuse_rerank_model=reuse_rerank_model,
        reuse_rerank_timeout_seconds=reuse_rerank_timeout_seconds,
        skills_dir=skills_dir,
    )


def _resolve_opencode_smoke_config(args: argparse.Namespace, *, workspace_root: Path) -> tuple[RunConfig, Path]:
    cfg = load_runtime_config_defaults(workspace_root)
    cwd_value = str(_first_non_none(args.cwd, str(Path.cwd())) or Path.cwd())
    out_dir_value = _first_non_none(args.out_dir, cfg.get("out_dir"))
    base_out_dir = resolve_run_config_out_dir(out_dir_value, workspace_root=workspace_root)
    run_dir = (base_out_dir / "opencode-smoke").expanduser().resolve()
    config = RunConfig(
        cwd=Path(cwd_value).expanduser().resolve(),
        out_dir=base_out_dir,
        agent_adapter="opencode",
        opencode_binary=_resolve_optional_text(args.opencode_binary, cfg.get("opencode_binary")),
        opencode_model=_resolve_optional_text(args.opencode_model, cfg.get("opencode_model")) or "",
        opencode_provider=_resolve_public_opencode_provider(args.opencode_provider, cfg.get("opencode_provider")),
        opencode_after_tool_delivery=_resolve_choice(
            args.opencode_after_tool_delivery,
            cfg.get("opencode_after_tool_delivery"),
            DEFAULT_AFTER_TOOL_DELIVERY,
        ),
        opencode_bash_policy=_resolve_choice(
            args.opencode_bash_policy,
            cfg.get("opencode_bash_policy"),
            DEFAULT_BASH_POLICY,
        ),
        opencode_structured_output=False,
        agent_mode="naive",
        knowledge_mode="off",
        auto_knowledge_cycle=False,
    )
    return config, run_dir


def _resolve_opencode_real_smoke_config(args: argparse.Namespace, *, workspace_root: Path) -> tuple[RunConfig, Path]:
    cfg = load_runtime_config_defaults(workspace_root)
    out_dir_value = _first_non_none(args.out_dir, cfg.get("out_dir"))
    base_out_dir = resolve_run_config_out_dir(out_dir_value, workspace_root=workspace_root)
    config = RunConfig(
        cwd=workspace_root,
        out_dir=base_out_dir,
        agent_adapter="opencode",
        opencode_binary=_resolve_optional_text(args.opencode_binary, cfg.get("opencode_binary")),
        opencode_model=_resolve_optional_text(args.opencode_model, cfg.get("opencode_model")) or "",
        opencode_provider=_resolve_public_opencode_provider(args.opencode_provider, cfg.get("opencode_provider")),
        opencode_after_tool_delivery=DEFAULT_AFTER_TOOL_DELIVERY,
        opencode_bash_policy=_resolve_choice(
            args.opencode_bash_policy,
            cfg.get("opencode_bash_policy"),
            DEFAULT_BASH_POLICY,
        ),
        opencode_post_phase_mode=_resolve_choice(
            args.opencode_post_phase_mode,
            cfg.get("opencode_post_phase_mode"),
            DEFAULT_POST_PHASE_MODE,
        ),
        opencode_structured_output=_resolve_toggle(
            args.opencode_structured_output,
            cfg.get("opencode_structured_output"),
            True,
        ),
        agent_mode="flowark",
        knowledge_mode="warm",
        auto_knowledge_cycle=False,
    )
    return config, base_out_dir


def _resolve_eval_config(args: argparse.Namespace, *, workspace_root: Path) -> tuple["EvaluationConfig", dict[str, object]]:
    from flowark.eval.harness import EvaluationConfig
    from flowark.eval.harness.models import (
        FLOWARK_STUDIO_EFFECTIVE_PARAMS_ENV,
        FLOWARK_STUDIO_NORMALIZATION_WARNINGS_ENV,
        FLOWARK_STUDIO_REQUESTED_PARAMS_ENV,
        validate_embedding_packaging_backend_config,
    )

    cfg = load_eval_config_defaults(workspace_root)
    _reject_removed_router_config(cfg, context_label="evaluation")
    env_cfg = resolve_eval_env_config(workspace_root)
    repo_env_cfg = resolve_repo_env_config(workspace_root)
    if args.enable_mcp is not None:
        raise ValueError("参数 --enable-mcp 已废弃且不再支持，请移除该参数")
    dummy_run_enabled = _resolve_toggle(
        args.dummy_run if hasattr(args, "dummy_run") else None,
        cfg.get("dummy_run"),
        False,
    )
    llm_judge_enabled = _resolve_toggle(
        args.llm_judge,
        cfg.get("llm_judge_enabled"),
        True,
    )
    llm_judge_base_url = _internal_transport_text(INTERNAL_LLM_JUDGE_BASE_URL_ENV) or str(env_cfg.get("llm_judge_base_url") or "").strip()
    llm_judge_api_key = _internal_transport_text(INTERNAL_LLM_JUDGE_API_KEY_ENV) or str(env_cfg.get("llm_judge_api_key") or "").strip()
    llm_judge_model = _resolve_optional_text(
        args.llm_judge_model if hasattr(args, "llm_judge_model") else None,
        _first_non_none(_internal_transport_text(INTERNAL_LLM_JUDGE_MODEL_ENV), env_cfg.get("llm_judge_model"), cfg.get("llm_judge_model")),
    ) or ""
    runtime_backend_marker = _env_text(STUDIO_BACKEND_PROFILE_ENV)
    if runtime_backend_marker:
        runtime_backend_base_url = _env_text(STUDIO_RUNTIME_BASE_URL_ENV) or None
        runtime_backend_auth_token = _env_text(STUDIO_RUNTIME_AUTH_TOKEN_ENV) or None
        runtime_backend_model = _env_text(STUDIO_RUNTIME_MODEL_ENV) or None
    else:
        runtime_backend_base_url = _env_text("ANTHROPIC_BASE_URL") or _env_text("OPENAI_BASE_URL") or repo_env_cfg.anthropic_base_url
        runtime_backend_auth_token = _env_text("ANTHROPIC_AUTH_TOKEN") or _env_text("OPENAI_API_KEY") or repo_env_cfg.anthropic_auth_token
        runtime_backend_model = _env_text("ANTHROPIC_MODEL") or _env_text("OPENAI_MODEL") or repo_env_cfg.anthropic_model
    reuse_embed_base_url = _internal_transport_text(INTERNAL_REUSE_EMBED_BASE_URL_ENV) or str(env_cfg.get("reuse_embed_base_url") or "").strip() or None
    reuse_embed_api_key = _internal_transport_text(INTERNAL_REUSE_EMBED_API_KEY_ENV) or str(env_cfg.get("reuse_embed_api_key") or "").strip() or None
    reuse_embed_model = _internal_transport_text(INTERNAL_REUSE_EMBED_MODEL_ENV) or str(env_cfg.get("reuse_embed_model") or "").strip() or None
    reuse_embed_verify_ssl = _env_bool(INTERNAL_REUSE_EMBED_VERIFY_SSL_ENV) if _env_text(INTERNAL_BACKEND_TRANSPORT_ACTIVE_ENV) == "1" else None
    if reuse_embed_verify_ssl is None:
        reuse_embed_verify_ssl = str(env_cfg.get("reuse_embed_verify_ssl") or "").strip().lower() in {"1", "true", "yes", "on"}
    reuse_rerank_base_url = _internal_transport_text(INTERNAL_REUSE_RERANK_BASE_URL_ENV) or str(env_cfg.get("reuse_rerank_base_url") or "").strip() or None
    reuse_rerank_api_key = _internal_transport_text(INTERNAL_REUSE_RERANK_API_KEY_ENV) or str(env_cfg.get("reuse_rerank_api_key") or "").strip() or None
    reuse_rerank_model = _internal_transport_text(INTERNAL_REUSE_RERANK_MODEL_ENV) or str(env_cfg.get("reuse_rerank_model") or "").strip() or None
    reuse_rerank_timeout_raw = _internal_transport_text(INTERNAL_REUSE_RERANK_TIMEOUT_SECONDS_ENV) or str(env_cfg.get("reuse_rerank_timeout_seconds") or "").strip()
    reuse_rerank_timeout_seconds = max(1, int(reuse_rerank_timeout_raw or 60))
    if llm_judge_enabled and not dummy_run_enabled:
        missing: list[str] = []
        if not llm_judge_base_url:
            missing.append("FLOWARK_LLM_JUDGE_BASE_URL")
        if not llm_judge_api_key:
            missing.append("FLOWARK_LLM_JUDGE_API_KEY")
        if not llm_judge_model:
            missing.append("FLOWARK_LLM_JUDGE_MODEL")
        if missing:
            raise ValueError("LLM judge 配置缺失，请在 .env 中补充字段: " + ", ".join(missing))

    if getattr(args, "app_names", None) is None:
        app_names = list(cfg.get("app_names") or [])
    else:
        app_names = _parse_csv_list(args.app_names)

    eval_modes = _parse_modes(
        str(
            _first_non_none(
                args.modes,
                ",".join(cfg.get("modes") or ["naive", "flowark"]),
                "naive,flowark",
            )
        )
    )
    knowledge_packaging_mode = normalize_knowledge_packaging_mode(
        _resolve_choice(
            getattr(args, "knowledge_packaging_mode", None),
            cfg.get("knowledge_packaging_mode"),
            "dsl_rule",
        )
    )
    knowledge_distillation_mode, auto_knowledge_validate_mode, knowledge_reuse_digest_mode = (
        _resolve_knowledge_runtime_modes_for_cli(
            args,
            cfg,
            context_label="evaluation run",
            require_validate_mode=(
                "flowark" in {str(mode).strip().lower() for mode in eval_modes}
                and not is_analysis_log_rag_packaging_mode(knowledge_packaging_mode)
            ),
        )
    )
    opencode_model = _resolve_optional_text(args.opencode_model, cfg.get("opencode_model")) or ""
    runtime_backend_mode = _resolve_runtime_backend_mode_for_cli(
        workspace_root=workspace_root,
        runtime_backend_mode=getattr(args, "runtime_backend_mode", None),
    )
    studio_requested_params = _env_json_object(FLOWARK_STUDIO_REQUESTED_PARAMS_ENV)
    studio_effective_params = _env_json_object(FLOWARK_STUDIO_EFFECTIVE_PARAMS_ENV)
    studio_normalization_warnings = _env_json_list(FLOWARK_STUDIO_NORMALIZATION_WARNINGS_ENV)

    config = EvaluationConfig(
        input_path=Path(str(_first_non_none(args.input, cfg.get("input_path"), ""))).expanduser().resolve(),
        out_dir=resolve_eval_config_out_dir(_first_non_none(args.out_dir, cfg.get("out_dir")), workspace_root=workspace_root),
        agent_adapter="opencode",
        opencode_binary=_resolve_optional_text(args.opencode_binary, cfg.get("opencode_binary")),
        opencode_model=opencode_model,
        opencode_provider=_resolve_public_opencode_provider(args.opencode_provider, cfg.get("opencode_provider")),
        opencode_after_tool_delivery=_resolve_choice(
            args.opencode_after_tool_delivery,
            cfg.get("opencode_after_tool_delivery"),
            DEFAULT_AFTER_TOOL_DELIVERY,
        ),
        opencode_bash_policy=_resolve_choice(
            args.opencode_bash_policy,
            cfg.get("opencode_bash_policy"),
            DEFAULT_BASH_POLICY,
        ),
        opencode_post_phase_mode=_resolve_choice(
            args.opencode_post_phase_mode,
            cfg.get("opencode_post_phase_mode"),
            DEFAULT_POST_PHASE_MODE,
        ),
        opencode_structured_output=_resolve_toggle(
            args.opencode_structured_output,
            cfg.get("opencode_structured_output"),
            True,
        ),
        modes=eval_modes,
        parallel=max(1, int(_first_non_none(args.parallel, cfg.get("parallel"), 1) or 1)),
        serialize_within_app=(
            _resolve_toggle(
                args.serialize_within_app,
                cfg.get("serialize_within_app"),
                True,
            )
        ),
        repeats=max(1, int(_first_non_none(args.repeats, cfg.get("repeats"), 1) or 1)),
        max_cases=(int(args.max_cases) if getattr(args, "max_cases", None) else None),
        app_names=app_names,
        max_apps=(int(_first_non_none(args.max_apps, cfg.get("max_apps"), None)) if _first_non_none(args.max_apps, cfg.get("max_apps"), None) is not None else None),
        max_sources=(int(_first_non_none(args.max_sources, cfg.get("max_sources"), None)) if _first_non_none(args.max_sources, cfg.get("max_sources"), None) is not None else None),
        classification_filter=_resolve_choice(args.classification_filter, cfg.get("classification_filter"), "all"),
        dummy_run=dummy_run_enabled,
        knowledge_mode=_resolve_choice(args.knowledge_mode, cfg.get("knowledge_mode"), "warm"),
        knowledge_allow_repeat_injection_within_session=_resolve_toggle(
            args.knowledge_allow_repeat_injection_within_session,
            cfg.get("knowledge_allow_repeat_injection_within_session"),
            True,
        ),
        auto_knowledge_cycle=_resolve_toggle(
            args.auto_knowledge_cycle,
            cfg.get("auto_knowledge_cycle"),
            True,
        ),
        runtime_injection_mode=normalize_runtime_injection_mode(
            _resolve_choice(
                getattr(args, "runtime_injection_mode", None),
                cfg.get("runtime_injection_mode"),
                "context_aware",
            )
        ),
        knowledge_distillation_mode=knowledge_distillation_mode,
        knowledge_packaging_mode=knowledge_packaging_mode,
        auto_knowledge_validate_mode=auto_knowledge_validate_mode,
        knowledge_reuse_digest_mode=knowledge_reuse_digest_mode,
        knowledge_repeat_summary_react_gap=_resolve_nonnegative_int(
            args.knowledge_repeat_summary_react_gap,
            cfg.get("knowledge_repeat_summary_react_gap"),
            default=0,
        ),
        knowledge_repeat_full_react_gap=_resolve_nonnegative_int(
            args.knowledge_repeat_full_react_gap,
            cfg.get("knowledge_repeat_full_react_gap"),
            default=1,
        ),
        knowledge_top_k=max(
            1,
            int(
                _first_non_none(
                    args.knowledge_top_k,
                    cfg.get("knowledge_top_k"),
                    3,
                )
                or 3
            ),
        ),
        knowledge_recall_top_m=max(
            1,
            int(
                _first_non_none(
                    args.knowledge_recall_top_m,
                    cfg.get("knowledge_recall_top_m"),
                    8,
                )
                or 8
            ),
        ),
        timeout_seconds=(int(_first_non_none(args.timeout_seconds, cfg.get("timeout_seconds"), 1800)) if _first_non_none(args.timeout_seconds, cfg.get("timeout_seconds"), 1800) is not None else 1800),
        runtime_backend_profile=runtime_backend_marker,
        runtime_backend_mode=runtime_backend_mode,
        runtime_backend_pool=None,
        runtime_backend_pool_candidates=[],
        runtime_backend_base_url=runtime_backend_base_url,
        runtime_backend_auth_token=runtime_backend_auth_token,
        runtime_backend_model=runtime_backend_model,
        llm_judge_enabled=llm_judge_enabled,
        llm_judge_base_url=llm_judge_base_url or "",
        llm_judge_api_key=llm_judge_api_key,
        llm_judge_model=llm_judge_model,
        llm_judge_timeout_seconds=max(1, int(_first_non_none(args.llm_judge_timeout_seconds, _internal_transport_text(INTERNAL_LLM_JUDGE_TIMEOUT_SECONDS_ENV), cfg.get("llm_judge_timeout_seconds"), 120) or 120)),
        llm_judge_max_retries=max(0, int(_first_non_none(args.llm_judge_max_retries, _internal_transport_text(INTERNAL_LLM_JUDGE_MAX_RETRIES_ENV), cfg.get("llm_judge_max_retries"), 2) or 2)),
        reuse_embed_base_url=reuse_embed_base_url,
        reuse_embed_api_key=reuse_embed_api_key,
        reuse_embed_model=reuse_embed_model,
        reuse_embed_verify_ssl=bool(reuse_embed_verify_ssl),
        reuse_rerank_base_url=reuse_rerank_base_url,
        reuse_rerank_api_key=reuse_rerank_api_key,
        reuse_rerank_model=reuse_rerank_model,
        reuse_rerank_timeout_seconds=reuse_rerank_timeout_seconds,
        requested_params=studio_requested_params,
        effective_params=studio_effective_params,
        normalization_warnings=studio_normalization_warnings,
    )
    validate_embedding_packaging_backend_config(config)
    return config, cfg


async def run_command(args: argparse.Namespace) -> int:
    """执行 run 子命令。"""
    query = args.query or ""
    if not query and args.interactive:
        query = _prompt_query()
    if not query:
        print("错误: 缺少 --query（或使用 --interactive 输入）。")
        return 2

    from flowark.runtime.runner import FlowArkRunner  # 延迟导入，避免 kb 子命令依赖 SDK

    sink_types = _parse_sink_types(args.sink_types)
    workspace_root = Path(__file__).parent.resolve()
    config = _resolve_runtime_run_config(args, workspace_root=workspace_root)
    _resolve_runtime_backend_mode_for_cli(
        workspace_root=workspace_root,
        runtime_backend_mode=getattr(args, "runtime_backend_mode", None),
    )
    request = AnalysisRequest(
        query=query,
        source=args.source,
        sink_types=sink_types,
        app_name=(args.app_name or None),
    )

    runner = FlowArkRunner(config)
    artifacts = await runner.run_query(request)
    artifact_audit = None
    if artifacts.run_dir and str(config.agent_mode or "").strip().lower() == "flowark":
        audit_path, audit_result = write_note_only_artifact_audit(artifacts.run_dir)
        artifact_audit = compact_artifact_audit_result(audit_result, path=audit_path)

    if artifacts.run_dir:
        print(f"\n运行产物目录: {artifacts.run_dir}")
    if artifacts.final_report_md:
        print(f"最终报告: {artifacts.final_report_md}")
    if artifacts.final_report_json:
        print(f"结构化报告: {artifacts.final_report_json}")
    if artifacts.cost_summary_json:
        print(f"成本/聚合指标: {artifacts.cost_summary_json}")
    if artifacts.knowledge_injection_log:
        print(f"知识注入日志: {artifacts.knowledge_injection_log}")
    if artifact_audit is not None:
        print(f"产物审计: {artifact_audit.get('path')}")
        print(
            "产物审计结果: "
            f"{'ok' if artifact_audit.get('ok') else 'failed'} "
            f"(errors={artifact_audit.get('error_count', 0)}, warnings={artifact_audit.get('warning_count', 0)})"
        )
        if not bool(artifact_audit.get("ok")):
            return 1
    return 0


async def eval_run_command(args: argparse.Namespace) -> int:
    from flowark.eval.harness import run_evaluation

    workspace_root = Path(__file__).parent.resolve()
    config, _ = _resolve_eval_config(args, workspace_root=workspace_root)
    result = await run_evaluation(config, workspace_root=workspace_root)
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
    print(f"任务数: {result['task_count']} (cases={result['case_count']}, modes={','.join(result['modes'])})")
    return 0 if artifact_audit is None or bool(artifact_audit.get("ok")) else 1


async def eval_resume_command(args: argparse.Namespace) -> int:
    from flowark.eval.harness import resume_evaluation

    workspace_root = Path(__file__).parent.resolve()
    if not getattr(args, "eval_root", None):
        raise ValueError("参数 --evaluation-root 不能为空")
    eval_root = Path(args.eval_root).expanduser().resolve()
    result = await resume_evaluation(eval_root, workspace_root=workspace_root)
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
    if result.get("paused"):
        print(f"评测状态: paused ({result.get('pause_mode') or 'none'})")
    else:
        print("评测状态: completed")
    print(f"任务数: {result['task_count']} (cases={result['case_count']}, modes={','.join(result['modes'])})")
    return 0 if artifact_audit is None or bool(artifact_audit.get("ok")) else 1


async def opencode_smoke_command(args: argparse.Namespace) -> int:
    from flowark.adapters.opencode import run_opencode_server_smoke

    workspace_root = Path(__file__).parent.resolve()
    config, run_dir = _resolve_opencode_smoke_config(args, workspace_root=workspace_root)
    result = await run_opencode_server_smoke(
        config=config,
        workspace_root=workspace_root,
        run_dir=run_dir,
        send_prompt=bool(args.send_prompt),
        prompt=str(args.prompt or ""),
    )
    print(f"OpenCode smoke: {'ok' if result.get('ok') else 'failed'}")
    print(f"产物目录: {run_dir}")
    print(f"smoke JSON: {run_dir / 'opencode_smoke.json'}")
    if result.get("server", {}).get("log_path"):
        print(f"server log: {result['server']['log_path']}")
    if not bool(args.send_prompt):
        print("未发送模型 prompt；当前 smoke 只验证 server/session。")
    return 0


async def opencode_real_smoke_command(args: argparse.Namespace) -> int:
    from flowark.adapters.opencode import run_opencode_delivery_real_smoke

    workspace_root = Path(__file__).parent.resolve()
    config, out_dir = _resolve_opencode_real_smoke_config(args, workspace_root=workspace_root)
    result = await run_opencode_delivery_real_smoke(
        base_config=config,
        workspace_root=workspace_root,
        out_dir=out_dir,
        deliveries=args.delivery,
    )
    smoke_root = Path(str(result.get("smoke_root") or out_dir)).expanduser()
    recommendation = result.get("recommended_default") if isinstance(result.get("recommended_default"), dict) else {}
    print(f"OpenCode real smoke: {'ok' if result.get('ok') else 'needs_review'}")
    print(f"产物目录: {smoke_root}")
    print(f"对比 JSON: {smoke_root / 'opencode_delivery_comparison.json'}")
    print(f"对比报告: {smoke_root / 'opencode_delivery_comparison.md'}")
    if recommendation:
        print(f"推荐默认 delivery: {recommendation.get('delivery')} ({recommendation.get('reason')})")
    if bool(result.get("backend_rate_limited")):
        print("后端状态: rate_limited_or_429（已记录，不作为 schema/parser 结论）")
        return 0
    if bool(result.get("environment_error")):
        print("后端状态: environment_error（未形成 delivery/cache 结论，请检查 settings/env）")
        return 1
    return 0 if bool(result.get("ok")) else 1


def kb_synth_command(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else run_dir

    synthesizer = KnowledgeSynthesizer()
    candidates = synthesizer.propose_from_run(run_dir)
    reason = synthesizer.disabled_reason()
    out_path = save_candidates(out_dir / "knowledge_candidates.json", candidates, reason=reason)

    print(reason)
    print(f"已生成候选: {len(candidates)}")
    print(f"输出文件: {out_path}")
    return 0


def kb_validate_command(args: argparse.Namespace) -> int:
    candidate_path = Path(args.candidate).expanduser().resolve()
    cwd = Path(args.cwd).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve() if args.out else candidate_path.with_name("knowledge_validation.json")

    candidates = load_candidates(candidate_path)
    validator = KnowledgeValidator()
    results = [validator.validate(candidate, cwd=cwd) for candidate in candidates]
    save_validation_results(out_path, results)

    print(f"已验证候选: {len(results)}")
    print(f"PASS: {sum(1 for r in results if r.status == 'PASS')}")
    print(f"REVISE: {sum(1 for r in results if r.status == 'REVISE')}")
    print(f"REJECT: {sum(1 for r in results if r.status == 'REJECT')}")
    print(f"输出文件: {out_path}")
    return 0


def _load_validation_results_or_validate(
    candidate_file: Path,
    *,
    cwd: Path,
) -> list[ValidationResult]:
    data = json.loads(candidate_file.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "results" in data:
        results: list[ValidationResult] = []
        for item in data.get("results") or []:
            if not isinstance(item, dict):
                continue
            candidate_data = item.get("normalized_candidate")
            normalized_candidate = None
            if isinstance(candidate_data, dict):
                normalized_candidate = knowledge_candidate_from_dict(candidate_data)
            status = str(item.get("status") or "REJECT").upper()
            if status not in {"PASS", "REVISE", "REJECT"}:
                status = "REJECT"
            results.append(
                ValidationResult(
                    candidate_id=str(item.get("candidate_id") or ""),
                    status=status,  # type: ignore[arg-type]
                    reasons=[str(v) for v in (item.get("reasons") or [])],
                    normalized_candidate=normalized_candidate,
                    evidence_summary=(str(item["evidence_summary"]) if item.get("evidence_summary") else None),
                )
            )
        return results

    candidates = load_candidates(candidate_file)
    validator = KnowledgeValidator()
    return [validator.validate(candidate, cwd=cwd) for candidate in candidates]


def kb_apply_command(args: argparse.Namespace) -> int:
    candidate_file = Path(args.candidate).expanduser().resolve()
    cwd = Path(args.cwd).expanduser().resolve()
    store = KnowledgeStore(_resolve_explicit_skills_dir(args.skills_dir, context_label="kb apply"))

    results = _load_validation_results_or_validate(candidate_file, cwd=cwd)
    applied_paths = [store.apply_validation_result(result) for result in results]
    applied_paths = [path for path in applied_paths if path is not None]

    print(f"输入结果数: {len(results)}")
    print(f"已写入 skills: {len(applied_paths)}")
    for path in applied_paths:
        print(f"- {path}")
    return 0


def kb_lint_command(args: argparse.Namespace) -> int:
    skills_dir = _resolve_explicit_skills_dir(args.skills_dir, context_label="kb lint")
    result = lint_skills_dir(skills_dir, max_frontmatter_chars=int(args.max_frontmatter_chars))
    if args.out:
        out_path = Path(args.out).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"输出文件: {out_path}")
    print(f"已检查 skills: {result.get('checked_count', 0)}")
    print(f"发现问题: {result.get('issue_count', 0)}")
    for item in (result.get("issues") or [])[:20]:
        print(f"- {item.get('skill_id')}: {', '.join(item.get('issue_types') or [])}")
    return 0 if int(result.get("issue_count", 0) or 0) == 0 else 1


def kb_migrate_command(args: argparse.Namespace) -> int:
    skills_dir = _resolve_explicit_skills_dir(args.skills_dir, context_label="kb migrate")
    archive_dir = Path(args.archive_dir).expanduser().resolve() if args.archive_dir else None
    result = migrate_skills_to_archive(
        skills_dir,
        archive_dir=archive_dir,
        dry_run=(not bool(args.apply)),
        max_frontmatter_chars=int(args.max_frontmatter_chars),
    )
    if args.out:
        out_path = Path(args.out).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"输出文件: {out_path}")
    mode = "dry-run" if result.get("dry_run") else "apply"
    print(f"迁移模式: {mode}")
    print(f"候选数量: {result.get('candidate_count', 0)}")
    print(f"已迁移数量: {result.get('migrated_count', 0)}")
    for item in (result.get("entries") or [])[:30]:
        print(
            f"- {item.get('skill_id')}: {item.get('from')} -> {item.get('to')} "
            f"({'moved' if item.get('moved') else 'planned'})"
        )
    return 0


def kb_audit_artifacts_command(args: argparse.Namespace) -> int:
    root = Path(args.path).expanduser().resolve()
    result = audit_note_only_artifacts(root, include_skill_lint=not bool(args.skip_skill_lint))
    if args.out:
        out_path = Path(args.out).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"输出文件: {out_path}")
    print(f"检查根目录: {result.get('root')}")
    print(f"检查结果: {'ok' if result.get('ok') else 'failed'}")
    print(f"错误: {result.get('error_count', 0)}")
    print(f"警告: {result.get('warning_count', 0)}")
    counts = result.get("counts") if isinstance(result.get("counts"), dict) else {}
    if counts:
        print("计数: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    for item in (result.get("issues") or [])[:30]:
        print(f"- [{item.get('severity')}] {item.get('kind')}: {item.get('file')} - {item.get('message')}")
    return 0 if bool(result.get("ok")) else 1


async def main_async() -> int:
    parser = build_parser()
    argv = sys.argv[1:]
    if argv and argv[0] == "eval":
        argv = ["evaluation", *argv[1:]]
    args = parser.parse_args(argv)
    workspace_root = Path(__file__).parent.resolve()

    if args.command in {"run", "evaluation"}:
        warning = format_legacy_state_dirs_warning(workspace_root)
        if warning:
            print(f"[flowark] {warning}", file=sys.stderr, flush=True)

    if args.command == "run":
        return await run_command(args)
    if args.command == "opencode-smoke":
        return await opencode_smoke_command(args)
    if args.command == "opencode-real-smoke":
        return await opencode_real_smoke_command(args)
    if args.command == "kb":
        if args.kb_command == "synth":
            return kb_synth_command(args)
        if args.kb_command == "validate":
            return kb_validate_command(args)
        if args.kb_command == "apply":
            return kb_apply_command(args)
        if args.kb_command == "lint":
            return kb_lint_command(args)
        if args.kb_command == "migrate":
            return kb_migrate_command(args)
        if args.kb_command == "audit-artifacts":
            return kb_audit_artifacts_command(args)
        parser.parse_args(["kb", "-h"])
        return 2
    if args.command == "evaluation":
        if args.eval_command == "run":
            return await eval_run_command(args)
        if args.eval_command == "resume":
            return await eval_resume_command(args)
        parser.parse_args(["evaluation", "-h"])
        return 2
    parser.print_help()
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\n用户中断，退出 FlowArk。")
        return 130
    except Exception as exc:
        print(f"\n错误: {exc}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
