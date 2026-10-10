"""FlowArk 运行器子包。

目录心智模型：
- `facade.py`: 对外入口 `FlowArkRunner` 与主流程编排
- `session.py`: Claude SDK turn 执行、限流、usage 汇总
- `reporting.py`: final report / eval summary 分支
- `knowledge_synth.py`: auto knowledge synth 生成与解析
- `knowledge_pipeline.py`: validation / apply / background worker
"""

from __future__ import annotations

import subprocess

from .facade import FlowArkRunner

__all__ = ["FlowArkRunner"]
