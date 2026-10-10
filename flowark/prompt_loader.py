"""Prompt 统一加载模块。

提供统一的 prompt 读取接口，支持版本管理和模板变量替换。
"""

import re
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_ARCHIVED_DIR_NAME = "archived"


def _iter_prompt_files() -> list[Path]:
    """遍历当前可用 prompt 文件，跳过冷存档目录。"""
    if not _PROMPTS_DIR.exists():
        return []
    return [
        path
        for path in _PROMPTS_DIR.rglob("*.md")
        if path.is_file() and _ARCHIVED_DIR_NAME not in path.relative_to(_PROMPTS_DIR).parts
    ]


def _parse_version_number(version: str) -> int:
    """从版本字符串中提取数值版本号。"""
    match = re.fullmatch(r"v(\d+)", str(version).strip())
    if not match:
        raise ValueError(f"Invalid prompt version: {version}")
    return int(match.group(1))


def _resolve_prompt_path(name: str, version: str | None = None) -> Path | None:
    """解析 prompt 文件路径。

    Args:
        name: prompt 名称（不含版本和扩展名）
        version: 指定版本（如 "v1"），为 None 时自动选择最新版本

    Returns:
        prompt 文件路径，不存在时返回 None
    """
    if not _PROMPTS_DIR.exists():
        return None

    if version:
        # 指定版本
        prompt_filename = f"{name}-{version}.md"
        candidates = sorted(
            path for path in _iter_prompt_files() if path.name == prompt_filename
        )
        return candidates[0] if candidates else None

    # 匹配 {name}-v{major}.md 格式
    pattern = re.compile(rf"^{re.escape(name)}-v(\d+)\.md$")
    versions: list[tuple[int, Path]] = []

    for file in _iter_prompt_files():
        match = pattern.match(file.name)
        if match:
            major = int(match.group(1))
            versions.append((major, file))

    if not versions:
        return None

    # 返回版本号最大的
    versions.sort(key=lambda x: x[0], reverse=True)
    return versions[0][1]


def get_prompt(name: str, version: str | None = None, **kwargs) -> str:
    """获取原始 prompt 模板内容。

    Args:
        name: prompt 名称（如 "knowledge_synth"、"final_report"）
        version: 指定版本（如 "v1"），为 None 时自动选择最新版本

    Returns:
        原始 prompt 模板字符串

    Raises:
        FileNotFoundError: prompt 文件不存在
        TypeError: 调用方误把 get_prompt 当作模板渲染接口使用
    """
    if kwargs:
        keys = ", ".join(sorted(kwargs.keys()))
        raise TypeError(
            f"get_prompt() 只负责读取原始模板，不再接收模板变量（收到: {keys}）。"
            "请改用 render_prompt(..., **kwargs)。"
        )

    path = _resolve_prompt_path(name, version)
    if path is None:
        raise FileNotFoundError(f"Prompt not found: {name} (version={version or 'latest'})")

    return path.read_text(encoding="utf-8")


def render_prompt(name: str, version: str | None = None, **kwargs) -> str:
    """渲染 prompt 模板。

    Args:
        name: prompt 名称（如 "knowledge_synth"、"final_report"）
        version: 指定版本（如 "v1"），为 None 时自动选择最新版本
        **kwargs: 模板变量，用于 .format() 风格的变量替换

    Returns:
        渲染后的 prompt 内容字符串

    Raises:
        FileNotFoundError: prompt 文件不存在
        KeyError: 缺少必需模板变量
    """
    content = get_prompt(name, version=version)
    return content.format(**kwargs)


def list_prompts() -> list[dict]:
    """列出所有可用的 prompt。

    Returns:
        prompt 信息列表，每个元素包含 name、version、path
    """
    pattern = re.compile(r"^(.+)-v(\d+)\.md$")
    prompts: dict[str, list[dict]] = {}

    for file in _iter_prompt_files():
        match = pattern.match(file.name)
        if match:
            name = match.group(1)
            version = f"v{match.group(2)}"
            if name not in prompts:
                prompts[name] = []
            prompts[name].append({
                "name": name,
                "version": version,
                "path": str(file.relative_to(_PROMPTS_DIR.parent.parent)),
            })

    # 每个名称按版本号排序
    result: list[dict] = []
    for name in sorted(prompts.keys()):
        versions = sorted(
            prompts[name],
            key=lambda x: _parse_version_number(x["version"]),
            reverse=True,
        )
        result.extend(versions)

    return result
