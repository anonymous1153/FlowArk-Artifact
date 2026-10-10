"""轻量 DSL 规则匹配。"""

from __future__ import annotations

import re

from flowark.types import MatchRule, MatchRules, RuleCandidateMatch, RuleMatchResult


_GENERIC_SYMBOL_TAILS = {
    "activity",
    "adapter",
    "client",
    "dao",
    "fragment",
    "helper",
    "log",
    "logger",
    "manager",
    "modifier",
    "repository",
    "resource",
    "service",
    "util",
    "viewmodel",
}
_GENERIC_PACKAGE_SEGMENTS = {
    "adapter",
    "base",
    "client",
    "common",
    "core",
    "dao",
    "data",
    "helper",
    "manager",
    "model",
    "repository",
    "service",
    "ui",
    "util",
    "viewmodel",
}
_FRAMEWORK_PACKAGE_PREFIXES = (
    "android.",
    "androidx.",
    "java.",
    "javax.",
    "kotlin.",
    "kotlinx.",
    "retrofit2.",
    "okhttp3.",
    "io.ktor.",
    "com.google.",
    "org.jetbrains.",
    "dagger.",
    "hilt.",
    "coil.",
)
_CALL_METHOD_INVALID_RE = re.compile(r"[.|()|]")
_IDENTIFIER_CHARS = "A-Za-z0-9_"
_KOTLIN_FUN_PREFIX_RE = re.compile(
    r"(?:(?:public|private|protected|internal|open|final|abstract|override|suspend|inline|operator|"
    r"infix|tailrec|external|data|sealed|const|lateinit)\s+)*fun\s*$",
    flags=re.IGNORECASE,
)
_KOTLIN_EXTENSION_FUN_PREFIX_RE = re.compile(
    r"(?:(?:public|private|protected|internal|open|final|abstract|override|suspend|inline|operator|"
    r"infix|tailrec|external|data|sealed|const|lateinit)\s+)*fun\s+[A-Za-z_][A-Za-z0-9_<>?, ]*$",
    flags=re.IGNORECASE,
)
_STRONG_RULE_SCORE = {
    "exact_symbol": 120,
    "call": 100,
}
_WEAK_RULE_SCORE = {
    "symbol_tail": 40,
    "package_prefix": 25,
    "call": 10,
}
_GENERIC_RUNTIME_ANCHOR_SCORE = 5
_GENERIC_RUNTIME_CALL_ANCHORS = {
    "intent.getBooleanExtra",
    "intent.getParcelableExtra",
    "intent.getStringExtra",
    "bundle.getBoolean",
    "bundle.getInt",
    "bundle.getString",
    "bundle.putBoolean",
    "bundle.putInt",
    "bundle.putString",
    "pixel.fire",
    "webView.loadUrl",
}
_GENERIC_RUNTIME_CALL_PREFIXES = {
    ("bundle", "get"),
    ("bundle", "put"),
    ("sharedpreferences", "get"),
    ("sharedprefs", "get"),
}
_GENERIC_RUNTIME_SYMBOL_ANCHORS = {
    "CredentialsSyncMetadata",
    "Entity",
    "Intent.EXTRA_STREAM",
    "Intent.EXTRA_SUBJECT",
    "Intent.EXTRA_TEXT",
    "Result.Success",
    "SitePermissionsEntity",
    "SyncStore",
    "add",
    "getUpdatesSince",
    "launchNewSearchOrQuery",
    "onSuccess",
}

def _normalize(value: str) -> str:
    return str(value or "").strip().casefold()


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _package_segments(value: str) -> list[str]:
    return [segment for segment in _normalize(value).split(".") if segment]


def _call_shape_issue_types(rule: MatchRule) -> list[str]:
    if rule.kind != "call":
        return []
    method = str(rule.method or "").strip()
    if not method:
        return ["invalid_call_shape"]
    if _CALL_METHOD_INVALID_RE.search(method):
        return ["invalid_call_shape"]
    return []


