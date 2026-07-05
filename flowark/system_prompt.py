"""系统提示词模块。"""

import re

from flowark.prompt_loader import get_prompt, render_prompt


def _render_system_prompt(*, include_knowledge_guidance: bool) -> str:
    kwargs = {
        "knowledge_capability_block": "",
        "knowledge_section_block": "",
    }
    if include_knowledge_guidance:
        kwargs.update(
            {
                "knowledge_capability_block": get_prompt("flowark_system_knowledge_capability").strip(),
                "knowledge_section_block": get_prompt("flowark_system_knowledge_section").strip(),
            }
        )
    rendered = render_prompt("flowark_system", **kwargs).rstrip()
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    return rendered + "\n"


def get_flowark_prompt() -> str:
    """获取启用知识指导的系统提示词追加内容。"""
    return _render_system_prompt(include_knowledge_guidance=True)


def get_naive_prompt() -> str:
    """获取朴素基线模式的系统提示词。

    目标是与 FlowArk 保持基本一致的分析约束，只去掉知识相关提示，
    从而把对比变量收敛到知识注入/复用机制本身。
    """
    return _render_system_prompt(include_knowledge_guidance=False)
