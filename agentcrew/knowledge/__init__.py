"""成功经验沉淀与跨 run 知识复用（P3-5 / P3-7 / R3 实体记忆）。

一次 run 成功完成后，把「任务描述 → 解决方案 / 关键决策 / 产物」的映射从本 run
提取出来，追加到 ``output_dir/knowledge/experiences.jsonl`` —— 它**不**在任何单个
run 目录之下，而是全局共享：下一次 run 的规划阶段可以据此检索相关历史经验并注入
上下文，把「上次怎么做成的」变成「这次开局就知道」。

R3 追加 ``entities.jsonl``：实体记忆补上「这个项目里出现的名字」（技术选型、文件
路径、角色、缩写、概念），同样全局共享、跨 run 累积，规划阶段与历史经验并列注入。
"""

from agentcrew.knowledge.entity import (
    EntityMemoryStore,
    EntityRecord,
    entities_from_experience,
    extract_entities,
)
from agentcrew.knowledge.experience import Experience, ExperienceStore

__all__ = [
    "Experience", "ExperienceStore",
    "EntityRecord", "EntityMemoryStore",
    "extract_entities", "entities_from_experience",
]