def is_framework_package_prefix(value: str) -> bool:
    normalized = _normalize(value)
    return any(normalized == prefix[:-1] or normalized.startswith(prefix) for prefix in _FRAMEWORK_PACKAGE_PREFIXES)


def is_package_prefix_strong_candidate(value: str) -> bool:
    segments = _package_segments(value)
    if not segments:
        return False
    if is_framework_package_prefix(value):
        return False
    if len(segments) < 3:
        return False
    if segments[-1] in _GENERIC_PACKAGE_SEGMENTS:
        return False
    return True


def is_symbol_tail_strong_candidate(value: str) -> bool:
    normalized = _normalize(value)
    if not normalized or "." in normalized:
        return False
    if len(normalized) < 6:
        return False
    if normalized in _GENERIC_SYMBOL_TAILS:
        return False
    return True


def is_strong_positive_rule(rule: MatchRule) -> bool:
    try:
        compile_match_rule(rule)
    except Exception:
        return False
    if rule.kind == "exact_symbol":
        return True
    if rule.kind == "call":
        return bool(str(rule.receiver or "").strip() and str(rule.method or "").strip())
    return False


def audit_match_rules(rules: MatchRules) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []

    def _add_issue(issue_type: str, bucket_name: str, index: int | None, rule: MatchRule | None) -> None:
        issues.append(
            {
                "type": issue_type,
                "bucket": bucket_name,
                "index": index,
                "rule": rule,
            }
        )

    for bucket_name, bucket in (
        ("require_all", list(rules.require_all or [])),
        ("require_any", list(rules.require_any or [])),
        ("exclude", list(rules.exclude or [])),
    ):
        for index, rule in enumerate(bucket, start=1):
            if rule.kind == "call":
                for issue_type in _call_shape_issue_types(rule):
                    _add_issue(issue_type, bucket_name, index, rule)
            if bucket_name == "require_all":
                continue
            if bucket_name != "require_any":
                continue
            if _is_generic_runtime_anchor(rule):
                _add_issue("generic_runtime_anchor_in_require_any", bucket_name, index, rule)
            if rule.kind == "package_prefix" and is_framework_package_prefix(rule.value):
                _add_issue("framework_prefix_as_strong_rule", bucket_name, index, rule)
            if rule.kind == "symbol_tail" and not is_symbol_tail_strong_candidate(rule.value):
                if _normalize(rule.value) in _GENERIC_SYMBOL_TAILS:
                    _add_issue("generic_symbol_tail_as_strong_rule", bucket_name, index, rule)
                else:
                    _add_issue("broad_require_any_rule", bucket_name, index, rule)
    require_any_rules = list(rules.require_any or [])
    if require_any_rules and not any(is_strong_positive_rule(rule) for rule in require_any_rules):
        if all(rule.kind == "package_prefix" for rule in require_any_rules):
            _add_issue("package_prefix_only_positive_evidence", "require_any", None, None)
        else:
            _add_issue("weak_require_any_only", "require_any", None, None)
    require_all_rules = list(rules.require_all or [])
    if require_all_rules:
        if all(_is_generic_runtime_anchor(rule) for rule in require_all_rules):
            _add_issue("generic_runtime_anchor_only_require_all", "require_all", None, None)
        elif not any(_is_require_all_stable_anchor(rule) for rule in require_all_rules):
            _add_issue("require_all_missing_stable_anchor", "require_all", None, None)
        elif len(require_all_rules) == 1 and not is_strong_positive_rule(require_all_rules[0]):
            _add_issue("require_all_single_weak_anchor", "require_all", None, require_all_rules[0])

    return issues


