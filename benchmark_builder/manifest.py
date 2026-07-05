"""Android manifest helpers for benchmark_builder v2."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import xml.etree.ElementTree as ET

ANDROID_NS = "{http://schemas.android.com/apk/res/android}"
_COMPONENT_TAGS = {"activity", "activity-alias", "receiver", "service"}


@dataclass(frozen=True)
class ManifestComponent:
    qualified_name: str
    simple_name: str
    tag: str
    exported: bool
    has_intent_filter: bool
    manifest_path: str


@dataclass(frozen=True)
class ManifestIndex:
    external_components: tuple[ManifestComponent, ...]
    external_qualified_names: frozenset[str]
    external_simple_names: frozenset[str]

    def is_external_entry_class(self, package_name: str | None, class_name: str | None) -> bool:
        if not class_name:
            return False
        if package_name:
            qualified = f"{package_name}.{class_name}"
            if qualified in self.external_qualified_names:
                return True
        return class_name in self.external_simple_names


def build_manifest_index(source_dir: Path, excluded_dirs: set[str]) -> ManifestIndex:
    components: list[ManifestComponent] = []
    excluded_lower = {directory.lower() for directory in excluded_dirs}
    for manifest_path in sorted(source_dir.rglob("AndroidManifest.xml")):
        relative_path = manifest_path.relative_to(source_dir)
        if any(part.lower() in excluded_lower for part in relative_path.parts[:-1]):
            continue
        components.extend(_parse_manifest(source_dir, manifest_path))
    external = tuple(component for component in components if component.exported)
    return ManifestIndex(
        external_components=external,
        external_qualified_names=frozenset(component.qualified_name for component in external),
        external_simple_names=frozenset(component.simple_name for component in external),
    )


def _parse_manifest(source_dir: Path, manifest_path: Path) -> list[ManifestComponent]:
    try:
        tree = ET.parse(manifest_path)
    except ET.ParseError:
        return []
    root = tree.getroot()
    manifest_package = root.attrib.get("package", "")
    relative_manifest = manifest_path.relative_to(source_dir).as_posix()
    components: list[ManifestComponent] = []
    for element in root.iter():
        tag = _strip_namespace(element.tag)
        if tag not in _COMPONENT_TAGS:
            continue
        raw_name = element.attrib.get(f"{ANDROID_NS}name")
        if not raw_name:
            continue
        exported = element.attrib.get(f"{ANDROID_NS}exported") == "true"
        has_intent_filter = any(_strip_namespace(child.tag) == "intent-filter" for child in list(element))
        qualified_name = _resolve_component_name(raw_name, manifest_package)
        components.append(
            ManifestComponent(
                qualified_name=qualified_name,
                simple_name=qualified_name.rsplit(".", 1)[-1],
                tag=tag,
                exported=exported,
                has_intent_filter=has_intent_filter,
                manifest_path=relative_manifest,
            )
        )
    return components


def _strip_namespace(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _resolve_component_name(name: str, manifest_package: str) -> str:
    if name.startswith(".") and manifest_package:
        return f"{manifest_package}{name}"
    if "." not in name and manifest_package:
        return f"{manifest_package}.{name}"
    return name
