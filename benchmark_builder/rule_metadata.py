"""Rule metadata for benchmark_builder v3.2 source inventory."""

from __future__ import annotations

from dataclasses import dataclass

from benchmark_builder.schemas import SourceKind


@dataclass(frozen=True)
class RuleMetadata:
    source_kind: SourceKind
    source_subtype: str
    boundary_type: str
    alignment_tier: str
    literature_basis: tuple[str, ...]


RULE_METADATA: dict[str, RuleMetadata] = {
    "ui.code.text_getter.v1": RuleMetadata(
        source_kind=SourceKind.UI_INPUT,
        source_subtype="text_widget_value",
        boundary_type="ui_widget_getter",
        alignment_tier="supor_ui_extension",
        literature_basis=("SUPOR", "DroidBench/FlowDroid UI source mechanisms"),
    ),
    "ui.code.checked_value.v1": RuleMetadata(
        source_kind=SourceKind.UI_INPUT,
        source_subtype="checked_widget_value",
        boundary_type="ui_widget_getter",
        alignment_tier="supor_ui_extension",
        literature_basis=("SUPOR", "DroidBench/FlowDroid UI source mechanisms"),
    ),
    "ui.compose.on_value_change.v1": RuleMetadata(
        source_kind=SourceKind.UI_INPUT,
        source_subtype="compose_value_change",
        boundary_type="callback_param",
        alignment_tier="supor_ui_extension",
        literature_basis=("SUPOR", "DroidBench/FlowDroid UI source mechanisms"),
    ),
    "app_entry.intent_extra.v1": RuleMetadata(
        source_kind=SourceKind.ICC_PAYLOAD,
        source_subtype="intent_extra",
        boundary_type="component_entry_api",
        alignment_tier="icc_extension",
        literature_basis=("IccTA", "ICC-Bench"),
    ),
    "app_entry.bundle_read.v1": RuleMetadata(
        source_kind=SourceKind.ICC_PAYLOAD,
        source_subtype="bundle_value",
        boundary_type="component_entry_api",
        alignment_tier="icc_extension",
        literature_basis=("IccTA", "ICC-Bench"),
    ),
    "app_entry.uri_query.v1": RuleMetadata(
        source_kind=SourceKind.ICC_PAYLOAD,
        source_subtype="uri_query_parameter",
        boundary_type="component_entry_api",
        alignment_tier="icc_extension",
        literature_basis=("IccTA", "ICC-Bench"),
    ),
    "remote.callback.remote_message.v1": RuleMetadata(
        source_kind=SourceKind.REMOTE_PAYLOAD,
        source_subtype="push_message",
        boundary_type="callback_param",
        alignment_tier="susi_extension",
        literature_basis=("SuSi network category", "TaintBench-style source inventory"),
    ),
    "remote.callback.data_snapshot.v1": RuleMetadata(
        source_kind=SourceKind.REMOTE_PAYLOAD,
        source_subtype="firebase_snapshot",
        boundary_type="callback_param",
        alignment_tier="susi_extension",
        literature_basis=("SuSi network category", "TaintBench-style source inventory"),
    ),
    "remote.callback.document_snapshot.v1": RuleMetadata(
        source_kind=SourceKind.REMOTE_PAYLOAD,
        source_subtype="firestore_document_snapshot",
        boundary_type="callback_param",
        alignment_tier="susi_extension",
        literature_basis=("SuSi network category", "TaintBench-style source inventory"),
    ),
    "remote.callback.query_snapshot.v1": RuleMetadata(
        source_kind=SourceKind.REMOTE_PAYLOAD,
        source_subtype="firestore_query_snapshot",
        boundary_type="callback_param",
        alignment_tier="susi_extension",
        literature_basis=("SuSi network category", "TaintBench-style source inventory"),
    ),
    "remote.callback.response.v1": RuleMetadata(
        source_kind=SourceKind.REMOTE_PAYLOAD,
        source_subtype="http_response_callback",
        boundary_type="callback_param",
        alignment_tier="susi_extension",
        literature_basis=("SuSi network category", "TaintBench-style source inventory"),
    ),
    "remote.response.body.v1": RuleMetadata(
        source_kind=SourceKind.REMOTE_PAYLOAD,
        source_subtype="http_response_body",
        boundary_type="response_body_call",
        alignment_tier="susi_extension",
        literature_basis=("SuSi network category", "TaintBench-style source inventory"),
    ),
    "remote.response.body_stream.v1": RuleMetadata(
        source_kind=SourceKind.REMOTE_PAYLOAD,
        source_subtype="http_response_body_stream",
        boundary_type="response_body_call",
        alignment_tier="susi_extension",
        literature_basis=("SuSi network category", "TaintBench-style source inventory"),
    ),
    "remote.parse.json_object.v1": RuleMetadata(
        source_kind=SourceKind.REMOTE_PAYLOAD,
        source_subtype="json_object_parse",
        boundary_type="parser_call",
        alignment_tier="susi_extension",
        literature_basis=("SuSi network category", "TaintBench-style source inventory"),
    ),
    "remote.parse.json_array.v1": RuleMetadata(
        source_kind=SourceKind.REMOTE_PAYLOAD,
        source_subtype="json_array_parse",
        boundary_type="parser_call",
        alignment_tier="susi_extension",
        literature_basis=("SuSi network category", "TaintBench-style source inventory"),
    ),
    "remote.parse.gson_from_json.v1": RuleMetadata(
        source_kind=SourceKind.REMOTE_PAYLOAD,
        source_subtype="gson_from_json",
        boundary_type="parser_call",
        alignment_tier="susi_extension",
        literature_basis=("SuSi network category", "TaintBench-style source inventory"),
    ),
    "remote.parse.decode_from_string.v1": RuleMetadata(
        source_kind=SourceKind.REMOTE_PAYLOAD,
        source_subtype="decode_from_string",
        boundary_type="parser_call",
        alignment_tier="susi_extension",
        literature_basis=("SuSi network category", "TaintBench-style source inventory"),
    ),
    "remote.parse.parse_from.v1": RuleMetadata(
        source_kind=SourceKind.REMOTE_PAYLOAD,
        source_subtype="parse_from",
        boundary_type="parser_call",
        alignment_tier="susi_extension",
        literature_basis=("SuSi network category", "TaintBench-style source inventory"),
    ),
    "local.preferences.getter.v1": RuleMetadata(
        source_kind=SourceKind.PERSISTENT_STORAGE,
        source_subtype="shared_preferences",
        boundary_type="storage_api_call",
        alignment_tier="susi_extension",
        literature_basis=("SuSi file/database categories", "TaintBench-style source inventory"),
    ),
    "local.cursor.getter.v1": RuleMetadata(
        source_kind=SourceKind.PERSISTENT_STORAGE,
        source_subtype="database_cursor",
        boundary_type="storage_api_call",
        alignment_tier="susi_core",
        literature_basis=("SuSi database category", "TaintBench-style source inventory"),
    ),
    "local.file.input_stream.v1": RuleMetadata(
        source_kind=SourceKind.PERSISTENT_STORAGE,
        source_subtype="file_input_stream",
        boundary_type="storage_api_call",
        alignment_tier="susi_core",
        literature_basis=("SuSi file category", "TaintBench-style source inventory"),
    ),
    "local.file.read.v1": RuleMetadata(
        source_kind=SourceKind.PERSISTENT_STORAGE,
        source_subtype="file_read",
        boundary_type="storage_api_call",
        alignment_tier="susi_core",
        literature_basis=("SuSi file category", "TaintBench-style source inventory"),
    ),
    "local.dao.return.v1": RuleMetadata(
        source_kind=SourceKind.PERSISTENT_STORAGE,
        source_subtype="dao_return",
        boundary_type="app_wrapper_call",
        alignment_tier="app_wrapper_extension",
        literature_basis=("DAISY app/library-defined source discovery", "TaintBench-style source inventory"),
    ),
    "system.callback.sensor_event.v1": RuleMetadata(
        source_kind=SourceKind.PLATFORM_API,
        source_subtype="sensor_event",
        boundary_type="callback_param",
        alignment_tier="susi_core",
        literature_basis=("SuSi sensor category", "DroidBench/FlowDroid callback mechanisms"),
    ),
    "system.settings.getter.v1": RuleMetadata(
        source_kind=SourceKind.PLATFORM_API,
        source_subtype="settings",
        boundary_type="api_call",
        alignment_tier="susi_core",
        literature_basis=("SuSi settings category", "Stowaway/PScout Android API universe"),
    ),
    "system.clipboard.getter.v1": RuleMetadata(
        source_kind=SourceKind.PLATFORM_API,
        source_subtype="clipboard",
        boundary_type="api_call",
        alignment_tier="susi_core",
        literature_basis=("SuSi framework source taxonomy", "Stowaway/PScout Android API universe"),
    ),
    "system.location.getter.v1": RuleMetadata(
        source_kind=SourceKind.PLATFORM_API,
        source_subtype="location",
        boundary_type="api_call",
        alignment_tier="susi_core",
        literature_basis=("SuSi location category", "Stowaway/PScout Android API universe"),
    ),
    "system.location.field_access.v1": RuleMetadata(
        source_kind=SourceKind.PLATFORM_API,
        source_subtype="location",
        boundary_type="field_access",
        alignment_tier="susi_core",
        literature_basis=("SuSi location category", "Stowaway/PScout Android API universe"),
    ),
    "system.network_state.getter.v1": RuleMetadata(
        source_kind=SourceKind.PLATFORM_API,
        source_subtype="network_state",
        boundary_type="api_call",
        alignment_tier="susi_core",
        literature_basis=("SuSi network category", "Stowaway/PScout Android API universe"),
    ),
    "system.account.getter.v1": RuleMetadata(
        source_kind=SourceKind.PLATFORM_API,
        source_subtype="account",
        boundary_type="api_call",
        alignment_tier="susi_core",
        literature_basis=("SuSi account category", "Stowaway/PScout Android API universe"),
    ),
    "system.package_info.getter.v1": RuleMetadata(
        source_kind=SourceKind.PLATFORM_API,
        source_subtype="package_info",
        boundary_type="api_call",
        alignment_tier="susi_core",
        literature_basis=("SuSi framework source taxonomy", "Stowaway/PScout Android API universe"),
    ),
    "system.telephony.getter.v1": RuleMetadata(
        source_kind=SourceKind.PLATFORM_API,
        source_subtype="telephony",
        boundary_type="api_call",
        alignment_tier="susi_core",
        literature_basis=("SuSi telephony/identifier categories", "Stowaway/PScout Android API universe"),
    ),
    "system.build.info.v1": RuleMetadata(
        source_kind=SourceKind.PLATFORM_API,
        source_subtype="build_info",
        boundary_type="api_call",
        alignment_tier="susi_core",
        literature_basis=("SuSi framework source taxonomy", "Stowaway/PScout Android API universe"),
    ),
}


def metadata_for_rule(rule_id: str) -> RuleMetadata:
    try:
        return RULE_METADATA[rule_id]
    except KeyError as exc:
        raise ValueError(f"missing benchmark_builder rule metadata: {rule_id}") from exc