def compile_match_rule(rule: MatchRule) -> list[str]:
    """将单条规则压缩为稳定字符串。"""
    if rule.kind == "exact_symbol":
        value = str(rule.value or "").strip()
        if not value:
            raise ValueError("exact_symbol 规则缺少 value")
        return [f"exact_symbol:{value}"]
    if rule.kind == "symbol_tail":
        value = str(rule.value or "").strip()
        if not value:
            raise ValueError("symbol_tail 规则缺少 value")
        return [f"symbol_tail:{value}"]
    if rule.kind == "package_prefix":
        value = str(rule.value or "").strip()
        if not value:
            raise ValueError("package_prefix 规则缺少 value")
        return [f"package_prefix:{value}"]
    if rule.kind == "call":
        if _call_shape_issue_types(rule):
            raise ValueError("call 规则的 method 必须是单一方法名，且不能包含 receiver.method 或多方法列表")
        receiver = str(rule.receiver or "").strip()
        method = str(rule.method or "").strip()
        if receiver and method:
            return [f"call:{receiver}.{method}"]
        if method:
            return [f"call:.{method}"]
        raise ValueError("call 规则必须提供 method，且不允许仅提供 receiver")
    raise ValueError(f"unsupported rule kind: {rule.kind}")


def compile_match_rules(rules: MatchRules) -> dict[str, list[str]]:
    return {
        "require_all": [item for rule in rules.require_all for item in compile_match_rule(rule)],
        "require_any": [item for rule in rules.require_any for item in compile_match_rule(rule)],
        "exclude": [item for rule in rules.exclude for item in compile_match_rule(rule)],
    }


def normalize_match_rules(rules: MatchRules) -> MatchRules:
    def _normalize_bucket(items: list[MatchRule]) -> list[MatchRule]:
        normalized: list[MatchRule] = []
        seen: set[tuple[str, str]] = set()
        for rule in items:
            identity = (rule.kind, "|".join(compile_match_rule(rule)))
            if identity in seen:
                continue
            seen.add(identity)
            normalized.append(rule)
        return normalized

    return MatchRules(
        require_all=_normalize_bucket(list(rules.require_all or [])),
        require_any=_normalize_bucket(list(rules.require_any or [])),
        exclude=_normalize_bucket(list(rules.exclude or [])),
    )


def summarize_match_rules(rules: MatchRules, *, limit: int = 6) -> list[str]:
    summary: list[str] = []
    for bucket_name, bucket in (
        ("require_all", list(rules.require_all or [])),
        ("require_any", list(rules.require_any or [])),
        ("exclude", list(rules.exclude or [])),
    ):
        for rule in bucket:
            for item in compile_match_rule(rule):
                summary.append(f"{bucket_name}:{item}")
                if len(summary) >= limit:
                    return summary
    return summary


def _boundary_pattern(value: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![{_IDENTIFIER_CHARS}]){re.escape(value)}(?![{_IDENTIFIER_CHARS}])",
        flags=re.IGNORECASE,
    )


def _package_prefix_pattern(value: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![{_IDENTIFIER_CHARS}]){re.escape(value)}(?=\.|[^\w]|$)",
        flags=re.IGNORECASE,
    )


def _call_with_receiver_pattern(receiver: str, method: str) -> re.Pattern[str]:
    receiver_pattern = re.escape(receiver)
    method_pattern = re.escape(method)
    return re.compile(
        rf"(?<![{_IDENTIFIER_CHARS}]){receiver_pattern}\s*(?:\?\s*|\!\!\s*)?\.\s*{method_pattern}\s*\(",
        flags=re.IGNORECASE,
    )


def _method_only_call_pattern(method: str) -> re.Pattern[str]:
    method_pattern = re.escape(method)
    return re.compile(
        rf"(?:\?\s*|\!\!\s*)?\.\s*{method_pattern}\s*\(",
        flags=re.IGNORECASE,
    )


def _looks_like_definition_context(text: str, start: int) -> bool:
    line_start = text.rfind("\n", 0, start) + 1
    prefix = text[line_start:start].strip()
    if not prefix:
        return False
    return bool(_KOTLIN_FUN_PREFIX_RE.fullmatch(prefix) or _KOTLIN_EXTENSION_FUN_PREFIX_RE.fullmatch(prefix))


