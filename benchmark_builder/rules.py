"""Static scan rules for benchmark_builder v2."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from benchmark_builder.manifest import ManifestIndex
from benchmark_builder.schemas import SourceKind

_CONTROL_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "throw", "when"}
_CLASS_RE = re.compile(r"\b(?:class|interface|object|enum)\s+(?P<name>[A-Za-z_][\w$]*)")
_KOTLIN_FUN_RE = re.compile(r"\bfun\s+(?P<name>[A-Za-z_][\w$]*)\s*\(")
_JAVA_METHOD_RE = re.compile(
    r"^\s*(?:public|private|protected|static|final|synchronized|native|abstract|default|override|internal|\s)+"
    r"[\w<>\[\], ?]+\s+(?P<name>[A-Za-z_][\w$]*)\s*\([^;]*\)\s*(?:\{|throws\b)"
)
_CALLBACK_TYPED_PARAM_RE = re.compile(r"(?P<name>[A-Za-z_][\w$]*)\s*:\s*(?P<type>[A-Za-z_][\w$.<>?]*)")
_JAVA_TYPED_PARAM_RE = re.compile(r"(?P<type>[A-Za-z_][\w$.<>?]*)\s+(?P<name>[A-Za-z_][\w$]*)")
_REMOTE_PARSE_CONTEXT_RE = re.compile(
    r"\b(?:response|responseBody|remoteMessage|snapshot|http|[A-Za-z_]\w*client|websocket|remoteConfig)\b",
    re.IGNORECASE,
)
_BUILD_VERSION_GUARD_RE = re.compile(
    r"Build\.VERSION\.SDK_INT\b.*(?:[<>]=?|==|!=|\bin\b)"
)
_KOTLIN_FUNCTION_DECL_RE = re.compile(r"^(?:actual\s+|expect\s+|override\s+|private\s+|public\s+|protected\s+|internal\s+|suspend\s+)*fun\b")
_JAVA_FUNCTION_DECL_RE = re.compile(
    r"^(?:public|private|protected|static|final|synchronized|native|abstract|default|\s)+"
    r"[\w<>\[\], ?]+\s+[A-Za-z_][\w$]*\s*\([^;]*\)\s*(?:\{|throws\b)"
)
_TEXT_VALUE_RE = re.compile(
    r"\b(?P<receiver>[A-Za-z_][\w$]*)\s*(?:!!|\?)?\s*\.\s*(?:(?:getText|editableText)\s*\(\s*\)|text)\s*(?:\?\.)?\s*\.?\s*toString\s*\(",
)
_UI_TEXT_RECEIVER_HINT_RE = re.compile(
    r"(?:edittext|edit_text|textinput|input|field|password|search|username|email)",
    re.IGNORECASE,
)
_CHECKED_VALUE_RE = re.compile(r"\b(?P<receiver>[A-Za-z_][\w$]*)\s*(?:!!|\?)?\s*\.\s*(?:isChecked\s*\(\s*\)|isChecked\b)")
_CHECKED_RECEIVER_HINT_RE = re.compile(r"(?:check|radio|switch|toggle)", re.IGNORECASE)
_FILE_READ_RECEIVER_HINT_RE = re.compile(
    r"(?:file|path|cache|cached|dir|document|credential|secret|encrypted|archive|zip|json|csv|xml|txt|key)",
    re.IGNORECASE,
)
_CLIPBOARD_PRIMARY_CLIP_PROPERTY_RE = re.compile(
    r"\b(?:clipboard|clipManager|clipboardManager|mClipboard|systemClipboard)[A-Za-z_0-9$]*\??\s*\.\s*primaryClip\b",
    re.IGNORECASE,
)
_TELEPHONY_GETTER_RE = re.compile(
    r"\b(?P<receiver>[A-Za-z_][\w$]*)\s*\.\s*"
    r"(?:getDeviceId|getSubscriberId|getLine1Number|getCallState|getPhoneType|getSimState)\s*\(",
)
_APP_ENTRY_METHOD_HINTS = {
    "handleactivityresult",
    "handleintent",
    "ondeepplink",
    "onnewintent",
    "onreceive",
    "onstart",
    "oncreate",
}


@dataclass(frozen=True)
class RuleCandidate:
    source_kind: SourceKind
    source_subtype: str
    rule_id: str
    file_path: str
    line_number: int
    classname: str | None
    method: str | None
    statement: str
    description: str
    boundary_rank: int = 0
    boundary_subject: str | None = None


@dataclass(frozen=True)
class ScanContext:
    relative_path: str
    language: str
    manifest_index: ManifestIndex | None = None


def scan_source_file(
    source_dir: Path,
    file_path: Path,
    enabled_kinds: set[SourceKind],
    manifest_index: ManifestIndex | None = None,
) -> list[RuleCandidate]:
    relative_path = file_path.relative_to(source_dir).as_posix()
    suffix = file_path.suffix.lower()
    text = file_path.read_text(encoding="utf-8", errors="ignore")
    context = ScanContext(relative_path=relative_path, language=suffix.lstrip("."), manifest_index=manifest_index)
    if suffix == ".xml":
        return _scan_xml(context, text, enabled_kinds)
    if suffix in {".kt", ".java"}:
        return _scan_code(context, text, enabled_kinds)
    return []


def _scan_xml(context: ScanContext, text: str, enabled_kinds: set[SourceKind]) -> list[RuleCandidate]:
    return []


def _scan_code(context: ScanContext, text: str, enabled_kinds: set[SourceKind]) -> list[RuleCandidate]:
    candidates: list[RuleCandidate] = []
    package_name: str | None = None
    current_class: str | None = None
    current_method: str | None = None
    current_method_is_preview = False
    preview_annotation_pending = False
    in_block_comment = False
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        uncommented, in_block_comment = _strip_block_comments(raw_line, in_block_comment)
        line = uncommented.strip()
        if line.startswith("package "):
            package_name = line.removeprefix("package ").strip().rstrip(";")
            continue
        if (
            not line
            or line.startswith("//")
            or line.startswith("*")
            or line.startswith("/*")
            or line.startswith("import ")
        ):
            continue
        if re.match(r"@\s*(?:androidx\.compose\.ui\.tooling\.preview\.)?Preview\b", line):
            preview_annotation_pending = True
            continue
        class_match = _CLASS_RE.search(line)
        if class_match:
            current_class = class_match.group("name")
        method_name = _extract_method_name(line)
        if method_name:
            current_method = method_name
            current_method_is_preview = preview_annotation_pending or "preview" in method_name.lower()
            preview_annotation_pending = False

        if SourceKind.UI_INPUT in enabled_kinds:
            candidates.extend(
                _match_ui_input(
                    context.relative_path,
                    line_number,
                    current_class,
                    current_method,
                    line,
                    is_preview_method=current_method_is_preview,
                )
            )
        if SourceKind.ICC_PAYLOAD in enabled_kinds:
            candidates.extend(
                _match_app_entry(
                    context.relative_path,
                    line_number,
                    current_class,
                    current_method,
                    line,
                    package_name=package_name,
                    manifest_index=context.manifest_index,
                )
            )
        if SourceKind.REMOTE_PAYLOAD in enabled_kinds:
            candidates.extend(_match_remote_input(context.relative_path, line_number, current_class, current_method, line))
        if SourceKind.PERSISTENT_STORAGE in enabled_kinds:
            candidates.extend(_match_local_io_input(context.relative_path, line_number, current_class, current_method, line))
        if SourceKind.PLATFORM_API in enabled_kinds:
            candidates.extend(_match_system_context_input(context.relative_path, line_number, current_class, current_method, line))
    return candidates


def _strip_block_comments(line: str, in_block_comment: bool) -> tuple[str, bool]:
    remainder = line
    output = ""
    while remainder:
        if in_block_comment:
            end = remainder.find("*/")
            if end < 0:
                return output, True
            remainder = remainder[end + 2 :]
            in_block_comment = False
            continue
        start = remainder.find("/*")
        if start < 0:
            output += remainder
            break
        output += remainder[:start]
        remainder = remainder[start + 2 :]
        end = remainder.find("*/")
        if end < 0:
            return output, True
        remainder = remainder[end + 2 :]
    return output, in_block_comment


def _extract_method_name(line: str) -> str | None:
    kotlin = _KOTLIN_FUN_RE.search(line)
    if kotlin:
        return kotlin.group("name")
    java = _JAVA_METHOD_RE.search(line)
    if java:
        name = java.group("name")
        if name not in _CONTROL_KEYWORDS:
            return name
    return None


def _statement(line: str) -> str:
    return line.strip()


def _receiver_subject(line: str, method_name: str) -> str | None:
    match = re.search(rf"(?P<receiver>[A-Za-z_][\w$]*)\s*\.\s*{re.escape(method_name)}\s*(?:<[^>]+>)?\s*\(", line)
    if match:
        return match.group("receiver")
    property_match = re.search(rf"(?P<receiver>[A-Za-z_][\w$]*)\s*\.\s*{re.escape(method_name)}\b", line)
    if property_match:
        return property_match.group("receiver")
    return None


def _extract_typed_param(line: str, type_name: str) -> str | None:
    if not _looks_like_typed_param_boundary(line):
        return None
    for match in _CALLBACK_TYPED_PARAM_RE.finditer(line):
        if _type_matches(match.group("type"), type_name):
            return match.group("name")
    open_paren = line.find("(")
    close_paren = line.rfind(")")
    for match in _JAVA_TYPED_PARAM_RE.finditer(line):
        if open_paren < 0 or close_paren <= open_paren or not (open_paren < match.start() < close_paren):
            continue
        if _type_matches(match.group("type"), type_name):
            return match.group("name")
    return None


def _type_matches(observed_type: str, expected_type: str) -> bool:
    observed = str(observed_type or "").strip().rstrip("?")
    if not observed:
        return False
    simple = observed.split(".")[-1]
    expected = expected_type.rstrip("<")
    if expected_type.endswith("<"):
        return simple == expected or simple.startswith(f"{expected}<")
    return simple == expected


def _looks_like_typed_param_boundary(line: str) -> bool:
    text = line.strip()
    if text.startswith(("val ", "var ")):
        return False
    if " fun " in f" {text}" or "->" in text:
        return True
    return bool(re.search(r"\([^)]*\)\s*(?:\{|=|throws\b)", text))


def _first_identifier_argument(line: str, call_name: str) -> str | None:
    match = re.search(rf"{re.escape(call_name)}\s*\(\s*(?P<arg>[A-Za-z_][\w$]*)", line)
    if match:
        return match.group("arg")
    return None


def _looks_like_remote_parse(line: str) -> bool:
    return bool(_REMOTE_PARSE_CONTEXT_RE.search(line))


def _is_build_version_guard(line: str) -> bool:
    return bool(_BUILD_VERSION_GUARD_RE.search(line))


def _is_function_declaration(line: str) -> bool:
    return bool(_KOTLIN_FUNCTION_DECL_RE.search(line) or _JAVA_FUNCTION_DECL_RE.search(line))


def _text_input_receiver(line: str) -> str | None:
    if _is_function_declaration(line):
        return None
    match = _TEXT_VALUE_RE.search(line)
    if not match:
        return None
    receiver = match.group("receiver")
    if "editor" in receiver.lower() and "edittext" not in receiver.lower():
        return None
    if not _UI_TEXT_RECEIVER_HINT_RE.search(receiver):
        return None
    return receiver


def _checked_receiver(line: str) -> str | None:
    match = _CHECKED_VALUE_RE.search(line)
    if not match:
        return None
    receiver = match.group("receiver")
    if not _CHECKED_RECEIVER_HINT_RE.search(receiver):
        return None
    if re.search(r"\bisChecked\b\s*=", line):
        return None
    return receiver


def _looks_like_app_uri_query(method: str | None, line: str) -> bool:
    lowered = line.lower()
    if "intent" in lowered or "deeplink" in lowered:
        return True
    method_key = str(method or "").strip().lower()
    return method_key in _APP_ENTRY_METHOD_HINTS


def _is_manifest_external_entry(
    manifest_index: ManifestIndex | None,
    package_name: str | None,
    classname: str | None,
) -> bool:
    return bool(manifest_index and manifest_index.is_external_entry_class(package_name, classname))


def _intent_extra_rule(line: str) -> tuple[str, str, str] | None:
    if re.search(
        r"\b(?:intent|getIntent\(\))\??\s*\.\s*get(?:String|Int|Long|Boolean|Parcelable|Serializable|Bundle|StringArrayList)Extra\(",
        line,
    ):
        return ("intent_extra", "app_entry.intent_extra.v1", "读取外部入口 Intent extra 参数")
    if re.search(r"\bIntentCompat\.get(?:Parcelable|Serializable)Extra\(\s*intent\s*,", line):
        return ("intent_extra", "app_entry.intent_extra.v1", "读取外部入口 Intent extra 参数")
    return None


def _bundle_extra_rule(line: str) -> tuple[str, str, str] | None:
    if "notification" in line.lower():
        return None
    if re.search(r"\b(?:intent\.)?extras\??\.\s*get(?:String|Int|Long|Boolean|Parcelable|Serializable)\(", line):
        return ("bundle_read", "app_entry.bundle_read.v1", "读取外部入口 Bundle 参数")
    return None


def _local_file_read_receiver(line: str) -> str | None:
    lowered = line.lower()
    if any(token in lowered for token in ("assets.", "assetmanager", "/proc/", "processbuilder", ".inputstream", "inputstream", "reader.read")):
        return None
    if re.search(r"\bFile\s*\([^)]*\)\s*\.\s*read(?:Text|Bytes)\s*\(", line):
        return "File"
    subject = _receiver_subject(line, "readText") or _receiver_subject(line, "readBytes")
    if not subject:
        return None
    if not _FILE_READ_RECEIVER_HINT_RE.search(subject):
        return None
    return subject


def _looks_like_local_file_stream(file_path: str, line: str) -> bool:
    lowered = f"{file_path} {line}".lower()
    if any(
        token in lowered
        for token in (
            "smb",
            "webdav",
            "http://",
            "https://",
            "ftp://",
            "appnativedir",
            "filetor",
            ".so",
            "/torcore/",
            "/util/",
            "/utils/",
        )
    ):
        return False
    return True


def _is_maintenance_path(file_path: str) -> bool:
    return bool(re.search(r"(^|/)(migration|migrations)(/|_|\b)", file_path, re.IGNORECASE))


def _looks_like_package_manager_getter(line: str) -> bool:
    return bool(
        re.search(
            r"\b(?:packageManager|package_manager|pm|pkgManager|context\.packageManager)\s*\.\s*(?:getPackageInfo|getInstalledPackages)\s*\(",
            line,
        )
    )


def _looks_like_clipboard_getter(line: str) -> bool:
    if _is_function_declaration(line):
        return False
    return bool(re.search(r"\bgetPrimaryClip\s*\(", line) or _CLIPBOARD_PRIMARY_CLIP_PROPERTY_RE.search(line))


def _looks_like_location_getter(line: str) -> bool:
    if _is_function_declaration(line):
        return False
    receiver = _receiver_subject(line, "getLastKnownLocation")
    if not receiver:
        return False
    lowered = receiver.lower()
    return lowered in {"lm", "locationmanager", "locmanager", "mlocationmanager"} or "locationmanager" in lowered


def _looks_like_response_body_call(line: str) -> bool:
    if not (".body()" in line or re.search(r"\.body\s*<[^>]+>\s*\(", line)):
        return False
    receiver = _receiver_subject(line, "body")
    return str(receiver or "").lower() not in {"doc", "document"}


def _looks_like_fcm_remote_message_callback(method: str | None, line: str) -> bool:
    return str(method or "") == "onMessageReceived" or bool(
        re.search(r"\bonMessageReceived\s*\([^)]*\bRemoteMessage\b", line)
    )


def _looks_like_telephony_getter(line: str) -> bool:
    match = _TELEPHONY_GETTER_RE.search(line)
    if not match:
        return False
    receiver = match.group("receiver").lower()
    return receiver in {"tm", "telephony", "telephonymanager", "phonemanager"} or "telephony" in receiver


def _looks_like_compose_value_change(line: str) -> bool:
    match = re.search(r"onValueChange\s*=\s*\{\s*(?P<body>[^}]*)", line)
    if not match:
        return False
    body = match.group("body")
    return "->" in body or bool(re.search(r"\bit\b", body))


def _match_ui_input(
    file_path: str,
    line_number: int,
    classname: str | None,
    method: str | None,
    line: str,
    *,
    is_preview_method: bool = False,
) -> list[RuleCandidate]:
    out: list[RuleCandidate] = []
    if is_preview_method:
        return out
    statement = _statement(line)
    if _text_input_receiver(line):
        out.append(
            RuleCandidate(
                source_kind=SourceKind.UI_INPUT,
                source_subtype="text_getter",
                rule_id="ui.code.text_getter.v1",
                file_path=file_path,
                line_number=line_number,
                classname=classname,
                method=method,
                statement=statement,
                description="读取文本输入值",
            )
        )
    if _checked_receiver(line):
        out.append(
            RuleCandidate(
                source_kind=SourceKind.UI_INPUT,
                source_subtype="checked_value",
                rule_id="ui.code.checked_value.v1",
                file_path=file_path,
                line_number=line_number,
                classname=classname,
                method=method,
                statement=statement,
                description="读取选择型输入值",
            )
        )
    if _looks_like_compose_value_change(line):
        out.append(
            RuleCandidate(
                source_kind=SourceKind.UI_INPUT,
                source_subtype="compose_on_value_change",
                rule_id="ui.compose.on_value_change.v1",
                file_path=file_path,
                line_number=line_number,
                classname=classname,
                method=method,
                statement=statement,
                description="Compose 输入回调参数",
            )
        )
    return out


def _match_app_entry(
    file_path: str,
    line_number: int,
    classname: str | None,
    method: str | None,
    line: str,
    *,
    package_name: str | None,
    manifest_index: ManifestIndex | None,
) -> list[RuleCandidate]:
    out: list[RuleCandidate] = []
    statement = _statement(line)
    if not _is_manifest_external_entry(manifest_index, package_name, classname):
        return out

    for rule in (_intent_extra_rule(line), _bundle_extra_rule(line)):
        if not rule:
            continue
        subkind, rule_id, description = rule
        out.append(
            RuleCandidate(
                source_kind=SourceKind.ICC_PAYLOAD,
                source_subtype=subkind,
                rule_id=rule_id,
                file_path=file_path,
                line_number=line_number,
                classname=classname,
                method=method,
                statement=statement,
                description=description,
            )
        )
    if re.search(r"getQueryParameter\(", line) and _looks_like_app_uri_query(method, line):
        out.append(
            RuleCandidate(
                source_kind=SourceKind.ICC_PAYLOAD,
                source_subtype="uri_query_parameter",
                rule_id="app_entry.uri_query.v1",
                file_path=file_path,
                line_number=line_number,
                classname=classname,
                method=method,
                statement=statement,
                description="读取 URI query 参数",
            )
        )
    return out


def _match_remote_input(file_path: str, line_number: int, classname: str | None, method: str | None, line: str) -> list[RuleCandidate]:
    out: list[RuleCandidate] = []
    statement = _statement(line)
    for type_name, subkind, rule_id, description in [
        ("RemoteMessage", "push_callback", "remote.callback.remote_message.v1", "推送消息回调参数"),
        ("DataSnapshot", "firebase_snapshot", "remote.callback.data_snapshot.v1", "Firebase 数据快照回调参数"),
        ("DocumentSnapshot", "firestore_snapshot", "remote.callback.document_snapshot.v1", "Firestore 文档快照回调参数"),
        ("QuerySnapshot", "firestore_query_snapshot", "remote.callback.query_snapshot.v1", "Firestore 查询快照回调参数"),
    ]:
        subject = _extract_typed_param(line, type_name)
        if subject:
            if rule_id == "remote.callback.remote_message.v1" and not _looks_like_fcm_remote_message_callback(method, line):
                continue
            out.append(
                RuleCandidate(
                    source_kind=SourceKind.REMOTE_PAYLOAD,
                    source_subtype=subkind,
                    rule_id=rule_id,
                    file_path=file_path,
                    line_number=line_number,
                    classname=classname,
                    method=method,
                    statement=statement,
                    description=description,
                    boundary_rank=0,
                    boundary_subject=subject,
                )
            )

    if _looks_like_response_body_call(line):
        subject = _receiver_subject(line, "body")
        out.append(
            RuleCandidate(
                source_kind=SourceKind.REMOTE_PAYLOAD,
                source_subtype="response_body",
                rule_id="remote.response.body.v1",
                file_path=file_path,
                line_number=line_number,
                classname=classname,
                method=method,
                statement=statement,
                description="读取响应体对象",
                boundary_rank=1,
                boundary_subject=subject,
            )
        )
    if re.search(r"\.bodyAs(?:Channel|Text|Bytes)\s*\(", line) and _looks_like_remote_parse(line):
        out.append(
            RuleCandidate(
                source_kind=SourceKind.REMOTE_PAYLOAD,
                source_subtype="response_body_stream",
                rule_id="remote.response.body_stream.v1",
                file_path=file_path,
                line_number=line_number,
                classname=classname,
                method=method,
                statement=statement,
                description="读取响应体流或文本内容",
                boundary_rank=1,
                boundary_subject=None,
            )
        )

    parse_specs = [
        (r"\bJSONObject\s*\(\s*[^)]", "json_object_parse", "remote.parse.json_object.v1", "JSON 对象反序列化入口", "JSONObject"),
        (r"\bJSONArray\s*\(\s*[^)]", "json_array_parse", "remote.parse.json_array.v1", "JSON 数组反序列化入口", "JSONArray"),
        (r"\bfromJson\s*\(", "gson_from_json", "remote.parse.gson_from_json.v1", "Gson 反序列化入口", "fromJson"),
        (r"\bdecodeFromString\s*\(", "decode_from_string", "remote.parse.decode_from_string.v1", "字符串反序列化入口", "decodeFromString"),
        (r"\bparseFrom\s*\(", "parse_from", "remote.parse.parse_from.v1", "二进制反序列化入口", "parseFrom"),
    ]
    for pattern, subkind, rule_id, description, call_name in parse_specs:
        if re.search(pattern, line) and _looks_like_remote_parse(line):
            out.append(
                RuleCandidate(
                    source_kind=SourceKind.REMOTE_PAYLOAD,
                    source_subtype=subkind,
                    rule_id=rule_id,
                    file_path=file_path,
                    line_number=line_number,
                    classname=classname,
                    method=method,
                    statement=statement,
                    description=description,
                    boundary_rank=2,
                    boundary_subject=_first_identifier_argument(line, call_name),
                )
            )
    return out


def _match_local_io_input(file_path: str, line_number: int, classname: str | None, method: str | None, line: str) -> list[RuleCandidate]:
    out: list[RuleCandidate] = []
    statement = _statement(line)
    if re.search(r"\b[A-Za-z_][\w$]*(?:prefs|pref|preferences|sharedPreferences)[A-Za-z_0-9$]*\s*\.\s*get(?:String|Int|Long|Boolean|Float|StringSet)\(", line, re.IGNORECASE):
        out.append(
            RuleCandidate(
                source_kind=SourceKind.PERSISTENT_STORAGE,
                source_subtype="preferences_getter",
                rule_id="local.preferences.getter.v1",
                file_path=file_path,
                line_number=line_number,
                classname=classname,
                method=method,
                statement=statement,
                description="读取本地 preferences 值",
                boundary_rank=0,
                boundary_subject=_receiver_subject(line, "getString")
                or _receiver_subject(line, "getInt")
                or _receiver_subject(line, "getLong")
                or _receiver_subject(line, "getBoolean")
                or _receiver_subject(line, "getFloat"),
            )
        )
    if not _is_maintenance_path(file_path) and re.search(r"\b[A-Za-z_][\w$]*cursor[A-Za-z_0-9$]*\s*\.\s*get(?:String|Int|Long|Float|Double|Blob|Short)\(", line, re.IGNORECASE):
        out.append(
            RuleCandidate(
                source_kind=SourceKind.PERSISTENT_STORAGE,
                source_subtype="cursor_getter",
                rule_id="local.cursor.getter.v1",
                file_path=file_path,
                line_number=line_number,
                classname=classname,
                method=method,
                statement=statement,
                description="读取 Cursor 中的本地数据",
                boundary_rank=1,
                boundary_subject=_receiver_subject(line, "getString")
                or _receiver_subject(line, "getInt")
                or _receiver_subject(line, "getLong")
                or _receiver_subject(line, "getFloat")
                or _receiver_subject(line, "getDouble")
                or _receiver_subject(line, "getBlob")
                or _receiver_subject(line, "getShort"),
            )
        )
    if "FileInputStream(" in line and _looks_like_local_file_stream(file_path, line):
        out.append(
            RuleCandidate(
                source_kind=SourceKind.PERSISTENT_STORAGE,
                source_subtype="file_stream_open",
                rule_id="local.file.input_stream.v1",
                file_path=file_path,
                line_number=line_number,
                classname=classname,
                method=method,
                statement=statement,
                description="打开本地文件输入流",
                boundary_rank=0,
                boundary_subject=_first_identifier_argument(line, "FileInputStream"),
            )
        )
    file_read_subject = _local_file_read_receiver(line)
    if file_read_subject:
        out.append(
            RuleCandidate(
                source_kind=SourceKind.PERSISTENT_STORAGE,
                source_subtype="file_read",
                rule_id="local.file.read.v1",
                file_path=file_path,
                line_number=line_number,
                classname=classname,
                method=method,
                statement=statement,
                description="读取本地文件内容",
                boundary_rank=1,
                boundary_subject=file_read_subject,
            )
        )
    dao_match = re.search(
        r"\b[A-Za-z_][\w$]*dao[A-Za-z_0-9$]*\s*\.\s*(?P<method>(?:get|load|read|query|find)[A-Za-z_0-9$]*)\(",
        line,
        re.IGNORECASE,
    )
    if dao_match and dao_match.group("method").lower() != "querybuilder":
        out.append(
            RuleCandidate(
                source_kind=SourceKind.PERSISTENT_STORAGE,
                source_subtype="dao_return",
                rule_id="local.dao.return.v1",
                file_path=file_path,
                line_number=line_number,
                classname=classname,
                method=method,
                statement=statement,
                description="通过 DAO 读取本地持久化数据",
                boundary_rank=0,
                boundary_subject=_receiver_subject(line, dao_match.group("method")),
            )
        )
    return out


def _match_system_context_input(file_path: str, line_number: int, classname: str | None, method: str | None, line: str) -> list[RuleCandidate]:
    out: list[RuleCandidate] = []
    statement = _statement(line)
    for type_name, subkind, rule_id, description in [
        ("SensorEvent", "sensor_event_callback", "system.callback.sensor_event.v1", "传感器回调参数"),
    ]:
        subject = _extract_typed_param(line, type_name)
        if subject:
            out.append(
                RuleCandidate(
                    source_kind=SourceKind.PLATFORM_API,
                    source_subtype=subkind,
                    rule_id=rule_id,
                    file_path=file_path,
                    line_number=line_number,
                    classname=classname,
                    method=method,
                    statement=statement,
                    description=description,
                    boundary_rank=0,
                    boundary_subject=subject,
                )
            )

    getter_specs = [
        (r"Settings\.(?:Secure|System|Global)\.get(?:String|Int|Long|Float)\(", "settings_getter", "system.settings.getter.v1", "读取系统设置值"),
        (r"getPrimaryClip\(|\.\s*primaryClip\b", "clipboard_getter", "system.clipboard.getter.v1", "读取剪贴板内容"),
        (r"getLastKnownLocation\(", "location_getter", "system.location.getter.v1", "读取系统位置上下文"),
        (r"\.\s*activeNetworkInfo\b|\.\s*activeNetwork\b|getNetworkCapabilities\(", "network_state_getter", "system.network_state.getter.v1", "读取网络状态"),
        (
            r"(?:\b[A-Za-z_][\w$]*accountManager[A-Za-z_0-9$]*|AccountManager\.get\([^)]*\))\s*\.\s*(?:getAccounts|getAccountsByType|getAuthToken|peekAuthToken|getUserData)\(",
            "account_getter",
            "system.account.getter.v1",
            "读取账户上下文",
        ),
        (r"getPackageInfo\(|getInstalledPackages\(", "package_info_getter", "system.package_info.getter.v1", "读取包或安装信息"),
        (r"getDeviceId\(|getSubscriberId\(|getLine1Number\(|getCallState\(|getPhoneType\(|getSimState\(", "telephony_getter", "system.telephony.getter.v1", "读取电话上下文"),
    ]
    for pattern, subkind, rule_id, description in getter_specs:
        if rule_id == "system.build.info.v1" and (_is_build_version_guard(line) or _is_weak_build_info(file_path, line)):
            continue
        if rule_id == "system.package_info.getter.v1" and not _looks_like_package_manager_getter(line):
            continue
        if rule_id == "system.clipboard.getter.v1" and not _looks_like_clipboard_getter(line):
            continue
        if rule_id == "system.location.getter.v1" and not _looks_like_location_getter(line):
            continue
        if rule_id == "system.telephony.getter.v1" and not _looks_like_telephony_getter(line):
            continue
        if re.search(pattern, line):
            out.append(
                RuleCandidate(
                    source_kind=SourceKind.PLATFORM_API,
                    source_subtype=subkind,
                    rule_id=rule_id,
                    file_path=file_path,
                    line_number=line_number,
                    classname=classname,
                    method=method,
                    statement=statement,
                    description=description,
                    boundary_rank=1,
                    boundary_subject=_receiver_subject(line, "getLastKnownLocation")
                    or _receiver_subject(line, "getPrimaryClip")
                    or _receiver_subject(line, "primaryClip")
                    or _receiver_subject(line, "getAccounts")
                    or _receiver_subject(line, "getPackageInfo")
                    or _receiver_subject(line, "getInstalledPackages")
                    or _receiver_subject(line, "getDeviceId")
                    or _receiver_subject(line, "getSubscriberId")
                    or _receiver_subject(line, "getLine1Number"),
                )
            )

    return out