def _has_non_definition_match(pattern: re.Pattern[str], text: str) -> bool:
    for match in pattern.finditer(text):
        if _looks_like_definition_context(text, match.start()):
            continue
        return True
    return False


def _rule_matches_text(rule: MatchRule, text: str) -> bool:
    value = str(text or "")
    if not value:
        return False
    if rule.kind == "exact_symbol":
        return _boundary_pattern(str(rule.value or "").strip()).search(value) is not None
    if rule.kind == "symbol_tail":
        return _boundary_pattern(str(rule.value or "").strip()).search(value) is not None
    if rule.kind == "package_prefix":
        return _package_prefix_pattern(str(rule.value or "").strip()).search(value) is not None
    if rule.kind == "call":
        method = str(rule.method or "").strip()
        receiver = str(rule.receiver or "").strip()
        if receiver:
            return _has_non_definition_match(_call_with_receiver_pattern(receiver, method), value)
        return _has_non_definition_match(_method_only_call_pattern(method), value)
    return False


def _match_descriptor(rule: MatchRule) -> str:
    return compile_match_rule(rule)[0]


def _is_generic_runtime_anchor(rule: MatchRule) -> bool:
    if rule.kind == "call":
        receiver = str(rule.receiver or "").strip()
        method = str(rule.method or "").strip()
        if not receiver or not method:
            return False
        call_name = f"{receiver}.{method}"
        if call_name in _GENERIC_RUNTIME_CALL_ANCHORS:
            return True
        receiver_key = _normalize(receiver)
        method_key = _normalize(method)
        if receiver_key == "intent" and method_key.startswith("get") and method_key.endswith("extra"):
            return True
        return any(
            receiver_key == prefix_receiver and method_key.startswith(prefix_method)
            for prefix_receiver, prefix_method in _GENERIC_RUNTIME_CALL_PREFIXES
        )
    if rule.kind == "exact_symbol":
        return str(rule.value or "").strip() in _GENERIC_RUNTIME_SYMBOL_ANCHORS
    return False


def _is_require_all_stable_anchor(rule: MatchRule) -> bool:
    if _is_generic_runtime_anchor(rule):
        return False
    if is_strong_positive_rule(rule):
        return True
    if rule.kind == "symbol_tail":
        return is_symbol_tail_strong_candidate(rule.value)
    if rule.kind == "package_prefix":
        return is_package_prefix_strong_candidate(rule.value)
    return False


def _is_require_any_recallable_anchor(rule: MatchRule) -> bool:
    if _is_generic_runtime_anchor(rule):
        return False
    if is_strong_positive_rule(rule):
        return True
    if rule.kind == "symbol_tail":
        return is_symbol_tail_strong_candidate(rule.value)
    if rule.kind == "package_prefix":
        return is_package_prefix_strong_candidate(rule.value)
    return False


def _require_all_is_recallable(rules: list[MatchRule]) -> bool:
    require_all = list(rules or [])
    if not require_all:
        return False
    if all(_is_generic_runtime_anchor(rule) for rule in require_all):
        return False
    if not any(_is_require_all_stable_anchor(rule) for rule in require_all):
        return False
    if len(require_all) == 1 and not is_strong_positive_rule(require_all[0]):
        return False
    return True


def is_generic_runtime_anchor(rule: MatchRule) -> bool:
    return _is_generic_runtime_anchor(rule)


def rule_matches_text(rule: MatchRule, text: str) -> bool:
    return _rule_matches_text(rule, text)


def describe_match_rule(rule: MatchRule) -> str:
    return _match_descriptor(rule)


def _bucket_score(rule: MatchRule) -> int:
    if _is_generic_runtime_anchor(rule):
        return _GENERIC_RUNTIME_ANCHOR_SCORE
    if is_strong_positive_rule(rule):
        return _STRONG_RULE_SCORE.get(rule.kind, 0)
    return _WEAK_RULE_SCORE.get(rule.kind, 0)


def _matched_bucket(rules: list[MatchRule], text: str) -> tuple[list[str], list[int], int, int]:
    matched: list[str] = []
    scores: list[int] = []
    strong_hits = 0
    weak_hits = 0
    for rule in rules:
        if not _rule_matches_text(rule, text):
            continue
        matched.append(_match_descriptor(rule))
        scores.append(_bucket_score(rule))
        if _is_require_any_recallable_anchor(rule):
            strong_hits += 1
        else:
            weak_hits += 1
    return _dedupe_preserve_order(matched), scores, strong_hits, weak_hits


def match_rules(rules: MatchRules, text: str) -> RuleMatchResult:
    matched_exclude, _, _, _ = _matched_bucket(list(rules.exclude or []), text)
    if matched_exclude:
        return RuleMatchResult(
            matched=False,
            score=0,
            matched_exclude=matched_exclude,
            probe_hits=matched_exclude.copy(),
        )

    require_all_rules = list(rules.require_all or [])
    matched_require_all, require_all_scores, _, _ = _matched_bucket(require_all_rules, text)
    require_all_matched = bool(require_all_rules) and len(matched_require_all) == len(require_all_rules)
    require_all_score = 200 + sum(require_all_scores) if require_all_matched else 0

    matched_require_any, require_any_scores, require_any_recallable_hits, _ = _matched_bucket(list(rules.require_any or []), text)
    require_any_matched = bool(matched_require_any) and require_any_recallable_hits > 0
    require_any_score = 0
    if require_any_matched:
        require_any_score = max(require_any_scores) + 20 * min(max(len(require_any_scores) - 1, 0), 2)

    if not require_all_matched and not require_any_matched:
        return RuleMatchResult(
            matched=False,
            score=0,
            matched_require_all=matched_require_all,
            matched_require_any=matched_require_any,
        )

    probe_hits = _dedupe_preserve_order(matched_require_all + matched_require_any)
    return RuleMatchResult(
        matched=True,
        score=require_all_score + require_any_score,
        matched_require_all=matched_require_all,
        matched_require_any=matched_require_any,
        probe_hits=probe_hits,
    )


def rank_rule_candidates(candidates: list[tuple[str, MatchRules]], text: str) -> list[RuleCandidateMatch]:
    ranked: list[RuleCandidateMatch] = []
    for candidate_id, rules in candidates:
        result = match_rules(rules, text)
        ranked.append(
            RuleCandidateMatch(
                candidate_id=candidate_id,
                score=result.score,
                matched=result.matched,
                matched_require_all=result.matched_require_all,
                matched_require_any=result.matched_require_any,
                matched_exclude=result.matched_exclude,
            )
        )

    ranked.sort(
        key=lambda item: (
            not item.matched,
            -item.score,
            -len(item.matched_require_all),
            -len(item.matched_require_any),
            item.candidate_id,
        )
    )
    return ranked


def has_strong_positive_rule(rules: MatchRules) -> bool:
    for rule in [*list(rules.require_all or []), *list(rules.require_any or [])]:
        if is_strong_positive_rule(rule):
            return True
    return False


def has_recallable_require_any(rules: MatchRules) -> bool:
    if _require_all_is_recallable(list(rules.require_all or [])):
        return True
    for rule in list(rules.require_any or []):
        if _is_require_any_recallable_anchor(rule):
            return True
    return False


def match_exact_labels(labels: list[str], text: str) -> list[str]:
    return [label for label in labels if _boundary_pattern(str(label or "").strip()).search(text or "") is not None]


def match_egress_case(selectors: list[str], negative_selectors: list[str], text: str) -> tuple[list[str], bool]:
    if match_exact_labels(negative_selectors, text):
        return [], False
    return match_exact_labels(selectors, text), True
